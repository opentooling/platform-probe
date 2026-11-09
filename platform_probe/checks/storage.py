"""Storage: can a tenant get a volume, keep data on it, grow it and snapshot it?

For each StorageClass under test, a PVC is written by one pod and read back by another, so
the data has to survive the volume moving between pods (and often nodes). The expansion and
snapshot checks reuse that PVC.
"""

from __future__ import annotations

import time

from .. import kube, scratch
from ..kube import StatusError
from ..runner import GROUP_STORAGE, Outcome, Probe, check, failed, first_non_empty, passed, skipped

SUITE = "storage"
DEFAULT = "storageclass.kubernetes.io/is-default-class"
PVCS = "/api/v1/persistentvolumeclaims"
SNAPSHOTS = "/apis/snapshot.storage.k8s.io/v1/volumesnapshots"


def _volume(pvc: str) -> tuple[list[dict], list[dict]]:
    return [{"name": "data", "persistentVolumeClaim": {"claimName": pvc}}], [{"name": "data", "mountPath": "/data"}]


async def _classes(p: Probe) -> list[dict]:
    return kube.items(await p.kube.get("/apis/storage.k8s.io/v1/storageclasses"))


@check("storage-default-class", GROUP_STORAGE, "Exactly one default StorageClass", SUITE)
async def check_default_class(p: Probe) -> Outcome:
    try:
        classes = await _classes(p)
    except StatusError as e:
        return failed(str(e))
    defaults = [
        kube.name(c) for c in classes if ((c.get("metadata") or {}).get("annotations") or {}).get(DEFAULT) == "true"
    ]
    if not defaults:
        return failed(
            f"none of {len(classes)} StorageClasses is the default", "A PVC without storageClassName stays Pending."
        )
    if len(defaults) > 1:
        return failed(
            f"{len(defaults)} defaults: {', '.join(defaults)}", "Which one a PVC gets is undefined; keep one."
        )
    return passed(f"{defaults[0]} (of {len(classes)})")


async def _pvc(p: Probe, pvc_name: str, sc: str, size: str, data_source: dict | None = None) -> None:
    spec: dict = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": size}},
        "storageClassName": sc,
    }
    if data_source:
        spec["dataSource"] = data_source
    await p.kube.create(
        scratch.path(p, PVCS),
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": scratch.meta(p, pvc_name), "spec": spec},
    )


async def _pvc_state(p: Probe, pvc_name: str) -> str:
    try:
        pvc = await p.kube.get(scratch.path(p, PVCS, pvc_name))
    except StatusError as e:
        return str(e)
    return f"PVC {(pvc.get('status') or {}).get('phase', '?')}"


async def _write_read(p: Probe, sc: str, pvc_name: str, token: str) -> str:
    """Write the token from one pod and read it from another. Returns a problem, or ""."""
    timeout = p.cfg.suites.storage.pod_timeout
    vols, mounts = _volume(pvc_name)
    w = await scratch.run_task(
        p, f"write-{sc}", "write", {"path": "/data/probe", "data": token}, volumes=vols, mounts=mounts, timeout=timeout
    )
    if not w.ok:
        return f"writer: {w.explain()} ({await _pvc_state(p, pvc_name)})"
    r = await scratch.run_task(
        p, f"read-{sc}", "read", {"path": "/data/probe"}, volumes=vols, mounts=mounts, timeout=timeout
    )
    if not r.ok:
        return f"reader: {r.explain()}"
    if r.data.get("data") != token:
        return f"read back {r.data.get('data')!r}, wrote {token!r}"
    return ""


@check("storage-provision", GROUP_STORAGE, "A PVC binds, and data written by one pod is read by another", SUITE)
async def check_provision(p: Probe) -> Outcome:
    cfg = p.cfg.suites.storage
    names = list(cfg.storage_classes)
    if not names:
        try:
            classes = await _classes(p)
        except StatusError as e:
            return failed(str(e))
        names = [
            kube.name(c) for c in classes if ((c.get("metadata") or {}).get("annotations") or {}).get(DEFAULT) == "true"
        ][:1]
        if not names:
            return failed("no default StorageClass, and suites.storage.storageClasses is empty")
    p.state["pvcs"] = {}
    good, bad = [], []
    for sc in names:
        pvc_name = scratch.name(p, f"pvc-{sc}")
        start = time.monotonic()
        try:
            await _pvc(p, pvc_name, sc, cfg.size)
        except StatusError as e:
            bad.append(f"{sc}: creating the PVC: {e}")
            continue
        token = f"{p.run_id}-{sc}"
        problem = await _write_read(p, sc, pvc_name, token)
        if problem:
            bad.append(f"{sc}: {problem}")
            continue
        p.state["pvcs"][sc] = (pvc_name, token)
        good.append(f"{sc} in {time.monotonic() - start:.0f}s")
    if bad:
        return failed("; ".join(bad), "kubectl describe pvc in the scratch namespace shows the provisioner's events.")
    return passed("written and read back: " + ", ".join(good))


