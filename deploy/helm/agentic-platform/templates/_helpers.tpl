{{/* Names, labels and the pieces every pod shares. */}}

{{- define "agentic.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agentic.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "agentic.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Selector labels of one component: include "agentic.selector" (list $ "api") */}}
{{- define "agentic.selector" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: {{ include "agentic.name" $root }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{/* All labels of one component: include "agentic.labels" (list $ "api") */}}
{{- define "agentic.labels" -}}
{{- $root := index . 0 -}}
helm.sh/chart: {{ include "agentic.chart" $root }}
{{ include "agentic.selector" . }}
app.kubernetes.io/part-of: agentic-platform
app.kubernetes.io/version: {{ $root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ $root.Release.Service }}
{{- end -}}

{{- define "agentic.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agentic.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "agentic.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "agentic.secretName" -}}
{{- required "secrets.existingSecret is required: the Secret holding the control plane's DSNs and tokens" .Values.secrets.existingSecret -}}
{{- end -}}

{{/* One env entry read from the existing Secret: (list $ "NAME" "keyName" optional) */}}
{{- define "agentic.secretEnv" -}}
{{- $root := index . 0 -}}
- name: {{ index . 1 }}
  valueFrom:
    secretKeyRef:
      name: {{ include "agentic.secretName" $root }}
      key: {{ index . 2 }}
      {{- if index . 3 }}
      optional: true
      {{- end }}
{{- end -}}

{{/* Settings that hold a secret: only ever read from the existing Secret. */}}
{{- define "agentic.secretSettings" -}}
AGENTIC_CP_DATABASE_URL AGENTIC_CP_OWNER_DATABASE_URL AGENTIC_CP_METRICS_TOKEN AGENTIC_CP_SMTP_PASSWORD AGENTIC_CP_GATEWAY_ADMIN_KEY
{{- end -}}

{{/* The policy repo branch the environment tracks (M3-04): environment.policyBranch, else
the environment's own (dev, staging, main for prod), else none. */}}
{{- define "agentic.policyBranch" -}}
{{- $e := .Values.environment -}}
{{- default (get (dict "dev" "dev" "staging" "staging" "prod" "main") (default "" $e.name)) $e.policyBranch -}}
{{- end -}}

{{/* Install-time guards of the environment (M3-04), rendered once by the API Deployment.
prod refuses plaintext TLS, a Secret no secret manager writes, and a movable image tag. */}}
{{- define "agentic.environmentChecks" -}}
{{- $v := .Values -}}
{{- $name := default "" $v.environment.name -}}
{{- if not (has $name (list "" "dev" "staging" "prod")) }}
{{- fail (printf "environment.name %q: use dev, staging or prod (or leave it empty)" $name) }}
{{- end }}
{{- if has $name (list "staging" "prod") }}
{{- $np := $v.networkPolicy }}
{{- $reason := trim (default "" $np.optOutReason) }}
{{- if $np.enabled }}
{{- if not $np.cniEnforces }}
{{- fail (printf "%s needs networkPolicy.cniEnforces=true with networkPolicy.enabled: confirm that the cluster's CNI enforces NetworkPolicy (Calico, Cilium; not kindnet or plain Flannel), because an unenforced policy is ignored silently and the egress guard for tenant git URLs would not exist" $name) }}
{{- end }}
{{- if $reason }}
{{- fail "networkPolicy.optOutReason is set but networkPolicy.enabled is true: keep one of them" }}
{{- end }}
{{- else if not $reason }}
{{- fail (printf "%s without a NetworkPolicy needs networkPolicy.optOutReason (why the platform team cannot enforce one), or set networkPolicy.enabled=true and networkPolicy.cniEnforces=true: without it tenant-set git URLs can reach internal hosts" $name) }}
{{- end }}
{{- end }}
{{- if eq $name "prod" }}
{{- if not (and $v.ingress.enabled $v.ingress.host $v.ingress.tlsSecretName) }}
{{- fail "prod serves TLS only: set ingress.enabled, ingress.host and ingress.tlsSecretName (ingress.certManager makes the certificate)" }}
{{- end }}
{{- if not (hasPrefix "https://" (default "" $v.publicUrl)) }}
{{- fail "prod needs publicUrl to start with https:// (invite links)" }}
{{- end }}
{{- if not (or $v.externalSecrets.enabled $v.secrets.externallyManaged) }}
{{- fail "prod refuses a hand-made Secret: set externalSecrets.enabled (or secrets.externallyManaged=true when Sealed Secrets or a CSI driver writes it)" }}
{{- end }}
{{- if not $v.image.digest }}
{{- fail "prod needs image.digest (sha256:...): a tag can move under a running deployment" }}
{{- end }}
{{- end }}
{{- end -}}

{{/* The control plane's environment. Pass (list $ mode), mode "api", "worker" or "job".
Only the migration Job gets the owner DSN, and it gets none of the optional secrets the
long-running pods use (DF-807: least privilege on the pod that holds the owner role). */}}
{{- define "agentic.env" -}}
{{- $root := index . 0 -}}
{{- $mode := index . 1 -}}
{{- if not (has $mode (list "api" "worker" "job")) }}
{{- fail (printf "agentic.env: unknown mode %q" $mode) }}
{{- end }}
{{- $v := $root.Values -}}
{{- $keys := $v.secrets.keys -}}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_DATABASE_URL" $keys.databaseUrl false) }}
{{- if eq $mode "job" }}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_OWNER_DATABASE_URL" $keys.ownerDatabaseUrl false) }}
{{- else }}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_METRICS_TOKEN" $keys.metricsToken true) }}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_SMTP_PASSWORD" $keys.smtpPassword true) }}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_GATEWAY_ADMIN_KEY" $keys.gatewayAdminKey true) }}
{{- end }}
- name: AGENTIC_CP_POLICY_WORKDIR
  value: /tmp/agentic-cp/policy
{{- with include "agentic.policyBranch" $root }}
- name: AGENTIC_CP_POLICY_BRANCH
  value: {{ . | quote }}
{{- end }}
{{- with $v.publicUrl }}
- name: AGENTIC_CP_PUBLIC_URL
  value: {{ . | quote }}
{{- end }}
{{- with $v.minCliVersion }}
- name: AGENTIC_CP_MIN_CLI_VERSION
  value: {{ . | quote }}
{{- end }}
{{- with $v.tokenlean.url }}
- name: AGENTIC_CP_GATEWAY_URL
  value: {{ . | quote }}
{{- end }}
{{- with $v.tokenlean.adminUrl }}
- name: AGENTIC_CP_GATEWAY_ADMIN_URL
  value: {{ . | quote }}
{{- end }}
{{- if $v.smtp.host }}
- name: AGENTIC_CP_SMTP_HOST
  value: {{ $v.smtp.host | quote }}
