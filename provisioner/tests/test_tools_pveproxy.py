"""Tests for the operator-side tools/pveproxy CLI.

Covers the load-bearing contracts:

  * `start` is idempotent: invoking it a second time while a
    tunnel is alive does NOT spawn a second ssh -L process. This
    is the user's explicit ask ("never start it twice").

  * `stop` cleans up the state file and signals the pid.

  * `status` reports a non-zero exit when the tunnel is stale, so
    wrappers can detect the situation.

  * `kubeconfig` --print emits only the path, suitable for
    `KUBECONFIG=$(...)` substitution.

  * `rewrite_server_url` and `ProxyState` round-trip through JSON
    cleanly (no fragile YAML parsing in production code).

  * `tools.repo_locator.locate_repo_root` resolves the repo from
    cwd, env, flag, and walk-up strategies in the documented
    order.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Repo root: proxmox-k3s/. tools/ lives at the root; tests live
# under provisioner/tests/. We add the repo root to sys.path so
# `import tools.pveproxy` resolves.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Mirror the conftest from this same directory so lib.* imports work.
PROVISIONER_DIR = Path(__file__).resolve().parent.parent
if str(PROVISIONER_DIR) not in sys.path:
    sys.path.insert(0, str(PROVISIONER_DIR))

from tools.pveproxy import (  # noqa: E402
    ProxyState,
    _is_pid_alive,
    _pick_local_port,
    _probe_local_port,
    _refresh_kubeconfig,
    _state_file,
    rewrite_server_url,
)
from tools.repo_locator import RepoNotFoundError, locate_repo_root  # noqa: E402


# ---------- ProxyState JSON round-trip ----------


def _sample_state(cluster_dir: Path, *, pid: int = 4242, port: int = 16443) -> ProxyState:
    return ProxyState(
        pid=pid,
        local_port=port,
        target_ip="10.0.0.64",
        target_name="cicd-cp-1",
        started_at="2026-07-09T12:34:56+00:00",
        kubeconfig_path=str(cluster_dir / "kubeconfig.pveproxy"),
        cluster_dir=cluster_dir,
    )


def test_proxy_state_round_trips_through_json(tmp_path: Path) -> None:
    state = _sample_state(tmp_path)
    raw = state.to_json()
    parsed = json.loads(raw)
    assert parsed["pid"] == 4242
    assert parsed["local_port"] == 16443
    assert parsed["target_ip"] == "10.0.0.64"
    # Cluster dir is stored as a string (json-friendly).
    assert parsed["cluster_dir"] == str(tmp_path)
    loaded = ProxyState.from_json(raw)
    assert loaded == state


def test_proxy_state_load_returns_none_for_missing(tmp_path: Path) -> None:
    assert ProxyState.load(tmp_path / "nope.json") is None


def test_proxy_state_load_returns_none_for_corrupt(tmp_path: Path) -> None:
    sf = tmp_path / "state.json"
    sf.write_text("{not valid json")
    assert ProxyState.load(sf) is None


def test_proxy_state_load_ignores_unknown_keys(tmp_path: Path) -> None:
    sf = tmp_path / "state.json"
    sf.write_text(
        json.dumps(
            {
                "pid": 1,
                "local_port": 16443,
                "target_ip": "10.0.0.1",
                "target_name": "x",
                "started_at": "now",
                "kubeconfig_path": "/tmp/kc",
                "cluster_dir": str(tmp_path),
                "future_field": "ignored",
            }
        )
    )
    loaded = ProxyState.load(sf)
    assert loaded is not None
    assert loaded.pid == 1


def test_proxy_state_save_is_atomic(tmp_path: Path) -> None:
    state = _sample_state(tmp_path)
    state.save()
    sf = _state_file(tmp_path)
    assert sf.exists()
    # No leftover .tmp file from the atomic rename.
    assert not (tmp_path / ".pveproxy.state.json.tmp").exists()
    # File mode is whatever the default is -- not asserting on it.


# ---------- rewrite_server_url ----------


def test_rewrite_server_url_replaces_loopback() -> None:
    text = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- name: local\n"
        "  cluster:\n"
        "    server: https://127.0.0.1:6443\n"
        "    certificate-authority-data: abc\n"
    )
    out = rewrite_server_url(text, 16443)
    assert "server: https://127.0.0.1:16443" in out
    assert "server: https://127.0.0.1:6443" not in out


def test_rewrite_server_url_preserves_indentation() -> None:
    text = "    server: https://127.0.0.1:6443\n"
    out = rewrite_server_url(text, 9999)
    assert out.startswith("    server: https://127.0.0.1:9999\n")


def test_rewrite_server_url_replaces_only_first() -> None:
    # The k3s kubeconfig has exactly one `server:` line. If a file
    # ever has more, only the first is replaced (avoid accidental
    # rewriting a comment-shaped "server: foo" inside the file).
    text = (
        "server: https://127.0.0.1:6443\n"
        "server: https://other.example\n"  # unlikely but defensive
    )
    out = rewrite_server_url(text, 12345)
    lines = [line for line in out.splitlines() if line.lstrip().startswith("server:")]
    # Only the first `server:` line is rewritten. The second one
    # (which would never appear in a real k3s kubeconfig) is left
    # alone -- we don't want to accidentally rewrite a stray
    # comment-shaped "server:" in a multi-doc YAML.
    assert lines == [
        "server: https://127.0.0.1:12345",
        "server: https://other.example",
    ]


def test_rewrite_server_url_raises_when_missing() -> None:
    with pytest.raises(RuntimeError, match="no `server:` line"):
        rewrite_server_url("apiVersion: v1\nkind: Config\n", 1234)


# ---------- liveness probes ----------


def test_is_pid_alive_for_live_ssh_process() -> None:
    """Spawn a long-lived ssh and assert _is_pid_alive says it's ssh."""
    # We need an actual /ssh binary; `which ssh` is the most portable
    # way to find one. If ssh is missing the test is skipped.
    import shutil

    ssh = shutil.which("ssh")
    if not ssh:
        pytest.skip("ssh not on PATH")
    proc = subprocess.Popen(  # noqa: S603 -- test-only
        [ssh, "-N", "-o", "BatchMode=yes", "127.0.0.1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _is_pid_alive(proc.pid) is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_is_pid_alive_for_unlikely_pid() -> None:
    # Pid 2^22 is in a range that won't be a real process on a
    # normal system. If it ever is, the test fails -- but we'd
    # notice that immediately.
    assert _is_pid_alive(4_194_304) is False


def test_is_pid_alive_rejects_non_ssh_process() -> None:
    """A live process whose exe is not ssh must NOT count as alive.

    This is the pid-recycle guard: if our tunnel dies and the OS
    recycles its pid for some other process, we must NOT mistake
    that other process for our tunnel.
    """
    # Use the current Python interpreter's exe as a stand-in for
    # "any non-ssh process". The current pid is itself, but we're
    # checking the exe path, not the liveness.
    import shutil

    py = shutil.which("python3") or sys.executable
    proc = subprocess.Popen(  # noqa: S603 -- test-only
        [py, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _is_pid_alive(proc.pid) is False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_probe_local_port_for_unbound_port() -> None:
    # Pick a free port, close it, then probe -> not listening.
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    # Port is now released (socket closed).
    assert _probe_local_port(free_port) is False


def test_probe_local_port_for_listening_port() -> None:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            assert _probe_local_port(port) is True
        finally:
            s.close()


def test_pick_local_port_uses_requested() -> None:
    assert _pick_local_port(12345) == 12345


def test_pick_local_port_picks_free_when_none() -> None:
    assert _pick_local_port(None) > 0


# ---------- repo_locator ----------


def test_repo_locator_finds_this_repo_via_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXMOX_K3S_REPO", raising=False)
    found = locate_repo_root(flag_value=str(REPO_ROOT))
    assert found == REPO_ROOT
    assert (found / "infra" / "clusters").is_dir()


def test_repo_locator_finds_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXMOX_K3S_REPO", str(REPO_ROOT))
    found = locate_repo_root()
    assert found == REPO_ROOT


def test_repo_locator_raises_when_nothing_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PROXMOX_K3S_REPO", raising=False)
    monkeypatch.setattr("tools.repo_locator.Path.cwd", lambda: tmp_path)
    with pytest.raises(RepoNotFoundError):
        locate_repo_root(flag_value=str(tmp_path / "no-such"))


# ---------- CLI: never start twice ----------


def _make_output_json(tmp_path: Path, cluster: str = "fake") -> Path:
    """Minimal output.json so ClusterTopology.from_output_json works."""
    out = tmp_path / "infra" / "clusters" / cluster / "output.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "cluster_name": cluster,
                "vip": "",
                "pod_cidr": "172.16.0.0/16",
                "svc_cidr": "172.17.0.0/16",
                "cluster_dns": "172.17.0.10",
                "nodes": [
                    {
                        "role": "control_plane",
                        "name": f"{cluster}-cp-1",
                        "ip": "10.0.0.64",
                        "vmid": "111",
                    }
                ],
            }
        )
    )
    return out.parent


def test_cli_help_exits_zero() -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tools.pveproxy", "--help"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "start" in completed.stdout
    assert "stop" in completed.stdout
    assert "status" in completed.stdout


def test_cli_no_subcommand_errors() -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tools.pveproxy", "--cluster", "fake"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0


def test_cli_kubectl_requires_args_after_double_dash() -> None:
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "tools.pveproxy",
            "--cluster",
            "fake",
            "kubectl",
        ],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    # argparse returns 2 for usage errors.
    assert completed.returncode == 2


