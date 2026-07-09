"""tools.repo_locator — find the proxmox-k3s repo root.

Mirrors the cicd repo's `tools/lib/repo_locator.py` but standalone
(the operator tools here are meant to run from any cwd; they don't
import the orchestrator).

Resolution order:
  1. The explicit --repo-root CLI flag (if passed).
  2. The PROXMOX_K3S_REPO env var (CI / wrapper-script override).
  3. The current working directory, if it contains infra/clusters/.
  4. Walk up from cwd to the filesystem root; the first ancestor
     that contains infra/clusters/ wins.
  5. Bail with RepoNotFoundError (caught by main() for a structured
     error message).
"""

from __future__ import annotations

import os
from pathlib import Path


class RepoNotFoundError(RuntimeError):
    """Couldn't locate the proxmox-k3s repo root from any source."""


def locate_repo_root(*, flag_value: str | None = None) -> Path:
    """Return the proxmox-k3s repo root (directory containing infra/clusters/).

    See module docstring for resolution order.
    """
    candidates: list[Path] = []
    if flag_value:
        candidates.append(Path(flag_value).expanduser().resolve())
    env = os.environ.get("PROXMOX_K3S_REPO")
    if env:
        candidates.append(Path(env).expanduser().resolve())
    candidates.append(Path.cwd().resolve())
    cwd = Path.cwd().resolve()
    for ancestor in [cwd, *cwd.parents]:
        candidates.append(ancestor)

    seen: set[Path] = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if (c / "infra" / "clusters").is_dir():
            return c
    raise RepoNotFoundError(
        "could not find a proxmox-k3s repo root from any source. "
        "Pass --repo-root /path/to/proxmox-k3s, set PROXMOX_K3S_REPO, "
        "or run from a directory (or ancestor) that contains "
        "infra/clusters/."
    )
