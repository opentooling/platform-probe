"""The Internal CA suite against the in-memory API server."""

import pytest
from conftest import run
from fakekube import FakeKube

from platform_probe import scratch
from platform_probe.checks import pki
from platform_probe.config import parse_config
from platform_probe.runner import GROUP_PKI, Probe, Status

NS = "platform-probe-scratch"
SVC = "probe-platform-probe-pki"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(scratch, "POLL", 0)


def make_probe(fake, **cfg) -> Probe:
    p = Probe(
        parse_config(
            {
                "release": "probe",
                "namespace": "platform-probe",
                "scratch": {"caConfigMap": "probe-ca-bundle"},
                "suites": {
                    "pki": {
                        "enabled": True,
                        "issuer": {"name": "cluster-ca", "kind": "ClusterIssuer"},
                        "service": SVC,
                        "secret": "probe-platform-probe-pki-tls",
                        "timeout": "1s",
                        **cfg,
                    }
                },
            }
        ),
        fake,
    )
    p.run_id, p.state = "run1", {}
    return p


def go(p, fn):
    return run(fn(p))


def test_runs_after_ingress_and_needs_scratch():
    p = make_probe(FakeKube(None))
    assert GROUP_PKI in {c.group for c in p.checks} and p.cfg.write_suites()
    assert [c.category for c in p.checks if c.group == GROUP_PKI] == ["pki"] * 3


def test_issuer(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    out = go(p, pki.check_issuer)
    assert out.status == Status.FAIL and "no ClusterIssuer cluster-ca" in out.detail, out
    fake.put(
        "/apis/cert-manager.io/v1/clusterissuers",
        {
            "metadata": {"name": "cluster-ca"},
            "spec": {"ca": {"secretName": "x"}},
            "status": {"conditions": [{"type": "Ready", "status": "False", "message": "secret x not found"}]},
        },
    )
    out = go(p, pki.check_issuer)
    assert out.status == Status.FAIL and "(ca) not Ready: secret x not found" in out.detail, out
    fake.objects["/apis/cert-manager.io/v1/clusterissuers"]["cluster-ca"]["status"] = {
        "conditions": [{"type": "Ready", "status": "True"}]
    }
    assert go(p, pki.check_issuer).detail == "ClusterIssuer/cluster-ca (ca) ready"


def test_certificate_names_and_reissue(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    out = go(p, pki.check_certificate)
    assert out.status == Status.PASS, out
    assert f"{SVC}.{NS}.svc, {SVC}.{NS}.svc.cluster.local: issued" in out.detail
    cert = fake.items(f"/apis/cert-manager.io/v1/namespaces/{NS}/certificates")[0]
    names = cert["spec"]["dnsNames"]
    assert names[:4] == [SVC, f"{SVC}.{NS}", f"{SVC}.{NS}.svc", f"{SVC}.{NS}.svc.cluster.local"]
    # A name unique to the run, so cert-manager issues anew instead of keeping the Secret.
    assert "run1" in names[4] and cert["spec"]["secretName"] == "probe-platform-probe-pki-tls"


def test_certificate_not_issued(tmp_path):
    fake = FakeKube(tmp_path)
    fake.cert_status = {
        "conditions": [
            {
                "type": "Ready",
                "status": "False",
                "reason": "Pending",
                "message": "Issuing certificate as Secret does not exist",
            }
        ]
    }
    p = make_probe(fake)
    out = go(p, pki.check_certificate)
    assert out.status == Status.FAIL and "Issuing certificate as Secret does not exist" in out.detail, out
    # The trust check reports the same, without asking again.
    assert go(p, pki.check_trusted).status == Status.FAIL
    assert len(fake.items(f"/apis/cert-manager.io/v1/namespaces/{NS}/certificates")) == 1


def test_trusted(tmp_path):
    fake = FakeKube(tmp_path)
    seen = []

    def fetch(requests):
        seen.extend(r["url"] for r in requests)
        cert = {"issuer": "CN=cluster-ca,O=Example", "issuerCN": "cluster-ca"}
        return {"results": [{"url": r["url"], "status": 200, "service": "pricing", "cert": cert} for r in requests]}

    fake.fetch = fetch
    p = make_probe(fake, issuerContains="cluster-ca")
    out = go(p, pki.check_trusted)
    assert out.status == Status.PASS, out
    assert "trusted through the scratch namespace's CA bundle (issuer CN=cluster-ca,O=Example)" in out.detail
    assert seen == [f"https://{SVC}.{NS}.svc:8443/", f"https://{SVC}.{NS}.svc.cluster.local:8443/"]
    server = next(
        o
        for o in fake.items(f"/api/v1/namespaces/{NS}/pods")
        if o["metadata"]["labels"].get("platform-probe/pki-server")
    )
    assert server["spec"]["volumes"] == [{"name": "tls", "secret": {"secretName": "probe-platform-probe-pki-tls"}}]


def test_untrusted_names_the_bundle(tmp_path):
    fake = FakeKube(tmp_path)
    mounted = []
    original = fake._run_pod

    def spy(pod):
        mounted.extend(v["configMap"]["name"] for v in pod["spec"]["volumes"] if "configMap" in v)
        return original(pod)

    fake._run_pod = spy
    fake.fetch = lambda reqs: {
        "results": [
            {
                "url": r["url"],
                "error": "certificate not trusted: unable to get local issuer certificate",
                "errorKind": "untrusted",
            }
            for r in reqs
        ]
    }
    p = make_probe(fake, trustConfigMap="internal-ca-bundle", trustKey="trust-bundle.pem")
    out = go(p, pki.check_trusted)
    assert out.status == Status.FAIL and "unable to get local issuer certificate" in out.detail, out
    assert "ConfigMap internal-ca-bundle" in out.hint and "trust-manager" in out.hint
    assert mounted == ["internal-ca-bundle"], "the client trusts the configured bundle, not the scratch one"


def test_wrong_issuer(tmp_path):
    fake = FakeKube(tmp_path)
    fake.fetch = lambda reqs: {
        "results": [
            {"url": r["url"], "status": 200, "service": "pricing", "cert": {"issuer": "CN=other"}} for r in reqs
        ]
    }
    out = go(make_probe(fake, issuerContains="cluster-ca"), pki.check_trusted)
    assert out.status == Status.FAIL and "doesn't contain" in out.detail, out
