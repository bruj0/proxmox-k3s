"""apiserver_ready phase — verify the k3s server is up.

This phase runs BEFORE kubeconfig_pull (which writes the operator's
kubeconfig and opens the apiserver tunnel). So we can't use
`kubectl get --raw /healthz` here — that requires a kubeconfig +
tunnel that don't exist yet. Instead we poll the k3s process over
SSH and check that the apiserver is listening on 6443.

Once `apiserver_ready` returns, the next phase (`kubeconfig_pull`)
opens the tunnel and writes the kubeconfig so downstream phases
(`cilium_install`, `helm_releases`, `gateway_crds`) can use
`kubectl` / `helm` / `cilium` from the operator host.
"""

from __future__ import annotations

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class ApiserverReadyPhase(Phase):
    """Poll k3s.service over SSH until the apiserver is listening on :6443."""

    name = "apiserver_ready"
    requires = ("install_k3s",)

    def run(self, ctx: Container) -> PhaseResult:
        topo = ctx.upstream_topology
        if topo is None or not topo.control_plane:
            raise BootstrapError("apiserver_ready", {"reason": "no control plane"})

        cp = topo.control_plane[0]
        # RemoteExecutor's PveSshProxy adds `ssh_user@` itself.
        target = cp.ip

        # 1. service must be active
        rc = ctx.remote.run(target, "systemctl is-active k3s", check=False, timeout=10.0)
        if rc.exit_code != 0 or rc.stdout.strip() != "active":
            raise BootstrapError(
                "apiserver_ready",
                {
                    "reason": "k3s service is not active",
                    "stdout": rc.stdout.strip(),
                    "stderr": rc.stderr.strip(),
                },
            )

        # 2. kube-apiserver must be listening on 6443
        rc = ctx.remote.run(
            target,
            "sudo ss -tlnp | grep -E ':6443'",
            check=False,
            timeout=10.0,
        )
        if rc.exit_code != 0 or ":6443" not in rc.stdout:
            raise BootstrapError(
                "apiserver_ready",
                {
                    "reason": "k3s not listening on :6443 yet",
                    "ss": rc.stdout.strip(),
                },
            )

        # 3. apiserver self-check via loopback (cp -> itself).
        #    Don't fail hard — apiserver takes a few seconds to start
        #    serving after the port opens. kubeconfig_pull will
        #    catch this via the tunnel.
        rc = ctx.remote.run(
            target,
            "curl -sf -k https://127.0.0.1:6443/healthz || echo DOWN",
            check=False,
            timeout=10.0,
        )
        if "ok" not in rc.stdout.lower():
            ctx.logger.warn(
                step="apiserver_healthz_deferred",
                message="apiserver port open but /healthz not yet ok",
                stdout=rc.stdout.strip(),
            )
        else:
            ctx.logger.info(step="apiserver_healthz_ok")

        ctx.logger.info(step="apiserver_ready_ok", cp_ip=cp.ip)
        return PhaseResult.make_done("apiserver_ready", cp_ip=cp.ip)