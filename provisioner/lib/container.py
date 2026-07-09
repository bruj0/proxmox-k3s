"""DI container — wires concrete implementations behind the Protocols.

The orchestrator constructs one `Container` per `bootstrap apply`
invocation. The container is the **only** place in the codebase
that knows about `PveSshProxy`, `K3sInstaller`, `SecretLoader`, etc.

Phases receive a `Container` and read `.remote`, `.probe`, etc.
They never import concrete classes directly. That is the
Dependency-Inversion principle in action.

Substitution rule: `Container.for_tests(...)` returns a container
backed entirely by in-memory fakes. Production callers use
`Container.production(...)`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .log import StructuredLogger
from .protocols import (
    ClusterProbe,
    ClusterTopology,
    OutputSink,
    RemoteExecutor,
    RemoteResult,
    SecretsSource,
    StateStore,
    VersionsSource,
)
from .pve_ssh import PveSshProxy
from .secret_loader import SecretLoader
from .versions import VersionsLockReader

# ----------------------------------------------------------- fakes (live here, not in tests/)

# Fakes live in the production package, not in tests/, so other
# packages (and the next operator who forks this repo) can reuse them
# without copying the file. Each one implements exactly one Protocol.


class FakeRemoteExecutor:
    """In-memory RemoteExecutor — records every run() for assertions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        # `responses` maps (target, command_prefix) -> RemoteResult.
        # The test sets up entries; the fake returns them on match.
        self.responses: dict[tuple[str, str], RemoteResult] = {}
        self.default_response = RemoteResult(stdout="", stderr="", exit_code=0)

    def queue(self, target: str, command_prefix: str, result: RemoteResult) -> None:
        self.responses[(target, command_prefix)] = result

    def run(
        self,
        target: str,
        command: str,
        *,
        check: bool = True,
        timeout: float = 15.0,
    ) -> RemoteResult:
        self.calls.append(
            {"target": target, "command": command, "check": check, "timeout": timeout}
        )
        for (tgt, prefix), result in self.responses.items():
            if tgt == target and command.startswith(prefix):
                return result
        return self.default_response


class InMemoryStateStore:
    """In-memory StateStore — phases_done lives in a frozenset."""

    def __init__(self, initial: frozenset[str] | None = None) -> None:
        self._phases: set[str] = set(initial or ())

    def phases_done(self) -> frozenset[str]:
        return frozenset(self._phases)

    def mark_done(self, phase: str) -> None:
        self._phases.add(phase)

    def reset(self) -> None:
        self._phases.clear()


class FileStateStore:
    """File-backed StateStore — reads/writes bootstrap_state.json."""

    def __init__(self, cluster_dir: Path) -> None:
        self._path = cluster_dir / "bootstrap_state.json"
        self._cache: set[str] | None = None

    def _load(self) -> set[str]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = set()
            return self._cache
        import json
        data = json.loads(self._path.read_text())
        self._cache = set(data.get("phases_done", []))
        return self._cache

    def phases_done(self) -> frozenset[str]:
        return frozenset(self._load())

    def mark_done(self, phase: str) -> None:
        phases = self._load()
        phases.add(phase)
        import json
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"phases_done": sorted(phases)}, indent=2))

    def reset(self) -> None:
        self._cache = set()
        if self._path.exists():
            self._path.unlink()


class EnvSecretsSource:
    """Read secrets from the operator's environment / .env file."""

    def __init__(self, env_file: Path | None = None) -> None:
        if env_file and env_file.exists():
            for raw in env_file.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)

    def cf_api_token(self) -> str:
        return os.environ.get("CF_API_TOKEN", "")

    def cf_account_id(self) -> str:
        return os.environ.get("CF_ACCOUNT_ID", "")


class StaticSecretsSource:
    """Secrets source for tests — values are passed in directly."""

    def __init__(self, *, cf_api_token: str = "", cf_account_id: str = "") -> None:
        self._cf_token = cf_api_token
        self._cf_account = cf_account_id

    def cf_api_token(self) -> str:
        return self._cf_token

    def cf_account_id(self) -> str:
        return self._cf_account


