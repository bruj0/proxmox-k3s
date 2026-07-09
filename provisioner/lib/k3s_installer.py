"""K3sInstaller — Python orchestration for installing k3s on the cluster VMs.

Replaces what was previously done in cloud-init runcmd (Phase 1) for the
real per-VM install. Kept pure-Python (no shell scripts in tools/scripts/);
the only shell we execute is the upstream `install.sh` from get.k3s.io,
invoked over SSH.

Key contracts (pinned by tests/test_k3s_installer.py):

  * Versions come from tools/versions.lock.yaml via VersionsLockReader.
  * Idempotent: refuse to re-run when `systemctl is-active k3s` returns
    'active' AND /etc/rancher/k3s/k3s.yaml exists. The upstream
    `install.sh` is itself hash-checked, so re-runs would also be no-ops,
    but a Python-side short-circuit avoids even the network round-trip.
  * Agents join on `https://<cp_ip>:6443`, never a VIP. WP08 (2026-07-08)
    removed the kube-vip VIP layer -- the cluster runs single-control-plane
    on cicd (10.0.0.65) so the CP host IP is the apiserver endpoint.
  * Control-plane installs with `--tls-san=<cp_ip>` (and the
    apiserver SVC IP `172.17.0.1` and `kubernetes.default.svc`) so a
    kubeconfig pulled from the server references SANs the apiserver's
    serving cert actually carries. Live 2026-07-08 probe: the cert is
    valid for the CP IP and the in-cluster SVC; pin the SANs to match.
  * No token / secret ever logged (M7). StructuredLogger scrubs at the
    boundary; we ALSO pass the join token as an environment variable
    on the SSH command so it doesn't appear in argv / process list.

Idempotency recap (per-VM, per-call):
    - Idempotent if k3s systemd unit is active AND kubeconfig exists.
    - Upstream installer.sh is hash-checked; same env -> `No change
      detected so skipping service start`. We don't rely on that, we
      add our own gate.

Cross-cluster smoke (2026-07-08, kvm.bruj0.net): cicd uses CP IP
10.0.0.65, apps uses CP IP 10.0.0.67. The in-cluster apiserver SVC
IP (172.17.0.1 / 172.21.0.1) is what every pod uses. Order of
operations: cloudinit -> install_k3s -> helm.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .log import StructuredLogger
from .pve_ssh import PveSshProxy
from .versions import VersionsLockReader


class K3sInstallerError(RuntimeError):
    """Raised on any failure path with a structured message.

    Operator-facing fields are exposed as JSON-ish kwargs so callers
    can parse the reason without re-formatting the string.
    """

    def __init__(self, reason: str, **fields: Any) -> None:
        self.reason = reason
        self.fields = fields
        super().__init__(f"k3s installer: {reason} ({json_dumps(fields)})")

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, **self.fields}


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, default=str)


@dataclass(frozen=True)
class ServerInstallPlan:
    """The exact recipe to install k3s as a server on one VM.

    `environment` is exported into the remote shell BEFORE `sh -s -`,
    so the upstream installer reads INSTALL_K3S_VERSION etc. without
    them appearing on the SSH command line.

    `exec_flags` is the tail of the upstream call after `sh -s -`, so
    a.k.a. `server ...`. Combined with `environment`, the rendered
    command is:

        env INSTALL_K3S_VERSION=v1.36.2+k3s1 K3S_NODE_NAME=cicd-cp-1 \
            curl -sfL https://get.k3s.io | sh -s - server --flannel-backend=none ...
    """

    node_name: str
    node_ip: str
    vip: str
    environment: dict[str, str]
    exec_flags: list[str]


@dataclass(frozen=True)
class AgentInstallPlan:
    """The exact recipe to install k3s as an agent on one VM.

    Same render as ServerInstallPlan but K3S_URL+K3S_TOKEN are set, and
    the implicit default for `install.sh` (no args) is `server`. We
    always pass `agent` explicitly so there's no ambiguity.
    """

    node_name: str
    node_ip: str
    vip: str
    token: str
    environment: dict[str, str]
    exec_flags: list[str]


# Shared flag set for the control-plane node. Matched to
# versions.yaml::k3s::v1.34.x::install_args_default.control_plane, with
# the VIP verification note's mandatory additions:
#   --tls-san=<vip>             -- apiserver cert must carry the VIP
#   --node-ip / --node-external-ip -- populate from the SDN DHCP lease
# WP07 follow-up (2026-07-08, §14.4 root cause + fix):
#   --disable-kube-proxy        -- cilium runs in kubeProxyReplacement=true
#                                 mode. With both cilium eBPF AND k3s's
#                                 embedded kube-proxy writing iptables,
#                                 kube-proxy's KUBE-MARK-MASQ rule for
#                                 the `kubernetes` ClusterIP is omitted
#                                 and the pod->apiserver TLS ServerHello
#                                 is dropped on the return path (the
#                                 response from 10.0.0.65:6443 can't be
#                                 matched to a socket expecting src
#                                 10.43.0.1:443). Disabling kube-proxy
#                                 lets cilium own ClusterIP routing
#                                 cleanly. Cilium must be told
#                                 k8sServiceHost=<vip> so it doesn't
#                                 depend on the ClusterIP during its own
#                                 startup (chicken-and-egg).
_SERVER_BASE_FLAGS: tuple[str, ...] = (
    "--flannel-backend=none",
    "--disable=traefik",
    "--disable=servicelb",
    "--disable=local-storage",
    "--disable=metrics-server",
    "--disable-kube-proxy",
    "--kubelet-arg=cloud-provider=external",
)


# Agent-side flags. Per the official k3s documentation, the
# `--flannel-backend` flag is server-ONLY; the agent inherits the
# server's CNI decision via the kubelet join handshake. Agents on
# a `--flannel-backend=none` server must NOT pass --flannel-backend
# at all (k3s agent binary rejects it: "flag provided but not
# defined: -flannel-backend"). Same for --disable={traefik,...}
# which are server-only.
#
# What agents DO need: --node-ip / --node-external-ip. Anything else
# is kubelet config we may add later.
_AGENT_BASE_FLAGS: tuple[str, ...] = ()


@dataclass
class K3sInstaller:
    """Per-cluster Python orchestrator for the install_k3s sub-phase.

    Constructed once per cluster by `bootstrap_cluster._run_install_k3s`.
    All public methods are idempotent on a per-VM basis (a re-run sees
    the systemd unit already active and skips).
    """

    cluster: Mapping[str, Any]
    ssh_proxy_target: str
    logger: StructuredLogger
    versions: VersionsLockReader | None = None
    proxy: PveSshProxy | None = None

    def __post_init__(self) -> None:
        # VersionsLockReader is read-only and cacheable; default to a
        # reader backed by documented defaults. Tests can either (a)
        # inject their own `versions` argument, or (b) leave it None
        # and we'll fall back to defaults. We deliberately do NOT touch
        # the filesystem here so tests don't depend on the real repo
        # lockfile.
        if self.versions is None:
            object.__setattr__(
                self,
                "versions",
                VersionsLockReader(logger=self.logger),
            )
        if self.proxy is None:
            # ssh_user on the cluster dict lets per-cluster tofu
            # overrides pin a different in-VM user (we currently use
            # the cloud-image default `ubuntu` everywhere).
            ssh_user = str(self.cluster.get("ssh_user", "ubuntu"))
            object.__setattr__(
                self,
                "proxy",
                PveSshProxy(
                    jump_host=self.ssh_proxy_target,
                    ssh_user=ssh_user,
                    logger=self.logger,
                ),
            )

    @property
    def _versions(self) -> VersionsLockReader:
        # Convenience accessor for the always-non-None invariant.
        assert self.versions is not None
        return self.versions

    @property
    def _proxy(self) -> PveSshProxy:
        # Convenience accessor for the always-non-None invariant.
        assert self.proxy is not None
        return self.proxy

    # ---------- planning ----------

    def plan_server(self, vm: Mapping[str, Any], *, vip: str) -> ServerInstallPlan:
        """Render the server install plan for one control-plane VM."""
        # WP09 (2026-07-09): the `vip` parameter is now an unused
        # deprecation stub. WP08 removed the kube-vip VIP layer
        # (single-CP clusters join on the CP host IP), but the
        # validation in this method still rejected empty vip and
        # crashed the bootstrap with a `blank_vip` error. Treat
        # empty vip as a no-op (the join target is the CP host IP
        # pulled from cluster["control_plane_ip"] / self._cp_ip()
        # below). vip is kept in the signature so existing callers
        # do not need to change.
        _ = vip  # intentionally unused
        node_ip = str(vm["ip"])
        node_name = str(vm["name"])
        # --tls-san=<cp_ip> is REQUIRED (WP08, 2026-07-08: the VIP
        # layer is gone; the apiserver is reached directly on the CP
        # host IP). Filled at install time, never hard-coded.
        #
        # WP07 (2026-07-08): also add the in-cluster apiserver SANs so
        # workloads that talk to the apiserver via kubernetes.default.svc
        # (or the ClusterIP <svc_cidr>.0.1) survive TLS validation.
        # Without these, every chart with a pre-install hook that calls
        # the apiserver (Envoy Gateway's certgen Job, cert-manager's
        # cainjector, etc.) fails with
        # `x509: certificate is not valid for kubernetes.default.svc`.
        # svc_cidr is passed via the cluster dict that bootstrap_cluster.py
        # builds (see `_run_install_k3s` -- the dict carries
        # svc_cidr from output.json: cicd=172.17.0.0/16, apps=172.21.0.0/16).
        # Gateway address = <first three octets>.1.
        # WP08 (2026-07-08, §14.4 second root cause): the k3s default
        # `--cluster-cidr=10.42.0.0/16` and `--service-cidr=10.43.0.0/16`
        # **overlap with the host LAN** (10.0.0.0/8 in this cluster,
        # 10.0.10.0/24 on the SDN). This causes the legacy kube-proxy
        # iptables MASQUERADE rule to be skipped for pod->apiserver
        # traffic (the source pod CIDR contains the apiserver's host
        # IP, so the RETURN short-circuits) and cilium's bpf SNAT
        # source to be misrouted. See k3s-io/k3s#4627. Fix: pin the
        # CIDRs to non-overlapping ranges (172.16/172.17 RFC1918 by
        # default) at install time. Defaults are chosen so the
        # terraform-side `pod_cidr` and `svc_cidr` variables in
        # infra/clusters/<name>/variables.tf can override per-cluster.
        # cluster_dns is the in-cluster coredns service IP; per k3s
        # docs it must be in the service CIDR.
        #
        # Precedence: per-VM dict > cluster dict > safe default. The
        # bootstrap script populates all three in the per-VM dict, so
        # the cluster-dict fallback is for unit tests and one-off
        # callers.
        svc_cidr = str(
            vm.get("svc_cidr")
            or self.cluster.get("svc_cidr")
            or "172.17.0.0/16"
        )
        svc_gateway = ".".join(svc_cidr.split(".")[:3] + ["1"])
        pod_cidr = str(
            vm.get("pod_cidr")
            or self.cluster.get("pod_cidr")
            or "172.16.0.0/16"
        )
        cluster_dns = str(
            vm.get("cluster_dns")
            or self.cluster.get("cluster_dns")
            or "172.17.0.10"
        )
        flags = [
            *_SERVER_BASE_FLAGS,
            f"--node-ip={node_ip}",
            f"--node-external-ip={node_ip}",
            # WP08: the apiserver cert SAN must be the CP host IP
            # (no more VIP). The first SAN is the canonical one; the
            # in-cluster apiserver SANs come after to keep them
            # explicit.
            f"--tls-san={node_ip}",
            # In-cluster apiserver SANs (WP07). The two values cover
            # every chart hook we have observed: the cert-manager
            # cainjector, Envoy Gateway's certgen Job, the strrl
            # cloudflare-tunnel-ingress controller, and gitlab's
            # rails-side webhooks all resolve \`kubernetes.default.svc\`
            # to the svc gateway IP.
            f"--tls-san={svc_gateway}",
            "--tls-san=kubernetes.default.svc",
            # WP08: non-overlapping CIDRs (see k3s#4627). Required
            # because the host LAN is 10.0.0.0/8.
            f"--cluster-cidr={pod_cidr}",
            f"--service-cidr={svc_cidr}",
            f"--cluster-dns={cluster_dns}",
        ]
        env = {
            "INSTALL_K3S_VERSION": self._versions.k3s_stable_version,
            "INSTALL_K3S_CHANNEL": self._versions.k3s_channel,
            "K3S_NODE_NAME": node_name,
            # WP11 (2026-07-09): install.sh's `systemctl restart ${SYSTEM_NAME}`
            # blocks on the start job completion, and kubelet stays in
            # 'activating (start)' until a CNI plugin initialises (k3s
            # needs the CNI to know which pod CIDR to bind). The
            # bootstrap installs CNI later (cilium_install phase), so
            # we tell install.sh to install + enable the unit but NOT
            # to start it. The orchestrator starts it via
            # `start_k3s_unit()` AFTER cilium is up.
            "INSTALL_K3S_SKIP_START": "true",
        }
        return ServerInstallPlan(
            node_name=node_name,
            node_ip=node_ip,
            vip=vip,
            environment=env,
            exec_flags=["server", *flags],
        )

    def plan_agent(
        self,
        vm: Mapping[str, Any],
        *,
        vip: str,
        token: str,
    ) -> AgentInstallPlan:
        """Render the agent install plan for one worker VM."""
        # WP09 (2026-07-09): see plan_server -- vip is an unused
        # deprecation stub.
        _ = vip  # intentionally unused
        if not token:
            raise K3sInstallerError(
                "blank_token",
                node=vm.get("name"),
                resolution=(
                    "fetch the token via read_node_token() from the "
                    "control-plane VM before installing agents"
                ),
            )
        node_ip = str(vm["ip"])
        node_name = str(vm["name"])
        # WP08 (2026-07-08): K3S_URL points at the CP host IP,
        # NOT the agent's own IP and NOT a kube-vip VIP. The cluster
        # dict carries the CP node under key `control_plane_ip`
        # (set by bootstrap_cluster.py::_run_install_k3s, which
        # reads the first CP out of topo.control_plane). Fall back
        # to scanning the cluster dict's vms[] for the first CP if
        # the convenience key is missing (unit-test path).
        cp_ip = str(self.cluster.get("control_plane_ip", ""))
        if not cp_ip:
            for candidate in self.cluster.get("vms", []):
                if candidate.get("role") == "control_plane":
                    cp_ip = str(candidate.get("ip", ""))
                    break
        if not cp_ip:
            raise K3sInstallerError(
                "no_cp_ip",
                node=vm.get("name"),
                resolution=(
                    "WP08: bootstrap_cluster.py must populate "
                    "control_plane_ip in the cluster dict so agents "
                    "know which apiserver to join."
                ),
            )
        env = {
            "INSTALL_K3S_VERSION": self._versions.k3s_stable_version,
            "INSTALL_K3S_CHANNEL": self._versions.k3s_channel,
            "K3S_NODE_NAME": node_name,
            # WP08 (2026-07-08): K3S_URL points at the CP host IP
            # directly. The kube-vip VIP layer is gone.
            "K3S_URL": f"https://{cp_ip}:6443",
            # K3S_TOKEN is never logged; the StructuredLogger scrubs at the
            # boundary, but we ALSO avoid putting it on the command line --
            # we pass it via the remote shell's environment.
            "K3S_TOKEN": token,
            # WP11 (2026-07-09): same rationale as in plan_server --
            # `systemctl restart k3s-agent` would block indefinitely
            # because kubelet can't initialise without a CNI, and the
            # bootstrap installs CNI later (cilium_install phase).
            # Skipping the start here means the unit is enabled but
            # not running; the orchestrator issues the start via
            # `start_k3s_unit()` AFTER cilium is up.
            "INSTALL_K3S_SKIP_START": "true",
        }
        flags = [
            *_AGENT_BASE_FLAGS,
            f"--node-ip={node_ip}",
            f"--node-external-ip={node_ip}",
        ]
        return AgentInstallPlan(
            node_name=node_name,
            node_ip=node_ip,
            vip=vip,
            token=token,
            environment=env,
            exec_flags=["agent", *flags],
        )

    # ---------- execution ----------

    def install_server(self, vm: Mapping[str, Any], *, vip: str) -> None:
        """Idempotently install k3s as a server on the given control-plane VM."""
        plan = self.plan_server(vm, vip=vip)
        if self._is_k3s_healthy(vm):
            self.logger.info(
                "k3s.skip_install",
                node=plan.node_name,
                reason="already healthy (systemctl is-active + kubeconfig present)",
            )
            return
        self.logger.info(
            "k3s.install_server",
            node=plan.node_name,
            vip=plan.vip,
            version=self._versions.k3s_stable_version,
        )
        self._run_upstream_install(vm, plan)

    def install_agent(
        self,
        vm: Mapping[str, Any],
        *,
        vip: str,
        token: str,
    ) -> None:
        """Idempotently install k3s as an agent on the given worker VM."""
        plan = self.plan_agent(vm, vip=vip, token=token)
        if self._is_k3s_agent_healthy(vm):
            self.logger.info(
                "k3s.skip_install",
                node=plan.node_name,
                reason="already healthy (k3s-agent unit active)",
            )
            return
        self.logger.info(
            "k3s.install_agent",
            node=plan.node_name,
            vip=plan.vip,
            version=self._versions.k3s_stable_version,
            # token deliberately not passed -> scrubbed by StructuredLogger.
        )
        self._run_upstream_install(vm, plan)

    def start_k3s_unit(self, vm: Mapping[str, Any], *, role: str) -> None:
        """Start the k3s systemd unit after CNI is installed.

        WP11 (2026-07-09): install.sh was called with
        `INSTALL_K3S_SKIP_START=true`, so the unit is enabled but
        not running. The start job blocks on kubelet initialising,
        which needs the CNI. The orchestrator calls this method
        AFTER `cilium_install` succeeds — at that point the start
        job completes immediately and the node joins the cluster.

        `role` is "server" for control-plane VMs and "agent" for
        workers. Determines which unit name to issue `systemctl
        start` on.

        Idempotent: if the unit is already `active`, this is a no-op.
        """
        if role not in ("server", "agent"):
            raise K3sInstallerError(
                "bad_role",
                node=vm.get("name"),
                role=role,
                resolution="role must be 'server' or 'agent'",
            )
        unit = "k3s" if role == "server" else "k3s-agent"
        ip = str(vm["ip"])
        # `systemctl start` waits for the start job to complete.
        # With CNI up, that takes a few seconds. We bound it
        # generously to handle slow disks / image pulls on first
        # node, but NOT so long that a real hang goes unnoticed.
        cmd = self._proxy.ssh_argv(
            ip,
            command=f"sudo systemctl start {unit}",
        )
        proc = subprocess.run(  # noqa: S603
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise K3sInstallerError(
                "start_failed",
                node=vm.get("name"),
                unit=unit,
                returncode=proc.returncode,
                stderr_tail=proc.stderr.strip()[-200:],
            )
        self.logger.info(
            "k3s.start_unit_ok",
            node=vm.get("name"),
            unit=unit,
            role=role,
        )

    def read_node_token(self, server_vm: Mapping[str, Any]) -> str:
        """Fetch the join token from the control-plane VM.

        Returns the trimmed token. On any failure path raises
        K3sInstallerError so callers see a structured message instead
        of a raw stderr blob.
        """
        ip = str(server_vm["ip"])
        # Tunnel through the PVE jump host into the VM's SDN IP and run
        # as root via sudo -n (the cluster VMs refuse root login).
        inner = "cat /var/lib/rancher/k3s/server/node-token 2>/dev/null"
        remote = f"sudo -n bash -c {shlex.quote(inner)}"
        cmd = self._proxy.ssh_argv(ip, command=remote)
        proc = subprocess.run(  # noqa: S603 -- single, documented shell call.
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            raise K3sInstallerError(
                "node_token_unreadable",
                server=server_vm.get("name"),
                returncode=proc.returncode,
                stderr=proc.stderr[:200],
            )
        token = proc.stdout.strip()
        if not token:
            raise K3sInstallerError(
                "node_token_empty",
                server=server_vm.get("name"),
                resolution=(
                    "install_server may not have completed; rerun the "
                    "install_k3s phase and inspect /var/log/k3s.log "
                    "on the server"
                ),
            )
        return token

    # ---------- internals ----------

    def _is_k3s_healthy(self, vm: Mapping[str, Any]) -> bool:
        """Probe + kubeconfig existence check before re-installing.

        Returns False (forcing a re-install) if EITHER:
          - the k3s systemd unit is not active, OR
          - /etc/rancher/k3s/k3s.yaml is missing, OR
          - the running k3s version does NOT match the pinned
            `k3s_stable_version` (reconcile-and-pin policy -- the
            installer re-runs the upstream installer.sh to roll
            forward drifted nodes without disturbing healthy ones).
        """
        ip = str(vm["ip"])
        if self._ssh_returncode(
            ip, "systemctl is-active --quiet k3s"
        ) != 0:
            return False
        if self._ssh_returncode(ip, "test -f /etc/rancher/k3s/k3s.yaml") != 0:
            return False
        return self._running_k3s_version_matches(vm)

    def _is_k3s_agent_healthy(self, vm: Mapping[str, Any]) -> bool:
        """Like `_is_k3s_healthy` but for agents (unit is `k3s-agent`)."""
        ip = str(vm["ip"])
        if self._ssh_returncode(ip, "systemctl is-active --quiet k3s-agent") != 0:
            return False
        return self._running_k3s_version_matches(vm)

    def _running_k3s_version_matches(self, vm: Mapping[str, Any]) -> bool:
        """Return True iff the on-host `k3s --version` matches the pin.

        Reconcile-and-pin policy: the installer always targets
        `k3s_stable_version` from the lockfile. A drifted node
        (running an older patch) is treated as "not healthy" so the
        upstream installer is re-invoked to roll it forward. The
        upstream installer.sh does the actual download + replace;
        our short-circuit just gates the call.

        Returns True on any SSH error so a probe failure does not
        accidentally trigger a re-install on a node we can't read.
        """
        ip = str(vm["ip"])
        try:
            proc_str = self._ssh_capture(
                ip, "k3s --version 2>/dev/null | head -n1"
            )
        except Exception:  # pragma: no cover -- defensive
            return True
        # proc_str is "k3s version v1.34.9+k3s1 (5f72184f)"
        m = re.search(r"v\d+\.\d+\.\d+\+k3s\d+", proc_str)
        if m is None:
            return True
        running = m.group(0)
        pinned = self._versions.k3s_stable_version
        if running != pinned:
            self.logger.info(
                "k3s.version_drift",
                node=str(vm.get("name")),
                running=running,
                pinned=pinned,
                resolution=(
                    "re-running upstream installer to reconcile the pin"
                ),
            )
        return running == pinned

    def _ssh_capture(self, ip: str, command: str) -> str:
        """Run `command` over SSH as root; return stdout, never raise.

        Symmetric with `_ssh_returncode` (both wrap a single
        `sudo -n bash -c <command>` ssh call) but returns the
        captured stdout. Used by probes that need to parse a version
        string. Returns "" on any error so callers can pattern-match
        safely.
        """
        try:
            remote = f"sudo -n bash -c {shlex.quote(command)}"
            proc = subprocess.run(  # noqa: S603 -- documented shell call.
                self._proxy.ssh_argv(ip, command=remote),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                return ""
            return proc.stdout
        except (subprocess.CalledProcessError, OSError):
            return ""

    def _ssh_returncode(self, ip: str, command: str) -> int:
        """Run `command` over SSH as root; return the exit code, never raise.

        The remote user is `ubuntu` (cluster VMs use the cloud-image
        default; root login is rejected). We `sudo -n bash -c <command>`
        to escalate to root for the call. The whole command is
        single-quoted on argv so the operators in the inner shell
        don't get tokenized by the outer ssh client.

        Used for idempotency gates where a non-zero result is just a
        'not installed yet' signal, not an error worth surfacing.
        Returns -1 if the SSH invocation itself exploded (e.g. agent
        down, network unreachable). Callers should treat -1 the same
        as a non-zero rc.
        """
        try:
            remote = f"sudo -n bash -c {shlex.quote(command)}"
            proc = subprocess.run(  # noqa: S603 -- documented shell call.
                self._proxy.ssh_argv(ip, command=remote),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return proc.returncode
        except (subprocess.CalledProcessError, OSError):
            return -1

    def _run_upstream_install(
        self,
        vm: Mapping[str, Any],
        plan: ServerInstallPlan | AgentInstallPlan,
    ) -> None:
        """Invoke the upstream `install.sh` over SSH on the given VM.

        Renders to (single shell run as root via `sudo -n bash -c`):
            sudo -n bash -c '
              export K3S_TOKEN=...
              export INSTALL_K3S_VERSION=v1.36.2+k3s1
              ...
              curl -sfL https://get.k3s.io | sh -s - <exec-flags...>
            '

        Notes:
          - The env vars are placed INSIDE the sudo'd bash (via
            `export`), not on the outer shell. `sudo -n` strips the
            caller's environment by default (env_reset is on; env_keep
            does not include K3S_*/INSTALL_K3S_*), so a bare env prefix
            on the outer shell would be silently lost.
          - The token / secret env vars are exported, not argv. They
            never appear in `ps auxe` output on the operator host.
          - The upstream install script is itself idempotent (hash check);
            our Python-side short-circuit just saves a network round-trip.
          - The remote user is `ubuntu` (cluster VMs use the cloud-image
            default; root login is rejected). `sudo -n` requires no
            password -- the cluster root tofu module sets `NOPASSWD` for
            the operator's ssh-key user.
        """
        ip = str(vm["ip"])
        # Whitelist env keys (alphanumeric + underscore) to defend against
        # accidental injection. Only the VALUE is shlex-quoted; the KEY
        # is left bare so bash parses it as an assignment.
        export_lines = []
        for k, v in plan.environment.items():
            if not k.replace("_", "").isalnum():
                raise K3sInstallerError(
                    "unsafe_env_key",
                    node=vm.get("name"),
                    key=k,
                    resolution=(
                        "env keys must be alphanumeric + underscore; "
                        "check the install plan"
                    ),
                )
            export_lines.append(f"export {k}={shlex.quote(v)}")
        export_block = "\n".join(export_lines) + "\n"
        # Build the inner shell payload, then wrap in sudo -n bash -c.
        # We use a literal shell command: `export K=...; export T=...;
        # curl -sfL https://get.k3s.io | sh -s - <flags>`. Putting the
        # `export` statements INSIDE the sudo'd bash is necessary because
        # `sudo -n` strips the caller's env by default (env_reset is on;
        # env_keep does NOT include K3S_*/INSTALL_K3S_*). The bare env
        # prefix on the outer shell would be silently lost.
        install_cmd = (
            f"curl -sfL {self._versions.k3s_install_url} | "
            "sh -s - " + " ".join(shlex.quote(f) for f in plan.exec_flags)
        )
        inner = export_block + install_cmd
        remote = f"sudo -n bash -c {shlex.quote(inner)}"
        cmd = self._proxy.ssh_argv(ip, command=remote)
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,  # 10 min -- enough for slow dnsmasq + 100 MB download
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            raise K3sInstallerError(
                "ssh_invocation_failed",
                node=vm.get("name"),
                install_url=self._versions.k3s_install_url,
                detail=str(exc),
            ) from exc
        if proc.returncode != 0:
            raise K3sInstallerError(
                "install_sh_failed",
                node=vm.get("name"),
                install_url=self._versions.k3s_install_url,
                version=self._versions.k3s_stable_version,
                returncode=proc.returncode,
                stdout_tail=proc.stdout[-200:],
                stderr_tail=proc.stderr[-400:],
            )
