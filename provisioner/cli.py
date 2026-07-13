"""provisioner CLI — `bootstrap plan|apply|destroy|validate|kubeconfig <cluster>`.

Subcommands (mirrors proxmox-vms/provisioner/cli.py):

  bootstrap plan        <cluster>     # diff desired vs live cluster (no mutations)
  bootstrap apply       <cluster>     # run every selected phase; idempotent
  bootstrap destroy     <cluster>     # remove the cluster's state
  bootstrap validate    <cluster>     # parse main.tf + verify upstream output.json
  bootstrap kubeconfig  <cluster>     # fetch + merge the admin kubeconfig into the
                                       # operator's local kubeconf (~/.kube/config)
                                       # (default uses CP's internal IP; --use-tunnel
                                       # routes through the SSH port-forward; --output
                                       # writes to a standalone file instead)

Exit codes (mirrors proxmox-vms):
   0  success
   2  prerequisite failure
   3  phase failure
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import yaml
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROVISIONER_DIR = Path(__file__).resolve().parent
REPO_ROOT_DEFAULT = PROVISIONER_DIR.parent
# Make `provisioner` importable when running as `python provisioner/cli.py`
# (the install entry point uses `provisioner.cli:main` so the package
# is already on sys.path; this is just belt-and-suspenders).
if str(PROVISIONER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PROVISIONER_DIR.parent))

from provisioner.lib.container import Container, FileStateStore  # noqa: E402
from provisioner.lib.log import StructuredLogger  # noqa: E402
from provisioner.lib.orchestrator import (  # noqa: E402
    BootstrapPlan,
    attach_intent,
    build_topology,
    parse_intent,
    run as run_orchestrator,
)
# noqa: I001
from provisioner.lib.phases import all_phases  # noqa: E402
from provisioner.lib.protocols import BootstrapError  # noqa: E402

EXIT_OK = 0
EXIT_PREREQ = 2
EXIT_PHASE = 3


@dataclass(frozen=True)
class CliContext:
    cluster: str
    repo_root: Path
    cluster_dir: Path
    proxmox_vms_repo: Path
    ssh_key: Path | None
    log: StructuredLogger
    audit_log_path: Path


def _load_env_file(path: Path, logger: StructuredLogger) -> int:
    """KEY=VALUE per line; os.environ.setdefault so shell exports win."""
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
        count += 1
    logger.info(step="env_loaded", path=str(path), count=count)
    return count


def _resolve_ctx(args: argparse.Namespace, *, subcommand: str) -> CliContext:
    repo_root = (args.repo_root or REPO_ROOT_DEFAULT).resolve()
    cluster_dir = (repo_root / "infra" / "clusters" / args.cluster).resolve()
    proxmox_vms_repo = (args.proxmox_vms_repo or (repo_root.parent / "proxmox-vms")).resolve()
    ssh_key = Path(args.ssh_key).expanduser() if args.ssh_key else None

    audit_dir = repo_root / "logs"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    audit_log_path = audit_dir / f"{subcommand}_{args.cluster}_{stamp}.audit.jsonl"
    log = StructuredLogger(name=f"{subcommand}_{args.cluster}", log_path=audit_log_path)
    _load_env_file(repo_root / ".env", log)

    return CliContext(
        cluster=args.cluster,
        repo_root=repo_root,
        cluster_dir=cluster_dir,
        proxmox_vms_repo=proxmox_vms_repo,
        ssh_key=ssh_key,
        log=log,
        audit_log_path=audit_log_path,
    )


def _print_plan_table(plan: BootstrapPlan) -> None:
    """Human-readable plan summary."""
    print(f"Cluster: {plan.cluster_name}")
    print(f"Phases run: {len(plan.phases)}")
    print()
    print(f"{'PHASE':<22} {'STATUS':<10}  DATA")
    print(f"{'------':<22} {'------':<10}  ----")
    for name, r in plan.phases:
        status = "skipped" if r.skipped else ("done" if r.changed else "noop")
        data = json.dumps(r.data)[:80] if r.data else ""
        print(f"{name:<22} {status:<10}  {data}")
    print()
    counts = plan.summary
    print(
        "Summary: "
        + ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        + (" (no changes)" if all(v == 0 for v in counts.values()) else "")
    )


# ------------------------------------------------------------- subcommands

def cmd_plan(args: argparse.Namespace) -> int:
    ctx = _resolve_ctx(args, subcommand="plan")
    if not ctx.cluster_dir.exists():
        ctx.log.error(step="prereq_failed", error=f"cluster root not found: {ctx.cluster_dir}", resolution="create infra/clusters/<name>/main.tf")
        return EXIT_PREREQ

    intent = parse_intent(ctx.cluster_dir / "main.tf")
    container = Container.for_tests(
        logger=ctx.log,
        cluster_dir=ctx.cluster_dir,
        repo_root=ctx.repo_root,
        cluster_name=ctx.cluster,
    )
    attach_intent(container, intent)
    try:
        build_topology(container=container, proxmox_vms_repo=ctx.proxmox_vms_repo)
    except BootstrapError as exc:
        ctx.log.error(step="prereq_failed", error=str(exc), resolution="run proxmox-vms apply first")
        return EXIT_PREREQ

    # `plan` only runs validate + ssh_probe (no mutations).
    selected = ("validate", "ssh_probe")
    try:
        plan = run_orchestrator(container, selected_phases=selected)
    except BootstrapError as exc:
        ctx.log.error(step="plan_failed", error=str(exc), resolution="see detail")
        return EXIT_PHASE
    _print_plan_table(plan)
    return EXIT_OK


def cmd_apply(args: argparse.Namespace) -> int:
    ctx = _resolve_ctx(args, subcommand="apply")
    if not ctx.cluster_dir.exists():
        ctx.log.error(step="prereq_failed", error=f"cluster root not found: {ctx.cluster_dir}", resolution="create infra/clusters/<name>/main.tf")
        return EXIT_PREREQ

    intent = parse_intent(ctx.cluster_dir / "main.tf")
    # Production container (real SSH, real kubectl, real StateStore).
    container = Container.production(
        logger=ctx.log,
        repo_root=ctx.repo_root,
        cluster_dir=ctx.cluster_dir,
        cluster_name=ctx.cluster,
        kubeconfig=ctx.cluster_dir / "kubeconfig.yaml",  # written by kubeconfig_pull phase
        ssh_proxy_target=os.environ.get("PVE_SSH_TARGET", "root@kvm.bruj0.net -p 6022"),
        env_file=ctx.repo_root / ".env",
    )
    # Restore state from the previous run (FileStateStore is already loaded by the factory).
    container.cluster_intent = intent

    try:
        build_topology(container=container, proxmox_vms_repo=ctx.proxmox_vms_repo)
    except BootstrapError as exc:
        ctx.log.error(step="prereq_failed", error=str(exc), resolution="run proxmox-vms apply first")
        return EXIT_PREREQ

    selected: tuple[str, ...] | None = tuple(args.phases.split(",")) if args.phases else None
    if selected is not None:
        unknown = [p for p in selected if p not in all_phases()]
        if unknown:
            ctx.log.error(step="apply_failed", error=f"unknown phases: {unknown}", resolution=f"available: {all_phases()}")
            return EXIT_PHASE
    try:
        plan = run_orchestrator(container, selected_phases=selected)
    except BootstrapError as exc:
        ctx.log.error(step="apply_failed", error=str(exc), resolution="see detail")
        return EXIT_PHASE

    _print_plan_table(plan)
    print(f"\nApply complete. Audit log: {ctx.audit_log_path}")
    return EXIT_OK


def cmd_destroy(args: argparse.Namespace) -> int:
    ctx = _resolve_ctx(args, subcommand="destroy")
    if not ctx.cluster_dir.exists():
        return EXIT_OK

    container = Container.for_tests(
        logger=ctx.log,
        cluster_dir=ctx.cluster_dir,
        repo_root=ctx.repo_root,
        cluster_name=ctx.cluster,
    )
    container.cluster_intent = parse_intent(ctx.cluster_dir / "main.tf")

    store = FileStateStore(cluster_dir=ctx.cluster_dir)
    store.reset()
    ctx.log.info(step="state_reset", path=str(ctx.cluster_dir / "bootstrap_state.json"))

    # Remove k3s.json (the canonical handoff artifact).
    k3s_json = ctx.cluster_dir / "k3s.json"
    if k3s_json.exists():
        k3s_json.unlink()
        ctx.log.info(step="k3s_json_removed", path=str(k3s_json))
    print("\nDestroy complete (state + k3s.json removed). VMs untouched — run `make destroy` in proxmox-vms to remove them.")
    return EXIT_OK


def cmd_kubeconfig(args: argparse.Namespace) -> int:
    """Fetch the cluster's admin kubeconfig and copy it to a local path.

    By default the kubeconfig is rewritten so its `server:` URL
    is the cluster's internal CP IP (e.g. https://10.0.0.64:6443)
    — handy when the operator host already has a route to the
    SDN. k3s bakes the in-cluster kubeconfig with
    `server: https://127.0.0.1:6443` which doesn't resolve from
    outside the cluster; we normalize it to the CP's internal IP.

    With `--use-tunnel`, the kubeconfig is instead routed through
    the same SSH port-forward plumbing the bootstrap uses
    internally (apiserver_ready + kubeconfig_pull phases), so
    `server:` becomes `https://127.0.0.1:<local_port>` and the
    tunnel lives for the lifetime of this process.
    """
    ctx = _resolve_ctx(args, subcommand="kubeconfig")
    if not ctx.cluster_dir.exists():
        ctx.log.error(
            step="prereq_failed",
            error=f"cluster root not found: {ctx.cluster_dir}",
            resolution="create infra/clusters/<name>/main.tf",
        )
        return EXIT_PREREQ

    cluster_kubeconfig = ctx.cluster_dir / "kubeconfig.yaml"
    use_tunnel: bool = bool(args.use_tunnel)

    intent = parse_intent(ctx.cluster_dir / "main.tf")
    container = Container.production(
        logger=ctx.log,
        repo_root=ctx.repo_root,
        cluster_dir=ctx.cluster_dir,
        cluster_name=ctx.cluster,
        kubeconfig=cluster_kubeconfig,
        ssh_proxy_target=os.environ.get("PVE_SSH_TARGET", "root@kvm.bruj0.net -p 6022"),
        env_file=ctx.repo_root / ".env",
    )
    container.cluster_intent = intent

    try:
        build_topology(container=container, proxmox_vms_repo=ctx.proxmox_vms_repo)
    except BootstrapError as exc:
        ctx.log.error(step="prereq_failed", error=str(exc), resolution="run proxmox-vms apply first")
        return EXIT_PREREQ

    topo = container.upstream_topology
    if topo is None or not topo.control_plane:
        ctx.log.error(step="prereq_failed", error="no control plane in upstream topology", resolution="run proxmox-vms apply first")
        return EXIT_PREREQ
    cp_ip = topo.control_plane[0].ip

    if use_tunnel:
        # Default bootstrap path: re-run the apiserver_ready +
        # kubeconfig_pull phases (idempotent if previously done;
        # the state-store will short-circuit). The phase opens the
        # tunnel and writes cluster_dir/kubeconfig.yaml with the
        # rewritten 127.0.0.1:<local_port> server URL.
        selected = ("apiserver_ready", "kubeconfig_pull")
        try:
            run_orchestrator(container, selected_phases=selected)
        except BootstrapError as exc:
            ctx.log.error(step="kubeconfig_failed", error=str(exc), resolution="see detail")
            return EXIT_PHASE

        if not cluster_kubeconfig.exists():
            ctx.log.error(
                step="kubeconfig_missing",
                error=f"phase completed but {cluster_kubeconfig} was not written",
                resolution="check apiserver readiness + tunnel logs above",
            )
            return EXIT_PHASE

        written = cluster_kubeconfig.read_text()
        ctx.log.info(step="kubeconfig_written", path=str(cluster_kubeconfig), mode="tunnel")
    else:
        # Direct path: SSH to the CP, fetch the raw kubeconfig,
        # and normalize the server URL to the CP's internal IP.
        # No tunnel is opened — the operator must already have a
        # route to <cp.ip>:6443 (typical for operators on the
        # same SDN or with a VPN into the lab).
        fetch = container.remote.run(
            cp_ip,
            "sudo cat /etc/rancher/k3s/k3s.yaml",
            check=False,
            timeout=15.0,
        )
        if fetch.exit_code != 0 or "apiVersion" not in fetch.stdout:
            ctx.log.error(
                step="kubeconfig_fetch_failed",
                stderr=fetch.stderr.strip(),
                exit_code=fetch.exit_code,
                resolution=f"verify ssh proxy + that k3s is running on {cp_ip}",
            )
            return EXIT_PHASE

        raw = fetch.stdout
        # k3s uses 127.0.0.1 (in-cluster); rewrite to the CP's LAN IP.
        rewritten = raw.replace(
            "server: https://127.0.0.1:6443",
            f"server: https://{cp_ip}:6443",
        )
        if rewritten == raw:
            # No rewrite needed — CP already uses its own IP.
            rewritten = raw

        cluster_kubeconfig.write_text(rewritten)
        ctx.log.info(
            step="kubeconfig_written",
            path=str(cluster_kubeconfig),
            mode="direct",
            cp_ip=cp_ip,
        )
        written = rewritten

    # Parse the in-memory kubeconfig as a YAML mapping. The fetch
    # path (cluster + user + context) is what we merge into the
    # user's local kubeconf.
    try:
        new_doc = yaml.safe_load(written) or {}
    except yaml.YAMLError as exc:
        ctx.log.error(
            step="kubeconfig_parse_failed",
            error=str(exc),
            resolution="the upstream kubeconfig is not valid YAML",
        )
        return EXIT_PHASE

    if args.output is not None:
        # Explicit output path — overwrite the file with the
        # rewritten kubeconfig (this is the "I want a standalone
        # kubeconfig file" mode). No merging.
        output_path: Path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(written)
        ctx.log.info(
            step="kubeconfig_written",
            path=str(output_path),
            mode="standalone",
            source=str(cluster_kubeconfig),
        )
    else:
        # Default: merge into the operator's local kubeconf.
        # Honor $KUBECONFIG if it points at a single file (kubectl
        # supports a colon-separated list, but we don't try to
        # merge across multiple files — that's the operator's job).
        default_path = Path(os.environ.get("KUBECONFIG") or "~/.kube/config").expanduser()
        default_path.parent.mkdir(parents=True, exist_ok=True)
        merge_kubeconfig(default_path, new_doc, cluster_name=ctx.cluster)
        ctx.log.info(
            step="kubeconfig_merged",
            path=str(default_path),
            cluster=ctx.cluster,
            current_context=ctx.cluster,
            source=str(cluster_kubeconfig),
        )

    # Best-effort summary.
    tunnel = container.apiserver_tunnel
    local_port = tunnel.local_port if tunnel is not None and use_tunnel else None
    if args.output is not None:
        output_str = str(args.output.expanduser().resolve())
    else:
        output_str = str(Path(os.environ.get("KUBECONFIG") or "~/.kube/config").expanduser())
    print(f"\nKubeconfig written to {output_str}")
    print(f"Server URL: https://{cp_ip}:6443" + (f"  (via 127.0.0.1:{local_port} tunnel)" if local_port else ""))
    print(
        "Quickstart:\n"
        f"  kubectl get nodes --context {ctx.cluster}\n"
        f"  kubectl get pods -A --context {ctx.cluster}"
    )
    if local_port is not None:
        print(
            "Note: the tunnel lives in this process. Closing it (ctrl-c)\n"
            "breaks all kubectl calls until you re-run this command."
        )
    return EXIT_OK


def merge_kubeconfig(path: Path, new_doc: dict, *, cluster_name: str) -> None:
    """Merge `new_doc` into the YAML kubeconfig at `path` (in place).

    Idempotent on re-run: re-merging a cluster with the same
    `cluster_name` overwrites its cluster/user/context entries
    instead of duplicating. Sets `current-context: <cluster_name>`
    so `kubectl` points at the freshly-merged cluster.

    If `path` does not yet exist, write `new_doc` as the initial
    contents (caller is responsible for setting
    `current-context` to the freshly-merged cluster in that case).
    """
    if path.exists():
        text = path.read_text()
        try:
            existing = yaml.safe_load(text)
        except yaml.YAMLError:
            existing = None
        if not isinstance(existing, dict):
            existing = {}
    else:
        existing = {}

    # Defensive: drop None entries that kubectl chokes on.
    new_cluster = new_doc.get("clusters") or []
    new_users = new_doc.get("users") or []
    new_contexts = new_doc.get("contexts") or []

    clusters = [c for c in (existing.get("clusters") or []) if isinstance(c, dict) and c.get("name") != cluster_name]
    clusters.extend(new_cluster)
    users = [u for u in (existing.get("users") or []) if isinstance(u, dict) and u.get("name") != cluster_name]
    users.extend(new_users)
    contexts = [c for c in (existing.get("contexts") or []) if isinstance(c, dict) and c.get("name") != cluster_name]
    contexts.extend(new_contexts)

    merged = {
        "apiVersion": existing.get("apiVersion", "v1"),
        "kind": existing.get("kind", "Config"),
        "preferences": existing.get("preferences", {}),
        "clusters": clusters,
        "users": users,
        "contexts": contexts,
        "current-context": cluster_name,
    }
    path.write_text(yaml.safe_dump(merged, sort_keys=False))


def cmd_validate(args: argparse.Namespace) -> int:
    ctx = _resolve_ctx(args, subcommand="validate")
    if not ctx.cluster_dir.exists():
        ctx.log.error(step="prereq_failed", error=f"cluster root not found: {ctx.cluster_dir}", resolution="create infra/clusters/<name>/main.tf")
        return EXIT_PREREQ
    intent = parse_intent(ctx.cluster_dir / "main.tf")
    print(f"Cluster:    {intent.cluster_name}")
    print(f"pod_cidr:   {intent.pod_cidr}")
    print(f"svc_cidr:   {intent.svc_cidr}")
    print(f"k3s_version:{intent.k3s_version}")
    print(f"tunnel:     {intent.cf_tunnel_name}")
    print(f"exec server ({len(intent.install_k3s_exec_server)} flags):")
    for flag in intent.install_k3s_exec_server:
        print(f"  - {flag}")
    # Sanity-check upstream output.json without mutating anything.
    container = Container.for_tests(
        logger=ctx.log,
        cluster_dir=ctx.cluster_dir,
        repo_root=ctx.repo_root,
        cluster_name=ctx.cluster,
    )
    try:
        build_topology(container=container, proxmox_vms_repo=ctx.proxmox_vms_repo)
        topo = container.upstream_topology
        assert topo is not None
        print(f"\nUpstream: {ctx.proxmox_vms_repo}")
        print(f"  control_plane: {[n.name for n in topo.control_plane]}")
        print(f"  worker:        {[n.name for n in topo.worker]}")
    except BootstrapError as exc:
        ctx.log.error(step="validate_failed", error=str(exc), resolution="see detail")
        return EXIT_PREREQ
    return EXIT_OK


# ----------------------------------------------------------- argparse + main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provisioner",
        description="Stage-2 of the proxmox provisioning pipeline: turn proxmox-vms clones into a k3s cluster.",
    )
    parser.add_argument("--repo-root", type=Path, default=None, help="Path to the proxmox-k3s repo root (default: parent of this script).")
    parser.add_argument("--proxmox-vms-repo", type=Path, default=None, help="Path to the sibling proxmox-vms repo (default: ../proxmox-vms).")
    parser.add_argument("--ssh-key", type=str, default="~/.ssh/id_ed25519", help="Path to the operator's SSH private key (default: ~/.ssh/id_ed25519).")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_plan = sub.add_parser("plan", help="Diff desired cluster against live state (no changes).")
    p_plan.add_argument("cluster")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="Install k3s + helm charts (idempotent).")
    p_apply.add_argument("cluster")
    p_apply.add_argument("--auto-approve", action="store_true", help="Skip the interactive confirmation prompt.")
    p_apply.add_argument("--phases", type=str, default=None, help="Comma-separated phase names to run (default: all).")
    p_apply.set_defaults(func=cmd_apply)

    p_destroy = sub.add_parser("destroy", help="Tear down workloads (does NOT remove VMs).")
    p_destroy.add_argument("cluster")
    p_destroy.add_argument("--auto-approve", action="store_true")
    p_destroy.set_defaults(func=cmd_destroy)

    p_val = sub.add_parser("validate", help="Parse main.tf + verify upstream output.json (no SSH).")
    p_val.add_argument("cluster")
    p_val.set_defaults(func=cmd_validate)

    p_kc = sub.add_parser(
        "kubeconfig",
        help=(
            "Fetch the cluster's admin kubeconfig and merge it into the "
            "operator's local kubeconf (~/.kube/config, or $KUBECONFIG if "
            "set). Re-runs are idempotent — existing entries for the same "
            "cluster name are replaced in place. By default the server URL "
            "is rewritten to the CP's internal IP (operator must have a "
            "route to it). Use --use-tunnel to route through the SSH port-"
            "forward plumbing instead. Pass --output to write the kubeconfig "
            "to a standalone file instead of merging."
        ),
    )
    p_kc.add_argument("cluster")
    p_kc.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Write the kubeconfig to this path instead of merging into the "
            "operator's local kubeconf (~/.kube/config, or $KUBECONFIG if "
            "set). The file is overwritten if it already exists."
        ),
    )
    p_kc.add_argument(
        "--use-tunnel",
        action="store_true",
        help=(
            "Open an SSH port-forward through the PVE proxy and rewrite the "
            "kubeconfig's server URL to https://127.0.0.1:<ephemeral>. The "
            "tunnel lives for the lifetime of this process."
        ),
    )
    p_kc.set_defaults(func=cmd_kubeconfig)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
