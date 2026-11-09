import ipaddress
import json

import pytest

from platform_probe.netpol import FRONTEND_PORT, FRONTEND_PORT_NAME, Peer, allowing_policies, match_selector

POLICIES = json.loads("""[
  {"metadata": {"name": "probe-default-deny"},
   "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "probe"}}, "policyTypes": ["Ingress"]}},
  {"metadata": {"name": "probe-frontend"},
   "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "frontend"}}, "policyTypes": ["Ingress"],
    "ingress": [{"from": [
      {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "api-gateway"}},
       "podSelector": {"matchLabels": {"gateway.networking.k8s.io/gateway-name": "api-gateway"}}},
      {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "console"}}}],
     "ports": [{"port": 8080, "protocol": "TCP"}]}]}},
  {"metadata": {"name": "allow-from-same-namespace"},
   "spec": {"podSelector": {}, "ingress": [{"from": [{"podSelector": {}}]}]}},
  {"metadata": {"name": "allow-from-openshift-ingress"},
   "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"namespaceSelector":
     {"matchLabels": {"policy-group.network.openshift.io/ingress": ""}}}]}]}},
  {"metadata": {"name": "egress-only"},
   "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}},
  {"metadata": {"name": "other-port"},
   "spec": {"podSelector": {}, "ingress": [{"ports": [{"port": 9090}]}]}},
  {"metadata": {"name": "named-port"},
   "spec": {"podSelector": {}, "ingress": [{"ports": [{"port": "http"}]}]}},
  {"metadata": {"name": "port-range"},
   "spec": {"podSelector": {}, "ingress": [{"ports": [{"port": 8000, "endPort": 8100}]}]}},
  {"metadata": {"name": "udp-only"},
   "spec": {"podSelector": {}, "ingress": [{"ports": [{"port": 8080, "protocol": "UDP"}]}]}},
  {"metadata": {"name": "pod-cidr-except-outsider"},
   "spec": {"podSelector": {}, "ingress": [{"from": [{"ipBlock": {"cidr": "10.128.0.0/14", "except": ["10.128.2.0/24"]}}]}]}},
  {"metadata": {"name": "pod-cidr"},
   "spec": {"podSelector": {}, "ingress": [{"from": [{"ipBlock": {"cidr": "10.128.0.0/14"}}]}]}},
  {"metadata": {"name": "not-outsider"},
   "spec": {"podSelector": {}, "ingress": [{"from": [{"podSelector": {"matchExpressions":
     [{"key": "app.kubernetes.io/component", "operator": "NotIn", "values": ["outsider"]}]}}]}]}},
  {"metadata": {"name": "other-pods"},
   "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "catalog"}}, "ingress": [{}]}}
]""")


def test_allowing_policies():
    frontend = {"app.kubernetes.io/instance": "probe", "app.kubernetes.io/component": "frontend"}
    outsider = Peer(
        labels={"app.kubernetes.io/instance": "probe", "app.kubernetes.io/component": "outsider"},
        ns_labels={"kubernetes.io/metadata.name": "platform-probe"},
        ip=ipaddress.ip_address("10.128.2.15"),
    )
    got = allowing_policies(POLICIES, frontend, outsider, FRONTEND_PORT, FRONTEND_PORT_NAME)
    assert got == ["allow-from-same-namespace", "named-port", "port-range", "pod-cidr"]


LABELS = {"a": "1", "b": "2"}


@pytest.mark.parametrize(
    ("sel", "want"),
    [
        ({}, True),
        ({"matchLabels": {"a": "1"}}, True),
        ({"matchLabels": {"a": "2"}}, False),
        ({"matchExpressions": [{"key": "a", "operator": "In", "values": ["0", "1"]}]}, True),
        ({"matchExpressions": [{"key": "c", "operator": "In", "values": ["1"]}]}, False),
        ({"matchExpressions": [{"key": "c", "operator": "NotIn", "values": ["1"]}]}, True),
        ({"matchExpressions": [{"key": "b", "operator": "Exists"}]}, True),
        ({"matchExpressions": [{"key": "b", "operator": "DoesNotExist"}]}, False),
        ({"matchExpressions": [{"key": "b", "operator": "Bogus"}]}, False),
    ],
)
def test_match_selector(sel, want):
    assert match_selector(sel, LABELS) is want


def test_missing_selector_matches_nothing():
    assert not match_selector(None, LABELS)
