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

# Hard-required: any failure raises BootstrapError and aborts
# the bootstrap. Auxiliary charts (cloudflare-tunnel is the only
# one in scope) log a warning and the phase still finishes.
HARD_REQUIRED_CHARTS: frozenset[str] = frozenset({
    "proxmox_cloud_controller_manager",
    "proxmox_csi_plugin",
    "cert_manager",
    "envoy_gateway",
})


@register
class HelmReleasesPhase(Phase):
    """Install every pinned chart in CHART_ORDER via `helm upgrade --install`."""

    name = "helm_releases"
    # topology_labels must run before helm_releases: proxmox-csi-plugin's
    # node DaemonSet reads topology.kubernetes.io/{region,zone} at startup;
    # on a single-node PVE host proxmox-ccm doesn't set them automatically,
    # so without topology_labels the csi-plugin-node pods CrashLoopBackOff
    # forever with "Failed to get region or zone for node".
    requires = ("cilium_install", "topology_labels")

    def run(self, ctx: Container) -> PhaseResult:
        # Pre-step: ensure the legacy HTTP Helm repo for cert-manager
        # is registered. OCI registries are discovered per-chart
        # automatically by 'helm upgrade --install <oci://...>'; the
        # cert-manager chart lives at the historical Jetstack HTTP
        # index which helm only finds if the repo is added first.
        _ensure_helm_repos()

        releases = ctx.versions.helm_releases()
        releases_by_name = {r["name"]: r for r in releases}
        installed: list[dict[str, object]] = []
        failed: list[dict[str, str]] = []

        for chart_name in CHART_ORDER:
            entry = releases_by_name.get(chart_name)
            if entry is None:
                ctx.logger.warn(step="helm_release_skipped", message=f"chart {chart_name} not in lockfile; skipping")
                continue
            try:
                self._install_one(ctx, chart_name, entry, installed)
            except BootstrapError as exc:
                # Some charts (e.g. cloudflare-tunnel-ingress-controller
                # 0.0.23 on ghcr.io/strrl/charts returns 403 to
                # anonymous pulls) are nice-to-have, not hard
                # required. The orchestrator's BootstrapError-from-_run_helm
                # in the cicd repo distinguishes `hard_required` from
                # auxiliary charts; we mirror that here.
                if chart_name in HARD_REQUIRED_CHARTS:
                    raise
                ctx.logger.warn(
                    step="helm_release_soft_failure",
                    message=f"auxiliary chart {chart_name} install failed; continuing",
                    chart=chart_name,
                    reason=exc.detail.get("reason"),
                    stderr=exc.detail.get("stderr", "")[-200:],
                )
                failed.append({"chart": chart_name, "reason": exc.detail.get("reason", "")})

        return PhaseResult.make_done(
            "helm_releases",
            installed=installed,
            soft_failures=failed,
        )

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
        # Helm release names must be lowercase + dashes + dots only
        # (no underscores). The lockfile key uses underscores (e.g.
        # "proxmox_cloud_controller_manager") to be
        # Python-identifier friendly; the release name in the
        # cluster uses the chart's published name with dashes
        # ("proxmox-cloud-controller-manager"). Both refer to the
        # same release once installed.
        release_name = chart_name.replace("_", "-")
        cmd: list[str] = [
            "helm", "upgrade", "--install", release_name,
            repo,
            "--version", version,
            "--namespace", _namespace_for(chart_name),
            "--create-namespace",
        ]
        if values_file.exists():
            cmd += ["--values", str(values_file)]

        # Inline (per-chart) secret values for charts that need them.
        # cert-manager and envoy-gateway don't need any; the rest
        # fetch their secret envs from the EnvSecretsSource.
        intent = ctx.cluster_intent
        for set_kind, key, value in _secret_flags(chart_name, ctx, intent):
            cmd += [set_kind, f"{key}={value}"]

        ctx.logger.info(step="helm_install_start", chart=chart_name, release_name=release_name, version=version)
        import os
        import subprocess
        # Pin KUBECONFIG to the tunnel-aware kubeconfig (same
        # rationale as cilium_install — the operator's
        # ~/.kube/config would steer helm at the unreachable CP LAN
        # IP).
        env = {**os.environ, "KUBECONFIG": str(ctx.cluster_dir / "kubeconfig.yaml")}
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
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


