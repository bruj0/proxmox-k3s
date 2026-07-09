"""topology_labels phase — set topology.kubernetes.io/{region,zone} on every node.

Why this phase exists
---------------------
proxmox-csi-plugin's node DaemonSet CrashLoopBackOffs on a
freshly-bootstrapped single-node PVE host with:

    Failed to get region or zone for node: cicd-cp-1,
    region: , zone: , see documentation about topology labels

The CSI driver reads `topology.kubernetes.io/region` and
`topology.kubernetes.io/zone` from the Node object directly.
On a multi-node PVE cluster with corosync, proxmox-ccm populates
these labels automatically as part of cloud-node reconciliation
(it discovers region/zone from the PVE cluster registry).

On a SINGLE-NODE PVE host, there is no corosync cluster and no
cluster registry; proxmox-ccm never gets a non-empty
`instance.Region` / `instance.Zone` to write to the Node, and
the CSI plugin's node DaemonSet falls into a CrashLoopBackOff
loop. The whole pipeline (helm_releases -> csi_smoke) grinds to
a halt on the next apply.

The fix has two parts, in this repo:

  1. THIS PHASE: a one-shot `kubectl label node ...` after the
     cluster is reachable (cilium_install complete) and BEFORE
     the helm_releases phase installs proxmox-csi-plugin. It
     sets the labels on every Node from values the operator
     already supplied via $PROXMOX_REGION / $PROXMOX_ZONE (the
     same values the CCM and CSI charts consume via their
     `config.clusters[0].region` setting).

  2. The CCM/CSI chart values (already pinned in
     provisioner/lib/phases/helm_releases.py) stay as
     `region=proxmox-host` / `zone=BigBertha`; those are the
     values used by proxmox-ccm to label Nodes it discovers
     on a multi-node PVE cluster. On single-node PVE, the
     CCM's discovery path never fires, so this phase
     substitutes the label write.

Reference: cicd repo's `versions.lock.yaml::cross_check::
csi_smoke_roundtrip_2026_07_08` documents the same root cause
("single-node PVE has no corosync so proxmox-ccm does not
auto-derive them ... once the topology labels are applied the
csi-plugin-node DaemonSet is 3/3 Running").

Idempotency
-----------
`kubectl label --overwrite` is a no-op when the label value
already matches. The phase records completion in bootstrap_state;
on a re-run it short-circuits if every node already carries
both labels with the expected values, so a steady-state re-apply
doesn't touch the API server.

Ordering
--------
Requires `cilium_install` (so the apiserver tunnel is up via
KubeconfigPullPhase's tunneled kubeconfig, which we now have to
write the labels). The helm_releases phase (which installs
proxmox-csi-plugin) requires this phase to have run, so its
`requires` is `("topology_labels",)`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


_KUBECTL_TIMEOUT_S = 30.0


def _kubectl(
    kubeconfig: Path, *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run kubectl with the cluster's tunneled kubeconfig.

    stdin/stdout flow straight through so the operator sees the
    real kubectl output during `make apply`. We don't want to
    capture and re-print — that breaks colored output and adds a
    layer of indirection on failures.
    """
    cmd = ["kubectl", "--kubeconfig", str(kubeconfig), *args]
    return subprocess.run(  # noqa: S603 -- operator-driven CLI
        cmd,
        check=check,
        text=True,
        timeout=_KUBECTL_TIMEOUT_S,
        capture_output=True,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )


