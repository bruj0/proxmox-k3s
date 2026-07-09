"""csi_smoke phase — functional smoke test for proxmox-csi-plugin.

Why
---
The `helm_releases` phase installs proxmox-csi-plugin, but a
green helm release only proves the controller Pods RUNNING, not
that they actually serve LVM-thin volumes end-to-end. On
single-node PVE without corosync, proxmox-ccm does not
auto-derive region/zone topology labels (see topology_labels
phase + proxmox-k8s-cicd/versions.lock.yaml::
csi_smoke_roundtrip_2026_07_08), and the CSI node DaemonSet
silently CrashLoopBackOffs on a missing label:

    Failed to get region or zone for node

This phase pins the live behaviour:

  1. Pre-flight — assert `proxmox-lvm-thin` StorageClass exists
     AND is marked default, the CSI controller is Ready, and
     every Node carries both topology labels. Fail fast with a
     clear BootstrapError pointing at the relevant phase.

  2. Apply a tiny manifest: Namespace + PVC + writer-pod
     busybox that writes a marker file to `/data/marker`. The
     StorageClass is `WaitForFirstConsumer`, so the PVC won't
     bind until a Pod actually references it. This matches
     real usage and exercises the entire chain (PVC -> PV ->
     createVolume on the LVM-thin pool -> node mount).

  3. Wait for PVC `status.phase=Bound` (60s budget — LCS's
     thin-pool create + the node kubelet's NodeStageVolume
     round-trip on a fresh PV typically takes 5-20s; 60s
     accommodates the k3s sync lag).

  4. Wait for the writer pod to complete.

  5. Re-create a fresh pod with the same PVC. Read
     `/data/marker`. If the marker survived the pod churn,
     the LVM-thin volume is persistent; the CSI smoke is
     green.

  6. Cleanup (best-effort): delete the smoke namespace so
     future ci runs don't accumulate dead PVs. PVC
     `persistentvolume-protection` finalizer means the PV
     is deleted after the PVC; cluster ends with no leftover
     state from this phase.

Ordering
--------
Requires `helm_releases` (so proxmox-csi-plugin is installed)
and `topology_labels` (which guarantees the CSI node DaemonSet
is unblocked). On a re-run the phase no-ops via `should_run`
when `k3s.json.csi_smoke.completed_at` is set; until then it
will re-run the smoke (operator can rerun the smoke by
hand). The `phases_done` state-cache takes care of repeated
applies that target the same cluster.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


_SMOKE_NS = "proxmox-k3s-smoke"
_SMOKE_PVC = "smoke-pvc"
_SMOKE_WRITER = "smoke-write"
_SMOKE_READER = "smoke-read"
_MARKER = "proxmox-k3s-smoke-csi-marker"
_DEFAULT_SC = "proxmox-lvm-thin"
_PVC_BOUND_TIMEOUT_S = 60.0
_KUBECTL_TIMEOUT_S = 30.0


def _kubectl(
    kubeconfig: Path, *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    cmd = ["kubectl", "--kubeconfig", str(kubeconfig), *args]
    return subprocess.run(  # noqa: S603 -- operator-driven CLI
        cmd,
        check=check,
        text=True,
        timeout=_KUBECTL_TIMEOUT_S,
        capture_output=True,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )


def _kubectl_wait(
    kubeconfig: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """kubectl wait with a slightly larger timeout (PVC bind can
    take 30s on first apply; reader pod can take 60s if the
    PV/PVC pair is brand new and node mount needs a fresh
    device-mapper activation).
    """
    cmd = ["kubectl", "--kubeconfig", str(kubeconfig), *args]
    return subprocess.run(  # noqa: S603 -- operator-driven CLI
        cmd,
        check=False,
        text=True,
        timeout=90,
        capture_output=True,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )


def _assert_preflight(ctx: Container, kubeconfig: Path) -> None:
    """Raise BootstrapError if the cluster is not ready for a CSI smoke."""
    # StorageClass exists AND is default.
    sc = _kubectl(
        kubeconfig,
        "get",
        "sc",
        _DEFAULT_SC,
        "-o",
        "jsonpath={.metadata.annotations.storageclass\\.kubernetes\\.io/is-default-class}",
    )
    if sc.returncode != 0 or sc.stdout.strip().lower() != "true":
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": (
                    f"StorageClass {_DEFAULT_SC!r} missing or not marked "
                    "default; CSI smoke cannot proceed."
                ),
                "actual": sc.stdout.strip() or "<empty>",
                "resolution": (
                    "`kubectl get sc proxmox-lvm-thin -o yaml | grep "
                    "is-default-class`; the helm_releases phase should "
                    "have set this annotation. If absent, re-run "
                    "`make apply PHASES=helm_releases`."
                ),
            },
        )

    # Controller Deployment + node DaemonSet are Ready.
    rollout = _kubectl(
        kubeconfig,
        "rollout",
        "status",
        "-n",
        "proxmox-csi-plugin",
        "deploy/proxmox-csi-plugin-controller",
        "--timeout=60s",
    )
    if rollout.returncode != 0:
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": "proxmox-csi-plugin-controller not Ready",
                "detail": rollout.stderr.strip()[:300],
            },
        )
    rollout = _kubectl(
        kubeconfig,
        "rollout",
        "status",
        "-n",
        "proxmox-csi-plugin",
        "ds/proxmox-csi-plugin-node",
        "--timeout=60s",
    )
    if rollout.returncode != 0:
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": (
                    "proxmox-csi-plugin-node DaemonSet not Ready. "
                    "On single-node PVE without corosync the most "
                    "common cause is missing topology.kubernetes.io/"
                    "{region,zone} labels — see topology_labels phase."
                ),
                "detail": rollout.stderr.strip()[:300],
            },
        )

    # Every node carries both topology labels.
    nodes = _kubectl(kubeconfig, "get", "nodes", "-o", "json")
    import json as _json

    items = _json.loads(nodes.stdout).get("items") or []
    region = ctx.secrets.proxmox_region() or "proxmox-host"
    zone = ctx.secrets.proxmox_zone() or "BigBertha"
    for n in items:
        labels = n["metadata"].get("labels") or {}
        if (
            labels.get("topology.kubernetes.io/region") != region
            or labels.get("topology.kubernetes.io/zone") != zone
        ):
            raise BootstrapError(
                "csi_smoke",
                {
                    "reason": (
                        f"node {n['metadata']['name']!r} missing required "
                        f"topology labels (region={region}, zone={zone})"
                    ),
                    "resolution": (
                        "the topology_labels phase should have set "
                        "these; re-run `make apply PHASES=topology_labels`"
                    ),
                },
            )


def _apply_smoke_manifest(kubeconfig: Path) -> None:
    """Write the smoke manifest to disk and `kubectl apply` it."""
    smoke_dir = Path(__file__).resolve().parent.parent.parent / "manifests" / "_smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_yaml = smoke_dir / "csi-smoke.yaml"
    # Materialise once. `kubectl apply` is idempotent on namespaces
    # and PVCs (server-resolves name conflicts); the writer pod is
    # Recreate, so a re-apply always replaces it.
    if not smoke_yaml.exists():
        smoke_yaml.write_text(
            f"""---
