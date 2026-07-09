locals {
  cluster_name = "cicd"
  pod_cidr    = "172.16.0.0/16"
  svc_cidr    = "172.17.0.0/16"
  cluster_dns = "172.17.0.10"
  install_k3s_exec_server = [
    "--flannel-backend=none",
    "--disable-kube-proxy",
    "--cluster-cidr=${local.pod_cidr}",
  ]
  install_k3s_exec_agent = [
    "--kubelet-arg=cloud-provider=external",
  ]
  cf_tunnel_name = "cicd"
  csi_storage = "data1"
  ccm_region  = "proxmox-host"
  ccm_zone    = "BigBertha"
  k3s_version = "v1.36.2+k3s1"
  tags = ["proxmox-k3s", "cicd"]
}