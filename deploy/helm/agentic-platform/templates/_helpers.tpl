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

{{/* The control plane's environment (API, worker, migration Job). The owner DSN is added by
the Job alone. Pass (list $ includeOwner). */}}
{{- define "agentic.env" -}}
{{- $root := index . 0 -}}
{{- $v := $root.Values -}}
{{- $keys := $v.secrets.keys -}}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_DATABASE_URL" $keys.databaseUrl false) }}
{{- if index . 1 }}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_OWNER_DATABASE_URL" $keys.ownerDatabaseUrl false) }}
{{- end }}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_METRICS_TOKEN" $keys.metricsToken true) }}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_SMTP_PASSWORD" $keys.smtpPassword true) }}
{{ include "agentic.secretEnv" (list $root "AGENTIC_CP_GATEWAY_ADMIN_KEY" $keys.gatewayAdminKey true) }}
- name: AGENTIC_CP_POLICY_WORKDIR
  value: /tmp/agentic-cp/policy
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
{{- $chartSet := dict "AGENTIC_CP_POLICY_WORKDIR" "the chart's fixed path" "AGENTIC_CP_PUBLIC_URL" "publicUrl" "AGENTIC_CP_MIN_CLI_VERSION" "minCliVersion" "AGENTIC_CP_GATEWAY_URL" "tokenlean.url" "AGENTIC_CP_GATEWAY_ADMIN_URL" "tokenlean.adminUrl" "AGENTIC_CP_SMTP_HOST" "smtp.host" "AGENTIC_CP_SMTP_PORT" "smtp.port" "AGENTIC_CP_SMTP_SECURITY" "smtp.security" "AGENTIC_CP_SMTP_USERNAME" "smtp.username" "AGENTIC_CP_SMTP_FROM" "smtp.from" "AGENTIC_CP_GIT_CONFIG_DIR" "gitConfigSecret" }}
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