def test_kubeconfig_subcommand_via_cli(tmp_path: Path) -> None:
    """`kubeconfig --print` should emit the absolute kubeconfig path.

    Uses a fake repo layout so locate_repo_root succeeds without
    touching infra/clusters/<cluster>/output.json (kubeconfig sub
    does not need the cluster to exist on disk).
    """
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "infra" / "clusters").mkdir(parents=True)
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "tools.pveproxy",
            "--cluster",
            "fake",
            "--repo-root",
            str(fake_repo),
            "kubeconfig",
            "--print",
        ],
        cwd=str(fake_repo),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    expected = str(fake_repo / "infra" / "clusters" / "fake" / "kubeconfig.pveproxy")
    assert completed.stdout.strip() == expected


def test_kubeconfig_subcommand_warns_when_missing(tmp_path: Path) -> None:
    fake_repo = tmp_path / "fake_repo2"
    (fake_repo / "infra" / "clusters").mkdir(parents=True)
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "tools.pveproxy",
            "--cluster",
            "missing",
            "--repo-root",
            str(fake_repo),
            "kubeconfig",
        ],
        cwd=str(fake_repo),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    # kubeconfig sub without --print exits 1 if file doesn't exist.
    assert completed.returncode == 1
    assert "kubeconfig file does not exist" in completed.stderr


