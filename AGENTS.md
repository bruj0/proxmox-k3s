# AGENTS.md — guide for AI agents modifying this repo

This repository is **stage 2** of the proxmox provisioning pipeline.
It takes the two VMs that `proxmox-vms` cloned and turns them into
a working k3s cluster.

If you are an AI agent making modifications:

1. **Read `docs/architecture.md`** — the SOLID design (single
   source of truth for how the code is structured and WHY).
2. **Read this file** — the conventions that don't fit in the
   architecture doc.
3. **Read `versions.lock.yaml`** — the canonical record of
   what is installed and where the pin came from.
4. **Read the `provisioner/lib/phases/` directory** — see how
   each Phase is a single-responsibility class. Mirror that shape
   for any new phase.
5. **Read `provisioner/tests/test_solid_seams.py`** — see how
   every Phase is testable in isolation with fakes.

## SOLID refactor: the architectural contract

This repo is a deliberate refactor of the cicd repo's
`tools/bootstrap_cluster.py` (1,801-line god module) into
SOLID-compliant modules. The refactor's contract is:

- **Every Phase depends ONLY on Protocols** (`provisioner.lib.protocols`).
  Phases never import `PveSshProxy`, `K3sInstaller`,
  `SecretLoader`, or any concrete class.
- **The DI container** (`provisioner.lib.container`) is the
  ONLY place in the codebase that wires concrete implementations.
- **Adding a new phase** = writing ONE new file under
  `provisioner/lib/phases/` + adding one import line to
  `phases/__init__.py`. No edits to the orchestrator, CLI, or
  other phases.
- **Adding a new collaborator** = defining a Protocol in
  `protocols.py` + adding a fake + production implementation in
  `container.py`. Phases that need it import the Protocol.

If you find yourself reaching for `subprocess.run(["ssh", ...])`
or `subprocess.run(["helm", ...])` from a phase, **stop** — go
through `ctx.remote` / `ctx.cluster_probe` / a new Protocol.

## Canonical vocabulary

- **Bootstrap** / `bootstrap apply` — the Python orchestrator entry
  point (`provisioner/cli.py:cmd_apply`).
- **Upstream repo** — the sibling `proxmox-vms` repo whose
  `output.json` is this repo's input contract.
- **Cluster root** — `infra/clusters/<name>/` (a directory with a
  `main.tf` declaring the cluster identity and a `k3s.json` written
  after apply).
- **Phase** — one bootstrap step. Each phase is a `Phase` subclass
  with a stable `name`, a `requires` tuple, and a `run(ctx)` method.
- **Protocol** — a `typing.Protocol` (interface) that phases depend on.
- **Container** — the DI object that wires Protocols to concrete
  implementations. `Container.production(...)` for real runs,
  `Container.for_tests(...)` for mock-based tests.

## Repository conventions

### File layout

- `provisioner/cli.py` — `bootstrap plan|apply|destroy|validate`.
- `provisioner/lib/protocols.py` — the 5 Protocols (interfaces).
- `provisioner/lib/container.py` — the DI wiring + fakes.
- `provisioner/lib/phases/base.py` — `Phase` ABC + registry.
- `provisioner/lib/phases/<name>.py` — one Phase each.
- `provisioner/lib/phases/__init__.py` — self-registering imports.
- `provisioner/lib/orchestrator.py` — 150-line runner.
- `provisioner/lib/hcl_parser.py` — typed HCL reader for `main.tf`.
- `provisioner/lib/upstream_reader.py` — typed reader for proxmox-vms output.json.
- `provisioner/lib/log.py` (vendored from cicd, unchanged).
- `provisioner/lib/pve_ssh.py`, `pve_client.py`, `secret_loader.py`,
  `versions.py`, `k3s_installer.py`, `kubeconfig_merger.py`,
  `host_ports.py`, `repo_locator.py`, `cluster_topology_writer.py`
  (all vendored from cicd, unchanged).
- `provisioner/tests/conftest.py` — sys.path setup.
- `provisioner/tests/test_log.py` + `test_pve_ssh.py` +
  `test_versions.py` + `test_secret_loader.py` + `test_repo_locator.py`
  (all vendored from cicd).
- `provisioner/tests/test_hcl_parser.py` — HCL parser tests.
- `provisioner/tests/test_solid_seams.py` — the SOLID seam tests
  (this is the file that proves the design works).
- `versions.yaml` + `tools/versions.lock.yaml` — pinned versions
  + provenance. Read by `LockfileVersionsSource`.
- `infra/clusters/<name>/main.tf` — declarative cluster intent.
- `infra/clusters/<name>/k3s.json` — written after a successful
  apply (downstream app input).
- `infra/clusters/<name>/bootstrap_state.json` — the StateStore file
  (which phases have run successfully).
- `logs/<subcommand>_<cluster>_<utc>.audit.jsonl` — JSONL audit log.

### Python style

