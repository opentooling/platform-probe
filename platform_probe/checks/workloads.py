"""Workload security: what the platform lets a tenant's pod do, and what it refuses.

Rejections are tested with server-side dry runs: admission (SCC, Pod Security, quota) runs in
full and nothing is created.
"""

from __future__ import annotations

import math

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


# Quota keys on CPU and memory, and the container field each one counts.
QUOTA_KEYS = {
    "cpu": ("requests", "cpu"),
    "requests.cpu": ("requests", "cpu"),
    "limits.cpu": ("limits", "cpu"),
    "memory": ("requests", "memory"),
    "requests.memory": ("requests", "memory"),
    "limits.memory": ("limits", "memory"),
}
# How far past the room left a test pod asks for, and what it asks for otherwise.
STEP = {"cpu": 0.1, "memory": 64 * 2**20}
SMALL = {"cpu": 0.01, "memory": 32 * 2**20}


def _q(resource: str, amount: float) -> str:
    """A quantity: millicores for CPU, Mi (rounded up) for memory."""
    if resource == "cpu":
        return f"{math.ceil(amount * 1000)}m"
    return f"{math.ceil(amount / 2**20)}Mi"


def _bounds(ranges: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """The tightest per-container (or per-pod) max and min the LimitRanges set, by resource."""
    maxes: dict[str, float] = {}
    mins: dict[str, float] = {}
    for lr in ranges:
        for item in (lr.get("spec") or {}).get("limits") or []:
            if item.get("type") not in ("Container", "Pod"):
                continue
            for res, v in (item.get("max") or {}).items():
                maxes[res] = min(maxes.get(res, math.inf), scratch.parse_quantity(v))
            for res, v in (item.get("min") or {}).items():
                mins[res] = max(mins.get(res, 0.0), scratch.parse_quantity(v))
    return maxes, mins


def _over(hard: dict, used: dict, ranges: list[dict]) -> tuple[dict | None, str]:
    """Container resources that go just past the quota's room on one CPU or memory key, within
    what the LimitRanges allow (LimitRange admission runs before quota, so a pod over a
    LimitRange max would be refused by it, not by the quota). Every other resource gets a small
    request and limit, so a quota on limits.* doesn't refuse it for a missing one. Returns the
    resources and what they test, or None and why no such pod exists."""
    maxes, mins = _bounds(ranges)
    why = []
    for key, h in hard.items():
        if key not in QUOTA_KEYS:
            continue
        kind, res = QUOTA_KEYS[key]
        room = scratch.parse_quantity(h) - scratch.parse_quantity(used.get(key, "0"))
        need = max(room, 0.0) + STEP[res]
        if need > maxes.get(res, math.inf):
            why.append(f"{key} has {_q(res, room)} left, more than a container may ask for ({_q(res, maxes[res])})")
            continue
        small = {r: _q(r, max(SMALL[r], mins.get(r, 0.0))) for r in ("cpu", "memory")}
        resources = {"requests": dict(small), "limits": dict(small)}
        resources["limits"][res] = _q(res, need)
        if kind == "requests":
            resources["requests"][res] = _q(res, need)
        return resources, f"{key} {_q(res, need)} with {_q(res, max(room, 0.0))} left"
    return None, "; ".join(why) or "it sets no CPU or memory limit"


async def _refused_by_quota(p: Probe, suffix: str, resources: dict, names: list[str]) -> tuple[bool, str]:
    """Dry-run a pod with these resources. (refused by one of the quotas, what happened)."""
    try:
        await p.kube.create(scratch.path(p, PODS), _pod(p, suffix, {"resources": resources}), dry_run=True)
    except StatusError as e:
        hit = next((n for n in names if f"quota: {n}" in e.message), "")
        return bool(hit), e.message[:200]
    return False, "admitted"


def _missing(p: Probe, what: str, expected: bool, hint: str) -> Outcome:
    if expected:
        return failed(what, hint)
    return skipped(what)


@check("workloads-quota", GROUP_WORKLOADS, "ResourceQuota refuses a pod over the quota", SUITE)
async def check_quota(p: Probe) -> Outcome:
    quotas = kube.items(await p.kube.get(scratch.path(p, "/api/v1/resourcequotas")))
    ranges = kube.items(await p.kube.get(scratch.path(p, "/api/v1/limitranges")))
    for q in quotas:
        st = q.get("status") or {}
        resources, what = _over(st.get("hard") or (q.get("spec") or {}).get("hard") or {}, st.get("used") or {}, ranges)
        if resources is None:
            continue
        name = kube.name(q)
        refused, msg = await _refused_by_quota(p, "quota", resources, [name])
        if refused:
            return passed(f"{name}: a pod asking {what} was refused")
        return failed(
            f"{name}: a pod asking {what}: {msg}", "The quota's enforcement is off, or something else refused it first."
        )
    if quotas:
        return skipped(f"can't test {', '.join(kube.name(q) for q in quotas)} with one pod: {what}")
    return _missing(
        p,
        f"no ResourceQuota in {scratch.ns(p)} (create it with oc new-project to test your project template)",
        p.cfg.suites.workloads.expect_quota,
        "Create the scratch namespace with oc new-project, so it gets your project template.",
    )


@check(
    "workloads-cluster-quota",
    GROUP_WORKLOADS,
    "ClusterResourceQuota covers the namespace and refuses a pod over it",
    SUITE,
)
async def check_cluster_quota(p: Probe) -> Outcome:
    """OpenShift's ClusterResourceQuotas span namespaces chosen by label or annotation (often the
    requester's). AppliedClusterResourceQuota shows the ones that cover a namespace, to anyone
    who may read it."""
    try:
        applied = kube.items(
            await p.kube.get(scratch.path(p, "/apis/quota.openshift.io/v1/appliedclusterresourcequotas"))
        )
    except StatusError as e:
        if e.code == 404:
            return skipped("no quota.openshift.io API: not an OpenShift cluster")
        return failed(str(e))
    if not applied:
        return _missing(
            p,
            f"no ClusterResourceQuota selects {scratch.ns(p)}",
            p.cfg.suites.workloads.expect_cluster_quota,
            "Give the scratch namespace the labels or annotations your ClusterResourceQuotas select "
            "(scratch.labels, scratch.annotations), or create it with oc new-project as a team member would.",
        )
    ranges = kube.items(await p.kube.get(scratch.path(p, "/api/v1/limitranges")))
    names = [kube.name(q) for q in applied]
    why = []
    for q in applied:
        total = (q.get("status") or {}).get("total") or {}
        hard = total.get("hard") or ((q.get("spec") or {}).get("quota") or {}).get("hard") or {}
        resources, what = _over(hard, total.get("used") or {}, ranges)
        if resources is None:
            why.append(f"{kube.name(q)}: {what}")
            continue
        refused, msg = await _refused_by_quota(p, "cluster-quota", resources, names)
        if refused:
            return passed(f"covered by {', '.join(names)}; a pod asking {what} on {kube.name(q)} was refused")
        return failed(
            f"{kube.name(q)}: a pod asking {what}: {msg}", "Is the ClusterResourceQuota admission plugin enforcing it?"
        )
    return skipped(f"covered by {', '.join(names)}, but can't test them with one pod: {'; '.join(why)}")


@check("workloads-limitrange", GROUP_WORKLOADS, "LimitRange sets defaults and refuses a container over its max", SUITE)
async def check_limit_range(p: Probe) -> Outcome:
    ranges = kube.items(await p.kube.get(scratch.path(p, "/api/v1/limitranges")))
    if not ranges:
        return _missing(
            p,
            f"no LimitRange in {scratch.ns(p)} (create it with oc new-project to test your project template)",
            p.cfg.suites.workloads.expect_limit_range,
            "Create the scratch namespace with oc new-project, so it gets your project template.",
        )
    names = ", ".join(kube.name(r) for r in ranges)
    pod = _pod(p, "limitrange", {"resources": {}})
    try:
        made = await p.kube.create(scratch.path(p, PODS), pod, dry_run=True)
    except StatusError as e:
        return failed(f"a pod without resources was refused: {e.message[:160]}")
    res = ((made.get("spec") or {}).get("containers") or [{}])[0].get("resources") or {}
    if not res.get("requests") and not res.get("limits"):
        return failed(f"{names} set no defaults on a pod without resources")
    shown = "; ".join(
        f"{k} {', '.join(f'{r}={v}' for r, v in res[k].items())}" for k in ("requests", "limits") if res.get(k)
    )
    maxes, _ = _bounds(ranges)
    over = [r for r in ("cpu", "memory") if r in maxes]
    if not over:
        return passed(f"{names}: defaults {shown}; no max set")
    r = over[0]
    big = _q(r, maxes[r] + STEP[r])
    pod = _pod(p, "limitrange-max", {"resources": {"requests": {r: big}, "limits": {r: big}}})
    try:
        await p.kube.create(scratch.path(p, PODS), pod, dry_run=True)
    except StatusError as e:
        if "maximum" in e.message:
            return passed(
                f"{names}: defaults {shown}; a container asking {r} {big} was refused (max {_q(r, maxes[r])})"
            )
        return failed(f"a container asking {r} {big} was refused, but not for the max: {e.message[:160]}")
    return failed(f"a container asking {r} {big} was admitted, over the LimitRange max {_q(r, maxes[r])}")
