"""topology_writer phase — emit infra/clusters/<name>/k3s.json.

Always last. Collects the PhaseResult.data from every preceding
phase and writes the canonical handoff artifact downstream apps
read. Mirrors the cicd repo's `cluster_topology_writer` output
shape (so a downstream app parser written for either repo works
on both).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


@register
class TopologyWriterPhase(Phase):
    """Write infra/clusters/<name>/k3s.json with the live cluster state."""

    name = "topology_writer"
    requires = ("host_ports",)

    def run(self, ctx: Container) -> PhaseResult:
        topo = ctx.upstream_topology
        intent = ctx.cluster_intent
        if topo is None or intent is None:
            raise BootstrapError("topology_writer", {"reason": "missing topology/intent"})

        # Discover live state from the cluster.
        nodes = ctx.cluster_probe.get_nodes()
        pods = ctx.cluster_probe.list_pods("kube-system")
        cilium_running = sum(1 for p in pods if "cilium" in str(p.get("metadata", {}).get("name", "")))
        csi_running = ctx.cluster_probe.helm_release_present("proxmox-csi-plugin", "csi-proxmox")
        tunnel_running = ctx.cluster_probe.helm_release_present("cloudflare-tunnel", "cloudflare-tunnel")
        cert_running = ctx.cluster_probe.helm_release_present("cert-manager", "cert-manager")
        gateway_running = ctx.cluster_probe.helm_release_present("eg", "envoy-gateway-system")

        payload: dict[str, Any] = {
            "cluster_name": ctx.cluster_name,
            "k3s_version": intent.k3s_version,
            "api_endpoint": f"https://{topo.control_plane[0].ip}:6443" if topo.control_plane else "",
            "pod_cidr": intent.pod_cidr,
            "svc_cidr": intent.svc_cidr,
            "cluster_dns": intent.cluster_dns,
            "nodes": [
                {"role": n.role, "name": n.name, "vmid": n.vmid, "ip": n.ip}
                for n in topo.all_nodes
            ],
            "helm_releases": _helm_releases_from_versions(ctx),
            "smoke": {
                "nodes_ready": len(nodes) > 0 and all(_node_ready(n) for n in nodes),
                "cilium_pods_running": cilium_running,
                "csi_driver_registered": bool(csi_running),
                "cert_manager_ready": bool(cert_running),
                "envoy_gateway_available": bool(gateway_running),
                "cloudflare_tunnel_healthy": bool(tunnel_running),
            },
            "generated_at": datetime.now(UTC).isoformat(),
        }
        out = ctx.output_sink.write(payload)
        ctx.logger.info(step="k3s_json_written", path=str(out))
        return PhaseResult.make_done("topology_writer", path=str(out))


def _node_ready(node: Any) -> bool:
    """A node is Ready if its status.conditions include Ready=True."""
    for condition in node.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == "Ready":
            return bool(condition.get("status") == "True")
    return False


def _helm_releases_from_versions(ctx: Container) -> list[dict[str, str]]:
    out = []
    for entry in ctx.versions.helm_releases():
        name = str(entry.get("name", ""))
        if not name:
            continue
        out.append(
            {
                "name": name,
                "namespace": _namespace_from_chart(name),
                "version": str(entry.get("version", "")),
            }
        )
    return out


def _namespace_from_chart(chart_name: str) -> str:
    return {
        "cilium": "kube-system",
        "proxmox_cloud_controller_manager": "kube-system",
        "proxmox_csi_plugin": "csi-proxmox",
        "strrl_cloudflare_tunnel_ingress_controller": "cloudflare-tunnel",
        "cert_manager": "cert-manager",
        "envoy_gateway": "envoy-gateway-system",
    }.get(chart_name, "default")
