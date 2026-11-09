import aiohttp
from aiohttp.test_utils import TestServer
from conftest import run

from platform_probe import console
from platform_probe.config import parse_config
from platform_probe.runner import GROUP_GATEWAY, GROUP_MESH, Check, Probe, failed, passed


async def _pass(_p):
    return passed("fine")


async def _fail(_p):
    return failed("got 200", "check it")


def probe() -> Probe:
    cfg = parse_config({"release": "probe", "namespace": "ns", "gateway": {"host": "probe.example"}})
    return Probe(cfg, checks=[Check("a", GROUP_GATEWAY, "passes", _pass), Check("b", GROUP_MESH, "fails", _fail)])


async def with_console(fn):
    srv = TestServer(console.app(probe()))
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            return await fn(s, srv)
    finally:
        await srv.close()


def test_run_wait_and_junit():
    async def go(s, srv):
        async with s.post(srv.make_url("/api/run?wait=true")) as r:
            rep = await r.json()
            assert r.status == 200 and not rep["running"] and rep["passed"] == 1 and rep["failed"] == 1, rep
        async with s.get(srv.make_url("/api/report.junit")) as r:
            body = await r.text()
        assert '<failure message="got 200">check it</failure>' in body, body

    run(with_console(go))


def test_run_in_background():
    async def go(s, srv):
        async with s.post(srv.make_url("/api/run")) as r:
            assert r.status == 202
        for _ in range(50):
            async with s.get(srv.make_url("/api/report")) as r:
                rep = await r.json()
            if not rep["running"]:
                break
        assert rep["passed"] == 1 and "finishedAt" in rep, rep

    run(with_console(go))


def test_ui_and_config():
    async def go(s, srv):
        for path, want in {
            "/": "<title>platform-probe</title>",
            "/app.js": "runExperiment",
            "/api/config": '"host": "probe.example"',
            "/healthz": "ok",
            "/style.css": "prefers-color-scheme",
            "/api/report": '"running": false',
            "/api/experiments": '"name": "gateway-split"',
        }.items():
            async with s.get(srv.make_url(path)) as r:
                body = await r.text()
            assert r.status == 200 and want in body, (path, r.status, body[:200])
        async with s.post(srv.make_url("/api/experiments/nope")) as r:
            assert r.status == 404

    run(with_console(go))
