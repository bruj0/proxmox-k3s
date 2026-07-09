"""Phase base class + registry (Open/Closed + Liskov principles).

Every step of the bootstrap (install k3s, apply gateway CRDs, install
helm charts, ...) is a `Phase`. Each Phase:

  - Has a stable `name` (used in `--phases` CLI arg + bootstrap_state.json).
  - Declares its dependencies on OTHER phases via `requires`
    (so the orchestrator can topologically sort).
  - Implements `run(ctx)` returning a `PhaseResult`.
  - Is **idempotent**: a re-run sees `ctx.state.phases_done()` and
    short-circuits to `PhaseResult.make_skipped()`.
  - Lives in its own file under `phases/` and registers itself
    via the `@register` decorator. The bootstrap orchestrator
    discovers phases by importing `provisioner.phases` (which
    imports every submodule).

Open/Closed: new phases = new files. The orchestrator never edits
its list of phases — the registry does that.
Liskov: every Phase is substitutable for any other Phase; the
orchestrator calls `.run(ctx)` uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..container import Container
from ..protocols import BootstrapError


@dataclass(frozen=True)
class PhaseResult:
    """The outcome of a phase.

    `skipped` is set when the phase ran noop (already done OR no
    work to do). `changed` is True if the phase actually mutated
    state. `data` is the phase's structured payload — the
    orchestrator collects these to populate `k3s.json`.
    """

    name: str
    changed: bool = False
    skipped: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make_done(cls, name: str, **data: Any) -> PhaseResult:
        return cls(name=name, changed=True, data=dict(data))

    @classmethod
    def make_noop(cls, name: str, **data: Any) -> PhaseResult:
        return cls(name=name, changed=False, data=dict(data))

    @classmethod
    def make_skipped(cls, name: str, reason: str = "") -> PhaseResult:
        return cls(name=name, changed=False, skipped=True, data={"reason": reason})


class Phase(ABC):
    """Base class for every bootstrap step.

    Subclasses must:
      - set `name` (the public identifier; stable across runs)
      - set `requires` (a tuple of phase names that must run before this one)
      - implement `run(ctx) -> PhaseResult`
      - optionally override `should_run(ctx) -> bool` for finer-grained
        skipping (e.g. install_k3s only on a cluster that has no
        k3s systemd unit)
    """

    name: ClassVar[str] = ""
    requires: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} must set `name`")
        if not self.requires and self.name != "validate":
            # `validate` has no deps (it's the first phase in plan mode).
            pass

    @abstractmethod
    def run(self, ctx: Container) -> PhaseResult: ...

    def should_run(self, ctx: Container) -> bool:
        """Override to gate the phase on cluster state.

        Default: run unless `ctx.state.phases_done()` already lists
        this phase. Phases that need richer gating (e.g. "skip if
        apiserver unreachable AND no k3s installed") override this.
        """
        return self.name not in ctx.state_store.phases_done()

    def done_data(self, ctx: Container) -> dict[str, Any]:
        """Hook for phases to publish structured data on completion.

        The orchestrator merges the result.data from every phase
        into the final `k3s.json` payload. Default: nothing.
        """
        return {}


# ----------------------------------------------------------- registry


class PhaseRegistry:
    """Maps phase names to Phase instances.

    The registry is populated by the `@register` decorator at
    import time. The orchestrator looks up phases by name when
    resolving `--phases a,b,c` and when computing the topological
    order of dependencies.
    """

    def __init__(self) -> None:
        self._phases: dict[str, Phase] = {}

    def register(self, phase: Phase) -> None:
        if phase.name in self._phases:
            raise ValueError(f"duplicate phase name: {phase.name!r}")
        self._phases[phase.name] = phase

    def get(self, name: str) -> Phase:
        if name not in self._phases:
            raise KeyError(f"unknown phase: {name!r}; available: {sorted(self._phases)}")
        return self._phases[name]

    def all_names(self) -> tuple[str, ...]:
        return tuple(self._phases.keys())

    def topological_order(self, requested: tuple[str, ...]) -> list[str]:
        """Return `requested` ordered so each phase's deps come first.

        Raises BootstrapError on a cycle or a missing dep.
        """
        for name in requested:
            if name not in self._phases:
                raise BootstrapError("plan", {"unknown_phase": name})
        # DFS-based topological sort with cycle detection.
        order: list[str] = []
        seen: set[str] = set()
        on_stack: set[str] = set()

        def visit(name: str) -> None:
            if name in seen:
                return
            if name in on_stack:
                raise BootstrapError("plan", {"cycle": name})
            on_stack.add(name)
            for dep in self._phases[name].requires:
                if dep not in self._phases:
                    raise BootstrapError("plan", {"missing_dep": f"{name} -> {dep}"})
                visit(dep)
            on_stack.discard(name)
            seen.add(name)
            order.append(name)

        for name in requested:
            visit(name)
        return order


# Module-level singleton. The @register decorator populates it.
_REGISTRY = PhaseRegistry()


def register(cls: type[Phase]) -> type[Phase]:
    """Class decorator: instantiate and register a Phase subclass.

    Used as:
        @register
        class MyPhase(Phase):
            name = "my_phase"
            ...

    The decorator instantiates the class (no args needed) and
    adds it to the module-level registry. Returning the class
    itself keeps the decorator transparent to subclassers.
    """
    instance = cls()
    _REGISTRY.register(instance)
    return cls


def get_registry() -> PhaseRegistry:
    """Return the module-level phase registry."""
    return _REGISTRY