- name: AGENTIC_CP_SMTP_PORT
  value: {{ $v.smtp.port | quote }}
- name: AGENTIC_CP_SMTP_SECURITY
  value: {{ $v.smtp.security | quote }}
{{- with $v.smtp.username }}
- name: AGENTIC_CP_SMTP_USERNAME
  value: {{ . | quote }}
{{- end }}
{{- with $v.smtp.from }}
- name: AGENTIC_CP_SMTP_FROM
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- if $v.gitConfigSecret }}
- name: AGENTIC_CP_GIT_CONFIG_DIR
  value: /etc/agentic/git
{{- end }}
{{- /* The control plane lower-cases a setting's name, so every check compares the upper-case
form (review F1); a name the chart sets itself would be a duplicate env entry, which
server-side apply refuses. */}}
{{- $secretSettings := splitList " " (include "agentic.secretSettings" $root) }}
{{- $chartSet := dict "AGENTIC_CP_POLICY_WORKDIR" "the chart's fixed path" "AGENTIC_CP_PUBLIC_URL" "publicUrl" "AGENTIC_CP_MIN_CLI_VERSION" "minCliVersion" "AGENTIC_CP_GATEWAY_URL" "tokenlean.url" "AGENTIC_CP_GATEWAY_ADMIN_URL" "tokenlean.adminUrl" "AGENTIC_CP_SMTP_HOST" "smtp.host" "AGENTIC_CP_SMTP_PORT" "smtp.port" "AGENTIC_CP_SMTP_SECURITY" "smtp.security" "AGENTIC_CP_SMTP_USERNAME" "smtp.username" "AGENTIC_CP_SMTP_FROM" "smtp.from" "AGENTIC_CP_GIT_CONFIG_DIR" "gitConfigSecret" "AGENTIC_CP_POLICY_BRANCH" "environment.policyBranch" }}
{{- range $name, $value := $v.config }}
{{- $upper := upper $name }}
{{- if has $upper $secretSettings }}
{{- fail (printf "config.%s holds a secret: put it in the Secret named by secrets.existingSecret" $name) }}
{{- end }}
{{- if hasKey $chartSet $upper }}
{{- fail (printf "config.%s is set by the chart: use %s" $name (get $chartSet $upper)) }}
{{- end }}
{{- if not (regexMatch "^AGENTIC_CP_[A-Z0-9_]+$" $name) }}
{{- fail (printf "config.%s: only AGENTIC_CP_* settings, in upper case, belong here (%s)" $name $upper) }}
{{- end }}
- name: {{ $name }}
  value: {{ $value | toString | quote }}
{{- end }}
{{- end -}}

{{/* The pod-level settings every pod shares: (list $ serviceAccount). An empty service
account means the chart's; the pre-install hook names another, because it runs before the
chart's own objects exist. */}}
{{- define "agentic.podCommon" -}}
{{- $sa := index . 1 -}}
{{- with index . 0 -}}
serviceAccountName: {{ default (include "agentic.serviceAccountName" .) $sa }}
automountServiceAccountToken: false
# Kubernetes service-link variables (AGENTIC_CP_SERVICE_HOST...) would be refused as
# unknown settings (DF-415)
enableServiceLinks: false
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
{{- end -}}

{{/* Volumes every pod has: a writable /tmp, and the tenants' git configs when set. */}}
{{- define "agentic.volumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ .Values.tmpSizeLimit }}
{{- if .Values.gitConfigSecret }}
- name: git-config
  secret:
    secretName: {{ .Values.gitConfigSecret }}
    defaultMode: 0440
{{- end }}
{{- end -}}

{{- define "agentic.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- if .Values.gitConfigSecret }}
- name: git-config
  mountPath: /etc/agentic/git
  readOnly: true
{{- end }}
{{- end -}}
