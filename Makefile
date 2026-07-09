# Makefile — proxmox-k3s pipeline (stage 2, SOLID refactor).
#
# Targets mirror proxmox-vms/Makefile so operators who know one
# repo know both:
#
#   make plan      — diff desired cluster against live state (no changes)
#   make apply     — install k3s + helm charts (idempotent; plan then apply)
#   make destroy   — tear down the cluster's workloads
#   make validate  — parse main.tf, no SSH / no mutations
#   make test      — run pytest
#   make lint      — run ruff + mypy
#   make clean     — remove build/ + logs/

SHELL := /bin/bash
PYTHON ?= python

# .env is gitignored; -include so the makefile works without one.
-include .env
export

# Default CLUSTER. Override on the command line: `make apply CLUSTER=cicd`.
CLUSTER ?= cicd

# Path to the sibling proxmox-vms repo. Defaults to ../proxmox-vms,
# which matches the layout we use in this monorepo.
PROXMOX_VMS_REPO ?= $(PWD)/../proxmox-vms

# Path to the SSH private key for the PVE jump box.
SSH_KEY ?= ~/.ssh/id_ed25519

# ------------------------------------------------------ public targets

.PHONY: plan apply destroy validate test lint clean help

help:
	@echo "Targets:"
	@echo "  plan     [CLUSTER=<name>]  -- diff desired vs live cluster (no changes)"
	@echo "  apply    [CLUSTER=<name>]  -- install k3s + helm charts (idempotent)"
	@echo "  destroy  [CLUSTER=<name>]  -- tear down workloads (does NOT touch VMs)"
	@echo "  validate [CLUSTER=<name>]  -- parse main.tf, no SSH / no mutations"
	@echo "  test            -- run pytest"
	@echo "  lint            -- run ruff + mypy"
	@echo "  clean           -- remove build/ + logs/ + audit files"

plan:
	@$(PYTHON) -m provisioner \
		--proxmox-vms-repo $(PROXMOX_VMS_REPO) \
		--ssh-key $(SSH_KEY) \
		plan $(CLUSTER)

apply:
	@$(PYTHON) -m provisioner \
		--proxmox-vms-repo $(PROXMOX_VMS_REPO) \
		--ssh-key $(SSH_KEY) \
		apply $(CLUSTER) \
		--auto-approve

destroy:
	@$(PYTHON) -m provisioner \
		--proxmox-vms-repo $(PROXMOX_VMS_REPO) \
		--ssh-key $(SSH_KEY) \
		destroy $(CLUSTER) \
		--auto-approve

validate:
	@$(PYTHON) -m provisioner validate $(CLUSTER) \
		--proxmox-vms-repo $(PROXMOX_VMS_REPO) \
		--ssh-key $(SSH_KEY)

# ------------------------------------------------------ internal targets

test:
	@$(PYTHON) -m pytest provisioner/tests/ -q

lint:
	@$(PYTHON) -m ruff check provisioner/	@$(PYTHON) -m mypy provisioner/lib/

install-deps:
	@$(PYTHON) -m pip install --user pytest ruff mypy

clean:
	@rm -rf build/ logs/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned"
