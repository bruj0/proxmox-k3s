"""tools.pveproxy — operator-side port-forward + kubeconfig helper.

Subcommands
-----------
  start       Open a `127.0.0.1:<local_port> -> <cp>:6443` tunnel
              through PVE for the named cluster AND fetch the
              kubeconfig so kubectl/k9s can hit it. Idempotent:
              if a tunnel is already running for this cluster,
              refresh the kubeconfig and re-print the state
              WITHOUT spawning a second tunnel. If the existing
              tunnel is dead (pid reused / process exited),
              start a fresh one.

  stop        Stop the cluster's tunnel (if any) and leave the
              kubeconfig in place.

  status      Print the current tunnel state: pid, local_port,
              target CP, started_at, and a probe result (is the
              port actually listening?).

  restart     stop + start.

  kubeconfig  Print the absolute path to the kubeconfig file
              for this cluster. Useful as:
                $(python -m tools.pveproxy --cluster cicd kubeconfig --print)

  kubectl -- ARGS...
              Run `kubectl --kubeconfig <cluster kubeconfig> ARGS`.
              Auto-starts the tunnel if it's not up.

Why
---
  The operator's host is NOT on the SDN, so it cannot route to the
  cluster VM's LAN IP. The apiserver is also bound to the CP
  node's loopback by default (k3s default). So the only path is:

    operator:127.0.0.1:<local>
       <- ssh -L over PVE ->
    PVE proxy (root@kvm.bruj0.net:6022)
       <- ProxyCommand ssh ->
    CP node:127.0.0.1:6443

  This tool owns that tunnel's lifecycle so the operator never has
  to remember the ssh flags. `kubectl` / `helm` / `k9s` just need
  `KUBECONFIG=<cluster_dir>/kubeconfig.pveproxy` and they work.

State file
----------
  `infra/clusters/<cluster>/.pveproxy.state.json` holds:
    {
      "pid": <int>,
      "local_port": <int>,
      "target_ip": <str>,
      "target_name": <str>,
      "started_at": <iso8601 str>,
      "kubeconfig_path": <abs path>,
      "ssh_argv_first_arg": <str>  # for human eyeballing
    }
  Written atomically (`os.replace`) so a half-written file from a
  crash never makes `start` think a tunnel is up when it isn't.

Usage examples
--------------
  python -m tools.pveproxy --cluster cicd start
  python -m tools.pveproxy --cluster cicd status
  python -m tools.pveproxy --cluster cicd kubectl get nodes -o wide
  python -m tools.pveproxy --cluster cicd stop
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Sequence

# These imports are intentionally absolute and reach into the
# orchestrator's vendored helpers. The orchestrator itself is not
# imported (no phases run on the operator side); we only use the
# leaf utilities.
from provisioner.lib.cluster_topology import ClusterTopology
from provisioner.lib.log import StructuredLogger
from provisioner.lib.pve_ssh import PveSshProxy, _pick_free_port

from tools.repo_locator import RepoNotFoundError, locate_repo_root


_K3S_KUBECONFIG_PATH = "/etc/rancher/k3s/k3s.yaml"
_DEFAULT_LOCAL_PORT = 16443  # matches the operator's prior picks
_STATE_FILENAME = ".pveproxy.state.json"
_KUBECONFIG_FILENAME = "kubeconfig.pveproxy"


@dataclasses.dataclass(frozen=True)
class ProxyState:
    """On-disk state for a cluster's tunnel.

    `pid` is the OS pid of the detached ssh -L process. We probe it
    via signal 0 (`os.kill(pid, 0)`) to check liveness; if the pid
    has been recycled, we treat the tunnel as dead and start a
    fresh one.
    """

    pid: int
    local_port: int
    target_ip: str
    target_name: str
    started_at: str
    kubeconfig_path: str
    cluster_dir: Path

    def to_json(self) -> str:
        d = dataclasses.asdict(self)
        d["cluster_dir"] = str(self.cluster_dir)
        return json.dumps(d, indent=2, sort_keys=True) + "\n"

    @property
    def local_endpoint(self) -> str:
        return f"https://127.0.0.1:{self.local_port}"

    @classmethod
    def from_json(cls, raw: str) -> ProxyState:
        d = json.loads(raw)
        # Forward-compat: ignore keys the tool doesn't know about.
        known = {f.name for f in dataclasses.fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        d["cluster_dir"] = Path(d["cluster_dir"])
        return cls(**d)

    @classmethod
    def load(cls, state_file: Path) -> ProxyState | None:
        """Read state from disk. Returns None if missing or corrupt."""
        if not state_file.exists():
            return None
        try:
            return cls.from_json(state_file.read_text())
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Corrupt state file -- treat as no state. The caller
            # will start a fresh tunnel.
            return None

    def save(self) -> None:
        """Atomically write state to disk."""
        self.cluster_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cluster_dir / f"{_STATE_FILENAME}.tmp"
        tmp.write_text(self.to_json())
        os.replace(tmp, self.cluster_dir / _STATE_FILENAME)


def _state_file(cluster_dir: Path) -> Path:
    return cluster_dir / _STATE_FILENAME


def _kubeconfig_path(cluster_dir: Path) -> Path:
    return cluster_dir / _KUBECONFIG_FILENAME


def _is_pid_alive(pid: int) -> bool:
    """Return True iff `pid` is running AND is an ssh -L process.

    We use signal 0 for liveness (no permission needed for a same-uid
    probe), then `os.readlink(/proc/<pid>/exe)` to confirm it's
    actually ssh. Without the exe check, a recycled pid would let
    us "find" a tunnel that doesn't really exist.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Pid is alive but we can't signal it -- it belongs to
        # another user. Definitely not our tunnel.
        return False
    exe_link = Path(f"/proc/{pid}/exe")
    if not exe_link.exists():
        # Not Linux /proc -- fall back to liveness only.
        return True
    try:
        target = os.readlink(exe_link)
    except OSError:
        return False
    return target.endswith("/ssh")


