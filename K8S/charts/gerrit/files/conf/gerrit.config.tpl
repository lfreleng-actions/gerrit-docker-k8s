{{- /*
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation

etc/gerrit.config, rendered with tpl into the <fullname>-config
ConfigMap. Keys the chart derives win over the same key in values.
*/ -}}
{{- $v := .Values -}}
{{- $derived := dict
  "gerrit" (dict
    "basePath" "git"
    "canonicalWebUrl" (include "gerrit.canonicalWebUrl" .)
    "instanceId" $v.gerrit.instanceId
    "serverId" $v.gerrit.serverId)
  "httpd" (dict "listenUrl" (include "gerrit.listenUrl" .))
  "sshd" (dict "advertisedAddress" (include "gerrit.sshAdvertisedAddress" .))
  "cache" (dict "directory" "cache")
  "container" (dict "javaOptions" (include "gerrit.javaOptions" . | fromJsonArray)) -}}
{{- $sections := list "gerrit" "httpd" "sshd" "auth" "sendemail" "container" "cache" "change" "receive" "download" "index" "gc" "plugins" "user" "trackingid" "accounts" "suggest" -}}
{{- if include "gerrit.samlEnabled" . }}
{{- /* The SAML filter authenticates each request and hands Gerrit the
account in the headers its HTTP authentication reads. The filter needs
the module's bindings, and refuses to start with a display name
header set. */}}
{{- include "gerrit.samlRefuseDisplayNameHeader" . -}}
{{- $_ := set $derived.gerrit "installModule" "com.googlesource.gerrit.plugins.saml.Module" -}}
{{- $_ := set $derived.httpd "filterClass" "com.googlesource.gerrit.plugins.saml.SamlWebFilter" -}}
{{- $_ := set $derived "auth" (dict
  "type" "HTTP"
  "httpHeader" "X-SAML-UserName"
  "httpEmailHeader" "X-SAML-EmailHeader"
  "httpExternalIdHeader" "X-SAML-ExternalId") -}}
{{- $_ := set $derived "saml" (include "gerrit.samlConfig" . | fromJson) -}}
{{- $sections = append $sections "saml" -}}
{{- end }}
{{- range $sections }}
{{- $values := deepCopy (default dict (index $v .)) -}}
{{- if or (eq . "gerrit") (eq . "saml") }}{{ $values = dict }}{{ end -}}
{{- if eq . "auth" }}{{ $values = omit $values "saml" }}{{ end -}}
{{- if eq . "gc" }}
{{- $values = omit $values "cronJob" -}}
{{- if dig "cronJob" "enabled" false $v.gc }}
{{- $values = omit $values "startTime" "interval" -}}
{{- end }}
{{- end -}}
{{- if eq . "cache" }}
{{- /* cache.limits fills diskLimit into each named subsection that
does not set its own */}}
{{- $limits := default dict $values.limits -}}
{{- $values = omit $values "limits" -}}
{{- range $name, $size := $limits }}
{{- $sub := deepCopy (default dict (index $values $name)) }}
{{- if not (or (hasKey $sub "diskLimit") (kindIs "invalid" $size) (eq (toString $size) "")) }}
{{- $_ := set $values $name (set $sub "diskLimit" $size) }}
{{- end }}
{{- end }}
{{- end -}}
{{- $values = merge (deepCopy (default dict (index $derived .))) $values -}}
{{- include "gerrit.configSection" (dict "name" . "values" $values) }}
{{- end }}
{{- with $v.gerrit.extraConfig }}
{{ trimSuffix "\n" . }}
{{- end }}