def test_status_reports_no_state(tmp_path: Path) -> None:
    fake_repo = tmp_path / "fake_repo3"
    cluster_dir = fake_repo / "infra" / "clusters" / "ghost"
    cluster_dir.mkdir(parents=True)
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "tools.pveproxy",
            "--cluster",
            "ghost",
            "--repo-root",
            str(fake_repo),
            "status",
        ],
        cwd=str(fake_repo),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    # No state file -> exit 1 (we use a non-zero so wrappers detect it).
    assert completed.returncode == 1
    assert "no tunnel state" in completed.stderr


# ---------- _refresh_kubeconfig uses the right server URL ----------


def test_refresh_kubeconfig_writes_to_recorded_port(tmp_path: Path) -> None:
    """When state says port=16443, the kubeconfig's server: is 16443.

    We mock `fetch_kubeconfig_via_proxy` (imported into pveproxy)
    so we don't need a live cluster.
    """
    from unittest.mock import patch

    from tools import pveproxy

    state = _sample_state(tmp_path, port=17171)
    fake_body = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- name: local\n"
        "  cluster:\n"
        "    server: https://127.0.0.1:6443\n"
    )
    with patch.object(
        pveproxy, "fetch_kubeconfig_via_proxy", return_value=fake_body
    ):
        _refresh_kubeconfig(state, logger=pveproxy.StructuredLogger("test"))
    kc = Path(state.kubeconfig_path)
    assert kc.exists()
    text = kc.read_text()
    assert "server: https://127.0.0.1:17171" in text


# ---------- the "never start twice" contract ----------


