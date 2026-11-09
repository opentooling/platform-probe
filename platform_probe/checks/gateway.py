"""API gateway: features exercised with real requests through the gateway."""

from __future__ import annotations

import asyncio
import math
import ssl
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from cryptography import x509
from cryptography.x509.oid import NameOID

from ..config import Limit
from ..durations import format_duration, rounded
from ..oidc import oidc_token, tamper
from ..runner import (
    GROUP_GATEWAY,
    Outcome,
    Probe,
    Resp,
    Status,
    burst,
    chain,
    check,
    codes_string,
    do,
    failed,
    new_id,
    passed,
    q,
    skipped,
    trim,
    version,
    wait_window,
)

# OpenSSL verify codes that mean "no trusted CA for this chain".
_UNKNOWN_AUTHORITY = {2, 18, 19, 20, 21}


@check("gateway-tls", GROUP_GATEWAY, "TLS certificate for the probe hostname")
async def check_tls(p: Probe) -> Outcome:
    g = p.cfg.gateway
    try:
        async with asyncio.timeout(5):
            _, writer = await asyncio.open_connection(
                g.address, g.https_port, ssl=p.gateway_ssl(), server_hostname=g.host
            )
    except ssl.SSLCertVerificationError as e:
        hint = ""
        if e.verify_code in _UNKNOWN_AUTHORITY:
            hint = (
                "The console doesn't trust the gateway's CA. Set trustedCA in the chart values, "
                "or add the CA to the cluster trust bundle."
            )
        return failed(f"{g.host}: {e.verify_message or e}", hint)
    except Exception as e:  # noqa: BLE001
        return failed(f"{g.host}: {e or type(e).__name__}")
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    cert = x509.load_der_x509_certificate(der)
    days = int((cert.not_valid_after_utc - datetime.now(UTC)).total_seconds() / 86400)
    issuer = cert.issuer.rfc4514_string()
    if g.issuer_contains and g.issuer_contains not in issuer:
        return failed(
            f"issuer {q(issuer)} doesn't contain {q(g.issuer_contains)}",
            "Is the TLSPolicy using the issuer you expect?",
        )
    if days < 7:
        return failed(f"expires in {days} days (issuer {issuer})", "cert-manager should have renewed it by now.")
    cn = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    verified = "not verified (insecureSkipVerify)" if g.insecure_skip_verify else "verified"
    return passed(f"issuer {cn[0].value if cn else ''}, {days} days left, {verified}")


@check("gateway-redirect", GROUP_GATEWAY, "HTTP redirects to HTTPS")
async def check_redirect(p: Probe) -> Outcome:
    if not p.cfg.expect.http_redirect:
        return skipped("expect.httpRedirect is off")
    r = await do(p.gateway, "GET", f"http://{p.cfg.gateway.host}/public")
    loc = r.header("Location")
    if r.status != 301 or not loc.startswith("https://"):
        return failed(f"got {r} {loc}", "Is the platform's HTTP→HTTPS redirect route in place?")
    return passed(f"301 {loc}")


@check("gateway-routing", GROUP_GATEWAY, "Host and path routing")
async def check_routing(p: Probe) -> Outcome:
    r = await p.gw("GET", "/public/routing")
    if r.status != 200 or r.hop is None or r.hop.service != "frontend" or r.hop.path != "/public/routing":
        return failed(f"/public/routing: got {r} {trim(r.body)}")
    miss = await p.gw("GET", "/no-such-route-" + new_id())
    if miss.status != 404:
        return failed(f"unmatched path: expected 404, got {miss}")
    return passed("/public → frontend (200), unmatched path → 404")


@check("gateway-default-deny", GROUP_GATEWAY, "Default deny on a route without AuthPolicy")
async def check_default_deny(p: Probe) -> Outcome:
    if not p.cfg.expect.default_deny:
        return skipped("expect.defaultDeny is off")
    r = await p.gw("GET", "/locked")
    if r.status != 403:
        return failed(f"expected 403, got {r}", "Is there a deny-all AuthPolicy on the gateway?")
    return passed(f"403 {trim(r.body)}")


@check("gateway-no-credentials", GROUP_GATEWAY, "Request without credentials is rejected")
async def check_no_credentials(p: Probe) -> Outcome:
    r = await p.gw("GET", "/api")
    if r.status != 401:
        return failed(f"expected 401, got {r}")
    return passed("401")


