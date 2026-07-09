"""envoy_smoke phase — functional smoke test for Envoy Gateway.

Why
---
The `helm_releases` phase installs envoy-gateway v1.8.2 with
`crds.enabled=false` and `crds.gatewayAPI.safeUpgradePolicy
.enabled=false`. The Gateway API standard-channel CRDs are
applied separately (in `gateway_crds` phase) so a future
chart bump can't silently upgrade the CRD schema. **BUT** the
helm chart's `crds.enabled=false` also disables the bootstrap
GatewayClass resource that the controller would otherwise
create on startup. Without it, the controller logs
`no accepted gatewayclass` forever and never reconciles
Gateways.

The bootstrap's envoy-gateway controller logs (live cluster,
2026-07-09T21:33Z):
    info provider kubernetes/controller.go:338
    no accepted gatewayclass {"runner": "provider"}

This phase pins the live behaviour:

  1. Pre-flight — assert Gateway API CRDs are installed AND
     the Envoy Gateway CRDs are present
     (envoyproxies.gateway.envoyproxy.io, etc. — required
     for the controller's CRD-backed status reporting).

  2. Live fix — if `GatewayClass=envoy` is missing, create it
     with `controllerName:
     gateway.envoyproxy.io/gatewayclass-controller`. This
     matches the controller's pinned value from the helm
     values (`config.envoyGateway.gateway.controllerName`).
     Idempotent on a re-run (kubectl apply tolerates an
     already-present resource via server-side conflict
     resolution).

  3. Apply — Namespace + echo Deployment (hashicorp/http-echo)
     + Service + Gateway + HTTPRoute in `proxmox-k3s-smoke`.
     The Gateway uses `gatewayClassName: envoy` and exposes a
     plain HTTP listener on port 80; the HTTPRoute matches
     `PathPrefix=/` and forwards to the echo service.

  4. Wait — until `Gateway/smoke-gw` reaches Programmed=True
     (60s budget). Programmed=True means the controller has
     reconciled and provisioned its data-plane Service.

  5. Discover — the data-plane Service ClusterIP via the
     `gateway.envoyproxy.io/owning-gateway-name=smoke-gw`
     label that Envoy Gateway stamps on its data-plane
     Services.

  6. Curl — `kubectl run --rm` a busybox wget pod that hits
     `http://<data-plane ClusterIP>/` and assert the body is
     the exact echo string. We use busybox + wget rather
     than `kubectl exec ... curl` because the data-plane is
     a ClusterIP service only; you have to be inside the
     cluster to reach it.

  7. Cleanup — delete the smoke namespace. Best-effort;
     failure doesn't fail the phase (the smoke already
     passed).

This is the live equivalent of the cicd repo's
`tools/bootstrap_cluster.py::_run_gateway_smoke`, but written
as a @register Phase so it lands in the orchestrator's
topological order automatically and the audit log captures
every step.

Ordering
--------
Requires `helm_releases` (so envoy-gateway is installed and
the controller is up). Runs after `csi_smoke` so the cluster
logs show both smoke phases in sequence (csi then envoy);
the actual dependency is just on helm_releases.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from ..container import Container
from ..phases.base import Phase, PhaseResult, register
from ..protocols import BootstrapError


_SMOKE_NS = "proxmox-k3s-smoke"
_SMOKE_GATEWAY = "smoke-gw"
_ECHO_BODY = "proxmox-k3s-smoke-envoy-gateway"
_CONTROLLER_NAME = "gateway.envoyproxy.io/gatewayclass-controller"
_GATEWAY_PROGRAMMED_TIMEOUT_S = 60.0
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
    cmd = ["kubectl", "--kubeconfig", str(kubeconfig), *args]
    return subprocess.run(  # noqa: S603 -- operator-driven CLI
        cmd,
        check=False,
        text=True,
        timeout=120,
        capture_output=True,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )


def _ensure_gateway_class(kubeconfig: Path) -> None:
    """Ensure GatewayClass=envoy exists with the right controllerName.

    Live fix for the `crds.enabled=false` issue (see module
    docstring). Idempotent: kubectl apply tolerates an existing
    resource whose fields match.
    """
    gc = _kubectl(
        kubeconfig,
        "get",
        "gatewayclass",
        "envoy",
        "-o",
        "jsonpath={.spec.controllerName}",
    )
    if gc.returncode == 0 and gc.stdout.strip() == _CONTROLLER_NAME:
        # Already present and correct.
        return
    manifest = (
        "apiVersion: gateway.networking.k8s.io/v1\n"
        "kind: GatewayClass\n"
        "metadata:\n"
        "  name: envoy\n"
        "spec:\n"
        f"  controllerName: {_CONTROLLER_NAME}\n"
    )
    apply = subprocess.run(  # noqa: S603 -- operator-driven CLI
        ["kubectl", "--kubeconfig", str(kubeconfig), "apply", "-f", "-"],
        input=manifest,
        check=False,
        text=True,
        timeout=_KUBECTL_TIMEOUT_S,
        capture_output=True,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )
    if apply.returncode != 0:
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": "could not create GatewayClass=envoy",
                "stderr": apply.stderr.strip()[:500],
                "stdout": apply.stdout.strip()[:500],
                "hint": (
                    "Are the Gateway API CRDs installed? The "
                    "gateway_crds phase should have applied them. "
                    "Re-run `make apply PHASES=gateway_crds`."
                ),
            },
        )


def _assert_preflight(kubeconfig: Path) -> None:
    """Assert the cluster is ready for an envoy smoke.

    1. Envoy Gateway controller Pod is Ready.
    2. GatewayClass=envoy exists (created above if missing).
    3. Gateway API CRDs installed (smoke manifest uses them).
    """
    # Controller Ready.
    rollout = _kubectl(
        kubeconfig,
        "rollout",
        "status",
        "-n",
        "envoy-gateway-system",
        "deploy/envoy-gateway",
        "--timeout=60s",
    )
    if rollout.returncode != 0:
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": "envoy-gateway controller not Ready",
                "detail": rollout.stderr.strip()[:500],
            },
        )

    # Gateway API CRDs installed (otherwise manifest apply fails).
    crd = _kubectl(
        kubeconfig,
        "get",
        "crd",
        "gateways.gateway.networking.k8s.io",
        "-o",
        "jsonpath={.metadata.name}",
    )
    if crd.returncode != 0:
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": (
                    "Gateway API CRDs missing (no "
                    "gateways.gateway.networking.k8s.io). The "
                    "gateway_crds phase should have applied them."
                ),
                "resolution": "Re-run `make apply PHASES=gateway_crds`.",
            },
        )


def _apply_smoke_manifest(kubeconfig: Path) -> None:
    """Write the smoke manifest to disk and `kubectl apply` it.

    hashicorp/http-echo writes `args.text` body on every request
    and returns 200 OK. Using it (instead of busybox's httpd)
    removes a class of "did the smoke even bring up the right
    server" debugging.
    """
    smoke_dir = Path(__file__).resolve().parent.parent.parent / "manifests" / "_smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_yaml = smoke_dir / "envoy-gateway-smoke.yaml"
    if not smoke_yaml.exists():
        smoke_yaml.write_text(
            f"""---
apiVersion: v1
kind: Namespace
metadata:
  name: {_SMOKE_NS}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: echo
  namespace: {_SMOKE_NS}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: echo
  template:
    metadata:
      labels:
        app: echo
    spec:
      containers:
        - name: echo
          image: hashicorp/http-echo:0.2.3
          args: ["-text={_ECHO_BODY}"]
          ports:
            - containerPort: 5678
---
apiVersion: v1
kind: Service
metadata:
  name: echo
  namespace: {_SMOKE_NS}
spec:
  selector:
    app: echo
  ports:
    - port: 5678
      targetPort: 5678
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: {_SMOKE_GATEWAY}
  namespace: {_SMOKE_NS}
spec:
  gatewayClassName: envoy
  listeners:
    - name: http
      port: 80
      protocol: HTTP
      allowedRoutes:
        namespaces:
          from: Same
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: smoke
  namespace: {_SMOKE_NS}
spec:
  parentRefs:
    - name: {_SMOKE_GATEWAY}
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: echo
          port: 5678
"""
        )
        smoke_yaml.chmod(0o600)
    result = _kubectl(
        kubeconfig, "apply", "-f", str(smoke_yaml)
    )
    if result.returncode != 0:
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": "kubectl apply smoke manifest failed",
                "stderr": result.stderr.strip()[:500],
                "stdout": result.stdout.strip()[:500],
            },
        )

    # The envoy-gateway controller's status updater has a
    # `status unchanged, bypassing update` fast-path. If the
    # Gateway was applied once (and reached a non-Programmed
    # state) and then the data plane was provisioned later,
    # the controller's in-memory diff says "no change" and
    # never writes the updated status back. This bites
    # retry runs of the smoke phase: the previous run's
    # Gateway is still around with stale conditions.
    #
    # Workaround: delete the Gateway (and its data-plane
    # Service) before re-applying, so the controller sees a
    # brand-new resource with no in-memory baseline to diff
    # against.
    _kubectl(
        kubeconfig,
        "delete",
        "gateway",
        _SMOKE_GATEWAY,
        "-n",
        _SMOKE_NS,
        "--ignore-not-found",
        "--wait=false",
    )
    # The HTTPRoute gets cascade-deleted by the Gateway
    # owner reference; the data-plane Service and Pod
    # cascade-delete from the Gateway's
    # `gateway.envoyproxy.io/owning-gateway-name` selector.
    # Give the apiserver a moment to GC the data-plane
    # Service so the new Gateway gets a fresh ClusterIP.
    time.sleep(2)
    result = _kubectl(
        kubeconfig, "apply", "-f", str(smoke_yaml)
    )
    if result.returncode != 0:
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": "kubectl apply smoke manifest (post-delete) failed",
                "stderr": result.stderr.strip()[:500],
                "stdout": result.stdout.strip()[:500],
            },
        )


def _wait_data_plane_ready(kubeconfig: Path) -> None:
    """Block until the data-plane Deployment is Available (or timeout).

    The envoy-gateway controller stamps its data-plane
    resources with the
    `gateway.envoyproxy.io/owning-gateway-name=<gateway>`
    label. We wait on the Deployment of that label reaching
    `Available=True` because (a) it's a real signal that the
    Envoy proxy is up and listening, and (b) it sidesteps
    the envoy-gateway 1.8 controller's `status unchanged,
    bypassing update` quirk where a Gateway's
    `status.conditions[].Programmed` gets stuck on
    `AddressNotAssigned` even when the data-plane Service
    has been provisioned (the controller's in-memory diff
    says "no change" and never writes the new status).
    """
    # The data-plane resources live in the same namespace as
    # the controller (envoy-gateway-system).
    result = _kubectl_wait(
        kubeconfig,
        "wait",
        "-n",
        "envoy-gateway-system",
        "--for=condition=Available=true",
        f"--timeout={int(_GATEWAY_PROGRAMMED_TIMEOUT_S)}s",
        "deploy",
        "-l",
        f"gateway.envoyproxy.io/owning-gateway-name={_SMOKE_GATEWAY}",
    )
    if result.returncode != 0:
        deploys = _kubectl(
            kubeconfig,
            "-n",
            "envoy-gateway-system",
            "get",
            "deploy",
            "-l",
            f"gateway.envoyproxy.io/owning-gateway-name={_SMOKE_GATEWAY}",
            "-o",
            "wide",
        )
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": (
                    f"data-plane Deployment for {_SMOKE_GATEWAY} "
                    f"did not become Available within "
                    f"{int(_GATEWAY_PROGRAMMED_TIMEOUT_S)}s. The "
                    f"controller may not have reconciled the "
                    f"Gateway (check the GatewayClass and the "
                    f"envoy-gateway controller logs)."
                ),
                "deploy_state": deploys.stdout.strip()[:500],
                "wait_stderr": result.stderr.strip()[:500],
            },
        )


def _data_plane_cluster_ip(kubeconfig: Path) -> str:
    """Discover the data-plane Service ClusterIP via owning-gateway-name label."""
    result = _kubectl(
        kubeconfig,
        "get",
        "svc",
        "-n",
        "envoy-gateway-system",
        "-l",
        f"gateway.envoyproxy.io/owning-gateway-name={_SMOKE_GATEWAY}",
        "-o",
        "jsonpath={.items[0].spec.clusterIP}",
    )
    cluster_ip = result.stdout.strip()
    if result.returncode != 0 or not cluster_ip:
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": (
                    f"no data-plane Service in envoy-gateway-system "
                    f"with owning-gateway-name={_SMOKE_GATEWAY}. "
                    "Did the Gateway reach Programmed?"
                ),
                "kubectl_stderr": result.stderr.strip()[:500],
            },
        )
    return cluster_ip


def _curl_via_wget_pod(kubeconfig: Path, cluster_ip: str) -> str:
    """Run wget from a busybox pod against the data-plane ClusterIP.

    We create a one-shot Pod from a YAML manifest, wait for
    it to complete, then `kubectl logs` the response body.
    `kubectl run --rm` only works for attached containers
    (it requires a TTY), so the manifest approach is the
    only way to do this non-interactively from a script.
    """
    pod_yaml = f"""---
apiVersion: v1
kind: Pod
metadata:
  name: smoke-curl
  namespace: {_SMOKE_NS}
spec:
  restartPolicy: Never
  containers:
    - name: curl
      image: busybox:1.37
      command:
        - sh
        - -c
        - "wget -qO- http://{cluster_ip}/"
"""
    apply = subprocess.run(  # noqa: S603 -- operator-driven CLI
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "apply",
            "-n",
            _SMOKE_NS,
            "-f",
            "-",
        ],
        input=pod_yaml,
        text=True,
        check=False,
        timeout=30,
        capture_output=True,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )
    if apply.returncode != 0 and "already exists" not in (
        apply.stdout + apply.stderr
    ):
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": "kubectl apply smoke-curl pod failed",
                "kubectl_stderr": apply.stderr.strip()[:500],
                "kubectl_stdout": apply.stdout.strip()[:500],
            },
        )

    # Wait for the pod to complete.
    wait_result = _kubectl_wait(
        kubeconfig,
        "wait",
        "-n",
        _SMOKE_NS,
        "--for=jsonpath=.status.phase=Succeeded",
        "--timeout=30s",
        "pod/smoke-curl",
    )
    if wait_result.returncode != 0:
        # Surface pod status and logs on failure.
        pod_state = _kubectl(
            kubeconfig,
            "-n",
            _SMOKE_NS,
            "get",
            "pod",
            "smoke-curl",
            "-o",
            "wide",
        )
        pod_logs = _kubectl(
            kubeconfig,
            "-n",
            _SMOKE_NS,
            "logs",
            "smoke-curl",
        )
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": "smoke-curl pod did not reach Succeeded within 30s",
                "pod_state": pod_state.stdout.strip()[:500],
                "pod_logs": pod_logs.stdout.strip()[:500],
                "wait_stderr": wait_result.stderr.strip()[:500],
            },
        )

    logs = _kubectl(
        kubeconfig,
        "-n",
        _SMOKE_NS,
        "logs",
        "smoke-curl",
    )
    # Best-effort cleanup of the one-shot pod.
    _kubectl(
        kubeconfig,
        "delete",
        "pod",
        "smoke-curl",
        "-n",
        _SMOKE_NS,
        "--ignore-not-found",
        "--wait=false",
    )
    if logs.returncode != 0:
        raise BootstrapError(
            "envoy_smoke",
            {
                "reason": "kubectl logs smoke-curl failed",
                "kubectl_stderr": logs.stderr.strip()[:500],
            },
        )
    return logs.stdout.strip()


def _cleanup(kubeconfig: Path) -> None:
    """Best-effort namespace delete — wait for the ns to be
    fully gone so a follow-up apply in a chained phase doesn't
    hit `namespace is being terminated`.

    The ns may take a few seconds to fully terminate after the
    data-plane Service and Pods are GC'd (k8s ns controller
    finalizes once the namespace's contents are deleted). We
    poll for up to 30s; if it's still terminating after that,
    the next phase will see the ns gone (k8s returns 404 on
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
            "--timeout=30s",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
    )


@register
class EnvoySmokePhase(Phase):
    """Functional smoke test: Gateway -> HTTPRoute -> echo -> curl asserts body."""

    name = "envoy_smoke"
    requires = ("helm_releases",)

    def should_run(self, ctx: Container) -> bool:
        return True

    def run(self, ctx: Container) -> PhaseResult:
        kubeconfig = ctx.cluster_dir / "kubeconfig.yaml"
        if not kubeconfig.exists():
            raise BootstrapError(
                "envoy_smoke",
                {
                    "reason": "kubeconfig.yaml missing",
                    "expected": str(kubeconfig),
                    "resolution": (
                        "kubeconfig_pull phase must run first "
                        "(check phase ordering)"
                    ),
                },
            )

        _assert_preflight(kubeconfig)
        _ensure_gateway_class(kubeconfig)
        ctx.logger.info("envoy_smoke.preflight_ok")

        _apply_smoke_manifest(kubeconfig)
        _wait_data_plane_ready(kubeconfig)
        ctx.logger.info(
            "envoy_smoke.data_plane_ready", gateway=_SMOKE_GATEWAY
        )

        cluster_ip = _data_plane_cluster_ip(kubeconfig)
        body = _curl_via_wget_pod(kubeconfig, cluster_ip)
        ctx.logger.info(
            "envoy_smoke.curl_ok",
            data_plane=cluster_ip,
            body=body[:120].replace("\n", " | "),
        )

        if body != _ECHO_BODY:
            # Body mismatch is a real functional failure: data
            # plane is up but didn't match the HTTPRoute's
            # routing rule. Operator should investigate
            # `kubectl get httproute -n proxmox-k3s-smoke -o yaml`
            # to confirm the route's parentRefs and matches.
            _cleanup(kubeconfig)
            raise BootstrapError(
                "envoy_smoke",
                {
                    "reason": "echo body mismatch; data plane is up but did not match the HTTPRoute",
                    "expected": _ECHO_BODY,
                    "actual": body[:500],
                    "data_plane_cluster_ip": cluster_ip,
                },
            )

        _cleanup(kubeconfig)
        return PhaseResult.make_done(
            "envoy_smoke",
            namespace=_SMOKE_NS,
            gateway=_SMOKE_GATEWAY,
            data_plane=cluster_ip,
            body=_ECHO_BODY,
        )
