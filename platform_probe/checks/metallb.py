"""MetalLB: LoadBalancer Services get an address, it is announced, and traffic reaches the pods.

The end-to-end check uses a LoadBalancer Service the chart creates in the scratch namespace
(the console may not create Services) and the backend pod each run starts behind it. So it
shows that the Service has an address from the expected pool, that MetalLB announces it (the
node answering for it with L2, the peers it is advertised to with BGP), and that a pod reaches
the backend through it; not that a brand-new Service gets an address on every run.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import quote

from .. import kube, scratch
from ..kube import StatusError
from ..runner import GROUP_METALLB, Outcome, Probe, check, failed, passed

SUITE = "metallb"
V1B1 = "/apis/metallb.io/v1beta1/namespaces/{ns}/{res}"
FRR = "/apis/frrk8s.metallb.io/v1beta1/namespaces/{ns}/bgpsessionstates"


async def _list(p: Probe, path: str) -> list[dict] | None:
    """The items at path, or None when the API isn't installed."""
    try:
        return kube.items(await p.kube.get(path))
    except StatusError as e:
        if e.code == 404:
            return None
        raise


def _res(p: Probe, res: str) -> str:
    return V1B1.format(ns=p.cfg.suites.metallb.namespace, res=res)


@check("metallb-components", GROUP_METALLB, "MetalLB controller and speakers ready", SUITE)
async def check_components(p: Probe) -> Outcome:
    ns = p.cfg.suites.metallb.namespace
    try:
        deployments = kube.items(await p.kube.get(f"/apis/apps/v1/namespaces/{ns}/deployments"))
        daemonsets = kube.items(await p.kube.get(f"/apis/apps/v1/namespaces/{ns}/daemonsets"))
    except StatusError as e:
        return failed(f"{ns}: {e}")
    if not deployments and not daemonsets:
        return failed(f"nothing deployed in {ns}", "Is MetalLB installed there (suites.metallb.namespace)?")
    ready, bad = [], []
    for d in deployments:
        st, want = d.get("status") or {}, (d.get("spec") or {}).get("replicas", 1)
        (ready if st.get("availableReplicas", 0) >= want else bad).append(
            f"{kube.name(d)} {st.get('availableReplicas', 0)}/{want}"
        )
    for ds in daemonsets:
        st = ds.get("status") or {}
        want, got = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
        (ready if got >= want else bad).append(f"{kube.name(ds)} {got}/{want}")
    if bad:
        return failed(
            "not ready: " + ", ".join(bad),
            "A speaker that isn't ready on a node can't announce addresses from it.",
        )
    return passed(", ".join(ready))


def _advertised(pool: dict, ads: list[dict]) -> bool:
    """Whether an L2Advertisement or BGPAdvertisement covers the pool. One that names no pools
    and no selectors covers them all."""
    name, pool_labels = kube.name(pool), kube.labels(pool)
    for ad in ads:
        spec = ad.get("spec") or {}
        names, selectors = spec.get("ipAddressPools") or [], spec.get("ipAddressPoolSelectors") or []
        if not names and not selectors:
            return True
        if name in names:
            return True
        for sel in selectors:
            ml = (sel or {}).get("matchLabels") or {}
            if ml and all(pool_labels.get(k) == v for k, v in ml.items()):
                return True
    return False


@check("metallb-pools", GROUP_METALLB, "Address pools advertised, BGP sessions up, addresses left", SUITE)
async def check_pools(p: Probe) -> Outcome:
    m = p.cfg.suites.metallb
    try:
        pools = await _list(p, _res(p, "ipaddresspools"))
        if pools is None:
            return failed("no metallb.io API", "Is MetalLB installed?")
        l2 = await _list(p, _res(p, "l2advertisements")) or []
        bgp = await _list(p, _res(p, "bgpadvertisements")) or []
        sessions = await _list(p, FRR.format(ns=m.bgp_namespace))
    except StatusError as e:
        return failed(str(e))
    if not pools:
        return failed(f"no IPAddressPool in {m.namespace}", "LoadBalancer Services stay Pending without one.")
    problems, notes = [], []
    for pool in pools:
        name = kube.name(pool)
        if not _advertised(pool, l2 + bgp):
            problems.append(f"{name} isn't advertised (no L2Advertisement or BGPAdvertisement covers it)")
        st = pool.get("status") or {}
        if "availableIPv4" in st or "availableIPv6" in st:
            free = st.get("availableIPv4", 0) + st.get("availableIPv6", 0)
            used = st.get("assignedIPv4", 0) + st.get("assignedIPv6", 0)
            auto = (pool.get("spec") or {}).get("autoAssign", True)
            if auto and free < m.min_free_addresses:
                problems.append(f"{name} has {free} free addresses ({used} assigned)")
            else:
                notes.append(f"{name} {used} used, {free} free")
        else:
            notes.append(name)
    modes = [mode for mode, ads in (("L2", l2), ("BGP", bgp)) if ads]
    if bgp and sessions is not None:
        down = [
            f"{kube.name(s)} ({(s.get('status') or {}).get('bgpStatus', 'unknown')})"
            for s in sessions
            if (s.get("status") or {}).get("bgpStatus") != "Established"
        ]
        if down:
            problems.append("BGP sessions not established: " + ", ".join(down[:5]))
        elif sessions:
            notes.append(f"{len(sessions)} BGP sessions established")
    if problems:
        return failed("; ".join(problems))
    return passed(f"{'/'.join(modes) or 'no advertisements'}: " + ", ".join(notes))


