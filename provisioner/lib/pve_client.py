"""PveClient — thin subprocess wrapper for Proxmox VE CLI utilities.

Backs onto `qm` and `pvesh` (always present on a PVE host). Wraps them so
the rest of the pipeline doesn't have to construct CLI strings, and so
failures become structured log entries instead of raw stderr.

This is intentionally not the bpg/proxmox Go SDK — we want the build_image
script to run on a workstation without requiring a Go toolchain.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from .log import StructuredLogger


@dataclass
class PveError(RuntimeError):
    """Raised when `qm`/`pvesh` exits non-zero on a non-cleanup call."""

    command: list[str]
    returncode: int
    stderr: str


@dataclass
class PveClient:
    logger: StructuredLogger
    endpoint: str = ""
    ssh_target: str = ""  # e.g. "ssh -p 6022 -o BatchMode=yes root@kvm.bruj0.net"
                           # when set, every qm/pvesh/pvesm call is SSH-wrapped
                           # so the operator host needs no PVE CLI installed.

    @dataclass
    class RunResult:
        returncode: int
        stdout: str
        stderr: str

    def _ssh_prefix(self) -> list[str]:
        """Return the ssh argv prefix for remote command invocation.

        Parses `ssh_target` into argv (assumed space-separated), then
        prepends the literal "ssh" executable so the resulting argv
        is a valid `subprocess.run` argv.

        Cached on first call so we don't re-split on every command.
        """
        if not hasattr(self, "_ssh_prefix_cache"):
            self._ssh_prefix_cache = (
                ["ssh", *self.ssh_target.split()] if self.ssh_target else []
            )
        return list(self._ssh_prefix_cache)

    def _run(
        self,
        args: list[str],
        *,
        timeout: float = 30.0,
        allow_failure: bool = False,
    ) -> RunResult:
        """Run a subprocess command and stream structured logs.

        `allow_failure=True` swallows non-zero exit codes (used for
        best-effort cleanup like qm destroy on a non-existent VM).

        When `ssh_target` is set on this client, the command is run on
        the PVE host via `ssh <target> <args...>`. The operator host then
        never needs the PVE CLI installed.

        Implementation note: we use `Popen.communicate(timeout=...)`
        rather than `subprocess.run(timeout=...)` so that on timeout we
        can recover any partial stderr the subprocess produced before
        we killed it. This is critical for diagnosing hangs like
        "qm shutdown" stuck on a PVE lock — the stderr often contains
        "trying to acquire lock..." which we want to surface to the
        operator in the audit log.
        """
        import subprocess as _sp
        if self.ssh_target:
            args = [*self._ssh_prefix(), *args]
        self.logger.info(
            step="pve_exec",
            command=args[0],
            args_count=len(args),
            remote=bool(self.ssh_target),
            timeout_s=timeout,
        )
        proc = _sp.Popen(
            args,
            stdin=_sp.DEVNULL,
            stdout=_sp.PIPE,
            stderr=_sp.PIPE,
            text=True,
        )
        timed_out = False
        partial_stdout = ""
        partial_stderr = ""
        try:
            partial_stdout, partial_stderr = proc.communicate(timeout=timeout)
        except _sp.TimeoutExpired:
            timed_out = True
            # Capture whatever the subprocess wrote before we killed it.
            # We don't wait() — communicate() with timeout already
            # terminated the process group behaviour; calling wait()
            # again would risk a deadlock on a still-pipe child.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                partial_stdout, partial_stderr = proc.communicate(timeout=2.0)
            except _sp.TimeoutExpired:
                partial_stdout, partial_stderr = "", "process killed; stderr unreadable"
        returncode = proc.returncode if proc.returncode is not None else -1

        result = self.RunResult(
            returncode=returncode,
            stdout=partial_stdout,
            stderr=partial_stderr,
        )

        if timed_out:
            self.logger.error(
                step="pve_timeout",
                error=f"{args[0]} timed out after {timeout}s",
                partial_stderr_summary=_one_line(partial_stderr),
                command_preview=" ".join(args[:5]) + (" ..." if len(args) > 5 else ""),
                resolution=(
                    "inspect partial stderr above; usually a PVE-side "
                    "lock contention (other qm/pvesh call in flight). "
                    "force-stop the VM if the call was shutdown-related."
                ),
            )
            if not allow_failure:
                raise PveError(
                    command=args,
                    returncode=-1,
                    stderr=f"TIMEOUT after {timeout}s; partial: {partial_stderr}",
                )
            return result

        if returncode != 0:
            self.logger.warn(
                step="pve_nonzero_exit",
                message=f"{args[0]} exited with code {returncode}",
                stderr_summary=_one_line(partial_stderr),
                allow_failure=allow_failure,
            )
            if not allow_failure:
                raise PveError(
                    command=args,
                    returncode=returncode,
                    stderr=partial_stderr,
                )
        return result

    # ----- public API -----

    def destroy_vm(self, vmid: int) -> None:
        """Best-effort VM destroy. Swallows non-zero exit (the VM may
        already be gone — that's still a successful cleanup).

        We also stop the VM first in case it's running; qm destroy on a
        running VM will fail.
        """
        self.logger.info(step="cleanup_destroy_vm", vmid=vmid, mode="stop_then_destroy")
        try:
            self._run(["qm", "stop", str(vmid)], timeout=30.0, allow_failure=True)
        except subprocess.TimeoutExpired:
            pass
        try:
            self._run(
                ["qm", "destroy", str(vmid), "--skiplock", "--purge"],
                timeout=60.0,
                allow_failure=True,
            )
        except subprocess.TimeoutExpired:
            self.logger.warn(
                step="cleanup_destroy_vm_timeout",
                vmid=vmid,
                message="qm destroy timed out — VM may still exist",
                resolution=(
                    "run `qm destroy 900 --skiplock --purge` manually on the proxmox host"
                ),
            )

    def find_template_vmid(self, name: str) -> int | None:
        """Look up the VMID for a Proxmox template by name. Returns None if absent.

        Uses `qm list` (output: whitespace-padded columns). We avoid the
        JSON output because `qm list --json` requires a Proxmox >=7.2 with
        a new enough qm, which we don't want to require for the CLI.
        """
        result = self._run(
            ["qm", "list"], timeout=30.0, allow_failure=False
        )
        for line in result.stdout.splitlines():
            # Match e.g.: "       900  ubuntu-noble-template    stopped"
            # The first whitespace-padded column is the VMID, the second
            # is the name; subsequent columns (status, etc.) are ignored.
            m = re.match(r"^\s*(\d+)\s+(\S+)", line)
            if m and m.group(2) == name:
                return int(m.group(1))
        return None

    # ------------------------------------------------------------------
    # SS1 Phase 1 helpers — build Ubuntu Noble template (no Packer).
    # ------------------------------------------------------------------

    def create_template_shell(
        self, vmid: int, name: str, *, memory_mb: int,
        iso_path: str | None = None,
    ) -> None:
        """Create an empty VM shell at VMID with EFI + 32 GB scsi0 + Ubuntu Noble defaults.

        Phase 1 (tools/build_image/__init__.py) calls this from the
        PVE jump host to scaffold the golden VM, then imports the
        Ubuntu Noble cloud image into scsi0 and uses virt-customize
        to bake qemu-guest-agent + cloud-init + the per-cluster seed
        ISO. Cluster roots then `qm clone` this template for each
        cluster node.

        Layout (mirrors the canonical Proxmox + Ubuntu-Noble recipe):
        - bios=ovmf (UEFI boot)
        - machine=q35
        - efidisk0 on the operator-configured storage pool
        - scsi0 virtio-scsi-pci
        - agent enabled (qemu-guest-agent)
        - serial0 socket + vga serial0 (required to see Ubuntu early
          boot logs from the Proxmox web UI)

        Args:
            vmid: Proxmox VMID to create (typically 900).
            name: VM name (typically "ubuntu-noble-template").
            memory_mb: RAM in MiB for the template. The bpg/proxmox
              proxmox_cloned_vm in Phase 2 overrides this per-node.
            iso_path: optional `local:iso/<basename>` to attach at
              VM creation. When set, boot order is `ide2` first so the
              VM boots the ISO; the install flow will reboot from
              disk after cloud-init finishes.
        """
        args = [
            "qm", "create", str(vmid),
            "--name", name,
            "--memory", str(memory_mb),
            "--balloon", "0",
            "--cores", "2",
            "--sockets", "1",
            "--cpu", "host",
            "--machine", "q35",
            "--bios", "ovmf",
            "--ostype", "l26",
            "--agent", "enabled=1",
            "--scsihw", "virtio-scsi-pci",
            "--serial0", "socket",
            "--vga", "serial0",
            "--net0", "virtio,bridge=vnet0",
            # EFI disk on data1 (4 MB). The Ubuntu cloud image's
            # bootloader lives here after first-boot grub install.
            # efidisk0 occupies vm-950-disk-0 so the imported root
            # disk lands at vm-950-disk-1 deterministically.
            "--efidisk0", "data1:1,efitype=4m",
        ]
        if iso_path:
            args.extend(["--ide2", f"{iso_path},media=cdrom"])
            # Boot the ISO first; the install flow reboots from
            # scsi0 after the bootloader is written.
            args.extend(["--boot", "order=ide2"])
        else:
            # No ISO at template creation; start from disk.
            args.extend(["--boot", "order=scsi0"])
        self._run(args, timeout=60.0)

    def import_disk(
        self,
        vmid: int,
        *,
        source: str,
        storage: str,
        slot: str = "scsi0",
        format: str = "raw",
    ) -> None:
        """Import a local disk image (.raw / .img / .qcow2) into VMID at the
        given slot in one shot.

        Wrapper around `qm importdisk <vmid> <source> <storage>
        -format <fmt> -target-disk <slot>` which creates the disk in
        the storage pool AND attaches it to the requested slot in one
        call. This avoids the brittleness of guessing which
        `vm-950-disk-N` index Proxmox will assign next (it depends on
        every other disk already attached to the VM, including
        efidisk0).

        Args:
            vmid: target VMID.
            source: local path on the PVE host (not the operator host).
              The caller is responsible for getting the image there first
              (typically via scp or ssh curl).
            storage: PVE storage pool name (e.g. "data1").
            slot: disk slot; defaults to "scsi0".
            format: target disk format. qcow2 for the Ubuntu cloud
              image is the canonical setting.
        """
        result = self._run(
            [
                "qm", "importdisk", str(vmid), source, storage,
                "-format", format,
                "-target-disk", slot,
            ],
            timeout=600.0,
        )
        # qm prints "  successfully imported volume '<name>'" to stdout
        # when it succeeds; surface that for the audit log.
        self.logger.info(
            step="import_disk_ok",
            vmid=vmid,
            source=source,
            storage=storage,
            slot=slot,
            stdout_summary=_one_line(result.stdout),
        )

    def set_vm_config(self, vmid: int, args: list[str]) -> None:
        """Run `qm set <vmid> <args...>` to update VM configuration.

        Thin pass-through for ad-hoc updates (boot order, agent, etc.).
        Always idempotent on PVE side.
        """
        self._run(["qm", "set", str(vmid), *args], timeout=60.0)

    def start_vm(self, vmid: int) -> None:
        """Boot the VM."""
        self._run(["qm", "start", str(vmid)], timeout=60.0)

    def stop_vm(self, vmid: int, *, timeout: float = 60.0) -> None:
        """Graceful ACPI shutdown. Best-effort: callers should not raise on failure."""
        self._run(["qm", "shutdown", str(vmid)], timeout=timeout, allow_failure=True)

    def stop_vm_forcible(self, vmid: int, *, timeout: float = 15.0) -> None:
        """Hard-stop a VM (`qm stop` = immediate SIGKILL, no ACPI grace).

        Use this when the VM has ignored a graceful `qm shutdown` and
        you need to release the PVE lock so the next operation (e.g.
        `qm template`) can proceed. `qm stop` is intentionally short —
        it returns as soon as the QEMU process has exited, typically
        within a few seconds.
        """
        self._run(["qm", "stop", str(vmid)], timeout=timeout, allow_failure=True)

    def template_vm(self, vmid: int) -> None:
        """Convert a stopped VM into a Proxmox template."""
        self._run(["qm", "template", str(vmid)], timeout=60.0)

    def wait_for_vm_stopped(self, vmid: int, *, timeout_s: int = 120) -> bool:
        """Poll qm status until VM reports 'stopped' or timeout.

        Returns True if the VM reached 'stopped' within the timeout,
        False otherwise. Used by Phase 1 to wait for the disk-imported
        Ubuntu VM to come up, then for the graceful-shutdown to
        complete before converting to template.
        """
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = self._run(
                ["qm", "status", str(vmid)], timeout=10.0, allow_failure=True
            )
            if "stopped" in result.stdout:
                return True
            time.sleep(2.0)
        return False

    def list_storage(self, storage: str) -> str:
        """Return raw text of `pvesm status <storage>` (size + usage)."""
        result = self._run(
            ["pvesm", "status", storage], timeout=10.0, allow_failure=True
        )
        return result.stdout

    def ssh_run(
        self,
        command: str,
        *,
        timeout: float = 60.0,
        allow_failure: bool = False,
    ) -> RunResult:
        """Run a shell command on the PVE host via SSH.

        Used for one-off ops that don't have a `qm`/`pvesh` equivalent
        (e.g. `curl -fLO <url>` to download the Ubuntu cloud image, or
        `xz -d`).
        Routes through `ssh_target` so the operator host can reach the
        PVE host without needing qm/pvesh locally.

        `allow_failure=True` suppresses the WARNING log line that
        normally fires on a nonzero exit. The RunResult still has
        `returncode` set so callers can branch on it.

        NB: this method invokes `subprocess.run` directly, NOT `_run`,
        because `_run` would prepend the SSH prefix a second time
        (yielding `ssh ... ssh ...` double-wrap on the operator host).
        """
        import subprocess
        if self.ssh_target:
            ssh = [*self._ssh_prefix(), command]
        else:
            # Backwards-compat default for tests that don't set ssh_target.
            ssh = ["ssh", "-p", "6022", "-o", "BatchMode=yes", "root@kvm.bruj0.net", command]
        self.logger.info(
            step="pve_ssh_exec",
            remote=bool(self.ssh_target),
            command_preview=command[:80],
        )
        try:
            completed = subprocess.run(
                ssh,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            self.logger.error(
                step="pve_ssh_timeout",
                error=f"ssh subprocess exceeded {timeout}s",
                resolution="Investigate PVE host load or network latency.",
                command_preview=command[:80],
                timeout=timeout,
            )
            raise
        if completed.returncode != 0:
            if allow_failure:
                self.logger.info(
                    step="pve_ssh_nonzero_exit_allowed",
                    message=f"ssh exit {completed.returncode}",
                    returncode=completed.returncode,
                )
            else:
                self.logger.warn(
                    step="pve_ssh_nonzero_exit",
                    message=f"ssh exit {completed.returncode}",
                    returncode=completed.returncode,
                    stderr_summary=_one_line(completed.stderr),
                )
            raise PveError(
                command=ssh,
                returncode=completed.returncode,
                stderr=completed.stderr,
            )
        return self.RunResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def ssh_run_stdin(
        self,
        command: str,
        stdin_payload: str | bytes,
        *,
        timeout: float = 60.0,
    ) -> RunResult:
        """Like `ssh_run` but feeds `stdin_payload` into the remote stdin.

        Used to stage files on the PVE host without needing scp:
        `ssh_run_stdin("cat > /tmp/foo.yaml", yaml_bytes)`.

        `stdin_payload` is sent verbatim — callers are responsible for
        closing heredocs / quoting characters that the remote shell
        would otherwise expand.
        """
        import subprocess
        if self.ssh_target:
            ssh = [*self._ssh_prefix(), command]
        else:
            ssh = [
                "ssh", "-p", "6022", "-o", "BatchMode=yes",
                "root@kvm.bruj0.net", command,
            ]
        if isinstance(stdin_payload, str):
            stdin_bytes = stdin_payload.encode("utf-8")
        else:
            stdin_bytes = stdin_payload
        self.logger.info(
            step="pve_ssh_stdin_exec",
            remote=bool(self.ssh_target),
            command_preview=command[:80],
            payload_bytes=len(stdin_bytes),
        )
        try:
            completed = subprocess.run(
                ssh,
                check=False,
                input=stdin_bytes,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            self.logger.error(
                step="pve_ssh_stdin_timeout",
                error=f"ssh stdin exec exceeded {timeout}s",
                resolution=(
                    "Investigate PVE host load or network latency;"
                    " ssh with stdin payload can hang on large payloads"
                    " (image uploads, ISO seeds)."
                ),
                command_preview=command[:80],
                timeout=timeout,
            )
            raise
        if completed.returncode != 0:
            stderr_text = (
                completed.stderr.decode("utf-8", errors="replace")
                if isinstance(completed.stderr, bytes)
                else completed.stderr
            )
            stdout_text = (
                completed.stdout.decode("utf-8", errors="replace")
                if isinstance(completed.stdout, bytes)
                else completed.stdout
            )
            self.logger.warn(
                step="pve_ssh_stdin_nonzero_exit",
                message=f"ssh exit {completed.returncode}",
                returncode=completed.returncode,
                stderr_summary=_one_line(stderr_text),
            )
            raise PveError(
                command=ssh,
                returncode=completed.returncode,
                stderr=stderr_text,
            )
        return self.RunResult(
            returncode=completed.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
        )


def _one_line(text: str, *, limit: int = 240) -> str:
    """Backwards-compat shim: collapse multi-line text for log readability.

    Implementation now lives in :mod:`tools.lib.log` (single source of
    truth). This thin wrapper exists so existing imports keep working
    until the call sites are migrated.
    """
    from .log import _one_line as _impl
    return _impl(text, limit=limit)