apiVersion: v1
kind: Namespace
metadata:
  name: {_SMOKE_NS}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {_SMOKE_PVC}
  namespace: {_SMOKE_NS}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
  storageClassName: {_DEFAULT_SC}
---
apiVersion: v1
kind: Pod
metadata:
  name: {_SMOKE_WRITER}
  namespace: {_SMOKE_NS}
spec:
  restartPolicy: Never
  containers:
    - name: writer
      image: busybox:1.37
      command:
        - sh
        - -c
        - |
          echo {_MARKER} > /data/marker && sync && cat /data/marker
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: {_SMOKE_PVC}
"""
        )
        smoke_yaml.chmod(0o600)
    result = _kubectl(
        kubeconfig,
        "apply",
        "-n",
        _SMOKE_NS,
        "-f",
        str(smoke_yaml),
    )
    if result.returncode != 0:
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": "kubectl apply smoke manifest failed",
                "stderr": result.stderr.strip()[:500],
                "stdout": result.stdout.strip()[:500],
            },
        )


def _wait_pvc_bound(kubeconfig: Path) -> None:
    """Block until the smoke PVC is Bound (or timeout)."""
    result = _kubectl_wait(
        kubeconfig,
        "wait",
        "-n",
        _SMOKE_NS,
        "--for=jsonpath={.status.phase}=Bound",
        f"--timeout={int(_PVC_BOUND_TIMEOUT_S)}s",
        f"pvc/{_SMOKE_PVC}",
    )
    if result.returncode != 0:
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": (
                    f"PVC {_SMOKE_PVC} did not reach Bound within "
                    f"{int(_PVC_BOUND_TIMEOUT_S)}s; the cluster's "
                    "StorageClass/provisioner chain is broken."
                ),
                "detail": result.stderr.strip()[:500],
                "hint": (
                    "Check `kubectl -n proxmox-csi-plugin logs "
                    "proxmox-csi-plugin-controller-<id>` for the "
                    "CSI CreateVolume error; common causes: "
                    "missing topology labels, PVE token lacks the "
                    "CSI role, or the lvm-thin pool is full."
                ),
            },
        )


def _wait_writer_completed(kubeconfig: Path) -> None:
    """Block until the writer Pod terminates (Completed or Error)."""
    # `kubectl wait --for=jsonpath=...` against .status.phase works
    # for Succeeded / Failed / Running. busybox exits fast so this
    # completes in well under a minute.
    result = _kubectl_wait(
        kubeconfig,
        "wait",
        "-n",
        _SMOKE_NS,
        "--for=jsonpath={.status.phase}=Succeeded",
        "--timeout=60s",
        f"pod/{_SMOKE_WRITER}",
    )
    if result.returncode != 0:
        # Surface the pod's last state in the error so the
        # operator knows whether the writer ran but errored
        # vs. never scheduled.
        pod_state = _kubectl(
            kubeconfig, "-n", _SMOKE_NS, "get", "pod", _SMOKE_WRITER, "-o", "jsonpath={.status}"
        )
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": (
                    "writer pod did not reach Succeeded within 60s "
                    "(CSI CreateVolume/NodeStageVolume likely OK; "
                    "check pod state)"
                ),
                "writer_status": pod_state.stdout.strip()[:500],
                "wait_stderr": result.stderr.strip()[:500],
            },
        )


def _verify_marker_persists(kubeconfig: Path) -> str:
    """Recreate a reader pod against the same PVC and assert the marker."""
    reader_yaml = f"""---
