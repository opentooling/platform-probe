{{- define "platform-probe.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified name, short enough to take the longest suffix (-pricing-faulty) and stay a
valid Service name.
*/}}
{{- define "platform-probe.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 40 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 40 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 40 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "platform-probe.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "platform-probe.selectorLabels" -}}
app.kubernetes.io/name: {{ include "platform-probe.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "platform-probe.labels" -}}
helm.sh/chart: {{ include "platform-probe.chart" . }}
{{ include "platform-probe.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector for one component's pods. Pass a dict: ctx, component. */}}
{{- define "platform-probe.componentSelector" -}}
{{ include "platform-probe.selectorLabels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* Selector for every pod of a role (catalog = v1 and v2). Pass a dict: ctx, role. */}}
{{- define "platform-probe.roleSelector" -}}
{{ include "platform-probe.selectorLabels" .ctx }}
platform-probe/role: {{ .role }}
{{- end }}

{{- define "platform-probe.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{/* A ServiceAccount name for a role. Pass a dict: ctx, role. */}}
{{- define "platform-probe.sa" -}}
{{- printf "%s-%s" (include "platform-probe.fullname" .ctx) .role }}
{{- end }}

{{/* Value of the platform-probe/api label the API-key Secrets need, for extraObjects too. */}}
{{- define "platform-probe.apiKeySelector" -}}
{{- printf "%s-%s" (include "platform-probe.fullname" .) .Release.Namespace | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "platform-probe.gatewaySA" -}}
{{- default (printf "%s-istio" .Values.gateway.name) .Values.gateway.serviceAccount }}
{{- end }}

{{/* An AuthorizationPolicy principal. Pass a dict: ctx, namespace, sa. */}}
{{- define "platform-probe.principal" -}}
{{- printf "%s/ns/%s/sa/%s" .ctx.Values.trustDomain .namespace .sa }}
{{- end }}

{{/* Base URL of one of the probe's Services. Pass a dict: ctx, svc. */}}
{{- define "platform-probe.url" -}}
{{- printf "http://%s-%s:8080" (include "platform-probe.fullname" .ctx) .svc }}
{{- end }}

{{/* The CA bundle ConfigMap the console mounts, or empty for none. */}}
{{- define "platform-probe.caConfigMap" -}}
{{- if .Values.trustedCA.configMap }}
{{- .Values.trustedCA.configMap }}
{{- else if .Values.trustedCA.openshiftInjected }}
{{- printf "%s-ca-bundle" (include "platform-probe.fullname" .) }}
{{- end }}
{{- end }}

{{/* The scratch namespace, where the platform suites create their test objects. */}}
{{- define "platform-probe.scratchNamespace" -}}
{{- default (printf "%s-scratch" .Release.Namespace) .Values.scratch.namespace | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Whether any suite that creates objects in the scratch namespace is on. */}}
{{- define "platform-probe.writeSuites" -}}
{{- $s := .Values.suites }}
{{- $argoDeploy := false }}
{{- if $s.argocd.enabled }}
{{- range $s.argocd.instances }}{{ if and .deploy .deploy.enabled }}{{ $argoDeploy = true }}{{ end }}{{ end }}
{{- end }}
{{- if or $s.ingress.enabled $s.workloads.enabled $s.storage.enabled $s.observability.enabled $s.metallb.enabled $argoDeploy }}true{{ end }}
{{- end }}

{{/* The check categories that run, in run order: one `helm test` pod each. */}}
{{- define "platform-probe.categories" -}}
{{- $s := .Values.suites }}
{{- $c := list "platform" "gateway" "mesh" }}
{{- range $name := list "ingress" "workloads" "storage" "observability" "argocd" "metallb" }}
{{- if (index $s $name).enabled }}{{ $c = append $c $name }}{{ end }}
{{- end }}
{{- join "," $c }}
{{- end }}

{{/* Labels for sidecar injection. Pass a dict: ctx, mesh (bool). */}}
{{- define "platform-probe.meshLabels" -}}
{{- if not .mesh }}
sidecar.istio.io/inject: "false"
platform-probe/mesh: "false"
{{- else if .ctx.Values.mesh.revision }}
istio.io/rev: {{ .ctx.Values.mesh.revision }}
platform-probe/mesh: "true"
{{- else }}
sidecar.istio.io/inject: "true"
platform-probe/mesh: "true"
{{- end }}
{{- end }}

{{/* restricted-v2 friendly: no fixed UID, so OpenShift can assign one. */}}
{{- define "platform-probe.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "platform-probe.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}

{{- define "platform-probe.validate" -}}
{{- if not .Values.gateway.hostname }}
{{- fail "gateway.hostname is required: a hostname under the gateway listener's hostname" }}
{{- end }}
{{- if and .Values.auth.oidc.enabled (or (not .Values.auth.oidc.issuerUrl) (not .Values.auth.oidc.clientId) (not .Values.auth.oidc.clientSecret.existingSecret)) }}
{{- fail "auth.oidc.enabled needs auth.oidc.issuerUrl, auth.oidc.clientId and auth.oidc.clientSecret.existingSecret" }}
{{- end }}
{{- $o := .Values.suites.observability }}
{{- if and $o.enabled (or (not $o.grafana.url) (not $o.grafana.token.existingSecret)) }}
{{- fail "suites.observability.enabled needs suites.observability.grafana.url and .grafana.token.existingSecret" }}
{{- end }}
{{- if .Values.suites.argocd.enabled }}
{{- range .Values.suites.argocd.instances }}
{{- if not .namespace }}
{{- fail "every suites.argocd.instances entry needs a namespace" }}
{{- end }}
{{- if and .deploy .deploy.enabled (not .deploy.repoURL) }}
{{- fail (printf "suites.argocd.instances %s: deploy.enabled needs deploy.repoURL" (.name | default .namespace)) }}
{{- end }}
{{- end }}
{{- end }}
{{- if not (has $o.metrics.method (list "annotations" "otlp")) }}
{{- fail "suites.observability.metrics.method must be annotations or otlp" }}
{{- end }}
{{- $pct := list (int .Values.routes.splitV2Percent) (int .Values.mesh.splitV2Percent) }}
{{- range $pct }}
{{- if or (lt . 0) (gt . 100) }}
{{- fail "routes.splitV2Percent and mesh.splitV2Percent must be between 0 and 100" }}
{{- end }}
{{- end }}
{{- end }}
