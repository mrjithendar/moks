{{- define "moks.name" -}}moks{{- end -}}

{{/* resourceGroup picks what this render contains: "all" for the whole chart, or one group for one
     child Argo CD Application (docs/ARGOCD_APP_SPLIT.md). Every resource belongs to exactly one group.
     Returns "true" or nothing, and refuses a typo rather than rendering an empty Application. */}}
{{- define "moks.inGroup" -}}
{{- $selected := toString .root.Values.resourceGroup -}}
{{- if not (has $selected (list "all" "foundation" "database" "migrate" "opensearch" "api" "worker" "executor" "terminal" "reranker" "routing" "backup")) -}}
{{- fail (printf "resourceGroup %q is not a resource group; see docs/ARGOCD_APP_SPLIT.md" $selected) -}}
{{- end -}}
{{- if or (eq $selected "all") (eq $selected .group) -}}true{{- end -}}
{{- end -}}

{{- define "moks.labels" -}}
app.kubernetes.io/name: moks
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "moks.image" -}}
{{- if .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" (toString .Values.image.digest)) -}}{{- fail "image.digest must be sha256:<64 hex characters>; to follow a tag, leave it empty and set image.tag" -}}{{- end -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ required "image.digest or image.tag is required" .Values.image.tag }}
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
  {{- if and (eq .component "executor") .root.Values.databaseBackups.enabled }}
  - name: backup-work
    emptyDir: {sizeLimit: {{ .root.Values.databaseBackups.workspace }}}
  {{- end }}
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
  {{- if and (include "moks.searchActive" .root) (has .component (list "api" "worker")) }}
  - name: search-ca
    secret: {secretName: {{ .root.Values.rag.opensearch.secure.secrets.tls }}, items: [{key: ca.crt, path: ca.crt}]}   # never the node's key
  {{- end }}
{{- end -}}

{{- define "moks.volumeMounts" -}}
{{- $credentials := include "moks.credentialsSecret" . -}}
volumeMounts:
  - {name: tmp, mountPath: /tmp}
  {{- if and (eq .component "executor") .root.Values.databaseBackups.enabled }}
  - {name: backup-work, mountPath: /backup-work}
  {{- end }}
  - {name: seed, mountPath: /etc/moks, readOnly: true}
  {{- if .root.Values.customSkills }}
  - {name: skills, mountPath: /etc/moks-skills, readOnly: true}
  {{- end }}
  {{- if $credentials }}
  - {name: credentials, mountPath: /var/run/moks-credentials, readOnly: true}
  {{- end }}
  {{- if and (include "moks.searchActive" .root) (has .component (list "api" "worker")) }}
  - {name: search-ca, mountPath: /var/run/moks-search, readOnly: true}
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
    command: ["sh", "-c", "moks migrate && moks seed /etc/moks/seed.yaml --if-present && moks skills bootstrap"]
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

{{/* opensearch.yml for moks-search: the chart's file, then what depends on values. */}}
{{- define "moks.searchConfig" -}}
{{- $secure := .Values.rag.opensearch.secure -}}
{{ .Files.Get "files/opensearch/opensearch.yml" }}
node.store.allow_mmap: {{ $secure.allowMmap }}
{{- with $secure.diskWatermarks }}
cluster.routing.allocation.disk.watermark.low: {{ required "diskWatermarks.low" .low | quote }}
cluster.routing.allocation.disk.watermark.high: {{ required "diskWatermarks.high" .high | quote }}
cluster.routing.allocation.disk.watermark.flood_stage: {{ required "diskWatermarks.floodStage" .floodStage | quote }}
{{- end }}
{{- if $secure.pluginsInstalled }}
prometheus.indices: false   # per-index labels would multiply with every generation
{{- end }}
{{- if $secure.snapshots.enabled }}
s3.client.default.endpoint: {{ required "snapshots.endpoint" $secure.snapshots.endpoint | quote }}
s3.client.default.region: {{ $secure.snapshots.region | quote }}
s3.client.default.path_style_access: {{ $secure.snapshots.pathStyle }}
{{- end }}
{{- end -}}

{{- define "moks.searchSecurityFiles" -}}
[{{ range $i, $f := list "config" "roles" "roles_mapping" "action_groups" "tenants" "nodes_dn" "allowlist" "audit" }}{{ if $i }}, {{ end }}{key: {{ $f }}.yml, path: {{ $f }}.yml}{{ end }}]
{{- end -}}

{{/* Who a component is to moks-search. An API pod gets the search identity and no other. */}}
{{- define "moks.searchIdentities" -}}
{{- $clients := .root.Values.rag.opensearch.secure.secrets.clients -}}
- {name: MOKS_OPENSEARCH_USERNAME, value: moks_search}
- name: MOKS_OPENSEARCH_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ $clients }}, key: search-password}}
{{- if ne .component "api" }}
- {name: MOKS_OPENSEARCH_INDEX_USERNAME, value: moks_indexer}
- name: MOKS_OPENSEARCH_INDEX_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ $clients }}, key: index-password}}
- {name: MOKS_OPENSEARCH_LIFECYCLE_USERNAME, value: moks_lifecycle}
- name: MOKS_OPENSEARCH_LIFECYCLE_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ $clients }}, key: lifecycle-password}}
{{- end }}
{{- end -}}

{{/* True once search and indexing use moks-search. */}}
{{- define "moks.searchActive" -}}
{{- if and (eq .Values.rag.backend "opensearch") .Values.rag.opensearch.secure.active -}}true{{- end -}}
{{- end -}}

{{/* Settings for moks-search-dashboards. A file rather than variables, so TLS verification cannot fall back to an image default. */}}
{{- define "moks.searchDashboardsConfig" -}}
server.host: "0.0.0.0"
server.port: 5601
server.name: moks-search-dashboards
server.basePath: {{ .base | quote }}
server.rewriteBasePath: {{ ne .base "" }}
opensearch.hosts: ["https://moks-search:9200"]
opensearch.ssl.verificationMode: full
opensearch.ssl.certificateAuthorities: ["/etc/moks-dashboards/ca.crt"]
opensearch.username: moks_dashboards
opensearch.password: "${MOKS_DASHBOARDS_PASSWORD}"
opensearch.requestHeadersAllowlist: ["Authorization", "security_tenant", "securitytenant"]
opensearch_security.cookie.password: "${MOKS_DASHBOARDS_COOKIE}"
opensearch_security.cookie.secure: {{ .secureCookie }}
opensearch_security.multitenancy.enabled: false
opensearch_security.readonly_mode.roles: ["moks_knowledge_viewer"]
telemetry.enabled: false
{{- end -}}
