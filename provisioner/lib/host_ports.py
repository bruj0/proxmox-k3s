"""WP05: no-new-host-ports verifier.

M2 misfit verification. SSHes to the PVE host, dumps the current nft
prerouting chain, and diffs it against a captured baseline. Any new DNAT
rule (TCP/UDP dport -> ip:port) is treated as an unauthorized host port
addition and surfaces as HostPortsAddedError -- which the
bootstrap_cluster.py harness converts into a structured BootstrapError.

The baseline file is captured once at WP00 bootstrap (via a baseline
script). On every WP05+ run we assert the diff is empty.

Output of `nft list chain ip nat prerouting` looks like:

    table ip nat {
        chain prerouting {
            type nat hook prerouting priority 0; policy accept;
        }
    }

If a Cloudflare-tunnel-induced DNAT rule were silently added (e.g. via a
chart hook), it would appear inline as a `tcp dport X dnat to Y:Z` line.
We diff line-by-line against the baseline; new lines with `dnat to`
are flagged.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .log import StructuredLogger

_LOG = StructuredLogger("host_ports")


class HostPortsAddedError(RuntimeError):
    """Raised when a new DNAT rule is detected in the PVE nft prerouting chain."""


_DNAT_LINE = re.compile(r"\bdnat\s+to\b", re.IGNORECASE)


def _read_baseline(path: Path) -> list[str]:
    if not path.exists():
        raise HostPortsAddedError(
            f"baseline file missing: {path}; "
            "run tools/scripts/capture_host_ports_baseline.sh before bootstrapping."
        )
    return path.read_text().splitlines()


def _read_current(ssh_target: str, ssh_port: str) -> list[str]:
    result = subprocess.run(
        [
            "ssh",
            "-p",
            ssh_port,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            ssh_target,
            "nft list chain ip nat prerouting",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.splitlines()


def _diff_dnat_lines(baseline: Sequence[str], current: Sequence[str]) -> list[str]:
    """Return current DNAT lines that do not appear (verbatim) in the baseline.

    We compare line-by-line (after .strip()) so that an *additional* DNAT
    rule shows up even when the baseline already has some. White-space
    differences are tolerated.

    Note: we don't try to detect deletions (DNAT rules that were removed)
    -- M2 is about new host ports, not about churning existing ones.
    """
    baseline_set = {line.strip() for line in baseline if _DNAT_LINE.search(line)}
    new: list[str] = []
    for line in current:
        stripped = line.strip()
        if _DNAT_LINE.search(stripped) and stripped not in baseline_set:
            new.append(stripped)
    return new


def verify_no_new_dnat_rules(
    baseline_path: Path,
    *,
    ssh_target: str = "root@10.0.0.1",
    ssh_port: str = "6022",
    on_ssh_failure: Callable[[str, str, Exception], None] | None = None,
) -> None:
    """Assert the live PVE prerouting chain matches the captured baseline.

    Args:
        baseline_path: text file containing the captured `nft list chain
            ip nat prerouting` output.
        ssh_target: PVE ssh host (`user@host`). Defaults to root@10.0.0.1
            (the proxmox host's LAN address).
        ssh_port: PVE ssh port. Defaults to 6022 (the non-default sshd port
            operators use to avoid the auto-attack surface on 22).
        on_ssh_failure: optional callable (phase_name, ssh_target, exc)
            invoked when the ssh sub-process fails. The caller (typically
            bootstrap_cluster.py) passes a function that raises
            BootstrapError. Defaults to raising HostPortsAddedError so the
            unit tests can exercise the path without bootstrapping the
            whole orchestrator.

    Raises:
        HostPortsAddedError: new DNAT rule detected, OR non-zero ssh exit
            (when no `on_ssh_failure` was provided).
    """
    baseline = _read_baseline(baseline_path)
    try:
        current = _read_current(ssh_target, ssh_port)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        if on_ssh_failure is not None:
            on_ssh_failure("host_ports", ssh_target, exc)
            return
        raise HostPortsAddedError(
            f"ssh to PVE failed: {exc!r}"
        ) from exc
    new_dnat = _diff_dnat_lines(baseline, current)
    if new_dnat:
        _LOG.error(
            "host_ports.violation",
            error="new_dnat_rules",
            resolution="inspect nft table; revert any unintended DNAT rules",
            count=len(new_dnat),
            sample=new_dnat[0],
        )
        raise HostPortsAddedError(
            f"new DNAT rules detected in PVE prerouting chain: {new_dnat!r}"
        )
    _LOG.info("host_ports.ok", baseline=str(baseline_path), ssh_target=ssh_target)
