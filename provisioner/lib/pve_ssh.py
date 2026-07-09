"""PveSshProxy -- central SSH-jump-host plumbing for the cluster pipeline.

All cluster VMs live on the SDN (`vnet0`, address range 10.0.0.50-200),
which is not directly routable from the operator host. We tunnel
through the Proxmox VE host via a `ProxyCommand` wrapper around
`ssh -W %h:%p` -- the shape the live `install_k3s` run validated
(more in step 4a.3.8 of the skill).

This module centralises that plumbing so `tools/ssh_proxy.py`
(interactive SSH into a VM) and `tools/kubeconfig_puller.py`
(kubectl + tunnel) don't drift in how they build the argv.

Three helpers:

  ssh_argv(target, command=...) -> list[str]
      Returns the argv list to hand to `subprocess.run`. If
      `command` is None, the argv stops at the host token (so
      the caller can append arbitrary remote args).

  run(target, command, *, check=True, timeout=15) -> subprocess.CompletedProcess
      Convenience wrapper that calls `subprocess.run` and,
      when `check=True`, raises `RuntimeError` on non-zero
      return code. Never logs token-bearing env.

  port_forward(target_ip, remote_port=6443, local_port=None) -> ForwardedPort
      Starts a background `ssh -L local_port:127.0.0.1:remote_port`
      tunnel from the operator host through PVE to the cluster
      VM. Returns a `ForwardedPort` object with `.local_port`,
      `.local_endpoint`, `.terminate()`, and `.wait_ready()`.
      The PVE-side `ProxyCommand` ensures the forward goes
      through to the target VM, not just to the PVE host.

The kube-apiserver is reachable via the CP host IP after the
`install_k3s` phase installs the server. Before that, the apiserver
is only on the cluster control-plane's loopback (k3s binds 127.0.0.1
on first install). To talk to that loopback from the operator
host through this proxy we forward `127.0.0.1:6443` on the CP node
to a local port. The `ForwardedPort` shape makes it easy for
kubectl-context writers to point a kubeconfig at
`https://127.0.0.1:<local_port>`.

WP08 (2026-07-08): the cluster VIP layer (kube-vip) is gone. We
talk directly to the CP host IP for control-plane operations.
"""
from __future__ import annotations

import shlex
import socket
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass

from .log import StructuredLogger

_DEFAULT_JUMP = "root@kvm.bruj0.net -p 6022"
_DEFAULT_SSH_USER = "ubuntu"

# Ports we know how to forward. We only forward what k8s actually needs:
#   6443  - kube-apiserver
#   10250 - kubelet (read-only stats, /exec if the operator opens it)
# 10255 is the read-only-port on kubelet; k3s does not enable it by
# default, so we leave it out.
DEFAULT_API_PORT = 6443
DEFAULT_KUBELET_PORT = 10250


