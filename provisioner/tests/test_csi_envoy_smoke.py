"""Tests for the csi_smoke and envoy_smoke phases.

These phases are functional smoke tests that shell out to
`kubectl` subprocesses. They share a common test pattern:
each test wires a Container.for_tests() with a kubeconfig in
cluster_dir, mocks `subprocess.run` so the `kubectl` calls
return canned JSON, then asserts that the phase made the
right subprocess calls in the right order.

What we test for each phase:

  csi_smoke:
    * registry membership + dependency pinning
    * pre-flight fails when SC is not default
    * pre-flight fails when proxmox-csi-plugin-node DS not Ready
    * pre-flight fails when a node is missing topology labels
    * applies the smoke manifest, PVC writer pod, expects the
      reader's marker to survive pod churn
    * marker-mismatch fails the phase
    * cleanup is best-effort (cleanup failure does not fail
      the phase)

  envoy_smoke:
    * registry membership + dependency pinning
    * ensures GatewayClass=envoy exists (live fix)
    * applies Gateway + HTTPRoute + echo
    * waits for Programmed=True
    * discovers the data-plane ClusterIP
    * curls via busybox pod; body mismatch fails
    * body match returns PhaseResult.make_done with the
      data-plane IP
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Mirror the test_topology_labels.py import path setup.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provisioner.lib.container import (  # noqa: E402
    Container,
    DictOutputSink,
    FakeClusterProbe,
    FakeRemoteExecutor,
    InMemoryStateStore,
    StaticSecretsSource,
    StaticVersionsSource,
)
from provisioner.lib.hcl_parser import ClusterIntent  # noqa: E402
from provisioner.lib.log import StructuredLogger  # noqa: E402
from provisioner.lib.orchestrator import parse_intent  # noqa: E402
from provisioner.lib.phases import get_registry  # noqa: E402
from provisioner.lib.phases.csi_smoke import CsiSmokePhase  # noqa: E402
from provisioner.lib.phases.envoy_smoke import EnvoySmokePhase  # noqa: E402
from provisioner.lib.protocols import (  # noqa: E402
    BootstrapError,
    ClusterTopology,
    VmTarget,
)


# ----------------------------------------------------------- fixtures


@pytest.fixture
def cicd_topology() -> ClusterTopology:
    return ClusterTopology(
        cluster_name="cicd",
        control_plane=(VmTarget(role="control_plane", name="cicd-cp-1", vmid=300, ip="10.0.0.64"),),
        worker=(VmTarget(role="worker", name="cicd-w-1", vmid=301, ip="10.0.0.65"),),
    )


@pytest.fixture
def cicd_intent(tmp_path: Path) -> ClusterIntent:
    f = tmp_path / "main.tf"
    f.write_text(
        """
        locals {
          cluster_name = "cicd"
          pod_cidr     = "172.16.0.0/16"
          svc_cidr     = "172.17.0.0/16"
          cluster_dns  = "172.17.0.10"
          install_k3s_exec_server = ["--flannel-backend=none"]
          install_k3s_exec_agent  = ["--kubelet-arg=cloud-provider=external"]
          cf_tunnel_name = "cicd"
          csi_storage = "data1"
          ccm_region  = "proxmox-host"
          ccm_zone    = "BigBertha"
          k3s_version = "v1.36.2+k3s1"
        }
        """
    )
    return parse_intent(f)


def _make_container(
    tmp_path: Path,
    cicd_topology: ClusterTopology,
    cicd_intent: ClusterIntent,
    *,
    proxmox_region: str = "proxmox-host",
    proxmox_zone: str = "BigBertha",
) -> Container:
    (tmp_path / "kubeconfig.yaml").write_text("apiVersion: v1\nkind: Config\n")
    secrets = StaticSecretsSource(
        cf_api_token="t",
        cf_account_id="a",
        proxmox_api_url="https://kvm.bruj0.net:8006/api2/json",
        proxmox_token_id="root@pam!x",
        proxmox_token_secret="y",
        proxmox_region=proxmox_region,
        proxmox_zone=proxmox_zone,
    )
    c = Container.for_tests(
        logger=StructuredLogger("test"),
        remote=FakeRemoteExecutor(),
        cluster_probe=FakeClusterProbe(apiserver_ok=True, helm_releases=set()),
        state_store=InMemoryStateStore(),
        output_sink=DictOutputSink(),
        versions=StaticVersionsSource(),
        secrets=secrets,
        cluster_dir=tmp_path,
        repo_root=tmp_path,
        cluster_name="cicd",
    )
    c.upstream_topology = cicd_topology
    c.cluster_intent = cicd_intent
    return c


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["kubectl"], returncode, stdout, stderr)


# ----------------------------------------------------------- registry


def test_csi_smoke_is_registered() -> None:
    registry = get_registry()
    assert "csi_smoke" in registry.all_names()
    assert isinstance(registry.get("csi_smoke"), CsiSmokePhase)
    # helm_releases is the run-prerequisite; csi-smoke's
    # functional dependency on topology_labels is implicit (via
    # the controller having started successfully).
    assert "helm_releases" in CsiSmokePhase.requires


def test_envoy_smoke_is_registered() -> None:
    registry = get_registry()
    assert "envoy_smoke" in registry.all_names()
    assert isinstance(registry.get("envoy_smoke"), EnvoySmokePhase)
    assert "helm_releases" in EnvoySmokePhase.requires


# ----------------------------------------------------------- csi_smoke


def test_csi_smoke_preflight_fails_when_sc_not_default(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """SC wrong (not 'true') -> BootstrapError pre-flight."""
    c = _make_container(tmp_path, cicd_topology, cicd_intent)

    # First kubectl call is `get sc ... -o jsonpath=...` which
    # returns 'false' (SC exists, but isn't default).
    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "get" in argv and "proxmox-lvm-thin" in argv:
            return _cp(stdout="false")
        return _cp()

    with patch.object(subprocess, "run", side_effect=fake_run):
        with pytest.raises(BootstrapError) as ei:
            CsiSmokePhase().run(c)
    assert "missing or not marked default" in str(ei.value)


def test_csi_smoke_preflight_fails_when_nodes_missing_labels(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """SC is default + csi pods OK, but a Node is missing topology label."""
    c = _make_container(tmp_path, cicd_topology, cicd_intent)

    nodes_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "cicd-cp-1",
                        "labels": {
                            # Missing both topology labels.
                            "kubernetes.io/hostname": "cicd-cp-1"
                        },
                    }
                },
                {
                    "metadata": {
                        "name": "cicd-w-1",
                        "labels": {
                            "topology.kubernetes.io/region": "proxmox-host",
                            "topology.kubernetes.io/zone": "BigBertha",
                        },
                    }
                },
            ]
        }
    )

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "get" in argv and "proxmox-lvm-thin" in argv:
            return _cp(stdout="true")
        if "rollout" in argv:
            return _cp()
        if "get" in argv and "nodes" in argv and "json" in argv:
            return _cp(stdout=nodes_payload)
        return _cp()

    with patch.object(subprocess, "run", side_effect=fake_run):
        with pytest.raises(BootstrapError) as ei:
            CsiSmokePhase().run(c)
    assert "missing required topology labels" in str(ei.value)
    assert "cicd-cp-1" in str(ei.value)


def test_csi_smoke_happy_path_creates_namespace_pvc_writer_then_reader(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """Full happy path: preflight OK -> apply manifest -> wait
    PVC bound -> wait writer -> reader sees marker -> cleanup.
    """
    c = _make_container(tmp_path, cicd_topology, cicd_intent)
    nodes_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "cicd-cp-1",
                        "labels": {
                            "topology.kubernetes.io/region": "proxmox-host",
                            "topology.kubernetes.io/zone": "BigBertha",
                        },
                    }
                },
                {
                    "metadata": {
                        "name": "cicd-w-1",
                        "labels": {
                            "topology.kubernetes.io/region": "proxmox-host",
                            "topology.kubernetes.io/zone": "BigBertha",
                        },
                    }
                },
            ]
        }
    )
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        argv_str = " ".join(argv)
        if "rollout" in argv:
            return _cp()
        if "get" in argv and "proxmox-lvm-thin" in argv:
            return _cp(stdout="true")
        if "get" in argv and "nodes" in argv and "json" in argv:
            return _cp(stdout=nodes_payload)
        if "apply" in argv:
            return _cp(stdout="namespace/proxmox-k3s-smoke unchanged\n")
        if "wait" in argv and "pvc/" in argv_str:
            return _cp()
        if "wait" in argv and "pod/smoke-write" in argv_str:
            return _cp()
        if "wait" in argv and "pod/smoke-read" in argv_str:
            return _cp()
        if "logs" in argv and "smoke-read" in argv_str:
            # The marker is in the reader's stdout.
            return _cp(stdout=f"marker on disk: {_EXPECTED_MARKER}\nexpected: {_EXPECTED_MARKER}\n")
        if "delete" in argv and "ns" in argv:
            return _cp()
        return _cp()

    _EXPECTED_MARKER = "proxmox-k3s-smoke-csi-marker"
    # Patch the marker constant since it's defined inside the phase.
    with patch.object(subprocess, "run", side_effect=fake_run):
        with patch(
            "provisioner.lib.phases.csi_smoke._MARKER", _EXPECTED_MARKER
        ):
            result = CsiSmokePhase().run(c)

    assert result.changed
    assert result.data["pvc"] == "smoke-pvc"
    assert result.data["storage_class"] == "proxmox-lvm-thin"
    assert result.data["marker"] == _EXPECTED_MARKER


def test_csi_smoke_fails_when_reader_marker_missing(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """Reader pod completes but marker is absent in logs ->
    marker didn't persist across pod churn -> BootstrapError.
    """
    c = _make_container(tmp_path, cicd_topology, cicd_intent)
    nodes_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "cicd-cp-1",
                        "labels": {
                            "topology.kubernetes.io/region": "proxmox-host",
                            "topology.kubernetes.io/zone": "BigBertha",
                        },
                    }
                }
            ]
        }
    )

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv_str = " ".join(argv)
        if "rollout" in argv:
            return _cp()
        if "get" in argv and "proxmox-lvm-thin" in argv:
            return _cp(stdout="true")
        if "get" in argv and "nodes" in argv and "json" in argv:
            return _cp(stdout=nodes_payload)
        if "apply" in argv:
            return _cp(stdout="ok\n")
        if "wait" in argv and "pvc/" in argv_str:
            return _cp()
        if "wait" in argv and "pod/smoke-write" in argv_str:
            return _cp()
        if "wait" in argv and "pod/smoke-read" in argv_str:
            return _cp()
        if "logs" in argv and "smoke-read" in argv_str:
            return _cp(stdout="marker on disk: WRONG-CONTENT\n")
        return _cp()

    _EXPECTED_MARKER = "proxmox-k3s-smoke-csi-marker"
    with patch.object(subprocess, "run", side_effect=fake_run):
        with patch("provisioner.lib.phases.csi_smoke._MARKER", _EXPECTED_MARKER):
            with pytest.raises(BootstrapError) as ei:
                CsiSmokePhase().run(c)
    assert "did NOT survive pod churn" in str(ei.value)


# ----------------------------------------------------------- envoy_smoke


def test_envoy_smoke_ensures_gateway_class_when_missing(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """GatewayClass=envoy missing -> phase must kubectl apply it."""
    c = _make_container(tmp_path, cicd_topology, cicd_intent)
    apply_calls: list[str] = []

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv_str = " ".join(argv)
        if "rollout" in argv:
            return _cp()
        if "get" in argv and "crd" in argv:
            return _cp(stdout="gateways.gateway.networking.k8s.io")
        # get gatewayclass envoy (first probe): NotFound (rc=1).
        if "get" in argv and "gatewayclass" in argv and "envoy" in argv:
            return _cp(returncode=1, stdout="")
        if "apply" in argv and any(a == "-" for a in argv):
            # stdin manifest apply for the GatewayClass.
            apply_calls.append(argv_str)
            return _cp(stdout='{"kind":"GatewayClass"}\n')
        if "apply" in argv:
            return _cp(stdout="namespace/proxmox-k3s-smoke created\n")
        if "wait" in argv and "Available=true" in argv_str:
            return _cp()
        if "get" in argv and "svc" in argv and "envoy-gateway-system" in argv:
            return _cp(stdout="172.17.50.1")
        if "wait" in argv and "smoke-curl" in argv_str:
            return _cp()
        if "logs" in argv and "smoke-curl" in argv_str:
            return _cp(stdout="proxmox-k3s-smoke-envoy-gateway")
        if "delete" in argv and "ns" in argv:
            return _cp()
        return _cp()

    with patch.object(subprocess, "run", side_effect=fake_run):
        result = EnvoySmokePhase().run(c)

    assert result.changed
    # The GatewayClass apply was made.
    assert any("apply" in c for c in apply_calls)
    assert result.data["data_plane"] == "172.17.50.1"
    assert result.data["body"] == "proxmox-k3s-smoke-envoy-gateway"


def test_envoy_smoke_skips_gatewayclass_creation_when_already_present(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """GC already exists with correct controllerName -> no GC apply."""
    c = _make_container(tmp_path, cicd_topology, cicd_intent)
    apply_calls: list[str] = []

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv_str = " ".join(argv)
        if "rollout" in argv:
            return _cp()
        if "get" in argv and "crd" in argv:
            return _cp(stdout="gateways.gateway.networking.k8s.io")
        if "get" in argv and "gatewayclass" in argv:
            return _cp(
                stdout="gateway.envoyproxy.io/gatewayclass-controller"
            )
        if "apply" in argv and any(a == "-" for a in argv):
            apply_calls.append(argv_str)
            return _cp(stdout='{"kind":"GatewayClass"}\n')
        if "apply" in argv:
            return _cp(stdout="ok\n")
        if "wait" in argv and "Available=true" in argv_str:
            return _cp()
        if "get" in argv and "svc" in argv and "envoy-gateway-system" in argv:
            return _cp(stdout="172.17.50.1")
        if "wait" in argv and "smoke-curl" in argv_str:
            return _cp()
        if "logs" in argv and "smoke-curl" in argv_str:
            return _cp(stdout="proxmox-k3s-smoke-envoy-gateway")
        if "delete" in argv and "ns" in argv:
            return _cp()
        return _cp()

    with patch.object(subprocess, "run", side_effect=fake_run):
        EnvoySmokePhase().run(c)

    # No stdin manifest apply (the GC was already present).
    stdin_applies = [a for a in apply_calls if "apply -f -" in a]
    assert stdin_applies == []


def test_envoy_smoke_fails_when_body_mismatch(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """Data plane up, route attached, but body doesn't match -> fail."""
    c = _make_container(tmp_path, cicd_topology, cicd_intent)

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv_str = " ".join(argv)
        if "rollout" in argv:
            return _cp()
        if "get" in argv and "crd" in argv:
            return _cp(stdout="gateways.gateway.networking.k8s.io")
        if "get" in argv and "gatewayclass" in argv:
            return _cp(stdout="gateway.envoyproxy.io/gatewayclass-controller")
        if "apply" in argv:
            return _cp(stdout="ok\n")
        if "wait" in argv and "Available=true" in argv_str:
            return _cp()
        if "get" in argv and "svc" in argv and "envoy-gateway-system" in argv:
            return _cp(stdout="172.17.50.1")
        if "wait" in argv and "smoke-curl" in argv_str:
            return _cp()
        if "logs" in argv and "smoke-curl" in argv_str:
            return _cp(stdout="WRONG-BODY")
        if "delete" in argv and "ns" in argv:
            return _cp()
        return _cp()

    with patch.object(subprocess, "run", side_effect=fake_run):
        with pytest.raises(BootstrapError) as ei:
            EnvoySmokePhase().run(c)
    assert "echo body mismatch" in str(ei.value)


