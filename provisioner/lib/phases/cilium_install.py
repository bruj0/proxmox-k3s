"""cilium_install phase — install cilium CNI (kube-proxy replacement).

Uses the cilium CLI (`cilium install --version 1.19.5`) which is
the documented install path for cilium-on-k3s. We run the CLI
on the operator host (not via SSH) because the cilium CLI needs
direct apiserver access (via the kubeconfig the kubeconfig_pull
phase wrote to ~/.kube/config).

The chart values (k8sServiceHost, cgroup.hostRoot, operator.replicas)
live in `values/cilium.yaml` and are passed via --set.
"""

from __future__ import annotations

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class CiliumInstallPhase(Phase):
    """Run `cilium install --version <pinned> --set ...` on the operator host."""

    name = "cilium_install"
    requires = ("gateway_crds",)

    def run(self, ctx: Container) -> PhaseResult:
        intent = ctx.cluster_intent
        topo = ctx.upstream_topology
        if intent is None or topo is None or not topo.control_plane:
            raise BootstrapError("cilium_install", {"reason": "missing intent/topology"})

        # Resolve --set values from intent + topology.
        cp_ip = topo.control_plane[0].ip
        cilium_version = ctx.versions.cilium_chart_version()

        cmd: list[str] = [
            "cilium", "install",
            "--version", cilium_version,
            "--set", f"k8sServiceHost={cp_ip}",
            "--set", "k8sServicePort=6443",
            "--set", "kubeProxyReplacement=true",
            "--set", "cgroup.hostRoot=/sys/fs/cgroup",
            "--set", "cgroup.autoMount.enabled=false",
            # Cilium 1.19.x's gatewayAPI controller (per
            # cilium/cilium@v1.19.5/operator/pkg/gateway-api/cell.go)
            # hard-requires `gateway.networking.k8s.io/v1alpha2/
            # TLSRoute`, which Gateway API v1.3+ removed from the
            # standard channel. The simplest fix is to disable
            # cilium's gateway-api reconciler entirely; the
            # envoy-gateway chart that the bootstrap installs later
            # owns the Gateway API surface.
            "--set", "gatewayAPI.enabled=false",
            "--set", "operator.replicas=1",
            "--set", f"ipam.operator.clusterPoolIPv4PodCIDRList={intent.pod_cidr}",
            "--set", "mtu=1450",
        ]
        # If the values file exists, append it via --values.
        values_file = ctx.repo_root / "values" / "cilium.yaml"
        if values_file.exists():
            cmd += ["--values", str(values_file)]

        import os
        import subprocess
        # Pin KUBECONFIG to the tunnel-aware kubeconfig the
        # kubeconfig_pull phase wrote. The cilium CLI resolves the
        # apiserver via the kubeconfig's server: URL (which already
        # points at 127.0.0.1:<tunnel_port>). If we left KUBECONFIG
        # unset, the operator host's `~/.kube/config` would attempt
        # to reach the CP's LAN IP, which is not routable from the
        # operator host (different VLAN/SDN).
        kubeconfig = ctx.cluster_dir / "kubeconfig.yaml"
        env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
        ctx.logger.info(step="cilium_install_start", cmd=" ".join(cmd), kubeconfig=str(kubeconfig))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        except FileNotFoundError as exc:
            raise BootstrapError("cilium_install", {"reason": "cilium CLI not on PATH; install from https://docs.cilium.io/"}) from exc
        except subprocess.TimeoutExpired as exc:
            raise BootstrapError("cilium_install", {"reason": "cilium install timed out (180s)"}) from exc
        if result.returncode != 0:
            raise BootstrapError(
                "cilium_install",
                {"reason": "cilium install failed", "stderr": result.stderr.strip()[-400:]},
            )
        ctx.logger.info(step="cilium_install_ok", version=cilium_version)
        return PhaseResult.make_done("cilium_install", version=cilium_version, cp_ip=cp_ip)
