"""kubeconfig_pull phase — fetch the admin kubeconfig.

Reads `/etc/rancher/k3s/k3s.yaml` from the first control-plane VM
over SSH and writes it to `infra/clusters/<name>/kubeconfig.yaml`.
This is the kubeconfig downstream apps use to reach the apiserver
(direct, NOT through a tunnel — the cluster exposes 6443 on the
CP's LAN IP).

For the operator's interactive `kubectl` (which goes through the
PVE tunnel), the cicd repo's `merge_kubeconfig_for_pveproxy` tool
is the right entry point — see docs/runbooks/operator-kubectl.md.
"""

from __future__ import annotations

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class KubeconfigPullPhase(Phase):
    """Pull /etc/rancher/k3s/k3s.yaml from the first CP."""

    name = "kubeconfig_pull"
    requires = ("apiserver_ready",)

    def run(self, ctx: Container) -> PhaseResult:
        topo = ctx.upstream_topology
        if topo is None or not topo.control_plane:
            raise BootstrapError("kubeconfig_pull", {"reason": "no control plane"})

        cp = topo.control_plane[0]
        target = f"ubuntu@{cp.ip}"
        # Read the kubeconfig via the RemoteExecutor.
        result = ctx.remote.run(
            target,
            "sudo cat /etc/rancher/k3s/k3s.yaml",
            check=False,
            timeout=15.0,
        )
        if result.exit_code != 0 or "apiVersion" not in result.stdout:
            raise BootstrapError(
                "kubeconfig_pull",
                {
                    "reason": "could not read /etc/rancher/k3s/k3s.yaml",
                    "stderr": result.stderr.strip(),
                },
            )

        out_path = ctx.cluster_dir / "kubeconfig.yaml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Pin the server URL to the CP's IP (in-cluster kubeconfig
        # points at 127.0.0.1:6443 which is only reachable from the CP).
        rewritten = result.stdout.replace("server: https://127.0.0.1:6443", f"server: https://{cp.ip}:6443")
        out_path.write_text(rewritten)
        ctx.logger.info(step="kubeconfig_pulled", path=str(out_path))
        return PhaseResult.make_done("kubeconfig_pull", path=str(out_path))
