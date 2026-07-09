"""Tests for tools/lib/repo_locator.py.

The two operator CLIs (`ssh_proxy`, `kubeconfig_puller`) read
`infra/clusters/<name>/output.json` from the repo. When installed
via `uv tool install`, the script's `__file__` is in site-packages,
so the canonical "walk up from `__file__`" pattern doesn't work.
This module's resolver:

  1. Honors an explicit `--repo-root` flag.
  2. Honors the `PROXMOX_K8S_REPO` env var.
  3. Tries the cwd.
  4. Walks up from the cwd.
  5. Raises a clear error listing all attempts and any
     cluster dirs found on disk.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.repo_locator import (  # noqa: E402
    RepoNotFoundError,
    locate_repo_root,
)


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """Make a directory tree that looks like the repo:
    tmp_path/infra/clusters/<name>/output.json
    """
    clusters = tmp_path / "infra" / "clusters" / "cicd"
    clusters.mkdir(parents=True)
    (clusters / "output.json").write_text("{}")
    return tmp_path


def test_explicit_flag_wins(tmp_path: Path, fake_repo: Path) -> None:
    """An explicit --repo-root always wins, even if the env var
    points elsewhere."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "infra" / "clusters").mkdir(parents=True)
    import os
    os.environ["PROXMOX_K8S_REPO"] = str(other)
    try:
        result = locate_repo_root(flag_value=str(fake_repo))
        assert result == fake_repo.resolve()
    finally:
        os.environ.pop("PROXMOX_K8S_REPO", None)


def test_env_var_when_no_flag(
    tmp_path: Path, fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROXMOX_K8S_REPO", str(fake_repo))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = locate_repo_root()
    assert result == fake_repo.resolve()


def test_cwd_when_no_flag_no_env(
    tmp_path: Path, fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most common case: operator is in the repo root and runs
    the CLI without any flag or env var."""
    monkeypatch.delenv("PROXMOX_K8S_REPO", raising=False)
    monkeypatch.chdir(fake_repo)
    result = locate_repo_root()
    assert result == fake_repo.resolve()


def test_walks_up_from_subdirectory(
    tmp_path: Path, fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cd infra/clusters/cicd && kubeconfig-puller ...` should
    still find the repo, by walking up two levels."""
    monkeypatch.delenv("PROXMOX_K8S_REPO", raising=False)
    sub = fake_repo / "infra" / "clusters" / "cicd"
    monkeypatch.chdir(sub)
    result = locate_repo_root()
    assert result == fake_repo.resolve()


def test_raises_with_searched_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When nothing works, the error message must list every
    location tried so the operator can fix the env or cwd."""
    monkeypatch.delenv("PROXMOX_K8S_REPO", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RepoNotFoundError) as excinfo:
        locate_repo_root()
    # The cwd is in the searched list.
    assert tmp_path.resolve() in excinfo.value.searched
    # The error message mentions how to fix it.
    assert "PROXMOX_K8S_REPO" in str(excinfo.value)


def test_raises_with_env_var_in_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If PROXMOX_K8S_REPO points at a non-repo dir, it appears
    in the searched list so the operator notices the misconfig."""
    monkeypatch.setenv("PROXMOX_K8S_REPO", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RepoNotFoundError) as excinfo:
        locate_repo_root()
    assert tmp_path.resolve() in excinfo.value.searched


def test_explicit_flag_must_point_at_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --repo-root that doesn't contain infra/clusters
    is treated as wrong -- the operator probably fat-fingered the
    path -- and falls through to env/cwd/walk-up."""
    not_repo = tmp_path / "not-a-repo"
    not_repo.mkdir()
    monkeypatch.chdir(not_repo)
    with pytest.raises(RepoNotFoundError):
        locate_repo_root(flag_value=str(not_repo))


def test_discovered_clusters_appear_in_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the resolver fails AND there's nothing on the cwd
    walk that looks like a repo, the discovered_clusters list
    is empty (we don't make up paths)."""
    monkeypatch.delenv("PROXMOX_K8S_REPO", raising=False)
    monkeypatch.chdir(tmp_path)  # nothing under tmp_path looks like a repo
    with pytest.raises(RepoNotFoundError) as excinfo:
        locate_repo_root()
    # Nothing on the walk -- discovered is empty.
    assert excinfo.value.discovered_clusters == []
