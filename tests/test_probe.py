import base64
import json
import os
import secrets

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import GATEWAY_HOST, run

from platform_probe.checks import gateway, mesh, platform
from platform_probe.config import ConfigError, Limit, load_config, parse_config
from platform_probe.durations import format_duration, parse_duration
from platform_probe.junit import junit
from platform_probe.oidc import tamper
from platform_probe.runner import (
    GROUP_GATEWAY,
    GROUP_MESH,
    Check,
    Probe,
    Report,
    Resp,
    Result,
    Status,
    failed,
    passed,
)
from platform_probe.service import Hop


def test_limit_outcome():
    limit = Limit(count=3, window=10)
    assert gateway.limit_outcome(limit, [200, 200, 200, 429, 429]).status == Status.PASS
    assert gateway.limit_outcome(limit, [200, 200, 200, 200, 429]).status == Status.FAIL, "limit too high passed"
    assert gateway.limit_outcome(limit, [200, 429, 429, 429]).status == Status.FAIL, "limit hit early passed"
    assert gateway.limit_outcome(limit, [200, 200, 200, 429]).detail == "limit 3 / 10s: 200 200 200 429"


def _sequence(v2_every: int):
    i = 0

    async def send():
        nonlocal i
        i += 1
        v = "v2" if v2_every and i % v2_every == 0 else "v1"
        return Resp(status=200, hop=Hop(service="catalog", version=v))

    return send


@pytest.mark.parametrize(("v2_every", "want"), [(10, Status.PASS), (0, Status.FAIL), (2, Status.FAIL)])
def test_split_outcome(v2_every, want):
    out = run(gateway.split_outcome(10, 60, _sequence(v2_every), lambda r: r.hop.version))
    assert out.status == want, out


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def test_tamper():
    """Every tampered signature must decode to different bytes, including when the last base64url
    character carries padding bits (an RS256 signature ending in "A" was the case that slipped)."""
    for i in range(2000):
        sig = bytearray(secrets.token_bytes(256))
        if i == 0:
            sig[255] = 0  # encodes to a final "A"
        token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0." + _b64(bytes(sig))
        parts = tamper(token).split(".")
        got = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        assert got != bytes(sig) and parts[:2] == ["eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJ4In0"], i
    assert tamper("not-a-jwt") != "not-a-jwt"


def test_junit():
    out = junit(
        Report(
            results=[
                Result("a", GROUP_GATEWAY, "one", Status.PASS),
                Result("b", GROUP_GATEWAY, "two", Status.FAIL, "got 200", "fix it"),
                Result("c", GROUP_MESH, "three", Status.SKIP, "off"),
            ],
            failed=1,
            skipped=1,
        )
    ).decode()
    for want in (
        '<testsuite name="API gateway" tests="2" failures="1"',
        '<failure message="got 200">fix it</failure>',
        '<skipped message="off"',
    ):
        assert want in out, out


@pytest.mark.parametrize(
    ("text", "seconds", "formatted"),
    [
        ("10s", 10, "10s"),
        ("1m", 60, "1m0s"),
        ("1m30s", 90, "1m30s"),
        ("500ms", 0.5, "500ms"),
        ("1.5s", 1.5, "1.5s"),
        ("1h", 3600, "1h0m0s"),
    ],
)
def test_durations(text, seconds, formatted):
    assert parse_duration(text) == pytest.approx(seconds)
    assert format_duration(seconds) == formatted


