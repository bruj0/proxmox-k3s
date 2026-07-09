"""upstream_reader — load the proxmox-vms output.json contract.

The sibling `proxmox-vms` repo writes `output.json` per cluster
after a successful `provisioner apply`. This module loads that
file and lifts it into our typed `ClusterTopology` (a Protocol
defined in protocols.py).

Format (from proxmox-vms/provisioner/lib/output_writer.py):

    {
      "cluster_name": "cicd",
      "vnet_bridge": "vnet0",
      "storage_pool": "data1",
      "pve_node": "BigBertha",
      "vmid_start": 300,
      "template_vmid": 900,
      "nodes": [
        {"role": "control_plane", "name": "cicd-cp-1", "vmid": 300, "ip": "10.0.0.64"},
        {"role": "worker",        "name": "cicd-w-1",  "vmid": 301, "ip": "10.0.0.65"}
      ]
    }

The reader is intentionally small — no validation beyond "the
file exists and parses as JSON". The orchestrator validates
shape against `ClusterTopology` and raises BootstrapError on
mismatch.
"""

from __future__ import annotations

import json
from pathlib import Path

from .protocols import BootstrapError, ClusterTopology, VmTarget


def read_upstream_topology(
    *,
    proxmox_vms_repo: Path,
    cluster_name: str,
) -> ClusterTopology:
    """Read `proxmox-vms/infra/clusters/<name>/output.json` and lift it.

    Returns a typed ClusterTopology; raises BootstrapError on any
    file-not-found / JSON error / shape mismatch.
    """
    output_json = proxmox_vms_repo / "infra" / "clusters" / cluster_name / "output.json"
    if not output_json.exists():
        raise BootstrapError(
            "validate",
            {"missing_upstream_output_json": str(output_json)},
        )
    try:
        payload = json.loads(output_json.read_text())
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            "validate",
            {"upstream_output_json_invalid": str(output_json), "error": str(exc)},
        ) from exc

    if payload.get("cluster_name") != cluster_name:
        raise BootstrapError(
            "validate",
            {"upstream_cluster_name_mismatch": f"file={payload.get('cluster_name')} wanted={cluster_name}"},
        )

    cps: list[VmTarget] = []
    workers: list[VmTarget] = []
    for raw_node in payload.get("nodes", []):
        try:
            vm = VmTarget(
                role=str(raw_node["role"]),
                name=str(raw_node["name"]),
                vmid=int(raw_node["vmid"]),
                ip=str(raw_node["ip"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BootstrapError(
                "validate",
                {"upstream_node_shape_invalid": str(raw_node), "error": str(exc)},
            ) from exc
        if vm.role == "control_plane":
            cps.append(vm)
        elif vm.role == "worker":
            workers.append(vm)
        else:
            raise BootstrapError(
                "validate",
                {"upstream_unknown_role": vm.role, "node": vm.name},
            )

    return ClusterTopology(
        cluster_name=cluster_name,
        control_plane=tuple(cps),
        worker=tuple(workers),
    )
