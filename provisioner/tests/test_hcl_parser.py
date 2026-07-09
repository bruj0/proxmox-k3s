"""Tests for the HCL parser.

Pins the contract that the orchestrator relies on:
  - cluster_name / pod_cidr / svc_cidr / cluster_dns are required locals.
  - list-of-strings locals parse correctly (multi-line supported).
  - ${local.X} interpolations resolve.
  - Missing required fields raise HclParseError.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lib.hcl_parser import ClusterIntent, HclParseError, parse_cluster_root

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_cicd_fixture() -> None:
    intent = parse_cluster_root(FIXTURES / "cicd_main.tf")
    assert isinstance(intent, ClusterIntent)
    assert intent.cluster_name == "cicd"
    assert intent.pod_cidr == "172.16.0.0/16"
    assert intent.svc_cidr == "172.17.0.0/16"
    assert intent.cluster_dns == "172.17.0.10"
    assert intent.cf_tunnel_name == "cicd"
    assert intent.csi_storage == "data1"
    assert intent.ccm_region == "proxmox-host"
    assert intent.ccm_zone == "BigBertha"
    assert intent.k3s_version == "v1.36.2+k3s1"


def test_interpolation_resolves_local_reference(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(
        """
        locals {
          cluster_name = "demo"
          pod_cidr     = "10.0.0.0/16"
          svc_cidr     = "10.1.0.0/16"
          cluster_dns  = "10.1.0.10"
          install_k3s_exec_server = [
            "--cluster-cidr=${local.pod_cidr}",
            "--service-cidr=${local.svc_cidr}",
          ]
          install_k3s_exec_agent = []
          cf_tunnel_name = "demo"
          csi_storage = "data1"
          ccm_region  = "proxmox-host"
          ccm_zone    = "BigBertha"
          k3s_version = "v1.36.2+k3s1"
        }
        """
    )
    intent = parse_cluster_root(main_tf)
    assert intent.install_k3s_exec_server == (
        "--cluster-cidr=10.0.0.0/16",
        "--service-cidr=10.1.0.0/16",
    )


def test_interpolation_resolves_variable_default(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(
        """
        variable "image_tag" {
          type    = string
          default = "v1.36.2+k3s1"
        }
        locals {
          cluster_name = "demo"
          pod_cidr     = "10.0.0.0/16"
          svc_cidr     = "10.1.0.0/16"
          cluster_dns  = "10.1.0.10"
          install_k3s_exec_server = []
          install_k3s_exec_agent  = []
          cf_tunnel_name = "demo"
          csi_storage = "data1"
          ccm_region  = "proxmox-host"
          ccm_zone    = "BigBertha"
          k3s_version = "${var.image_tag}"
        }
        """
    )
    intent = parse_cluster_root(main_tf)
    assert intent.k3s_version == "v1.36.2+k3s1"


def test_missing_required_field_raises(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(
        """
        locals {
          cluster_name = "demo"
          # pod_cidr missing!
          svc_cidr    = "10.1.0.0/16"
          cluster_dns = "10.1.0.10"
          install_k3s_exec_server = []
          install_k3s_exec_agent  = []
          cf_tunnel_name = "demo"
          csi_storage = "data1"
          ccm_region  = "proxmox-host"
          ccm_zone    = "BigBertha"
          k3s_version = "v1.36.2+k3s1"
        }
        """
    )
    with pytest.raises(HclParseError, match="missing required locals keys"):
        parse_cluster_root(main_tf)


def test_no_locals_block_raises(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text("# nothing here")
    with pytest.raises(HclParseError, match="no `locals"):
        parse_cluster_root(main_tf)


def test_unknown_variable_reference_raises(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(
        """
        locals {
          cluster_name = "${var.foo}"
          pod_cidr     = "10.0.0.0/16"
          svc_cidr     = "10.1.0.0/16"
          cluster_dns  = "10.1.0.10"
          install_k3s_exec_server = []
          install_k3s_exec_agent  = []
          cf_tunnel_name = "demo"
          csi_storage = "data1"
          ccm_region  = "proxmox-host"
          ccm_zone    = "BigBertha"
          k3s_version = "v1.36.2+k3s1"
        }
        """
    )
    with pytest.raises(HclParseError, match=r"variable 'foo'"):
        parse_cluster_root(main_tf)


def test_comments_are_stripped(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(
        """
        locals {
          # this is a comment with a { brace inside
          cluster_name = "demo" # trailing comment
          pod_cidr     = "10.0.0.0/16"
          svc_cidr     = "10.1.0.0/16"
          cluster_dns  = "10.1.0.10"
          install_k3s_exec_server = []
          install_k3s_exec_agent  = []
          cf_tunnel_name = "demo"
          csi_storage = "data1"
          ccm_region  = "proxmox-host"
          ccm_zone    = "BigBertha"
          k3s_version = "v1.36.2+k3s1"
        }
        """
    )
    intent = parse_cluster_root(main_tf)
    assert intent.cluster_name == "demo"
