{{/*
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
*/}}

{{/* Chart name, truncated to the 63-character DNS label limit */}}
{{- define "gerrit.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release-qualified name; the release name alone when it already
holds the chart name. Capped at 52 characters: the StatefulSet
controller labels each revision "<name>-<hash>" within the 63-character
label limit, and that cap also leaves room for "-headless" on the
longest Service name. */}}
{{- define "gerrit.fullname" -}}
{{- $full := "" -}}
{{- if .Values.fullnameOverride -}}
{{- $full = .Values.fullnameOverride -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- $full = .Release.Name -}}
{{- else -}}
{{- $full = printf "%s-%s" .Release.Name $name -}}
{{- end -}}
{{- end -}}
{{- $full | trunc 52 | trimSuffix "-" -}}
{{- end -}}

{{- define "gerrit.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "gerrit.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gerrit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "gerrit.labels" -}}
helm.sh/chart: {{ include "gerrit.chart" . }}
{{ include "gerrit.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gerrit
{{- end -}}

{{/* Name label of the git gc CronJob's pods: the chart name, cut to
56 characters so the "-git-gc" suffix fits the 63-character label
limit. A name of 62 or 63 characters already ending in "-git-gc" comes
out unchanged, so it takes "-gc" instead, which is shorter than the
name and so differs from it. */}}
{{- define "gerrit.gcName" -}}
{{- $name := include "gerrit.name" . -}}
{{- $base := $name | trunc 56 | trimSuffix "-" -}}
{{- $gc := printf "%s-git-gc" $base -}}
{{- if eq $gc $name -}}
{{- $gc = printf "%s-gc" $base -}}
{{- end -}}
{{- $gc -}}
{{- end -}}

{{/* Labels of the git gc CronJob's pods. Their name differs from the
Gerrit pod's, so no Service selector or Gerrit NetworkPolicy matches
them. */}}
{{- define "gerrit.gcSelectorLabels" -}}
app.kubernetes.io/name: {{ include "gerrit.gcName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: git-gc
{{- end -}}

{{- define "gerrit.gcLabels" -}}
helm.sh/chart: {{ include "gerrit.chart" . }}
{{ include "gerrit.gcSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gerrit
{{- end -}}

{{/* Name label of the log cleanup CronJob's pods: the chart name, cut
to 51 characters so the "-log-cleanup" suffix fits the 63-character
label limit. A name that already ends in "-log-cleanup" and would come
out unchanged takes "-lc" instead, as gerrit.gcName does. */}}
{{- define "gerrit.logCleanupName" -}}
{{- $name := include "gerrit.name" . -}}
{{- $base := $name | trunc 51 | trimSuffix "-" -}}
{{- $cleanup := printf "%s-log-cleanup" $base -}}
{{- if eq $cleanup $name -}}
{{- $cleanup = printf "%s-lc" $base -}}
{{- end -}}
{{- $cleanup -}}
{{- end -}}

{{/* Labels of the log cleanup CronJob's pods. Their name differs from
the Gerrit pod's, so no Service selector or Gerrit NetworkPolicy
matches them. */}}
{{- define "gerrit.logCleanupSelectorLabels" -}}
app.kubernetes.io/name: {{ include "gerrit.logCleanupName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: log-cleanup
{{- end -}}

{{- define "gerrit.logCleanupLabels" -}}
helm.sh/chart: {{ include "gerrit.chart" . }}
{{ include "gerrit.logCleanupSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gerrit
{{- end -}}

{{/* Name label of the admin toolbox's pods: the chart name, cut to 55
characters so the "-toolbox" suffix fits the 63-character label limit.
A name that already ends in "-toolbox" and would come out unchanged
takes "-tb" instead, as gerrit.gcName does. */}}
{{- define "gerrit.toolboxName" -}}
{{- $name := include "gerrit.name" . -}}
{{- $base := $name | trunc 55 | trimSuffix "-" -}}
{{- $toolbox := printf "%s-toolbox" $base -}}
{{- if eq $toolbox $name -}}
{{- $toolbox = printf "%s-tb" $base -}}
{{- end -}}
{{- $toolbox -}}
{{- end -}}

{{/* Labels of the admin toolbox's pods. Their name differs from the
Gerrit pod's, so no Service selector or Gerrit NetworkPolicy matches
them. */}}
{{- define "gerrit.toolboxSelectorLabels" -}}
app.kubernetes.io/name: {{ include "gerrit.toolboxName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: admin-toolbox
{{- end -}}

{{- define "gerrit.toolboxLabels" -}}
helm.sh/chart: {{ include "gerrit.chart" . }}
{{ include "gerrit.toolboxSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gerrit
{{- end -}}

{{/* Fully qualified DNS name of the Gerrit pod under the headless
Service, with the trailing dot so a lookup never tries a search domain.
It exists exactly while the pod does, ready or not. */}}
{{- define "gerrit.podDNSName" -}}
{{- $fullname := include "gerrit.fullname" . -}}
{{- printf "%s-0.%s-headless.%s.svc.%s." $fullname $fullname .Release.Namespace (trimSuffix "." .Values.clusterDomain) -}}
{{- end -}}

{{/* Image reference; a digest takes precedence over the tag. Release
images are tagged vX.Y.Z rather than with the Gerrit version in
appVersion, so there is no fallback: one of the two must be set. */}}
{{- define "gerrit.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else if .Values.image.tag -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- else -}}
{{- fail "set image.tag (a release tag such as v1.2.3) or image.digest; the chart published with each release sets image.digest" -}}
{{- end -}}
{{- end -}}

{{- define "gerrit.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ if kindIs "map" . }}{{ .name }}{{ else }}{{ . }}{{ end }}
{{- end }}
{{- end }}
{{- end -}}

{{/* The gerrit user and group of the official Gerrit image */}}
{{- define "gerrit.uid" -}}1001{{- end -}}

{{- define "gerrit.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ include "gerrit.uid" . }}
runAsGroup: {{ include "gerrit.uid" . }}
fsGroup: {{ include "gerrit.uid" . }}
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "gerrit.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
capabilities:
  drop:
    - ALL
{{- end -}}

{{/* Keep the OpenTelemetry operator from injecting agents */}}
{{- define "gerrit.otelOptOutAnnotations" -}}
instrumentation.opentelemetry.io/inject-java: "false"
instrumentation.opentelemetry.io/inject-nodejs: "false"
instrumentation.opentelemetry.io/inject-python: "false"
instrumentation.opentelemetry.io/inject-dotnet: "false"
instrumentation.opentelemetry.io/inject-go: "false"
instrumentation.opentelemetry.io/inject-sdk: "false"
{{- end -}}

{{/* Pre-created Secrets survive uninstall and Argo CD pruning */}}
{{- define "gerrit.keepAnnotations" -}}
helm.sh/resource-policy: keep
argocd.argoproj.io/sync-options: Prune=false,Delete=false
{{- end -}}

{{/* URL prefix without a trailing slash: "/r", or "" for root */}}
{{- define "gerrit.urlPrefix" -}}
{{- $p := trimAll "/" .Values.gerrit.urlPrefix -}}
{{- if $p -}}/{{ $p }}{{- end -}}
{{- end -}}

{{/* gerrit.host without any port, as the Ingress host */}}
{{- define "gerrit.httpHost" -}}
{{- regexReplaceAll ":[0-9]+$" .Values.gerrit.host "" -}}
{{- end -}}

{{/* ssh.host, or gerrit.host, without any port */}}
{{- define "gerrit.sshHost" -}}
{{- regexReplaceAll ":[0-9]+$" (default .Values.gerrit.host .Values.ssh.host) "" -}}
{{- end -}}

{{/* Behind the chart's Ingress the schemes follow ingress.tls.enabled,
so the URL Gerrit builds links with matches what the Ingress serves */}}
{{- define "gerrit.webScheme" -}}
{{- if and .Values.ingress.enabled .Values.ingress.tls.enabled -}}
https
{{- else if .Values.ingress.enabled -}}
http
{{- else -}}
{{- .Values.gerrit.webScheme -}}
{{- end -}}
{{- end -}}

{{- define "gerrit.listenScheme" -}}
{{- if and .Values.ingress.enabled .Values.ingress.tls.enabled -}}
proxy-https
{{- else if .Values.ingress.enabled -}}
proxy-http
{{- else -}}
{{- .Values.gerrit.listenScheme -}}
{{- end -}}
{{- end -}}

{{- define "gerrit.canonicalWebUrl" -}}
{{- printf "%s://%s%s/" (include "gerrit.webScheme" .) .Values.gerrit.host (include "gerrit.urlPrefix" .) -}}
{{- end -}}

{{- define "gerrit.listenUrl" -}}
{{- printf "%s://*:8080%s/" (include "gerrit.listenScheme" .) (include "gerrit.urlPrefix" .) -}}
{{- end -}}

{{/* The SSH port clients use: ssh.port, else a fixed node port on a
NodePort Service, else 29418. Kubernetes picks an unset node port at
random, so a NodePort Service without either cannot be advertised. */}}
{{- define "gerrit.sshPort" -}}
{{- $svc := .Values.ssh.service -}}
{{- $nodePort := and $svc.enabled (eq $svc.type "NodePort") -}}
{{- if .Values.ssh.port -}}
{{- int .Values.ssh.port -}}
{{- else if and $nodePort $svc.nodePort -}}
{{- int $svc.nodePort -}}
{{- else if $nodePort -}}
{{- fail "ssh.service.type NodePort needs ssh.service.nodePort or ssh.port, so that sshd.advertisedAddress names a port clients can reach" -}}
{{- else -}}
29418
{{- end -}}
{{- end -}}

{{- define "gerrit.sshAdvertisedAddress" -}}
{{- printf "%s:%s" (include "gerrit.sshHost" .) (include "gerrit.sshPort" .) -}}
{{- end -}}

{{- define "gerrit.tlsSecretName" -}}
{{- default (printf "%s-tls" (include "gerrit.fullname" .)) .Values.ingress.tls.secretName -}}
{{- end -}}

{{/* container.javaOptions: the options the image needs, the chart's
log4j configuration, a heap of 70% of the memory limit unless
container.heapLimit is set, then the operator's own, as a list */}}
{{- define "gerrit.javaOptions" -}}
{{- $opts := list
  "-Dflogger.backend_factory=com.google.common.flogger.backend.log4j.Log4jBackendFactory#getInstance"
  "-Dflogger.logging_context=com.google.gerrit.server.logging.LoggingContext#getInstance"
  "-Dlog4j.configuration=file:///var/gerrit/etc/log4j.properties"
  "-Djava.security.egd=file:/dev/./urandom"
  "--add-opens java.base/java.net=ALL-UNNAMED"
  "--add-opens java.base/java.lang.invoke=ALL-UNNAMED" -}}
{{- if and (not .Values.container.heapLimit) (dig "limits" "memory" "" (default dict .Values.resources)) -}}
{{- $opts = append $opts "-XX:MaxRAMPercentage=70.0" -}}
{{- end -}}
{{- toJson (concat $opts (default list .Values.container.javaOptions)) -}}
{{- end -}}

{{/* One git-config value. Whole numbers print without an exponent;
strings are quoted when git config would otherwise misread them. */}}
{{- define "gerrit.configValue" -}}
{{- if kindIs "float64" . -}}
{{- if eq . (float64 (int64 .)) }}{{ int64 . }}{{ else }}{{ . }}{{ end -}}
{{- else if kindIs "string" . -}}
{{- if regexMatch "(^\\s|\\s$|[#;\"\\\\]|\\n)" . -}}
"{{ . | replace "\\" "\\\\" | replace "\"" "\\\"" | replace "\n" "\\n" }}"
{{- else -}}
{{ . }}
{{- end -}}
{{- else -}}
{{ . }}
{{- end -}}
{{- end -}}

{{/* The key lines of one section: null and "" are left out, a list
repeats the key */}}
{{- define "gerrit.configKeys" -}}
{{- range $key, $value := . }}
{{- if kindIs "slice" $value }}
{{- range $value }}
{{ printf "\t%s = %s" $key (include "gerrit.configValue" .) }}
{{- end }}
{{- else if not (or (kindIs "invalid" $value) (and (kindIs "string" $value) (eq $value ""))) }}
{{ printf "\t%s = %s" $key (include "gerrit.configValue" $value) }}
{{- end }}
{{- end }}
{{- end -}}

{{/* A git-config section from a map: scalar and list keys under
[name], and each map-valued key as a [name "key"] subsection. Renders
nothing when no key has a value. */}}
{{- define "gerrit.configSection" -}}
{{- $name := .name -}}
{{- $plain := dict -}}
{{- $subs := dict -}}
{{- range $key, $value := (default dict .values) -}}
{{- if kindIs "map" $value -}}
{{- $_ := set $subs $key $value -}}
{{- else -}}
{{- $_ := set $plain $key $value -}}
{{- end -}}
{{- end -}}
{{- $keys := include "gerrit.configKeys" $plain -}}
{{- if $keys }}
[{{ $name }}]{{ $keys }}
{{- end }}
{{- range $sub, $values := $subs }}
{{- $keys := include "gerrit.configKeys" $values }}
{{- if $keys }}
[{{ $name }} "{{ $sub | replace "\\" "\\\\" | replace "\"" "\\\"" }}"]{{ $keys }}
{{- end }}
{{- end }}
{{- end -}}

{{/* A whole config file from a map of section name to section map */}}
{{- define "gerrit.configFile" -}}
{{- range $name, $values := . }}
{{- include "gerrit.configSection" (dict "name" $name "values" $values) }}
{{- end }}
{{- end -}}

{{/* Existing value of a Secret key, or a new random one. lookup sees
nothing when the chart is rendered offline, as Argo CD does, so GitOps
installs name pre-created Secrets in secrets.*.existingSecret. */}}
{{- define "gerrit.secretValue" -}}
{{- $existing := lookup "v1" "Secret" .ctx.Release.Namespace .name -}}
{{- if and $existing (index $existing.data .key) -}}
{{- index $existing.data .key -}}
{{- else -}}
{{- randAlphaNum 40 | b64enc -}}
{{- end -}}
{{- end -}}

{{/* Existing value of a Secret key, or an empty one, for keys the
operator fills in after install */}}
{{- define "gerrit.optionalSecretValue" -}}
{{- $existing := lookup "v1" "Secret" .ctx.Release.Namespace .name -}}
{{- if $existing -}}
{{- index (default dict $existing.data) .key | default "" -}}
{{- end -}}
{{- end -}}

{{/* Name of the Secret holding secure.config values */}}
{{- define "gerrit.secureConfigSecretName" -}}
{{- default (printf "%s-secure-config" (include "gerrit.fullname" .)) .Values.secrets.secureConfig.existingSecret -}}
{{- end -}}

{{/* Site directories the image declares as VOLUMEs. Without an
explicit mount the runtime gives each an anonymous volume seeded from
the image, hiding the site on the persistent volume. */}}
{{- define "gerrit.imageVolumeDirs" -}}
cache db etc git index
{{- end -}}

{{/* Site directories site-init creates before the kubelet mounts into
them: the image VOLUME dirs, and .ssh with replication enabled */}}
{{- define "gerrit.siteDirs" -}}
{{- include "gerrit.imageVolumeDirs" . }}{{ if .Values.replication.enabled }} .ssh{{ end }}
{{- end -}}

{{/* Site volume mounted at /var/gerrit, plus its image VOLUME dirs */}}
{{- define "gerrit.siteMounts" -}}
- name: site
  mountPath: /var/gerrit
{{- range splitList " " (include "gerrit.imageVolumeDirs" .) }}
- name: site
  mountPath: /var/gerrit/{{ . }}
  subPath: {{ . }}
{{- end }}
{{- end -}}

{{/* Default Content-Security-Policy for the Gerrit web UI: same-origin
resources, with the inline scripts and styles the UI and the SAML
auto-post page use, workers from blob: URLs as the UI's syntax
highlighting creates them, and avatars from any HTTPS origin */}}
{{- define "gerrit.contentSecurityPolicy" -}}
default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'
{{- end -}}

{{/* Ingress annotations: cert-manager's and, for ingress-nginx, those
for large git pushes and the root redirect, then ingress.annotations */}}
{{- define "gerrit.ingressAnnotations" -}}
{{- $ing := .Values.ingress -}}
{{- $ann := dict -}}
{{- if and $ing.tls.enabled $ing.tls.issuer -}}
{{- $_ := set $ann "cert-manager.io/cluster-issuer" $ing.tls.issuer -}}
{{- end -}}
{{- if eq $ing.className "nginx" -}}
{{- $_ := set $ann "nginx.ingress.kubernetes.io/proxy-body-size" "0" -}}
{{- $_ := set $ann "nginx.ingress.kubernetes.io/proxy-read-timeout" "3600" -}}
{{- $_ := set $ann "nginx.ingress.kubernetes.io/proxy-send-timeout" "3600" -}}
{{- if include "gerrit.nginxRootRedirect" . -}}
{{- $_ := set $ann "nginx.ingress.kubernetes.io/app-root" (printf "%s/" (include "gerrit.urlPrefix" .)) -}}
{{- end -}}
{{- end -}}
{{- toYaml (merge (deepCopy (default dict $ing.annotations)) $ann) -}}
{{- end -}}

{{/* "true" when ingress-nginx redirects "/" to the URL prefix. The
Ingress then also routes an Exact "/" path, so that "/" reaches a
location carrying its app-root annotation rather than the default
backend */}}
{{- define "gerrit.nginxRootRedirect" -}}
{{- $ing := .Values.ingress -}}
{{- if and (eq $ing.className "nginx") $ing.redirectRootToPrefix (include "gerrit.urlPrefix" .) -}}
true
{{- end -}}
{{- end -}}

{{/* etc/replication.config: [gerrit] autoReload, the [replication]
globals, then one [remote "<name>"] section per entry of
replication.remotes with only the keys it sets. Fails on a remote
without a name or url, or on two remotes of one name. */}}
{{- define "gerrit.replicationConfig" -}}
{{- $r := .Values.replication -}}
{{- include "gerrit.configSection" (dict "name" "gerrit" "values" (dict "autoReload" $r.autoReload)) }}
{{- include "gerrit.configSection" (dict "name" "replication" "values" (dict "lockErrorMaxRetries" $r.lockErrorMaxRetries "maxRetries" $r.maxRetries)) }}
{{- $seen := dict -}}
{{- range $i, $remote := (default list $r.remotes) }}
{{- $name := toString (default "" $remote.name) -}}
{{- if not $name }}
{{- fail (printf "replication.remotes[%d] needs a name" $i) }}
{{- end }}
{{- if not $remote.url }}
{{- fail (printf "replication remote %q needs a url" $name) }}
{{- end }}
{{- if hasKey $seen $name }}
{{- fail (printf "replication remote %q is listed twice" $name) }}
{{- end }}
{{- $_ := set $seen $name true -}}
{{- include "gerrit.configSection" (dict "name" "remote" "values" (dict $name (omit $remote "name"))) }}
{{- end }}
{{- end -}}

{{/* GitHub's published SSH host keys, from
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints */}}
{{- define "gerrit.githubKnownHosts" -}}
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
{{- end -}}

{{/* Plugin jars site-init copies from the catalogue: those
pluginCatalogue.plugins names, plus replication, hooks, its-jira,
healthcheck and metrics-reporter-prometheus when each is enabled.
its-base is compiled into its-jira.jar, so it is not a jar of its
own. */}}
{{- define "gerrit.plugins" -}}
{{- $plugins := default list .Values.pluginCatalogue.plugins -}}
{{- if .Values.replication.enabled -}}
{{- $plugins = append $plugins "replication" -}}
{{- end -}}
{{- if include "gerrit.hooksEnabled" . -}}
{{- $plugins = append $plugins "hooks" -}}
{{- end -}}
{{- if include "gerrit.itsJiraEnabled" . -}}
{{- $plugins = append $plugins "its-jira" -}}
{{- end -}}
{{- if include "gerrit.healthcheckEnabled" . -}}
{{- $plugins = append $plugins "healthcheck" -}}
{{- end -}}
{{- if include "gerrit.metricsEnabled" . -}}
{{- $plugins = append $plugins "metrics-reporter-prometheus" -}}
{{- end -}}
{{- join " " (uniq $plugins) -}}
{{- end -}}

{{/* "true" when hooks is enabled, else empty */}}
{{- define "gerrit.hooksEnabled" -}}
{{- if dig "enabled" false (default dict .Values.hooks) }}true{{ end -}}
{{- end -}}

{{/* "true" when hooks.g2p is enabled, else empty. Fails when hooks
itself is not, since gerrit-to-platform runs as a hook, and when
replication has no remote, since gerrit-to-platform finds the platform
and repositories in replication.config. */}}
{{- define "gerrit.g2pEnabled" -}}
{{- if dig "g2p" "enabled" false (default dict .Values.hooks) -}}
{{- if not (include "gerrit.hooksEnabled" .) -}}
{{- fail "hooks.g2p.enabled needs hooks.enabled" -}}
{{- end -}}
{{- if not (and .Values.replication.enabled .Values.replication.remotes) -}}
{{- fail "hooks.g2p.enabled needs replication.enabled with a remote in replication.remotes" -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

{{/* "true" when plugins.its-jira is enabled, else empty */}}
{{- define "gerrit.itsJiraEnabled" -}}
{{- if dig "its-jira" "enabled" false (default dict .Values.plugins) }}true{{ end -}}
{{- end -}}

{{/* "true" when plugins.healthcheck is enabled, else empty */}}
{{- define "gerrit.healthcheckEnabled" -}}
{{- if dig "healthcheck" "enabled" false (default dict .Values.plugins) }}true{{ end -}}
{{- end -}}

{{/* Path the readiness probe calls: the healthcheck plugin's status
with the plugin enabled, else the version endpoint */}}
{{- define "gerrit.readinessProbePath" -}}
{{- if include "gerrit.healthcheckEnabled" . -}}
{{- printf "%s/config/server/healthcheck~status" (include "gerrit.urlPrefix" .) -}}
{{- else -}}
{{- printf "%s/config/server/version" (include "gerrit.urlPrefix" .) -}}
{{- end -}}
{{- end -}}

{{/* "true" when plugins.metrics-reporter-prometheus is enabled, else
empty */}}
{{- define "gerrit.metricsEnabled" -}}
{{- if dig "metrics-reporter-prometheus" "enabled" false (default dict .Values.plugins) }}true{{ end -}}
{{- end -}}

{{/* Name of the Secret holding the metrics bearer token */}}
{{- define "gerrit.metricsSecretName" -}}
{{- default (printf "%s-metrics" (include "gerrit.fullname" .)) .Values.metrics.existingSecret -}}
{{- end -}}

{{/* "true" when the ServiceMonitor renders: metrics.serviceMonitor is
enabled and the cluster serves the Prometheus Operator's API, else
empty. Fails when the toggle is on without the metrics plugin, which
the ServiceMonitor would scrape. */}}
{{- define "gerrit.serviceMonitorEnabled" -}}
{{- if .Values.metrics.serviceMonitor.enabled -}}
{{- if not (include "gerrit.metricsEnabled" .) -}}
{{- fail "metrics.serviceMonitor.enabled needs plugins.metrics-reporter-prometheus.enabled" -}}
{{- end -}}
{{- if .Capabilities.APIVersions.Has "monitoring.coreos.com/v1" }}true{{ end -}}
{{- end -}}
{{- end -}}

{{/* etc/log4j.properties, which -Dlog4j.configuration in
container.javaOptions names. Gerrit then skips its built-in error log,
so the root logger writes JSON to the container's stderr for the
cluster's log pipeline. The request logs look up the appenders named
httpd_log and sshd_log, which keep writing text to logs/ on the site
volume, rotated daily with log.rotate. Fails on log.jsonLogging or
log.textLogging: Gerrit ignores both for its error log under this
file, but they rename the request log appenders it looks up, which
can silently disable request logging. */}}
{{- define "gerrit.log4jProperties" -}}
{{- $log := default dict .Values.log -}}
{{- range list "jsonLogging" "textLogging" -}}
{{- if hasKey $log . -}}
{{- fail (printf "log.%s is not supported: the chart configures logging through etc/log4j.properties" .) -}}
{{- end -}}
{{- end -}}
{{- $level := upper (default "INFO" .Values.logs.level) -}}
{{- if not (has $level (list "OFF" "FATAL" "ERROR" "WARN" "INFO" "DEBUG" "TRACE" "ALL")) -}}
{{- fail (printf "logs.level must be a log4j level such as INFO, not %v" .Values.logs.level) -}}
{{- end -}}
{{- $rotate := ne (toString (dig "rotate" true $log)) "false" -}}
# Rendered by the chart; site-init installs it at every start
log4j.rootLogger={{ $level }}, stderr
log4j.appender.stderr=org.apache.log4j.ConsoleAppender
log4j.appender.stderr.Target=System.err
log4j.appender.stderr.layout=com.google.gerrit.pgm.util.ErrorLogJsonLayout
{{- range $name, $layout := dict "httpd_log" "com.google.gerrit.pgm.http.jetty.HttpLogLayout" "sshd_log" "com.google.gerrit.sshd.SshLogLayout" }}

log4j.logger.{{ $name }}=INFO, {{ $name }}
log4j.additivity.{{ $name }}=false
{{- if $rotate }}
log4j.appender.{{ $name }}=org.apache.log4j.DailyRollingFileAppender
log4j.appender.{{ $name }}.DatePattern='.'yyyy-MM-dd
{{- else }}
log4j.appender.{{ $name }}=org.apache.log4j.FileAppender
{{- end }}
log4j.appender.{{ $name }}.File=/var/gerrit/logs/{{ $name }}
log4j.appender.{{ $name }}.Encoding=UTF-8
log4j.appender.{{ $name }}.layout={{ $layout }}
{{- end }}
{{- end -}}

{{/* logs.retentionDays as a whole number of days; 0 when unset.
Fails on a negative or fractional value. */}}
{{- define "gerrit.logRetentionDays" -}}
{{- $days := default 0 .Values.logs.retentionDays -}}
{{- if not (regexMatch `^[0-9]+$` (toString $days)) -}}
{{- fail (printf "logs.retentionDays must be a whole number of days, not %v" $days) -}}
{{- end -}}
{{- int64 $days -}}
{{- end -}}

{{/* hooks.path, relative to the site. site-init replaces the
directory's contents at every start, so it must not be, or lie under,
a directory Gerrit or the chart keeps anything else in. */}}
{{- define "gerrit.hooksPath" -}}
{{- $path := trimSuffix "/" (toString (default "" .Values.hooks.path)) -}}
{{- $segment := `[A-Za-z0-9_][A-Za-z0-9._-]*` -}}
{{- if or (not (regexMatch (printf `^%s(/%s)*$` $segment $segment) $path)) (regexMatch `(^|/)\.\.?(/|$)` $path) -}}
{{- fail (printf "hooks.path %q must be a relative path of plain names under the site" .Values.hooks.path) -}}
{{- end -}}
{{- $reserved := list "bin" "cache" "data" "db" "etc" "git" "index" "lib" "logs" "plugins" "static" "tmp" -}}
{{- if has (first (splitList "/" $path)) $reserved -}}
{{- fail (printf "hooks.path %q must not lie in a site directory Gerrit uses for anything else" .Values.hooks.path) -}}
{{- end -}}
{{- $path -}}
{{- end -}}

{{/* gerrit-to-platform hooks site-init links, space-separated; empty
unless hooks.g2p is enabled. Fails on a hook the tool has no entry
point for. */}}
{{- define "gerrit.g2pHooks" -}}
{{- if include "gerrit.g2pEnabled" . -}}
{{- $known := list "patchset-created" "comment-added" "change-merged" -}}
{{- $hooks := uniq (default list .Values.hooks.g2p.hooks) -}}
{{- range $hooks }}
{{- if not (has . $known) }}
{{- fail (printf "hooks.g2p.hooks: gerrit-to-platform has no %q hook; use %s" . (join ", " $known)) }}
{{- end }}
{{- end }}
{{- join " " $hooks -}}
{{- end -}}
{{- end -}}

{{/* hooks.scripts, checked: each name a hook name, and none also
listed in hooks.g2p.hooks */}}
{{- define "gerrit.hookScripts" -}}
{{- $g2p := splitList " " (include "gerrit.g2pHooks" .) -}}
{{- range $name, $_ := (default dict .Values.hooks.scripts) }}
{{- if not (regexMatch `^[a-z][a-z0-9-]*$` $name) }}
{{- fail (printf "hooks.scripts: %q is not a hook name" $name) }}
{{- end }}
{{- if has $name $g2p }}
{{- fail (printf "hooks.scripts: %q is also in hooks.g2p.hooks" $name) }}
{{- end }}
{{- end }}
{{- toJson (default dict .Values.hooks.scripts) -}}
{{- end -}}

{{/* "true" when hooks is enabled with scripts, which site-init reads
from the <fullname>-hooks ConfigMap, else empty */}}
{{- define "gerrit.hookScriptsVolume" -}}
{{- if and (include "gerrit.hooksEnabled" .) .Values.hooks.scripts }}true{{ end -}}
{{- end -}}

{{/* Name of the Secret holding gerrit_to_platform.ini */}}
{{- define "gerrit.g2pSecretName" -}}
{{- default (printf "%s-g2p" (include "gerrit.fullname" .)) .Values.hooks.g2p.existingSecret -}}
{{- end -}}

{{/* Name of the Secret holding the Jira credentials */}}
{{- define "gerrit.itsJiraSecretName" -}}
{{- default (printf "%s-its-jira" (include "gerrit.fullname" .)) (index .Values.plugins "its-jira").existingSecret -}}
{{- end -}}

{{/* plugins.its-jira.url, checked to be an http or https URL; empty
when unset */}}
{{- define "gerrit.itsJiraUrl" -}}
{{- $url := toString (default "" (index .Values.plugins "its-jira").url) -}}
{{- if and $url (not (regexMatch "^(?i)https?://[^/]" $url)) -}}
{{- fail "plugins.its-jira.url must be an http:// or https:// URL" -}}
{{- end -}}
{{- $url -}}
{{- end -}}

{{/* etc/its/actions-its-jira.config: one rule per comment toggle
that is on, then plugins.its-jira.extraConfig. A comment on every
patch set covers the first, so commentOnChangeCreated adds no rule of
its own beside commentOnPatchSetCreated. */}}
{{- define "gerrit.itsJiraActions" -}}
{{- $jira := index .Values.plugins "its-jira" -}}
{{- $rules := list -}}
{{- if and $jira.commentOnChangeCreated (not $jira.commentOnPatchSetCreated) -}}
{{- $rules = append $rules (dict "name" "change-created" "event-type" "patchset-created" "patchSetNumber" 1) -}}
{{- end -}}
{{- if $jira.commentOnPatchSetCreated -}}
{{- $rules = append $rules (dict "name" "patchset-created" "event-type" "patchset-created") -}}
{{- end -}}
{{- if $jira.commentOnChangeMerged -}}
{{- $rules = append $rules (dict "name" "change-merged" "event-type" "change-merged") -}}
{{- end -}}
{{- if $jira.commentOnChangeAbandoned -}}
{{- $rules = append $rules (dict "name" "change-abandoned" "event-type" "change-abandoned") -}}
{{- end -}}
{{- range $rules }}
[rule "comment-on-{{ .name }}"]
{{ printf "\tevent-type = %s" (index . "event-type") }}
{{- with .patchSetNumber }}
{{ printf "\tpatchSetNumber = %d" . }}
{{- end }}
{{ printf "\taction = add-standard-comment" }}
{{- end }}
{{- with $jira.extraConfig }}
{{ trimSuffix "\n" . }}
{{- end }}
{{- end -}}

{{/* TCP port of plugins.its-jira.url when the NetworkPolicy needs a
rule for it: set, enabled and not 443, which is always allowed; else
empty */}}
{{- define "gerrit.itsJiraEgressPort" -}}
{{- if include "gerrit.itsJiraEnabled" . -}}
{{- $port := include "gerrit.urlPort" (include "gerrit.itsJiraUrl" .) | int -}}
{{- if and $port (ne $port 443) }}{{ $port }}{{ end -}}
{{- end -}}
{{- end -}}

{{/* Library names site-init copies from the image catalogue:
pluginCatalogue.libs, plus saml when auth.saml is enabled */}}
{{- define "gerrit.libs" -}}
{{- $libs := default list .Values.pluginCatalogue.libs -}}
{{- if include "gerrit.samlEnabled" . -}}
{{- $libs = append $libs "saml" -}}
{{- end -}}
{{- join " " (uniq $libs) -}}
{{- end -}}

{{/* "true" when auth.saml is enabled, else empty */}}
{{- define "gerrit.samlEnabled" -}}
{{- if dig "saml" "enabled" false (default dict .Values.auth) }}true{{ end -}}
{{- end -}}

{{/* "true" when auth.allowDevelopmentAccounts is boolean true, else
empty. Fails on any value but a boolean or null, so a string such as
"false" cannot pass for the opt-in. */}}
{{- define "gerrit.developmentAccountsAllowed" -}}
{{- $allow := dig "allowDevelopmentAccounts" false (default dict .Values.auth) -}}
{{- if not (or (kindIs "bool" $allow) (kindIs "invalid" $allow)) -}}
{{- fail (printf "auth.allowDevelopmentAccounts must be a boolean, true or false, not %s %q; with --set-string or a quoted value it is a string" (kindOf $allow) (toString $allow)) -}}
{{- end -}}
{{- if eq $allow true }}true{{ end -}}
{{- end -}}

{{/* auth.type from values, upper-cased with spaces and hyphens as
underscores, the way Gerrit reads an enum; empty when unset. Fails on
any value but a string or null: gerrit.config would render each item
of a list as its own type key. */}}
{{- define "gerrit.authTypeValue" -}}
{{- $type := dig "type" "" (default dict .Values.auth) -}}
{{- if not (or (kindIs "string" $type) (kindIs "invalid" $type)) -}}
{{- fail (printf "auth.type must be a string naming one Gerrit auth type, not %s" (kindOf $type)) -}}
{{- end -}}
{{- default "" $type | trim | upper | replace " " "_" | replace "-" "_" -}}
{{- end -}}

{{/* The [auth] type the chart renders: HTTP with SAML, else auth.type.
Fails the render unless values choose authentication on purpose: no
auth.type without SAML, DEVELOPMENT_BECOME_ANY_ACCOUNT without
auth.allowDevelopmentAccounts, or that opt-in together with SAML. */}}
{{- define "gerrit.authType" -}}
{{- $type := include "gerrit.authTypeValue" . -}}
{{- $development := include "gerrit.developmentAccountsAllowed" . -}}
{{- if include "gerrit.samlEnabled" . -}}
{{- if $development -}}
{{- fail "auth.allowDevelopmentAccounts cannot be combined with auth.saml.enabled: SAML signs users in through the identity provider; remove one of them" -}}
{{- end -}}
HTTP
{{- else if not $type -}}
{{- fail "choose authentication: set auth.saml.enabled: true for SAML, auth.type to another Gerrit auth type, or auth.type: DEVELOPMENT_BECOME_ANY_ACCOUNT with auth.allowDevelopmentAccounts: true for CI and sandboxes; see Authentication in the chart README" -}}
{{- else if and (eq $type "DEVELOPMENT_BECOME_ANY_ACCOUNT") (not $development) -}}
{{- fail "auth.type DEVELOPMENT_BECOME_ANY_ACCOUNT lets anyone sign in as any account, the administrator included; set auth.allowDevelopmentAccounts: true to use it on purpose, and never in production" -}}
{{- else -}}
{{- $type -}}
{{- end -}}
{{- end -}}

{{/* "true" when the render enables DEVELOPMENT_BECOME_ANY_ACCOUNT */}}
{{- define "gerrit.developmentAccounts" -}}
{{- if eq (include "gerrit.authType" .) "DEVELOPMENT_BECOME_ANY_ACCOUNT" }}true{{ end -}}
{{- end -}}

{{/* Name of the Secret holding the SAML keystore and its passwords */}}
{{- define "gerrit.samlSecretName" -}}
{{- default (printf "%s-saml" (include "gerrit.fullname" .)) .Values.auth.saml.existingSecret -}}
{{- end -}}

{{/* The [saml] section of gerrit.config as JSON. The keystore and
the identity provider's metadata file are mounted from the Secret into
etc/; the passwords go to secure.config. Fails unless exactly one
metadata source is set. */}}
{{- define "gerrit.samlConfig" -}}
{{- $saml := .Values.auth.saml -}}
{{- $url := toString (default "" $saml.metadataUrl) -}}
{{- if and $url $saml.metadataFromSecret -}}
{{- fail "auth.saml: set metadataUrl or metadataFromSecret, not both" -}}
{{- end -}}
{{- if not (or $url $saml.metadataFromSecret) -}}
{{- fail "auth.saml: set metadataUrl or metadataFromSecret" -}}
{{- end -}}
{{- if and $url (not (regexMatch "^(?i)https?://[^/]" $url)) -}}
{{- fail "auth.saml.metadataUrl must be an http:// or https:// URL" -}}
{{- end -}}
{{- $values := omit $saml "enabled" "existingSecret" "metadataUrl" "metadataFromSecret" "serviceProviderEntityId" -}}
{{- $_ := set $values "keystorePath" "etc/samlKeystore.jks" -}}
{{- $_ := set $values "metadataPath" (ternary "etc/saml-idp-metadata.xml" $url (empty $url)) -}}
{{- $_ := set $values "serviceProviderEntityId" (default (include "gerrit.canonicalWebUrl" .) $saml.serviceProviderEntityId) -}}
{{- toJson $values -}}
{{- end -}}

{{/* Scans gerrit.extraConfig, taken as the context, for the
unqualified [auth] section. Returns a JSON object: "header" is true
when any line, after leading whitespace, starts with an [auth] header
in any case, even a line the scan takes for a continued value; "keys"
lists the active entries in that section, lower-cased. Read line by
line as git config: an entry on the line of its section header
counts, although Gerrit ignores the rest of a header line, and never
starts a continued value. Comments, named subsections such as
[auth "x"], and values, quoted or continued with a trailing
backslash, do not count. A carriage return or Unicode whitespace,
such as U+2003, counts as a space or tab. extraConfig follows a
section the chart renders, never [auth], so entries before its first
header do not count. The scan is best effort, which is why callers
guarding authentication refuse on "header" rather than on "keys". */}}
{{- define "gerrit.extraConfigAuth" -}}
{{- $name := `^[A-Za-z][A-Za-z0-9-]*` -}}
{{- $ws := `[\t\v\f\r\p{Z}\x{1C}-\x{1F}]` -}}
{{- $header := printf `^\[%s*([A-Za-z0-9.-]+)%s*("(?:[^"\\]|\\.)*")?%s*\]` $ws $ws $ws -}}
{{- $quoted := `(?:[^"\\]|\\.)*` -}}
{{- $plain := printf `(?:[^"\\#;]|\\.|"%s")*` $quoted -}}
{{- $section := "" -}}
{{- $keys := list -}}
{{- $authHeader := false -}}
{{- $continued := "" -}}
{{- range splitList "\n" (toString (default "" .)) }}
{{- $text := trimSuffix "\r" . -}}
{{- $value := "" -}}
{{- $inValue := false -}}
{{- $onHeader := false -}}
{{- $line := regexReplaceAll (printf `^%s+` $ws) $text "" -}}
{{- $lineHeader := regexFind $header $line -}}
{{- if and $lineHeader (eq (lower (regexReplaceAll (printf `%s.*$` $header) $line "${1}")) "auth") (not (regexReplaceAll (printf `%s.*$` $header) $line "${2}")) }}
{{- $authHeader = true -}}
{{- end }}
{{- if $continued }}
{{- $value = $text -}}
{{- $inValue = true -}}
{{- else }}
{{- $text = regexReplaceAll (printf `^%s+` $ws) $text "" -}}
{{- $found := regexFind $header $text -}}
{{- if $found }}
{{- $section = lower (regexReplaceAll (printf `%s.*$` $header) $text "${1}") -}}
{{- if regexReplaceAll (printf `%s.*$` $header) $text "${2}" }}
{{- $section = "" -}}
{{- end }}
{{- $text = regexReplaceAll (printf `^%s+` $ws) (trimPrefix $found $text) "" -}}
{{- $onHeader = true -}}
{{- end }}
{{- if regexMatch (printf `%s%s*([=#;]|$)` $name $ws) $text }}
{{- if eq $section "auth" }}
{{- $keys = append $keys (lower (regexFind $name $text)) -}}
{{- end }}
{{- if and (not $onHeader) (regexMatch (printf `%s%s*=` $name $ws) $text) }}
{{- $value = regexReplaceAll (printf `%s%s*=` $name $ws) $text "" -}}
{{- $inValue = true -}}
{{- end }}
{{- end }}
{{- end }}
{{- /* A value continues on the next line when it ends in an unescaped
backslash outside a comment, inside quotes or not */}}
{{- $open := ternary (printf `%s"` $quoted) "" (eq $continued "quoted") -}}
{{- if not $inValue }}
{{- $continued = "" -}}
{{- else if and (eq $continued "quoted") (regexMatch (printf `^%s\\$` $quoted) $value) }}
{{- $continued = "quoted" -}}
{{- else if regexMatch (printf `^%s%s\\$` $open $plain) $value }}
{{- $continued = "plain" -}}
{{- else if regexMatch (printf `^%s%s"%s\\$` $open $plain $quoted) $value }}
{{- $continued = "quoted" -}}
{{- else }}
{{- $continued = "" -}}
{{- end }}
{{- end }}
{{- toJson (dict "header" $authHeader "keys" (uniq $keys)) -}}
{{- end -}}

{{/* Fails the render when values set auth.httpDisplaynameHeader, in
auth or gerrit.extraConfig: the SAML filter refuses to start with it.
Git config keys ignore case. An auth value of null, "" or an empty
list renders no key, as gerrit.configKeys does, so it passes.
gerrit.extraConfigAuth reads extraConfig. The check is best
effort: the SAML filter itself refuses to start when the header is
set anywhere. */}}
{{- define "gerrit.samlRefuseDisplayNameHeader" -}}
{{- $message := "auth.httpDisplaynameHeader is not compatible with SAML; remove it from auth and gerrit.extraConfig" -}}
{{- range $key, $value := (default dict .Values.auth) }}
{{- if eq (lower $key) "httpdisplaynameheader" }}
{{- if kindIs "slice" $value }}
{{- if $value }}{{ fail $message }}{{ end -}}
{{- else if not (or (kindIs "invalid" $value) (and (kindIs "string" $value) (eq $value ""))) }}
{{- fail $message -}}
{{- end }}
{{- end }}
{{- end }}
{{- if has "httpdisplaynameheader" (include "gerrit.extraConfigAuth" (toString (default "" .Values.gerrit.extraConfig)) | fromJson).keys }}
{{- fail $message -}}
{{- end }}
{{- end -}}

{{/* Fails the render when gerrit.extraConfig carries an [auth]
section, whatever its keys and the typed choice. Gerrit reads the last
value of a repeated key, so a type entry there would replace the type
the chart renders, development accounts included, without their
opt-in. Every [auth] setting is reached through auth values, so the
check refuses the section header itself rather than trusting the scan
of its keys to match Gerrit's parser; see gerrit.extraConfigAuth. */}}
{{- define "gerrit.refuseExtraConfigAuth" -}}
{{- if (include "gerrit.extraConfigAuth" (toString (default "" .Values.gerrit.extraConfig)) | fromJson).header }}
{{- fail "gerrit.extraConfig cannot carry an [auth] section: every auth setting is set through auth values, and Gerrit reads the last value of a repeated key, so an [auth] entry there would replace the one values render, auth.type included; set auth.type, or auth.saml.enabled: true for SAML, and auth.allowDevelopmentAccounts: true for DEVELOPMENT_BECOME_ANY_ACCOUNT" -}}
{{- end }}
{{- end -}}

{{/* Fails the render when values set auth.cookieSecure, in any case:
Gerrit 3.13 removed it and marks the session cookie secure itself on
HTTPS requests, so the key would be silently ignored. null, "" or an
empty list renders no key, as gerrit.configKeys does, so it passes. */}}
{{- define "gerrit.refuseCookieSecure" -}}
{{- range $key, $value := (default dict .Values.auth) }}
{{- if and (eq (lower $key) "cookiesecure") (not (or (kindIs "invalid" $value) (and (kindIs "string" $value) (eq $value "")) (and (kindIs "slice" $value) (empty $value)))) }}
{{- fail "auth.cookieSecure was removed in Gerrit 3.13, which marks the session cookie secure itself on requests that arrive over HTTPS; remove it from auth" -}}
{{- end }}
{{- end }}
{{- end -}}

{{/* TCP port of auth.saml.metadataUrl when the NetworkPolicy needs a
rule for it: set, enabled and not 443, which is always allowed; else
empty */}}
{{- define "gerrit.samlEgressPort" -}}
{{- if include "gerrit.samlEnabled" . -}}
{{- $port := include "gerrit.urlPort" .Values.auth.saml.metadataUrl | int -}}
{{- if and $port (ne $port 443) }}{{ $port }}{{ end -}}
{{- end -}}
{{- end -}}

{{/* TCP port a URL reaches: the port it names, or else its scheme's
default: 22 for SSH and the scp-like user@host:path form, 80 for http
and gerrit+http, 443 for https, and 9418 for git; 0 when unknown */}}
{{- define "gerrit.urlPort" -}}
{{- $url := toString (default "" .) -}}
{{- $port := 0 -}}
{{- if regexMatch "^[A-Za-z][A-Za-z0-9+.-]*://" $url }}
{{- $scheme := lower (regexReplaceAll "^([^:]+)://.*$" $url "${1}") -}}
{{- $authority := regexReplaceAll "^.*@" (regexReplaceAll "^[^:]+://([^/?#]*).*$" $url "${1}") "" -}}
{{- if regexMatch ":[0-9]+$" $authority }}
{{- $port = regexReplaceAll "^.*:([0-9]+)$" $authority "${1}" | int -}}
{{- else if has $scheme (list "ssh" "git+ssh" "ssh+git" "gerrit+ssh") }}
{{- $port = 22 -}}
{{- else if has $scheme (list "http" "gerrit+http") }}
{{- $port = 80 -}}
{{- else if eq $scheme "https" }}
{{- $port = 443 -}}
{{- else if eq $scheme "git" }}
{{- $port = 9418 -}}
{{- end }}
{{- else if regexMatch "^[^/]+:" $url }}
{{- $port = 22 -}}
{{- end }}
{{- $port -}}
{{- end -}}

{{/* TCP ports the replication remotes' url and adminUrl reach, other
than 443, which the NetworkPolicy always allows, as a JSON list. Each
key may hold one URL or a list of them; see gerrit.urlPort. */}}
{{- define "gerrit.replicationEgressPorts" -}}
{{- $ports := list -}}
{{- range (default list .Values.replication.remotes) }}
{{- $urls := list -}}
{{- range $value := list .url .adminUrl }}
{{- if kindIs "slice" $value }}
{{- $urls = concat $urls $value -}}
{{- else }}
{{- $urls = append $urls $value -}}
{{- end }}
{{- end }}
{{- range $url := $urls }}
{{- $port := include "gerrit.urlPort" $url | int -}}
{{- if and $port (ne $port 443) }}
{{- $ports = append $ports $port -}}
{{- end }}
{{- end }}
{{- end }}
{{- toJson (uniq $ports) -}}
{{- end -}}

{{/* Environment site-init needs to install the binaries, jars and
rendered configuration; site-import passes it on when it runs
site-init.sh to prepare an imported site */}}
{{- define "gerrit.sitePrepareEnv" -}}
- name: GERRIT_SITE
  value: /mnt/site
- name: GERRIT_SITE_DIRS
  value: {{ include "gerrit.siteDirs" . | quote }}
- name: GERRIT_RENDERED_CONFIG
  value: /config
- name: GERRIT_CATALOGUE
  value: {{ .Values.pluginCatalogue.path | quote }}
- name: GERRIT_PLUGINS
  value: {{ include "gerrit.plugins" . | quote }}
- name: GERRIT_LIBS
  value: {{ include "gerrit.libs" . | quote }}
{{- end -}}

{{/* The secure.config names render-config can write, space separated;
site-import reports an imported name missing from this list */}}
{{- define "gerrit.secureConfigKeys" -}}
{{- $keys := list "auth.registerEmailPrivateKey" "sendemail.smtpPass" -}}
{{- if include "gerrit.samlEnabled" . -}}
{{- $keys = concat $keys (list "saml.keystorePassword" "saml.privateKeyPassword") -}}
{{- end -}}
{{- if include "gerrit.itsJiraEnabled" . -}}
{{- $keys = concat $keys (list "its-jira.username" "its-jira.password") -}}
{{- end -}}
{{- if include "gerrit.metricsEnabled" . -}}
{{- $keys = append $keys "plugin.metrics-reporter-prometheus.prometheusBearerToken" -}}
{{- end -}}
{{- join " " $keys -}}
{{- end -}}

{{/* Checks siteImport and prints its source type. The mode and token
are checked again by site-import, which refuses before it changes the
volume; encrypted archives are not supported yet. */}}
{{- define "gerrit.siteImportSource" -}}
{{- $si := .Values.siteImport -}}
{{- if not (has $si.mode (list "auto" "force" "skip")) -}}
{{- fail (printf "siteImport.mode must be auto, force or skip, not %q" (toString $si.mode)) -}}
{{- end -}}
{{- $type := $si.source.type -}}
{{- if not (has $type (list "none" "pvc" "https" "s3")) -}}
{{- fail (printf "siteImport.source.type must be none, pvc, https or s3, not %q" (toString $type)) -}}
{{- end -}}
{{- if and (eq $type "pvc") (or (not $si.source.volume) (not $si.source.url)) -}}
{{- fail "siteImport.source.type pvc needs siteImport.source.volume and a siteImport.source.url under /mnt/import" -}}
{{- end -}}
{{- if and (eq $type "pvc") (not (hasPrefix "/mnt/import" $si.source.url)) -}}
{{- fail "siteImport.source.url must be a directory under /mnt/import, where siteImport.source.volume is mounted" -}}
{{- end -}}
{{- if and (eq $type "https") (not $si.source.url) (not $si.source.existingSecret) -}}
{{- fail "siteImport.source.type https needs siteImport.source.url or siteImport.source.existingSecret" -}}
{{- end -}}
{{- if eq $type "s3" -}}
{{- $s3 := default dict $si.source.s3 -}}
{{- if or (not (regexMatch "^s3://[^/]+(/.*)?$" (toString $si.source.url))) (has ".." (splitList "/" (toString $si.source.url))) -}}
{{- fail "siteImport.source.type s3 needs siteImport.source.url s3://<bucket>/<prefix>" -}}
{{- end -}}
{{- with $s3.endpoint -}}
{{- $port := include "gerrit.urlPort" (toString .) | int64 -}}
{{- if or (not (regexMatch `^(?i:https?)://([^\s/?#@:\[\]]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?$` (toString .))) (lt $port 1) (gt $port 65535) -}}
{{- fail "siteImport.source.s3.endpoint must be an http:// or https:// URL with a host and an optional port, and no user, path, query or fragment" -}}
{{- end -}}
{{- end -}}
{{- if not $s3.provider -}}
{{- fail "siteImport.source.s3.provider must name an rclone S3 provider" -}}
{{- end -}}
{{- end -}}
{{- $_ := include "gerrit.siteImportEgressPorts" . -}}
{{- $type -}}
{{- end -}}

{{/* TCP ports an https or s3 import source reaches, other than 443,
which the NetworkPolicy always allows, as a JSON list. https: the port
of siteImport.source.url, and siteImport.source.egressPorts for URLs
the Secret holds; fails on an egressPorts entry that is not a port.
s3: the port of siteImport.source.s3.endpoint. */}}
{{- define "gerrit.siteImportEgressPorts" -}}
{{- $ports := list -}}
{{- $source := .Values.siteImport.source -}}
{{- if eq (toString $source.type) "https" -}}
{{- $port := include "gerrit.urlPort" $source.url | int64 -}}
{{- if and $port (ne $port 443) }}
{{- $ports = append $ports $port -}}
{{- end }}
{{- range (default list $source.egressPorts) }}
{{- if not (and (regexMatch `^[0-9]+$` (toString .)) (le (int64 .) 65535) (ge (int64 .) 1)) }}
{{- fail (printf "siteImport.source.egressPorts must hold TCP port numbers, not %v" .) }}
{{- end }}
{{- if ne (int64 .) 443 }}
{{- $ports = append $ports (int64 .) -}}
{{- end }}
{{- end }}
{{- else if eq (toString $source.type) "s3" -}}
{{- $port := include "gerrit.urlPort" (dig "endpoint" "" (default dict $source.s3)) | int64 -}}
{{- if and $port (ne $port 443) }}
{{- $ports = append $ports $port -}}
{{- end }}
{{- end -}}
{{- toJson (uniq $ports) -}}
{{- end -}}

{{/* The Gerrit pod's ServiceAccount: serviceAccount.name, else the
full name when the chart creates one; "" for the namespace default */}}
{{- define "gerrit.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "gerrit.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* site-import's resources: siteImport.resources, else resources */}}
{{- define "gerrit.siteImportResources" -}}
{{- toYaml (.Values.siteImport.resources | default .Values.resources) -}}
{{- end -}}

{{/* The heap site-import gives init and reindex, as gerrit.javaOptions
gives Gerrit: container.heapLimit when set, else 70% of site-import's
memory limit, else the JVM's default */}}
{{- define "gerrit.siteImportHeap" -}}
{{- if .Values.container.heapLimit -}}
-Xmx{{ .Values.container.heapLimit }}
{{- else if dig "limits" "memory" "" (include "gerrit.siteImportResources" . | fromYaml) -}}
-XX:MaxRAMPercentage=70.0
{{- end -}}
{{- end -}}