def _probe_local_port(port: int, timeout_s: float = 0.5) -> bool:
    """Return True iff 127.0.0.1:port accepts a TCP SYN right now."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _pick_local_port(requested: int | None) -> int:
    if requested is None:
        return _pick_free_port()
    return requested


def _find_first_cp(topo: ClusterTopology) -> dict[str, str]:
    if not topo.control_plane:
        raise SystemExit(
            f"cluster {topo.name!r} has no control-plane VMs in output.json"
        )
    return dict(topo.control_plane[0])


def _load_topology(repo_root: Path, cluster: str) -> tuple[ClusterTopology, Path]:
    """Load the cluster topology from either output.json or k3s.json.

    The cicd repo writes `output.json` from the stage-1 tofu apply.
    This proxmox-k3s repo writes `k3s.json` from the stage-2
    bootstrap. Either is acceptable; we prefer output.json when
    present (matches cicd's flow) and fall back to k3s.json
    (matches this repo's flow).
    """
    cluster_dir = repo_root / "infra" / "clusters" / cluster
    for fname in ("output.json", "k3s.json"):
        path = cluster_dir / fname
        if path.exists():
            return ClusterTopology.from_output_json(path), path
    raise SystemExit(
        f"could not find output.json or k3s.json under {cluster_dir}. "
        f"Run the proxmox-vms apply and the proxmox-k3s apply first."
    )


def fetch_kubeconfig_via_proxy(
    proxy: PveSshProxy,
    target_ip: str,
    logger: StructuredLogger,
) -> str:
    """Run `sudo cat <k3s.yaml>` on the CP node, return the file body.

    Used both by the bootstrap (Phase 4 kubeconfig_pull) and by this
    operator tool. The cloud image refuses root login (see
    `provisioner/lib/k3s_installer.py::_USER_DATA`); we land as
    `ubuntu` and `sudo -n` to read the kubeconfig.
    """
    inner = f"cat {_K3S_KUBECONFIG_PATH}"
    remote = f"sudo -n bash -c {shlex.quote(inner)}"
    proc = proxy.run(target_ip, remote, check=True, timeout=20)
    body = proc.stdout
    if "apiVersion: v1" not in body or "kind: Config" not in body:
        raise RuntimeError(
            f"refusing to write a kubeconfig that does not look like one. "
            f"first 200 chars of stdout: {body[:200]!r}, "
            f"stderr: {proc.stderr[:200]!r}"
        )
    logger.info(
        "pveproxy.kubeconfig_fetched",
        bytes=len(body),
        node_ip=target_ip,
    )
    return body


def rewrite_server_url(kubeconfig_text: str, local_port: int) -> str:
    """Replace the `server:` line with the local-forwarded URL.

    The CP-side kubeconfig points at `https://127.0.0.1:6443` (k3s
    binds loopback). We rewrite to `https://127.0.0.1:<local_port>`
    so kubectl on the operator host hits the tunnel instead of the
    operator's own loopback.

    No YAML parser -- the k3s-generated file is line-oriented and a
    silent mis-parse of a multi-doc YAML would be worse than a
    literal line replace.
    """
    new_url = f"https://127.0.0.1:{local_port}"
    out_lines: list[str] = []
    replaced = False
    for line in kubeconfig_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("server:") and not replaced:
            indent = line[: len(line) - len(stripped)]
            out_lines.append(f"{indent}server: {new_url}")
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        raise RuntimeError("no `server:` line in the k3s kubeconfig; refusing")
    return "\n".join(out_lines) + "\n"


# ---------- subcommand handlers ----------


def _cmd_start(args: argparse.Namespace, logger: StructuredLogger) -> int:
    cfg = args.config
    cluster_dir = cfg.cluster_dir
    state_file = _state_file(cluster_dir)

    existing = ProxyState.load(state_file)
    if existing is not None:
        pid_alive = _is_pid_alive(existing.pid)
        port_listening = _probe_local_port(existing.local_port)
        if pid_alive and port_listening:
            # Tunnel is up and listening. Refresh the kubeconfig
            # (cheap, idempotent) and re-print state. We do NOT
            # start a second tunnel -- that's the whole point.
            logger.info(
                "pveproxy.start.already_running",
                pid=existing.pid,
                local_port=existing.local_port,
                target_ip=existing.target_ip,
            )
            _refresh_kubeconfig(existing, logger)
            _print_status(existing, pid_alive=True, port_listening=True)
            return 0
        # Tunnel is stale. Fall through to start a fresh one --
        # but kill the old pid first if it's still alive (just
        # not listening on the expected port anymore).
        if pid_alive and not port_listening:
            logger.warn(
                "pveproxy.start.stale_tunnel",
                message=(
                    f"existing tunnel pid={existing.pid} on "
                    f"port {existing.local_port} is not listening; "
                    f"killing and starting a fresh tunnel"
                ),
                pid=existing.pid,
                local_port=existing.local_port,
            )
            try:
                os.kill(existing.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(0.5)

    topo, topo_path = _load_topology(cfg.repo_root, cfg.cluster)
    cp = _find_first_cp(topo)
    target_ip = cp["ip"]
    target_name = cp["name"]

    proxy = PveSshProxy(logger=logger)
    local_port = _pick_local_port(cfg.local_port)
    forward = proxy.port_forward(
        target_ip,
        remote_port=6443,
        remote_bind="127.0.0.1",
        local_port=local_port,
    )
    # `port_forward()` blocks until the local port accepts a TCP
    # SYN. If we get here, the tunnel is up.
    state = ProxyState(
        pid=forward.proc.pid,
        local_port=forward.local_port,
        target_ip=target_ip,
        target_name=target_name,
        started_at=datetime.now(UTC).isoformat(),
        kubeconfig_path=str(_kubeconfig_path(cluster_dir)),
        cluster_dir=cluster_dir,
    )
    state.save()

    _refresh_kubeconfig(state, logger)
    _print_status(state, pid_alive=True, port_listening=True)
    logger.info(
        "pveproxy.start.ok",
        pid=state.pid,
        local_port=state.local_port,
        target_ip=state.target_ip,
    )
    return 0


def _refresh_kubeconfig(state: ProxyState, logger: StructuredLogger) -> None:
    """Re-fetch the kubeconfig and rewrite `server:` to the tunnel port.

    Done on every `start` (including the idempotent "already
    running" path) so a rotated cert on the CP gets picked up the
    next time the operator starts the tunnel.
    """
    proxy = PveSshProxy(logger=logger)
    body = fetch_kubeconfig_via_proxy(proxy, state.target_ip, logger)
    rewritten = rewrite_server_url(body, state.local_port)
    kc_path = Path(state.kubeconfig_path)
    kc_path.parent.mkdir(parents=True, exist_ok=True)
    kc_path.write_text(rewritten)
    kc_path.chmod(0o600)


def _cmd_stop(args: argparse.Namespace, logger: StructuredLogger) -> int:
    cfg = args.config
    state_file = _state_file(cfg.cluster_dir)
    existing = ProxyState.load(state_file)
    if existing is None:
        print(f"[pveproxy] no tunnel state at {state_file}", file=sys.stderr)
        return 0
    pid_alive = _is_pid_alive(existing.pid)
    if pid_alive:
        try:
            os.kill(existing.pid, signal.SIGTERM)
            logger.info(
                "pveproxy.stop.terminated",
                pid=existing.pid,
                local_port=existing.local_port,
            )
        except ProcessLookupError:
            pass
    else:
        logger.warn(
            "pveproxy.stop.no_pid",
            message=(
                f"state file refers to pid={existing.pid} which is not "
                f"alive; clearing state without signaling"
            ),
            pid=existing.pid,
        )
    # Always remove the state file. We never want a stale state
    # file to fool a future `start` into thinking the tunnel is up.
    state_file.unlink(missing_ok=True)
    print(f"[pveproxy] stopped tunnel (pid={existing.pid})")
    return 0


def _cmd_status(args: argparse.Namespace, logger: StructuredLogger) -> int:
    cfg = args.config
    state_file = _state_file(cfg.cluster_dir)
    existing = ProxyState.load(state_file)
    if existing is None:
        print(f"[pveproxy] {cfg.cluster}: no tunnel state on disk", file=sys.stderr)
        return 1
    pid_alive = _is_pid_alive(existing.pid)
    port_listening = _probe_local_port(existing.local_port)
    _print_status(existing, pid_alive=pid_alive, port_listening=port_listening)
    # Exit non-zero if the tunnel is stale so wrappers can detect it.
    return 0 if (pid_alive and port_listening) else 2


def _cmd_restart(args: argparse.Namespace, logger: StructuredLogger) -> int:
    rc = _cmd_stop(args, logger)
    if rc != 0:
        return rc
    return _cmd_start(args, logger)


def _cmd_kubeconfig(args: argparse.Namespace, logger: StructuredLogger) -> int:
    cfg = args.config
    kc = _kubeconfig_path(cfg.cluster_dir)
    if args.print:
        # For `$(python -m tools.pveproxy --cluster cicd kubeconfig --print)`
        print(str(kc))
        return 0
    print(f"[pveproxy] kubeconfig: {kc}")
    if not kc.exists():
        print(
            f"[pveproxy] WARNING: kubeconfig file does not exist yet; "
            f"run `python -m tools.pveproxy --cluster {cfg.cluster} start` "
            f"first.",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_kubectl(args: argparse.Namespace, logger: StructuredLogger) -> int:
    """Run `kubectl` against the cluster's kubeconfig.

    Auto-starts the tunnel if it's not already up. We always pass
    --kubeconfig explicitly so we never accidentally pick up
    $KUBECONFIG from the operator's environment (which could point
    at a stale or wrong-cluster config).
    """
    cfg = args.config
    kubectl_args = args.kubectl_args
    if not kubectl_args:
        print("[pveproxy] `kubectl` subcommand requires ARGS after `--`", file=sys.stderr)
        return 2

    # Ensure the tunnel is up. If `start` is a no-op because the
    # tunnel is already running, this is cheap; if it's not up,
    # this brings it up.
    state_file = _state_file(cfg.cluster_dir)
    existing = ProxyState.load(state_file)
    needs_start = existing is None or not (
        _is_pid_alive(existing.pid) and _probe_local_port(existing.local_port)
    )
    if needs_start:
        rc = _cmd_start(args, logger)
        if rc != 0:
            return rc
        # Re-read state -- `_cmd_start` may have changed local_port
        # (e.g. operator picked a different port than we asked for).
        existing = ProxyState.load(state_file)
        if existing is None:
            print("[pveproxy] start claimed success but no state file", file=sys.stderr)
            return 1

    assert existing is not None  # narrowed above
    kc = Path(existing.kubeconfig_path)
    if not kc.exists():
        print(
            f"[pveproxy] kubeconfig missing at {kc}; run `start` first",
            file=sys.stderr,
        )
        return 1

    cmd = ["kubectl", "--kubeconfig", str(kc), *kubectl_args]
    print(f"[pveproxy] $ {' '.join(shlex.quote(c) for c in cmd)}", file=sys.stderr)
    # We deliberately do NOT capture stdout -- kubectl output goes
    # straight to the operator's terminal. stderr is also inherited
    # so kubectl errors are visible.
    completed = subprocess.run(cmd, check=False)  # noqa: S603 -- explicit operator invocation
    return completed.returncode


def _print_status(
    state: ProxyState, *, pid_alive: bool, port_listening: bool
) -> None:
    status = "UP" if (pid_alive and port_listening) else "STALE"
    print(
        f"[pveproxy] {status}  pid={state.pid}  "
        f"local={state.local_endpoint}  "
        f"target={state.target_name}({state.target_ip}):6443  "
        f"started={state.started_at}  "
        f"kubeconfig={state.kubeconfig_path}"
    )


# ---------- argparse plumbing ----------


@dataclasses.dataclass(frozen=True)
class ToolConfig:
    """Operator-passed config that all subcommands share."""

    cluster: str
    repo_root: Path
    cluster_dir: Path
    output_json: Path
    local_port: int | None


def _resolve_tool_config(args: argparse.Namespace) -> ToolConfig:
    try:
        repo_root = locate_repo_root(flag_value=args.repo_root)
    except RepoNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    cluster_dir = repo_root / "infra" / "clusters" / args.cluster
    output_json = cluster_dir / "output.json"
    return ToolConfig(
        cluster=args.cluster,
        repo_root=repo_root,
        cluster_dir=cluster_dir,
        output_json=output_json,
        local_port=args.local_port,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools.pveproxy",
        description=(
            "Operator-side PVE port-forward + kubeconfig helper for "
            "the proxmox-k3s clusters. Subcommands: "
            "start | stop | status | restart | kubeconfig | kubectl -- ARGS..."
        ),
    )
    parser.add_argument("--cluster", required=True)
    parser.add_argument(
        "--repo-root",
        default=None,
        help=(
            "proxmox-k3s repo root (the dir that contains "
            "infra/clusters/<cluster>/output.json). "
            "Defaults to PROXMOX_K3S_REPO, then cwd, then walking "
            "up to find infra/clusters/."
        ),
    )
    parser.add_argument(
        "--local-port",
        type=int,
        default=None,
        help=(
            "operator-side port for the apiserver tunnel. "
            f"Default {_DEFAULT_LOCAL_PORT}. The kubeconfig's "
            f"`server:` URL always points at this port."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "start",
        help=(
            "Open the PVE port-forward for this cluster and write "
            "the kubeconfig. Idempotent: never starts a second "
            "tunnel if one is already listening."
        ),
    )

    sub.add_parser(
        "stop",
        help="Stop the cluster's port-forward (if any).",
    )

    sub.add_parser(
        "status",
        help="Print the current tunnel state.",
    )

    sub.add_parser(
        "restart",
        help="Stop + start.",
    )

    p_kc = sub.add_parser(
        "kubeconfig",
        help="Print the path to the cluster's kubeconfig file.",
    )
    p_kc.add_argument(
        "--print",
        action="store_true",
        help=(
            "Print only the absolute path, no decoration. Useful as: "
            "KUBECONFIG=$(python -m tools.pveproxy --cluster cicd "
            "kubeconfig --print)"
        ),
    )

    p_kctl = sub.add_parser(
        "kubectl",
        help=(
            "Run `kubectl` against the cluster's kubeconfig. "
            "Auto-starts the tunnel if not already up. ARGS after "
            "`--` are passed to kubectl verbatim."
        ),
    )
    p_kctl.add_argument(
        "kubectl_args",
        nargs=argparse.REMAINDER,
        help="Args to pass to kubectl (use `--` to separate).",
    )

    args = parser.parse_args(argv)
    args.config = _resolve_tool_config(args)
    return args


_HANDLERS = {
    "start": _cmd_start,
    "stop": _cmd_stop,
    "status": _cmd_status,
    "restart": _cmd_restart,
    "kubeconfig": _cmd_kubeconfig,
    "kubectl": _cmd_kubectl,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logger = StructuredLogger("pveproxy")
    handler = _HANDLERS[args.command]
    # If `kubectl_args` was filled with the leading `--` token from
    # argparse.REMAINDER, strip it -- kubectl doesn't want it.
    if (
        args.command == "kubectl"
        and getattr(args, "kubectl_args", None)
        and args.kubectl_args
        and args.kubectl_args[0] == "--"
    ):
        args.kubectl_args = args.kubectl_args[1:]
    return handler(args, logger)


if __name__ == "__main__":
    sys.exit(main())
