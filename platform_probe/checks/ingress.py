"""Ingress & certificates: what a team gets when it exposes an app with a Route.

The checks create a backend (the probe's own pricing service, on HTTP and on HTTPS with a
service-CA certificate) and one Route per TLS termination in the scratch namespace, then fetch
each Route from a pod in that namespace, the way a tenant's client would.
"""

from __future__ import annotations

import re

from .. import kube, scratch
from ..kube import StatusError
from ..runner import GROUP_INGRESS, Outcome, Probe, check, failed, first_non_empty, passed, q, skipped

SUITE = "ingress"
SERVICE_CA = "openshift-service-serving-signer"
# How long a Certificate may take to be issued.
CERT_TIMEOUT = 120.0


@check("ingress-controller", GROUP_INGRESS, "Default IngressController available", SUITE)
async def check_ingress_controller(p: Probe) -> Outcome:
    try:
        ic = await p.kube.get(
            "/apis/operator.openshift.io/v1/namespaces/openshift-ingress-operator/ingresscontrollers/default"
        )
    except StatusError as e:
        if e.code == 404:
            return failed("no IngressController named default", "Is this an OpenShift cluster?")
        return failed(str(e))
    conds = kube.status_conditions(ic)
    avail, degraded = kube.find(conds, "Available"), kube.find(conds, "Degraded")
    st = ic.get("status") or {}
    if avail.status != "True" or degraded.status == "True":
        bad = degraded if degraded.status == "True" else avail
        return failed(f"default: {bad.type}={bad.status}: {first_non_empty(bad.message, bad.reason)}")
    return passed(f"default: domain {st.get('domain', '?')}, {st.get('availableReplicas', '?')} replicas")


async def _route(p: Probe, termination: str) -> tuple[str, Outcome | None]:
    """Create a Route of the given termination to the backend and wait for the router to admit
    it. Returns its host, or the outcome to report if that didn't work."""
    if not p.cfg.scratch.backend_service:
        return "", failed("no ingress backend Service in the config (scratch.backendService)")
    try:
        backend = await scratch.ensure_backend(p)
    except Exception as e:  # noqa: BLE001
        return "", failed(
            str(e),
            "The backend uses a certificate from OpenShift's service CA, and the probe's image: "
            "check the image can be pulled in the scratch namespace (scratch.imagePullSecrets).",
        )
    route_name = scratch.name(p, termination)
    tls: dict = {"termination": termination}
    if termination == "edge":
        tls["insecureEdgeTerminationPolicy"] = "Redirect"
    route = {
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": scratch.meta(p, route_name),
        "spec": {
            "to": {"kind": "Service", "name": backend},
            "port": {"targetPort": "http" if termination == "edge" else "https"},
            "tls": tls,
        },
    }
    try:
        await p.kube.create(scratch.path(p, "/apis/route.openshift.io/v1/routes"), route)
    except StatusError as e:
        return "", failed(f"creating the Route: {e}")

    def admitted(r: dict) -> bool:
        ing = (r.get("status") or {}).get("ingress") or []
        return bool(ing) and kube.find(kube.conditions(ing[0].get("conditions")), "Admitted").status in (
            "True",
            "False",
        )

    r = await scratch.wait_for(
        lambda: p.kube.get(scratch.path(p, "/apis/route.openshift.io/v1/routes", route_name)), admitted, 30
    )
    ing = ((r.get("status") or {}).get("ingress") or [{}])[0]
    adm = kube.find(kube.conditions(ing.get("conditions")), "Admitted")
    if adm.status != "True":
        return "", failed(
            f"route not admitted: {first_non_empty(adm.message, adm.reason, 'no answer from the router in 30s')}"
        )
    return ing.get("host", ""), None


def _answer(res: dict) -> str:
    if res.get("error"):
        return res["error"]
    return f"{res.get('status')} from {res.get('service') or 'something else: ' + res.get('body', '')}"