class LockfileVersionsSource:
    """Read pinned versions from tools/versions.lock.yaml.

    The cicd vendored `VersionsLockReader` exposes only `k3s_stable_version`
    and `helm_floor`. We extend its surface with `cilium_chart_version`
    and `helm_releases` by reading the lockfile's
    `additional_dependencies` list (the format the proxmox-k3s repo
    writes — see tools/versions.lock.yaml).
    """

    def __init__(self, repo_root: Path, logger: StructuredLogger | None = None) -> None:
        self._reader = VersionsLockReader.from_default(repo_root=repo_root, logger=logger)
        # Read the lockfile directly so we can surface the
        # additional_dependencies entries as a typed list.
        import yaml
        lockfile = repo_root / "tools" / "versions.lock.yaml"
        if lockfile.exists():
            try:
                self._payload = yaml.safe_load(lockfile.read_text()) or {}
            except yaml.YAMLError:
                self._payload = {}
        else:
            self._payload = {}

    def k3s_version(self) -> str:
        return self._reader.k3s_stable_version

    def cilium_chart_version(self) -> str:
        # The proxmox-k3s lockfile pins cilium via
        # additional_dependencies[name=cilium].version.
        for entry in self._payload.get("additional_dependencies", []) or []:
            if isinstance(entry, dict) and entry.get("name") == "cilium":
                return str(entry.get("version", "1.19.5"))
        return "1.19.5"

    def helm_releases(self) -> list[dict[str, Any]]:
        # Surface every additional_dependencies entry as a typed dict.
        out: list[dict[str, Any]] = []
        for entry in self._payload.get("additional_dependencies", []) or []:
            if isinstance(entry, dict):
                out.append(dict(entry))
        return out


class StaticVersionsSource:
    """Static versions for tests."""

    def __init__(
        self,
        *,
        k3s_version: str = "v1.36.2+k3s1",
        cilium_chart_version: str = "1.19.5",
        helm_releases: list[dict[str, Any]] | None = None,
    ) -> None:
        self._k3s = k3s_version
        self._cilium = cilium_chart_version
        self._releases = helm_releases or []

    def k3s_version(self) -> str:
        return self._k3s

    def cilium_chart_version(self) -> str:
        return self._cilium

    def helm_releases(self) -> list[dict[str, Any]]:
        return list(self._releases)


class JsonFileOutputSink:
    """Write the `k3s.json` artifact under infra/clusters/<name>/."""

    def __init__(self, cluster_dir: Path) -> None:
        self._path = cluster_dir / "k3s.json"

    def write(self, payload: Any) -> Path:
        # Signature widened to Any so the OutputSink protocol's
        # Mapping[str, Any] parameter type checks pass.
        import json
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return self._path


class DictOutputSink:
    """In-memory output sink for tests."""

    def __init__(self) -> None:
        self.last: dict[str, Any] | None = None

    def write(self, payload: dict[str, Any]) -> Path:
        self.last = dict(payload)
        from pathlib import Path
        return Path("/tmp/proxmox-k3s-test-output.json")


class KubectlClusterProbe:
    """Production ClusterProbe — shells out to kubectl on the operator host.

    The orchestrator writes a kubeconfig to a temp path that points
    at the apiserver tunnel (`127.0.0.1:<local_port>`), so kubectl
    commands resolve correctly without the operator needing to set
    `KUBECONFIG`.
    """

    def __init__(self, kubeconfig: Path, logger: StructuredLogger | None = None) -> None:
        self._kubeconfig = kubeconfig
        self._log = logger

    def _kubectl(self, *args: str, timeout: float = 30.0) -> str:
        import subprocess
        cmd = ["kubectl", "--kubeconfig", str(self._kubeconfig), *args]
        if self._log is not None:
            self._log.info(step="kubectl_call", args=list(args))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def list_pods(self, namespace: str = "kube-system") -> tuple[dict[str, Any], ...]:
        import json
        out = self._kubectl("get", "pods", "-n", namespace, "-o", "json")
        data = json.loads(out)
        return tuple(data.get("items", []))

    def get_nodes(self) -> tuple[dict[str, Any], ...]:
        import json
        out = self._kubectl("get", "nodes", "-o", "json")
        data = json.loads(out)
        return tuple(data.get("items", []))

    def apiserver_reachable(self) -> bool:
        try:
            out = self._kubectl("get", "--raw", "/healthz", timeout=10.0)
            return "ok" in out.lower()
        except Exception:
            return False

    def helm_release_present(self, name: str, namespace: str) -> bool:
        try:
            out = self._kubectl(
                "get", "svc,deployment,daemonset,statefulset",
                "-n", namespace,
                "-l", f"app.kubernetes.io/instance={name}",
                "-o", "name",
                timeout=10.0,
            )
            return bool(out.strip())
        except Exception:
            return False


