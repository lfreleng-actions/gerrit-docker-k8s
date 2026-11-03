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
{{- /* Fails the render unless values choose authentication */}}
{{- $_ := include "gerrit.authType" . -}}
{{- include "gerrit.refuseCookieSecure" . -}}
{{- $sections := list "gerrit" "httpd" "sshd" "auth" "sendemail" "container" "cache" "change" "receive" "download" "index" "gc" "plugins" "log" "user" "trackingid" "accounts" "suggest" -}}
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
{{- /* Fails the render when gerrit.extraConfig carries an [auth]
section, which could override the choice above. After the SAML check,
so a display name header there gets its own message. */}}
{{- include "gerrit.refuseExtraConfigAuth" . -}}
{{- if include "gerrit.hooksEnabled" . }}
{{- $_ := set $derived "hooks" (dict
  "path" (printf "/var/gerrit/%s" (include "gerrit.hooksPath" .))
  "syncHookTimeout" $v.hooks.syncHookTimeout) -}}
{{- $sections = append $sections "hooks" -}}
{{- end }}
{{- if include "gerrit.itsJiraEnabled" . }}
{{- $jira := index $v.plugins "its-jira" -}}
{{- $association := toString (default "" $jira.association) -}}
{{- if and $association (not (has $association (list "MANDATORY" "SUGGESTED" "OPTIONAL"))) }}
{{- fail "plugins.its-jira.association must be MANDATORY, SUGGESTED or OPTIONAL" -}}
{{- end }}
{{- $_ := set $derived "its-jira" (dict "url" (include "gerrit.itsJiraUrl" .)) -}}
{{- $_ := set $derived "plugin" (dict "its-jira" (dict "association" $association)) -}}
{{- $sections = concat $sections (list "its-jira" "plugin") -}}
{{- end }}
{{- range $sections }}
{{- $values := deepCopy (default dict (index $v .)) -}}
{{- if has . (list "gerrit" "saml" "hooks" "its-jira" "plugin") }}{{ $values = dict }}{{ end -}}
{{- if eq . "auth" }}{{ $values = omit $values "saml" "allowDevelopmentAccounts" }}{{ end -}}
{{- if eq . "plugins" }}{{ $values = omit $values "its-jira" "healthcheck" "metrics-reporter-prometheus" }}{{ end -}}
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
