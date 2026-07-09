"""validate phase — pre-flight checks. No mutations.

Single responsibility: verify the cluster root is parseable, the
upstream (proxmox-vms) output.json exists, and the .env secrets
are populated. This is the ONLY phase that runs in `plan` mode
without `--phases` filtering.
"""

from __future__ import annotations

from ..container import Container
from ..hcl_parser import ClusterIntent, HclParseError, parse_cluster_root
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class ValidatePhase(Phase):
    """Parse main.tf + verify the upstream output.json exists."""

    name = "validate"
    requires = ()

    def run(self, ctx: Container) -> PhaseResult:
        main_tf = ctx.cluster_dir / "main.tf"
        if not main_tf.exists():
            raise BootstrapError("validate", {"missing": str(main_tf)})
        try:
            intent: ClusterIntent = parse_cluster_root(main_tf)
        except HclParseError as exc:
            raise BootstrapError("validate", {"hcl_error": str(exc)}) from exc

        # Upstream output.json sanity check.
        upstream_json = ctx.repo_root / ".." / "proxmox-vms" / "infra" / "clusters" / ctx.cluster_name / "output.json"
        if not upstream_json.exists():
            ctx.logger.warn(
                step="upstream_output_missing",
                message=f"proxmox-vms output.json not found at {upstream_json}",
            )
            raise BootstrapError("validate", {"upstream_output_missing": str(upstream_json)})

        # Secret sanity (warn only — destroy doesn't need secrets).
        if not ctx.secrets.cf_api_token():
            ctx.logger.warn(step="cf_api_token_missing", message="CF_API_TOKEN is empty; cloudflare-tunnel install will be skipped")

        ctx.logger.info(
            step="validate_done",
            cluster=intent.cluster_name,
            pod_cidr=intent.pod_cidr,
            k3s_version=intent.k3s_version,
        )
        return PhaseResult.make_done(
            "validate",
            cluster_name=intent.cluster_name,
            pod_cidr=intent.pod_cidr,
            svc_cidr=intent.svc_cidr,
            k3s_version=intent.k3s_version,
        )
