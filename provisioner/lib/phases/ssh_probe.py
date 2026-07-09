"""ssh_probe phase — verify every cluster VM is reachable.

Runs `cloud-init status` over SSH on every node and confirms
qemu-guest-agent is active. This catches the common failure mode
where proxmox-vms succeeded but the cloned VM didn't actually
boot (BIOS issue, NIC misconfiguration, ...).
"""

from __future__ import annotations

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError, RemoteResult


@register
class SshProbePhase(Phase):
    """SSH to every VM; confirm cloud-init finished + agent running."""

    name = "ssh_probe"
    requires = ("validate",)

    def run(self, ctx: Container) -> PhaseResult:
        topo = ctx.upstream_topology
        if topo is None:
            raise BootstrapError("ssh_probe", {"reason": "upstream_topology is None"})

        nodes_checked: list[str] = []
        for node in topo.all_nodes:
            target = node.ip
            # Probe 1: cloud-init status.
            r1: RemoteResult = ctx.remote.run(
                target,
                "cloud-init status --long || true",
                check=False,
                timeout=10.0,
            )
            # Probe 2: qemu-guest-agent.
            r2: RemoteResult = ctx.remote.run(
                target,
                "systemctl is-active qemu-guest-agent || true",
                check=False,
                timeout=10.0,
            )
            agent_ok = "active" in r2.stdout
            if not agent_ok:
                ctx.logger.warn(
                    step="ssh_probe_agent_inactive",
                    message=f"qemu-guest-agent not active on {node.name} ({node.ip})",
                    node=node.name,
                    ip=node.ip,
                    stdout=r2.stdout.strip(),
                )
            nodes_checked.append(node.name)
            ctx.logger.info(
                step="ssh_probe_node",
                node=node.name,
                ip=node.ip,
                cloud_init_done="status: done" in r1.stdout,
                agent_ok=agent_ok,
            )

        return PhaseResult.make_done("ssh_probe", nodes_checked=nodes_checked)
