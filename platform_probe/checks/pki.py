"""Internal CA: a cluster-local CA, through cert-manager, issues certificates for
<service>.<namespace>.svc[.cluster.local] names, and workloads trust them.

The checks request a Certificate for the Service the chart creates in the scratch namespace,
start a pod that serves it, and connect to it by its cluster names from another pod that
trusts only what workloads are given: the CA bundle in the scratch namespace (by default the
cluster's, or a trust-manager Bundle's target with trustBundle).
"""

from __future__ import annotations

import time

from .. import kube, scratch
from ..kube import StatusError
from ..runner import GROUP_PKI, Outcome, Probe, ProbeError, check, failed, first_non_empty, passed, q

SUITE = "pki"
CERTS = "/apis/cert-manager.io/v1/certificates"


def _names(p: Probe) -> list[str]:
    """The names a Service is reached by: svc, svc.ns, svc.ns.svc, svc.ns.svc.<domain>."""
    svc, ns, domain = p.cfg.suites.pki.service, scratch.ns(p), p.cfg.suites.pki.cluster_domain
    return [svc, f"{svc}.{ns}", f"{svc}.{ns}.svc", f"{svc}.{ns}.svc.{domain}"]


@check("pki-issuer", GROUP_PKI, "The internal CA's issuer is ready", SUITE)
async def check_issuer(p: Probe) -> Outcome:
    issuer = p.cfg.suites.pki.issuer
    if issuer.kind == "ClusterIssuer":
        path = f"/apis/cert-manager.io/v1/clusterissuers/{issuer.name}"
    else:
        path = scratch.path(p, "/apis/cert-manager.io/v1/issuers", issuer.name)
    try:
        obj = await p.kube.get(path)
    except StatusError as e:
        if e.code == 404:
            return failed(
                f"no {issuer.kind} {issuer.name}", "Is cert-manager installed, and is suites.pki.issuer right?"
            )
        return failed(str(e))
    ready = kube.find(kube.status_conditions(obj), "Ready")
    kind = next(
        (k for k in ("ca", "vault", "acme", "selfSigned", "venafi") if k in (obj.get("spec") or {})), "external"
    )
    if ready.status != "True":
        return failed(
            f"{issuer.kind}/{issuer.name} ({kind}) not Ready: {first_non_empty(ready.message, ready.reason, 'no status')}"
        )
    return passed(f"{issuer.kind}/{issuer.name} ({kind}) ready")


async def _issue(p: Probe) -> tuple[str, float, str]:
    """Request this run's Certificate and wait until it is issued, once per run. Returns its
    name, how long it took and its notAfter; raises ProbeError with the issuer's answer."""
    if "pki" in p.state:
        if isinstance(p.state["pki"], Exception):
            raise p.state["pki"]
        return p.state["pki"]
    o = p.cfg.suites.pki
    cert_name = scratch.name(p, "pki")
    names = _names(p)
    cert = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": scratch.meta(p, cert_name),
        "spec": {
            "secretName": o.secret,
            # A name only this run uses, so the Certificate differs from what the Secret holds
            # and cert-manager issues a new one every run.
            "dnsNames": [*names, f"{scratch.name(p, 'run')}.{names[-1]}"],
            "usages": ["server auth", "digital signature", "key encipherment"],
            "issuerRef": {"name": o.issuer.name, "kind": o.issuer.kind, "group": "cert-manager.io"},
        },
    }
    # A common name only if one fits: X.509 allows 64 characters, and a Service's full name is
    # often longer. The DNS names carry every name anyway.
    common = next((n for n in reversed(names) if len(n) <= 64), "")
    if common:
        cert["spec"]["commonName"] = common
    start = time.monotonic()
    try:
        await p.kube.create(scratch.path(p, CERTS), cert)
        c = await scratch.wait_for(
            lambda: p.kube.get(scratch.path(p, CERTS, cert_name)),
            lambda o_: kube.find(kube.status_conditions(o_), "Ready").status == "True",
            o.timeout,
        )
        ready = kube.find(kube.status_conditions(c), "Ready")
        if ready.status != "True":
            raise ProbeError(
                f"not issued after {o.timeout:.0f}s: {first_non_empty(ready.message, ready.reason, 'no status')}"
            )
    except StatusError as e:
        err = ProbeError("no certificates.cert-manager.io API" if e.code == 404 else f"creating the Certificate: {e}")
        p.state["pki"] = err
        raise err from None
    except ProbeError as e:
        p.state["pki"] = e
        raise
    p.state["pki"] = (cert_name, time.monotonic() - start, (c.get("status") or {}).get("notAfter", "?"))
    return p.state["pki"]