- `python -m ruff check provisioner/` — must pass.
- `mypy --strict --explicit-package-bases` on `provisioner/lib` +
  `provisioner/cli.py` — must pass.
- All CLI entry points expose `main()` returning `int` exit code.
- Tests live in `provisioner/tests/`. Every Phase has a test in
  `test_solid_seams.py` that uses `Container.for_tests(...)` with
  fakes — NO live SSH, NO live helm, NO live kubectl.
- The audit log uses `StructuredLogger`; secret-bearing keys are
  scrubbed at the boundary (M7 misfit).

### SSH rules

- Every SSH call MUST go through `ctx.remote.run(...)`. Never call
  `subprocess.run(["ssh", ...])` from a phase — that's what the
  Protocol is for.
- `ExitOnForwardFailure=yes` and `ServerAliveInterval=15` +
  `ServerAliveCountMax=4` are mandatory. The vendored
  `pve_ssh.PveSshProxy` enforces these via its `_DEFAULT_JUMP`.
- The PVE proxy user is `root`, the proxy port is `6022`, the
  proxy host is `kvm.bruj0.net` (operator's jump box). Override
  via `PVE_SSH_TARGET` env var if running against a non-standard
  topology.

### Live-host notes (kvm.example.net, 2026-07-09)

- The PVE proxy host is `kvm.bruj0.net:6022` (NOT the PVE's own
  UI port 8006). `PROXMOX_API_URL=https://kvm.bruj0.net:8006/api2/json`
  is for `proxmox-vms`; this repo reads the output.json, not the API.
- The cluster VMs are on `10.0.0.0/8` (PVE SDN). Inside the VMs,
  the network is `172.16.0.0/16` (pod) and `172.17.0.0/16` (svc)
  — see `versions.yaml::k3s.<v>.install_args_default`.
- Cilium operator's default `replicas: 2` is too high for a
  single-CP cluster; we pin `operator.replicas=1` in
  `values/cilium.yaml`. Verifier fails if the operator pod is
  Pending.
- k3s's default `cluster-cidr=10.42.0.0/16` overlaps the host LAN
  `10.0.0.0/8` and short-circuits pod->apiserver traffic
  (k3s-io/k3s#4627). We pin `cluster-cidr=172.16.0.0/16` etc. in
  the cluster root.

## How to add a cluster

1. Run `proxmox-vms` first; verify
   `proxmox-vms/infra/clusters/<name>/output.json` exists and lists
   2 VMs with IPs.
2. `cp -r infra/clusters/cicd infra/clusters/<name>`.
3. Edit the new cluster root's `main.tf`:
   - `cluster_name` (in `locals`)
   - `pod_cidr`, `svc_cidr`, `cluster_dns` (in `locals`)
   - `cf_tunnel_name`, `cf_api_token`, `cf_account_id`
4. `make validate CLUSTER=<name>` (no mutations).
5. `make apply CLUSTER=<name>` (installs k3s + helm charts).

## Common pitfalls

- **Don't import concrete classes from phases.** The Protocol
  exists so that `FakeRemoteExecutor` can substitute for
  `PveSshRemoteAdapter`. If you `import pve_ssh.PveSshProxy` from
  a phase, you've broken the DIP — the test will fail.
- **Don't use `subprocess.run` to shell out to `ssh`/`helm`/`kubectl`
  from a phase.** Go through `ctx.remote` / `ctx.cluster_probe` /
  a new Protocol. The subprocess paths bypass the test fakes.
- **Don't write `k3s.json` if any phase failed.** The
  `topology_writer` phase runs last and is gated on the orchestrator's
  phase ordering.
- **Don't use `helm install`** (use `helm upgrade --install`).
  The orchestrator's `helm_releases` phase enforces this.
- **Don't freeze the cluster at the install version.** k3s's
  built-in upgrade controller rolls forward automatically; we
  pin the install version for reproducibility, not for permanence.

## Quick reference

| Want to... | File |
|---|---|
| Read the SOLID design | `docs/architecture.md` |
| Read what is installed | `versions.yaml` |
| Read the bootstrapper pins + provenance | `tools/versions.lock.yaml` |
| Read what is *currently* running | `infra/clusters/<name>/k3s.json` |
| Run a phase | `make plan CLUSTER=<name>` / `make apply CLUSTER=<name>` |
| Modify the k3s install | `provisioner/lib/phases/install_k3s.py` + tests in `test_solid_seams.py` |
| Modify a helm release | `provisioner/lib/phases/helm_releases.py` + matching `values/*.yaml` |
| Modify the SSH plumbing | `provisioner/lib/pve_ssh.py` (vendored) |
| Add a new cluster | `cp -r infra/clusters/cicd infra/clusters/<name>` + edit main.tf |
| Add a new Phase | write `provisioner/lib/phases/<name>.py` with `@register` decorator + import in `phases/__init__.py` |
| Add a new collaborator | add a Protocol in `protocols.py` + Production + Fake impl in `container.py` |