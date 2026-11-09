# Troubleshooting

The console runs with an Istio sidecar, and with `mesh.egress.registryOnly: true` (the default)
that sidecar only lets it reach hosts in the mesh's registry. Both shape what the checks see,
so a check can fail even though the same request works with curl from your laptop or a node.
The cases below are the ones seen so far.

Commands use `<ns>` for the release namespace, `<release>` for the Helm release name and
`<console-pod>` for the console's pod. The `istio-proxy` image has no shell tools beyond
`pilot-agent`, so the curl tests below bring their own image. Any image with curl works;
`registry.access.redhat.com/ubi9/ubi` is one. Use your mirror if the cluster can't pull from
there.

## Redirect check: `got 502`

**Symptom.** `gateway-redirect` fails with `got 502`, while `curl -I http://<hostname>/` from
outside the cluster gets `301` with an `https://` location.

**What the check does.** It sends `GET http://<gateway.hostname>/public` to `gateway.address`
(default `<gateway.name>-istio.<gateway.namespace>.svc`) on `gateway.httpPort`, without
following redirects, and passes only on exactly `301` with a `Location` starting `https://`.

**Cause.** The console's sidecar answers the `502` itself, for one of two reasons:

- **The hostname isn't in the mesh's registry.** The sidecar routes plain HTTP by `Host`
  header, not by the address the console connects to, and the probe's hostname isn't a
  Service. With `mesh.egress.registryOnly: true` the sidecar refuses it. HTTPS isn't affected,
  because the sidecar routes it by the gateway Service's IP. This is the case if the check
  passes with `registryOnly: false`.
- **Mesh mTLS to a plaintext listener.** When the request is routed to the gateway's Service
  through the mesh, the sidecar wraps it in mesh mTLS, because Istio's gateway pods count as
  mTLS-capable. The gateway's port-80 listener expects plain HTTP and rejects it.

An outside client has no sidecar, so it gets the `301`.

**Confirm.**

```sh
# block_all or BlackHole: the hostname isn't in the registry.
# A flag such as UC, UF or URX: the mTLS case.
kubectl logs -n <ns> <console-pod> -c istio-proxy --since=10m | grep ' 502 '
```

**Fix.** The chart renders two objects named `<release>-gateway-http`, both visible to the
release namespace only (`templates/mesh.yaml`). Upgrade to a chart version that has both.

- A VirtualService that routes the probe's hostname on the HTTP port to the gateway's Service.
- A DestinationRule that turns mesh TLS off for that port.

Notes:

- Neither is rendered when `gateway.address` is an IP address, because their hosts can't be
  one. Use a hostname there.
- With `gateway.address` empty, the host is `<name>-istio.<namespace>.svc.cluster.local`. If
  your cluster's DNS domain isn't `cluster.local`, set `gateway.address` to the full name.

Other things that also fail this check:

| Detail | Meaning |
|---|---|
| `got error: …` | Nothing answered on `gateway.address`:`gateway.httpPort` |
| `got 404` or `got 200` | A listener answered, but no redirect route matches the probe's hostname or `/public` |
| `got 302 https://…` (or 307, 308) | The redirect works, but the check accepts only 301 |

## OIDC: `discovery: error: … connection reset by peer`

**Symptom.** The OIDC checks fail fetching
`<issuerUrl>/.well-known/openid-configuration` with `connection reset by peer`, while the URL
works from elsewhere.

**What the probe does.** It fetches the discovery document, then the token, with the console's
mesh HTTP client. That traffic goes through the sidecar. With `registryOnly` on, the chart adds
a ServiceEntry for the issuer's host, HTTPS on port 443 only.

**Find which side resets it.** Run the same request once without the sidecar and once through
it.

A. Without the sidecar, from a throwaway pod:

```sh
kubectl run kc-test -n <ns> --rm -it --restart=Never \
  --image=registry.access.redhat.com/ubi9/ubi \
  --labels=sidecar.istio.io/inject=false \
  --overrides='{"metadata":{"annotations":{"sidecar.istio.io/inject":"false"}}}' \
  -- curl -sv https://<keycloak-host>/realms/<realm>/.well-known/openid-configuration
```

B. Through the console's sidecar. The debug container shares the pod's network, so its traffic
takes the probe's path:

