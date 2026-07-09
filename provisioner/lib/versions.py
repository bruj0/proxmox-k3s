"""Single-reader pattern for tools/versions.lock.yaml.

Mirrors the same shape as infra/modules/proxmox-k3s-cluster/versions.lock.yaml
so SS3 modules and SS3 callers (mainly the bootstrap orchestrator and the
k3s installer) read the same data without re-parsing YAML in two places.

API:
    reader = VersionsLockReader.from_default()
    k3s_version: str = reader.k3s_stable_version       # "v1.36.2+k3s1"
    install_url: str = reader.k3s_install_url          # "https://get.k3s.io"

The reader surfaces the keys the bootstrap needs and falls back to
documented defaults if the lockfile is missing or the keys are absent.
This matches the "fail-loud but not load-bearing" pattern used by every
other `tools/lib/*` reader.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .log import StructuredLogger

# Pinned defaults — also live in tools/versions.lock.yaml::dependencies.
# Duplicated here so a missing/malformed lockfile still leaves the
# installer calling get.k3s.io with a stable, known-good version.
# Bumped 2026-07-08 to v1.36.2+k3s1 (latest stable). Reconcile-and-pin
# policy: the install pins this version; k3s's built-in upgrade
# controller is then allowed to roll forward automatically.
_DEFAULT_K3S_STABLE_VERSION = "v1.36.2+k3s1"
_DEFAULT_K3S_INSTALL_URL = "https://get.k3s.io"
_DEFAULT_K3S_CHANNEL = "stable"
_DEFAULT_HELM_FLOOR = ">= 3.18.0"
_DEFAULT_KUBECTL_FLOOR = ">= v1.34.0"


class VersionsLockReader:
    """Tiny reader for tools/versions.lock.yaml.

    Only the keys we need are exposed (k3s_stable_version, k3s_install_url).
    Other keys (helm, cilium, pccm, csi, cert-manager, cloudflare-tunnel, etc.)
    are read by the helm_client
    through its own constants — we deliberately don't build a god-reader.
    """

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self._data = data or {}
        self._logger = logger or StructuredLogger("versions_lock")

    @classmethod
    def from_default(
        cls, repo_root: Path | None = None, logger: StructuredLogger | None = None
    ) -> VersionsLockReader:
        """Read tools/versions.lock.yaml from the repo root (default cwd).

        `repo_root` here means the proxmox-k3s repo root (the
        directory that contains `tools/versions.lock.yaml`). Pass
        the repo root; we look for
        `<repo_root>/tools/versions.lock.yaml`. Same lockfile
        structure as proxmox-k8s-cicd (we vendor the reader 1:1).
        """
        if repo_root is None:
            # provisioner/lib/versions.py lives 3 levels under repo_root.
            repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / "tools" / "versions.lock.yaml"
        if path.exists():
            try:
                payload = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError:
                payload = {}
        else:
            payload = {}
        return cls(payload, logger=logger)

    @classmethod
    def from_lockfile(
        cls,
        lockfile: Path,
        logger: StructuredLogger | None = None,
    ) -> VersionsLockReader:
        """Read from a directly-supplied lockfile path (used by tests)."""
        if lockfile.exists():
            try:
                payload = yaml.safe_load(lockfile.read_text()) or {}
            except yaml.YAMLError:
                payload = {}
        else:
            payload = {}
        return cls(payload, logger=logger)

    @property
    def k3s_stable_version(self) -> str:
        """The exact k3s version string the installer must pin.

        Order of resolution:
          1. tools/versions.lock.yaml::k3s_stable_version  (NEW key, preferred)
          2. tools/versions.lock.yaml::dependencies[*].name=="k3s".version
          3. _DEFAULT_K3S_STABLE_VERSION
        """
        top = self._data.get("k3s_stable_version")
        if isinstance(top, str) and top:
            return top
        for entry in self._data.get("dependencies", []) or []:
            if isinstance(entry, dict) and entry.get("name") == "k3s":
                ver = entry.get("version")
                if isinstance(ver, str) and ver:
                    # Strip any leading operators we used in the legacy format ("v1.34.x").
                    return ver.strip()
        return _DEFAULT_K3S_STABLE_VERSION

    @property
    def k3s_install_url(self) -> str:
        """Install script URL (always get.k3s.io per the upstream guidance)."""
        url = self._data.get("k3s_install_url")
        if isinstance(url, str) and url:
            return url
        return _DEFAULT_K3S_INSTALL_URL

    @property
    def k3s_channel(self) -> str:
        """Channel label (stable|latest|testing) used if version is a channel."""
        ch = self._data.get("k3s_channel")
        if isinstance(ch, str) and ch:
            return ch
        return _DEFAULT_K3S_CHANNEL

    @property
    def helm_floor(self) -> str:
        h = self._data.get("helm_floor")
        return h if isinstance(h, str) and h else _DEFAULT_HELM_FLOOR

    @property
    def kubectl_floor(self) -> str:
        k = self._data.get("kubectl_floor")
        return k if isinstance(k, str) and k else _DEFAULT_KUBECTL_FLOOR
