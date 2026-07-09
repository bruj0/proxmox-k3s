"""host_ports_check phase — M2 misfit guard.

Per the WP06 design, the proxmox-k3s bootstrap must NEVER open
host ports on the PVE host (that's what Cloudflare Tunnel is
for). This phase queries the PVE's nft prerouting chain via the
cicd vendored `host_ports.verify_no_new_dnat_rules` and compares
it against the captured baseline. A regression here is the
visible half of the M2 misfit.
"""

from __future__ import annotations

from ..container import Container
from ..host_ports import verify_no_new_dnat_rules
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class HostPortsCheckPhase(Phase):
    """Verify the PVE host has no new hostPort DNAT rules."""

    name = "host_ports"
    requires = ("helm_releases",)

    def run(self, ctx: Container) -> PhaseResult:
        baseline_file = ctx.cluster_dir / "host_ports_baseline.txt"
        try:
            verify_no_new_dnat_rules(baseline_file)
        except Exception as exc:
            raise BootstrapError("host_ports", {"reason": str(exc)}) from exc
        ctx.logger.info(step="host_ports_ok", baseline=str(baseline_file))
        return PhaseResult.make_done("host_ports", baseline=str(baseline_file))