```sh
kubectl debug -n <ns> -it <console-pod> \
  --image=registry.access.redhat.com/ubi9/ubi \
  -- curl -sv https://<keycloak-host>/realms/<realm>/.well-known/openid-configuration
```

### A is reset too: the network needs an egress proxy

The cluster can't connect to Keycloak directly, and your other clients reach it through a
proxy. On OpenShift:

```sh
oc get proxy cluster -o jsonpath='{.spec.httpsProxy}'
```

The probe's mesh client doesn't read `HTTPS_PROXY`/`NO_PROXY` yet, so it always connects
directly. That needs a code change. Once it does, the proxy's host must also be in
`mesh.egress.allowedHosts`.

### A works, B is reset: the sidecar drops or breaks the connection

A quick way to land here: the OIDC checks pass with `mesh.egress.registryOnly: false`.

```sh
# Does the sidecar have healthy endpoints for Keycloak? Lines with an IP and
# health_flags are endpoints; the default_priority/high_priority lines are only settings.
kubectl exec -n <ns> <console-pod> -c istio-proxy -- \
  pilot-agent request GET clusters | grep <keycloak-host> | grep -E 'health_flags|cx_active'

# A count that rises by more than the egress check's own dropped call (it calls
# mesh.egress.blockedUrl on purpose) means the sidecar treats the host as unknown and drops it
kubectl exec -n <ns> <console-pod> -c istio-proxy -- \
  pilot-agent request GET stats | grep 'BlackHoleCluster.upstream_cx_total'

# A mesh-wide DestinationRule forcing ISTIO_MUTUAL (e.g. host "*") wraps the
# connection to Keycloak in mesh mTLS, which Keycloak resets
kubectl get destinationrule -A -o yaml | grep -B5 -A5 ISTIO_MUTUAL
```

Also look for another ServiceEntry for the same host in another namespace, which can conflict
with the chart's:

```sh
kubectl get serviceentry -A -o yaml | grep -B10 <keycloak-host>
```

**No endpoint lines** mean the sidecar knows the host but has no address for it. Older chart
versions created the ServiceEntry with `resolution: DNS`, so the sidecar resolved the name
itself, and where that lookup failed it reset every connection, though the console resolved
the name fine. The chart now uses `resolution: NONE`: the sidecar forwards to the address the
console resolved, and still lets only that host's TLS server name through. Upgrade the chart.

The chart's ServiceEntry assumes HTTPS on 443. An issuer on another port, on plain `http://`,
or on an in-cluster Service is dropped the same way.

### Unblock in the meantime

Setting `mesh.egress.registryOnly: false` removes the egress restriction. If the OIDC checks
then pass, the cause is on the sidecar side (case B). The egress check is skipped while it's
off.

## NetworkPolicy check: `outsider → frontend: 0 reset`

**Symptom.** `mesh-network-policy` fails with `outsider → frontend: 0 reset`, or `0 refused`.

**What the check does.** The `outsider` pod, which has no sidecar, calls the frontend directly.
The chart's NetworkPolicies only let the gateway and the console reach the frontend, so the
call should be dropped and time out.

**Cause.** The call got through to the frontend pod, and its sidecar reset it because it only
accepts mTLS. NetworkPolicies add up: if any policy in the namespace allows the connection, it
is allowed, whatever the chart's policies deny. OpenShift project templates, and cluster
policy tools such as ACM, often add policies like `allow-from-same-namespace`, which lets
every pod in the namespace, the outsider included, reach every other.

**What the check shows.** When it fails, it reads the namespace's NetworkPolicies and names the
ones that admit the outsider on the frontend's port:

```
outsider → frontend: 0 reset, allowed by NetworkPolicy allow-from-same-namespace
```

If none does, the hint says the network plugin isn't enforcing NetworkPolicies. If it can't
read them, the detail says why: it needs the Role and ClusterRole in `templates/rbac.yaml`.

**Fix.** A NetworkPolicy can't take away what another allows, so the probe can't pass in that
namespace. Either:

- install the probe in a namespace without the policy, or
- if the policy is enforced cluster-wide (ACM, for example) and can't be removed, accept the
  result: pods in that namespace can bypass the gateway, which is what the check reports.