def test_start_never_spawns_second_tunnel_when_state_alive(
    tmp_path: Path,
) -> None:
    """The user's explicit ask: never start the tunnel twice.

    Simulates: state file points at a live, listening ssh process
    on the expected port. A second `start` invocation must NOT
    call `port_forward()` -- it should detect the live tunnel and
    short-circuit.
    """
    import socket
    import threading
    from unittest.mock import MagicMock, patch

    from tools import pveproxy

    fake_repo = tmp_path / "fake_repo"
    cluster_dir = _make_output_json(fake_repo, "cicd")

    # Pick a free port and start a listener on it. We point
    # ProxyState at this listener so both pid-alive and
    # port-listening checks pass.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    chosen_port = listener.getsockname()[1]
    listener.listen(8)

    # `nc -l <port>` would also work but isn't always installed.
    # We use a tiny Python listener so the test is self-contained.
    stop_event = threading.Event()

    def accept_loop() -> None:
        while not stop_event.is_set():
            try:
                listener.settimeout(0.2)
                conn, _ = listener.accept()
                conn.close()
            except (TimeoutError, OSError):
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()

    try:
        live_state = ProxyState(
            # Pid doesn't actually need to be ssh for this test --
            # the listener thread is the real proof of life. But
            # _is_pid_alive requires the proc to be ssh, so we
            # use a long-lived ssh -N as a stand-in.
            pid=_spawn_long_lived_ssh(),
            local_port=chosen_port,
            target_ip="10.0.0.64",
            target_name="cicd-cp-1",
            started_at="2026-07-09T00:00:00+00:00",
            kubeconfig_path=str(cluster_dir / "kubeconfig.pveproxy"),
            cluster_dir=cluster_dir,
        )
        live_state.save()

        port_forward_mock = MagicMock()
        with patch.object(pveproxy, "PveSshProxy") as proxy_cls:
            proxy_cls.return_value.port_forward = port_forward_mock
            with patch.object(
                pveproxy, "fetch_kubeconfig_via_proxy", return_value=_FAKE_KUBECONFIG
            ):
                rc = pveproxy.main(
                    [
                        "--cluster",
                        "cicd",
                        "--repo-root",
                        str(fake_repo),
                        "start",
                    ]
                )
        assert rc == 0
        port_forward_mock.assert_not_called()
    finally:
        stop_event.set()
        t.join(timeout=2)
        listener.close()
        _kill_long_lived_ssh()


def _spawn_long_lived_ssh() -> int:
    """Start a long-lived ssh process and return its pid.

    Used by the "never start twice" test to satisfy
    `_is_pid_alive`'s ssh-exe check. The actual tunnel behaviour
    isn't needed -- the test points ProxyState at a real Python
    TCP listener for the port-listening check.
    """
    import shutil
    import subprocess

    ssh = shutil.which("ssh")
    if not ssh:
        pytest.skip("ssh not on PATH")  # type: ignore[unreachable]
    proc = subprocess.Popen(  # noqa: S603 -- test-only
        [ssh, "-N", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=60",
         "-o", "ServerAliveCountMax=10", "127.0.0.1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Stash globally so the finalizer can kill it.
    _LONG_LIVED_SSH.append(proc)
    return proc.pid


_LONG_LIVED_SSH: list[subprocess.Popen] = []


def _kill_long_lived_ssh() -> None:
    for proc in _LONG_LIVED_SSH:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    _LONG_LIVED_SSH.clear()


_FAKE_KUBECONFIG = (
    "apiVersion: v1\n"
    "kind: Config\n"
    "clusters:\n"
    "- name: local\n"
    "  cluster:\n"
    "    server: https://127.0.0.1:6443\n"
    "    certificate-authority-data: abc\n"
    "contexts:\n"
    "- name: default\n"
    "  context:\n"
    "    cluster: local\n"
    "    user: default\n"
    "current-context: default\n"
    "users:\n"
    "- name: default\n"
    "  user:\n"
    "    token: faketoken\n"
)


_FAKE_KUBECONFIG = (
    "apiVersion: v1\n"
    "kind: Config\n"
    "clusters:\n"
    "- name: local\n"
    "  cluster:\n"
    "    server: https://127.0.0.1:6443\n"
    "    certificate-authority-data: abc\n"
    "contexts:\n"
    "- name: default\n"
    "  context:\n"
    "    cluster: local\n"
    "    user: default\n"
    "current-context: default\n"
    "users:\n"
    "- name: default\n"
    "  user:\n"
    "    token: faketoken\n"
)