async def _fetch(p: Probe, termination: str, requests: list[dict]) -> tuple[list[dict], Outcome | None]:
    run = await scratch.run_task(p, "client-" + termination, "fetch", {"requests": requests}, timeout=120)
    if not run.ok:
        return [], failed(f"client pod: {run.explain()}")
    return run.data.get("results", []), None


@check("ingress-route-edge", GROUP_INGRESS, "Edge Route: trusted certificate, HTTP redirects to HTTPS", SUITE)
async def check_edge(p: Probe) -> Outcome:
    host, out = await _route(p, "edge")
    if out:
        return out
    results, out = await _fetch(p, "edge", [{"url": f"https://{host}/"}, {"url": f"http://{host}/"}])
    if out:
        return out
    tls, plain = results
    if tls.get("errorKind") == "untrusted":
        return failed(
            f"{host}: {tls['error']}",
            "A pod in a tenant namespace doesn't trust the router's certificate. Replace the default "
            "ingress certificate, or add its CA to the cluster trust bundle.",
        )
    if tls.get("status") != 200 or tls.get("service") != "pricing":
        return failed(
            f"https://{host}/: {_answer(tls)}",
            "A 503 from the router usually means a NetworkPolicy keeps it from the pod.",
        )
    cert = tls.get("cert") or {}
    days, limit = cert.get("days", 0), p.cfg.suites.ingress.cert_expiry_days
    if days < limit:
        return failed(f"router certificate expires in {days} days ({cert.get('notAfter')}), under {limit}")
    if plain.get("status") not in (301, 302, 307, 308) or not str(plain.get("location", "")).startswith("https://"):
        return failed(f"http://{host}/: expected a redirect to HTTPS, got {_answer(plain)}")
    return passed(
        f"{host}: 200 over HTTPS (issuer {cert.get('issuerCN')}, {days} days left), HTTP → {plain['status']} redirect"
    )


@check("ingress-route-reencrypt", GROUP_INGRESS, "Reencrypt Route to a service-CA certificate", SUITE)
async def check_reencrypt(p: Probe) -> Outcome:
    host, out = await _route(p, "reencrypt")
    if out:
        return out
    results, out = await _fetch(p, "reencrypt", [{"url": f"https://{host}/"}])
    if out:
        return out
    res = results[0]
    if res.get("status") != 200 or res.get("service") != "pricing":
        return failed(
            f"https://{host}/: {_answer(res)}",
            "The router must trust the service CA for reencrypt Routes without destinationCACertificate.",
        )
    return passed(f"{host}: 200, router → pod re-encrypted with the service CA certificate")


@check("ingress-route-passthrough", GROUP_INGRESS, "Passthrough Route: TLS ends at the pod", SUITE)
async def check_passthrough(p: Probe) -> Outcome:
    host, out = await _route(p, "passthrough")
    if out:
        return out
    results, out = await _fetch(p, "passthrough", [{"url": f"https://{host}/", "verify": False}])
    if out:
        return out
    res = results[0]
    if res.get("status") != 200 or res.get("service") != "pricing":
        return failed(f"https://{host}/: {_answer(res)}")
    issuer = (res.get("cert") or {}).get("issuer", "")
    if SERVICE_CA not in issuer:
        return failed(
            f"the client saw a certificate from {issuer or 'nobody'}, not the pod's", "The router terminated TLS."
        )
    return passed(f"{host}: 200 with the pod's own certificate ({SERVICE_CA})")


def _render(template: str, p: Probe) -> str:
    """Fill a name template's {run}, {namespace} and {release}."""
    for key, value in (("run", p.run_id), ("namespace", scratch.ns(p)), ("release", p.cfg.release)):
        template = template.replace("{" + key + "}", value)
    return template


def _asked(spec: dict) -> str:
    out = f"CN {spec['commonName']}, " if spec.get("commonName") else "no CN, "
    return out + "DNS " + ", ".join(spec.get("dnsNames") or [])


def _policy_refusal(e: StatusError) -> bool:
    """Whether an admission webhook (Kyverno, Gatekeeper) refused the request."""
    m = e.message.lower()
    return e.code in (400, 403, 422) and ("admission webhook" in m or "kyverno" in m or "policy" in m)


