###############################################################################
# apps cluster root — infra/clusters/apps.
#
# Mirrors infra/clusters/cicd/main.tf; differences:
#   - pod_cidr/svc_cidr/cluster_dns: 172.20/172.21/172.21.0.10
#   - cf_tunnel_name: "apps"
###############################################################################

locals {
  cluster_name = "apps"
  pod_cidr    = "172.20.0.0/16"
  svc_cidr    = "172.21.0.0/16"
  cluster_dns = "172.21.0.10"

  install_k3s_exec_server = [
    "--flannel-backend=none",
    "--disable-kube-proxy",
    "--disable-network-policy",
    "--disable=traefik",
    "--disable=servicelb",
    "--disable=local-storage",
    "--disable=metrics-server",
    "--kubelet-arg=cloud-provider=external",
    "--cluster-cidr=${local.pod_cidr}",
    "--service-cidr=${local.svc_cidr}",
    "--cluster-dns=${local.cluster_dns}",
  ]
  install_k3s_exec_agent = [
    "--kubelet-arg=cloud-provider=external",
  ]

  cf_tunnel_name = "apps"
  csi_storage = "data1"
  ccm_region  = "proxmox-host"
  ccm_zone    = "BigBertha"

  k3s_version = "v1.36.2+k3s1"
  tags = ["proxmox-k3s", "apps"]
}