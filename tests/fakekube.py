"""An in-memory API server for the platform suites' tests.

Objects live in a dict keyed by their collection path. Pods complete as soon as they are read:
a task pod runs its task in-process (tasks.py, the code the real pods run), with each
PersistentVolumeClaim mounted as a temporary directory, and its result becomes the
termination message, as the kubelet would make it. Hooks let a test decide admission
(dry runs), Route admission and what the fetch task sees.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from platform_probe import tasks
from platform_probe.kube import StatusError


class FakeKube:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.objects: dict[str, dict[str, dict]] = {}
        # Hooks a test can replace.
        self.admit = lambda pod: None  # dry-run pod admission: raise StatusError to refuse
        self.fetch = lambda requests: {"results": []}  # what the fetch task sees
        self.pod_status = None  # a pod's status instead of running its task, e.g. stuck Pending
        self.route_admitted = True
        self.calls: list[tuple[str, str]] = []
        self.emitted: list[dict] = []
        self.argo_result: dict | None = None  # a test Application's status instead of Synced/Healthy

    # --- storage ---

    RESOURCES = frozenset(
        {
            "pods",
            "services",
            "persistentvolumeclaims",
            "routes",
            "certificates",
            "volumesnapshots",
            "resourcequotas",
            "limitranges",
            "storageclasses",
            "namespaces",
            "appliedclusterresourcequotas",
            "argocds",
            "deployments",
            "daemonsets",
            "ipaddresspools",
            "l2advertisements",
            "bgpadvertisements",
            "bgpsessionstates",
            "servicel2statuses",
            "servicebgpstatuses",
            "applications",
        }
    )

    @classmethod
    def _split(cls, path: str) -> tuple[str, str, dict]:
        """(collection path, object name or "", query)."""
        u = urlsplit(path)
        coll, _, last = u.path.rstrip("/").rpartition("/")
        if last in cls.RESOURCES:
            return u.path.rstrip("/"), "", parse_qs(u.query)
        return coll, last, parse_qs(u.query)

    def put(self, collection: str, obj: dict) -> dict:
        self.objects.setdefault(collection, {})[obj["metadata"]["name"]] = obj
        return obj

    def items(self, collection: str) -> list[dict]:
        return list(self.objects.get(collection, {}).values())

    # --- the client interface ---

    async def request(self, method: str, path: str, body=None, content_type="application/json"):
        raise NotImplementedError(method)

    async def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        coll, name, query = self._split(path)
        if not name:
            items = self.items(coll)
            if sel := query.get("labelSelector"):
                want = dict(term.partition("=")[::2] for term in unquote(sel[0]).split(","))
                items = [
                    o for o in items if all((o["metadata"].get("labels") or {}).get(k) == v for k, v in want.items())
                ]
            return {"items": copy.deepcopy(items)}
        obj = self.objects.get(coll, {}).get(name)
        if obj is None:
            raise StatusError(404, f"{name} not found")
        # In a thread: a task may call a test server running on this event loop.
        await asyncio.to_thread(self._progress, coll, obj)
        return copy.deepcopy(obj)

    async def create(self, path: str, obj: dict, dry_run: bool = False) -> dict:
        self.calls.append(("POST" + (" dry-run" if dry_run else ""), path))
        obj = copy.deepcopy(obj)
        if dry_run:
            self.admit(obj)
            return obj
        coll = path.split("?")[0]
        if obj["metadata"]["name"] in self.objects.get(coll, {}):
            raise StatusError(409, "already exists")
        self.put(coll, obj)
        if coll.endswith("/persistentvolumeclaims"):
            self._progress(coll, obj)  # a restore is populated from its snapshot right away
        return copy.deepcopy(obj)

    async def patch(self, path: str, patch: dict) -> dict:
        self.calls.append(("PATCH", path))
        coll, name, _ = self._split(path)
        obj = self.objects[coll][name]
        _merge(obj, patch)
        return copy.deepcopy(obj)

    async def delete(self, path: str) -> None:
        self.calls.append(("DELETE", path))
        coll, name, _ = self._split(path)
        self.objects.get(coll, {}).pop(name, None)

    async def close(self) -> None:
        pass

    # --- what controllers and the kubelet would do ---

    def volume_dir(self, claim: str) -> Path:
        d = self.tmp / "volumes" / claim
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _progress(self, coll: str, obj: dict) -> None:
        if coll.endswith("/pods") and not obj.get("status"):
            obj["status"] = self._run_pod(obj)
        elif coll.endswith("/routes") and not obj.get("status"):
            cond = {
                "type": "Admitted",
                "status": "True" if self.route_admitted else "False",
                "reason": "HostAlreadyClaimed",
            }
            obj["status"] = {"ingress": [{"host": f"{obj['metadata']['name']}.apps.example.com", "conditions": [cond]}]}
        elif coll.endswith("/applications") and not obj.get("status"):
            # Argo CD syncs the test Application, or reports why it can't.
            obj["status"] = copy.deepcopy(self.argo_result) or {
                "sync": {"status": "Synced"},
                "health": {"status": "Healthy"},
                "resources": [{"kind": "ConfigMap", "name": "platform-probe-argocd"}],
            }
        elif coll.endswith("/certificates") and not obj.get("status"):
            obj["status"] = {"conditions": [{"type": "Ready", "status": "True"}], "notAfter": "2027-02-07T00:00:00Z"}
        elif coll.endswith("/volumesnapshots") and not obj.get("status"):
            src = obj["spec"]["source"]["persistentVolumeClaimName"]
            shutil.copytree(
                self.volume_dir(src), self.volume_dir("snapshot-" + obj["metadata"]["name"]), dirs_exist_ok=True
            )
            obj["status"] = {"readyToUse": True, "restoreSize": "1Gi"}
        elif coll.endswith("/persistentvolumeclaims"):
            want = obj["spec"]["resources"]["requests"]["storage"]
            obj.setdefault("status", {})["capacity"] = {"storage": want}
            if ds := obj["spec"].get("dataSource"):
                snap_dir = self.volume_dir("snapshot-" + ds["name"])
                dest = self.volume_dir(obj["metadata"]["name"])
                if not any(dest.iterdir()):
                    shutil.copytree(snap_dir, dest, dirs_exist_ok=True)

    def _run_pod(self, pod: dict) -> dict:
        labels = pod["metadata"].get("labels") or {}
        if labels.get("platform-probe/backend") == "true":
            return {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
        if self.pod_status is not None:
            return copy.deepcopy(self.pod_status)
        c = pod["spec"]["containers"][0]
        _, name, raw = c["args"]
        args = json.loads(raw)
        claims = {
            v["name"]: v["persistentVolumeClaim"]["claimName"]
            for v in pod["spec"].get("volumes", [])
            if "persistentVolumeClaim" in v
        }
        for m in c.get("volumeMounts", []):
            if m["name"] in claims and "path" in args and args["path"].startswith(m["mountPath"]):
                args["path"] = str(self.volume_dir(claims[m["name"]])) + args["path"][len(m["mountPath"]) :]
        if name == "emit":
            # Long-running: it logs and serves its metric until the run deletes it.
            self.emitted.append(args)
            return {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
        if name == "fetch":
            result = self.fetch(args["requests"])
        else:
            try:
                result = tasks.TASKS[name](**args)
            except Exception as e:  # noqa: BLE001
                result = {"error": f"{type(e).__name__}: {e}"}
        pod["metadata"].setdefault("annotations", {})["openshift.io/scc"] = "restricted-v2"
        term = {"exitCode": 0, "message": json.dumps(result)}
        return {"phase": "Succeeded", "containerStatuses": [{"state": {"terminated": term}}]}


def _merge(obj: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(obj.get(k), dict):
            _merge(obj[k], v)
        else:
            obj[k] = v