def _name_problems(info: dict, cfg, requested: list[str]) -> list[str]:
    """How the issued certificate differs from what was asked for and what the patterns allow."""
    problems = []
    cn, sans = info.get("commonName", ""), info.get("dnsNames") or []
    if cfg.cert_common_name and not cn:
        problems.append("no common name")
    if cfg.cert_common_name_pattern and cn and not re.fullmatch(cfg.cert_common_name_pattern, cn):
        problems.append(f"common name {q(cn)} doesn't match {q(cfg.cert_common_name_pattern)}")
    if cfg.cert_dns_name_pattern:
        off = [n for n in sans if not re.fullmatch(cfg.cert_dns_name_pattern, n)]
        if off:
            problems.append(f"DNS names {', '.join(off)} don't match {q(cfg.cert_dns_name_pattern)}")
    missing = [n for n in requested if n not in sans]
    if missing:
        problems.append(f"requested DNS names missing: {', '.join(missing)}")
    return problems


@check("ingress-cert-manager", GROUP_INGRESS, "cert-manager issues a certificate with the expected names", SUITE)
async def check_cert_manager(p: Probe) -> Outcome:
    cfg = p.cfg.suites.ingress
    issuer = cfg.cert_manager_issuer
    if not issuer.name:
        return skipped("suites.ingress.certManager.issuer.name is empty")
    for key, pattern in (
        ("commonNamePattern", cfg.cert_common_name_pattern),
        ("dnsNamePattern", cfg.cert_dns_name_pattern),
    ):
        try:
            re.compile(pattern)
        except re.error as e:
            return failed(f"suites.ingress.certManager.expect.{key} isn't a valid regular expression: {e}")
    cert_name = scratch.name(p, "cert")
    secret = f"{p.cfg.release}-probe-certificate"
    dns_names = [_render(t, p) for t in cfg.cert_dns_names] or [f"{cert_name}.{scratch.ns(p)}.svc"]
    spec = {
        # One Secret per release, reissued every run as long as a name changes with {run}.
        "secretName": secret,
        "dnsNames": dns_names,
        "issuerRef": {"name": issuer.name, "kind": issuer.kind, "group": "cert-manager.io"},
    }
    if cfg.cert_common_name:
        spec["commonName"] = _render(cfg.cert_common_name, p)
    cert = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": scratch.meta(p, cert_name),
        "spec": spec,
    }
    try:
        await p.kube.create(scratch.path(p, "/apis/cert-manager.io/v1/certificates"), cert)
    except StatusError as e:
        if e.code == 404:
            return failed("no certificates.cert-manager.io API", "Is cert-manager installed?")
        if _policy_refusal(e):
            return failed(
                f"a policy refused the Certificate for {_asked(spec)}: {e.message[:300]}",
                "Set suites.ingress.certManager.commonName and dnsNames to names the policy allows.",
            )
        return failed(f"creating the Certificate: {e}")
    c = await scratch.wait_for(
        lambda: p.kube.get(scratch.path(p, "/apis/cert-manager.io/v1/certificates", cert_name)),
        lambda o: kube.find(kube.status_conditions(o), "Ready").status == "True",
        CERT_TIMEOUT,
    )
    ready = kube.find(kube.status_conditions(c), "Ready")
    asked = f"CN {spec['commonName']}, " if "commonName" in spec else ""
    asked += "DNS " + ", ".join(dns_names)
    if ready.status != "True":
        return failed(
            f"{issuer.kind}/{issuer.name} for {asked}: not Ready after {CERT_TIMEOUT:.0f}s: "
            f"{first_non_empty(ready.message, ready.reason, 'no status')}",
            "A refusal from the issuer (e.g. a Vault role's allowed_domains) shows in kubectl describe "
            "certificaterequest in the scratch namespace; certManager.commonName and dnsNames set what is asked for.",
        )
    # Read back what was issued, from a pod that mounts the Secret: the console can't read Secrets.
    run = await scratch.run_task(
        p,
        "certinfo",
        "certinfo",
        {"path": "/tls/tls.crt"},
        volumes=[{"name": "tls", "secret": {"secretName": secret}}],
        mounts=[{"name": "tls", "mountPath": "/tls", "readOnly": True}],
    )
    if not run.ok:
        return failed(f"{issuer.kind}/{issuer.name} issued it, but reading it back failed: {run.explain()}")
    info = run.data
    problems = _name_problems(info, cfg, dns_names)
    got = f"CN {q(info.get('commonName', ''))}, DNS {', '.join(info.get('dnsNames') or []) or 'none'}"
    if problems:
        return failed(f"{issuer.kind}/{issuer.name} issued {got}: " + "; ".join(problems))
    detail = f"{issuer.kind}/{issuer.name} issued {got}, valid until {info.get('notAfter', '?')}"
    if not any("{run}" in t for t in [cfg.cert_common_name, *cfg.cert_dns_names]) and cfg.cert_dns_names:
        detail += " (no name changes with {run}, so cert-manager may have kept last run's certificate)"
    return passed(detail)


