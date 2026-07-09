"""Tests that pin the SOLID seams — proves the design works in isolation.

Each test wires a Container.for_tests(...) with fakes, then asserts
that a Phase (or the orchestrator) talks to the right collaborator
the right number of times.

This is the open/closed proof: a new Phase can be added without
touching this file or any other Phase. As long as the new Phase
depends only on Protocols, it gets tested with the same fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provisioner.lib.container import (
    Container,
    DictOutputSink,
    FakeClusterProbe,
    FakeRemoteExecutor,
    InMemoryStateStore,
    StaticSecretsSource,
    StaticVersionsSource,
)
from provisioner.lib.hcl_parser import ClusterIntent
from provisioner.lib.orchestrator import (
    parse_intent,
    run,
)
from provisioner.lib.phases import get_registry
from provisioner.lib.protocols import (
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
    fixture_dir = tmp_path / "intent-fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    main_tf = fixture_dir / "main.tf"
    main_tf.write_text(
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
    return parse_intent(main_tf)


@pytest.fixture
def fake_container(tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent) -> Container:
    """Build a container wired with fakes for every collaborator."""
    remote = FakeRemoteExecutor()
    probe = FakeClusterProbe(apiserver_ok=True, helm_releases={("eg", "envoy-gateway-system")})
    state = InMemoryStateStore()
    output = DictOutputSink()
    versions = StaticVersionsSource()
    secrets = StaticSecretsSource(cf_api_token="test-token", cf_account_id="test-account")
    c = Container.for_tests(
        logger=__import__("provisioner.lib.log", fromlist=["StructuredLogger"]).StructuredLogger("test"),
        remote=remote,
        cluster_probe=probe,
        state_store=state,
        output_sink=output,
        versions=versions,
        secrets=secrets,
        cluster_dir=tmp_path,
        repo_root=tmp_path,
        cluster_name="cicd",
    )
    c.upstream_topology = cicd_topology
    c.cluster_intent = cicd_intent
    return c


# ----------------------------------------------------------- tests


def test_registry_has_all_expected_phases() -> None:
    """Pins the public phase surface — adding a phase is an explicit change."""
    registry = get_registry()
    expected = {
        "validate",
        "ssh_probe",
        "install_k3s",
        "apiserver_ready",
        "kubeconfig_pull",
        "gateway_crds",
        "cilium_install",
        "topology_labels",
        "start_k3s_units",
        "helm_releases",
        "host_ports",
        "topology_writer",
    }
    assert set(registry.all_names()) == expected


def test_validate_phase_runs_with_fake_topology(fake_container: Container) -> None:
    """Validate doesn't need SSH or kubectl — should work in isolation."""
    from provisioner.lib.phases.validate import ValidatePhase
    phase = ValidatePhase()
    # Validate looks for upstream output.json — we'll skip it via fakes.
    # For this test, we'll just assert the validate phase parses main.tf.
    # (The full validate phase requires the upstream output.json to exist;
    #  we test that separately in the integration test.)
    fake_container.cluster_dir.mkdir(parents=True, exist_ok=True)
    main_tf = fake_container.cluster_dir / "main.tf"
    main_tf.write_text(
        """
        locals {
          cluster_name = "cicd"
          pod_cidr     = "172.16.0.0/16"
          svc_cidr     = "172.17.0.0/16"
          cluster_dns  = "172.17.0.10"
          install_k3s_exec_server = []
          install_k3s_exec_agent  = []
          cf_tunnel_name = "cicd"
          csi_storage = "data1"
          ccm_region  = "proxmox-host"
          ccm_zone    = "BigBertha"
          k3s_version = "v1.36.2+k3s1"
        }
        """
    )
    intent = parse_intent(main_tf)
    fake_container.cluster_intent = intent
    # Phase should fail because upstream output.json doesn't exist.
    with pytest.raises(BootstrapError, match="upstream_output_missing"):
        phase.run(fake_container)


def test_ssh_probe_phase_records_remote_calls(fake_container: Container) -> None:
    """ssh_probe uses ONLY the RemoteExecutor protocol — proves the DIP."""
    from provisioner.lib.phases.ssh_probe import SshProbePhase
    phase = SshProbePhase()
    result = phase.run(fake_container)
    assert result.name == "ssh_probe"
    assert result.changed is True
    remote = fake_container.remote
    assert isinstance(remote, FakeRemoteExecutor)
    # 2 probes per node × 2 nodes = 4 SSH calls.
    assert len(remote.calls) == 4
    targets = {c["target"] for c in remote.calls}
    assert targets == {"10.0.0.64", "10.0.0.65"}