@check("gateway-wrong-key", GROUP_GATEWAY, "Wrong API key is rejected")
async def check_wrong_key(p: Probe) -> Outcome:
    r = await p.gw("GET", "/api", {"Authorization": p.cfg.api_keys.prefix + " not-a-real-key"})
    if r.status != 401:
        return failed(f"expected 401, got {r}")
    return passed("401")


def missing_keys(p: Probe) -> Outcome | None:
    for user in ("alice", "bob"):
        if not p.cfg.api_key(user):
            return failed(f"no API key for {user}", "The console mounts the keys from the <release>-apikeys Secret.")
    return None


@check("gateway-api-key", GROUP_GATEWAY, "Valid API key is accepted")
async def check_valid_key(p: Probe) -> Outcome:
    if o := missing_keys(p):
        return o
    r = await p.gw("GET", "/api", p.api_key("alice"))
    if r.status != 200 or r.hop is None:
        return failed(
            f"alice: got {r} {trim(r.body)}",
            "A 401 here means Authorino doesn't see the key Secret: check auth.apiKeys.namespace.",
        )
    return passed(f"alice: 200 via {chain(r)}")


@check("gateway-oidc", GROUP_GATEWAY, "OIDC token accepted, tampered token rejected")
async def check_oidc(p: Probe) -> Outcome:
    if not p.cfg.oidc.enabled:
        return skipped("auth.oidc.enabled is off")
    try:
        token = await oidc_token(p)
    except Exception as e:  # noqa: BLE001
        return failed(
            str(e) or type(e).__name__,
            "The console couldn't get a token from the IdP. Check the client and the trusted CA.",
        )
    ok = await p.gw("GET", "/api", {"Authorization": "Bearer " + token})
    if ok.status != 200:
        return failed(
            f"valid token: got {ok} {trim(ok.body)}",
            "A 401 means Authorino rejected the token. If the IdP's certificate comes from an internal CA, "
            "Authorino must trust it.",
        )
    bad = await p.gw("GET", "/api", {"Authorization": "Bearer " + tamper(token)})
    if bad.status != 401:
        return failed(f"tampered token: expected 401, got {bad}")
    return passed("valid token 200, tampered 401")


@check("gateway-authz", GROUP_GATEWAY, "Only group admin may POST")
async def check_admin_writes(p: Probe) -> Outcome:
    if o := missing_keys(p):
        return o
    bob = await p.gw("POST", "/api", p.api_key("bob"))
    alice = await p.gw("POST", "/api", p.api_key("alice"))
    if bob.status != 403 or alice.status != 200:
        return failed(f"POST: bob (no group) expected 403, got {bob}; alice (admin) expected 200, got {alice}")
    return passed("POST: bob 403, alice 200")


@check("gateway-mtls", GROUP_GATEWAY, "Gateway to app is mutual TLS")
async def check_gateway_mtls(p: Probe) -> Outcome:
    if o := missing_keys(p):
        return o
    r = await p.gw("GET", "/api", p.api_key("alice"))
    want = p.cfg.principal(p.cfg.gateway.namespace, p.cfg.gateway.service_account)
    if r.hop is None:
        return failed(f"got {r}")
    if r.hop.caller != want:
        return failed(
            f"frontend saw caller {q(r.hop.caller)}, expected {want}",
            "No caller identity means the hop wasn't mTLS: does the frontend pod have a sidecar?",
        )
    return passed(want)


@check("gateway-header-match", GROUP_GATEWAY, "Header match routes x-version: v2 to catalog v2")
async def check_header_match(p: Probe) -> Outcome:
    headers = p.api_key("alice") | {"x-version": "v2"}
    for i in range(5):
        r = await p.gw("GET", "/split", headers)
        if r.hop is None or r.hop.version != "v2":
            return failed(f"request {i + 1}: got {r} {version(r)}")
    return passed("5 / 5 to catalog v2")


async def split_outcome(
    pct: int, n: int, send: Callable[[], Awaitable[Resp]], version_of: Callable[[Resp], str]
) -> Outcome:
    """Send n requests and check the share that reached v2 is plausible for a weighted split of
    pct percent: within four standard deviations, and never zero for 0 < pct < 100."""
    counts: Counter[str] = Counter()
    for _ in range(n):
        r = await send()
        if r.status != 200:
            return failed(f"got {r} {trim(r.body)}")
        counts[version_of(r)] += 1
    v2 = counts["v2"]
    exp = n * pct / 100
    sd = math.sqrt(n * pct / 100 * (1 - pct / 100))
    lo, hi = max(exp - 4 * sd, 0), exp + 4 * sd
    if 0 < pct < 100:
        lo = max(lo, 1)
    detail = f"{counts['v1']} to v1, {v2} to v2 (target {pct}% to v2)"
    if v2 < lo or v2 > hi or counts["v1"] + v2 != n:
        return failed(detail)
    return passed(detail)


