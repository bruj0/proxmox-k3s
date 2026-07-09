"""ClusterTopology — shape of infra/clusters/<name>/output.json as SS2 emits it.

WP08 (2026-07-08): moved out of tools/lib/talos_client.py when the
pipeline pivoted off Talos. The class is purely a JSON shape
adapter; nothing in it knows about Talos, k3s, or any specific
cluster runtime. All callers (bootstrap_cluster.py, ssh_proxy.py,
kubeconfig_puller.py) import from here.

SS2 contract (infra/modules/proxmox-k3s-cluster/outputs.tf::
local_sensitive_file cluster_output) emits a flat `nodes` array
with role="control_plane" or "worker". This class splits them into
the two collections the bootstrap script needs.

WP08 also drops `vip` from the cluster output JSON contract; the
field is still read for backwards compatibility (older output.json
files have it) but the bootstrap code only uses it for diagnostics.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClusterTopology:
    """Parsed view of `infra/clusters/<name>/output.json`."""

    name: str
    # WP08 (2026-07-08): `vip` is deprecated. Single-CP clusters
    # no longer have a VIP layer; agents join on the CP host IP.
    # We keep the field for backwards compatibility with older
    # output.json files (cicd v1.34.x output had 10.0.0.30 etc.).
    vip: str
    pod_cidr: str
    svc_cidr: str
    cluster_dns: str
    control_plane: Sequence[Mapping[str, str]]
    worker: Sequence[Mapping[str, str]]

    @property
    def all_nodes(self) -> list[Mapping[str, str]]:
        return [*self.control_plane, *self.worker]

    @classmethod
    def from_output_json(cls, path: Path) -> ClusterTopology:
        data: dict[str, Any] = json.loads(path.read_text())
        try:
            nodes = data["nodes"]
            cps = [n for n in nodes if n.get("role") == "control_plane"]
            wks = [n for n in nodes if n.get("role") == "worker"]
            return cls(
                name=data["cluster_name"],
                vip=data.get("vip", ""),
                pod_cidr=data.get("pod_cidr", "172.16.0.0/16"),
                svc_cidr=data.get("svc_cidr", "172.17.0.0/16"),
                # WP08 (2026-07-08): default to 172.17.0.10 for
                # cicd-compatible svc CIDR; apps reads 172.21.0.10
                # from output.json (output.tf contract).
                cluster_dns=data.get("cluster_dns", "172.17.0.10"),
                control_plane=cps,
                worker=wks,
            )
        except KeyError as exc:
            missing = exc.args[0]
            raise ValueError(
                f"output.json missing required field '{missing}'"
            ) from exc
