"""The Argo CD suite against the in-memory API server."""

import pytest
from conftest import run
from fakekube import FakeKube

from platform_probe import scratch
from platform_probe.checks import argocd
from platform_probe.config import ConfigError, parse_config
from platform_probe.runner import GROUP_ARGOCD, Probe, Status

PLATFORM, APPS_NS = "openshift-gitops", "gitops-apps"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(scratch, "POLL", 0)


def make_probe(fake, **apps_instance) -> Probe:
    cfg = parse_config(
        {
            "release": "probe",
            "namespace": "platform-probe",
            "suites": {
                "argocd": {
                    "enabled": True,
                    "instances": [
                        {"name": "platform", "namespace": PLATFORM, "argocd": "openshift-gitops"},
                        {
                            "name": "applications",
                            "namespace": APPS_NS,
                            "deploy": {
                                "enabled": True,
                                "repoUrl": "https://git.example.com/probe.git",
                                "path": "argocd/probe-app",
                            },
                            **apps_instance,
                        },
                    ],
                }
            },
        }
    )
    p = Probe(cfg, fake)
    p.run_id, p.state = "run1", {}
    return p


def argocd_cr(fake, ns, name, phase="Available", **components):
    status = {
        "phase": phase,
        "server": "Running",
        "repo": "Running",
        "applicationController": "Running",
        "redis": "Running",
        **components,
    }
    fake.put(argocd.ARGOCDS.format(ns=ns), {"metadata": {"name": name}, "status": status})


def app(fake, ns, name, sync="Synced", health="Healthy", **status):
    fake.put(
        argocd.APPS.format(ns=ns),
        {"metadata": {"name": name}, "status": {"sync": {"status": sync}, "health": {"status": health}, **status}},
    )


def go(p, fn):
    return run(fn(p))


def test_suite_adds_its_group():
    p = make_probe(FakeKube(None))
    assert GROUP_ARGOCD in {c.group for c in p.checks} and p.cfg.write_suites()


def test_instances(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    argocd_cr(fake, PLATFORM, "openshift-gitops")
    argocd_cr(fake, APPS_NS, "apps", phase="Pending", repo="Failed")
    out = go(p, argocd.check_instances)
    assert out.status == Status.FAIL and out.detail == "applications: phase Pending, repo Failed", out
    argocd_cr(fake, APPS_NS, "apps")
    out = go(p, argocd.check_instances)
    assert out.status == Status.PASS and out.detail == "platform: available; applications: available", out


def test_applications(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake, ignore=["sandbox"])
    app(fake, PLATFORM, "cluster-config")
    app(fake, PLATFORM, "monitoring", health="Progressing")
    app(fake, APPS_NS, "team-a")
    app(fake, APPS_NS, "sandbox", health="Degraded")  # ignored
    # The probe's own test Applications are left to the deploy check.
    fake.put(
        argocd.APPS.format(ns=APPS_NS),
        {"metadata": {"name": "probe-argocd-x", "labels": {scratch.LABEL_RUN: "x"}}, "status": {}},
    )
    out = go(p, argocd.check_applications)
    assert out.status == Status.PASS, out
    assert (
        out.detail
        == "platform: 2 Applications synced and healthy, 1 progressing; applications: 1 Applications synced and healthy"
    )

    app(fake, PLATFORM, "ingress", sync="OutOfSync")
    app(fake, APPS_NS, "team-b", operationState={"phase": "Failed", "message": "one or more objects failed to apply"})
    app(fake, APPS_NS, "team-c", conditions=[{"type": "ComparisonError", "message": "repository not accessible"}])
    out = go(p, argocd.check_applications)
    assert out.status == Status.FAIL, out
    assert "platform: 1 of 3 broken: ingress (OutOfSync)" in out.detail
    assert "team-b (sync Failed: one or more objects failed to apply)" in out.detail
    assert "team-c (ComparisonError: repository not accessible)" in out.detail


def test_out_of_sync_can_be_allowed(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    p.cfg.suites.argocd.instances[0].fail_on_out_of_sync = False
    app(fake, PLATFORM, "drifting", sync="OutOfSync")
    assert go(p, argocd.check_applications).status == Status.PASS


def test_deploy(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    out = go(p, argocd.check_deploy)
    assert out.status == Status.PASS, out
    assert "applications: synced ConfigMap/platform-probe-argocd into platform-probe-scratch" in out.detail
    created = next(o for m, path in fake.calls if m == "POST" for o in [path] if "applications" in path)
    assert created == argocd.APPS.format(ns=APPS_NS)
    assert not fake.items(argocd.APPS.format(ns=APPS_NS)), "the test Application is deleted"


def test_deploy_failure_explains(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    fake.argo_result = {
        "sync": {"status": "OutOfSync"},
        "health": {"status": "Missing"},
        "conditions": [{"type": "SyncError", "message": "namespace platform-probe-scratch is not managed"}],
    }
    out = go(p, argocd.check_deploy)
    assert out.status == Status.FAIL and "SyncError: namespace platform-probe-scratch is not managed" in out.detail, out
    assert "argocd.argoproj.io/managed-by" in out.hint
    assert not fake.items(argocd.APPS.format(ns=APPS_NS))


def test_deploy_skipped_without_it(tmp_path):
    p = make_probe(FakeKube(tmp_path), deploy={"enabled": False})
    assert go(p, argocd.check_deploy).status == Status.SKIP


def test_sweep_covers_argocd_namespaces(tmp_path):
    fake = FakeKube(tmp_path)
    p = make_probe(fake)
    fake.put(
        argocd.APPS.format(ns=APPS_NS),
        {"metadata": {"name": "probe-argocd-old", "labels": {scratch.LABEL_RELEASE: "probe"}}},
    )
    app(fake, APPS_NS, "team-a")
    run(scratch.sweep(p))
    assert [a["metadata"]["name"] for a in fake.items(argocd.APPS.format(ns=APPS_NS))] == ["team-a"]


def test_instances_config_rejects():
    with pytest.raises(ConfigError):
        parse_config({"suites": {"argocd": {"instances": {"namespace": "x"}}}})
    with pytest.raises(ConfigError):
        parse_config({"suites": {"argocd": {"instances": [{"namespace": "x", "typo": 1}]}}})