@check("gateway-split", GROUP_GATEWAY, "Weighted split between catalog v1 and v2")
async def check_gateway_split(p: Probe) -> Outcome:
    return await split_outcome(
        p.cfg.routes.split_v2_percent, 60, lambda: p.gw("GET", "/split", p.api_key("alice")), version
    )


@check("gateway-rewrite", GROUP_GATEWAY, "URL rewrite and header filters")
async def check_rewrite(p: Probe) -> Outcome:
    r = await p.gw("GET", "/rewrite/x", p.api_key("alice") | {"x-probe-remove": "please"})
    if r.hop is None:
        return failed(f"got {r} {trim(r.body)}")
    problems = []
    if r.hop.path != "/echo/x":
        problems.append(f"path {q(r.hop.path)}, expected /echo/x")
    if r.hop.headers.get("x-probe-added") != "gateway":
        problems.append("request header x-probe-added missing")
    if "x-probe-remove" in r.hop.headers:
        problems.append("request header x-probe-remove not removed")
    if r.header("x-probe-response") != "gateway":
        problems.append("response header x-probe-response missing")
    if problems:
        return failed("; ".join(problems))
    return passed("/rewrite/x → /echo/x, request header added and removed, response header added")


@check("gateway-mirror", GROUP_GATEWAY, "Request mirroring to catalog v2")
async def check_mirror(p: Probe) -> Outcome:
    probe_id = "mirror-" + new_id()
    r = await p.gw("GET", "/mirror", p.api_key("alice") | {"x-probe-id": probe_id})
    if r.status != 200 or r.hop is None or r.hop.version != "v1":
        return failed(f"primary: got {r} {version(r)}")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        seen = await p.call(p.cfg.services.catalog_v2, "/seen?id=" + probe_id)
        if seen.status == 200 and "true" in seen.body:
            return passed("answered by v1, copy reached v2")
        if seen.status == 403:
            return failed("catalog v2 /seen: 403", "The catalog AuthorizationPolicy must allow the console on /seen.")
        await asyncio.sleep(0.3)
    return failed("v1 answered, but no mirrored copy reached v2 in 5s")


@check("gateway-timeout", GROUP_GATEWAY, "Route timeout returns 504")
async def check_route_timeout(p: Probe) -> Outcome:
    t = p.cfg.routes.timeout
    r = await p.gw("GET", "/timeout", p.api_key("alice") | {"x-probe-delay": "frontend=" + format_duration(t + 2)})
    if r.status != 504 or r.dur > t + 1.5:
        return failed(f"expected 504 after about {format_duration(t)}, got {r} after {rounded(r.dur)}")
    return passed(f"504 after {rounded(r.dur, 0.01)} (timeout {format_duration(t)})")


def limit_outcome(limit: Limit, codes: list[int]) -> Outcome:
    """Expect the first limit.count requests through and 429 after them."""
    for i, c in enumerate(codes):
        want = 200 if i < limit.count else 429
        if c != want:
            return failed(
                f"limit {limit}: {codes_string(codes)}",
                "A limit that is exceeded late usually means other traffic shares the counter.",
            )
    return passed(f"limit {limit}: {codes_string(codes)}")


@check("gateway-public-limit", GROUP_GATEWAY, "Route rate limit on the public route")
async def check_public_limit(p: Probe) -> Outcome:
    limit = p.cfg.limits.public
    await wait_window(limit)
    codes = await burst(limit.count + 3, lambda: p.gw("GET", "/public"))
    return limit_outcome(limit, codes)


@check("gateway-plan-limit", GROUP_GATEWAY, "Per-caller limits by plan")
async def check_plan_limits(p: Probe) -> Outcome:
    if o := missing_keys(p):
        return o
    limit = p.cfg.limits.free
    await wait_window(limit)
    codes = await burst(limit.count + 3, lambda: p.gw("GET", "/api", p.api_key("bob")))
    out = limit_outcome(limit, codes)
    if out.status != Status.PASS:
        out.detail = "bob (free) " + out.detail
        return out
    alice = await p.gw("GET", "/api", p.api_key("alice"))
    if alice.status != 200:
        return failed(
            f"bob limited as expected, but alice then got {alice}", "Each caller should have its own counter."
        )
    return passed(f"bob (free) {out.detail.removeprefix('limit ')}; alice (gold) unaffected")
