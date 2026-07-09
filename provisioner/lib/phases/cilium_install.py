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
        topo = ctx.upstream_topology
        if topo is None or not topo.control_plane:
            raise BootstrapError("cilium_install", {"reason": "missing topology"})
        cilium_version = ctx.versions.cilium_chart_version()
        cp_ip = topo.control_plane[0].ip

        # Mirrors the canonical install command at
        # https://docs.cilium.io/en/stable/installation/k3s/#install-cilium:
        #     cilium install --version 1.19.5 \
        #       --set=ipam.operator.clusterPoolIPv4PodCIDRList="<pod_cidr>"
        #
        # cilium discovers the apiserver via the kubeconfig's
        # server: URL (the operator host's
        # 127.0.0.1:<tunnel_port>, written by kubeconfig_pull).
        # Passing --set k8sServiceHost=<cp_lan_ip> is unnecessary
        # AND wrong: cilium would try to reach the CP's LAN IP
        # directly, which is not routable from the operator host.
        #
        # cilium also auto-detects:
        #   - kubeProxyReplacement: implied by k3s --disable-kube-proxy
        #   - mtu: probed from the cluster-cidr + eth0 MTU
        #   - cgroup.hostRoot: discovered via /proc/self/mountinfo
        cmd: list[str] = [
            "cilium", "install",
            "--version", cilium_version,
            # k3s bakes the in-cluster kubeconfig with a
            # 127.0.0.1:<random> server: URL (the localhost proxy
            # that fronts k3s's apiserver). cilium's `config`
            # init container tries to reach that proxy at start-up
            # but the proxy binds to the HOST network, so the init
            # container sees "dial 127.0.0.1:<p>: connect refused"
            # and crash-loops. Pin k8sServiceHost to the CP's LAN
            # IP so cilium reaches the apiserver via the routable
            # interface instead. k8sServicePort stays at 6443.
            "--set", f"k8sServiceHost={cp_ip}",
            "--set", "k8sServicePort=6443",
            # Pod CIDR — explicit override required because k3s's
            # --cluster-cidr=172.16.0.0/16 differs from cilium's
            # 10.42.0.0/16 default (see docs: "Install Cilium with
            # --set=ipam.operator.clusterPoolIPv4PodCIDRList=... to
            # match k3s default podCIDR").
            "--set", f"ipam.operator.clusterPoolIPv4PodCIDRList={intent.pod_cidr}",
            # Cilium 1.19.x's gatewayAPI controller (per
            # cilium/cilium@v1.19.5/operator/pkg/gateway-api/cell.go)
            # hard-requires `gateway.networking.k8s.io/v1alpha2/
            # TLSRoute`, which Gateway API v1.3+ removed from the
            # standard channel. Disable cilium's gateway-api
            # reconciler entirely; envoy-gateway (installed later
            # in helm_releases) owns the Gateway API surface.
            "--set", "gatewayAPI.enabled=false",
            # Single-CP cluster: cilium-operator's default HA
            # replica=2 leaves the second pod Pending forever.
            "--set", "operator.replicas=1",
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
        return PhaseResult.make_done("cilium_install", version=cilium_version)