@check("storage-expand", GROUP_STORAGE, "A PVC can be expanded", SUITE)
async def check_expand(p: Probe) -> Outcome:
    tested = p.state.get("pvcs") or {}
    if not tested:
        return skipped("no PVC from the provisioning check to expand")
    expandable = {kube.name(c) for c in await _classes(p) if c.get("allowVolumeExpansion")}
    targets = {sc: v for sc, v in tested.items() if sc in expandable}
    if not targets:
        return skipped(f"allowVolumeExpansion is off on {', '.join(tested)}")
    new_size = str(int(scratch.parse_quantity(p.cfg.suites.storage.size) * 2))
    good, bad = [], []
    for sc, (pvc_name, token) in targets.items():
        try:
            await p.kube.patch(
                scratch.path(p, PVCS, pvc_name), {"spec": {"resources": {"requests": {"storage": new_size}}}}
            )
        except StatusError as e:
            bad.append(f"{sc}: {e}")
            continue

        def grown(o: dict) -> bool:
            cap = ((o.get("status") or {}).get("capacity") or {}).get("storage", "0")
            pending = kube.find(kube.status_conditions(o), "FileSystemResizePending").status == "True"
            return pending or scratch.parse_quantity(cap) >= float(new_size)

        pvc = await scratch.wait_for(lambda n=pvc_name: p.kube.get(scratch.path(p, PVCS, n)), grown, 90)
        if kube.find(kube.status_conditions(pvc), "FileSystemResizePending").status == "True":
            # The filesystem grows when a pod mounts the volume: read the data back while at it.
            vols, mounts = _volume(pvc_name)
            r = await scratch.run_task(
                p,
                f"resize-{sc}",
                "read",
                {"path": "/data/probe"},
                volumes=vols,
                mounts=mounts,
                timeout=p.cfg.suites.storage.pod_timeout,
            )
            if not r.ok or r.data.get("data") != token:
                bad.append(f"{sc}: after expansion: {r.explain() if not r.ok else 'data changed'}")
                continue
            pvc = await scratch.wait_for(lambda n=pvc_name: p.kube.get(scratch.path(p, PVCS, n)), grown, 90)
        cap = ((pvc.get("status") or {}).get("capacity") or {}).get("storage", "0")
        if scratch.parse_quantity(cap) < float(new_size):
            cond = kube.find(kube.status_conditions(pvc), "Resizing")
            bad.append(f"{sc}: capacity still {cap} after 3 minutes {first_non_empty(cond.message)}".rstrip())
            continue
        good.append(f"{sc} to {cap}")
    if bad:
        return failed("; ".join(bad))
    return passed("expanded: " + ", ".join(good))


@check("storage-snapshot", GROUP_STORAGE, "A snapshot restores to a new PVC with the data", SUITE)
async def check_snapshot(p: Probe) -> Outcome:
    snap_class = p.cfg.suites.storage.snapshot_class
    if not snap_class:
        return skipped("suites.storage.snapshotClass is empty")
    tested = p.state.get("pvcs") or {}
    if not tested:
        return skipped("no PVC from the provisioning check to snapshot")
    sc, (pvc_name, token) = next(iter(tested.items()))
    snap = scratch.name(p, f"snap-{sc}")
    try:
        await p.kube.create(
            scratch.path(p, SNAPSHOTS),
            {
                "apiVersion": "snapshot.storage.k8s.io/v1",
                "kind": "VolumeSnapshot",
                "metadata": scratch.meta(p, snap),
                "spec": {"volumeSnapshotClassName": snap_class, "source": {"persistentVolumeClaimName": pvc_name}},
            },
        )
    except StatusError as e:
        if e.code == 404:
            return failed("no volumesnapshots.snapshot.storage.k8s.io API", "Is the CSI snapshot controller installed?")
        return failed(f"creating the VolumeSnapshot: {e}")
    timeout = p.cfg.suites.storage.pod_timeout
    s = await scratch.wait_for(
        lambda: p.kube.get(scratch.path(p, SNAPSHOTS, snap)),
        lambda o: (o.get("status") or {}).get("readyToUse") is True,
        timeout,
    )
    st = s.get("status") or {}
    if st.get("readyToUse") is not True:
        err = (st.get("error") or {}).get("message", "")
        return failed(f"{snap_class}: snapshot of {sc} not ready after {timeout:.0f}s {err}".rstrip())
    restored = scratch.name(p, f"restore-{sc}")
    size = st.get("restoreSize") or p.cfg.suites.storage.size
    try:
        await _pvc(
            p, restored, sc, size, {"name": snap, "kind": "VolumeSnapshot", "apiGroup": "snapshot.storage.k8s.io"}
        )
    except StatusError as e:
        return failed(f"creating the PVC from the snapshot: {e}")
    vols, mounts = _volume(restored)
    r = await scratch.run_task(
        p, f"restore-{sc}", "read", {"path": "/data/probe"}, volumes=vols, mounts=mounts, timeout=timeout
    )
    if not r.ok:
        return failed(f"reading the restored PVC: {r.explain()} ({await _pvc_state(p, restored)})")
    if r.data.get("data") != token:
        return failed(f"the restored PVC holds {r.data.get('data')!r}, the snapshot was of {token!r}")
    return passed(f"{sc} via {snap_class}: snapshot ready, restored PVC has the data")
