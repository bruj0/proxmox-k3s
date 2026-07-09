"""gateway_crds phase — apply the pinned Gateway API CRDs.

Per the WP07 design, the standard-channel Gateway API CRDs are
applied via `kubectl apply --server-side` against the upstream-
pinned URL. Doing this BEFORE helm install means the Envoy
Gateway chart can install with `crds.enabled=false` and never
race with chart-bundled CRDs.
"""

from __future__ import annotations

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError

GATEWAY_API_STANDARD_URL = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/"
    "download/v1.6.0/standard-install.yaml"
)


@register
class GatewayCrdsPhase(Phase):
    """Apply the pinned upstream Gateway API standard-channel CRDs."""

    name = "gateway_crds"
    requires = ("kubeconfig_pull",)

    def run(self, ctx: Container) -> PhaseResult:
        # `kubectl apply --server-side` is idempotent for unchanged
        # CRDs (the apiserver returns "no changes"). For CRD schema
        # upgrades we add `--force-conflicts`.
        #
        # `--validate=false` is REQUIRED when applying a URL: kubectl
        # would otherwise try to fetch the openapi schema from the
        # in-cluster apiserver (which it discovers from the kubeconfig,
        # but openapi v2 was removed in k8s 1.30+) and fall back to a
        # spurious localhost:8080.
        #
        # The kubeconfig written by `kubeconfig_pull` points at the
        # SSH-tunneled apiserver (https://127.0.0.1:<local_port>);
        # we pass it explicitly here so this phase doesn't depend
        # on the operator's KUBECONFIG env var.
        import subprocess
        cmd = [
            "kubectl",
            "--kubeconfig",
            str(ctx.cluster_dir / "kubeconfig.yaml"),
            "apply",
            "--server-side",
            "--force-conflicts",
            "--validate=false",
            "-f",
            GATEWAY_API_STANDARD_URL,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise BootstrapError("gateway_crds", {"reason": str(exc)}) from exc
        if result.returncode != 0:
            raise BootstrapError(
                "gateway_crds",
                {"reason": "kubectl apply failed", "stderr": result.stderr.strip()},
            )
        ctx.logger.info(step="gateway_crds_applied", stdout=result.stdout.strip()[:200])
        return PhaseResult.make_done("gateway_crds", url=GATEWAY_API_STANDARD_URL)
