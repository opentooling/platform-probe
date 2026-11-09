"""Experiments are the hands-on side of the checks: send n requests, show each result."""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .runner import Probe, Resp, catalog_version, hops, version


@dataclass
class Sample:
    """One request of an experiment, as the console shows it: a short label and a tone."""

    status: int
    label: str
    tone: str  # ok, warn, bad or info
    ms: int

    def to_dict(self) -> dict:
        return {"status": self.status, "label": self.label, "tone": self.tone, "ms": self.ms}


@dataclass
class ExperimentResult:
    name: str = ""
    samples: list[Sample] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "samples": [s.to_dict() for s in self.samples], "summary": self.summary}


@dataclass
class Experiment:
    name: str
    title: str
    description: str
    config: str
    run: Callable[[Probe, int], Awaitable[ExperimentResult]]

    def to_dict(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description, "config": self.config}


def experiments(p: Probe) -> list[Experiment]:
    c = p.cfg
    return [
        Experiment(
            "gateway-split",
            "Gateway split",
            f"The gateway sends {c.routes.split_v2_percent}% of /split to catalog v2. "
            "A request with x-version: v2 always goes there.",
            "HTTPRoute backendRefs: catalog-v1, catalog-v2 by weight",
            exp_gateway_split,
        ),
        Experiment(
            "mesh-split",
            "Mesh split",
            f"frontend's sidecar sends {c.mesh.split_v2_percent}% of calls to catalog v2.",
            "VirtualService catalog: subsets v1 and v2 by weight",
            exp_mesh_split,
        ),
        Experiment(
            "retry",
            "Retries",
            "pricing fails 30% of requests. catalog's sidecar retries, so the caller still sees 200.",
            "x-probe-fail: pricing=0.3 · retries: attempts 8, retryOn 5xx",
            exp_retry,
        ),
        Experiment(
            "fault-abort",
            "Fault abort",
            "catalog's sidecar aborts half the calls to pricing with 503, before they leave the pod.",
            "fault.abort: 50%, httpStatus 503",
            exp_fault_abort,
        ),
        Experiment(
            "fault-delay",
            "Fault delay",
            "catalog's sidecar holds every call to pricing for 1s.",
            "fault.delay: 100%, fixedDelay 1s",
            exp_fault_delay,
        ),
        Experiment(
            "outlier",
            "Circuit breaker",
            "One pricing pod always fails. After 3 errors in a row, catalog's sidecar ejects it for 30s.",
            "outlierDetection: consecutive5xxErrors 3, baseEjectionTime 30s",
            exp_outlier,
        ),
        Experiment(
            "authz",
            "Authorization",
            "The console calls catalog directly. Only frontend and the gateway may, so the sidecar answers 403.",
            "AuthorizationPolicy catalog: from frontend and the gateway",
            exp_authz,
        ),
        Experiment(
            "rate-limit",
            "Rate limit",
            f"bob is on the free plan: {c.limits.free}.",
            "RateLimitPolicy: counter auth.identity.userid, by plan",
            exp_rate_limit,
        ),
    ]


async def run_experiment(p: Probe, name: str, n: int) -> ExperimentResult | None:
    """Run one experiment by name, with 1 to 60 requests. None if there is no such experiment."""
    n = max(1, min(n, 60))
    for e in experiments(p):
        if e.name == name:
            res = await e.run(p, n)
            res.name = name
            return res
    return None


def sample(r: Resp, label: str, tone: str) -> Sample:
    return Sample(r.status, label, tone, int(r.dur * 1000))


async def version_samples(
    n: int, send: Callable[[], Awaitable[Resp]], version_of: Callable[[Resp], str]
) -> ExperimentResult:
    res = ExperimentResult()
    count: Counter[str] = Counter()
    for _ in range(n):
        r = await send()
        if r.status != 200:
            res.samples.append(sample(r, str(r), "bad"))
            count["error"] += 1
            continue
        v = version_of(r)
        count[v] += 1
        res.samples.append(sample(r, v, "info" if v == "v2" else "ok"))
    res.summary = f"{count['v1']} to v1, {count['v2']} to v2"
    if count["error"]:
        res.summary += f", {count['error']} failed"
    return res


