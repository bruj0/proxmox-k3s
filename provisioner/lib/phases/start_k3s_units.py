"""start_k3s_units phase — start the k3s systemd units after CNI is up.

WP11 (2026-07-09): install_server/install_agent use
`INSTALL_K3S_SKIP_START=true` so the unit is enabled but not
running. The kubelet start job blocks on the CNI plugin
initialising, and CNI is installed by the cilium_install phase.
This phase runs AFTER cilium_install so the start job completes
immediately and the nodes join the cluster.

Order in dependency chain:
  install_k3s -> apiserver_ready -> kubeconfig_pull
    -> gateway_crds -> cilium_install
    -> **start_k3s_units** -> helm_releases -> host_ports -> topology_writer
"""

from __future__ import annotations

from typing import Any

from ..container import Container
from ..k3s_installer import K3sInstaller, K3sInstallerError
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class StartK3sUnitsPhase(Phase):
    """Start k3s server on every CP, k3s-agent on every worker."""

    name = "start_k3s_units"
    requires = ("cilium_install",)

    def run(self, ctx: Container) -> PhaseResult:
        topo = ctx.upstream_topology
        intent = ctx.cluster_intent
        if topo is None:
            raise BootstrapError("start_k3s_units", {"reason": "no topology"})

        proxy = ctx.pve_proxy
        if proxy is None:
            raise BootstrapError(
                "start_k3s_units",
                {"reason": "K3sInstaller requires the PveSshProxy"},
            )

        installer = K3sInstaller(
            cluster=_cluster_dict(topo, intent),
            ssh_proxy_target=_ssh_target(),
            logger=ctx.logger,
            proxy=proxy,
            versions=ctx.versions_reader,
        )
        try:
            started = 0
            for cp in topo.control_plane:
                installer.start_k3s_unit(_vm_dict(cp), role="server")
                started += 1
            for w in topo.worker:
                installer.start_k3s_unit(_vm_dict(w), role="agent")
                started += 1
        except K3sInstallerError as exc:
            raise BootstrapError("start_k3s_units", {"reason": exc.reason, **exc.fields}) from exc

        ctx.logger.info(step="start_k3s_units_done", units_started=started)
        return PhaseResult.make_done("start_k3s_units", units_started=started)


def _cluster_dict(topo: Any, intent: Any) -> dict[str, Any]:
    """Same shape install_k3s.py builds (see that file for the why)."""
    return {
        "name": topo.cluster_name,
        "vip": "",
        "control_plane_ip": topo.control_plane[0].ip if topo.control_plane else "",
        "svc_cidr": intent.svc_cidr,
        "pod_cidr": intent.pod_cidr,
        "cluster_dns": intent.cluster_dns,
        "vms": [
            {
                "name": n.name,
                "vmid": n.vmid,
                "role": n.role,
                "ip": n.ip,
                "svc_cidr": intent.svc_cidr,
                "pod_cidr": intent.pod_cidr,
                "cluster_dns": intent.cluster_dns,
            }
            for n in topo.all_nodes
        ],
    }


def _vm_dict(vm: Any) -> dict[str, Any]:
    return {"name": vm.name, "vmid": vm.vmid, "role": vm.role, "ip": vm.ip}


def _ssh_target() -> str:
    import os
    return os.environ.get("PVE_SSH_TARGET", "root@kvm.bruj0.net -p 6022")
