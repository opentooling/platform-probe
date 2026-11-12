# platform-probe

Tests the features an OpenShift platform promises its tenants, end to end, and shows which
ones work. It checks the platform the way a team using it would: real traffic, real objects,
each feature reported as passed, failed or skipped, with a hint when it fails.

The **connectivity** suite, always on, covers a **Red Hat Connectivity Link** API gateway and an
**OpenShift Service Mesh 3**. Three **platform suites**, each switched on in the values, cover
what else a team relies on: **ingress & certificates**, **workload security**, **storage** and
**observability** (telemetry reaching a central Grafana, Loki, Mimir and Tempo stack) and
**Argo CD** (every instance up, its Applications healthy, teams able to deploy through it) and
**MetalLB** (LoadBalancer Services get an announced address that carries traffic).
See [Platform suites](#platform-suites), and [Adding checks](#adding-checks) for more.

platform-probe started as [opentooling/rhcl-probe](https://github.com/opentooling/rhcl-probe),
ported from Go to Python. The checks, config, console and chart values are the same; the
names are not (package, chart, image, labels), so an existing rhcl-probe release is replaced
with a fresh install, not upgraded.

It installs a small demo app (frontend → catalog v1/v2 → pricing) and a console. The console
sends real traffic through your gateway and between the services, and reports each feature as
passed, failed or skipped. You can run it from the browser or with `helm test`.

```
Gateway ──► frontend ──► catalog v1 (90%) ──► pricing
 TLS, auth,              catalog v2 (10%)     pricing (faulty, outlier pool only)
 rate limits     every hop is mutual TLS and reports its caller, version and attempt count
```

## What it checks

| Group | Checks |
|---|---|
| Connectivity Link | Kuadrant ready · Gateway programmed · probe routes accepted · AuthPolicies and RateLimitPolicies enforced · sidecars injected |
| API gateway | TLS certificate (issuer, expiry) · HTTP→HTTPS redirect · host and path routing · default deny · API keys (missing, wrong, valid) · OIDC token (valid, tampered) · claim-based authorization (only `admin` may POST) · gateway → app mTLS · header match · weighted split · URL rewrite and header filters · request mirroring · route timeout · route rate limit · per-caller limits by plan |
| Service mesh | SPIFFE identity on every hop · request ID propagation · STRICT mTLS rejects plaintext · AuthorizationPolicy · east-west weighted split and header routing · retries · fault injection (abort, delay) · per-hop timeout · outlier detection · egress `REGISTRY_ONLY` · NetworkPolicy blocks gateway bypass |

Platform suites, when switched on ([details](#platform-suites)):

| Group | Checks |
|---|---|
| Ingress & certificates | default IngressController available · edge Route with a trusted certificate that isn't about to expire, HTTP redirected to HTTPS · reencrypt Route to a service-CA certificate · passthrough Route ends TLS at the pod · cert-manager issues a certificate |
| Internal CA | the cert-manager issuer of the cluster-local CA is ready · it issues a certificate for a Service's `<svc>.<ns>.svc` and `.svc.cluster.local` names · a pod serving it is trusted by another pod that only has the CA bundle workloads get |
| Workload security | a tenant pod gets the expected SCC and a UID from the namespace's range · privileged, root, hostPath and hostNetwork pods refused · ResourceQuota refuses a pod over quota · a ClusterResourceQuota covers the namespace and refuses a pod over it · LimitRange sets defaults and refuses a container over its max |
| Storage | exactly one default StorageClass · for each StorageClass: a PVC binds and data written by one pod is read by another · expansion · snapshot restored to a new PVC with the data |
| MetalLB | controller and speakers ready on every node · every address pool advertised (L2 or BGP), with free addresses, BGP sessions established · a LoadBalancer Service has an address from the expected pool, is announced, and carries traffic from a pod |
| Argo CD | each instance (e.g. platform and applications) available · its Applications synced and healthy, no failed syncs or repo errors · a test Application deploys through it into the scratch namespace and is pruned again |
| Observability | a pod's log line reaches Loki · its metric reaches Mimir (scraped or pushed over OTLP) · its span reaches Tempo; each with the time it took |

A feature you switch off in the values (OIDC, NetworkPolicies, the platform's default deny…) is
reported as **skipped**, not failed.

The console opens on an **Overview**: the totals and every failing check, with its detail and
hint, under **Needs attention**. A sidebar lists the categories, each marked with its state;
a category's page shows its checks, failures first, and runs just that category. Running one
category keeps the latest results of the others. Each view has its own address
(`#/storage`, `#/experiments`) to bookmark.

The Connectivity Link status checks (Kuadrant, the Gateway, the probe's routes, policies and
sidecars) are the `rhcl` category; until chart 0.1.11 it was called `platform`, so update
`tests.categories` and `--filter` names that use it.

`helm test` runs one pod per category, one after another, so a plain `helm test` runs
everything and fails if anything failed; run one category with `--filter`:

```bash
helm test probe -n platform-probe --filter name=probe-platform-probe-test-storage --logs
```

`--filter` can be repeated, or exclude a category with `!name=…`. To make a plain `helm test`
run only some categories, list them in the values:

```yaml
tests:
  categories: [rhcl, gateway, mesh]   # empty = every enabled category
```

The categories are `rhcl` (Connectivity Link status), `gateway`, `mesh`, and each enabled suite: `ingress`, `pki`,
`workloads`, `storage`, `observability`, `argocd`, `metallb`. From a terminal or CI, `POST
/api/run?groups=storage,ingress` runs several, as does `platform-probe check --groups
storage,ingress`.

The console's **Experiments** panel runs the same scenarios hands-on: pick traffic split,
retries, fault injection, circuit breaker, authorization or rate limit, send up to 60 requests
and see each result.

## Requirements

- OpenShift Service Mesh 3 (Istio 1.22+), sidecar mode, with the gateway in the mesh
- Connectivity Link (Kuadrant v1 policies) and a Gateway API gateway (v1.1+)
- An HTTPS listener whose hostname covers the probe's hostname

[jugash/RHCL](https://github.com/jugash/RHCL) sets all of that up with GitOps. The defaults
below match it.

## Install

```bash
oc new-project platform-probe
```

The gateway accepts routes only from labelled namespaces:

```bash
oc label namespace platform-probe api-gateway-access=true
```

```bash
helm install probe oci://ghcr.io/opentooling/charts/platform-probe -n platform-probe \
  --set gateway.hostname=probe.api.example.internal
```

Run the suite. `helm test` fails if any check fails:

```bash
helm test probe -n platform-probe --logs
```

Or open the console:

```bash
oc port-forward -n platform-probe svc/probe-platform-probe-console 8080:8080
```

## Configuration

The values you are most likely to set:

| Value | Default | |
|---|---|---|
| `gateway.name`, `gateway.namespace` | `api-gateway` | The Gateway the routes attach to |
| `gateway.hostname` | `probe.api.example.internal` | Hostname of the probe's routes. No DNS record needed: the console dials the gateway directly |
| `gateway.tls.issuerContains` | `""` | Expected issuer of the gateway certificate, e.g. `internal-ca` |
| `expect.defaultDeny`, `expect.httpRedirect` | `true` | Whether the platform enforces these |
| `auth.apiKeys.namespace` | `kuadrant-system` | Where Authorino reads API-key Secrets |
| `auth.apiKeys.alice`, `.bob` | generated | Set them under Argo CD, which can't keep generated keys |
| `auth.oidc.*` | off | Issuer and client for the client-credentials check |
| `trustedCA` | OpenShift trust bundle | CA the console trusts for the gateway and the IdP |
| `mesh.strictMTLS`, `.authorizationPolicies`, `.trafficManagement` | `true` | Mesh features to set up and check |
| `mesh.egress.registryOnly` | `true` | Limit the console to the mesh registry |
| `networkPolicies.enabled` | `true` | Ingress NetworkPolicies for the demo app |
| `suites.ingress`, `.pki`, `.workloads`, `.storage`, `.observability`, `.argocd`, `.metallb` | off | [Platform suites](#platform-suites) to run |
| `scratch.namespace`, `scratch.create` | `<namespace>-scratch`, `true` | Where the platform suites create their test objects |
| `scratch.labels`, `scratch.annotations` | `{}` | Labels and annotations for the scratch namespace the chart creates |
| `extraObjects` | `[]` | Extra manifests, see below |

See [values.yaml](charts/platform-probe/values.yaml) for all of them.

## Platform suites

```yaml
suites:
  ingress:
    enabled: true
    certManager:
      issuer: { name: internal-ca, kind: ClusterIssuer }   # optional
  workloads:
    enabled: true
  storage:
    enabled: true
    storageClasses: [gp3-csi, ocs-storagecluster-cephfs]  # empty = the default class
    snapshotClass: csi-aws-vsc                            # optional
```

```yaml
suites:
  observability:
    enabled: true
    grafana:
      url: https://grafana.observability.example.com
      token: { existingSecret: grafana-probe-token }      # a Viewer service account token
      datasources: { loki: <uid>, mimir: <uid>, tempo: <uid> }
    labels:
      extra: { cluster: prod-eu-1 }                       # picks this cluster in the queries
    otlp:
      endpoint: http://alloy.alloy.svc:4318               # for traces
```

The observability suite checks the pipeline from a team's side: a pod in the scratch namespace
logs a marker line, serves (or pushes over OTLP) a marker metric and sends a span, all tagged
with the run ID. The console then asks Grafana's data source proxy until each one shows up in
Loki, Mimir and Tempo, and reports how long it took. The label names are configurable
(`labels.namespace`, `labels.pod`), as are the pod annotations a scraping collector discovers
(`metrics.podAnnotations`). With a multi-tenant stack, each data source sends this cluster's
tenant ID, so the probe needs none. A failure names the query it ran, to paste into Explore.

```yaml
suites:
  argocd:
    enabled: true
    instances:
      - name: platform
        namespace: openshift-gitops
      - name: applications
        namespace: gitops-apps
        failOnOutOfSync: false          # teams' apps may drift on purpose
        deploy:
          enabled: true
          repoURL: https://github.com/opentooling/platform-probe   # or your mirror
          path: argocd/probe-app
          revision: main
scratch:
  labels:
    argocd.argoproj.io/managed-by: gitops-apps   # OpenShift GitOps: let that instance deploy here
```

The Argo CD suite checks each instance you list: its ArgoCD resource is Available with the
server, repo server, controller and Redis running; and its Applications are synced and healthy,
with Degraded, Missing, failed syncs and error conditions (such as a repository it can't reach)
named in the failure. `ignore` leaves Applications out, `failOnOutOfSync: false` tolerates
drift. With `deploy.enabled`, the check deploys as a team would: it creates an Application in
that instance's namespace that syncs [argocd/probe-app](argocd/probe-app) (one ConfigMap) into
the scratch namespace, waits for Synced and Healthy, then deletes it with Argo CD's resources
finalizer, so Argo CD prunes the ConfigMap. Turn deploy on for the instance teams use; the
platform instance usually has the rights to deploy anything, which a probe should not use. The
console gets a Role in each instance's namespace, allowing it to read the ArgoCD resource and
Applications, and, for deploy, to create and delete Applications there.

```yaml
suites:
  metallb:
    enabled: true
    pool: public                       # the IPAddressPool teams get; empty = MetalLB picks
    bgpNamespace: openshift-frr-k8s    # where frr-k8s keeps BGPSessionStates, for BGP
```

The MetalLB suite reads MetalLB's own resources (a read-only Role in its namespace): the
controller and every speaker are ready, each IPAddressPool is covered by an L2Advertisement or
BGPAdvertisement, BGP sessions are Established, and pools that report their usage (MetalLB
0.15+) have addresses left. Its end-to-end check uses a LoadBalancer Service the chart creates
in the scratch namespace, in front of the backend pod each run starts: the Service has an
address from the expected pool, MetalLB reports who announces it (the node, with L2, or the BGP
peers; MetalLB 0.14+), and a pod reaches the backend on it. The console may not create
Services, so this shows MetalLB serving an existing Service, not assigning a new one each run;
the Service holds one address while the release is installed. Traffic from a pod to the address
may be handled inside the cluster's network, so the announcement is what shows the address is
reachable from outside.

```yaml
suites:
  pki:
    enabled: true
    issuer: { name: cluster-ca, kind: ClusterIssuer }      # the cluster-local CA's issuer
    trustBundle: { configMap: internal-ca-bundle, key: trust-bundle.pem }  # what workloads trust
    issuerContains: cluster-ca                              # optional
```

The Internal CA suite checks the CA that issues certificates for in-cluster names. It requests
a Certificate for a Service the chart creates in the scratch namespace, covering
`<svc>.<ns>.svc` and `<svc>.<ns>.svc.<clusterDomain>` (plus a name unique to the run, so the
issuer really issues one every run, into one Secret per release), starts a pod that serves it,
and connects to it by both names from another pod. That pod trusts only the CA bundle workloads
are given: `trustBundle`, typically a trust-manager Bundle's ConfigMap in the namespace, or by
default the scratch namespace's CA bundle. So a CA that issues fine but that workloads don't
trust fails, with the bundle it tried named. The console may read the one ClusterIssuer named
(or Issuer, in the scratch namespace) and create Certificates in the scratch namespace.

They test from a tenant's side, so they need to create things: pods, Routes, PVCs and
snapshots, a cert-manager Certificate. (The Service the Routes point at comes with the chart:
the console may not create Services, which would let it take traffic meant for other pods.) All of it goes into a **scratch namespace**,
`<release namespace>-scratch` by default, where the console gets a Role and nothing more
(cluster-wide it can only read IngressControllers and StorageClasses). Everything a run creates
is labelled with the release and the run, and deleted when the run ends; the next run also
deletes anything an interrupted one left. The one exception is the cert-manager check's
Secret, `<release>-probe-certificate`, which is kept and reissued on every run.

The chart creates the scratch namespace. To test what your teams actually get, make it
yourself with `oc new-project`, so your project template applies (ResourceQuota, LimitRange,
default NetworkPolicies), and set `scratch.create: false`:

```bash
oc new-project platform-probe-scratch
```

Or keep `scratch.create: true` and give the namespace what your platform keys on, such as the
label your log collector selects or a Pod Security level. Values go through `tpl`:

```yaml
scratch:
  labels:
    logging.example.com/collect: "true"
    pod-security.kubernetes.io/enforce: restricted
  annotations:
    openshift.io/node-selector: node-role.kubernetes.io/worker=
```

Test pods run the probe's own image, as an arbitrary UID under `restricted-v2`, and report back
through their termination message. With a private registry, put the pull secrets listed in
`imagePullSecrets` in the scratch namespace too. With your own `trustedCA.configMap`, copy that
ConfigMap there as well: the ingress checks trust the router's certificate through it.

Rejections (privileged pods, a pod over quota) are tested with server-side dry runs, so
admission runs in full and nothing is created. A quota is tested with a pod that asks for just
more than the room the quota has left, and no more than the LimitRange allows a container:
LimitRange admission runs first, so a bigger pod would be refused by the LimitRange and prove
nothing about the quota. When the room left is more than a container may ask for, the check is
skipped and says so.

ClusterResourceQuotas (OpenShift) select namespaces by label or annotation, often the
requester's. The check reads the ones that apply to the scratch namespace
(AppliedClusterResourceQuota); give the namespace what your quotas select with
`scratch.labels` / `scratch.annotations`, or create it with `oc new-project` as a team member.
Set `suites.workloads.expectClusterQuota: true` to fail rather than skip when none applies.

The storage checks run pods one after another and wait for volumes to attach, which takes
minutes. Give `helm test` the time:

```bash
helm test probe -n platform-probe --logs --timeout 30m
```

### Example: the RHCL platform with Keycloak and Vault

[examples/rhcl-platform-values.yaml](examples/rhcl-platform-values.yaml) installs the probe on the
platform from [jugash/RHCL](https://github.com/jugash/RHCL), with:

- **OIDC** tokens from Keycloak. The `KeycloakClient` (for keycloak-client-operator,
  `keycloak.jugash.com/v1alpha1`) is a confidential client with the client credentials grant only, so it has no `redirectUris`,
  `postLogoutRedirectUris` or `webOrigins`. A hardcoded `plan` claim sets its rate-limit plan.
- **API keys from Vault**, synced by External Secrets, with `auth.apiKeys.create: false`.
- **A Route for the console**, off by default: the console has no login.

Each part is a conditional `extraObjects` item, switched by `features.*`.

### extraObjects

Anything else to deploy with the release: ExternalSecrets for the API keys, an OpenShift Route
for the console, policies of your own. Each item is a YAML object or a string, rendered with
`tpl`, so it can use the chart's helpers. Wrap an item as `{when, object}` to render it only
when a condition holds:

```yaml
features:
  consoleRoute: true
extraObjects:
  - when: "{{ .Values.features.consoleRoute }}"
    object:
      apiVersion: route.openshift.io/v1
      kind: Route
      metadata:
        name: '{{ include "platform-probe.fullname" $ }}-console'
      spec:
        to: { kind: Service, name: '{{ include "platform-probe.fullname" $ }}-console' }
        port: { targetPort: http }
        tls: { termination: edge }
```

## How it works

One image runs every role, chosen by `$ROLE`. Each service answers with what it saw, plus
the answer of the next hop:

```json
{"service":"frontend","caller":"spiffe://cluster.local/ns/api-gateway/sa/api-gateway-istio",
 "requestId":"…","status":200,
 "next":{"service":"catalog","version":"v2", "next":{"service":"pricing","attempt":2}}}
```

Checks steer the services with request headers, which each hop passes on:

| Header | Effect |
|---|---|
| `x-probe-fail: pricing=0.3` | pricing answers 500 on 30% of requests |
| `x-probe-delay: frontend=3s` | frontend waits 3s |
| `x-probe-scenario: retry \| fault-abort \| fault-delay \| timeout \| outlier` | selects the mesh route for that scenario |
| `x-version: v2` | routes to catalog v2 |

A pod outside the mesh (`outsider`) calls the services on request, to show what STRICT mTLS
(connection reset) and NetworkPolicies (timeout) do to traffic that bypasses the gateway.

A check that fails while the same request works with curl is usually the console's sidecar at
work: see [Troubleshooting](docs/troubleshooting.md).

### Service accounts

**Kubernetes ServiceAccounts.** The chart creates one per role: frontend, catalog, pricing,
outsider and console. They are mainly identities in the mesh:

- **Mesh identity.** Istio names each pod's certificate after its ServiceAccount, e.g.
  `spiffe://cluster.local/ns/platform-probe/sa/probe-platform-probe-frontend`. The
  AuthorizationPolicies use these to allow only frontend to call catalog, and only catalog to
  call pricing. With one shared ServiceAccount, every pod would have the same identity, and
  neither the policies nor the SPIFFE ID checks could tell them apart.
- **API access for the console only.** The console reads the status of the Gateway, Kuadrant
  and the policies, with read-only RBAC. The other ServiceAccounts mount no token at all.

catalog v1 and v2 share one ServiceAccount, and so do both pricing pods: each pair is one
service, so it gets one identity.

**`serviceAccountsEnabled` on the Keycloak client.** It turns on the client credentials grant,
the only way an unattended pod can get a token. Login in a browser needs a person, and the
password grant would mean storing a user's password (OAuth 2.1 drops it). Keycloak creates a
user for the client, `service-account-platform-probe`, whose ID is the token's `sub`: the caller
the gateway's rate limits count. Without it, set `auth.oidc.enabled: false`, and the OIDC check
is skipped.

## Adding checks

A check is an async function that takes the `Probe` and returns `passed(...)`, `failed(detail,
hint)` or `skipped(...)`. It registers itself with `@check`, and runs in the order it is
defined. A suite is a module in [platform_probe/checks/](platform_probe/checks/), imported
there in run order; its checks name the suite, and run only when `suites.<name>.enabled` is on:

```python
@check("workloads-uid", GROUP_WORKLOADS, "Tenant pods get the expected SCC and an arbitrary UID", "workloads")
async def check_uid(p: Probe) -> Outcome:
    run = await scratch.run_task(p, "whoami", "whoami")
    if not run.ok:
        return failed(f"pod: {run.explain()}")
    ...
    return passed(f"UID {run.data['uid']}")
```

For a check that needs to see something from inside a tenant pod, add a task to
[tasks.py](platform_probe/tasks.py) and run it with `scratch.run_task`; objects it creates go
into the scratch namespace with `scratch.meta(p, name)`, so the sweep deletes them. The checks
are tested against an in-memory API server, [tests/fakekube.py](tests/fakekube.py), which runs
the tasks in-process.

A new suite also needs its switch in [config.py](platform_probe/config.py), the chart's
values and [config.yaml](charts/platform-probe/templates/config.yaml), and the API rules it
uses in [rbac.yaml](charts/platform-probe/templates/rbac.yaml) (cluster-wide reads) or
[scratch.yaml](charts/platform-probe/templates/scratch.yaml) (the scratch namespace). Anything a
check expects needs a value that switches it off, so a platform without the feature reports
**skipped**, not failed.

## Development

Python 3.13, the version the image runs:

```bash
python3.13 -m venv .venv
```

```bash
.venv/bin/pip install --require-hashes -r requirements.txt
```

```bash
.venv/bin/pip install pytest ruff
```

```bash
.venv/bin/python -m pytest
```

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

```bash
helm lint charts/platform-probe -f charts/platform-probe/ci/full-values.yaml
```

The runtime dependencies are pinned with hashes in `requirements.txt`, generated from
`requirements.in`:

```bash
uv pip compile requirements.in --python-version 3.13 --generate-hashes --no-header -o requirements.txt
```

Run a role locally:

```bash
ROLE=frontend PORT=9001 NEXT=http://localhost:9002 .venv/bin/python -m platform_probe
```

CI starts with a Trivy scan of the repository (Python dependencies, secrets, the Dockerfile) and of
the chart as rendered with each `ci/` values file. Every other job waits for it, and a fixable
HIGH or CRITICAL finding stops the pipeline. Findings that are accepted on purpose are listed,
with the reason, in [.trivyignore](.trivyignore).

Every push to `main` then builds, smoke-tests and scans the amd64 and arm64 images, and publishes
`ghcr.io/opentooling/platform-probe:sha-<commit>` with the chart at
`oci://ghcr.io/opentooling/charts/platform-probe` (version `0.1.<run>`, defaulting to that image).

## License

MIT
