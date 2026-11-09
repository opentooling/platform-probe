"""Observability: does a team's telemetry reach the central Grafana stack?

Each run emits a marker for each signal from a pod in the scratch namespace, labelled with the
run ID, and looks for it through Grafana's data source proxy:

- logs: the pod prints a marker line; the cluster's collector ships it to Loki
- metrics: the pod serves a marker metric that the collector scrapes (found by pod
  annotations), or pushes it over OTLP; it lands in Mimir
- traces: the pod sends a span to the collector's OTLP endpoint; it lands in Tempo

A pass reports how long the signal took to show up, so a slow pipeline shows before a broken
one does.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from urllib.parse import quote, urlencode

from .. import scratch
from ..runner import GROUP_OBSERVABILITY, Outcome, Probe, ProbeError, check, do, failed, passed, skipped
from ..tasks import MARKER, METRIC

SUITE = "observability"
# How often to ask Grafana whether a marker has arrived.
POLL = 5.0


def _token(p: Probe) -> str:
    try:
        with open(p.cfg.suites.observability.token_file, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


async def _query(p: Probe, uid: str, path: str, params: dict | None = None):
    """GET a path on a data source through Grafana's proxy. Returns (status, JSON or None, text)."""
    o = p.cfg.suites.observability
    url = f"{o.grafana_url.rstrip('/')}/api/datasources/proxy/uid/{quote(uid, safe='')}/{path.lstrip('/')}"
    if params:
        url += "?" + urlencode(params)
    r = await do(p.mesh, "GET", url, {"Authorization": f"Bearer {_token(p)}", "Accept": "application/json"})
    try:
        data = json.loads(r.body)
    except ValueError:
        data = None
    return r, data


def _grafana_problem(p: Probe, r, uid: str, what: str) -> Outcome | None:
    """An outcome for an answer that isn't a query result, or None."""
    if r.err is not None:
        return failed(
            f"Grafana: {r}",
            "Is suites.observability.grafana.url reachable from the console? Its host gets a mesh egress entry.",
        )
    if r.status in (401, 403):
        return failed(
            f"Grafana answered {r.status}",
            "The service account token needs the Viewer role (query access) on the data source.",
        )
    if r.status == 404 and "not found" in r.body.lower() and "data source" in r.body.lower():
        return failed(
            f"no data source with UID {uid!r}",
            f"Set suites.observability.grafana.datasources.{what} to the UID in Grafana.",
        )
    return None


def _selector(p: Probe, labels: dict[str, str]) -> str:
    extra = p.cfg.suites.observability.labels.extra
    return ",".join(f'{k}="{v}"' for k, v in {**extra, **labels}.items())


async def _emitter(p: Probe) -> tuple[str, float]:
    """The long-running pod that prints the marker and serves the marker metric, started once
    per run. Returns its name and when it started emitting."""
    if "emitter" in p.state:
        if isinstance(p.state["emitter"], Exception):
            raise p.state["emitter"]
        return p.state["emitter"]
    o = p.cfg.suites.observability
    pod_name = scratch.name(p, "telemetry")
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": scratch.meta(p, pod_name, annotations=dict(o.pod_annotations)),
        "spec": scratch.pod_spec(
            p,
            {
                "name": "emit",
                "args": [
                    "task",
                    "emit",
                    json.dumps({"run": p.run_id, "seconds": 3 * o.timeout + 60, "metrics_port": o.metrics_port}),
                ],
                "ports": [{"name": "metrics", "containerPort": o.metrics_port}],
            },
        ),
    }
    try:
        await p.kube.create(scratch.path(p, "/api/v1/pods"), pod)
        final = await scratch.wait_for(
            lambda: p.kube.get(scratch.path(p, "/api/v1/pods", pod_name)),
            lambda o_: (o_.get("status") or {}).get("phase") in ("Running", "Succeeded", "Failed"),
            120,
        )
        phase = (final.get("status") or {}).get("phase")
        if phase != "Running":
            reason = scratch.waiting_reason(final) or f"pod {phase}"
            raise ProbeError(f"the telemetry pod didn't start in 120s: {reason}")
    except Exception as e:
        p.state["emitter"] = e
        raise
    p.state["emitter"] = (pod_name, time.monotonic())
    return p.state["emitter"]


async def _poll(p: Probe, ask) -> tuple[bool, float, Outcome | None]:
    """Call ask() until it finds the marker or the timeout passes. ask returns (found, outcome
    to stop with or None). Returns (found, seconds waited, outcome)."""
    start, deadline = time.monotonic(), time.monotonic() + p.cfg.suites.observability.timeout
    while True:
        found, out = await ask()
        if found or out is not None or time.monotonic() >= deadline:
            return found, time.monotonic() - start, out
        await asyncio.sleep(POLL)