class PveSshProxy:
    """Reusable SSH proxy through a PVE jump host.

    Stateless beyond configuration; safe to instantiate per-call or
    keep one around for the lifetime of the script.
    """

    def __init__(
        self,
        jump_host: str = _DEFAULT_JUMP,
        ssh_user: str = _DEFAULT_SSH_USER,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.jump_host = jump_host
        # The jump host is something like `root@kvm.bruj0.net -p 6022`.
        # Splittable; tail tokens (after the user@host) carry the
        # flags like `-p 6022`. Stash both halves.
        parts = jump_host.split()
        self._jump_argv = ["ssh", *parts]
        self._jump_user_host = parts[0]
        self.ssh_user = ssh_user
        self.logger = logger or StructuredLogger("pve_ssh")

    # ---------- argv rendering ----------

    def ssh_argv(
        self,
        target: str,
        *,
        command: Sequence[str] | str | None = None,
    ) -> list[str]:
        """Return an argv for `ssh <opts> [user@]target [command]`.

        Args:
          target: IP or DNS name to land on. When `target` equals the
            host portion of the jump (e.g. "kvm.bruj0.net"), the
            caller is sshing PVE itself and we skip the ProxyCommand.
          command: optional remote command. Pass a list (each token
            is shlex.quote'd) or a single string (used verbatim).
        """
        base = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
        target_user_host = f"{self.ssh_user}@{target}"
        if self._is_jump_host(target):
            # PVE itself: connect directly.
            argv = base + self._jump_argv
        else:
            # Cluster VM: tunnel via PVE.
            proxy_tokens = self._jump_argv + ["-W", "%h:%p"]
            proxy_cmd = " ".join(shlex.quote(t) for t in proxy_tokens)
            argv = base + [
                "-o",
                f"ProxyCommand={proxy_cmd}",
                target_user_host,
            ]
        if command is not None:
            argv.append("--")
            if isinstance(command, str):
                argv.append(command)
            else:
                argv.extend(shlex.quote(t) for t in command)
        return argv

    # ---------- convenience ----------

    def run(
        self,
        target: str,
        command: str,
        *,
        check: bool = True,
        timeout: int = 15,
    ) -> subprocess.CompletedProcess[str]:
        """Run a single command on `target`; return the CompletedProcess."""
        try:
            proc = subprocess.run(  # noqa: S603 -- single, documented shell call.
                self.ssh_argv(target, command=command),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError(
                f"ssh to {target!r} failed: {exc}"
            ) from exc
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"ssh {target} '{command}' returned {proc.returncode}: "
                f"stderr={proc.stderr.strip()[:200]}"
            )
        return proc

    # ---------- port forwarding ----------

    def port_forward(
        self,
        target: str,
        *,
        remote_port: int = DEFAULT_API_PORT,
        remote_bind: str = "127.0.0.1",
        local_port: int | None = None,
    ) -> ForwardedPort:
        """Start an SSH-local-forward tunnel to `target:remote_port`.

        Returns a `ForwardedPort` with `.local_port` and `.terminate()`.
        Blocks until the local listening port accepts a TCP connection
        (so the caller can immediately use it). If we cannot get the
        tunnel up within a few seconds we exit non-zero with a clear
        message.
        """
        local_port = local_port or _pick_free_port()
        # Build the argv from scratch. Order matters: every flag
        # (including -L) MUST appear BEFORE the destination host
        # token, otherwise ssh treats the trailing tokens as the
        # remote command to exec. `ssh_argv()` alone is built for
        # execing a command, so we re-derive the flag prefix.
        forward_spec = f"{local_port}:{remote_bind}:{remote_port}"
        target_user_host = f"{self.ssh_user}@{target}"
        if self._is_jump_host(target):
            base_prefix: list[str] = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                *self._jump_argv,
            ]
        else:
            proxy_tokens = self._jump_argv + ["-W", "%h:%p"]
            proxy_cmd = " ".join(shlex.quote(t) for t in proxy_tokens)
            base_prefix = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                f"ProxyCommand={proxy_cmd}",
                "-N",  # no remote command -- forward-only
                "-L",
                forward_spec,
                # `ExitOnForwardFailure=yes` is critical: without it,
                # if the local bind fails (e.g. port in use) ssh
                # silently retries on a different ephemeral port or
                # never returns a clear error, and the operator sees
                # "port not open" forever. With the flag set, ssh
                # exits non-zero as soon as the -L bind fails, and
                # `wait_ready` surfaces that as a clear error.
                "-o",
                "ExitOnForwardFailure=yes",
                # Keep the connection alive across brief network blips.
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=4",
                target_user_host,
            ]
        proc = subprocess.Popen(  # noqa: S603 -- forward-only tunnel.
            base_prefix,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # so terminate() can detach cleanly
        )
        fp = ForwardedPort(
            target=target,
            local_port=local_port,
            remote_port=remote_port,
            remote_bind=remote_bind,
            proc=proc,
            proxy=self,
            logger=self.logger,
        )
        fp.wait_ready()
        return fp

    # ---------- internals ----------

    def _is_jump_host(self, target: str) -> bool:
        # Strip a possibly-leading user@ so a "root@kvm.bruj0.net"
        # target compares equal to the jump user@host portion.
        bare = target.split("@", 1)[-1].split(":", 1)[0]
        jump_bare = self._jump_user_host.split("@", 1)[-1]
        return bare == jump_bare


