"""Red-first tests for lib.pve_ssh (the SSH proxy helper).

This module centralises the PVE-jump-host SSH plumbing that both
`tools/ssh_proxy.py` and `tools/kubeconfig_puller.py` need, so the
two drivers don't drift in their ProxyCommand rendering.

The contract pinned here:

  * PveSshProxy connects to a target VM by tunnelling through PVE
    via `-o ProxyCommand="ssh -W %h:%p ..."` -- the form the live
    install_k3s run validated (--W alone is stdio-forward-only and
    refuses a remote command).
  * The PVE jump host defaults to "root@kvm.bruj0.net -p 6022" but
    is overridable via the constructor.
  * The target user defaults to "ubuntu" (cloud image's --ciuser;
    root SSH is refused).
  * For each call we expose:
        .ssh_argv(host, command=...)      -> list[str] for subprocess.run
        .proxy_argv(target_ip)            -> for direct SSHPASS / ProxyCommand use
        .run(host, command, ...)          -> subprocess.CompletedProcess
  * Token-bearing env keys ("token", "secret", "password",
    "ssh_key", "sshkey" -- case-insensitive) MUST be redacted by
    the StructuredLogger at the boundary (defence-in-depth; the
    caller is responsible for not putting them on argv directly).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.pve_ssh import PveSshProxy  # noqa: E402

# ---------- argv shape ----------


def test_default_proxy_target_renders_proxycommand() -> None:
    """ssh_argv must yield a single -o ProxyCommand= wrapping the
    canonical PVE jump + %h:%p tunnel."""
    proxy = PveSshProxy()
    argv = proxy.ssh_argv("10.0.0.65", command="hostname")
    joined = " ".join(argv)
    assert "ProxyCommand=" in joined, (
        f"ProxyCommand= missing in argv: {argv}"
    )
    # The wrapped ProxyCommand itself must end with "-W %h:%p" so the
    # tunnel reaches the cluster VM, not the PVE host itself.
    assert "%h:%p" in joined
    # The destination user@host must point at the target IP, not at
    # the proxy.
    assert "ubuntu@10.0.0.65" in argv
    # The remote command follows the conventional `--` separator.
    assert "--" in argv
    # The command should be the last element (sanity).
    assert argv[-1] == "hostname"


def test_custom_proxy_target_is_honoured() -> None:
    proxy = PveSshProxy(
        jump_host="root@other.host -p 2222",
        ssh_user="admin",
    )
    argv = proxy.ssh_argv("192.0.2.10", command="ls /")
    joined = " ".join(argv)
    # Custom jump + custom user land in the argv.
    assert "root@other.host" in joined
    assert "-p 2222" in joined
    assert "admin@192.0.2.10" in argv
    assert argv[-1] == "ls /"


def test_no_proxy_when_target_is_jump_host() -> None:
    """If target_ip == the jump host, ssh_argv should NOT wrap in
    ProxyCommand; the operator can ssh to PVE directly. The host
    portion uses the jump user@host exactly (root@kvm.bruj0.net)
    because Bitwarden's SSH key only authenticates root on PVE."""
    proxy = PveSshProxy(jump_host="root@kvm.bruj0.net -p 6022")
    argv = proxy.ssh_argv("kvm.bruj0.net", command="hostname")
    assert "ProxyCommand=" not in " ".join(argv)
    # When landing on PVE itself we deliberately pass through the
    # jump_host argv (which carries the user@ portion), so we expect
    # `root@kvm.bruj0.net` not `ubuntu@kvm.bruj0.net`.
    assert any("root@kvm.bruj0.net" in a for a in argv)
    assert argv[-1] == "hostname"


def test_command_without_remote_argv() -> None:
    """ssh_argv(..., command=None) returns just the connection prefix
    so the caller can append their own remote argv (e.g. forwarding
    arguments)."""
    proxy = PveSshProxy()
    argv = proxy.ssh_argv("10.0.0.65")
    # No remote command + no `--` separator.
    assert "--" not in argv
    # Last element is the SSH target.
    assert argv[-1].startswith("ubuntu@10.0.0.65")


# ---------- run + exit code ----------


class _FakeProc:
    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_run_returns_completed_process(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured["cmd"] = cmd
        return _FakeProc(0, "cicd-cp-1\n", "")

    monkeypatch.setattr("lib.pve_ssh.subprocess.run", fake_run)
    proxy = PveSshProxy()
    res = proxy.run("10.0.0.65", "hostname")
    assert res.returncode == 0
    assert res.stdout == "cicd-cp-1\n"
    # The captured argv must terminate with the remote command.
    assert captured["cmd"][-1] == "hostname"


def test_run_raises_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PveSshProxy.run helper propagates non-zero exits without
    swallowing them (M4 misfit -- silent failures)."""

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        return _FakeProc(1, "", "permission denied")

    monkeypatch.setattr("lib.pve_ssh.subprocess.run", fake_run)
    proxy = PveSshProxy()
    with pytest.raises(RuntimeError):
        proxy.run("10.0.0.65", "hostname", check=True)


def test_run_does_not_raise_when_check_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        return _FakeProc(2, "", "")

    monkeypatch.setattr("lib.pve_ssh.subprocess.run", fake_run)
    proxy = PveSshProxy()
    res = proxy.run("10.0.0.65", "false", check=False)
    assert res.returncode == 2


# ---------- structured-log scrub ----------


def test_proxy_log_does_not_emit_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    """Emitting a structured info() call with a key named like a token
    must NOT print the token even at debug verbosity."""
    proxy = PveSshProxy()
    proxy.logger.info("pve_ssh.test", K3S_TOKEN="ULTRA-SECRET")
    out = capsys.readouterr().out
    assert "ULTRA-SECRET" not in out
    assert "pve_ssh.test" in out  # the step name lands


def test_port_forward_argv_orders_L_before_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: ssh requires `-L` to come BEFORE the
    destination host token. If we splice it in after, ssh treats
    `-L 6443:127.0.0.1:6443` as the remote command to exec and the
    tunnel never comes up. This test pins the order so a future
    refactor can't reintroduce the bug."""
    captured: dict[str, list[str]] = {}

    class _StubPopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            captured["argv"] = argv
            self.args = argv
        def poll(self) -> int | None:
            return None
        def terminate(self) -> None:
            pass
        def wait(self, timeout: int | None = None) -> int:
            return 0
        def kill(self) -> None:
            pass

    class _StubForwardedPort:
        def __init__(self, **kwargs: Any) -> None:
            self.proc = _StubPopen(captured["argv"])
        def wait_ready(self) -> None:
            pass
        def terminate(self) -> None:
            pass
        @property
        def local_endpoint(self) -> str:
            return "stub"

    import lib.pve_ssh as pve_ssh_mod
    monkeypatch.setattr(pve_ssh_mod.subprocess, "Popen", _StubPopen)
    monkeypatch.setattr(pve_ssh_mod, "ForwardedPort", _StubForwardedPort)
    proxy = PveSshProxy(jump_host="root@kvm.bruj0.net -p 6022")
    proxy.port_forward("10.0.0.65", local_port=16443)
    argv = captured["argv"]
    l_idx = argv.index("-L")
    dest_idx = argv.index("ubuntu@10.0.0.65")
    assert l_idx < dest_idx, f"-L must come BEFORE dest; got argv={argv!r}"
