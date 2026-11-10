"""The scratch namespace: where the platform suites create the objects they test with.

Everything a run creates carries two labels, the release and the run ID, and is deleted when
the run ends. A run also starts by deleting whatever an interrupted run left behind.

Test pods run the probe's own image with `python -m platform_probe task <name> <args>` (see
tasks.py) and hand their result back in the container's termination message, so no exec, logs
or extra image are needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import quote

from . import kube
from .kube import StatusError
from .runner import Probe, ProbeError

log = logging.getLogger(__name__)

LABEL_RELEASE = "platform-probe/release"
LABEL_RUN = "platform-probe/run"
CA_DIR = "/etc/platform-probe/ca"
CA_FILE = CA_DIR + "/ca-bundle.crt"

# What a run may create, in the order the sweep deletes it: pods first, so volumes and
# snapshots are no longer in use.
KINDS = (
    "/api/v1/namespaces/{ns}/pods",
    "/apis/route.openshift.io/v1/namespaces/{ns}/routes",
    "/apis/cert-manager.io/v1/namespaces/{ns}/certificates",
    "/apis/snapshot.storage.k8s.io/v1/namespaces/{ns}/volumesnapshots",
    "/api/v1/namespaces/{ns}/persistentvolumeclaims",
)


def ns(p: Probe) -> str:
    return p.cfg.scratch.namespace


def path(p: Probe, collection: str, name: str = "") -> str:
    """An API path in the scratch namespace, e.g. path(p, "/api/v1/pods", "x")."""
    prefix, _, resource = collection.rpartition("/")
    return f"{prefix}/namespaces/{ns(p)}/{resource}" + (f"/{name}" if name else "")


def labels(p: Probe) -> dict[str, str]:
    return {LABEL_RELEASE: p.cfg.release, LABEL_RUN: p.run_id, "sidecar.istio.io/inject": "false"}


def name(p: Probe, suffix: str) -> str:
    """A name unique to this run: probe-<suffix>-<run ID>, valid as a DNS label."""
    suffix = re.sub(r"[^a-z0-9-]+", "-", suffix.lower()).strip("-")
    return f"probe-{suffix}"[: 62 - len(p.run_id)] + "-" + p.run_id


def meta(p: Probe, obj_name: str, **extra) -> dict:
    return {"name": obj_name, "labels": labels(p), **extra}


async def sweep(p: Probe) -> None:
    """Delete everything this release created in the scratch namespace. An API that isn't
    installed (cert-manager, snapshots) or isn't allowed is skipped."""
    selector = quote(f"{LABEL_RELEASE}={p.cfg.release}", safe="")
    # Test Applications live in their Argo CD instance's namespace, not the scratch one.
    argo = [
        f"/apis/argoproj.io/v1alpha1/namespaces/{i.namespace}/applications"
        for i in p.cfg.suites.argocd.instances
        if p.cfg.suites.argocd.enabled and i.deploy.enabled
    ]
    for base in argo + [c.format(ns=ns(p)) for c in KINDS]:
        try:
            lst = await p.kube.get(f"{base}?labelSelector={selector}")
        except StatusError as e:
            if e.code not in (403, 404):
                log.warning("sweep %s: %s", base, e)
            continue
        for it in kube.items(lst):
            try:
                await p.kube.delete(f"{base}/{kube.name(it)}")
            except StatusError as e:
                log.warning("sweep %s/%s: %s", base, kube.name(it), e)


# How often wait_for polls the API.
POLL = 2.0


async def wait_for(fetch: Callable[[], Awaitable], done: Callable, timeout: float, interval: float | None = None):
    """Poll fetch() until done(result) holds or the timeout passes. Returns the last result."""
    deadline = time.monotonic() + timeout
    while True:
        result = await fetch()
        if done(result) or time.monotonic() >= deadline:
            return result
        await asyncio.sleep(POLL if interval is None else interval)


def restricted_container() -> dict:
    """A container security context restricted-v2 and PSA "restricted" both accept."""
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}


def pod_spec(p: Probe, container: dict, volumes: list[dict] | None = None) -> dict:
    spec = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [
            {
                "image": p.cfg.image,
                "imagePullPolicy": "IfNotPresent",
                "securityContext": restricted_container(),
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                **container,
            }
        ],
        "volumes": list(volumes or []),
    }
    if p.cfg.scratch.image_pull_secrets:
        spec["imagePullSecrets"] = [{"name": s} for s in p.cfg.scratch.image_pull_secrets]
    return spec


def ca_volume(p: Probe) -> tuple[list[dict], list[dict]]:
    """The CA bundle ConfigMap as a volume and its mount, or nothing without one."""
    if not p.cfg.scratch.ca_config_map:
        return [], []
    vol = {
        "name": "ca",
        "configMap": {
            "name": p.cfg.scratch.ca_config_map,
            "optional": True,
            "items": [{"key": p.cfg.scratch.ca_key, "path": "ca-bundle.crt"}],
        },
    }
    return [vol], [{"name": "ca", "mountPath": CA_DIR, "readOnly": True}]


@dataclass
class PodResult:
    phase: str = ""
    exit_code: int | None = None
    # The task's result, from the termination message; empty if it didn't finish.
    data: dict = field(default_factory=dict)
    # Why it didn't finish: a waiting reason (ImagePullBackOff), Unschedulable, a timeout.
    reason: str = ""
    pod: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.phase == "Succeeded" and not self.data.get("error")

    def explain(self) -> str:
        """What went wrong, for a check's detail."""
        if self.data.get("error"):
            return str(self.data["error"])
        return self.reason or f"pod {self.phase or 'not started'}"


