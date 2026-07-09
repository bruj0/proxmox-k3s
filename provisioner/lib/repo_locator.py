"""Locate the proxmox-k8s-cicd repo root from an installed CLI.

The two operator CLIs (`tools/ssh_proxy.py`, `tools/kubeconfig_puller.py`)
read `infra/clusters/<name>/output.json` to find SDN IPs. When the
tools are run from a `uv tool install`d venv, the script's
`__file__` points into site-packages, not the repo. So the
canonical "walk up to find output.json" pattern doesn't work
out of the box.

Resolution order (first match wins):

  1. Explicit `--repo-root` flag (caller wins, always).
  2. `PROXMOX_K8S_REPO` env var (also explicit; survives across
     shell invocations).
  3. Current working directory, if `infra/clusters/` exists there.
     Lets the operator run `kubeconfig-puller --cluster cicd`
     directly from the repo root without setting any env.
  4. Walk up from the cwd looking for an ancestor that contains
     `infra/clusters/`. Handles the common case where the
     operator is in a subdirectory of the repo.
  5. Raise `RepoNotFoundError` listing every location tried and
     every cluster directory it found, so the error message is
     actionable.
"""
from __future__ import annotations

import os
from pathlib import Path

# A directory is "the repo" iff it contains this subdirectory.
_REPO_MARKER = "infra/clusters"


class RepoNotFoundError(RuntimeError):
    """Raised when no repo root can be located.

    Carries the searched paths and the discovered cluster dirs
    (if any) so the operator can see at a glance whether they're
    in the right tree.
    """

    def __init__(
        self,
        searched: list[Path],
        discovered_clusters: list[Path],
    ) -> None:
        self.searched = searched
        self.discovered_clusters = discovered_clusters
        locations = "\n".join(f"  - {p}" for p in searched) or "  (none)"
        clusters = (
            "\n".join(f"  - {p}" for p in discovered_clusters)
            or "  (none found)"
        )
        super().__init__(
            "could not locate the proxmox-k8s-cicd repo root.\n"
            "searched:\n"
            f"{locations}\n"
            "cluster output.json dirs found on disk:\n"
            f"{clusters}\n"
            "fix: pass --repo-root <path>, or set PROXMOX_K8S_REPO, "
            "or run the command from a directory that contains "
            f"`{_REPO_MARKER}/`."
        )


def _looks_like_repo(p: Path) -> bool:
    """True iff `p/_REPO_MARKER` exists and is a directory."""
    return (p / _REPO_MARKER).is_dir()


def _walk_up_for_repo(start: Path) -> Path | None:
    """Walk up from `start` looking for an ancestor containing
    `_REPO_MARKER`. Stops at the filesystem root.
    """
    p = start.resolve()
    for candidate in (p, *p.parents):
        if _looks_like_repo(candidate):
            return candidate
    return None


def _list_cluster_dirs() -> list[Path]:
    """Best-effort list of `infra/clusters/*/output.json` parents
    found anywhere on the cwd walk. Used purely for the error
    message so the operator sees the on-disk state.
    """
    out: list[Path] = []
    p = Path.cwd().resolve()
    seen: set[Path] = set()
    for candidate in (p, *p.parents):
        cluster_root = candidate / "infra" / "clusters"
        if (
            cluster_root.is_dir()
            and cluster_root not in seen
        ):
            seen.add(cluster_root)
            for child in sorted(cluster_root.iterdir()):
                if (child / "output.json").exists():
                    out.append(child)
    return out


def locate_repo_root(
    *,
    flag_value: str | None = None,
) -> Path:
    """Return the best guess at the repo root, or raise
    `RepoNotFoundError` with a clear message.

    Args:
      flag_value: the value of the CLI's `--repo-root` flag, if
        the operator passed one. Always wins.
    """
    searched: list[Path] = []

    # 1. Explicit --repo-root.
    if flag_value:
        explicit = Path(flag_value).expanduser().resolve()
        if _looks_like_repo(explicit):
            return explicit
        searched.append(explicit)

    # 2. PROXMOX_K8S_REPO env var.
    env = os.environ.get("PROXMOX_K8S_REPO")
    if env:
        from_env = Path(env).expanduser().resolve()
        if _looks_like_repo(from_env):
            return from_env
        searched.append(from_env)

    # 3. Cwd (cheap check; common case for in-repo use).
    cwd = Path.cwd().resolve()
    if _looks_like_repo(cwd):
        return cwd
    searched.append(cwd)

    # 4. Walk up from cwd.
    walked = _walk_up_for_repo(cwd)
    if walked is not None:
        return walked

    # 5. Nothing found. Raise with all the diagnostic data.
    raise RepoNotFoundError(
        searched=searched,
        discovered_clusters=_list_cluster_dirs(),
    )