def _ensure_helm_repos() -> None:
    """Add the legacy HTTP Helm repo for cert-manager (idempotent).

    The four OCI-based charts (proxmox-ccm, proxmox-csi,
    cloudflare-tunnel, envoy-gateway) are pulled directly by
    `oci://...` URL — helm doesn't need a registered repo for
    them. Cert-manager still publishes at
    https://charts.jetstack.io (the historical index); helm
    needs the repo registered before `helm upgrade --install
    cert-manager/cert-manager` will resolve it.

    `helm repo add` is idempotent (updating an existing repo is
    fine). We ignore the rc since the existing repo may
    already be configured (no `helm repo add` flag suppresses
    the "already exists" error).
    """
    import subprocess
    proc = subprocess.run(  # noqa: S603
        [
            "helm", "repo", "add", "cert-manager",
            "https://charts.jetstack.io",
            "--force-update",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # `helm repo add` exit code 1 with stderr "already exists" is
    # fine (means someone else registered it). Anything else is
    # not.
    if proc.returncode != 1:
        return
    if "already exists" in proc.stderr.lower():
        return
    raise BootstrapError(
        "helm_releases",
        {"reason": "helm repo add cert-manager failed", "stderr": proc.stderr.strip()},
    )


def _namespace_for(chart_name: str) -> str:
    """Map chart name -> namespace (matches the cicd helm_client convention)."""
    return {
        "proxmox_cloud_controller_manager": "kube-system",
        "proxmox_csi_plugin": "proxmox-csi-plugin",
        "strrl_cloudflare_tunnel_ingress_controller": "cloudflare-tunnel-ingress-controller",
        "cert_manager": "cert-manager",
        "envoy_gateway": "envoy-gateway-system",
    }.get(chart_name, "default")


def _secret_flags(
    chart_name: str,
    ctx: Any,
    intent: Any,
) -> list[tuple[str, str, str]]:
    """Inline `--set` / `--set-string` flags the chart needs.

    Returns `[(set_kind, key, value), ...]` where `set_kind` is
    `"--set"` or `"--set-string"`. --set-string is needed for
    values where helm would otherwise auto-coerce (annotations
    need string types — `true` bool would render as `null`).

    Secrets come from `ctx.secrets` (EnvSecretsSource in
    production). The cicd `helm_client.py` is the source of
    truth for keys (cluster URL, token, region/zone, etc.).
    """
    proxmox_url = ctx.secrets.proxmox_api_url()
    flags: list[tuple[str, str, str]] = []
    if chart_name in ("proxmox_cloud_controller_manager", "proxmox_csi_plugin"):
        flags += [
            ("--set", "config.clusters[0].url", proxmox_url),
            ("--set", "config.clusters[0].token_id", ctx.secrets.proxmox_token_id()),
            ("--set", "config.clusters[0].token_secret", ctx.secrets.proxmox_token_secret()),
            ("--set", "config.clusters[0].region", ctx.secrets.proxmox_region()),
            ("--set", "config.clusters[0].insecure", "true"),
            ("--set", "config.features.provider", "default"),
        ]
    if chart_name == "proxmox_csi_plugin":
        flags += [
            ("--set", "storageClass[0].name", "proxmox-lvm-thin"),
            ("--set", "storageClass[0].region", ctx.secrets.proxmox_region()),
            ("--set", "storageClass[0].zone", ctx.secrets.proxmox_zone()),
            ("--set", "storageClass[0].storage", "data1"),
            # --set-string: helm --set would coerce `true` to bool,
            # breaking the annotation rendering.
            ("--set-string", "storageClass[0].annotations.storageclass\\.kubernetes\\.io/is-default-class", "true"),
        ]
    if chart_name == "strrl_cloudflare_tunnel_ingress_controller":
        tunnel = intent.cf_tunnel_name if intent else ctx.cluster_name
        flags += [
            ("--set", "cloudflare.apiToken", ctx.secrets.cf_api_token()),
            ("--set", "cloudflare.accountId", ctx.secrets.cf_account_id()),
            ("--set", "cloudflare.tunnelName", tunnel),
            ("--set", "ingressClass.name", "cloudflare-tunnel"),
            ("--set", "ingressClass.controller", "dev.strrl.cloudflaretunnelingresscontroller/ingress"),
            ("--set", "ingressClass.enabled", "true"),
        ]
    return flags