def _in_pool(ip: str, pool: dict) -> bool:
    addr = ipaddress.ip_address(ip)
    for spec in (pool.get("spec") or {}).get("addresses") or []:
        try:
            if "-" in spec:
                lo, hi = (ipaddress.ip_address(x.strip()) for x in spec.split("-", 1))
                if lo <= addr <= hi:
                    return True
            elif addr in ipaddress.ip_network(spec.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


async def _announced(p: Probe, svc: str) -> str:
    """Who announces the Service: the node (L2) or the peers (BGP), from MetalLB's status
    resources (0.14 and later). "" if they aren't available."""
    sel = "labelSelector=" + quote(
        f"metallb.io/service-name={svc},metallb.io/service-namespace={scratch.ns(p)}", safe=""
    )
    try:
        l2 = await _list(p, _res(p, "servicel2statuses") + "?" + sel)
        bgp = await _list(p, _res(p, "servicebgpstatuses") + "?" + sel)
    except StatusError:
        return ""
    nodes = [(s.get("status") or {}).get("node", "") for s in l2 or []]
    peers = [peer for s in bgp or [] for peer in (s.get("status") or {}).get("peers") or []]
    if any(nodes):
        return "announced by node " + ", ".join(n for n in nodes if n)
    if peers:
        return f"advertised to {len(peers)} BGP peers"
    return ""


@check("metallb-service", GROUP_METALLB, "A LoadBalancer Service gets an address and carries traffic", SUITE)
async def check_service(p: Probe) -> Outcome:
    m = p.cfg.suites.metallb
    if not m.service:
        return failed("no LoadBalancer Service in the config (suites.metallb.service)")
    svc_path = scratch.path(p, "/api/v1/services", m.service)
    try:
        svc = await scratch.wait_for(
            lambda: p.kube.get(svc_path),
            lambda o: bool(((o.get("status") or {}).get("loadBalancer") or {}).get("ingress")),
            m.timeout,
        )
    except StatusError as e:
        return failed(f"{m.service}: {e}")
    ingress = ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    ip = (ingress[0] if ingress else {}).get("ip", "")
    if not ip:
        return failed(
            f"{m.service} has no address after {m.timeout:.0f}s",
            f"kubectl describe service {m.service} -n {scratch.ns(p)} shows MetalLB's events: an exhausted "
            "or unknown pool, or no pool with autoAssign.",
        )
    detail = [ip]
    if m.pool:
        try:
            pool = await p.kube.get(_res(p, "ipaddresspools") + "/" + m.pool)
        except StatusError as e:
            return failed(f"{ip}; pool {m.pool}: {e}")
        if not _in_pool(ip, pool):
            return failed(f"{ip} isn't in pool {m.pool} ({', '.join((pool.get('spec') or {}).get('addresses') or [])})")
        detail[0] += f" from {m.pool}"
    if announced := await _announced(p, m.service):
        detail.append(announced)
    try:
        await scratch.ensure_backend(p)
    except Exception as e:  # noqa: BLE001
        return failed(f"{', '.join(detail)}; backend: {e}")
    run = await scratch.run_task(p, "client-metallb", "fetch", {"requests": [{"url": f"http://{ip}:8080/"}]})
    if not run.ok:
        return failed(f"{', '.join(detail)}; client pod: {run.explain()}")
    res = (run.data.get("results") or [{}])[0]
    if res.get("status") != 200 or res.get("service") != "pricing":
        return failed(
            f"{', '.join(detail)}; http://{ip}:8080/ from a pod: {res.get('error') or res.get('status')}",
            "The address is assigned but doesn't carry traffic: check the speaker's announcement and the node's network.",
        )
    if not announced:
        return passed(
            f"{', '.join(detail)}; reached the backend from a pod (MetalLB before 0.14 doesn't report who announces it)"
        )
    return passed(f"{', '.join(detail)}; reached the backend from a pod")