def _pick_free_port() -> int:
    """Pick a free TCP port on the operator host.

    Used by `port_forward()` when the caller didn't pick one. We
    bind a socket to port 0 (kernel assigns), read the port, close
    the socket; there is a small race window between close and the
    subsequent ssh -L but on loopback it's effectively zero.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@dataclass
class ForwardedPort:
    """A live background `ssh -L` tunnel.

    Lifetime is managed by the caller -- always call `.terminate()` in
    a `finally` block. `.wait_ready()` blocks until the local port
    accepts a TCP connection (or raises on timeout).
    """

    target: str
    local_port: int
    remote_port: int
    remote_bind: str
    proc: subprocess.Popen[bytes]  # stderr=PIPE -> bytes stream
    proxy: PveSshProxy
    logger: StructuredLogger

    @property
    def local_endpoint(self) -> str:
        return f"https://127.0.0.1:{self.local_port}"

    def wait_ready(self, timeout_s: float = 15.0) -> None:
        """Block until the local listening port accepts a TCP SYN.

        Implementation: poll `socket.create_connection()` against
        127.0.0.1:local_port with a 0.5s per-attempt timeout and a
        0.3s sleep between attempts. The pattern is from
        abnershang.com/blog/ssh-tunnel-inside-your-db-client -- the
        0.5s socket timeout gives the OS time to service the
        connect syscall, and the 0.3s sleep stops us from
        busy-spinning while the ProxyCommand ssh handshake is in
        flight (the double-ssh takes ~1.5s to bring the tunnel up
        on the live host).

        When ssh exits prematurely (rc != None), surface the
        captured stderr so the operator sees the actual ssh error
        message instead of a generic "port never opened".
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                stderr_text = ""
                if self.proc.stderr is not None:
                    raw = self.proc.stderr.read() or b""
                    stderr_text = (
                        raw.decode("utf-8", errors="replace")
                        if isinstance(raw, bytes)
                        else raw
                    )
                raise RuntimeError(
                    f"ssh forward exited prematurely (rc={self.proc.returncode}): "
                    f"stderr={stderr_text!r}"
                )
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.local_port), timeout=0.5
                ) as sock:
                    sock.close()
                self.logger.info(
                    "pve_ssh.forward.ready",
                    target=self.target,
                    local_port=self.local_port,
                    remote_port=self.remote_port,
                )
                return
            except OSError:
                time.sleep(0.3)
        # Timed out; if ssh is still alive, try to grab whatever
        # partial stderr has accumulated for the operator.
        partial = ""
        if self.proc.stderr is not None:
            try:
                # Non-blocking read of whatever is buffered on the
                # pipe. `read(1024)` returns up to 1024 bytes from
                # the OS pipe buffer; on a Popen stderr=PIPE the
                # pipe is blocking by default, so we use a tiny
                # timeout on the underlying fd. Polling Popen is
                # cheaper than monkey-patching the fd.
                raw = self.proc.stderr.read(1024) or b""
            except Exception:
                raw = b""
            partial = raw.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"ssh -L tunnel to {self.target}:{self.remote_port} did not "
            f"become ready within {timeout_s}s; "
            f"ssh_alive={self.proc.poll() is None}, "
            f"partial_stderr={partial!r}"
        )

    def terminate(self) -> None:
        """Stop the background ssh process; idempotent."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.logger.info(
            "pve_ssh.forward.terminated",
            target=self.target,
            local_port=self.local_port,
        )