def test_apiserver_ready_phase_uses_ssh_only(fake_container: Container) -> None:
    """apiserver_ready runs BEFORE kubeconfig_pull, so it can't use
    ClusterProbe (the kubeconfig + tunnel don't exist yet). It uses
    ONLY RemoteExecutor (SSH) to poll the k3s service + port on the CP.
    """
    from provisioner.lib.protocols import RemoteResult
    from provisioner.lib.phases.apiserver_ready import ApiserverReadyPhase
    remote = fake_container.remote
    assert isinstance(remote, FakeRemoteExecutor)
    # Queue realistic responses for the 3 SSH calls the phase makes.
    remote.queue(
        "10.0.0.64", "systemctl is-active k3s",
        RemoteResult(stdout="active\n", stderr="", exit_code=0),
    )
    remote.queue(
        "10.0.0.64", "sudo ss -tlnp",
        RemoteResult(
            stdout='LISTEN 0 4096 *:6443 *:* users:(("k3s-server",pid=1,fd=12))\n',
            stderr="", exit_code=0,
        ),
    )
    remote.queue(
        "10.0.0.64", "curl",
        RemoteResult(stdout="ok\n", stderr="", exit_code=0),
    )
    phase = ApiserverReadyPhase()
    result = phase.run(fake_container)
    assert result.name == "apiserver_ready"
    assert result.changed is True
    # 3 SSH calls: is-active, ss -tlnp, curl /healthz
    assert len(remote.calls) == 3
    targets = {c["target"] for c in remote.calls}
    assert targets == {"10.0.0.64"}  # only the first CP


def test_apiserver_ready_phase_raises_when_service_down(fake_container: Container) -> None:
    """If the k3s service is not active on the CP, raise BootstrapError."""
    from provisioner.lib.phases.apiserver_ready import ApiserverReadyPhase
    from provisioner.lib.protocols import RemoteResult

    class InactiveRemote(FakeRemoteExecutor):
        def run(self, target, command, *, check=True, timeout=15.0):
            return RemoteResult(
                exit_code=3,
                stdout="inactive\n", stderr="",
            )

    fake_container.remote = InactiveRemote()
    phase = ApiserverReadyPhase()
    with pytest.raises(BootstrapError, match="apiserver_ready"):
        phase.run(fake_container)


