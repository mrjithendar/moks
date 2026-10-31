{{- define "moks.name" -}}moks{{- end -}}

{{- define "moks.labels" -}}
app.kubernetes.io/name: moks
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "moks.image" -}}
{{- if .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" (toString .Values.image.digest)) -}}{{- fail "image.digest must be sha256:<64 hex characters> (scripts/set-image.sh)" -}}{{- end -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{- if eq (toString .Values.image.tag) "latest" -}}{{- fail "image.tag latest is mutable: pin image.digest (scripts/set-image.sh)" -}}{{- end -}}
{{ .Values.image.repository }}:{{ required "image.digest or image.tag is required (scripts/set-image.sh pins the digest)" .Values.image.tag }}
{{- end -}}
{{- end -}}

{{/* Shared pod-level settings for API and worker. */}}
{{- define "moks.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: {type: RuntimeDefault}
{{- end -}}

{{- define "moks.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: [ALL]}
{{- end -}}

{{/* The Secret a component reads. While secrets.split is false every component shares
     existingSecret, exactly as before; see docs/SECRET_SPLIT.md. */}}
{{- define "moks.secretName" -}}
{{- if .root.Values.secrets.split -}}{{ index .root.Values.secrets .component }}{{- else -}}{{ .root.Values.existingSecret }}{{- end -}}
{{- end -}}

{{- define "moks.env" -}}
envFrom:
  - configMapRef: {name: moks-config}
  - secretRef: {name: {{ include "moks.secretName" . }}}
{{- end -}}

{{- define "moks.serviceAccount" -}}
{{- if .root.Values.secrets.split -}}moks-{{ .component }}{{- else -}}{{ .root.Values.serviceAccount.name }}{{- end -}}
{{- end -}}

{{- define "moks.credentialsSecret" -}}
{{- if .root.Values.secrets.split -}}{{ index .root.Values.secrets.credentials .component | default "" }}{{- else -}}{{ .root.Values.credentialsSecret }}{{- end -}}
{{- end -}}

{{- define "moks.volumes" -}}
{{- $credentials := include "moks.credentialsSecret" . -}}
volumes:
  - name: tmp
    emptyDir: {sizeLimit: 64Mi}
  - name: seed
    configMap: {name: moks-seed}
  {{- if .root.Values.customSkills }}
  - name: skills
    configMap: {name: moks-skills}
  {{- end }}
  {{- if $credentials }}
  - name: credentials
    secret: {secretName: {{ $credentials }}, defaultMode: 0400}
  {{- end }}
{{- end -}}

{{- define "moks.volumeMounts" -}}
{{- $credentials := include "moks.credentialsSecret" . -}}
volumeMounts:
  - {name: tmp, mountPath: /tmp}
  - {name: seed, mountPath: /etc/moks, readOnly: true}
  {{- if .root.Values.customSkills }}
  - {name: skills, mountPath: /etc/moks-skills, readOnly: true}
  {{- end }}
  {{- if $credentials }}
  - {name: credentials, mountPath: /var/run/moks-credentials, readOnly: true}
  {{- end }}
{{- end -}}

{{/* With shared credentials each pod migrates, as before. Once split, only the moks-migrate Job
     holds the schema owner, and runtime pods wait for the schema they need. */}}
{{- define "moks.migrateInit" -}}
initContainers:
  {{- if .root.Values.secrets.split }}
  - name: wait-schema
    image: {{ include "moks.image" .root }}
    imagePullPolicy: {{ .root.Values.image.pullPolicy }}
    args: [wait-schema]
  {{- else }}
  - name: migrate
    image: {{ include "moks.image" .root }}
    imagePullPolicy: {{ .root.Values.image.pullPolicy }}
    command: ["sh", "-c", "moks migrate && moks seed /etc/moks/seed.yaml --if-present"]
  {{- end }}
    {{- include "moks.env" . | nindent 4 }}
    {{- include "moks.containerSecurity" .root | nindent 4 }}
    {{- include "moks.volumeMounts" . | nindent 4 }}
    resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {cpu: 500m, memory: 256Mi}}
{{- end -}}

{{- define "moks.readerSubjects" -}}
{{- if .Values.secrets.split }}
- {kind: ServiceAccount, name: moks-api, namespace: {{ .Release.Namespace }}}
- {kind: ServiceAccount, name: moks-worker, namespace: {{ .Release.Namespace }}}
{{- else }}
- {kind: ServiceAccount, name: {{ .Values.serviceAccount.name }}, namespace: {{ .Release.Namespace }}}
{{- end }}
{{- end -}}
