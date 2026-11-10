"""The MetalLB suite against the in-memory API server."""

import pytest
from conftest import run
from fakekube import FakeKube

from platform_probe import scratch
from platform_probe.checks import metallb
from platform_probe.config import parse_config
from platform_probe.runner import Probe, Status

NS, MLB = "platform-probe-scratch", "metallb-system"
SVC = "probe-metallb"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(scratch, "POLL", 0)


def make_probe(fake, **cfg) -> Probe:
    p = Probe(
        parse_config(
            {
                "release": "probe",
                "namespace": "platform-probe",
                "suites": {"metallb": {"enabled": True, "service": SVC, "timeout": "1s", **cfg}},
            }
        ),
        fake,
    )
    p.run_id, p.state = "run1", {}
    return p


def res(name):
    return f"/apis/metallb.io/v1beta1/namespaces/{MLB}/{name}"


def go(p, fn):
    return run(fn(p))


def test_components(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    assert go(p, metallb.check_components).status == Status.FAIL  # nothing deployed
    fake.put(
        f"/apis/apps/v1/namespaces/{MLB}/deployments",
        {"metadata": {"name": "controller"}, "spec": {"replicas": 1}, "status": {"availableReplicas": 1}},
    )
    fake.put(
        f"/apis/apps/v1/namespaces/{MLB}/daemonsets",
        {"metadata": {"name": "speaker"}, "status": {"desiredNumberScheduled": 6, "numberReady": 5}},
    )
    out = go(p, metallb.check_components)
    assert out.status == Status.FAIL and out.detail == "not ready: speaker 5/6", out
    fake.put(
        f"/apis/apps/v1/namespaces/{MLB}/daemonsets",
        {"metadata": {"name": "speaker"}, "status": {"desiredNumberScheduled": 6, "numberReady": 6}},
    )
    assert go(p, metallb.check_components).detail == "controller 1/1, speaker 6/6"


def pool(fake, name, addresses, labels=None, **status):
    fake.put(
        res("ipaddresspools"),
        {"metadata": {"name": name, "labels": labels or {}}, "spec": {"addresses": addresses}, "status": status},
    )


def test_pools(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    out = go(p, metallb.check_pools)
    assert out.status == Status.FAIL and "no IPAddressPool" in out.detail, out

    pool(fake, "public", ["192.168.10.0/28"], availableIPv4=10, assignedIPv4=4)
    pool(fake, "internal", ["10.0.0.10-10.0.0.20"], labels={"zone": "internal"})
    pool(fake, "orphan", ["10.9.9.0/30"])
    fake.put(res("l2advertisements"), {"metadata": {"name": "public"}, "spec": {"ipAddressPools": ["public"]}})
    fake.put(
        res("bgpadvertisements"),
        {"metadata": {"name": "internal"}, "spec": {"ipAddressPoolSelectors": [{"matchLabels": {"zone": "internal"}}]}},
    )
    out = go(p, metallb.check_pools)
    assert out.status == Status.FAIL and "orphan isn't advertised" in out.detail, out

    fake.objects[res("ipaddresspools")].pop("orphan")
    sessions = f"/apis/frrk8s.metallb.io/v1beta1/namespaces/{MLB}/bgpsessionstates"
    fake.put(sessions, {"metadata": {"name": "node1-peer1"}, "status": {"bgpStatus": "Established"}})
    out = go(p, metallb.check_pools)
    assert out.status == Status.PASS, out
    assert out.detail == "L2/BGP: public 4 used, 10 free, internal, 1 BGP sessions established"

    fake.put(sessions, {"metadata": {"name": "node2-peer1"}, "status": {"bgpStatus": "Active"}})
    pool(fake, "public", ["192.168.10.0/28"], availableIPv4=0, assignedIPv4=14)
    out = go(p, metallb.check_pools)
    assert out.status == Status.FAIL, out
    assert "public has 0 free addresses (14 assigned)" in out.detail and "node2-peer1 (Active)" in out.detail


def lb_service(fake, ip=None):
    status = {"loadBalancer": {"ingress": [{"ip": ip}]}} if ip else {}
    fake.put(f"/api/v1/namespaces/{NS}/services", {"metadata": {"name": SVC}, "status": status})


def test_service(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake, pool="public")
    pool(fake, "public", ["192.168.10.0/28"])
    lb_service(fake)
    out = go(p, metallb.check_service)
    assert out.status == Status.FAIL and "no address" in out.detail and "describe service" in out.hint, out

    lb_service(fake, "192.168.20.5")
    out = go(p, metallb.check_service)
    assert out.status == Status.FAIL and "isn't in pool public" in out.detail, out

    lb_service(fake, "192.168.10.5")
    fake.put(
        res("servicel2statuses"),
        {
            "metadata": {
                "name": "l2-x",
                "labels": {"metallb.io/service-name": SVC, "metallb.io/service-namespace": NS},
            },
            "status": {"node": "worker-2"},
        },
    )
    seen = []

    def fetch(requests):
        seen.extend(r["url"] for r in requests)
        return {"results": [{"status": 200, "service": "pricing"}]}

    fake.fetch = fetch
    out = go(p, metallb.check_service)
    assert out.status == Status.PASS, out
    assert out.detail == "192.168.10.5 from public, announced by node worker-2; reached the backend from a pod"
    assert seen == ["http://192.168.10.5:8080/"]
    # The backend pod each run starts, without the ingress suite's TLS.
    backend = next(
        o for o in fake.items(f"/api/v1/namespaces/{NS}/pods") if o["metadata"]["labels"].get("platform-probe/backend")
    )
    assert not backend["spec"]["volumes"]


def test_service_unreachable(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    lb_service(fake, "192.168.10.5")
    fake.fetch = lambda reqs: {"results": [{"error": "TimeoutError: timed out", "errorKind": "timeout"}]}
    out = go(p, metallb.check_service)
    assert out.status == Status.FAIL and "timed out" in out.detail and "announcement" in out.hint, out


@pytest.mark.parametrize(
    ("ip", "addresses", "inside"),
    [
        ("10.0.0.15", ["10.0.0.10-10.0.0.20"], True),
        ("10.0.0.21", ["10.0.0.10-10.0.0.20"], False),
        ("fc00::5", ["fc00::/64"], True),
    ],
)
def test_in_pool(ip, addresses, inside):
    assert metallb._in_pool(ip, {"spec": {"addresses": addresses}}) is inside