class FakeClusterProbe:
    """In-memory ClusterProbe for tests."""

    def __init__(
        self,
        *,
        pods: tuple[dict[str, Any], ...] = (),
        nodes: tuple[dict[str, Any], ...] = (),
        apiserver_ok: bool = True,
        helm_releases: set[tuple[str, str]] | None = None,
    ) -> None:
        self._pods = pods
        self._nodes = nodes
        self._apiserver_ok = apiserver_ok
        self._releases = helm_releases or set()

    def list_pods(self, namespace: str = "kube-system") -> tuple[dict[str, Any], ...]:
        return self._pods

    def get_nodes(self) -> tuple[dict[str, Any], ...]:
        return self._nodes

    def apiserver_reachable(self) -> bool:
        return self._apiserver_ok

    def helm_release_present(self, name: str, namespace: str) -> bool:
        return (name, namespace) in self._releases


# ----------------------------------------------------------- the container


@dataclass
class Container:
    """The DI container — wires protocols to concrete implementations.

    The orchestrator constructs one of these per `bootstrap apply`.
    Phases read the fields they need. NO production code outside
    `container.py` and the orchestrator should import the concrete
    classes (PveSshProxy, K3sInstaller, SecretLoader, ...).
    """

    logger: StructuredLogger
    remote: RemoteExecutor
    cluster_probe: ClusterProbe
    state_store: StateStore
    output_sink: OutputSink
    versions: VersionsSource
    secrets: SecretsSource

    # Topology (set by orchestrator after parsing main.tf + output.json).
    cluster_dir: Path
    repo_root: Path
    cluster_name: str
    upstream_topology: ClusterTopology | None = None
    cluster_intent: Any = None  # ClusterIntent from hcl_parser (set by orchestrator)

    # The raw VersionsLockReader (cicd vendored module). Kept here
    # so install_k3s can hand it to K3sInstaller (which expects
    # the cicd type, not our Protocol wrapper).
    versions_reader: Any = None

    # Phase gate: `--phases a,b,c` from the CLI narrows the set.
    selected_phases: tuple[str, ...] = field(default_factory=tuple)

    # The PveSshProxy (only populated in production). Kept here so the
    # tunnel-management helper can call into it without going through
    # the Protocol (which only exposes .run()).
    pve_proxy: PveSshProxy | None = None
    # Optional SecretLoader for chart values files (production only).
    secret_loader: SecretLoader | None = None

    @classmethod
    def production(
        cls,
        *,
        logger: StructuredLogger,
        repo_root: Path,
        cluster_dir: Path,
        cluster_name: str,
        kubeconfig: Path,
        ssh_proxy_target: str,
        ssh_user: str = "ubuntu",
        env_file: Path | None = None,
    ) -> Container:
        """Build the production container."""
        from .protocols import PveSshRemoteAdapter
        proxy = PveSshProxy(jump_host=ssh_proxy_target, ssh_user=ssh_user, logger=logger)
        return cls(
            logger=logger,
            remote=PveSshRemoteAdapter(proxy),
            cluster_probe=KubectlClusterProbe(kubeconfig=kubeconfig, logger=logger),
            state_store=FileStateStore(cluster_dir=cluster_dir),
            output_sink=JsonFileOutputSink(cluster_dir=cluster_dir),
            versions=LockfileVersionsSource(repo_root=repo_root, logger=logger),
            secrets=EnvSecretsSource(env_file=env_file),
            cluster_dir=cluster_dir,
            repo_root=repo_root,
            cluster_name=cluster_name,
            pve_proxy=proxy,
            secret_loader=SecretLoader(logger=logger),
        )

    @classmethod
    def for_tests(
        cls,
        *,
        cluster_dir: Path | None = None,
        repo_root: Path | None = None,
        cluster_name: str = "demo",
        remote: RemoteExecutor | None = None,
        cluster_probe: ClusterProbe | None = None,
        state_store: StateStore | None = None,
        output_sink: OutputSink | None = None,
        versions: VersionsSource | None = None,
        secrets: SecretsSource | None = None,
        logger: StructuredLogger | None = None,
    ) -> Container:
        """Build a container backed by fakes. The default factory."""
        from pathlib import Path
        return cls(
            logger=logger or StructuredLogger("test"),
            remote=remote or FakeRemoteExecutor(),
            cluster_probe=cluster_probe or FakeClusterProbe(),
            state_store=state_store or InMemoryStateStore(),
            output_sink=output_sink or DictOutputSink(),  # type: ignore[arg-type]
            versions=versions or StaticVersionsSource(),
            secrets=secrets or StaticSecretsSource(),
            cluster_dir=cluster_dir or Path("/tmp/proxmox-k3s-test"),
            repo_root=repo_root or Path("/tmp/proxmox-k3s-test-root"),
            cluster_name=cluster_name,
        )
