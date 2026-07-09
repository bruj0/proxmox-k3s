"""phases — one file per Phase.

The orchestrator imports this package to trigger the @register
decorators that populate the registry. Each phase is responsible
for one and only one logical step (single responsibility).

To add a new phase:
  1. Create provisioner/lib/phases/<name>.py
  2. Define a Phase subclass with a stable `name`
  3. Decorate it with `@register`
  4. Import it in this `__init__.py`

No changes needed in the orchestrator (open/closed).
"""

from __future__ import annotations

# Concrete phases are imported below this line. Each one
# self-registers via its @register decorator.
from . import (
    apiserver_ready,  # noqa: F401,E402
    base,  # noqa: F401  -- re-export for tests
    cilium_install,  # noqa: F401,E402
    gateway_crds,  # noqa: F401,E402
    helm_releases,  # noqa: F401,E402
    host_ports_check,  # noqa: F401,E402
    install_k3s,  # noqa: F401,E402
    kubeconfig_pull,  # noqa: F401,E402
    ssh_probe,  # noqa: F401,E402
    start_k3s_units,  # noqa: F401,E402
    topology_writer,  # noqa: F401,E402
    validate,  # noqa: F401,E402
)
from .base import Phase, PhaseRegistry, PhaseResult, get_registry, register


def all_phases() -> tuple[str, ...]:
    """Return the names of every registered phase, in registration order."""
    return get_registry().all_names()


__all__ = ["Phase", "PhaseRegistry", "PhaseResult", "all_phases", "get_registry", "register"]
