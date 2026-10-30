{{- define "moks.name" -}}moks{{- end -}}

{{- define "moks.labels" -}}
app.kubernetes.io/name: moks
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "moks.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
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

{{- define "moks.env" -}}
envFrom:
  - configMapRef: {name: moks-config}
  - secretRef: {name: {{ .Values.existingSecret }}}
{{- end -}}

{{- define "moks.volumes" -}}
volumes:
  - name: tmp
    emptyDir: {sizeLimit: 64Mi}
  - name: seed
    configMap: {name: moks-seed}
  {{- if .Values.customSkills }}
  - name: skills
    configMap: {name: moks-skills}
  {{- end }}
  {{- if .Values.credentialsSecret }}
  - name: credentials
    secret: {secretName: {{ .Values.credentialsSecret }}, defaultMode: 0400}
  {{- end }}
{{- end -}}

{{- define "moks.volumeMounts" -}}
volumeMounts:
  - {name: tmp, mountPath: /tmp}
  - {name: seed, mountPath: /etc/moks, readOnly: true}
  {{- if .Values.customSkills }}
  - {name: skills, mountPath: /etc/moks-skills, readOnly: true}
  {{- end }}
  {{- if .Values.credentialsSecret }}
  - {name: credentials, mountPath: /var/run/moks-credentials, readOnly: true}
  {{- end }}
{{- end -}}

{{/* Migration init container: every component that touches the database waits for the schema it
     was built for. Migrations take a PostgreSQL advisory lock, so pods starting together are safe. */}}
{{- define "moks.migrateInit" -}}
initContainers:
  - name: migrate
    image: {{ include "moks.image" . }}
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    command: ["sh", "-c", "moks migrate && moks seed /etc/moks/seed.yaml --if-present"]
    {{- include "moks.env" . | nindent 4 }}
    {{- include "moks.containerSecurity" . | nindent 4 }}
    {{- include "moks.volumeMounts" . | nindent 4 }}
    resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {cpu: 500m, memory: 256Mi}}
{{- end -}}
