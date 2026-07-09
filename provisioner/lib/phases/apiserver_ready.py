"""apiserver_ready phase — verify the k3s server is up.

Runs `kubectl get --raw /healthz` against the cluster via the
ClusterProbe protocol. If the apiserver is unreachable we raise
BootstrapError so the operator sees a clear failure before the
helm phase attempts to install charts.
"""

from __future__ import annotations

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class ApiserverReadyPhase(Phase):
    """Block until k3s /healthz returns ok."""

    name = "apiserver_ready"
    requires = ("install_k3s",)

    def run(self, ctx: Container) -> PhaseResult:
        if not ctx.cluster_probe.apiserver_reachable():
            raise BootstrapError(
                "apiserver_ready",
                {"reason": "kubectl get --raw /healthz did not return ok"},
            )
        nodes = ctx.cluster_probe.get_nodes()
        ctx.logger.info(step="apiserver_ready_ok", node_count=len(nodes))
        return PhaseResult.make_done("apiserver_ready", node_count=len(nodes))
