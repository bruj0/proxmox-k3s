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
            "--set", "gatewayAPI.enabled=true",
            "--set", "operator.replicas=1",
            "--set", f"ipam.operator.clusterPoolIPv4PodCIDRList={intent.pod_cidr}",
            "--set", "mtu=1450",
        ]
        # If the values file exists, append it via --values.
        values_file = ctx.repo_root / "values" / "cilium.yaml"
        if values_file.exists():
            cmd += ["--values", str(values_file)]

        import subprocess
        ctx.logger.info(step="cilium_install_start", cmd=" ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
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
