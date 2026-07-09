#!/usr/bin/env bash
# scripts/capture_host_ports_baseline.sh
#
# WP05: capture the PVE nft prerouting chain once at WP00 setup so subsequent
# boots can assert "no new host ports were added".
#
# Usage:
#   PVE_SSH=root@10.0.0.1 PVE_SSH_PORT=6022 \
#     ./scripts/capture_host_ports_baseline.sh infra/clusters/cicd
#
# Writes <cluster_dir>/host_ports_baseline.txt.

set -euo pipefail

cluster_dir="${1:-infra/clusters/cicd}"
mkdir -p "$cluster_dir"

target="${PVE_SSH:-root@10.0.0.1}"
port="${PVE_SSH_PORT:-6022}"

echo "capturing nft prerouting chain from $target:$port -> $cluster_dir/host_ports_baseline.txt"
ssh -p "$port" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  "$target" "nft list chain ip nat prerouting" \
  > "$cluster_dir/host_ports_baseline.txt"

# Sanity: confirm the captured baseline contains at least the chain header; if
# not, the snapshot is probably an ssh error message.
if ! grep -q "chain prerouting" "$cluster_dir/host_ports_baseline.txt"; then
    echo "ERROR: captured baseline does not contain 'chain prerouting'." >&2
    cat "$cluster_dir/host_ports_baseline.txt" >&2
    exit 1
fi

echo "captured $(wc -l < "$cluster_dir/host_ports_baseline.txt") lines"
