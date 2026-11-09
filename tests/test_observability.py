"""The observability suite against a fake Grafana (Loki, Mimir and Tempo behind the data source
proxy) and a fake OTLP receiver, which the real otlp task posts to."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import run
from fakekube import FakeKube

from platform_probe import scratch, tasks
from platform_probe.checks import observability as obs
from platform_probe.config import parse_config
from platform_probe.runner import Probe, Status

NS = "platform-probe-scratch"
TOKEN = "t0ken"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(scratch, "POLL", 0)
    monkeypatch.setattr(obs, "POLL", 0.01)


class Stack:
    """Grafana's data source proxy and an OTLP receiver. What the collector would have shipped is
    decided by the test: logs_shipped, scraped, and whatever the receiver got."""

    def __init__(self):
        self.logs_shipped = True
        self.scraped = True
        self.received = {"traces": [], "metrics": []}
        self.queries: list[str] = []

    async def otlp(self, request: web.Request) -> web.Response:
        self.received[request.match_info["signal"]].append(await request.json())
        return web.json_response({})

    def traces(self) -> set[str]:
        return {
            s["traceId"]
            for p in self.received["traces"]
            for rs in p["resourceSpans"]
            for ss in rs["scopeSpans"]
            for s in ss["spans"]
        }

    async def proxy(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"message": "Unauthorized"}, status=401)
        uid, tail = request.match_info["uid"], request.match_info["tail"]
        q = request.query.get("query", "")
        self.queries.append(q)
        if uid == "loki" and tail == "loki/api/v1/query_range":
            ok = self.logs_shipped and f'namespace="{NS}"' in q and 'cluster="prod"' in q and "marker run=" in q
            return web.json_response({"data": {"result": [{"values": [["1", "x"]]}] if ok else []}})
        if uid == "mimir" and tail == "api/v1/query":
            pushed = any("platform_probe_marker" in str(m) for m in self.received["metrics"])
            ok = (self.scraped or pushed) and q.startswith("platform_probe_marker{") and 'cluster="prod"' in q
            return web.json_response({"data": {"result": [{"value": [1, "1"]}] if ok else []}})
        if uid == "tempo" and tail.startswith("api/traces/"):
            return web.json_response({}, status=200 if tail.rsplit("/", 1)[1] in self.traces() else 404)
        return web.json_response({"message": "Data source not found"}, status=404)

    def app(self) -> web.Application:
        a = web.Application()
        a.router.add_get("/api/datasources/proxy/uid/{uid}/{tail:.*}", self.proxy)
        a.router.add_post("/v1/{signal}", self.otlp)
        return a


def probe(tmp_path, url: str, **obs_cfg) -> Probe:
    (tmp_path / "token").write_text(TOKEN + "\n")
    cfg = parse_config(
        {
            "release": "probe",
            "namespace": "platform-probe",
            "image": "img",
            "suites": {
                "observability": {
                    "enabled": True,
                    "grafanaUrl": url,
                    "tokenFile": str(tmp_path / "token"),
                    "datasources": {"loki": "loki", "mimir": "mimir", "tempo": "tempo"},
                    "labels": {"extra": {"cluster": "prod"}},
                    "otlpEndpoint": url,
                    "timeout": "1s",
                    **obs_cfg,
                }
            },
        }
    )
    p = Probe(cfg, FakeKube(tmp_path))
    p.run_id, p.state = "run1", {}
    return p


def with_stack(stack: Stack, tmp_path, fn, **cfg):
    async def go():
        srv = TestServer(stack.app(), host="127.0.0.1")
        await srv.start_server()
        p = probe(tmp_path, str(srv.make_url("")).rstrip("/"), **cfg)
        try:
            return p, [await f(p) for f in fn]
        finally:
            await p.close()
            await srv.close()

    return run(go())


ALL = [obs.check_logs, obs.check_metrics, obs.check_traces]


def test_every_signal_arrives(tmp_path):
    stack = Stack()
    p, (logs, metrics, traces) = with_stack(stack, tmp_path, ALL)
    assert logs.status == Status.PASS and "in Loki" in logs.detail, logs
    assert metrics.status == Status.PASS and metrics.detail.startswith("scraped"), metrics
    assert traces.status == Status.PASS and "in Tempo" in traces.detail, traces
    # One emitter pod for logs and metrics, labelled for the scrape.
    assert len(p.kube.emitted) == 1 and p.kube.emitted[0]["run"] == "run1"
    # The span the task really sent is the one Tempo was asked for.
    assert len(stack.traces()) == 1


def test_otlp_metrics(tmp_path):
    stack = Stack()
    stack.scraped = False
    _, (metrics,) = with_stack(stack, tmp_path, [obs.check_metrics], metricsMethod="otlp")
    assert metrics.status == Status.PASS and metrics.detail.startswith("pushed over OTLP"), metrics
    point = stack.received["metrics"][0]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    assert point["name"] == "platform_probe_marker"


def test_missing_signals_name_the_query(tmp_path):
    stack = Stack()
    stack.logs_shipped = stack.scraped = False
    _, (logs, metrics) = with_stack(stack, tmp_path, [obs.check_logs, obs.check_metrics])
    assert (
        logs.status == Status.FAIL and 'namespace="platform-probe-scratch"' in logs.detail and "Alloy" in logs.hint
    ), logs
    assert metrics.status == Status.FAIL and 'run="run1"' in metrics.detail, metrics


def test_bad_token_and_uid(tmp_path):
    stack = Stack()

    def wrong_token(p):
        (tmp_path / "token").write_text("nope")
        return obs.check_logs(p)

    _, (out,) = with_stack(stack, tmp_path, [wrong_token])
    assert out.status == Status.FAIL and "Viewer" in out.hint, out
    _, (out,) = with_stack(stack, tmp_path, [obs.check_traces], datasources={"tempo": "typo"})
    assert out.status == Status.FAIL and "datasources.tempo" in out.hint, out


def test_unreachable_otlp_endpoint(tmp_path):
    stack = Stack()
    _, (out,) = with_stack(stack, tmp_path, [obs.check_traces], otlpEndpoint="http://127.0.0.1:1")
    assert out.status == Status.FAIL and "Connection refused" in out.detail and "OTLP" in out.hint, out


def test_skips(tmp_path):
    _, outs = with_stack(Stack(), tmp_path, ALL, datasources={}, otlpEndpoint="")
    assert [o.status for o in outs] == [Status.SKIP] * 3


def test_emit_serves_the_marker_metric():
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), tasks._metrics_handler("run1"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/metrics") as resp:
            body = resp.read().decode()
    finally:
        srv.shutdown()
        srv.server_close()
    assert 'platform_probe_marker{run="run1"} 1' in body
