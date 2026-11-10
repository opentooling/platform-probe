"""Argo CD: every instance (e.g. one for the platform, one for teams' applications) is up, its
Applications are synced and healthy, and, where switched on, a team can deploy through it.

The deploy check creates an Application in the instance's namespace that syncs a small
ConfigMap from Git (argocd/probe-app in this repository, or your mirror of it) into the scratch
namespace, waits until it is Synced and Healthy, and deletes it with Argo CD's resources
finalizer, so Argo CD prunes what it deployed.
"""

from __future__ import annotations

import time

from .. import kube, scratch
from ..config import ArgoInstance
from ..kube import StatusError
from ..runner import GROUP_ARGOCD, Outcome, Probe, Status, check, failed, passed, skipped

SUITE = "argocd"
APPS = "/apis/argoproj.io/v1alpha1/namespaces/{ns}/applications"
ARGOCDS = "/apis/argoproj.io/v1beta1/namespaces/{ns}/argocds"
COMPONENTS = ("server", "repo", "applicationController", "redis")
FINALIZER = "resources-finalizer.argocd.argoproj.io"
# Shown per instance when there is more to say than fits.
MAX_LISTED = 5


def _instances(p: Probe) -> list[ArgoInstance]:
    return p.cfg.suites.argocd.instances


def _join(results: list[tuple[bool, str]]) -> Outcome:
    """One outcome from per-instance results: pass only if every instance passed."""
    bad = [d for ok, d in results if not ok]
    if bad:
        return failed("; ".join(bad))
    return passed("; ".join(d for _, d in results))


@check("argocd-instances", GROUP_ARGOCD, "Argo CD instances available", SUITE)
async def check_instances(p: Probe) -> Outcome:
    if not _instances(p):
        return failed("suites.argocd.instances is empty")
    results = []
    for inst in _instances(p):
        try:
            found = kube.items(await p.kube.get(ARGOCDS.format(ns=inst.namespace)))
        except StatusError as e:
            if e.code == 404:
                results.append((False, f"{inst.name}: no argocds.argoproj.io API (is the GitOps operator installed?)"))
            else:
                results.append((False, f"{inst.name}: {e}"))
            continue
        cr = next((a for a in found if not inst.argocd or kube.name(a) == inst.argocd), None)
        if cr is None:
            results.append((False, f"{inst.name}: no ArgoCD {inst.argocd or 'resource'} in {inst.namespace}"))
            continue
        st = cr.get("status") or {}
        down = [f"{c} {st.get(c)}" for c in COMPONENTS if st.get(c) not in (None, "Running")]
        if st.get("phase") != "Available" or down:
            results.append(
                (False, f"{inst.name}: phase {st.get('phase', 'unknown')}" + (f", {', '.join(down)}" if down else ""))
            )
        else:
            results.append((True, f"{inst.name}: available"))
    return _join(results)


def _app_problem(app: dict, out_of_sync_fails: bool) -> str:
    """Why an Application counts as broken, or ""."""
    st = app.get("status") or {}
    health = (st.get("health") or {}).get("status", "")
    sync = (st.get("sync") or {}).get("status", "")
    op = st.get("operationState") or {}
    errors = [c for c in st.get("conditions") or [] if str(c.get("type", "")).endswith("Error")]
    if errors:
        return f"{errors[0].get('type')}: {str(errors[0].get('message', ''))[:120]}"
    if op.get("phase") in ("Failed", "Error"):
        return f"sync {op['phase']}: {str(op.get('message', ''))[:120]}"
    if health in ("Degraded", "Missing"):
        return health
    if out_of_sync_fails and sync == "OutOfSync":
        return "OutOfSync"
    return ""


