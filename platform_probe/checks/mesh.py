"""Service mesh: features exercised between the probe's own services, from inside the mesh."""

from __future__ import annotations

from ..durations import rounded
from ..netpol import policies_allowing_outsider
from ..runner import (
    GROUP_MESH,
    Outcome,
    Probe,
    burst,
    catalog_version,
    check,
    codes_string,
    do,
    failed,
    hops,
    passed,
    q,
    skipped,
    trim,
)
from ..service import classify_error
from .gateway import split_outcome


def _traffic_off(p: Probe) -> Outcome | None:
    return None if p.cfg.expect.traffic_management else skipped("mesh.trafficManagement is off")


@check("mesh-mtls", GROUP_MESH, "Service-to-service calls carry SPIFFE identities")
async def check_mesh_mtls(p: Probe) -> Outcome:
    r = await p.front()
    f, c, pr = hops(r)
    if r.status != 200 or pr is None:
        return failed(f"chain broken: {r} {trim(r.body)}")
    ns, sa = p.cfg.namespace, p.cfg.service_accounts
    for hop, principal in (
        (f, p.cfg.principal(ns, sa.console)),
        (c, p.cfg.principal(ns, sa.frontend)),
        (pr, p.cfg.principal(ns, sa.catalog)),
    ):
        if hop.caller != principal:
            return failed(
                f"{hop.service} saw caller {q(hop.caller)}, expected {principal}",
                "An empty caller means that hop was plaintext.",
            )
    return passed("console → frontend → catalog → pricing, each caller identified by SPIFFE ID")


@check("mesh-request-id", GROUP_MESH, "Request ID propagated across hops")
async def check_request_id(p: Probe) -> Outcome:
    r = await p.front()
    f, c, pr = hops(r)
    if pr is None:
        return failed(f"chain broken: {r}")
    if not f.request_id or f.request_id != c.request_id or c.request_id != pr.request_id:
        return failed(
            f"x-request-id: frontend {q(f.request_id)}, catalog {q(c.request_id)}, pricing {q(pr.request_id)}"
        )
    return passed(f"x-request-id {f.request_id} on all three hops")


@check("mesh-strict", GROUP_MESH, "STRICT mTLS rejects plaintext")
async def check_strict(p: Probe) -> Outcome:
    if not p.cfg.expect.strict_mtls:
        return skipped("mesh.strictMTLS is off")
    try:
        fr = await p.from_outsider(p.cfg.services.catalog_v1 + "/healthz")
    except Exception as e:  # noqa: BLE001
        return failed(str(e))
    if fr.status:
        return failed(
            f"plaintext call from a pod without a sidecar got {fr.status}",
            "Is a STRICT PeerAuthentication in place for this namespace?",
        )
    if fr.error_kind == "timeout":
        return failed(fr.error, "The call timed out, so a NetworkPolicy dropped it before mTLS could reject it.")
    return passed(f"plaintext from a pod without a sidecar: connection {fr.error_kind}")


@check("mesh-authz", GROUP_MESH, "AuthorizationPolicy: only frontend may call catalog")
async def check_mesh_authz(p: Probe) -> Outcome:
    if not p.cfg.expect.authorization_policies:
        return skipped("mesh.authorizationPolicies is off")
    r = await p.call(p.cfg.services.catalog_v1, "/")
    if r.status != 403 or "RBAC" not in r.body:
        return failed(f"console → catalog: expected 403 RBAC: access denied, got {r} {trim(r.body)}")
    f = await p.front()
    if f.status != 200:
        return failed(f"console was denied as expected, but frontend → catalog then failed: {f}")
    return passed(f"console → catalog 403 ({trim(r.body)}); frontend → catalog 200")


@check("mesh-split", GROUP_MESH, "East-west split between catalog v1 and v2")
async def check_mesh_split(p: Probe) -> Outcome:
    if o := _traffic_off(p):
        return o
    return await split_outcome(p.cfg.mesh.split_v2_percent, 60, p.front, catalog_version)


@check("mesh-header", GROUP_MESH, "East-west header routing to catalog v2")
async def check_mesh_header(p: Probe) -> Outcome:
    if o := _traffic_off(p):
        return o
    for i in range(5):
        r = await p.front({"x-version": "v2"})
        if catalog_version(r) != "v2":
            return failed(f"request {i + 1} reached catalog {q(catalog_version(r))}")
    return passed("5 / 5 to catalog v2")


@check("mesh-retries", GROUP_MESH, "Retries hide pricing's injected failures")
async def check_retries(p: Probe) -> Outcome:
    if o := _traffic_off(p):
        return o
    headers = {"x-probe-scenario": "retry", "x-probe-fail": "pricing=0.3"}
    retried = 0
    for i in range(20):
        r = await p.front(headers)
        _, _, pr = hops(r)
        if r.status != 200 or pr is None:
            return failed(f"request {i + 1}: {r}", "Retries should hide pricing's failures from the caller.")
        if pr.attempt > 1:
            retried += 1
    if retried == 0:
        return failed("20 / 20 succeeded, but none needed a retry: is pricing failing on request?")
    return passed(f"20 / 20 succeeded; {retried} needed a retry (x-envoy-attempt-count > 1)")


@check("mesh-fault-abort", GROUP_MESH, "Fault injection: abort")
async def check_fault_abort(p: Probe) -> Outcome:
    if o := _traffic_off(p):
        return o
    aborted = ok = 0
    for i in range(20):
        r = await p.front({"x-probe-scenario": "fault-abort"})
        _, _, pr = hops(r)
        if r.status == 200:
            ok += 1
        elif pr is not None and pr.status == 503 and "fault filter abort" in pr.error:
            aborted += 1
        else:
            return failed(f"request {i + 1}: {r} {trim(r.body)}")
    if aborted < 3 or ok < 3:
        return failed(f"{aborted} aborted, {ok} succeeded (expected about half each)")
    return passed(f"{aborted} / 20 aborted with 503 by catalog's sidecar, {ok} succeeded")


