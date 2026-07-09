"""install_k3s phase — install k3s on every VM.

Wraps the cicd repo's `K3sInstaller` (vendored) and exposes it
behind our `RemoteExecutor` Protocol. The phase:

  1. Builds a cluster dict shaped the way `K3sInstaller` expects
     (same shape as the cicd orchestrator's `_run_install_k3s`).
  2. Calls `install_server` on every control-plane VM (serially).
  3. Reads the node-token off the first CP via the vendored
     `K3sInstaller.read_node_token`.
  4. Calls `install_agent` on every worker VM.

Idempotency: `K3sInstaller` short-circuits when the systemd unit
is already active and `/etc/rancher/k3s/k3s.yaml` exists. We also
gate on `ctx.state_store.phases_done()` so a re-run skips the
phase entirely.
"""

from __future__ import annotations

from typing import Any

from ..container import Container
from ..k3s_installer import K3sInstaller, K3sInstallerError
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class InstallK3sPhase(Phase):
    """Install k3s server on every CP, then agents on every worker."""

    name = "install_k3s"
    requires = ("ssh_probe",)

    def run(self, ctx: Container) -> PhaseResult:
        topo = ctx.upstream_topology
        intent = ctx.cluster_intent
        if topo is None:
            raise BootstrapError("install_k3s", {"reason": "upstream_topology is None"})

        cluster_dict = _build_cluster_dict(topo, intent)
        # The PveSshProxy lives on the container (production only).
        # K3sInstaller wants one; we adapt the same proxy the
        # RemoteExecutor adapter wraps.
        proxy = ctx.pve_proxy
        if proxy is None:
            raise BootstrapError(
                "install_k3s",
                {"reason": "K3sInstaller requires the PveSshProxy; production container missing it"},
            )
        installer = K3sInstaller(
            cluster=cluster_dict,
            ssh_proxy_target=_ssh_target(ctx),
            logger=ctx.logger,
            proxy=proxy,
            versions=ctx.versions_reader,
        )
        try:
            for cp in topo.control_plane:
                installer.install_server(_vm_to_dict(cp), vip="")
            first_cp = _vm_to_dict(topo.control_plane[0])
            token = installer.read_node_token(first_cp)
            for w in topo.worker:
                installer.install_agent(_vm_to_dict(w), vip="", token=token)
        except K3sInstallerError as exc:
            raise BootstrapError("install_k3s", {"reason": exc.reason, **exc.fields}) from exc

        return PhaseResult.make_done("install_k3s", k3s_version=ctx.versions.k3s_version())


def _build_cluster_dict(topo: Any, intent: Any) -> dict[str, Any]:
    """Build the dict shape that K3sInstaller expects.

    Mirrors the cicd orchestrator's `_run_install_k3s` block:
    top-level keys (name, vip, control_plane_ip, svc_cidr,
    pod_cidr, cluster_dns, vms) plus per-VM vmid / role / ip.
    """
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


def _vm_to_dict(vm: Any) -> dict[str, Any]:
    """Convert a VmTarget (frozen dataclass) to a plain dict.

    K3sInstaller expects a Mapping[str, Any], and mypy won't let
    us pass a frozen dataclass to `dict()` directly.
    """
    return {"name": vm.name, "vmid": vm.vmid, "role": vm.role, "ip": vm.ip}


def _ssh_target(ctx: Container) -> str:
    """The PveSshProxy `jump_host` string.

    Format: `user@host -p port`. The PveSshProxy class accepts
    this form (it parses it out of the constructor). We default
    to BigBertha's well-known ssh config; the production
    container can override by exporting `PVE_SSH_TARGET`.
    """
    import os
    return os.environ.get("PVE_SSH_TARGET", "root@kvm.bruj0.net -p 6022")
