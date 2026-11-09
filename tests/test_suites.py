"""The platform suites against the in-memory API server in fakekube.py."""

import json

import pytest
from conftest import run
from fakekube import FakeKube

from platform_probe import scratch, tasks
from platform_probe.checks import ingress, storage, workloads
from platform_probe.config import ConfigError, parse_config
from platform_probe.kube import StatusError
from platform_probe.runner import GROUP_INGRESS, GROUP_STORAGE, GROUP_WORKLOADS, Probe, Status

NS = "platform-probe-scratch"
PODS = f"/api/v1/namespaces/{NS}/pods"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(scratch, "POLL", 0)


def make_probe(fake: FakeKube, **suites) -> Probe:
    cfg = parse_config(
        {
            "release": "probe",
            "namespace": "platform-probe",
            "image": "ghcr.io/opentooling/platform-probe:test",
            "scratch": {"caConfigMap": "probe-ca-bundle"},
            "suites": suites,
        }
    )
    p = Probe(cfg, fake)
    p.run_id = "run1"
    p.state = {}
    return p


def go(p: Probe, fn):
    return run(fn(p))


# --- config and suite selection ------------------------------------------------------------------


def test_suites_off_by_default():
    p = make_probe(FakeKube(None))
    assert {c.group for c in p.checks} == {"Platform", "API gateway", "Service mesh"}
    assert p.cfg.scratch.namespace == NS and not p.cfg.write_suites()


def test_enabled_suites_add_their_checks_in_order():
    p = make_probe(FakeKube(None), ingress={"enabled": True}, storage={"enabled": True})
    groups = list(dict.fromkeys(c.group for c in p.checks))
    assert groups == ["Platform", "API gateway", "Service mesh", GROUP_INGRESS, GROUP_STORAGE]


@pytest.mark.parametrize(
    "suites",
    [{"storage": {"storageClasses": "gp3"}}, {"storage": {"storageClasses": [1]}}, {"ingress": {"domain": "x"}}],
)
def test_suite_config_rejects(suites):
    with pytest.raises(ConfigError):
        parse_config({"suites": suites})


# --- scratch helpers -----------------------------------------------------------------------------


def test_run_task_reports_why_a_pod_never_ran(tmp_path):
    fake = FakeKube(tmp_path)
    fake.pod_status = {
        "phase": "Pending",
        "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff", "message": "pull access denied"}}}],
    }
    p = make_probe(fake)
    res = run(scratch.run_task(p, "x", "whoami", timeout=0))
    assert not res.ok and "ImagePullBackOff: pull access denied" in res.explain()
    assert not fake.items(PODS), "the pod is deleted"


def test_task_pods_are_restricted_and_get_the_ca(tmp_path):
    fake = FakeKube(tmp_path)
    seen = {}
    original = fake._run_pod

    def spy(pod):
        seen.update(pod)
        return original(pod)

    fake._run_pod = spy
    p = make_probe(fake)
    assert run(scratch.run_task(p, "x", "whoami")).ok
    c = seen["spec"]["containers"][0]
    assert c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["capabilities"] == {
        "drop": ["ALL"]
    }
    assert seen["spec"]["securityContext"]["runAsNonRoot"] is True
    assert seen["metadata"]["labels"]["sidecar.istio.io/inject"] == "false"
    assert {"name": "ca", "mountPath": scratch.CA_DIR, "readOnly": True} in c["volumeMounts"]


