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
            "/app.js": "renderCategory",
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


def probe_with_categories() -> Probe:
    cfg = parse_config({"release": "probe", "namespace": "ns", "testPodPrefix": "probe-platform-probe-test-"})
    return Probe(
        cfg,
        checks=[
            Check("gateway-a", GROUP_GATEWAY, "a", _pass),
            Check("gateway-b", GROUP_GATEWAY, "b", _fail),
            Check("mesh-c", GROUP_MESH, "c", _pass),
        ],
    )


def test_run_one_category_keeps_the_others():
    async def go():
        srv = TestServer(console.app(probe_with_categories()))
        await srv.start_server()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(srv.make_url("/api/config")) as r:
                    cfg = await r.json()
                assert cfg["categories"] == [
                    {"id": "gateway", "name": GROUP_GATEWAY},
                    {"id": "mesh", "name": GROUP_MESH},
                ]
                assert {c["category"] for c in cfg["checks"]} == {"gateway", "mesh"}
                assert cfg["testPodPrefix"] == "probe-platform-probe-test-"

                async with s.post(srv.make_url("/api/run?wait=true")) as r:
                    assert (await r.json())["total"] == 3
                async with s.post(srv.make_url("/api/run?wait=true&groups=mesh")) as r:
                    rep = await r.json()
                assert r.status == 200 and rep["total"] == 1 and rep["categories"] == ["mesh"], rep
                assert [x["id"] for x in rep["results"]] == ["mesh-c"]

                # The report's latest results still hold the gateway checks from the first run.
                async with s.get(srv.make_url("/api/report")) as r:
                    rep = await r.json()
                assert [x["id"] for x in rep["latest"]] == ["gateway-a", "gateway-b", "mesh-c"]
                assert all(x["at"] for x in rep["latest"])

                async with s.post(srv.make_url("/api/run?groups=storage")) as r:
                    err = await r.json()
                assert (
                    r.status == 400 and "no such category: storage" in err["error"] and "gateway, mesh" in err["error"]
                )
        finally:
            await srv.close()

    run(go())


def test_ui_has_the_sidebar_layout():
    async def go(s, srv):
        async with s.get(srv.make_url("/")) as r:
            html = await r.text()
        for want in ('id="nav-cats"', 'id="attention"', 'id="view-category"', 'id="cat-run"', 'href="#/experiments"'):
            assert want in html, want

    run(with_console(go))