async def exp_gateway_split(p: Probe, n: int) -> ExperimentResult:
    return await version_samples(n, lambda: p.gw("GET", "/split", p.api_key("alice")), version)


async def exp_mesh_split(p: Probe, n: int) -> ExperimentResult:
    return await version_samples(n, p.front, catalog_version)


async def exp_retry(p: Probe, n: int) -> ExperimentResult:
    res = ExperimentResult()
    ok = retried = 0
    for _ in range(n):
        r = await p.front({"x-probe-scenario": "retry", "x-probe-fail": "pricing=0.3"})
        _, _, pr = hops(r)
        if r.status != 200 or pr is None:
            res.samples.append(sample(r, str(r), "bad"))
        elif pr.attempt > 1:
            ok += 1
            retried += 1
            res.samples.append(sample(r, f"200 ×{pr.attempt}", "warn"))
        else:
            ok += 1
            res.samples.append(sample(r, "200", "ok"))
    res.summary = f"{ok} / {n} succeeded; {retried} needed a retry, shown as ×attempts"
    return res


async def exp_fault_abort(p: Probe, n: int) -> ExperimentResult:
    res = ExperimentResult()
    aborted = 0
    for _ in range(n):
        r = await p.front({"x-probe-scenario": "fault-abort"})
        if r.status == 200:
            res.samples.append(sample(r, "200", "ok"))
            continue
        aborted += 1
        res.samples.append(sample(r, "503", "bad"))
    res.summary = (
        f"{aborted} aborted by the sidecar, {n - aborted} reached pricing. The app never saw the aborted ones."
    )
    return res


async def exp_fault_delay(p: Probe, n: int) -> ExperimentResult:
    res = ExperimentResult()
    n = min(n, 10)
    total = 0.0
    for _ in range(n):
        r = await p.front({"x-probe-scenario": "fault-delay"})
        total += r.dur
        res.samples.append(sample(r, f"{r.dur:.1f}s", "warn" if r.status == 200 else "bad"))
    res.summary = f"Average {total / n:.1f}s per request, capped at 10 requests."
    return res


async def exp_outlier(p: Probe, n: int) -> ExperimentResult:
    res = ExperimentResult()
    n_failed = 0
    for _ in range(n):
        r = await p.front({"x-probe-scenario": "outlier"})
        if r.status == 200:
            res.samples.append(sample(r, "200", "ok"))
            continue
        n_failed += 1
        res.samples.append(sample(r, str(r.status), "bad"))
    res.summary = f"{n_failed} failed before the faulty pod was ejected. Run again within 30s and none fail."
    return res


async def exp_authz(p: Probe, n: int) -> ExperimentResult:
    res = ExperimentResult()
    denied = 0
    for _ in range(n):
        r = await p.call(p.cfg.services.catalog_v1, "/")
        if r.status == 403:
            denied += 1
            res.samples.append(sample(r, "403", "bad"))
        else:
            res.samples.append(sample(r, str(r), "warn"))
    f = await p.front()
    res.summary = f"{denied} / {n} denied (RBAC: access denied). The same call through frontend: {f}."
    return res


async def exp_rate_limit(p: Probe, n: int) -> ExperimentResult:
    res = ExperimentResult()
    limited = 0
    for _ in range(n):
        r = await p.gw("GET", "/api", p.api_key("bob"))
        tone = "ok"
        if r.status == 429:
            limited += 1
            tone = "warn"
        elif r.status != 200:
            tone = "bad"
        res.samples.append(sample(r, str(r), tone))
    res.summary = f"{limited} / {n} limited with 429 (free plan: {p.cfg.limits.free})."
    return res