def waiting_reason(pod: dict) -> str:
    st = pod.get("status") or {}
    for cs in st.get("containerStatuses") or []:
        w = (cs.get("state") or {}).get("waiting")
        if w and w.get("reason") not in (None, "ContainerCreating"):
            return f"{w.get('reason')}: {w.get('message', '')}".rstrip(": ")
    sched = kube.find(kube.conditions(st.get("conditions")), "PodScheduled")
    if sched.status == "False":
        return f"{sched.reason}: {sched.message}"
    for cs in st.get("containerStatuses") or []:
        if (cs.get("state") or {}).get("waiting"):
            return "ContainerCreating (a volume, secret or image still pending)"
    return ""


async def run_task(
    p: Probe,
    suffix: str,
    task: str,
    args: dict | None = None,
    *,
    volumes: list[dict] | None = None,
    mounts: list[dict] | None = None,
    timeout: float = 120.0,
) -> PodResult:
    """Run one task pod to completion and return its result. The pod is deleted afterwards."""
    ca_vols, ca_mounts = ca_volume(p)
    pod_name = name(p, suffix)
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": meta(p, pod_name),
        "spec": pod_spec(
            p,
            {
                "name": "task",
                "args": ["task", task, json.dumps(args or {})],
                "volumeMounts": list(mounts or []) + ca_mounts,
                "terminationMessagePolicy": "FallbackToLogsOnError",
            },
            list(volumes or []) + ca_vols,
        ),
    }
    await p.kube.create(path(p, "/api/v1/pods"), pod)
    try:
        final = await wait_for(
            lambda: p.kube.get(path(p, "/api/v1/pods", pod_name)),
            lambda o: (o.get("status") or {}).get("phase") in ("Succeeded", "Failed"),
            timeout,
        )
    finally:
        # Deleted whatever happened; the sweep catches it if this fails.
        try:
            await p.kube.delete(path(p, "/api/v1/pods", pod_name))
        except Exception:
            log.warning("deleting pod %s", pod_name, exc_info=True)
    st = final.get("status") or {}
    res = PodResult(phase=st.get("phase", ""), pod=final)
    for cs in st.get("containerStatuses") or []:
        term = (cs.get("state") or {}).get("terminated")
        if term:
            res.exit_code = term.get("exitCode")
            try:
                data = json.loads(term.get("message") or "")
                res.data = data if isinstance(data, dict) else {}
            except ValueError:
                res.reason = (term.get("message") or term.get("reason") or "").strip()[-300:]
    if res.phase not in ("Succeeded", "Failed"):
        res.reason = f"didn't finish in {timeout:.0f}s: " + (waiting_reason(final) or f"pod {res.phase}")
    return res


async def ensure_backend(p: Probe) -> str:
    """The pod the ingress and MetalLB checks send traffic to, started once per run behind the
    Services the chart creates (the console may not create Services), which select it by the
    platform-probe/backend label: the probe's own pricing service on HTTP 8080, and, when the
    ingress backend Service exists, on HTTPS 8443 with its certificate from OpenShift's service
    CA. Returns the ingress backend Service name, or "" without one."""
    if "backend" in p.state:
        if isinstance(p.state["backend"], Exception):
            raise p.state["backend"]
        return p.state["backend"]
    svc = p.cfg.scratch.backend_service
    pod_name = name(p, "backend")
    try:
        env = [{"name": "ROLE", "value": "pricing"}, {"name": "VERSION", "value": "backend"}]
        mounts, volumes = [], []
        if svc:
            # Optional, so a run doesn't hang where the service CA hasn't issued it (yet).
            env += [
                {"name": "TLS_CERT_FILE", "value": "/tls/tls.crt"},
                {"name": "TLS_KEY_FILE", "value": "/tls/tls.key"},
            ]
            mounts = [{"name": "tls", "mountPath": "/tls", "readOnly": True}]
            volumes = [{"name": "tls", "secret": {"secretName": svc + "-tls", "optional": True}}]
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": meta(p, pod_name),
            "spec": pod_spec(
                p,
                {
                    "name": "backend",
                    "env": env,
                    "ports": [{"name": "http", "containerPort": 8080}, {"name": "https", "containerPort": 8443}],
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 2},
                    "volumeMounts": mounts,
                },
                volumes,
            ),
        }
        pod["metadata"]["labels"]["platform-probe/backend"] = "true"
        pod["spec"]["restartPolicy"] = "Always"
        await p.kube.create(path(p, "/api/v1/pods"), pod)
        final = await wait_for(
            lambda: p.kube.get(path(p, "/api/v1/pods", pod_name)),
            lambda o: kube.find(kube.conditions((o.get("status") or {}).get("conditions")), "Ready").status == "True",
            120,
        )
        if kube.find(kube.conditions((final.get("status") or {}).get("conditions")), "Ready").status != "True":
            raise ProbeError("the test backend didn't become ready in 120s: " + (waiting_reason(final) or "not ready"))
    except Exception as e:
        p.state["backend"] = e
        raise
    p.state["backend"] = svc
    return svc


def parse_quantity(q: str) -> float:
    """A Kubernetes quantity such as 1Gi, 500M or 1073741824, in base units."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KMGTPE]i?|m|k)?\s*", str(q))
    if not m:
        raise ValueError(f"invalid quantity {q!r}")
    n, unit = float(m.group(1)), m.group(2) or ""
    if unit == "m":
        return n / 1000
    if unit == "k":
        return n * 1000
    if unit.endswith("i"):
        return n * 1024 ** ("KMGTPE".index(unit[0]) + 1)
    if unit:
        return n * 1000 ** ("KMGTPE".index(unit) + 1)
    return n
