"""Which NetworkPolicies let the outsider reach the frontend, for the NetworkPolicy check's hint.

NetworkPolicies add up: any one that admits a connection lets it through, whatever the others
deny. This evaluates the namespace's policies the way the network plugin should.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import quote

from . import kube
from .runner import Probe

# The frontend's container port, as the chart declares it.
FRONTEND_PORT = 8080
FRONTEND_PORT_NAME = "http"


@dataclass
class Peer:
    """The pod a connection comes from, as NetworkPolicy peers see it."""

    labels: dict[str, str] = field(default_factory=dict)
    ns_labels: dict[str, str] = field(default_factory=dict)
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None


async def policies_allowing_outsider(p: Probe) -> list[str]:
    """The NetworkPolicies in the release namespace that let the outsider reach the frontend."""
    ns = p.cfg.namespace
    selector = quote("app.kubernetes.io/instance=" + p.cfg.release, safe="")
    pods = await p.kube.get(f"/api/v1/namespaces/{ns}/pods?labelSelector={selector}")
    frontend = outsider = None
    for pod in kube.items(pods):
        component = kube.labels(pod).get("app.kubernetes.io/component")
        if component == "frontend":
            frontend = pod
        elif component == "outsider":
            outsider = pod
    if frontend is None or outsider is None:
        raise LookupError("frontend or outsider pod not found")
    ns_obj = await p.kube.get("/api/v1/namespaces/" + quote(ns, safe=""))
    policies = await p.kube.get(f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies")
    pod_ip = (outsider.get("status") or {}).get("podIP")
    try:
        ip = ipaddress.ip_address(pod_ip) if isinstance(pod_ip, str) else None
    except ValueError:
        ip = None
    src = Peer(kube.labels(outsider), kube.labels(ns_obj), ip)
    return allowing_policies(kube.items(policies), kube.labels(frontend), src, FRONTEND_PORT, FRONTEND_PORT_NAME)


def allowing_policies(policies: list[dict], target: dict[str, str], src: Peer, port: int, port_name: str) -> list[str]:
    """The names of the policies that select the target pod for ingress and have a rule admitting
    the peer on the port. The peer is in the policies' own namespace."""
    names = []
    for pol in policies:
        spec = pol.get("spec") if isinstance(pol.get("spec"), dict) else {}
        if not match_selector(spec.get("podSelector"), target) or not has_ingress(spec):
            continue
        rules = spec.get("ingress")
        for rule in rules if isinstance(rules, list) else []:
            rule = rule if isinstance(rule, dict) else {}
            if rule_peers_match(rule.get("from"), src) and rule_ports_match(rule.get("ports"), port, port_name):
                names.append(kube.name(pol))
                break
    return names


def has_ingress(spec: dict) -> bool:
    """Without policyTypes, a policy always restricts ingress."""
    types = spec.get("policyTypes")
    return not isinstance(types, list) or "Ingress" in types


def rule_peers_match(raw, src: Peer) -> bool:
    """A rule without from admits every source."""
    peers = raw if isinstance(raw, list) else []
    if not peers:
        return True
    for pr in peers:
        pr = pr if isinstance(pr, dict) else {}
        if isinstance(pr.get("ipBlock"), dict):
            if in_ip_block(pr["ipBlock"], src.ip):
                return True
            continue
        has_pod, has_ns = "podSelector" in pr, "namespaceSelector" in pr
        if not (has_pod or has_ns):
            continue
        # Without a namespaceSelector, podSelector means pods in the policy's namespace, which
        # is the peer's. Both present: both must match.
        pod_ok = not has_pod or match_selector(pr["podSelector"], src.labels)
        ns_ok = not has_ns or match_selector(pr["namespaceSelector"], src.ns_labels)
        if pod_ok and ns_ok:
            return True
    return False


def rule_ports_match(raw, port: int, port_name: str) -> bool:
    """A rule without ports admits every port."""
    ports = raw if isinstance(raw, list) else []
    if not ports:
        return True
    for pp in ports:
        pp = pp if isinstance(pp, dict) else {}
        proto = pp.get("protocol")
        if isinstance(proto, str) and proto and proto != "TCP":
            continue
        v = pp.get("port")
        if v is None:
            return True
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            end = pp.get("endPort")
            end = end if isinstance(end, (int, float)) and not isinstance(end, bool) else 0
            if int(v) == port or (end > 0 and int(v) <= port <= int(end)):
                return True
        elif isinstance(v, str) and v == port_name:
            return True
    return False


def in_ip_block(ipb: dict, ip) -> bool:
    if ip is None:
        return False
    try:
        if ip not in ipaddress.ip_network(str(ipb.get("cidr", "")), strict=False):
            return False
    except (ValueError, TypeError):
        return False
    for e in ipb.get("except") or []:
        try:
            if ip in ipaddress.ip_network(str(e), strict=False):
                return False
        except (ValueError, TypeError):
            continue
    return True


def match_selector(raw, labels: dict[str, str]) -> bool:
    """Evaluate a LabelSelector. An empty selector matches everything; a missing one (None)
    matches nothing, as only a present selector is passed where the API makes it optional."""
    if not isinstance(raw, dict):
        return False
    ml = raw.get("matchLabels")
    for k, v in ml.items() if isinstance(ml, dict) else []:
        # The label must be present: OpenShift's policy-group labels match on an empty value.
        if k not in labels or labels[k] != (v if isinstance(v, str) else ""):
            return False
    exprs = raw.get("matchExpressions")
    for e in exprs if isinstance(exprs, list) else []:
        e = e if isinstance(e, dict) else {}
        key, op = e.get("key"), e.get("operator")
        values = [v for v in e.get("values") or [] if isinstance(v, str)]
        has = key in labels
        val = labels.get(key)
        if op == "In":
            if not has or val not in values:
                return False
        elif op == "NotIn":
            if has and val in values:
                return False
        elif op == "Exists":
            if not has:
                return False
        elif op == "DoesNotExist":
            if has:
                return False
        else:
            return False
    return True
