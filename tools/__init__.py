"""Operator-facing CLI tools for the proxmox-k3s bootstrap.

Each submodule is invokable via `python -m tools.<name>`. They wrap
the vendored orchestrator helpers (`provisioner/lib/*`) so the
operator never has to import the orchestrator directly.

Tools in this package:
  - tools.pveproxy  : open / stop / status / restart the PVE
                       port-forward that tunnels kubectl through
                       to the cluster's apiserver. Never starts the
                       tunnel twice (state file + alive-check
                       guard).

Run a tool like:
    python -m tools.pveproxy --cluster cicd start
    python -m tools.pveproxy --cluster cicd status
    python -m tools.pveproxy --cluster cicd stop
    python -m tools.pveproxy --cluster cicd kubectl get nodes
"""