apiVersion: v1
kind: Pod
metadata:
  name: {_SMOKE_READER}
  namespace: {_SMOKE_NS}
spec:
  restartPolicy: Never
  containers:
    - name: reader
      image: busybox:1.37
      command:
        - sh
        - -c
        - |
          echo "marker on disk:" && cat /data/marker && echo "expected:" && echo {_MARKER}
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: {_SMOKE_PVC}
"""
    # Apply via stdin (the YAML is small, generated inline).
    # The reader pod name is hard-coded; on retry the apply will
    # fail with "already exists" which is fine for our purposes.
    apply = subprocess.run(  # noqa: S603 -- operator-driven CLI
        ["kubectl", "--kubeconfig", str(kubeconfig), "apply", "-n", _SMOKE_NS, "-f", "-"],
        input=reader_yaml,
        text=True,
        check=False,
        timeout=_KUBECTL_TIMEOUT_S,
        capture_output=True,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )
    if apply.returncode != 0 and "already exists" not in (apply.stdout + apply.stderr):
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": "kubectl apply reader pod failed",
                "stderr": apply.stderr.strip()[:500],
            },
        )

    wait_result = _kubectl_wait(
        kubeconfig,
        "wait",
        "-n",
        _SMOKE_NS,
        "--for=jsonpath={.status.phase}=Succeeded",
        "--timeout=60s",
        f"pod/{_SMOKE_READER}",
    )
    if wait_result.returncode != 0:
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": (
                    "reader pod did not reach Succeeded within 60s. "
                    "If the writer pod succeeded but the reader "
                    "hangs, the volume was bound but the kubelet "
                    "could not mount it (NodeStageVolume failure)."
                ),
                "wait_stderr": wait_result.stderr.strip()[:500],
            },
        )

    logs = _kubectl(kubeconfig, "-n", _SMOKE_NS, "logs", _SMOKE_READER)
    if _MARKER not in logs.stdout:
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": (
                    "the marker file from the writer pod did NOT "
                    "survive pod churn. CSI volume is not "
                    "persistent across pods (or the reader pod "
                    "mounted a different PV)."
                ),
                "reader_logs": logs.stdout.strip()[:500],
                "expected_marker": _MARKER,
            },
        )
    return logs.stdout.strip()


def _cleanup(kubeconfig: Path) -> None:
    """Best-effort namespace delete — wait for the ns to be
    fully gone so a follow-up apply in a chained phase doesn't
    hit `namespace is being terminated`.

    The PVC's `kubernetes.io/pvc-protection` finalizer means
    the namespace itself may stall at Terminating for ~30s
    while k8s deletes the PV; that's fine. We poll for up to
    60s; if it's still terminating after that, the next
    phase will see the ns gone (k8s returns 404 on
    NotFound, which our `apply` tolerates via the
    `namespace unchanged` stdout).
    """
    subprocess.run(  # noqa: S603 -- best-effort cleanup
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "delete",
            "ns",
            _SMOKE_NS,
            "--ignore-not-found",
            "--wait=true",
            "--timeout=60s",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=75,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )


@register
class CsiSmokePhase(Phase):
    """Functional smoke test: PVC -> LVM-thin -> mount -> marker persists."""

    name = "csi_smoke"
    requires = ("helm_releases",)

    def should_run(self, ctx: Container) -> bool:
        # Re-runs of `make apply` re-run the smoke; the smoke
        # itself is idempotent (PV is deleted by the PVC
        # finalizer on cleanup; the smoke namespace is
        # short-lived). An operator who wants to skip the smoke
        # on a particular cluster can drop it from the phase
        # list.
        return True

    def run(self, ctx: Container) -> PhaseResult:
        kubeconfig = ctx.cluster_dir / "kubeconfig.yaml"
        if not kubeconfig.exists():
            raise BootstrapError(
                "csi_smoke",
                {
                    "reason": "kubeconfig.yaml missing",
                    "expected": str(kubeconfig),
                    "resolution": (
                        "kubeconfig_pull phase must run first "
                        "(check phase ordering)"
                    ),
                },
            )

        _assert_preflight(ctx, kubeconfig)
        ctx.logger.info("csi_smoke.preflight_ok")

        _apply_smoke_manifest(kubeconfig)
        _wait_pvc_bound(kubeconfig)
        _wait_writer_completed(kubeconfig)
        ctx.logger.info("csi_smoke.writer_ok", namespace=_SMOKE_NS, pvc=_SMOKE_PVC)

        reader_logs = _verify_marker_persists(kubeconfig)
        ctx.logger.info(
            "csi_smoke.marker_persists",
            marker=_MARKER,
            # Truncate so the audit-log line stays manageable.
            reader_excerpt=reader_logs[:200].replace("\n", " | "),
        )

        _cleanup(kubeconfig)
        return PhaseResult.make_done(
            "csi_smoke",
            namespace=_SMOKE_NS,
            pvc=_SMOKE_PVC,
            storage_class=_DEFAULT_SC,
            marker=_MARKER,
        )
