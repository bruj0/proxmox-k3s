"""Cluster topology writer -- emits infra/clusters/<name>/output.json.

As of 2026-07-09 the SS2 -> SS3 contract `output.json` is written by
the bootstrap dispatcher (this module), NOT by a `local_sensitive_file`
in tofu. The resource was removed because tofu's snapshot drifted the
moment SDN auto-allocated IPs and the bootstrap discovered the live
values via qemu-guest-agent. Two writers with different refresh
cadences is a recipe for drift. The bootstrap is the sole writer.

Inputs the writer needs (static, declared in cluster's main.tf):
  - cluster_name, pod_cidr, svc_cidr, cluster_dns -- read from
    `module "X" { source = "../../modules/proxmox-k3s-cluster" ... }`
    in infra/clusters/<name>/main.tf.
  - vmid_start -- ditto.
  - per-VM role/name -- derived from control_plane.count + workers.count.

Inputs the writer needs (live, post-apply):
  - per-VM IP -- discovered via PVE qemu-guest-agent
    /nodes/<node>/qemu/<vmid>/agent/network-get-interfaces, against
    the public PVE API at kvm.bruj0.net:8006.

Output (infra/clusters/<name>/output.json, mode 0600):
  {
    "cluster_name":        "apps",
    "vnet_bridge":         "vnet0",
    "control_plane_count": 1,
    "worker_count":        1,
    "pod_cidr":            "172.20.0.0/16",
    "svc_cidr":            "172.21.0.0/16",
    "cluster_dns":         "172.21.0.10",
    "nodes": [
      {"role": "control_plane", "name": "apps-cp-1", "vmid": 210, "ip": "10.44.0.223"},
      {"role": "worker",         "name": "apps-w-1", "vmid": 211, "ip": "10.44.1.25"},
    ],
  }

Public API:
  write_output_json(cluster_dir, *, cluster_name, pve_token, force=False)
    -- idempotent. If output.json exists and its ip fields are non-empty,
       it is left untouched (unless force=True). Static fields (CIDRs,
       cluster_dns, vnet_bridge, role names, VMIDs) are always refreshed
       from main.tf.

  discover_via_pve_api(token) -> dict[int, list[str]]
    -- per-VMID list of IPv4 addresses. Helper exposed for tests.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Default PVE endpoint -- kvm.bruj0.net is publicly reachable.
DEFAULT_PVE = "https://kvm.bruj0.net:8006/api2/json"
PVE_NODE = "BigBertha"


@dataclass(frozen=True)
class _ModuleField:
    """One parsed literal from the `module "X" { ... }` block in main.tf."""

    name: str
    value: str


def _extract_module_body(tf_text: str, module_keyword: str) -> str | None:
    """Locate `module "<module_keyword>" { ... }` and return its inner body.

    Tracks brace depth so `control_plane = { count = 1 }` is captured as
    part of the module body. Strips HCL string literals and `# ...` line
    comments so a `{` inside a string or comment doesn't confuse the
    counter.
    """
    header_pat = re.compile(
        rf'\bmodule\s+"{re.escape(module_keyword)}"\s*\{{',
        re.MULTILINE,
    )
    header = header_pat.search(tf_text)
    if not header:
        return None
    i = header.end()
    depth = 1
    in_string = False
    string_delim = ""
    while i < len(tf_text) and depth > 0:
        c = tf_text[i]
        # Strip HCL line comments outside strings.
        if not in_string and c == "#" and (i == 0 or tf_text[i - 1] == "\n"):
            j = tf_text.find("\n", i)
            i = -1 if j == -1 else j + 1
            if i == -1:
                break
            continue
        if not in_string and c == '"':
            in_string = True
            string_delim = c
        elif in_string and c == string_delim and tf_text[i - 1] != "\\":
            in_string = False
        elif not in_string and c == "{":
            depth += 1
        elif not in_string and c == "}":
            depth -= 1
        i += 1
    if depth != 0:
        return None
    return tf_text[header.end() : i - 1]


def _parse_module_literal_block(tf_text: str, module_keyword: str) -> list[_ModuleField]:
    """Return the module's top-level `key = "value"` or `key = <int>` literals."""
    body = _extract_module_body(tf_text, module_keyword)
    if body is None:
        return []

    fields: list[_ModuleField] = []
    for fm in re.finditer(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*("[^"]*"|\d+)\s*\n', body):
        raw = fm.group(2)
        if raw.startswith('"') and raw.endswith('"'):
            fields.append(_ModuleField(name=fm.group(1), value=raw[1:-1]))
        else:
            fields.append(_ModuleField(name=fm.group(1), value=raw))
    return fields


def _read_main_tf(cluster_dir: Path) -> str:
    main_tf = cluster_dir / "main.tf"
    if not main_tf.exists():
        raise FileNotFoundError(f"cluster root has no main.tf: {cluster_dir}")
    return main_tf.read_text(encoding="utf-8")


def _module_keyword(cluster_name: str, main_tf_text: str) -> str:
    """Return the literal `module "X" { source ... }` key for this cluster.

    Defaults to cluster_name; falls back to a regex sweep if main.tf uses
    a different identifier for the cluster module.
    """
    m = re.search(
        r'\bmodule\s+"([^"]+)"\s*\{[^}]*source\s*=\s*"[^"]*proxmox-k3s-cluster',
        main_tf_text,
    )
    if m:
        return m.group(1)
    return cluster_name


def _parse_nested_count(tf_text: str, module_key: str, child: str) -> int:
    """Find `<child> = { count = N ... }` inside the module and return int(N).

    Defaults to 1 if the field is absent or unbalanced -- the cluster
    module treats `count = 1` as the sensible default.
    """
    body = _extract_module_body(tf_text, module_key)
    if not body:
        return 1
    cm = re.search(rf'\b{re.escape(child)}\s*=\s*\{{', body)
    if not cm:
        return 1
    depth = 1
    j = cm.end()
    while j < len(body) and depth > 0:
        c = body[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        j += 1
    inner = body[cm.end() : j - 1]
    nm = re.search(r"\bcount\s*=\s*(\d+)", inner)
    if not nm:
        return 1
    return int(nm.group(1))


def _resolve_runtime_cluster_topology(
    cluster_dir: Path, cluster_name: str
) -> dict[str, Any]:
    """Read infra/clusters/<name>/main.tf and extract the static topology.

    Returns the JSON-shaped dict ready to be written into output.json,
    with `nodes[*].ip` left empty.
    """
    tf_text = _read_main_tf(cluster_dir)
    module_key = _module_keyword(cluster_name, tf_text)
    fields_by_name = {f.name: f.value for f in _parse_module_literal_block(tf_text, module_key)}

    def _need(name: str) -> str:
        if name not in fields_by_name:
            raise KeyError(
                f"main.tf module '{module_key}' has no field '{name}' "
                f"(expected: cluster_name, pod_cidr, svc_cidr, cluster_dns, "
                f"vnet_bridge, control_plane.count, workers.count, vmid_start)"
            )
        return fields_by_name[name]

    cluster_name_out = _need("cluster_name")
    pod_cidr = _need("pod_cidr")
    svc_cidr = _need("svc_cidr")
    cluster_dns = _need("cluster_dns")
    vnet_bridge = _need("vnet_bridge")
    vmid_start = int(_need("vmid_start"))

    cp_count = _parse_nested_count(tf_text, module_key, "control_plane")
    wk_count = _parse_nested_count(tf_text, module_key, "workers")

    nodes: list[dict[str, Any]] = []
    for i in range(cp_count):
        nodes.append({
            "role": "control_plane",
            "name": f"{cluster_name_out}-cp-{i + 1}",
            "vmid": vmid_start + i,
            "ip": "",
        })
    for i in range(wk_count):
        nodes.append({
            "role": "worker",
            "name": f"{cluster_name_out}-w-{i + 1}",
            "vmid": vmid_start + cp_count + i,
            "ip": "",
        })

    return {
        "cluster_name": cluster_name_out,
        "vnet_bridge": vnet_bridge,
        "control_plane_count": cp_count,
        "worker_count": wk_count,
        "pod_cidr": pod_cidr,
        "svc_cidr": svc_cidr,
        "cluster_dns": cluster_dns,
        "nodes": nodes,
    }


def _pve_list_vms(token: str) -> list[dict[str, Any]]:
    """Return /cluster/resources?type=vm filtered to the apps/cicd runs."""
    req = urllib.request.Request(
        f"{DEFAULT_PVE}/cluster/resources?type=vm",
        headers={"Authorization": f"PVEAPIToken={token}"},
    )
    raw = json.loads(urllib.request.urlopen(req, timeout=10).read())["data"]
    if not isinstance(raw, list):
        return []
    return [
        v
        for v in raw
        if isinstance(v, dict)
        and ("apps" in v.get("name", "") or "cicd" in v.get("name", ""))
    ]


def discover_via_pve_api(token: str) -> dict[int, list[str]]:
    """Discover per-VMID IPv4 addresses via the public PVE API + qemu-guest-agent.

    Returns ``{<vmid>: [<ip>, ...]}``. The writer correlates each entry
    back to its declared role/name via the cluster's main.tf (or by
    PVE-side VM naming if the VMID-to-name mapping diverges from the
    declared topo).
    """
    out: dict[int, list[str]] = {}
    for vmid, _name in _pve_list_vms_with_names(token):
        if vmid is None:
            continue
        node_url = PVE_NODE
        url = f"{DEFAULT_PVE}/nodes/{node_url}/qemu/{vmid}/agent/network-get-interfaces"
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"PVEAPIToken={token}"}
            )
            body = json.loads(urllib.request.urlopen(req, timeout=10).read())
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            KeyError,
        ):
            out[int(vmid)] = []
            continue
        data = body.get("data")
        if isinstance(data, dict):
            result = data.get("result", [])
        elif isinstance(data, list):
            result = data
        else:
            result = []
        ips: list[str] = []
        for iface in result:
            if not isinstance(iface, dict):
                continue
            for ip in iface.get("ip-addresses", []):
                if (
                    isinstance(ip, dict)
                    and ip.get("ip-address-type") == "ipv4"
                    and ip.get("ip-address")
                    and not ip["ip-address"].startswith("127.")
                ):
                    ips.append(ip["ip-address"])
        out[int(vmid)] = ips
    return out