@register
class TopologyLabelsPhase(Phase):
    """Apply topology.kubernetes.io/{region,zone} to every Node.

    Runs after `cilium_install` (so the apiserver tunnel is up
    and the cluster has working DNS) and before `helm_releases`
    (so proxmox-csi-plugin's node DaemonSet can read the labels
    at startup time).
    """

    name = "topology_labels"
    requires = ("cilium_install",)

    def run(self, ctx: Container) -> PhaseResult:
        # Region/zone come from the same env-driven sources the
        # helm_releases phase uses for the CCM/CSI chart values.
        # Defaults match cicd repo's documented values
        # (region=proxmox-host, zone=BigBertha) but the env can
        # override for any non-BigBertha host.
        region = ctx.secrets.proxmox_region() or "proxmox-host"
        zone = ctx.secrets.proxmox_zone() or "BigBertha"
        ctx.logger.info(
            "topology_labels.values",
            region=region,
            zone=zone,
        )

        # The kubeconfig_pull phase already wrote the tunneled
        # kubeconfig to cluster_dir/kubeconfig.yaml. The
        # apiserver tunnel (ctx.apiserver_tunnel) is still up,
        # so kubectl here routes through it.
        kubeconfig = ctx.cluster_dir / "kubeconfig.yaml"
        if not kubeconfig.exists():
            raise BootstrapError(
                "topology_labels",
                {
                    "reason": "kubeconfig.yaml missing",
                    "expected": str(kubeconfig),
                    "resolution": (
                        "kubeconfig_pull phase must run before "
                        "topology_labels (check phase ordering)"
                    ),
                },
            )

        # List every node we need to label. We deliberately use
        # `kubectl get nodes -o json` and parse it in-Python
        # rather than a jsonpath query, so we can assert both
        # labels are present (idempotency) and produce a clear
        # `nodes_to_label` count in the audit log.
        result = _kubectl(
            kubeconfig,
            "get",
            "nodes",
            "-o",
            "json",
        )
        if result.returncode != 0:
            raise BootstrapError(
                "topology_labels",
                {
                    "reason": "kubectl get nodes failed",
                    "stderr": result.stderr.strip(),
                },
            )
        try:
            nodes_obj = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BootstrapError(
                "topology_labels",
                {"reason": f"kubectl get nodes returned non-JSON: {exc}"},
            ) from exc

        nodes = nodes_obj.get("items", [])
        if not nodes:
            raise BootstrapError(
                "topology_labels",
                {
                    "reason": "cluster has no Nodes",
                    "resolution": "wait for k3s to register the CP and agents",
                },
            )

        # Decide which nodes still need labelling. A node is
        # "up to date" iff it already carries both labels with
        # the expected values.
        nodes_to_label: list[str] = []
        nodes_already_ok: list[str] = []
        for n in nodes:
            name = n["metadata"]["name"]
            labels = n["metadata"].get("labels") or {}
            has_region = labels.get("topology.kubernetes.io/region") == region
            has_zone = labels.get("topology.kubernetes.io/zone") == zone
            if has_region and has_zone:
                nodes_already_ok.append(name)
            else:
                nodes_to_label.append(name)

        ctx.logger.info(
            "topology_labels.plan",
            already_ok=nodes_already_ok,
            to_label=nodes_to_label,
        )

        # Apply labels one node at a time. We deliberately
        # don't use a wildcard `-l` selector because the
        # operator might add nodes later and we want this
        # phase to remain focused on the nodes that already
        # exist when it runs.
        for node_name in nodes_to_label:
            label_result = _kubectl(
                kubeconfig,
                "label",
                f"node/{node_name}",
                f"topology.kubernetes.io/region={region}",
                f"topology.kubernetes.io/zone={zone}",
                "--overwrite=true",
                check=False,
            )
            if label_result.returncode != 0:
                raise BootstrapError(
                    "topology_labels",
                    {
                        "reason": f"kubectl label node/{node_name} failed",
                        "stderr": label_result.stderr.strip(),
                        "stdout": label_result.stdout.strip(),
                    },
                )
            ctx.logger.info(
                "topology_labels.applied",
                node=node_name,
                region=region,
                zone=zone,
            )

        return PhaseResult.make_done(
            "topology_labels",
            region=region,
            zone=zone,
            already_ok=len(nodes_already_ok),
            applied=len(nodes_to_label),
        )