def test_sweep_deletes_only_this_release(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    fake.put(PODS, {"metadata": {"name": "mine", "labels": {scratch.LABEL_RELEASE: "probe"}}})
    fake.put(PODS, {"metadata": {"name": "theirs", "labels": {scratch.LABEL_RELEASE: "other"}}})
    run(scratch.sweep(p))
    assert [o["metadata"]["name"] for o in fake.items(PODS)] == ["theirs"]


@pytest.mark.parametrize(("q", "n"), [("1Gi", 1 << 30), ("2147483648", 1 << 31), ("500M", 5e8), ("100m", 0.1)])
def test_parse_quantity(q, n):
    assert scratch.parse_quantity(q) == n


# --- workload security ---------------------------------------------------------------------------


def refuse_unsafe(pod):
    """Admission as restricted-v2 does it."""
    spec, c = pod["spec"], pod["spec"]["containers"][0]
    sc = c.get("securityContext") or {}
    if (
        sc.get("privileged")
        or sc.get("runAsUser") == 0
        or spec.get("hostNetwork")
        or any("hostPath" in v for v in spec.get("volumes", []))
    ):
        raise StatusError(403, 'pods "x" is forbidden: unable to validate against any security context constraint')


def test_workloads_uid_and_scc(tmp_path):
    fake = FakeKube(tmp_path)
    fake.put(
        "/api/v1/namespaces",
        {"metadata": {"name": NS, "annotations": {"openshift.io/sa.scc.uid-range": "0/1000000000000"}}},
    )
    p = make_probe(fake, workloads={"enabled": True})
    out = go(p, workloads.check_uid)
    # The test process isn't root in CI, but may be here: either way the range check ran.
    assert out.status in (Status.PASS, Status.FAIL) and "UID" in out.detail, out


def test_workloads_privileged_refused(tmp_path):
    fake = FakeKube(tmp_path)
    fake.admit = refuse_unsafe
    p = make_probe(fake, workloads={"enabled": True})
    out = go(p, workloads.check_privileged)
    assert out.status == Status.PASS and out.detail.count("(SCC)") == 4, out
    assert not fake.items(PODS), "dry runs create nothing"


def test_workloads_privileged_admitted_fails(tmp_path):
    fake = FakeKube(tmp_path)
    fake.admit = lambda pod: None
    out = go(make_probe(fake, workloads={"enabled": True}), workloads.check_privileged)
    assert out.status == Status.FAIL and "privileged, root, hostPath, hostNetwork" in out.detail, out


def test_quota_and_limit_range(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake, workloads={"enabled": True})
    assert go(p, workloads.check_quota).status == Status.SKIP
    assert go(p, workloads.check_limit_range).status == Status.SKIP
    p.cfg.suites.workloads.expect_quota = True
    assert go(p, workloads.check_quota).status == Status.FAIL

    fake.put(
        f"/api/v1/namespaces/{NS}/resourcequotas",
        {"metadata": {"name": "compute"}, "spec": {"hard": {"requests.cpu": "4"}}},
    )
    fake.put(f"/api/v1/namespaces/{NS}/limitranges", {"metadata": {"name": "defaults"}})

    def admission(pod):
        res = pod["spec"]["containers"][0].get("resources") or {}
        if (res.get("requests") or {}).get("cpu") == "100000":
            raise StatusError(403, "exceeded quota: compute, requested: requests.cpu=100k")
        if not res:
            pod["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "100m"}, "limits": {"memory": "512Mi"}}

    fake.admit = admission
    out = go(p, workloads.check_quota)
    assert out.status == Status.PASS and "exceeded quota" in out.detail, out
    out = go(p, workloads.check_limit_range)
    assert out.status == Status.PASS and "requests cpu=100m" in out.detail and "limits memory=512Mi" in out.detail, out


# --- storage -------------------------------------------------------------------------------------


def storage_classes(fake, *classes):
    for name, default, expand in classes:
        ann = {storage.DEFAULT: "true"} if default else {}
        fake.put(
            "/apis/storage.k8s.io/v1/storageclasses",
            {"metadata": {"name": name, "annotations": ann}, "allowVolumeExpansion": expand},
        )


def test_default_class(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake, storage={"enabled": True})
    storage_classes(fake, ("a", False, True))
    assert go(p, storage.check_default_class).status == Status.FAIL
    storage_classes(fake, ("b", True, True))
    assert go(p, storage.check_default_class).detail == "b (of 2)"
    storage_classes(fake, ("c", True, True))
    assert go(p, storage.check_default_class).status == Status.FAIL


def test_storage_provision_expand_snapshot(tmp_path):
    fake = FakeKube(tmp_path)
    storage_classes(fake, ("gp3-csi", True, True), ("nfs", False, False))
    p = make_probe(fake, storage={"enabled": True, "storageClasses": ["gp3-csi", "nfs"], "snapshotClass": "csi-snap"})

    out = go(p, storage.check_provision)
    assert out.status == Status.PASS and "gp3-csi in" in out.detail and "nfs in" in out.detail, out
    # Two pods per class wrote and read the same volume.
    assert (tmp_path / "volumes" / "probe-pvc-gp3-csi-run1" / "probe").read_text() == "run1-gp3-csi"

    out = go(p, storage.check_expand)
    assert out.status == Status.PASS and out.detail == "expanded: gp3-csi to 2147483648", out

    out = go(p, storage.check_snapshot)
    assert out.status == Status.PASS and "restored PVC has the data" in out.detail, out


def test_storage_reader_sees_other_data(tmp_path):
    fake = FakeKube(tmp_path)
    storage_classes(fake, ("broken", True, False))
    p = make_probe(fake, storage={"enabled": True})
    original = fake._run_pod

    def lose_data(pod):
        status = original(pod)
        if (
            json.loads(pod["spec"]["containers"][0]["args"][2]).get("path")
            and pod["spec"]["containers"][0]["args"][1] == "read"
        ):
            status["containerStatuses"][0]["state"]["terminated"]["message"] = json.dumps({"data": ""})
        return status

    fake._run_pod = lose_data
    out = go(p, storage.check_provision)
    assert out.status == Status.FAIL and "read back ''" in out.detail, out
    assert go(p, storage.check_expand).status == Status.SKIP


def test_storage_skips(tmp_path):
    p = make_probe(FakeKube(tmp_path), storage={"enabled": True})
    assert go(p, storage.check_snapshot).status == Status.SKIP
    p.cfg.suites.storage.snapshot_class = "x"
    assert go(p, storage.check_snapshot).status == Status.SKIP


# --- ingress -------------------------------------------------------------------------------------


def ok_fetch(issuer="CN=ingress-operator@1700000000", days=300):
    def fetch(requests):
        out = []
        for r in requests:
            if r["url"].startswith("http://"):
                out.append({"url": r["url"], "status": 302, "location": r["url"].replace("http", "https", 1)})
            else:
                cert = {
                    "issuer": issuer,
                    "issuerCN": issuer.removeprefix("CN="),
                    "days": days,
                    "notAfter": "2027-09-01T00:00:00Z",
                }
                out.append({"url": r["url"], "status": 200, "service": "pricing", "cert": cert})
        return {"results": out}

    return fetch


def test_routes(tmp_path):
    fake = FakeKube(tmp_path)
    fake.fetch = ok_fetch()
    p = make_probe(fake, ingress={"enabled": True})
    out = go(p, ingress.check_edge)
    assert out.status == Status.PASS and "HTTP → 302 redirect" in out.detail, out
    assert go(p, ingress.check_reencrypt).status == Status.PASS
    # The backend is created once and shared.
    assert len(fake.items(f"/api/v1/namespaces/{NS}/services")) == 1
    out = go(p, ingress.check_passthrough)
    assert out.status == Status.FAIL and "router terminated TLS" in out.hint, out
    fake.fetch = ok_fetch(issuer="CN=openshift-service-serving-signer@1700000000")
    p.run_id, p.state = "run2", {}
    assert go(p, ingress.check_passthrough).status == Status.PASS


def test_edge_route_problems(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake, ingress={"enabled": True})
    fake.fetch = ok_fetch(days=3)
    out = go(p, ingress.check_edge)
    assert out.status == Status.FAIL and "expires in 3 days" in out.detail, out

    fake.fetch = lambda reqs: {
        "results": [{"error": "certificate not trusted: self-signed", "errorKind": "untrusted"}, {}]
    }
    p.run_id = "run2"
    out = go(p, ingress.check_edge)
    assert out.status == Status.FAIL and "trust bundle" in out.hint, out

    fake.route_admitted = False
    p.run_id = "run3"
    out = go(p, ingress.check_edge)
    assert out.status == Status.FAIL and "not admitted" in out.detail, out


def test_cert_manager(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake, ingress={"enabled": True})
    assert go(p, ingress.check_cert_manager).status == Status.SKIP
    p.cfg.suites.ingress.cert_manager_issuer.name = "internal-ca"
    out = go(p, ingress.check_cert_manager)
    assert out.status == Status.PASS and "ClusterIssuer/internal-ca issued it" in out.detail, out


def test_whole_run_cleans_up(tmp_path):
    """A run with every suite on leaves nothing behind in the scratch namespace."""
    fake = FakeKube(tmp_path)
    fake.fetch = ok_fetch()
    fake.admit = refuse_unsafe
    storage_classes(fake, ("gp3-csi", True, True))
    p = make_probe(fake, ingress={"enabled": True}, workloads={"enabled": True}, storage={"enabled": True})
    p.checks = [c for c in p.checks if c.group in (GROUP_INGRESS, GROUP_WORKLOADS, GROUP_STORAGE)]
    assert run(p.run())
    left = {coll: names for coll, names in fake.objects.items() if NS in coll and names}
    assert not left, left
    by_id = {r.id: r.status for r in p.report().results}
    assert by_id["storage-provision"] == Status.PASS and by_id["ingress-route-edge"] == Status.PASS, p.report().results


# --- tasks, as the pods run them -----------------------------------------------------------------


def test_task_main_writes_the_termination_message(tmp_path, monkeypatch, capsys):
    log = tmp_path / "termination-log"
    monkeypatch.setenv("TERMINATION_LOG", str(log))
    assert tasks.main(["write", json.dumps({"path": str(tmp_path / "f"), "data": "x"})]) == 0
    assert json.loads(log.read_text()) == {"written": 1}
    assert tasks.main(["read", json.dumps({"path": str(tmp_path / "missing")})]) == 0
    assert "FileNotFoundError" in json.loads(log.read_text())["error"]
    assert tasks.main(["nope"]) == 2


def test_fetch_task_reports_certificate(tmp_path, server_ssl, server_cert, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def go_():
        async def pricing(_request):
            return web.json_response({"service": "pricing"})

        app = web.Application()
        app.router.add_get("/", pricing)
        srv = TestServer(app, host="127.0.0.1")
        await srv.start_server(ssl=server_ssl)
        import asyncio

        try:
            url = f"https://127.0.0.1:{srv.port}/"
            untrusted = await asyncio.to_thread(tasks.fetch, [{"url": url}])
            monkeypatch.setattr(tasks, "CA_FILE", str(server_cert[0]))
            trusted = await asyncio.to_thread(tasks.fetch, [{"url": url}])
            unverified = await asyncio.to_thread(tasks.fetch, [{"url": url, "verify": False}])
        finally:
            await srv.close()
        return untrusted["results"][0], trusted["results"][0], unverified["results"][0]

    untrusted, trusted, unverified = run(go_())
    assert untrusted["errorKind"] == "untrusted", untrusted
    assert (
        trusted["status"] == 200 and trusted["service"] == "pricing" and trusted["cert"]["issuerCN"] == "probe-test-ca"
    ), trusted
    assert 80 < trusted["cert"]["days"] <= 90
    assert unverified["status"] == 200
