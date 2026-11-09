"""Workload security: what the platform lets a tenant's pod do, and what it refuses.

Rejections are tested with server-side dry runs: admission (SCC, Pod Security, quota) runs in
full and nothing is created.
"""

from __future__ import annotations

from .. import kube, scratch
from ..kube import StatusError
from ..runner import GROUP_WORKLOADS, Outcome, Probe, check, failed, passed, skipped

SUITE = "workloads"
PODS = "/api/v1/pods"


def _pod(p: Probe, suffix: str, container: dict | None = None, **spec) -> dict:
    s = scratch.pod_spec(p, {"name": "probe", "command": ["/usr/bin/python3", "-c", "pass"], **(container or {})})
    s.update(spec)
    return {"apiVersion": "v1", "kind": "Pod", "metadata": scratch.meta(p, scratch.name(p, suffix)), "spec": s}


@check("workloads-uid", GROUP_WORKLOADS, "Tenant pods get the expected SCC and an arbitrary UID", SUITE)
async def check_uid(p: Probe) -> Outcome:
    run = await scratch.run_task(p, "whoami", "whoami", timeout=120)
    if not run.ok:
        return failed(f"pod: {run.explain()}")
    uid = run.data.get("uid")
    scc = ((run.pod.get("metadata") or {}).get("annotations") or {}).get("openshift.io/scc", "")
    want = p.cfg.suites.workloads.expected_scc
    if uid == 0:
        return failed("the pod ran as root (UID 0)")
    if scc and want and scc != want:
        return failed(
            f"admitted under SCC {scc}, expected {want}", "A broader SCC is granted to the namespace's ServiceAccounts."
        )
    try:
        ns = await p.kube.get("/api/v1/namespaces/" + scratch.ns(p))
        uid_range = ((ns.get("metadata") or {}).get("annotations") or {}).get("openshift.io/sa.scc.uid-range", "")
    except StatusError:
        uid_range = ""
    detail = f"UID {uid}"
    if uid_range:
        start, _, size = uid_range.partition("/")
        if not int(start) <= uid < int(start) + int(size):
            return failed(f"UID {uid} is outside the namespace's range {uid_range}")
        detail += f" from the namespace's range {uid_range}"
    return passed(detail + (f", SCC {scc}" if scc else ""))


def _rejection(e: StatusError) -> str:
    m = e.message
    if "security context constraint" in m:
        return "SCC"
    if "PodSecurity" in m:
        return "Pod Security"
    return m[:80] or str(e.code)


@check("workloads-privileged", GROUP_WORKLOADS, "Privileged, root, hostPath and hostNetwork pods are refused", SUITE)
async def check_privileged(p: Probe) -> Outcome:
    variants = {
        "privileged": _pod(p, "privileged", {"securityContext": {"privileged": True}}),
        "root": _pod(p, "root", {"securityContext": {"runAsUser": 0}}, securityContext={}),
        "hostPath": _pod(
            p,
            "hostpath",
            {"volumeMounts": [{"name": "host", "mountPath": "/host"}]},
            volumes=[{"name": "host", "hostPath": {"path": "/"}}],
        ),
        "hostNetwork": _pod(p, "hostnetwork", hostNetwork=True),
    }
    refused, allowed = [], []
    for label, pod in variants.items():
        try:
            await p.kube.create(scratch.path(p, PODS), pod, dry_run=True)
            allowed.append(label)
        except StatusError as e:
            if e.code not in (403, 422):
                return failed(f"{label}: {e}")
            refused.append(f"{label} ({_rejection(e)})")
    if allowed:
        return failed(
            f"admitted: {', '.join(allowed)}",
            "A tenant ServiceAccount, or every authenticated user, has a privileged SCC, or the namespace's "
            "Pod Security level is privileged.",
        )
    return passed("refused: " + ", ".join(refused))


@check("workloads-quota", GROUP_WORKLOADS, "ResourceQuota refuses a pod over the quota", SUITE)
async def check_quota(p: Probe) -> Outcome:
    quotas = kube.items(await p.kube.get(scratch.path(p, "/api/v1/resourcequotas")))
    compute = [
        q for q in quotas if any(k.split(".")[-1] in ("cpu", "memory") for k in (q.get("spec") or {}).get("hard", {}))
    ]
    if not compute:
        msg = f"no ResourceQuota on CPU or memory in {scratch.ns(p)}"
        if p.cfg.suites.workloads.expect_quota:
            return failed(msg, "Create the scratch namespace with oc new-project, so it gets your project template.")
        return skipped(msg + " (create it with oc new-project to test your project template)")
    huge = {"cpu": "100000", "memory": "100Ti"}
    pod = _pod(p, "quota", {"resources": {"requests": huge, "limits": huge}})
    try:
        await p.kube.create(scratch.path(p, PODS), pod, dry_run=True)
    except StatusError as e:
        if "quota" in e.message or "maximum" in e.message:
            return passed(f"quota {', '.join(kube.name(q) for q in compute)}; over it: {e.message[:120]}")
        return failed(f"refused, but not by the quota: {e.message[:160]}")
    return failed(f"a pod requesting 100000 CPUs and 100Ti was admitted despite {kube.name(compute[0])}")


@check("workloads-limitrange", GROUP_WORKLOADS, "LimitRange gives pods default requests and limits", SUITE)
async def check_limit_range(p: Probe) -> Outcome:
    ranges = kube.items(await p.kube.get(scratch.path(p, "/api/v1/limitranges")))
    if not ranges:
        msg = f"no LimitRange in {scratch.ns(p)}"
        if p.cfg.suites.workloads.expect_limit_range:
            return failed(msg, "Create the scratch namespace with oc new-project, so it gets your project template.")
        return skipped(msg + " (create it with oc new-project to test your project template)")
    pod = _pod(p, "limitrange", {"resources": {}})
    try:
        made = await p.kube.create(scratch.path(p, PODS), pod, dry_run=True)
    except StatusError as e:
        return failed(f"a pod without resources was refused: {e.message[:160]}")
    res = ((made.get("spec") or {}).get("containers") or [{}])[0].get("resources") or {}
    if not res.get("requests") and not res.get("limits"):
        return failed(f"{kube.name(ranges[0])} set no defaults on a pod without resources")
    shown = "; ".join(
        f"{k} {', '.join(f'{r}={v}' for r, v in res[k].items())}" for k in ("requests", "limits") if res.get(k)
    )
    return passed(f"{kube.name(ranges[0])}: {shown}")
