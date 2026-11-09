"""Platform: the status of Kuadrant, the Gateway, the probe's routes, policies and pods, read
from the Kubernetes API."""

from __future__ import annotations

from urllib.parse import quote

from .. import kube
from ..runner import GROUP_PLATFORM, Outcome, Probe, check, failed, first_non_empty, passed


def instance_selector(p: Probe) -> str:
    return "labelSelector=" + quote("app.kubernetes.io/instance=" + p.cfg.release, safe="")


@check("platform-kuadrant", GROUP_PLATFORM, "Kuadrant ready")
async def check_kuadrant(p: Probe) -> Outcome:
    ns = p.cfg.kuadrant.namespace
    try:
        lst = await p.kube.get(f"/apis/kuadrant.io/v1beta1/namespaces/{ns}/kuadrants")
    except Exception as e:  # noqa: BLE001
        return failed(
            str(e), "Is Connectivity Link installed, and does the console's ClusterRole allow reading Kuadrant?"
        )
    its = kube.items(lst)
    if not its:
        return failed(f"no Kuadrant in {ns}", f"Create the Kuadrant CR in {ns}.")
    c = kube.find(kube.status_conditions(its[0]), "Ready")
    if c.status != "True":
        return failed(f"kuadrant/{kube.name(its[0])} Ready={c.status}: {c.message}")
    return passed(f"kuadrant/{kube.name(its[0])} in {ns}")


@check("platform-gateway", GROUP_PLATFORM, "Gateway programmed")
async def check_gateway_programmed(p: Probe) -> Outcome:
    g = p.cfg.gateway
    try:
        obj = await p.kube.get(f"/apis/gateway.networking.k8s.io/v1/namespaces/{g.namespace}/gateways/{g.name}")
    except Exception as e:  # noqa: BLE001
        return failed(str(e), "Set gateway.name and gateway.namespace in the chart values.")
    c = kube.find(kube.status_conditions(obj), "Programmed")
    if c.status != "True":
        return failed(f"{g.namespace}/{g.name} Programmed={c.status}: {c.message}")
    listeners = (obj.get("status") or {}).get("listeners")
    return passed(f"{g.namespace}/{g.name}, {len(listeners) if isinstance(listeners, list) else 0} listeners")


@check("platform-routes", GROUP_PLATFORM, "Probe routes accepted by the gateway")
async def check_routes(p: Probe) -> Outcome:
    try:
        lst = await p.kube.get(
            f"/apis/gateway.networking.k8s.io/v1/namespaces/{p.cfg.namespace}/httproutes?{instance_selector(p)}"
        )
    except Exception as e:  # noqa: BLE001
        return failed(str(e))
    routes = kube.items(lst)
    if not routes:
        return failed(f"no HTTPRoutes labelled app.kubernetes.io/instance={p.cfg.release} in {p.cfg.namespace}")
    bad, hint = [], ""
    for r in routes:
        parents = (r.get("status") or {}).get("parents")
        ok = False
        for par in parents if isinstance(parents, list) else []:
            conds = kube.conditions(par.get("conditions") if isinstance(par, dict) else None)
            acc, refs = kube.find(conds, "Accepted"), kube.find(conds, "ResolvedRefs")
            if acc.status == "True" and refs.status != "False":
                ok = True
            elif acc.reason == "NotAllowedByListeners":
                hint = (
                    "The gateway only accepts routes from labelled namespaces. Label this one: "
                    f"oc label namespace {p.cfg.namespace} api-gateway-access=true"
                )
        if not ok:
            bad.append(kube.name(r))
    if bad:
        return failed("not accepted: " + ", ".join(bad), hint)
    return passed(f"{len(routes)} HTTPRoutes accepted")


@check("platform-policies", GROUP_PLATFORM, "Probe AuthPolicies and RateLimitPolicies enforced")
async def check_policies(p: Probe) -> Outcome:
    bad, good = [], []
    for kind in ("authpolicies", "ratelimitpolicies"):
        try:
            lst = await p.kube.get(f"/apis/kuadrant.io/v1/namespaces/{p.cfg.namespace}/{kind}?{instance_selector(p)}")
        except Exception as e:  # noqa: BLE001
            return failed(f"{kind}: {e}")
        for it in kube.items(lst):
            c = kube.find(kube.status_conditions(it), "Enforced")
            name = kind.removesuffix("ies") + "y/" + kube.name(it)
            if c.status == "True":
                good.append(name)
            else:
                bad.append(f"{name} ({first_non_empty(c.message, c.reason, 'no Enforced condition')})")
    if bad:
        return failed("not enforced: " + "; ".join(bad))
    if not good:
        return failed("no probe policies found")
    return passed(f"{len(good)} policies enforced")


def container_names(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [c["name"] for c in raw if isinstance(c, dict) and isinstance(c.get("name"), str)]


@check("platform-sidecars", GROUP_PLATFORM, "Probe pods have the istio-proxy sidecar")
async def check_sidecars(p: Probe) -> Outcome:
    sel = quote(f"app.kubernetes.io/instance={p.cfg.release},platform-probe/mesh=true", safe="")
    try:
        lst = await p.kube.get(f"/api/v1/namespaces/{p.cfg.namespace}/pods?labelSelector={sel}")
    except Exception as e:  # noqa: BLE001
        return failed(str(e))
    pods = kube.items(lst)
    missing = []
    for pod in pods:
        spec = pod.get("spec") or {}
        if "istio-proxy" not in container_names(spec.get("containers")) + container_names(spec.get("initContainers")):
            missing.append(kube.name(pod))
    if not pods:
        return failed("no probe pods found")
    if missing:
        return failed(
            "no sidecar: " + ", ".join(missing),
            "Sidecar injection didn't happen. If the mesh revision isn't \"default\", set mesh.revision; "
            "existing pods need a restart.",
        )
    return passed(f"{len(pods)} / {len(pods)} pods")
