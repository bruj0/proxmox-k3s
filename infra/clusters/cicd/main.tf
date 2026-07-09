###############################################################################
# cicd cluster root — infra/clusters/cicd.
#
# Stage-2 declarative intent: turns the two VMs proxmox-vms cloned
# (cicd-cp-1, cicd-w-1) into a working k3s cluster.
#
# This file is NOT applied by tofu. It is parsed by the Python
# orchestrator (provisioner/lib/hcl_parser.py) — the `locals` block
# is the single source of truth for the cluster identity.
#
# Format mirrors proxmox-k8s-cicd/infra/modules/proxmox-k3s-cluster
# so an operator who knows one repo knows both.
###############################################################################

locals {
  cluster_name = "cicd"
  # Network — pinned to non-overlapping RFC1918 ranges (see
  # AGENTS.md §"Live-host notes" / k3s-io/k3s#4627).
  pod_cidr    = "172.16.0.0/16"
  svc_cidr    = "172.17.0.0/16"
  cluster_dns = "172.17.0.10"

  # K3s install args (mirrors versions.yaml::k3s.<v>.install_args_default).
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

  # Cloudflare tunnel — must match proxmox-vms/infra/clusters/cicd/main.tf.
  cf_tunnel_name = "cicd"

  # CSI / CCM storage ID — must match proxmox-vms.
  csi_storage = "data1"
  ccm_region  = "proxmox-host"
  ccm_zone    = "BigBertha"

  # K3s pinned version (mirrors tools/versions.lock.yaml::k3s_stable_version).
  k3s_version = "v1.36.2+k3s1"

  tags = ["proxmox-k3s", "cicd"]
}