@check("argocd-applications", GROUP_ARGOCD, "Argo CD Applications synced and healthy", SUITE)
async def check_applications(p: Probe) -> Outcome:
    instances = [i for i in _instances(p) if i.applications]
    if not instances:
        return skipped("no instance has applications checks on")
    results = []
    for inst in instances:
        try:
            apps = kube.items(await p.kube.get(APPS.format(ns=inst.namespace)))
        except StatusError as e:
            results.append((False, f"{inst.name}: {e}"))
            continue
        # The probe's own test Applications come and go; they have their own check.
        apps = [
            a
            for a in apps
            if kube.name(a) not in inst.ignore
            and scratch.LABEL_RUN not in ((a.get("metadata") or {}).get("labels") or {})
        ]
        broken = [(kube.name(a), why) for a in apps if (why := _app_problem(a, inst.fail_on_out_of_sync))]
        progressing = sum(
            1 for a in apps if ((a.get("status") or {}).get("health") or {}).get("status") == "Progressing"
        )
        if broken:
            listed = ", ".join(f"{n} ({why})" for n, why in broken[:MAX_LISTED])
            more = f" and {len(broken) - MAX_LISTED} more" if len(broken) > MAX_LISTED else ""
            results.append((False, f"{inst.name}: {len(broken)} of {len(apps)} broken: {listed}{more}"))
        else:
            note = f", {progressing} progressing" if progressing else ""
            results.append((True, f"{inst.name}: {len(apps)} Applications synced and healthy{note}"))
    return _join(results)


async def _deploy(p: Probe, inst: ArgoInstance) -> tuple[bool, str]:
    d = inst.deploy
    if not d.repo_url:
        return False, f"{inst.name}: deploy.repoURL is empty"
    coll = APPS.format(ns=inst.namespace)
    app_name = scratch.name(p, "argocd")
    app = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {**scratch.meta(p, app_name), "namespace": inst.namespace, "finalizers": [FINALIZER]},
        "spec": {
            "project": d.project,
            "source": {"repoURL": d.repo_url, "path": d.path, "targetRevision": d.revision},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": scratch.ns(p)},
            "syncPolicy": {"automated": {"prune": True}, "syncOptions": ["CreateNamespace=false"]},
        },
    }
    start = time.monotonic()
    try:
        await p.kube.create(coll, app)
    except StatusError as e:
        return False, f"{inst.name}: creating the Application: {e}"

    def settled(a: dict) -> bool:
        st = a.get("status") or {}
        ok = (st.get("sync") or {}).get("status") == "Synced" and (st.get("health") or {}).get("status") == "Healthy"
        return ok or bool(_app_problem(a, out_of_sync_fails=False))

    a = await scratch.wait_for(lambda: p.kube.get(f"{coll}/{app_name}"), settled, d.timeout)
    took = time.monotonic() - start
    st = a.get("status") or {}
    sync, health = (st.get("sync") or {}).get("status", "Unknown"), (st.get("health") or {}).get("status", "Unknown")
    problem = _app_problem(a, out_of_sync_fails=False)
    deployed = [f"{r.get('kind')}/{r.get('name')}" for r in st.get("resources") or []]

    # Delete it; the finalizer makes Argo CD prune what it deployed first.
    await p.kube.delete(f"{coll}/{app_name}")
    gone = await scratch.wait_for(_gone(p, f"{coll}/{app_name}"), lambda g: g, 60)

    if problem or sync != "Synced" or health != "Healthy":
        why = problem or f"{sync}/{health} after {d.timeout:.0f}s"
        return False, f"{inst.name}: {why}"
    detail = f"{inst.name}: synced {', '.join(deployed) or 'the app'} into {scratch.ns(p)} in {took:.0f}s"
    if not gone:
        detail += ", but the Application wasn't gone 60s after deletion"
    return True, detail


def _gone(p: Probe, path: str):
    async def fetch() -> bool:
        try:
            await p.kube.get(path)
        except StatusError as e:
            if e.code == 404:
                return True
            raise
        return False

    return fetch


@check("argocd-deploy", GROUP_ARGOCD, "A team can deploy through Argo CD", SUITE)
async def check_deploy(p: Probe) -> Outcome:
    instances = [i for i in _instances(p) if i.deploy.enabled]
    if not instances:
        return skipped("no instance has deploy.enabled on")
    out = _join([await _deploy(p, inst) for inst in instances])
    if out.status == Status.FAIL:
        out.hint = (
            "Argo CD must be allowed to manage the scratch namespace (OpenShift GitOps: the label "
            "argocd.argoproj.io/managed-by=<instance namespace>, e.g. via scratch.labels), the AppProject "
            "must allow it as a destination, and the instance must reach deploy.repoURL."
        )
    return out