def _pve_list_vms_with_names(token: str) -> list[tuple[int | None, str]]:
    """Return ``[(vmid, name)]`` filtered to the apps/cicd runs.

    Helper for the writer's VMID-to-name correlation. Used in
    preference to ``_pve_list_vms`` because consumers want both.
    """
    req = urllib.request.Request(
        f"{DEFAULT_PVE}/cluster/resources?type=vm",
        headers={"Authorization": f"PVEAPIToken={token}"},
    )
    raw = json.loads(urllib.request.urlopen(req, timeout=10).read())["data"]
    if not isinstance(raw, list):
        return []
    out: list[tuple[int | None, str]] = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        n = v.get("name", "")
        if "apps" in n or "cicd" in n:
            out.append((v.get("vmid"), n))
    return out


def write_output_json(
    cluster_dir: Path,
    *,
    cluster_name: str,
    pve_token: str,
    force: bool = False,
) -> Path:
    """Materialise infra/clusters/<name>/output.json.

    Idempotent by design:
      1. Resolve static topology (CIDRs, role names) from main.tf.
      2. Correlate per-node VMID + IP from the live PVE API (the
         authoritative source: Proxmox SDN auto-allocates IPs and the
         bpg/proxmox provider's `vmid_start` is only a minimum, not an
         assignment, so the live VMIDs may not equal `vmid_start + idx`).
      3. If existing output.json has non-empty IPs and force=False,
         keep its per-node IP values (trust the last successful write).

    Returns the path to the written file.
    """
    output_json = cluster_dir / "output.json"
    static = _resolve_runtime_cluster_topology(cluster_dir, cluster_name)

    # Carry-over previously-known IPs (idempotent re-runs).
    prior_ips: dict[str, str] = {}
    if output_json.exists() and not force:
        try:
            existing = json.loads(output_json.read_text())
            for n in existing.get("nodes", []):
                if isinstance(n, dict) and n.get("ip"):
                    prior_ips[n["name"]] = n["ip"]
        except (json.JSONDecodeError, KeyError):
            prior_ips = {}

    needs_discovery = any(not n["ip"] for n in static["nodes"]) or force
    live_vms: list[tuple[int | None, str]] = []
    discovered: dict[int, list[str]] = {}
    if needs_discovery:
        live_vms = _pve_list_vms_with_names(pve_token)
        discovered = discover_via_pve_api(pve_token)

    # Map declared node names -> live PVE (vmid, ips).
    name_to_vmid: dict[str, int] = {
        n: vmid
        for vmid, n in live_vms
        if n and vmid is not None
    }

    for n in static["nodes"]:
        if n["ip"]:
            continue
        if n["name"] in prior_ips and prior_ips[n["name"]]:
            n["ip"] = prior_ips[n["name"]]
            continue
        # Prefer the live-PVE VMID for this declared name over the
        # declared vmid_start + idx ordering. Proxmox may have
        # allocated non-contiguous VMIDs depending on which slots
        # were free at the time of `tofu apply`.
        vmid = name_to_vmid.get(n["name"], n["vmid"])
        ips = discovered.get(int(vmid), [])
        n["ip"] = ips[0] if ips else ""
        # Also reconcile the static VMID with the live one if it
        # differs (the writer is the canonical source post-apply).
        if int(vmid) != n["vmid"]:
            n["vmid"] = int(vmid)

    cluster_dir.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(static, indent=2) + "\n")
    output_json.chmod(0o600)
    return output_json
