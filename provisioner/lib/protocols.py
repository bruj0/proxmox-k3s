"""SOLID abstractions (the S/D/I of the SOLID principles).

Every collaborator the phases need is exposed here as a `Protocol`.
Phases depend on these Protocols, NOT on concrete classes like
`PveSshProxy` or `K3sInstaller`. That gives us three benefits:

  1. **Testability**: a test can pass a `FakeRemoteExecutor` that
     records calls without touching SSH at all.
  2. **Substitutability**: any class with a matching `.run(...)`
     method can be a RemoteExecutor — `PveSshProxy` in production,
     a mock in tests.
  3. **Decoupling**: phases do not import k3s_installer, helm_client,
     pve_client, etc. directly. The DI container (container.py)
     wires the concrete implementations.

Naming convention: each Protocol has a single responsibility.
  - `RemoteExecutor` — runs commands on a remote host over SSH.
  - `ClusterProbe` — queries the cluster's live state (kubectl get, etc.).
  - `StateStore` — persists `phases_done` so re-runs are idempotent.
  - `OutputSink` — writes the final `k3s.json` artifact.

Anything more domain-specific (k3s installer, helm installer, etc.)
is a concrete class that the container wires into a phase. Phases
never import those concrete classes — they consume Protocols.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# ----------------------------------------------------------- shared types


@dataclass(frozen=True)
class RemoteResult:
    """The outcome of a remote command.

    `stdout` / `stderr` are strings (the SSH layer decoded them).
    `exit_code` is the remote process's exit status, NOT the SSH
    returncode. The SSH layer distinguishes "ssh itself failed" from
    "the remote command failed" by raising on the former.
    """

    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class VmTarget:
    """The minimum info needed to SSH to a VM.

    Same shape as one entry in the upstream proxmox-vms output.json
    (`role`, `name`, `vmid`, `ip`).
    """

    role: str          # "control_plane" | "worker"
    name: str
    vmid: int
    ip: str


@dataclass(frozen=True)
class ClusterTopology:
    """The upstream (proxmox-vms) cluster layout.

    `control_plane` and `worker` are kept separate so phases that
    only act on CPs (k3s server install, kubeconfig pull) don't
    accidentally loop over workers.
    """

    cluster_name: str
    control_plane: tuple[VmTarget, ...]
    worker: tuple[VmTarget, ...]

    @property
    def all_nodes(self) -> tuple[VmTarget, ...]:
        return (*self.control_plane, *self.worker)


# ----------------------------------------------------------- Protocols


@runtime_checkable
class RemoteExecutor(Protocol):
    """Run a shell command on a remote host and return its result.

    Implementations:
      - `PveSshRemoteAdapter` (production) — wraps PveSshProxy,
        adapts its `CompletedProcess` return into a `RemoteResult`
      - `FakeRemoteExecutor` (tests) — records calls in memory

    The Protocol intentionally returns `RemoteResult` (not
    `CompletedProcess`) so the phase code never imports subprocess.
    """

    def run(self, target: str, command: str, *, check: bool = True, timeout: float = 15.0) -> RemoteResult: ...


@runtime_checkable
class ClusterProbe(Protocol):
    """Read live state from the cluster's apiserver.

    The cluster is reached via a kubeconfig on disk that already
    points at a reachable apiserver (the orchestrator opens a
    tunnel before any phase probes). Implementations:
      - `KubectlClusterProbe` (production) — `kubectl get`/`describe`
      - `FakeClusterProbe` (tests) — canned responses
    """

    def list_pods(self, namespace: str = "kube-system") -> tuple[Mapping[str, Any], ...]: ...

    def get_nodes(self) -> tuple[Mapping[str, Any], ...]: ...

    def apiserver_reachable(self) -> bool: ...

    def helm_release_present(self, name: str, namespace: str) -> bool: ...


@runtime_checkable
class StateStore(Protocol):
    """Persist the set of phases already completed.

    A re-run on a steady-state cluster is a no-op for phases whose
    name is in `phases_done`. This is the cheapest possible
    idempotency mechanism (no `kubectl get`, no chart diff, just a
    JSON file read).

    Implementations:
      - `FileStateStore` (production) — writes
        `infra/clusters/<name>/bootstrap_state.json`
      - `InMemoryStateStore` (tests)
    """

    def phases_done(self) -> frozenset[str]: ...

    def mark_done(self, phase: str) -> None: ...

    def reset(self) -> None: ...


@runtime_checkable
class OutputSink(Protocol):
    """Write the final `k3s.json` artifact.

    Implementations:
      - `JsonFileOutputSink` (production) — writes
        `infra/clusters/<name>/k3s.json`
      - `DictOutputSink` (tests) — returns the dict
    """

    def write(self, payload: Mapping[str, Any]) -> Path: ...


@runtime_checkable
class VersionsSource(Protocol):
    """Read version pins for the cluster.

    Wraps `versions.VersionsLockReader` so the phases don't import
    the cicd vendored module directly.

    Implementations:
      - `LockfileVersionsSource` (production) — reads tools/versions.lock.yaml
      - `StaticVersionsSource` (tests)
    """

    def k3s_version(self) -> str: ...
    def cilium_chart_version(self) -> str: ...
    def helm_releases(self) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class SecretsSource(Protocol):
    """Surface secret values (cloudflare token, PVE credentials).

    Implementations:
      - `EnvSecretsSource` (production) — reads CF_API_TOKEN, etc.
      - `StaticSecretsSource` (tests)
    """

    def cf_api_token(self) -> str: ...
    def cf_account_id(self) -> str: ...


# ----------------------------------------------------------- helpers


class BootstrapError(RuntimeError):
    """Raised by phases when a recoverable failure occurs.

    Carries structured detail so the operator / CI can parse the
    reason without scraping log text. Same shape as the cicd repo's
    BootstrapError for cross-repo grep parity.
    """

    def __init__(self, phase: str, detail: Mapping[str, str]) -> None:
        self.phase = phase
        self.detail = dict(detail)
        super().__init__(
            f"bootstrap failed in phase {phase!r}: "
            f"{json.dumps(detail, sort_keys=True)}"
        )


# ----------------------------------------------------------- adapters


class PveSshRemoteAdapter:
    """Adapts the cicd repo's `PveSshProxy` to our `RemoteExecutor`.

    The vendored `PveSshProxy.run` returns `subprocess.CompletedProcess`.
    Our `RemoteExecutor` protocol returns `RemoteResult`. The adapter
    translates between them and normalises the timeout type
    (`float` here vs `int` in the proxy).
    """

    def __init__(self, proxy: Any) -> None:
        self._proxy = proxy

    def run(
        self,
        target: str,
        command: str,
        *,
        check: bool = True,
        timeout: float = 15.0,
    ) -> RemoteResult:
        completed: subprocess.CompletedProcess[str] = self._proxy.run(
            target,
            command,
            check=check,
            timeout=int(timeout),
        )
        return RemoteResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
        )
