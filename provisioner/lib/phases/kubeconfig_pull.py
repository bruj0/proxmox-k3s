"""kubeconfig_pull phase — fetch + tunnel the admin kubeconfig.

The cluster VMs are on the PVE SDN (`10.0.0.0/8`). The operator
host is on a different LAN (`10.0.10.0/24`). Direct kubectl/helm
calls from the operator hit a routing black hole.

We solve this with a local-port-forward through the PVE proxy:

  operator 127.0.0.1:<local_port>  ──ssh──>  PVE 10.0.0.1:6022  ──tcp──>  CP 127.0.0.1:6443

The fetched kubeconfig is rewritten so its `server:` URL points
at `https://127.0.0.1:<local_port>`. Every subsequent kubectl /
helm call from the bootstrap (and from the operator's own
terminal if they `export KUBECONFIG=.../kubeconfig.yaml`)
transparently reaches the in-cluster apiserver.

The forwarded port is tracked on the container (`ctx.apiserver_tunnel`)
so subsequent phases (`cilium_install`, `helm_releases`,
`gateway_crds`, `topology_writer`) reuse the same tunnel — opening
multiple SSH tunnels per phase would consume proxy slots.
"""

from __future__ import annotations

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class KubeconfigPullPhase(Phase):
    """Open an apiserver tunnel, fetch + rewrite kubeconfig."""

    name = "kubeconfig_pull"
    requires = ("apiserver_ready",)

    def run(self, ctx: Container) -> PhaseResult:
        topo = ctx.upstream_topology
        if topo is None or not topo.control_plane:
            raise BootstrapError("kubeconfig_pull", {"reason": "no control plane"})

        cp = topo.control_plane[0]
        # RemoteExecutor's PveSshProxy adds `ssh_user@` itself.
        target = cp.ip

        # 1. Fetch the raw kubeconfig from the CP (in-cluster server: points at 127.0.0.1).
        result = ctx.remote.run(
            target,
            "sudo cat /etc/rancher/k3s/k3s.yaml",
            check=False,
            timeout=15.0,
        )
        if result.exit_code != 0 or "apiVersion" not in result.stdout:
            raise BootstrapError(
                "kubeconfig_pull",
                {
                    "reason": "could not read /etc/rancher/k3s/k3s.yaml",
                    "stderr": result.stderr.strip(),
                },
            )

        # 2. Open an SSH port-forward from 127.0.0.1:<free> on the
        #    operator -> CP's 127.0.0.1:6443 (k3s binds loopback).
        forward = ctx.open_apiserver_tunnel(cp.ip)
        local_port = forward.local_port
        ctx.logger.info(step="apiserver_tunnel_opened", local_port=local_port, cp_ip=cp.ip)

        # 3. Rewrite the kubeconfig's server: URL to point at the
        #    tunnel's local port. Re-running the bootstrap picks a
        #    fresh ephemeral port each time, so we always rewrite.
        rewritten = result.stdout.replace(
            "server: https://127.0.0.1:6443",
            f"server: https://127.0.0.1:{local_port}",
        )
        rewritten = rewritten.replace(
            f"server: https://{cp.ip}:6443",
            f"server: https://127.0.0.1:{local_port}",
        )

        out_path = ctx.cluster_dir / "kubeconfig.yaml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rewritten)
        ctx.logger.info(step="kubeconfig_pulled", path=str(out_path), local_port=local_port)
        return PhaseResult.make_done("kubeconfig_pull", path=str(out_path), local_port=local_port)