@check("observability-logs", GROUP_OBSERVABILITY, "Pod logs reach Loki", SUITE)
async def check_logs(p: Probe) -> Outcome:
    o = p.cfg.suites.observability
    if not o.datasources.loki:
        return skipped("suites.observability.grafana.datasources.loki is empty")
    try:
        pod_name, started = await _emitter(p)
    except Exception as e:  # noqa: BLE001
        return failed(str(e))
    query = (
        "{"
        + _selector(p, {o.labels.namespace: scratch.ns(p), o.labels.pod: pod_name})
        + "}"
        + f' |= "{MARKER}{p.run_id}"'
    )

    async def ask():
        end = time.time_ns()
        params = {"query": query, "start": end - 3_600_000_000_000, "end": end, "limit": 1, "direction": "backward"}
        r, data = await _query(p, o.datasources.loki, "loki/api/v1/query_range", params)
        if bad := _grafana_problem(p, r, o.datasources.loki, "loki"):
            return False, bad
        if r.status != 200:
            return False, failed(f"Loki answered {r.status}: {r.body[:200]}", f"Query: {query}")
        return bool(((data or {}).get("data") or {}).get("result")), None

    found, _, out = await _poll(p, ask)
    if out:
        return out
    if not found:
        return failed(
            f"no marker line in Loki after {o.timeout:.0f}s; query: {query}",
            "Does Alloy collect logs from the scratch namespace? Check labels.namespace, labels.pod and the "
            "cluster label in labels.extra against what Loki stores.",
        )
    return passed(f"in Loki {time.monotonic() - started:.0f}s after the pod started logging")


@check("observability-metrics", GROUP_OBSERVABILITY, "Metrics reach Mimir", SUITE)
async def check_metrics(p: Probe) -> Outcome:
    o = p.cfg.suites.observability
    if not o.datasources.mimir:
        return skipped("suites.observability.grafana.datasources.mimir is empty")
    if o.metrics_method == "otlp":
        if not o.otlp_endpoint:
            return failed("metrics.method is otlp, but otlp.endpoint is empty")
        run = await scratch.run_task(
            p,
            "otlp-metric",
            "otlp",
            {"endpoint": o.otlp_endpoint, "run": p.run_id, "metric": True, "namespace": scratch.ns(p)},
        )
        if not run.ok:
            return failed(
                f"pushing the metric to {o.otlp_endpoint}: {run.explain()}",
                "Can pods in the scratch namespace reach Alloy?",
            )
        started, labels = time.monotonic(), {"run": p.run_id}
        how = "pushed over OTLP"
    else:
        try:
            pod_name, started = await _emitter(p)
        except Exception as e:  # noqa: BLE001
            return failed(str(e))
        labels = {"run": p.run_id, o.labels.namespace: scratch.ns(p), o.labels.pod: pod_name}
        how = "scraped"
    query = f"{METRIC}{{{_selector(p, labels)}}}"

    async def ask():
        r, data = await _query(p, o.datasources.mimir, "api/v1/query", {"query": query})
        if bad := _grafana_problem(p, r, o.datasources.mimir, "mimir"):
            return False, bad
        if r.status != 200:
            return False, failed(f"Mimir answered {r.status}: {r.body[:200]}", f"Query: {query}")
        return bool(((data or {}).get("data") or {}).get("result")), None

    found, _, out = await _poll(p, ask)
    if out:
        return out
    if not found:
        hint = (
            "Does Alloy scrape pods with these annotations (metrics.podAnnotations) in the scratch namespace?"
            if o.metrics_method != "otlp"
            else "Does Alloy forward OTLP metrics to Mimir?"
        )
        return failed(
            f"no {METRIC} series in Mimir after {o.timeout:.0f}s; query: {query}", hint + " Check the label names."
        )
    return passed(f"{how}, in Mimir {time.monotonic() - started:.0f}s later")


@check("observability-traces", GROUP_OBSERVABILITY, "Traces reach Tempo", SUITE)
async def check_traces(p: Probe) -> Outcome:
    o = p.cfg.suites.observability
    if not o.datasources.tempo:
        return skipped("suites.observability.grafana.datasources.tempo is empty")
    if not o.otlp_endpoint:
        return skipped("suites.observability.otlp.endpoint is empty")
    trace_id = secrets.token_hex(16)
    run = await scratch.run_task(
        p,
        "otlp-trace",
        "otlp",
        {"endpoint": o.otlp_endpoint, "run": p.run_id, "trace_id": trace_id, "namespace": scratch.ns(p)},
    )
    if not run.ok:
        return failed(
            f"sending the span to {o.otlp_endpoint}: {run.explain()}",
            "Can pods in the scratch namespace reach Alloy's OTLP port?",
        )
    started = time.monotonic()

    async def ask():
        r, _ = await _query(p, o.datasources.tempo, f"api/traces/{trace_id}")
        if bad := _grafana_problem(p, r, o.datasources.tempo, "tempo"):
            return False, bad
        if r.status == 404:
            return False, None
        if r.status != 200:
            return False, failed(f"Tempo answered {r.status}: {r.body[:200]}")
        return True, None

    found, _, out = await _poll(p, ask)
    if out:
        return out
    if not found:
        return failed(
            f"trace {trace_id} not in Tempo after {o.timeout:.0f}s", "Does Alloy forward OTLP traces to Tempo?"
        )
    return passed(f"trace {trace_id[:8]}… in Tempo {time.monotonic() - started:.0f}s after it was sent")
