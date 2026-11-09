import asyncio
import errno
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from conftest import run

from platform_probe.service import (
    Hop,
    Service,
    ServiceConfig,
    caller_from_xfcc,
    classify_error,
    directive,
    in_cluster,
)


@pytest.mark.parametrize(
    ("xfcc", "want"),
    [
        ("", ""),
        (
            'By=spiffe://cluster.local/ns/p/sa/catalog;Hash=abc;Subject="";URI=spiffe://cluster.local/ns/p/sa/frontend',
            "spiffe://cluster.local/ns/p/sa/frontend",
        ),
        # Two elements: the last one is the immediate caller.
        (
            'By=a;URI=spiffe://td/ns/gw/sa/gw-istio,By=b;Subject="CN=x,O=y;z";URI=spiffe://td/ns/p/sa/frontend',
            "spiffe://td/ns/p/sa/frontend",
        ),
        ("Hash=abc", ""),
    ],
)
def test_caller_from_xfcc(xfcc, want):
    assert caller_from_xfcc(xfcc) == want


def test_directive():
    assert directive("frontend=3s, pricing = 0.3", "pricing") == "0.3"
    assert directive("frontend=3s", "pricing") is None


@pytest.mark.parametrize(
    ("host", "want"),
    [
        ("catalog", True),
        ("catalog.probe", True),
        ("catalog.probe.svc", True),
        ("catalog.probe.svc.cluster.local", True),
        ("example.com.evil", False),
        ("10.0.0.1", False),
        ("", False),
        ("api.example.com", False),
    ],
)
def test_in_cluster(host, want):
    assert in_cluster(host) is want


def test_classify_error():
    assert classify_error(ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")) == "reset"
    assert classify_error(aiohttp.ServerDisconnectedError()) == "reset"
    assert classify_error(ConnectionRefusedError(errno.ECONNREFUSED, "Connect call failed")) == "refused"
    assert classify_error(TimeoutError()) == "timeout"
    assert classify_error(ValueError("something else")) == "other"


def test_classify_real_errors():
    """The errors aiohttp raises for a refused port and a host that doesn't resolve."""

    async def go():
        async with aiohttp.ClientSession() as s:
            out = []
            for url in ("http://127.0.0.1:1/", "http://no-such-host.invalid/"):
                try:
                    async with s.get(url):
                        pass
                except Exception as e:  # noqa: BLE001
                    out.append(classify_error(e))
            return out

    assert run(go()) == ["refused", "dns"]


class Chain:
    """frontend → catalog v2 → pricing, with a faulty pricing for the outlier scenario."""

    async def __aenter__(self):
        self.servers = []
        pricing = await self.start(Service(ServiceConfig(role="pricing", version="v1")))
        faulty = await self.start(Service(ServiceConfig(role="pricing", faulty=True)))
        self.catalog = Service(
            ServiceConfig(
                role="catalog", version="v2", next=str(pricing.make_url("/")), next_outlier=str(faulty.make_url("/"))
            )
        )
        catalog = await self.start(self.catalog)
        self.frontend = await self.start(Service(ServiceConfig(role="frontend", next=str(catalog.make_url("/")))))
        self.client = aiohttp.ClientSession()
        return self

    async def start(self, svc: Service) -> TestServer:
        srv = TestServer(svc.app())
        await srv.start_server()
        self.servers.append(srv)
        return srv

    async def get(self, headers: dict[str, str]) -> tuple[int, Hop]:
        async with self.client.get(self.frontend.make_url("/api"), headers=headers) as resp:
            return resp.status, Hop.from_dict(await resp.json())

    async def __aexit__(self, *exc):
        await self.client.close()
        for s in reversed(self.servers):
            await s.close()


def test_chain():
    async def go():
        async with Chain() as c:
            code, hop = await c.get({"x-request-id": "r1", "x-probe-id": "id-1"})
            assert code == 200 and hop.next and hop.next.next, hop
            assert hop.next.version == "v2" and hop.next.next.service == "pricing" and hop.next.next.request_id == "r1"
            assert c.catalog.seen.has("id-1") and not c.catalog.seen.has("id-2")

            # A failure two hops down surfaces as 502, with the cause in the nested hop.
            code, hop = await c.get({"x-probe-fail": "pricing=1"})
            assert (code, hop.next.status, hop.next.next.status, hop.next.next.error) == (
                502,
                502,
                500,
                "injected failure",
            )

            code, hop = await c.get({"x-probe-scenario": "outlier"})
            assert code == 502 and hop.next.next.error == "faulty instance"

            start = time.monotonic()
            code, _ = await c.get({"x-probe-delay": "catalog=150ms"})
            assert code == 200 and time.monotonic() - start >= 0.15

    run(go())


def test_echo():
    async def go():
        srv = TestServer(Service(ServiceConfig(role="frontend", next="http://unused.invalid")).app())
        await srv.start_server()
        try:
            async with (
                aiohttp.ClientSession() as s,
                s.get(srv.make_url("/echo/x"), headers={"X-Probe-Added": "gateway"}) as resp,
            ):
                hop = Hop.from_dict(await resp.json())
        finally:
            await srv.close()
        assert hop.path == "/echo/x" and hop.headers["x-probe-added"] == "gateway" and hop.next is None

    run(go())


def test_outsider_fetch():
    async def go():
        target = TestServer(Service(ServiceConfig(role="pricing")).app(), host="127.0.0.1")
        outsider = TestServer(Service(ServiceConfig(role="outsider")).app())
        await target.start_server()
        await outsider.start_server()
        try:
            async with aiohttp.ClientSession() as s:
                # Only http:// to a cluster Service: an IP or external host is refused.
                async with s.get(outsider.make_url("/fetch"), params={"url": str(target.make_url("/healthz"))}) as r:
                    assert r.status == 400
                async with s.get(
                    outsider.make_url("/fetch"), params={"url": "http://no-such-svc.probe:1/healthz"}
                ) as r:
                    res = await r.json()
                assert r.status == 200 and res["errorKind"] == "dns", res
        finally:
            await target.close()
            await outsider.close()

    run(go())


def test_hop_round_trip():
    hop = Hop(
        service="frontend", status=200, caller="spiffe://x", next=Hop(service="catalog", version="v2", status=502)
    )
    d = hop.to_dict()
    assert "version" not in d and d["next"]["version"] == "v2" and d["elapsedMs"] == 0
    assert Hop.from_dict(d) == hop
    assert Hop.from_dict({"error": "not ours"}) is None


def test_ring_forgets_oldest():
    from platform_probe.service import Ring

    r = Ring(2)
    for i in ("a", "b", "c"):
        r.add(i)
    assert not r.has("a") and r.has("b") and r.has("c") and not r.has("")


def test_delay_does_not_block_others():
    """Delays are awaited, so one slow request doesn't hold up the next."""

    async def go():
        srv = TestServer(Service(ServiceConfig(role="pricing")).app())
        await srv.start_server()
        try:
            async with aiohttp.ClientSession() as s:

                async def get(h):
                    async with s.get(srv.make_url("/"), headers=h) as r:
                        return r.status

                start = time.monotonic()
                codes = await asyncio.gather(get({"x-probe-delay": "pricing=300ms"}), get({}))
                assert codes == [200, 200] and time.monotonic() - start < 0.6
        finally:
            await srv.close()

    run(go())