def _enforces(policy: dict) -> bool:
    """Whether a Kyverno policy blocks violations (Enforce) rather than only reporting them, by
    the policy-wide setting or any validate rule's own."""
    spec = policy.get("spec") or {}
    if str(spec.get("validationFailureAction", "")).lower() == "enforce":
        return True
    return any(
        str(((r.get("validate") or {}).get("failureAction")) or "").lower() == "enforce"
        for r in spec.get("rules") or []
    )


@check("ingress-cert-policy", GROUP_INGRESS, "Kyverno refuses a Certificate that breaks the naming rules", SUITE)
async def check_cert_policy(p: Probe) -> Outcome:
    cfg = p.cfg.suites.ingress
    if not cfg.cert_policy:
        return skipped("suites.ingress.certManager.policy.name is empty")
    try:
        pol = await p.kube.get(f"/apis/kyverno.io/v1/clusterpolicies/{cfg.cert_policy}")
    except StatusError as e:
        if e.code == 404:
            return failed(
                f"no Kyverno ClusterPolicy {cfg.cert_policy}", "Is Kyverno installed, and is the policy name right?"
            )
        return failed(str(e))
    ready = kube.find(kube.status_conditions(pol), "Ready")
    if ready.status != "True":
        return failed(
            f"ClusterPolicy {cfg.cert_policy} not Ready: {first_non_empty(ready.message, ready.reason, 'no status')}"
        )
    if not _enforces(pol):
        return failed(
            f"ClusterPolicy {cfg.cert_policy} is in Audit mode: violations are reported, not refused",
            "Set validationFailureAction (or the rule's failureAction) to Enforce.",
        )
    spec = {
        "secretName": f"{p.cfg.release}-probe-policy-test",
        "dnsNames": [_render(cfg.cert_policy_bad_dns_name, p)],
        "issuerRef": {
            "name": cfg.cert_manager_issuer.name or "none",
            "kind": cfg.cert_manager_issuer.kind,
            "group": "cert-manager.io",
        },
    }
    bad = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": scratch.meta(p, scratch.name(p, "bad-cert")),
        "spec": spec,
    }
    try:
        await p.kube.create(scratch.path(p, "/apis/cert-manager.io/v1/certificates"), bad, dry_run=True)
    except StatusError as e:
        if not _policy_refusal(e):
            return failed(f"refused, but not by a policy: {e}")
        if cfg.cert_policy not in e.message:
            return failed(f"refused by a different policy: {e.message[:200]}")
        return passed(f"{cfg.cert_policy} (Enforce) refused a Certificate with {_asked(spec)}")
    return failed(
        f"a Certificate with {_asked(spec)} was admitted",
        f"ClusterPolicy {cfg.cert_policy} doesn't match Certificates in the scratch namespace, or its rule allows these names.",
    )