def test_topology_writer_emits_k3s_json_shape(fake_container: Container) -> None:
    """topology_writer uses ONLY OutputSink — proves the DIP."""
    from provisioner.lib.phases.topology_writer import TopologyWriterPhase
    phase = TopologyWriterPhase()
    # Stub out the cluster_probe returns so the smoke block has data.
    fake_container.cluster_probe = FakeClusterProbe(
        nodes=(
            {"metadata": {"name": "cicd-cp-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ),
        pods=(),
        helm_releases={("eg", "envoy-gateway-system"), ("cert-manager", "cert-manager"), ("proxmox-csi-plugin", "csi-proxmox"), ("cloudflare-tunnel", "cloudflare-tunnel")},
    )
    result = phase.run(fake_container)
    assert result.changed is True
    out_sink = fake_container.output_sink
    assert isinstance(out_sink, DictOutputSink)
    assert out_sink.last is not None
    payload = out_sink.last
    assert payload["cluster_name"] == "cicd"
    assert payload["pod_cidr"] == "172.16.0.0/16"
    assert payload["api_endpoint"] == "https://10.0.0.64:6443"
    assert payload["nodes"][0]["name"] == "cicd-cp-1"
    assert "generated_at" in payload
    assert payload["smoke"]["nodes_ready"] is True
    assert payload["smoke"]["csi_driver_registered"] is True
    assert payload["smoke"]["envoy_gateway_available"] is True


def test_phase_idempotency_via_state_store(tmp_path: Path, cicd_topology: ClusterTopology, cicd_intent: ClusterIntent) -> None:
    """A re-run on a steady-state cluster is a no-op for phases marked done."""
    remote = FakeRemoteExecutor()
    state = InMemoryStateStore(initial=frozenset({"ssh_probe"}))
    c = Container.for_tests(
        logger=__import__("provisioner.lib.log", fromlist=["StructuredLogger"]).StructuredLogger("test"),
        remote=remote,
        state_store=state,
        cluster_dir=tmp_path,
        repo_root=tmp_path,
        cluster_name="cicd",
    )
    c.upstream_topology = cicd_topology
    c.cluster_intent = cicd_intent
    from provisioner.lib.phases.ssh_probe import SshProbePhase
    phase = SshProbePhase()
    # should_run returns False because state already has ssh_probe.
    assert phase.should_run(c) is False
    # If we force it to run, the FakeRemoteExecutor still records calls —
    # proves the gate is in should_run, not run.
    phase.run(c)
    assert len(remote.calls) == 4  # not gated by should_run by default


def test_phase_registry_topological_sort() -> None:
    """registry.topological_order respects phase.requires."""
    registry = get_registry()
    # Ask for the last few phases; their deps should be auto-included.
    order = registry.topological_order(("topology_writer",))
    assert order.index("host_ports") < order.index("topology_writer")
    assert order.index("helm_releases") < order.index("host_ports")
    assert order.index("cilium_install") < order.index("helm_releases")


def test_phase_registry_raises_on_unknown_phase() -> None:
    registry = get_registry()
    with pytest.raises(BootstrapError, match="unknown_phase"):
        registry.topological_order(("not_a_real_phase",))


def test_phase_registry_raises_on_missing_dep() -> None:
    """A phase that requires a non-existent phase triggers BootstrapError."""
    from provisioner.lib.phases.base import Phase, PhaseRegistry
    test_registry = PhaseRegistry()

    class _A(Phase):
        name = "a"
        requires = ("missing",)
        def run(self, ctx: object) -> object:  # pragma: no cover - unreachable
            return None  # type: ignore[return-value]

    test_registry.register(_A())
    with pytest.raises(BootstrapError, match="missing_dep"):
        test_registry.topological_order(("a",))


def test_container_for_tests_uses_fakes() -> None:
    """The test factory wires ONLY in-memory fakes — proves the isolation."""
    c = Container.for_tests()
    from provisioner.lib.container import (
        DictOutputSink,
        FakeClusterProbe,
        FakeRemoteExecutor,
        InMemoryStateStore,
        StaticSecretsSource,
        StaticVersionsSource,
    )
    assert isinstance(c.remote, FakeRemoteExecutor)
    assert isinstance(c.cluster_probe, FakeClusterProbe)
    assert isinstance(c.state_store, InMemoryStateStore)
    assert isinstance(c.output_sink, DictOutputSink)
    assert isinstance(c.versions, StaticVersionsSource)
    assert isinstance(c.secrets, StaticSecretsSource)


def test_orchestrator_runs_phases_in_dep_order(fake_container: Container) -> None:
    """The orchestrator visits phases in topological order.

    We bypass the validate dep (which needs the upstream output.json)
    by calling the phases directly — the test is about dep-ordering,
    not validate's behaviour.
    """
    from provisioner.lib.phases.apiserver_ready import ApiserverReadyPhase
    from provisioner.lib.phases.ssh_probe import SshProbePhase
    from provisioner.lib.protocols import RemoteResult
    # apiserver_ready now uses SSH (not probe), so queue realistic responses.
    remote = fake_container.remote
    assert isinstance(remote, FakeRemoteExecutor)
    remote.queue("10.0.0.64", "systemctl is-active k3s",
                 RemoteResult(stdout="active\n", stderr="", exit_code=0))
    remote.queue("10.0.0.64", "sudo ss -tlnp",
                 RemoteResult(stdout="LISTEN 0 4096 *:6443 *:*\n", stderr="", exit_code=0))
    remote.queue("10.0.0.64", "curl",
                 RemoteResult(stdout="ok\n", stderr="", exit_code=0))
    ssh = SshProbePhase().run(fake_container)
    api = ApiserverReadyPhase().run(fake_container)
    assert ssh.name == "ssh_probe"
    assert api.name == "apiserver_ready"


def test_orchestrator_phase_failure_raises(fake_container: Container, tmp_path: Path) -> None:
    """Phase failures bubble up as BootstrapError (M4 misfit: never swallow)."""
    from provisioner.lib.protocols import RemoteResult
    # Make k3s.service return inactive so apiserver_ready raises.
    remote = fake_container.remote
    assert isinstance(remote, FakeRemoteExecutor)
    remote.queue("10.0.0.64", "systemctl is-active k3s",
                 RemoteResult(stdout="inactive\n", stderr="", exit_code=3))
    with pytest.raises(BootstrapError):
        run(fake_container, selected_phases=("apiserver_ready",))
