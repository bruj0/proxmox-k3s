"""Bootstrap orchestrator — wires DI + phases into a single runner.

This module is intentionally small (~150 lines). All the work
happens in the Phase subclasses and the DI container. The
orchestrator's job is:

  1. Resolve which phases to run (CLI's --phases a,b,c).
  2. Topologically sort them via the registry.
  3. For each phase, call `phase.run(container)` and capture the
     PhaseResult.
  4. After all phases, mark each succeeded one in the StateStore.

The orchestrator never imports SSH, helm, kubectl, or k3s
directly. It talks to those via the Protocols on the container.
That is what makes every phase testable in isolation with a
FakeRemoteExecutor / FakeClusterProbe.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .container import Container
from .hcl_parser import ClusterIntent, HclParseError, parse_cluster_root
from .phases.base import Phase, PhaseRegistry, PhaseResult, get_registry
from .protocols import BootstrapError, ClusterTopology
from .upstream_reader import read_upstream_topology


@dataclass
class BootstrapPlan:
    """The result of running the orchestrator.

    `phases` is the ordered list of (phase_name, PhaseResult) pairs
    the orchestrator executed. The CLI prints this as a table.
    `intent` is the parsed cluster intent (so the CLI can echo it
    without re-parsing).
    """

    cluster_name: str
    phases: list[tuple[str, PhaseResult]] = field(default_factory=list)
    intent: ClusterIntent | None = None

    @property
    def summary(self) -> dict[str, int]:
        out = {"done": 0, "skipped": 0, "noop": 0, "failed": 0}
        for _, r in self.phases:
            if r.skipped:
                out["skipped"] += 1
            elif r.changed:
                out["done"] += 1
            else:
                out["noop"] += 1
        return out


def resolve_phases(
    registry: PhaseRegistry,
    selected: Iterable[str] | None,
) -> list[str]:
    """Resolve + topologically sort the requested phase list.

    `selected=None` means "every registered phase, in registry
    order". Otherwise it's a user-supplied subset.
    """
    requested = list(selected) if selected is not None else list(registry.all_names())
    return registry.topological_order(tuple(requested))


def build_topology(
    *,
    container: Container,
    proxmox_vms_repo: Path,
) -> ClusterTopology:
    """Read the upstream proxmox-vms output.json.

    `proxmox_vms_repo` is the path to the sibling repo (overridable
    via CLI). The orchestrator fills `container.upstream_topology`
    before any phase runs.
    """
    topo = read_upstream_topology(
        proxmox_vms_repo=proxmox_vms_repo,
        cluster_name=container.cluster_name,
    )
    container.upstream_topology = topo
    return topo


def parse_intent(main_tf: Path) -> ClusterIntent:
    """Parse the cluster root's main.tf and return its typed intent.

    `main_tf` is the path to the cluster root's `main.tf` file
    (not the cluster directory). The orchestrator's CLI builds
    this path before calling.
    """
    try:
        return parse_cluster_root(main_tf)
    except HclParseError as exc:
        raise BootstrapError("validate", {"hcl_error": str(exc)}) from exc


def attach_intent(container: Container, intent: ClusterIntent) -> None:
    """Stash the parsed intent on the container for phase consumption."""
    container.cluster_intent = intent


def run(
    container: Container,
    selected_phases: Iterable[str] | None = None,
) -> BootstrapPlan:
    """The main entry point. Runs every requested phase in dep order.

    Phase failures raise BootstrapError immediately; the orchestrator
    does NOT swallow them. M4 misfit: silent bootstrap failures are
    the worst kind of bug, because the operator only finds out when
    kubectl returns "no route to host".
    """
    registry = get_registry()
    phase_names = resolve_phases(registry, selected_phases)

    plan = BootstrapPlan(cluster_name=container.cluster_name)
    for name in phase_names:
        phase: Phase = registry.get(name)
        # State-store gate: if the phase already succeeded (recorded
        # by a previous `apply` on the same cluster_dir), and the
        # phase itself didn't override should_run to require
        # something fresher, skip the run. The phase's run() is
        # still idempotent, but skipping saves minutes on
        # already-bootstrapped clusters.
        if not phase.should_run(container):
            container.logger.info(
                step="phase_skipped",
                phase=name,
                reason="already marked done in bootstrap_state.json",
            )
            from .phases.base import PhaseResult
            skip_result: PhaseResult = PhaseResult.make_skipped(name, reason="already done (state cache)")
            plan.phases.append((name, skip_result))
            continue
        container.logger.info(step="phase_start", phase=name)
        try:
            result: PhaseResult = phase.run(container)
        except BootstrapError as exc:
            container.logger.error(
                step="phase_failed",
                error=f"phase {name!r} failed",
                resolution=f"detail={exc.detail}; see audit log + bootstrap_state.json",
            )
            raise
        # Record on the plan + the state store.
        plan.phases.append((name, result))
        if result.changed and not result.skipped:
            container.state_store.mark_done(name)
        container.logger.info(
            step="phase_done",
            phase=name,
            changed=result.changed,
            skipped=result.skipped,
        )
    plan.intent = container.cluster_intent
    return plan
