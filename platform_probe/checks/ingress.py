"""Ingress & certificates: what a team gets when it exposes an app with a Route.

The checks create a backend (the probe's own pricing service, on HTTP and on HTTPS with a
service-CA certificate) and one Route per TLS termination in the scratch namespace, then fetch
each Route from a pod in that namespace, the way a tenant's client would.
"""

from __future__ import annotations

from .. import kube, scratch
from ..kube import StatusError
from ..runner import GROUP_INGRESS, Outcome, Probe, check, failed, first_non_empty, passed, skipped

SUITE = "ingress"
SERVICE_CA = "openshift-service-serving-signer"


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


@check("ingress-cert-manager", GROUP_INGRESS, "cert-manager issues a certificate", SUITE)
async def check_cert_manager(p: Probe) -> Outcome:
    issuer = p.cfg.suites.ingress.cert_manager_issuer
    if not issuer.name:
        return skipped("suites.ingress.certManager.issuer.name is empty")
    cert_name = scratch.name(p, "cert")
    cert = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": scratch.meta(p, cert_name),
        "spec": {
            # One Secret per release, reissued every run: the dnsName changes with the run ID.
            "secretName": f"{p.cfg.release}-probe-certificate",
            "dnsNames": [f"{cert_name}.{scratch.ns(p)}.svc"],
            "issuerRef": {"name": issuer.name, "kind": issuer.kind, "group": "cert-manager.io"},
        },
    }
    try:
        await p.kube.create(scratch.path(p, "/apis/cert-manager.io/v1/certificates"), cert)
    except StatusError as e:
        if e.code == 404:
            return failed("no certificates.cert-manager.io API", "Is cert-manager installed?")
        return failed(f"creating the Certificate: {e}")
    c = await scratch.wait_for(
        lambda: p.kube.get(scratch.path(p, "/apis/cert-manager.io/v1/certificates", cert_name)),
        lambda o: kube.find(kube.status_conditions(o), "Ready").status == "True",
        120,
    )
    ready = kube.find(kube.status_conditions(c), "Ready")
    if ready.status != "True":
        return failed(
            f"{issuer.kind}/{issuer.name}: not Ready after 120s: {first_non_empty(ready.message, ready.reason, 'no status')}",
            "kubectl describe certificaterequest in the scratch namespace shows what the issuer said.",
        )
    return passed(f"{issuer.kind}/{issuer.name} issued it, valid until {(c.get('status') or {}).get('notAfter', '?')}")