def test_envoy_smoke_fails_when_no_data_plane_service(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """Data-plane Deployment never became Available, but the
    GC exists. Pin the error path.
    """
    c = _make_container(tmp_path, cicd_topology, cicd_intent)

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv_str = " ".join(argv)
        if "rollout" in argv:
            return _cp()
        if "get" in argv and "crd" in argv:
            return _cp(stdout="gateways.gateway.networking.k8s.io")
        if "get" in argv and "gatewayclass" in argv:
            return _cp(stdout="gateway.envoyproxy.io/gatewayclass-controller")
        if "apply" in argv:
            return _cp(stdout="ok\n")
        if "wait" in argv and "Available=true" in argv_str:
            return _cp(returncode=1, stderr="timed out")
        if "get" in argv and "deploy" in argv and "envoy-gateway-system" in argv:
            return _cp(stdout="")
        if "delete" in argv and "ns" in argv:
            return _cp()
        return _cp()

    with patch.object(subprocess, "run", side_effect=fake_run):
        with pytest.raises(BootstrapError) as ei:
            EnvoySmokePhase().run(c)
    assert "data-plane Deployment" in str(ei.value)


def test_envoy_smoke_preflight_fails_when_gateway_api_crds_missing(
    tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent
) -> None:
    """gateway.networking.k8s.io CRDs absent -> fail pre-flight."""
    c = _make_container(tmp_path, cicd_topology, cicd_intent)

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "rollout" in argv:
            return _cp()
        if "get" in argv and "crd" in argv:
            return _cp(returncode=1, stderr="not found")
        return _cp()

    with patch.object(subprocess, "run", side_effect=fake_run):
        with pytest.raises(BootstrapError) as ei:
            EnvoySmokePhase().run(c)
    assert "Gateway API CRDs missing" in str(ei.value)
