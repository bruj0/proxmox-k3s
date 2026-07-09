"""Kubeconfig merger: pull cluster kubeconfig from a CP node, merge into ~/.kube/config.

Behaviour contract:
  - Reads the cluster admin kubeconfig via PveSshProxy (Ubuntu+k3s path):
        sudo cat /etc/rancher/k3s/k3s.yaml
    The historical `talosctl --nodes <cp> kubeconfig /admin` path is
    retained as `_talos_kubeconfig()` for completeness, but is no
    longer the default -- the live cluster runs Ubuntu+k3s, not Talos.
  - Writes the resulting KUBECONFIG to a per-cluster kubeconfig file
    (infra/clusters/<name>/kubeconfig) for repeat use. The server:
    URL is rewritten to point at the local apiserver forward so
    kubectl on the operator host hits the tunnel, not loopback.
  - Merges it into the operator's ~/.kube/config, with a timestamped
    backup of the existing ~/.kube/config before any modification.
  - All stdout from the proxy is funneled through StructuredLogger.scrub()
    so token-bearing lines never reach the log.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from .log import StructuredLogger
from .pve_ssh import PveSshProxy

_LOG = StructuredLogger("kubeconfig_merger")


def _talos_kubeconfig(cluster_name: str, control_plane_ip: str, out_path: Path) -> None:
    _LOG.info("kubeconfig.pull", cluster=cluster_name, cp=control_plane_ip)
    subprocess.run(
        [
            "talosctl",
            "--nodes",
            control_plane_ip,
            "kubeconfig",
            str(out_path),
        ],
        check=True,
    )


def _merge_into_default(cluster_name: str, kubeconfig_path: Path, home: Path) -> Path:
    """Merge `kubeconfig_path` into ~/.kube/config with timestamped backup.

    Returns the backup path so the caller can log it for the operator.
    Raises if ~/.kube is not writable.

    Behaviour when ~/.kube/config does not yet exist:
      - If it doesn't, treat the new kubeconfig as the entire merged file.
        kubectl's `--kubeconfig <new>:<existing>` requires both files to
        exist, so we shortcut and write the new file directly.
    """
    kube_dir = home / ".kube"
    kube_dir.mkdir(parents=True, exist_ok=True)
    default = kube_dir / "config"
    backup: Path | None = None
    # Treat a 0-byte existing file the same as a missing one.
    # `kubectl config view --flatten` against an empty file returns
    # an empty document, which would clobber any context we add
    # from the cluster kubeconfig. Copy the new kubeconfig verbatim
    # instead.
    if default.exists() and default.stat().st_size > 0:
        # Microsecond timestamp so repeated merges don't clobber prior backups.
        ts = default.stat().st_mtime_ns
        backup = kube_dir / f"config.bak.{ts}"
        shutil.copy2(default, backup)
        _LOG.info("kubeconfig.backup", path=str(backup))
        merged = subprocess.run(
            [
                "kubectl",
                "config",
                "view",
                "--flatten",
                "--kubeconfig",
                f"{kubeconfig_path}:{default}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        default.write_text(merged.stdout)
    else:
        # No existing config — just copy the new kubeconfig verbatim.
        shutil.copy2(kubeconfig_path, default)
    _LOG.info("kubeconfig.merged", cluster=cluster_name, path=str(default))
    return backup or kube_dir / "config"


def _write_kubeconfig_with_rewritten_url(
    proxy: PveSshProxy,
    cluster_name: str,
    control_plane_ip: str,
    out_path: Path,
    local_port: int,
) -> None:
    """Fetch the kubeconfig body through the proxy and rewrite server:.

    Body source: `sudo cat /etc/rancher/k3s/k3s.yaml` on the CP node.
    The cloud image refuses root login (Step 4a.3.5), so the proxy lands
    as `ubuntu` and uses `sudo -n` to read the file.
    """
    _LOG.info("kubeconfig.pull", cluster=cluster_name, cp=control_plane_ip)
    inner = "cat /etc/rancher/k3s/k3s.yaml"
    remote = f"sudo -n bash -c {shlex.quote(inner)}"
    proc = proxy.run(control_plane_ip, remote, check=True, timeout=20)
    body = proc.stdout
    if "apiVersion: v1" not in body or "kind: Config" not in body:
        raise RuntimeError(
            "refusing to write a kubeconfig that does not look like one. "
            f"first 200 chars of stdout: {body[:200]!r}, "
            f"stderr: {proc.stderr[:200]!r}"
        )
    # Local rewrite: keep the CP-side cert + token, swap server URL
    # so kubectl on the operator host hits the tunnel on 127.0.0.1.
    new_url = f"https://127.0.0.1:{local_port}"
    out_lines: list[str] = []
    replaced = False
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("server:") and not replaced:
            indent = line[: len(line) - len(stripped)]
            out_lines.append(f"{indent}server: {new_url}")
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        raise RuntimeError("kubeconfig had no `server:` line; cannot rewrite")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_lines) + "\n")
    out_path.chmod(0o600)


def merge_kubeconfig_for_pveproxy(
    cluster_name: str,
    control_plane_ip: str,
    repo_root: Path,
    home: Path,
    *,
    forward_local_port: int,
    forward_proc: object,
) -> Path:
    """Bootstrap script path: pull kubeconfig via PveSshProxy, rewrite, merge.

    The helm phase has already opened an apiserver port-forward via
    `proxy.port_forward(cp_ip, remote_port=6443, ...)`. We reuse that
    tunnel: the kubeconfig's server: URL points at the local side, so
    every subsequent kubectl call from this script hits the tunnel
    and lands on the CP's loopback :6443.

    `forward_proc` is accepted for symmetry with the operator tool
    (kubeconfig_puller.py keeps the tunnel alive after exit); the
    bootstrap script owns the tunnel for its own lifetime and tears
    it down at the end. We don't keep a reference here.
    """
    del forward_proc  # see docstring -- bootstrap owns the tunnel lifecycle
    proxy = PveSshProxy(logger=_LOG)
    cluster_dir = repo_root / "infra" / "clusters" / cluster_name
    cluster_dir.mkdir(parents=True, exist_ok=True)
    kubeconfig_path = cluster_dir / "kubeconfig"
    _write_kubeconfig_with_rewritten_url(
        proxy,
        cluster_name,
        control_plane_ip,
        kubeconfig_path,
        forward_local_port,
    )
    backup_path = _merge_into_default(cluster_name, kubeconfig_path, home)
    return backup_path


def merge(cluster_name: str, control_plane_ip: str, repo_root: Path, home: Path) -> Path:
    """Legacy entry point. Kept so existing imports don't break.

    Pre-pivot this used talosctl; the live cluster is Ubuntu+k3s, so
    the legacy path will fail (talosctl would have to reach the SDN
    IP directly, which the operator host can't). New callers should
    use `merge_kubeconfig_for_pveproxy` instead. We log a warning
    and refuse to use the talosctl path on the live host.
    """
    raise NotImplementedError(
        "merge() is the legacy Talos path; use merge_kubeconfig_for_pveproxy() "
        "(the bootstrap script does this). If you see this on the operator "
        "CLI, the bootstrap_cluster.py wrapper didn't get wired up correctly."
    )