@check("mesh-fault-delay", GROUP_MESH, "Fault injection: delay")
async def check_fault_delay(p: Probe) -> Outcome:
    if o := _traffic_off(p):
        return o
    for i in range(3):
        r = await p.front({"x-probe-scenario": "fault-delay"})
        _, c, _ = hops(r)
        if r.status != 200 or c is None or c.elapsed_ms < 1000:
            return failed(f"request {i + 1}: {r} after {rounded(r.dur)}")
    return passed("3 / 3 delayed by at least 1s before reaching pricing")


@check("mesh-timeout", GROUP_MESH, "Per-hop timeout")
async def check_mesh_timeout(p: Probe) -> Outcome:
    if o := _traffic_off(p):
        return o
    r = await p.front({"x-probe-scenario": "timeout", "x-probe-delay": "pricing=3s"})
    _, _, pr = hops(r)
    if pr is None or pr.status != 504 or r.dur > 2.5:
        return failed(f"expected catalog → pricing to time out after 1s, got {r} after {rounded(r.dur)}")
    return passed(f"catalog → pricing: 504 {pr.error} after {rounded(r.dur, 0.01)}")


@check("mesh-outlier", GROUP_MESH, "Outlier detection ejects the faulty pricing pod")
async def check_outlier(p: Probe) -> Outcome:
    if o := _traffic_off(p):
        return o
    # Each caller's sidecar counts errors and ejects on its own. Spread over catalog v1 and v2,
    # v2's few requests rarely reach 3 errors, so its sidecar keeps the faulty pod and fails
    # requests at the end of the burst. One subset means one sidecar sees every error.
    headers = {"x-probe-scenario": "outlier", "x-version": "v2"}
    codes = await burst(30, lambda: p.front(headers))
    n_failed = sum(c != 200 for c in codes)
    if any(c != 200 for c in codes[20:]):
        return failed(
            f"{n_failed} / 30 failed, still failing at the end: {codes_string(codes)}",
            "The faulty pod should have been ejected after 3 errors.",
        )
    if n_failed > 8:
        return failed(f"{n_failed} / 30 failed before ejection: {codes_string(codes)}")
    if n_failed == 0:
        # Either it is still ejected from a run less than 30s ago, or it isn't in the pool at all:
        # from here the two look the same, so this is no evidence either way.
        return skipped(
            "no failures, so nothing was ejected: the faulty pod is either still ejected from an earlier run "
            "or not in the pool. Run again in 30s."
        )
    return passed(f"{n_failed} failed, then the faulty pod was ejected and the rest succeeded")


@check("mesh-egress", GROUP_MESH, "Egress limited to the service registry")
async def check_egress(p: Probe) -> Outcome:
    if not p.cfg.egress.registry_only:
        return skipped("mesh.egress.registryOnly is off")
    blocked = p.cfg.egress.blocked_url
    r = await do(p.mesh, "GET", blocked)
    kind = classify_error(r.err) if r.err is not None else ""
    if r.status == 502:
        detail = f"{blocked} blocked (502 from the sidecar)"
    elif kind == "reset":
        detail = f"{blocked} blocked (connection reset by the sidecar)"
    elif kind == "dns":
        # Air-gapped clusters: the lookup fails before the sidecar sees anything.
        return skipped(
            f"{blocked} doesn't resolve here, so the sidecar never saw it. "
            "Set mesh.egress.blockedUrl to an external host that resolves."
        )
    else:
        return failed(f"{blocked}: got {r}", "REGISTRY_ONLY makes the sidecar refuse hosts outside the registry.")
    if allowed := p.cfg.egress.allowed_url:
        a = await do(p.mesh, "GET", allowed)
        if a.err is not None or a.status >= 500:
            return failed(
                f"{detail}; allowed {allowed}: got {a}", "Is there a ServiceEntry for it (mesh.egress.allowedHosts)?"
            )
        detail += f"; {allowed} allowed ({a.status})"
    return passed(detail)


@check("mesh-network-policy", GROUP_MESH, "NetworkPolicy blocks calls that bypass the gateway")
async def check_network_policy(p: Probe) -> Outcome:
    if not p.cfg.expect.network_policies:
        return skipped("networkPolicies.enabled is off")
    try:
        fr = await p.from_outsider(p.cfg.services.frontend + "/healthz")
    except Exception as e:  # noqa: BLE001
        return failed(str(e))
    if fr.error_kind == "timeout":
        return passed(f"outsider → frontend dropped (timed out after {fr.elapsed_ms}ms)")
    got = f"outsider → frontend: {fr.status} {fr.error_kind}"
    try:
        names = await policies_allowing_outsider(p)
    except Exception as e:  # noqa: BLE001
        return failed(
            f"{got} (couldn't read the NetworkPolicies: {e})",
            "Only the gateway and the console may reach the frontend.",
        )
    if names:
        return failed(
            f"{got}, allowed by NetworkPolicy {', '.join(names)}",
            "NetworkPolicies add up: one that allows a connection lets it through whatever the others deny. "
            "Test in a namespace without it, or accept that pods in this namespace can bypass the gateway.",
        )
    return failed(got, "No NetworkPolicy here allows it, so the network plugin isn't enforcing them.")