@check("pki-certificate", GROUP_PKI, "It issues a certificate for a Service's cluster names", SUITE)
async def check_certificate(p: Probe) -> Outcome:
    try:
        _, took, not_after = await _issue(p)
    except ProbeError as e:
        return failed(
            str(e), "kubectl describe certificaterequest in the scratch namespace shows what the issuer answered."
        )
    return passed(f"{', '.join(_names(p)[2:])}: issued in {took:.0f}s, valid until {not_after}")


async def _serve(p: Probe) -> None:
    """Start the pod that serves the issued certificate on 8443, behind the chart's Service."""
    o = p.cfg.suites.pki
    pod_name = scratch.name(p, "pki-server")
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": scratch.meta(p, pod_name),
        "spec": scratch.pod_spec(
            p,
            {
                "name": "server",
                "env": [
                    {"name": "ROLE", "value": "pricing"},
                    {"name": "VERSION", "value": "pki"},
                    {"name": "TLS_CERT_FILE", "value": "/tls/tls.crt"},
                    {"name": "TLS_KEY_FILE", "value": "/tls/tls.key"},
                ],
                "ports": [{"name": "https", "containerPort": 8443}],
                "readinessProbe": {"tcpSocket": {"port": 8443}, "periodSeconds": 2},
                "volumeMounts": [{"name": "tls", "mountPath": "/tls", "readOnly": True}],
            },
            [{"name": "tls", "secret": {"secretName": o.secret}}],
        ),
    }
    pod["metadata"]["labels"]["platform-probe/pki-server"] = "true"
    pod["spec"]["restartPolicy"] = "Always"
    await p.kube.create(scratch.path(p, "/api/v1/pods"), pod)
    final = await scratch.wait_for(
        lambda: p.kube.get(scratch.path(p, "/api/v1/pods", pod_name)),
        lambda x: kube.find(kube.conditions((x.get("status") or {}).get("conditions")), "Ready").status == "True",
        o.timeout,
    )
    if kube.find(kube.conditions((final.get("status") or {}).get("conditions")), "Ready").status != "True":
        raise ProbeError("the TLS server didn't become ready: " + (scratch.waiting_reason(final) or "not ready"))


@check("pki-trusted", GROUP_PKI, "Workloads trust it: a pod connects by the Service's cluster names", SUITE)
async def check_trusted(p: Probe) -> Outcome:
    o = p.cfg.suites.pki
    try:
        await _issue(p)
        await _serve(p)
    except ProbeError as e:
        return failed(str(e))
    urls = [f"https://{n}:8443/" for n in _names(p)[2:]]
    trust = (o.trust_config_map, o.trust_key) if o.trust_config_map else None
    run = await scratch.run_task(p, "pki-client", "fetch", {"requests": [{"url": u} for u in urls]}, ca=trust)
    if not run.ok:
        return failed(f"client pod: {run.explain()}")
    bad, issuer = [], ""
    for res in run.data.get("results") or []:
        host = res.get("url", "").split("/")[2].split(":")[0]
        if res.get("errorKind") == "untrusted":
            bad.append(f"{host}: {res['error']}")
        elif res.get("error") or res.get("status") != 200:
            bad.append(f"{host}: {res.get('error') or res.get('status')}")
        else:
            issuer = (res.get("cert") or {}).get("issuer", issuer)
    where = f"ConfigMap {o.trust_config_map}" if o.trust_config_map else "the scratch namespace's CA bundle"
    if bad:
        return failed(
            "; ".join(bad),
            f"A workload trusting {where} doesn't trust the internal CA. Distribute it to namespaces "
            "(e.g. a trust-manager Bundle) and point suites.pki.trustBundle at it, or add it to the "
            "cluster trust bundle.",
        )
    if o.issuer_contains and o.issuer_contains not in issuer:
        return failed(f"issuer {q(issuer)} doesn't contain {q(o.issuer_contains)}")
    return passed(f"{', '.join(_names(p)[2:])}: trusted through {where} (issuer {issuer})")