@pytest.mark.parametrize("bad", ["", "10", "s", "10x", "1s2"])
def test_bad_durations(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


# --- a fake gateway, and the gateway checks against it -------------------------------------------


def fake_gateway(p: Probe) -> web.Application:
    """Answers like a gateway with the probe's routes and policies in place."""
    cfg = p.cfg
    gw_principal = cfg.principal(cfg.gateway.namespace, cfg.gateway.service_account)

    def hop(request: web.Request, h: Hop) -> web.Response:
        h.path, h.caller, h.status = request.path, gw_principal, 200
        return web.json_response(h.to_dict())

    async def handle(request: web.Request) -> web.Response:
        if request.host.split(":")[0] != cfg.gateway.host:
            return web.Response(status=404, text="wrong host " + request.host)
        auth = request.headers.get("Authorization", "")
        path = request.path
        if path == "/locked":
            return web.Response(status=403, text="no AuthPolicy")
        if path.startswith("/public"):
            return hop(request, Hop(service="frontend"))
        if path == "/api":
            if auth == "APIKEY alice-key":
                return hop(request, Hop(service="frontend", next=Hop(service="catalog", version="v1")))
            if auth == "APIKEY bob-key":
                if request.method == "POST":
                    return web.Response(status=403, text="forbidden")
                return hop(request, Hop(service="frontend"))
            return web.Response(status=401, text="unauthorized")
        return web.Response(status=404, text="not found")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def make_config(tmp_path, **gateway_overrides):
    keys = tmp_path / "apikeys"
    keys.mkdir()
    (keys / "alice").write_text("alice-key\n")
    (keys / "bob").write_text("bob-key")
    cfg = parse_config(
        {
            "release": "probe",
            "namespace": "platform-probe",
            "gateway": {"name": "api-gateway", "namespace": "api-gateway", "host": GATEWAY_HOST, "address": "127.0.0.1"}
            | gateway_overrides,
            "expect": {"defaultDeny": True},
            "apiKeys": {"dir": str(keys)},
        }
    )
    return cfg


async def with_gateway(cfg, server_ssl, fn):
    """Start the fake gateway over TLS, point the probe's HTTPS port at it, and run fn(probe)."""
    p = Probe(cfg, checks=[])
    srv = TestServer(fake_gateway(p), host="127.0.0.1")
    await srv.start_server(ssl=server_ssl)
    cfg.gateway.https_port = srv.port
    try:
        return await fn(p)
    finally:
        await p.close()
        await srv.close()


GATEWAY_CHECKS = [
    gateway.check_tls,
    gateway.check_routing,
    gateway.check_default_deny,
    gateway.check_no_credentials,
    gateway.check_wrong_key,
    gateway.check_valid_key,
    gateway.check_admin_writes,
    gateway.check_gateway_mtls,
]

# Not configured, so skipped rather than failed.
SKIPPED_CHECKS = [
    gateway.check_redirect,
    gateway.check_oidc,
    mesh.check_strict,
    mesh.check_egress,
    mesh.check_network_policy,
]


def test_gateway_checks(tmp_path, server_ssl):
    cfg = make_config(tmp_path, insecureSkipVerify=True)

    async def go(p):
        return [(fn.__name__, await fn(p)) for fn in GATEWAY_CHECKS + SKIPPED_CHECKS]

    results = run(with_gateway(cfg, server_ssl, go))
    for name, out in results[: len(GATEWAY_CHECKS)]:
        assert out.status == Status.PASS, (name, out)
    for name, out in results[len(GATEWAY_CHECKS) :]:
        assert out.status == Status.SKIP, (name, out)
    tls = results[0][1]
    assert tls.detail.startswith("issuer probe-test-ca, 8") and "not verified" in tls.detail, tls


def test_tls_verified_with_trusted_ca(tmp_path, server_ssl, server_cert):
    """With the CA bundle trusted, the certificate verifies against the probe hostname."""
    cfg = make_config(tmp_path, issuerContains="probe-test-ca")
    cfg.ca_file = str(server_cert[0])
    out = run(with_gateway(cfg, server_ssl, gateway.check_tls))
    assert out.status == Status.PASS and out.detail.endswith(", verified"), out
    # And the requests through the gateway verify too.
    assert run(with_gateway(cfg, server_ssl, gateway.check_valid_key)).status == Status.PASS


def test_tls_untrusted_ca_hint(tmp_path, server_ssl):
    out = run(with_gateway(make_config(tmp_path), server_ssl, gateway.check_tls))
    assert out.status == Status.FAIL and "trustedCA" in out.hint, out


def test_tls_wrong_issuer(tmp_path, server_ssl):
    cfg = make_config(tmp_path, insecureSkipVerify=True, issuerContains="internal-ca")
    out = run(with_gateway(cfg, server_ssl, gateway.check_tls))
    assert out.status == Status.FAIL and "internal-ca" in out.detail, out


def test_platform_checks_without_cluster(tmp_path):
    async def go():
        p = Probe(make_config(tmp_path), checks=[])
        try:
            return await platform.check_kuadrant(p)
        finally:
            await p.close()

    out = run(go())
    assert out.status == Status.FAIL and "no Kubernetes API access" in out.detail, out


def test_run_and_report(tmp_path):
    async def ok(_p):
        return passed("fine")

    async def boom(_p):
        raise RuntimeError("bad")

    async def go():
        p = Probe(
            make_config(tmp_path),
            checks=[Check("ok", GROUP_GATEWAY, "ok", ok), Check("boom", GROUP_GATEWAY, "boom", boom)],
        )
        assert await p.run()
        await p.close()
        return p.report()

    r = run(go())
    assert not r.running and r.passed == 1 and r.failed == 1 and "crashed" in r.results[1].detail, r
    assert Report.from_dict(json.loads(json.dumps(r.to_dict()))).results == r.results


def test_second_run_refused(tmp_path):
    p = Probe(make_config(tmp_path), checks=[Check("x", GROUP_GATEWAY, "x", lambda _p: failed("never"))])
    assert p.begin() and not p.begin()


# --- config --------------------------------------------------------------------------------------


def test_rendered_chart_config():
    """The config the chart renders must load, with every field the checks depend on set.
    CI renders the chart and points RENDERED_CONFIG at the result."""
    path = os.environ.get("RENDERED_CONFIG")
    if not path:
        pytest.skip("RENDERED_CONFIG not set")
    c = load_config(path)
    for name, v in {
        "gateway.host": c.gateway.host,
        "gateway.serviceAccount": c.gateway.service_account,
        "services.frontend": c.services.frontend,
        "services.catalogV2": c.services.catalog_v2,
        "services.outsider": c.services.outsider,
        "serviceAccounts.console": c.service_accounts.console,
        "serviceAccounts.catalog": c.service_accounts.catalog,
        "apiKeys.dir": c.api_keys.dir,
        "kuadrant.namespace": c.kuadrant.namespace,
    }.items():
        assert v, f"{name} is empty"
    for name, limit in {"public": c.limits.public, "free": c.limits.free, "gold": c.limits.gold}.items():
        assert limit.count > 0 and limit.window > 0, (name, limit)
    assert c.routes.timeout > 0 and c.routes.split_v2_percent > 0 and c.mesh.split_v2_percent > 0


@pytest.mark.parametrize(
    "doc",
    [
        {"gateway": {"hostname": "typo"}},
        {"gateway": {"httpsPort": "443"}},
        {"expect": {"strictMTLS": "yes"}},
        {"limits": {"free": {"limit": 3, "window": "10 seconds"}}},
    ],
)
def test_load_config_rejects(tmp_path, doc):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_load_config(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(
        '{"gateway":{"name":"gw","namespace":"gwns","host":"p.example"},'
        '"limits":{"free":{"limit":3,"window":"10s"}},"routes":{"timeout":"3s"},"expect":{"strictMTLS":true}}'
    )
    c = load_config(str(path))
    assert c.gateway.address == "gw-istio.gwns.svc"
    assert c.gateway.service_account == "gw-istio"
    assert c.limits.free.window == 10 and c.routes.timeout == 3 and c.expect.strict_mtls
    assert c.principal("ns", "sa") == "spiffe://cluster.local/ns/ns/sa/sa"


def test_categories_and_selected_runs(tmp_path):
    async def ok(_p):
        return passed("fine")

    checks = [Check("gateway-x", GROUP_GATEWAY, "x", ok), Check("mesh-y", GROUP_MESH, "y", ok)]
    p = Probe(make_config(tmp_path), checks=checks)
    assert p.categories() == [{"id": "gateway", "name": GROUP_GATEWAY}, {"id": "mesh", "name": GROUP_MESH}]
    with pytest.raises(ValueError):
        p.begin(["nope"])
    assert run(p.run(["mesh"]))
    assert [r.id for r in p.report().results] == ["mesh-y"] and p.report().total == 1
    assert [r.id for r in p.latest()] == ["mesh-y"]
    assert run(p.run(["gateway"]))
    assert [r.id for r in p.latest()] == ["gateway-x", "mesh-y"]
