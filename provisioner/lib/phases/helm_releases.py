"""helm_releases phase — install the remaining 5 helm charts.

After cilium owns CNI routing, the orchestrator installs:

  1. proxmox-cloud-controller-manager (providerID + topology labels)
  2. proxmox-csi-plugin (lvm-thin StorageClass)
  3. strrl/cloudflare-tunnel-ingress-controller
  4. cert-manager (in-cluster CA only)
  5. envoy-gateway (GatewayClass=envoy implementation)

Each chart's pin + values file lives in `versions.lock.yaml` +
`values/<chart>.yaml`. We render the values via `helm -f
values/<chart>.yaml` (idempotent: `helm upgrade --install`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError

# The chart install order matters: CCM before CSI (CSI checks
# topology.kubernetes.io labels that CCM sets), tunnel after CCM
# (tunnel uses cluster networking that CCM enables).
CHART_ORDER = (
    "proxmox_cloud_controller_manager",
    "proxmox_csi_plugin",
    "strrl_cloudflare_tunnel_ingress_controller",
    "cert_manager",
    "envoy_gateway",
)


@register
class HelmReleasesPhase(Phase):
    """Install every pinned chart in CHART_ORDER via `helm upgrade --install`."""

    name = "helm_releases"
    requires = ("cilium_install",)

    def run(self, ctx: Container) -> PhaseResult:
        releases = ctx.versions.helm_releases()
        releases_by_name = {r["name"]: r for r in releases}
        installed: list[dict[str, object]] = []

        for chart_name in CHART_ORDER:
            entry = releases_by_name.get(chart_name)
            if entry is None:
                ctx.logger.warn(step="helm_release_skipped", message=f"chart {chart_name} not in lockfile; skipping")
                continue
            self._install_one(ctx, chart_name, entry, installed)

        return PhaseResult.make_done("helm_releases", installed=installed)

    def _install_one(
        self,
        ctx: Container,
        chart_name: str,
        entry: Mapping[str, Any],
        installed: list[dict[str, object]],
    ) -> None:
        """Install one chart. Failures raise BootstrapError."""
        version = str(entry.get("version", ""))
        repo = str(entry.get("repo", ""))
        if not version or not repo:
            raise BootstrapError("helm_releases", {"chart": chart_name, "reason": "missing version/repo"})

        values_file = ctx.repo_root / "values" / f"{chart_name.replace('_', '-')}.yaml"
        cmd: list[str] = [
            "helm", "upgrade", "--install", chart_name,
            repo,
            "--version", version,
            "--namespace", _namespace_for(chart_name),
            "--create-namespace",
        ]
        if values_file.exists():
            cmd += ["--values", str(values_file)]

        # Inline secret values for the cloudflare tunnel.
        if chart_name == "strrl_cloudflare_tunnel_ingress_controller":
            intent = ctx.cluster_intent
            cmd += [
                "--set", f"cloudflare.apiToken={ctx.secrets.cf_api_token()}",
                "--set", f"cloudflare.accountId={ctx.secrets.cf_account_id()}",
                "--set", f"cloudflare.tunnelName={intent.cf_tunnel_name if intent else ctx.cluster_name}",
                "--set", "ingressClass.name=cloudflare-tunnel",
            ]

        ctx.logger.info(step="helm_install_start", chart=chart_name, version=version)
        import os
        import subprocess
        # Pin KUBECONFIG to the tunnel-aware kubeconfig (same
        # rationale as cilium_install — the operator's
        # ~/.kube/config would steer helm at the unreachable CP LAN
        # IP).
        env = {**os.environ, "KUBECONFIG": str(ctx.cluster_dir / "kubeconfig.yaml")}
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        except subprocess.TimeoutExpired as exc:
            raise BootstrapError("helm_releases", {"chart": chart_name, "reason": "helm install timed out (300s)"}) from exc
        if result.returncode != 0:
            raise BootstrapError(
                "helm_releases",
                {
                    "chart": chart_name,
                    "reason": "helm upgrade --install failed",
                    "stderr": result.stderr.strip()[-400:],
                },
            )
        installed.append({"name": chart_name, "version": version, "namespace": _namespace_for(chart_name)})
        ctx.logger.info(step="helm_install_ok", chart=chart_name, version=version)


def _namespace_for(chart_name: str) -> str:
    """Map chart name -> namespace (pinned in the cicd repo's design)."""
    return {
        "proxmox_cloud_controller_manager": "kube-system",
        "proxmox_csi_plugin": "csi-proxmox",
        "strrl_cloudflare_tunnel_ingress_controller": "cloudflare-tunnel",
        "cert_manager": "cert-manager",
        "envoy_gateway": "envoy-gateway-system",
    }.get(chart_name, "default")
