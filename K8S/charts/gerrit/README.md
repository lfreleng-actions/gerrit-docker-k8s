<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Gerrit Helm chart

Runs one Gerrit primary per release as a single-replica `StatefulSet`
with its site on a persistent volume. Gerrit's configuration comes
from chart values: Helm renders `etc/gerrit.config`, and
`etc/secure.config` is written at pod start from Secrets, so the site
volume holds state only.

## What it deploys

| Resource                          | Purpose                                                        |
| --------------------------------- | -------------------------------------------------------------- |
| `StatefulSet` `<fullname>`        | Gerrit; site PVC `site` mounted at `/var/gerrit`               |
| `Service` `<fullname>-http`       | ClusterIP, port 8080                                           |
| `Service` `<fullname>-ssh`        | Port 29418, type `LoadBalancer`, `externalTrafficPolicy: Local`; with `ssh.service.enabled` (the default) |
| `Service` `<fullname>-headless`   | Governing service of the `StatefulSet`                         |
| `Ingress` `<fullname>`            | With `ingress.enabled`; see [Exposing Gerrit](#exposing-gerrit) |
| `IngressRoute` `<fullname>`, `Middleware`s, `Certificate` `<fullname>-tls` | Instead of the `Ingress`, with `ingress.traefik.enabled` |
| `ConfigMap` `<fullname>-scripts`  | `files/scripts/*.sh` and the `site-import` Python modules      |
| `ConfigMap` `<fullname>-config`   | Rendered `gerrit.config`, `jgit.config` and `notedb.config`, `replication.config` with `replication.enabled`, and `healthcheck.config` with `plugins.healthcheck.enabled` |
| `ConfigMap` `<fullname>-hooks`    | `hooks.scripts`; with `hooks.enabled`, see [Hooks](#hooks)     |
| `ConfigMap` `<fullname>-known-hosts` | SSH host keys of the replication remotes; with `replication.enabled`, see [Replication](#replication) |
| `Secret` `<fullname>-admin`       | Admin credential placeholder; not consumed yet                 |
| `Secret` `<fullname>-secure-config` | Values `render-config.sh` writes to `etc/secure.config`; see the Secret contract |
| `Secret` `<fullname>-saml`        | SAML keystore, its passwords and the IdP metadata, created empty; with `auth.saml.enabled` and no `auth.saml.existingSecret`, see [SAML](#saml) |
| `Secret` `<fullname>-g2p`         | `gerrit_to_platform.ini`, created empty; with `hooks.g2p.enabled` and no `hooks.g2p.existingSecret`, see [Hooks](#hooks) |
| `Secret` `<fullname>-its-jira`    | Jira `username` and `password`, created empty; with `plugins.its-jira.enabled` and no `plugins.its-jira.existingSecret`, see [Issue tracker](#issue-tracker) |
| `Secret` `<fullname>-metrics`     | Metrics bearer `token`, generated; with `plugins.metrics-reporter-prometheus.enabled` and no `metrics.existingSecret`, see [Metrics](#metrics) |
| `ServiceMonitor` `<fullname>`     | With `metrics.serviceMonitor.enabled` where the cluster serves `monitoring.coreos.com/v1`; see [Metrics](#metrics) |
| `CronJob` `<fullname>-git-gc`, `NetworkPolicy` `<fullname>-git-gc-deny` | With `gc.cronJob.enabled`; see [Garbage collection](#garbage-collection) |
| `CronJob` `<fullname>-log-cleanup`, `NetworkPolicy` `<fullname>-log-cleanup-deny` | With `logs.retentionDays` above `0`; see [Logs](#logs) |
| `Deployment`, `ConfigMap` and `NetworkPolicy` `<fullname>-toolbox` | With `toolbox.enabled`, at zero replicas; see [Admin toolbox](#admin-toolbox) |
| `NetworkPolicy` (two)             | Default deny; allow ingress to 8080 (from `networkPolicy.ingressNamespaceSelector` when set, and then also from `networkPolicy.metricsNamespaceSelector` with the metrics plugin) and 29418, egress to DNS, HTTPS and, with mail configured, the SMTP relay's port, with replication enabled, each remote's port, with `auth.saml.metadataUrl`, its port, with `plugins.its-jira.enabled`, the Jira URL's port, and with an `https` `siteImport.source`, its URL's port and `siteImport.source.egressPorts` |

The Secrets carry `helm.sh/resource-policy: keep` and
`argocd.argoproj.io/sync-options: Prune=false,Delete=false`, so they
survive `helm uninstall` and Argo CD pruning. A Helm upgrade reuses
the values already in the cluster rather than generating new ones.

That reuse depends on Helm's `lookup`, which returns nothing when a
chart renders offline. Argo CD renders that way, so each sync would
generate and apply new values, and a new `registerEmailPrivateKey`
invalidates outstanding email verification links. Under GitOps, create
the Secrets outside the chart, for example with an external secrets
operator or a sealed Secret, holding the keys the table below names,
and set `secrets.admin.existingSecret` and
`secrets.secureConfig.existingSecret` to their names. The chart then
renders neither Secret, and `render-config` reads the named
secure-config Secret.

### Secret contract

| Value                                 | Key                       | Required | Written to                                    |
| ------------------------------------- | ------------------------- | -------- | --------------------------------------------- |
| `secrets.admin.existingSecret`        | `username`, `password`    | Yes      | Not consumed yet                              |
| `secrets.secureConfig.existingSecret` | `registerEmailPrivateKey` | Yes      | `auth.registerEmailPrivateKey` in `secure.config` |
| `secrets.secureConfig.existingSecret` | `smtpPass`                | No       | `sendemail.smtpPass` in `secure.config`       |
| `replication.sshKeySecret`            | `id_ed25519` or `id_rsa`  | For SSH remotes | Mounted in `/var/gerrit/.ssh` with mode `0400` |
| `replication.sshKeySecret`            | `known_hosts`             | No       | Mounted in `/var/gerrit/.ssh` with mode `0400` |
| `auth.saml.existingSecret`            | `samlKeystore.jks`        | With SAML | Mounted read-only at `etc/samlKeystore.jks` |
| `auth.saml.existingSecret`            | `keystore-password`, `private-key-password` | With SAML | `saml.keystorePassword` and `saml.privateKeyPassword` in `secure.config` |
| `auth.saml.existingSecret`            | `idp-metadata.xml`        | With `metadataFromSecret` | Mounted read-only at `etc/saml-idp-metadata.xml` |
| `hooks.g2p.existingSecret`            | `gerrit_to_platform.ini`  | With g2p | `$XDG_CONFIG_HOME/gerrit_to_platform/` on the site with mode `0600` |
| `plugins.its-jira.existingSecret`     | `username`, `password`    | With its-jira | `its-jira.username` and `its-jira.password` in `secure.config` |
| `metrics.existingSecret`              | `token`                   | With the metrics plugin | `plugin.metrics-reporter-prometheus.prometheusBearerToken` in `secure.config`, and the `ServiceMonitor`'s bearer token |
| `toolbox.existingSecret`              | `ssh-privatekey`          | No       | Mounted in the admin toolbox at `/etc/gerrit-toolbox/ssh/id` with mode `0400` |

The generated `<fullname>-secure-config` Secret carries a random
`registerEmailPrivateKey` and an empty `smtpPass`. Fill `smtpPass` in
the cluster when the mail relay needs a password; upgrades keep it. An
empty or absent optional key leaves its entry out of `secure.config`.

## Start-up sequence

1. `volume-permissions` (only with
   `persistence.volumePermissions.enabled`) chowns anything on the site
   volume the `gerrit` user (UID 1001) does not own.
2. `site-import` records a fresh site, or adopts a site exported with
   `gerrit-site-export.py`, before anything else writes to the volume;
   see [Site import](#site-import). Once the site's state is
   `complete` it does nothing.
3. `site-init` copies `gerrit.war` and `gerrit.sh` from the image,
   replaces everything in the site's `plugins/` and `lib/`, hidden
   files too, with the jars named in
   `pluginCatalogue`, plus `replication` with `replication.enabled`
   and the `saml` library with `auth.saml.enabled`, and copies the rendered `gerrit.config`, `jgit.config`,
   `notedb.config`, `log4j.properties` and `replication.config` over the site's own, and with
   `plugins.its-jira.enabled`, `etc/its/actions-its-jira.config`, and
   with `plugins.healthcheck.enabled`, `etc/healthcheck.config`. With
   replication disabled it removes the site's `etc/replication.config`. On a site
   without the repository `gerrit.allProjects` names (`All-Projects`
   unless `gerrit.extraConfig` sets it) it then runs
   `gerrit init --batch --no-auto-start` on that configuration and
   installs the rendered files again over whatever init added. With
   `hooks.enabled` it then fills the hooks directory, and with
   `hooks.g2p.enabled` installs `gerrit_to_platform.ini`; see
   [Hooks](#hooks).
4. `render-config` rewrites `etc/secure.config` from the Secrets,
   creating it with mode `0600` before writing any value.
5. `gerrit` runs `bin/gerrit.sh run`. A startup probe allows ten
   minutes for `<urlPrefix>/config/server/version` to answer, since
   the daemon loads its caches and indexes before it serves. The
   readiness probe then calls
   `<urlPrefix>/config/server/healthcheck~status` with
   `plugins.healthcheck.enabled`, and the version endpoint otherwise;
   the liveness probe always calls the version endpoint. See
   [Health checks](#health-checks).

The official Gerrit image declares `cache`, `db`, `etc`, `git` and
`index` under `/var/gerrit` as image volumes. The chart mounts each one
from the site PVC by `subPath`; without that, the container runtime
mounts empty anonymous volumes over them and the site disappears.

## Image

`image.repository` defaults to
`ghcr.io/lfreleng-actions/gerrit-docker-k8s/gerrit`, which each
release tag of this repository publishes, signs and attests; the
repository README describes the image and how to verify it. Its tags
are the release tags (`vX.Y.Z`), not the Gerrit version, so
`appVersion` only records the Gerrit version the image carries
(`GERRIT_VERSION` in `Dockerfile`) and the image tag never defaults to
it. Set `image.tag` to a release tag or `image.digest` to a manifest
digest; with neither, the chart refuses to render. A digest takes
precedence over the tag.

### Published chart

Each release tag also publishes this chart to
`oci://ghcr.io/lfreleng-actions/gerrit-docker-k8s/charts/gerrit`,
versioned as the tag without its `v` (`v1.2.3` publishes `1.2.3`).
The release workflow runs `scripts/pin-chart-images.py` to set
`image.repository`, `image.tag` and `image.digest` to the image the
same release published, packages the chart, lints and renders the
package and checks with `scripts/lib/chart_digests.py` that every
container image carries that digest. It then pushes the chart, signs
it with cosign (keyless, through GitHub OIDC) and attests its build
provenance, as it does the image, and attaches the packaged `.tgz` to
the GitHub release. The published chart therefore needs no image
values, but `my-values.yaml` must choose the authentication, as
[Authentication](#authentication) describes:

```sh
version=1.2.3
chart=oci://ghcr.io/lfreleng-actions/gerrit-docker-k8s/charts/gerrit
helm install gerrit "$chart" --version "$version" \
  -n gerrit --create-namespace -f my-values.yaml
```

Verify the chart before installing it:

```sh
repo=lfreleng-actions/gerrit-docker-k8s
workflow="https://github.com/${repo}/.github/workflows/release.yaml"
cosign verify "ghcr.io/${repo}/charts/gerrit:${version}" \
  --certificate-identity "${workflow}@refs/tags/v${version}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify "oci://ghcr.io/${repo}/charts/gerrit:${version}" \
  --repo "${repo}"
```

`K8S/argocd/example-gerrit.yaml` deploys the published chart with
Argo CD; `K8S/docs/DESIGN.md` section 6 explains its settings.

## Values

| Value                                    | Default                          | Notes                                                      |
| ---------------------------------------- | -------------------------------- | ---------------------------------------------------------- |
| `image.repository`                       | `ghcr.io/lfreleng-actions/gerrit-docker-k8s/gerrit` | The image each release publishes; see [Image](#image) |
| `image.tag`                              | `""`                             | A release tag such as `v1.2.3`; required unless `image.digest` is set |
| `image.digest`                           | `""`                             | Takes precedence over the tag when set                     |
| `gerrit.host`                            | `gerrit.example.org`             | Host part of `gerrit.canonicalWebUrl`                      |
| `gerrit.urlPrefix`                       | `/r`                             | Served path prefix; `""` serves from the root              |
| `gerrit.instanceId`                      | `""`                             | `gerrit.instanceId`; left out when empty                   |
| `gerrit.serverId`                        | `""`                             | See [Server ID](#server-id)                                |
| `gerrit.extraConfig`                     | `""`                             | Appended verbatim to `gerrit.config`                       |
| `gerrit.webScheme`                       | `https`                          | Scheme of `gerrit.canonicalWebUrl`; ignored with `ingress.enabled` |
| `gerrit.listenScheme`                    | `proxy-https`                    | `httpd.listenUrl` scheme; `http` for direct access; ignored with `ingress.enabled` |
| `ingress.enabled`                        | `false`                          | Renders the `Ingress`, or the Traefik objects              |
| `ingress.className`                      | `""`                             | `ingressClassName`; `nginx` adds the ingress-nginx annotations |
| `ingress.annotations`                    | `{}`                             | Added to the chart's own; a key here wins                  |
| `ingress.tls.enabled`                    | `true`                           | TLS ends at the controller                                 |
| `ingress.tls.secretName`                 | `""`                             | Defaults to `<fullname>-tls`                               |
| `ingress.tls.issuer`                     | `letsencrypt-prod`               | cert-manager `ClusterIssuer`; `""` to create the Secret yourself |
| `ingress.redirectRootToPrefix`           | `true`                           | Redirects `/` to `<gerrit.urlPrefix>/`                     |
| `ingress.securityHeaders.enabled`        | `true`                           | Traefik only; see below                                    |
| `ingress.securityHeaders.contentSecurityPolicy` | `""`                      | `""` for the chart's default policy                        |
| `ingress.traefik.enabled`                | `false`                          | `IngressRoute` and `Middleware`s instead of an `Ingress`   |
| `ingress.traefik.entryPoints`            | `[]`                             | Traefik's default entry points when empty                  |
| `pluginCatalogue.path`                   | `/var/gerrit-catalogue`          | Image directory holding `plugins/` and `lib/`              |
| `pluginCatalogue.plugins` / `.libs`      | `[]`                             | Jar names without `.jar`; a missing one fails `site-init`  |
| `persistence.storageClassName`           | `""`                             | Cluster default when empty                                 |
| `persistence.size`                       | `20Gi`                           |                                                            |
| `persistence.volumePermissions.enabled`  | `false`                          | See below                                                  |
| `resources`                              | 500m / 2Gi, 4Gi limit            |                                                            |
| `secrets.admin.existingSecret`           | `""`                             | Pre-created Secret with `username` and `password`          |
| `secrets.secureConfig.existingSecret`    | `""`                             | Pre-created Secret with `registerEmailPrivateKey`          |
| `ssh.host`                               | `""`                             | SSH host clients use; defaults to `gerrit.host` without its port |
| `ssh.port`                               | `null`                           | SSH port clients use; see [SSH](#ssh)                      |
| `ssh.service.enabled`                    | `true`                           | Adds the SSH `Service`                                     |
| `ssh.service.type`                       | `LoadBalancer`                   |                                                            |
| `ssh.service.nodePort`                   | `null`                           | Fixed node port with type `NodePort`                       |
| `ssh.service.annotations`                | `{}`                             | For example the AWS NLB annotations below                  |
| `ssh.service.externalTrafficPolicy`      | `Local`                          | Set on `LoadBalancer` and `NodePort` Services only         |
| `ssh.service.externalDNS`                | `false`                          | Adds `external-dns.alpha.kubernetes.io/hostname: <ssh.host>` |
| `networkPolicy.enabled`                  | `true`                           |                                                            |
| `auth.type`                              | `""`                             | Required unless SAML is on; see [Authentication](#authentication) |
| `auth.allowDevelopmentAccounts`          | `false`                          | Opt-in for `DEVELOPMENT_BECOME_ANY_ACCOUNT`; see [Authentication](#authentication) |
| `auth.saml.enabled`                      | `false`                          | See [SAML](#saml)                                          |
| `auth.saml.existingSecret`               | `""`                             | Pre-created Secret with the SAML keys; `""` creates `<fullname>-saml` |
| `auth.saml.metadataUrl`                  | `""`                             | IdP metadata URL; set this or `metadataFromSecret`         |
| `auth.saml.metadataFromSecret`           | `false`                          | Read the IdP metadata from the Secret's `idp-metadata.xml` |
| `auth.saml.serviceProviderEntityId`      | `""`                             | `""` for `gerrit.canonicalWebUrl`                          |
| `auth.saml.*Attr`, `.computedDisplayName`, `.maxAuthLifetime` | See `values.yaml` | The `[saml]` keys of the same name             |
| `auth.saml.memberOfAttr`                 | `null`                           | Group membership sync, off when unset; see [Group membership sync](#group-membership-sync) |
| `replication.enabled`                    | `false`                          | See [Replication](#replication)                            |
| `replication.autoReload`                 | `true`                           | `gerrit.autoReload` in `replication.config`                |
| `replication.lockErrorMaxRetries`, `.maxRetries` | `5`, `5`                 | `[replication]` globals                                    |
| `replication.sshKeySecret`               | `""`                             | Secret with the SSH identity; `""` mounts no key           |
| `replication.knownHosts`                 | `""`                             | Extra `known_hosts` lines; GitHub's host keys are always included |
| `replication.remotes`                    | `[]`                             | One `[remote "<name>"]` section per entry                  |
| `networkPolicy.ingressNamespaceSelector` | `{}`                             | Label selector for the namespaces that may reach 8080; `{}` admits any source |
| `clusterDomain`                          | `cluster.local`                  | Cluster DNS domain, for the Gerrit pod name the toolbox looks up |
| `toolbox.enabled`                        | `false`                          | See [Admin toolbox](#admin-toolbox)                        |
| `toolbox.replicas`                       | `0`                              | Scale to `1` for a maintenance session                     |
| `toolbox.existingSecret`                 | `""`                             | Secret with an administrator's `ssh-privatekey`; `""` mounts none |
| `toolbox.resources`                      | `{}`                             | Resources of the toolbox container                         |
| `hooks.enabled`                          | `false`                          | Adds the `hooks` plugin; see [Hooks](#hooks)               |
| `hooks.path`                             | `hooks`                          | Hooks directory under the site; the chart owns it          |
| `hooks.syncHookTimeout`                  | `30`                             | `hooks.syncHookTimeout`, in seconds                        |
| `hooks.scripts`                          | `{}`                             | Hook scripts by hook name, installed with mode `0755`      |
| `hooks.g2p.enabled`                      | `false`                          | gerrit-to-platform handles `hooks.g2p.hooks`; needs a replication remote |
| `hooks.g2p.existingSecret`               | `""`                             | Secret with `gerrit_to_platform.ini`; `""` creates `<fullname>-g2p` |
| `hooks.g2p.hooks`                        | `patchset-created`, `comment-added`, `change-merged` | Hooks linked to the g2p entry points |
| `plugins.its-jira.enabled`               | `false`                          | Adds `its-jira`; see [Issue tracker](#issue-tracker)       |
| `plugins.its-jira.url`                   | `""`                             | `its-jira.url`; required, `http` or `https`                |
| `plugins.its-jira.existingSecret`        | `""`                             | Secret with `username` and `password`; `""` creates `<fullname>-its-jira` |
| `plugins.its-jira.association`           | `OPTIONAL`                       | `MANDATORY`, `SUGGESTED` or `OPTIONAL`                     |
| `plugins.its-jira.commentOn*`            | `false`                          | One comment rule each; see [Issue tracker](#issue-tracker) |
| `plugins.its-jira.extraConfig`           | `""`                             | Appended verbatim to `etc/its/actions-its-jira.config`     |
| `plugins.healthcheck.enabled`            | `true`                           | Adds `healthcheck` and moves the readiness probe to it; see [Health checks](#health-checks) |
| `plugins.healthcheck.config`             | `auth` and `querychanges` off    | `etc/healthcheck.config`; a map per check                  |
| `plugins.metrics-reporter-prometheus.enabled` | `false`                     | Adds the Prometheus metrics plugin; see [Metrics](#metrics) |
| `metrics.existingSecret`                 | `""`                             | Secret with the bearer `token`; `""` creates `<fullname>-metrics` |
| `metrics.serviceMonitor.enabled`         | `false`                          | Renders a `ServiceMonitor` where the CRD exists            |
| `metrics.serviceMonitor.labels`          | `{}`                             | Added to the `ServiceMonitor`'s labels                     |
| `metrics.serviceMonitor.interval`, `.scrapeTimeout` | `60s`, `""`           | Scrape interval and timeout; `""` for Prometheus' default  |
| `networkPolicy.metricsNamespaceSelector` | `{}`                             | Namespaces of the scraper, admitted to 8080 beside `ingressNamespaceSelector` |
| `log.*`                                  | See [Logs](#logs)                | The `[log]` section of `gerrit.config`                     |
| `logs.level`                             | `INFO`                           | Lowest log4j level of the JSON error log on stderr         |
| `logs.retentionDays`                     | `0`                              | Days to keep rotated logs; `0` keeps them and adds no `CronJob` |
| `logs.schedule`, `.timeZone`, `.resources` | `30 4 * * *`, `""`, `{}`       | The log cleanup `CronJob`                                  |
| `siteImport.mode`                        | `auto`                           | `auto`, `force` or `skip`; see [Site import](#site-import) |
| `siteImport.forceToken`                  | `""`                             | With `force`, each new token replaces the site once        |
| `siteImport.source.type`                 | `none`                           | `none`, `pvc` or `https`                                   |
| `siteImport.source.url`                  | `""`                             | Export directory under `/mnt/import`, or the HTTPS base URL |
| `siteImport.source.volume`               | `{}`                             | With `pvc`, a volume source mounted read-only at `/mnt/import` |
| `siteImport.source.existingSecret`       | `""`                             | With `https`, a Secret of per-file URLs                    |
| `siteImport.source.egressPorts`          | `[]`                             | With `https`, ports other than 443 the Secret's URLs reach |
| `siteImport.source.expectedManifestSha256` | `""`                           | Pins the export's `manifest.json`                          |
| `siteImport.allowUnknownSourceVersion`   | `false`                          | Import an export whose manifest names no Gerrit version    |
| `siteImport.reindex`, `.verify`          | `true`, `true`                   | Reindex after the import; check repositories, host keys and server ID |
| `siteImport.scratchSizeLimit`            | `""`                             | `sizeLimit` of the scratch `emptyDir`                      |
| `siteImport.resources`                   | `{}`                             | Resources of `site-import`; `resources` when empty. Size it for the reindex |

### gerrit.config

Helm renders the whole of `etc/gerrit.config` from
`files/conf/gerrit.config.tpl`, so `helm template` shows the
configuration Gerrit runs with, and `site-init` replaces the site's
file with it at every start. Edits made on the volume do not survive a
restart.

The chart derives these keys, and they win over the same key in
values:

| Key                         | Value                                                         |
| --------------------------- | ------------------------------------------------------------- |
| `gerrit.basePath`           | `git`                                                         |
| `gerrit.canonicalWebUrl`    | `<scheme>://<gerrit.host><gerrit.urlPrefix>/`; the scheme is `https` behind the chart's Ingress with TLS, `http` without, and `gerrit.webScheme` with no Ingress |
| `httpd.listenUrl`           | `<scheme>://*:8080<gerrit.urlPrefix>/`; the scheme is `proxy-https` behind the chart's Ingress with TLS, `proxy-http` without, and `gerrit.listenScheme` with no Ingress |
| `sshd.advertisedAddress`    | `ssh.host`, else `gerrit.host`, without any port, then `:` and the SSH port: `ssh.port`, else `ssh.service.nodePort` on a `NodePort` Service, else `29418` |
| `cache.directory`           | `cache`                                                       |
| `container.javaOptions`     | The options the image needs and `-Dlog4j.configuration` naming `etc/log4j.properties` (see [Logs](#logs)), then `-XX:MaxRAMPercentage=70.0` unless `container.heapLimit` is set or there is no memory limit, then `container.javaOptions` from values |
| `httpd.filterClass`, `auth.type` and the `auth.http*Header` keys | The SAML filter and `HTTP` authentication, with `auth.saml.enabled`; see [SAML](#saml) |
| `saml.keystorePath`, `saml.metadataPath`, `saml.serviceProviderEntityId` | With `auth.saml.enabled`; see [SAML](#saml) |

Each of these top-level values renders the `gerrit.config` section of
the same name, key for key, with the defaults `values.yaml` lists:
`httpd`, `sshd`, `auth`, `sendemail`, `container`, `cache`, `change`,
`receive`, `download`, `index`, `gc`, `plugins`, `log`, `user`,
`trackingid`, `accounts` and `suggest`. A key set to `null` or `""` is left out, so
Gerrit's own default applies; a list repeats the key; a map renders a
subsection, as in `cache.diff_summary.diskLimit` becoming
`[cache "diff_summary"]`. Notable defaults:

| Value                       | Default                          | Notes                                                    |
| --------------------------- | -------------------------------- | -------------------------------------------------------- |
| `auth.type`                 | `""`                             | No default: values must choose; see [Authentication](#authentication) |
| `sendemail.enable`          | `false`                          | Gerrit's default relays through `localhost`, which the pod lacks |
| `sshd.listenAddress`        | `*:29418`                        |                                                          |
| `container.user`            | `gerrit`                         |                                                          |
| `cache.limits`              | See [Cache limits](#cache-limits) | `diskLimit` of each persistent cache                    |
| `gc.startTime`, `gc.interval` | `Sun 03:00`, `1 week`          | See [Garbage collection](#garbage-collection)            |
| `plugins.allowRemoteAdmin`  | `false`                          | Plugins come from `pluginCatalogue`, not remote installs |
| `log.rotate`, `log.compress` | `true`, `true`                  | See [Logs](#logs)                                        |

`jgit` and `notedb` render `etc/jgit.config` and `etc/notedb.config`
the same way, each top-level key a section. `jgit` defaults to
`receive.autogc: false`, which `gerrit init` would otherwise write and
the render would then replace. Secrets never go in
values: `auth.registerEmailPrivateKey`, `sendemail.smtpPass` and the
SAML keystore passwords come from the
[Secret contract](#secret-contract).

`gerrit.extraConfig` is appended verbatim, for settings with no typed
value. A key there that a typed value also sets adds a second entry
rather than replacing the first, so set such keys through the typed
value. The chart does not validate `extraConfig`, with two
exceptions: it refuses an `[auth]` section, as
[Authentication](#authentication) describes, and, on a best-effort
basis, `auth.httpDisplaynameHeader` with SAML enabled, as
[SAML](#saml) describes.

### Server ID

`gerrit.serverId` is site identity: Gerrit stamps it on every change
and refuses to load changes stamped with an ID it does not know. With
the value empty, Gerrit generates an ID on a new site and `site-init`
carries the site's ID into every later render. `site-init` stops
rather than read an unreadable or malformed `etc/gerrit.config` as
holding no ID. With the value set, `site-init` fails before Gerrit
starts when the site already holds a different ID, and on a new site
`gerrit init` takes it as the site's ID. When adopting an existing
site, copy the ID from its `etc/gerrit.config`; `site-import` refuses
an export whose ID differs from a set `gerrit.serverId`.

## Site import

The `site-import` init container decides what happens to the site
volume before `site-init` touches it, and records the result in
`.chart/site-state.json` on the volume. `K8S/docs/DESIGN.md`
section 1.7 holds the full design, and its section 4 the operator
checklist for a migration.

| `siteImport.mode` | Behaviour |
| ----------------- | --------- |
| `auto`            | Acts only while the site has no `complete` state: records a fresh site with `source.type: none`, or imports the source. With `source.type: none`, adopts a site an earlier chart version created; refuses any other volume that holds data but no state file |
| `force`           | Replaces the site from the source once for each new `forceToken`, then behaves as `auto`. Destructive; refuses an empty token or `source.type: none` |
| `skip`            | Does nothing; for a site the chart did not create |

The state file records the `state` (`in-progress`, `failed` or
`complete`), the `source` as `<type>:<url>`, the `manifestSha256`
of the imported export, the `gerritVersion` of the image, the
`forceToken` honoured, the time of the update, and the `reason` of a
failure or the `warnings` of the import, and `adopted: true` for a
site an earlier chart version created.

### Sources

The export must come from `gerrit-site-export.py final --pack`: a
directory holding `manifest.json`, `site.tar.zst` and `git.tar.zst`.
`site-import` checks each archive against the manifest, and
`source.expectedManifestSha256` pins the manifest itself.

- `pvc` mounts `source.volume` read-only at `/mnt/import`, and
  `source.url` names the export directory under it:

  ```yaml
  siteImport:
    source:
      type: pvc
      url: /mnt/import/final
      volume:
        persistentVolumeClaim:
          claimName: gerrit-export
  ```

- `https` fetches `<source.url>/manifest.json` and the archives
  beside it. With `source.existingSecret`, the Secret's
  `manifest.json`, `site.tar.zst` and `git.tar.zst` keys each hold a
  full URL, such as a pre-signed S3 URL, that wins over the base URL.
  `site-import` never logs a URL from the Secret, and refuses any
  request or redirect that is not HTTPS. With `networkPolicy.enabled`,
  the `NetworkPolicy` allows egress to the port `source.url` names;
  list any other port the Secret's URLs reach, beside 443, in
  `source.egressPorts`:

  ```yaml
  siteImport:
    source:
      type: https
      existingSecret: gerrit-export-urls
      egressPorts: [9000]
  ```

The chart refuses other source types. It does not fetch `s3://` URLs
or decrypt `.age` and `.gpg` archives yet; decrypt the export and
serve it over one of these sources.

The archives hold `secure.config`, the SSH host keys and every
account. `site-import` never logs their contents, but treat the export
volume, the bucket and any pre-signed URL as secrets, and delete them
once the import completes, in this order:

1. Set `siteImport.mode: auto` and `siteImport.source.type: none`,
   and upgrade the release.
2. Wait for the replacement pod to become Ready.
3. Delete the export volume, the URL Secret and the bucket's copy.

The pod mounts a configured source volume or Secret before
`site-import` can decide there is nothing to do, so a pod that still
names a deleted one never starts, even after a complete import.

### What an import runs

1. Reads `manifest.json` and refuses an export of a Gerrit release
   older than 3.7.0, the oldest release site import supports: upgrade
   the source site to 3.7 or newer and export it again. The export
   tool records the release as `gerrit_version`; when the manifest
   names none, the import refuses unless
   `siteImport.allowUnknownSourceVersion` is `true`, which does not
   lift the floor for a release the manifest names. A refused import
   leaves the site as it stands. The scheduled
   `.github/workflows/site-import-test.yaml` imports sites built with
   older official images to test this range.
2. Fetches and checks the export in the scratch `emptyDir`, which
   must hold the archives and their unpacked copy; size it with
   `siteImport.scratchSizeLimit`. It leaves out the export's `bin/`,
   `plugins/` and `lib/`: the chart's image supplies those, and a
   plugin or library built for the source release can stop the new
   one starting. It refuses links that resolve outside the archive's
   `site/` or `git/` tree, or into `.chart/`, `index/`, `cache/`,
   `bin/`, `plugins/` or `lib/`, except two kinds `site-init` recreates at
   every start, which it leaves out and logs: links in the site's
   `hooks.path` (default `hooks`) to the gerrit-to-platform entry
   point of the same name in `/opt/gerrit-tools/bin`, and
   `.config/gerrit_to_platform/replication.config` when it links to
   `/var/gerrit/etc/replication.config`. It refuses an export whose
   `gerrit.allProjects` or `gerrit.allUsers` differs from the chart's
   (`All-Projects` and `All-Users` by default): the chart's
   `gerrit.config` replaces the export's, and `init` would create
   empty repositories under the chart's names, leaving the imported
   permissions and accounts unused. Set the export's names in
   `gerrit.extraConfig`. With `verify`, it checks the repositories,
   their refs, the server ID and the SSH host keys there, before the
   site changes.
3. Replaces the site with the unpacked copy.
4. Runs `site-init` to install the chart's configuration, then
   `gerrit init --batch --no-auto-start` to migrate the site to the
   image's Gerrit, then installs the chart's configuration again.
5. With `reindex`, runs a full `gerrit reindex`, with one thread per
   core of the container's CPU limit, or Gerrit's default without one.
6. With `verify`, checks again that the repositories, the exported
   host keys and the server ID survived.

`init` and `reindex` get the heap the Gerrit container gets:
`container.heapLimit` when set, else 70% of the memory limit of
`siteImport.resources`, or of `resources` when that is empty. The
offline reindex is the most memory-hungry step of a migration, so size
`siteImport.resources` for it: a large site needs several GB.

With SAML enabled, `site-import` also refuses an export whose
keystore differs from the Secret's, before the site changes; see
[Importing a site that uses SAML](#importing-a-site-that-uses-saml).

`site-import` then logs a migration report: projects that leave
`receive.rejectImplicitMerges` unset, `commentlink` sections with
`html`, `rules.pl` files, `auth.cookieSecure`, `secure.config` names
the chart does not write and so drops, and a reminder that a new
`registerEmailPrivateKey` invalidates outstanding email verification
links. Read it with `kubectl logs <fullname>-0 -c site-import`.

### Recovery

Any failure records `failed` with its `reason` and stops the pod
before Gerrit starts. Each restart retries the import, so fix the
source or the archive and delete the pod. `source.type: none` after a
failed import refuses rather than start an empty site; set `force` with
a new `forceToken` to replace a site, or `skip` to keep it as it
stands.

A site created by chart `0.6.0` or `0.6.1` has no state file. With
`mode: auto` and `source.type: none`, the defaults, `site-import`
adopts it when `etc/gerrit.config` exists and the `All-Projects`
repository it names exists in the git store: it records `complete`
with `manifestSha256: null` and `adopted: true`, and logs that it
adopted the site, so upgrading the chart keeps the release running.
Any other data on a volume with no state file stops the pod; repair
the volume, force an import over it, or set `siteImport.mode: skip`
to keep it as it stands.

## Exposing Gerrit

HTTP goes through the cluster's Ingress controller to the
`<fullname>-http` Service; SSH goes through its own `<fullname>-ssh`
Service, because an HTTP Ingress cannot route Gerrit's SSH protocol.
The chart derives `gerrit.canonicalWebUrl`, `httpd.listenUrl` and
`sshd.advertisedAddress` from `gerrit.host`, `gerrit.urlPrefix`,
`ssh.host` and `ingress.tls.enabled`, so the links Gerrit builds match
what the Ingress and the SSH Service serve.

### Ingress

With `ingress.enabled`, the chart renders a `networking.k8s.io/v1`
`Ingress` for the host in `gerrit.host`, without its port, and the path
`gerrit.urlPrefix` (`/` for the root) with `pathType: Prefix`. It does
not rewrite the path: Gerrit expects the prefix in the request. With
`ingress.tls.enabled`, TLS ends at the controller with the certificate
in `ingress.tls.secretName`, and with `ingress.tls.issuer` set
cert-manager issues that Secret through the
`cert-manager.io/cluster-issuer` annotation.

Only ingress-nginx has annotations for the root redirect and for large
`git push` requests, so with `ingress.className: nginx` the chart adds
`app-root` for the redirect, with an `Exact` path for `/` so that the
Ingress receives the request it redirects, `proxy-body-size: "0"` and
one-hour read and send timeouts. For another controller, set its
equivalents in `ingress.annotations`, or use the Traefik objects
below. The security headers are Traefik-only for the same reason.

### Traefik

With `ingress.traefik.enabled`, the chart renders a Traefik
`IngressRoute` and `Middleware`s instead of the `Ingress`:

- a route for `Host(<host>)` and the prefix, matched by path segment;
- with `ingress.redirectRootToPrefix` and a prefix, a route for `/`
  through a `redirectRegex` Middleware to `<prefix>/`;
- with `ingress.securityHeaders.enabled`, a `headers` Middleware on
  both routes, ahead of the redirect so that the redirect carries the
  headers too, setting `Content-Security-Policy`,
  `Strict-Transport-Security` (with TLS), `Referrer-Policy`,
  `Permissions-Policy` and `X-Content-Type-Options: nosniff`;
- with TLS and an issuer, a cert-manager `Certificate`, since
  cert-manager does not read `IngressRoute`s.

The default `Content-Security-Policy` allows same-origin resources,
the inline scripts and styles the web UI and SAML's auto-post page
use, web workers from `blob:` URLs, which the web UI's syntax
highlighting creates, and avatar images from any HTTPS origin. Set
`ingress.securityHeaders.contentSecurityPolicy` to replace it.

The chart adds no buffering Middleware, so Traefik puts no limit on
the request body and streams a `git push` through. The read timeout
is the entry point's `respondingTimeouts.readTimeout`, which Traefik
v3 sets to 60 seconds by default. It belongs to Traefik's own static
configuration, not to the chart, so raise it on the entry point Gerrit
uses, for example to `0` or several hours, before pushing large
repositories over HTTP.

`ingress.className` becomes the `kubernetes.io/ingress.class`
annotation, which Traefik's `kubernetesCRD` provider filters on when it
has an `ingressClass` set; that key in `ingress.annotations` wins.

### SSH

`ssh.service.enabled` (the default) renders `<fullname>-ssh` on port
29418 as `ssh.service.type`, `LoadBalancer` by default, with
`externalTrafficPolicy: Local` so that `sshd_log` records each
client's address rather than a node's. With `Local`, only nodes running
the Gerrit pod pass the load balancer's health check, which is what a
single-replica StatefulSet needs. `sshd.advertisedAddress` is
`<ssh.host>:<port>`, with `ssh.host` defaulting to `gerrit.host`.

The port is `ssh.port` when set, for a proxy or load balancer that
publishes 29418 on another port. Otherwise a `NodePort` Service
advertises `ssh.service.nodePort`, which the chart also sets as the
Service's node port, and any other type advertises 29418. Kubernetes
picks an unset node port at random, so a `NodePort` Service with
neither value fails to render rather than advertise a port that does
not answer.

With `ssh.service.externalDNS`, the Service carries
`external-dns.alpha.kubernetes.io/hostname: <ssh.host>`. The Ingress
publishes `gerrit.host` through a different load balancer, so with both
enabled `ssh.host` has to name another host, such as
`ssh.gerrit.example.org`; the chart fails to render otherwise.

On AWS, an NLB through the AWS Load Balancer Controller needs:

```yaml
ssh:
  host: ssh.gerrit.example.org
  service:
    annotations:
      service.beta.kubernetes.io/aws-load-balancer-type: external
      service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip
      service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing
      service.beta.kubernetes.io/aws-load-balancer-target-group-attributes: preserve_client_ip.enabled=true
    externalDNS: true
```

IP targets send traffic straight to the pod, bypassing
`externalTrafficPolicy`. The NLB then keeps the client address only
with the target group attribute `preserve_client_ip.enabled=true`,
which the last annotation sets; without it `sshd_log` records the
NLB's address.

### CDN or TCP proxy in front

A CDN or HTTP proxy in front of the Ingress works for HTTP when it
forwards the `Host` header and the path unchanged and sets
`X-Forwarded-Proto: https`; leave `gerrit.host` as the public name.
Raise its body size limit and read timeout as for the Ingress, or
large pushes over HTTP fail at the CDN.

An HTTP CDN cannot carry SSH. Point `ssh.host` at a name that resolves
to the SSH load balancer, or to a TCP (layer 4) proxy that forwards
port 29418 unchanged, not at the CDN's name. Leave the PROXY protocol
off on such a proxy unless the Gerrit release in use accepts it on its
SSH port: a PROXY header sent to a daemon that does not expect one
breaks every connection. Without it, `sshd_log` records the proxy's
address.

### Why TLS cannot end in front of SSH

Gerrit's SSH port speaks the SSH protocol, which runs its own key
exchange and encryption directly over TCP. A TLS terminator in front
of 29418 would expect a TLS handshake that SSH clients never send, and
would send TLS records that Gerrit's SSH daemon cannot read, so the
connection fails at once. A TLS-terminating HTTP load balancer or CDN
also routes by HTTP request, which SSH does not make. SSH therefore
needs a plain TCP path end to end, and its host key, not a TLS
certificate, proves the server's identity.

### Network policy

`networkPolicy.ingressNamespaceSelector` limits port 8080 to pods in
the namespaces it selects, for example the Ingress controller's:

```yaml
networkPolicy:
  ingressNamespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: traefik
```

Left empty, 8080 admits any source, as port 29418 always does: SSH
clients arrive from outside the cluster through the load balancer.

With the selector set, a Prometheus outside those namespaces cannot
scrape the [metrics](#metrics) plugin. With
`plugins.metrics-reporter-prometheus.enabled`,
`networkPolicy.metricsNamespaceSelector` admits its namespaces to 8080
as well:

```yaml
networkPolicy:
  ingressNamespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: traefik
  metricsNamespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: monitoring
```

### Volume permissions

The pod runs as UID and GID 1001 with `fsGroup` and
`fsGroupChangePolicy: OnRootMismatch`, a read-only root filesystem,
every capability dropped and no service account token, so it meets the
`restricted` Pod Security level. Some provisioners, kind's
`local-path` among them, ignore `fsGroup`. For those,
`persistence.volumePermissions.enabled` adds an init container that
runs as root with `CHOWN` and `DAC_OVERRIDE`. That container breaks
the `restricted` level but stays within `baseline`, so enable it only
where the namespace allows `baseline` or less, as in local smoke tests.

## Replication

`replication.enabled` configures the core `replication` plugin to push
from this Gerrit to other remotes, such as a GitHub organisation
mirror or a plain git mirror. This is the opposite direction from the
pull-replication the GitHub Action in this repository sets up.
Enabling it:

- adds `replication` to the plugins `site-init` copies from the image
  catalogue, so the image must provide
  `<pluginCatalogue.path>/plugins/replication.jar`;
- renders `etc/replication.config`: `[gerrit] autoReload`, the
  `[replication]` globals, and one `[remote "<name>"]` section per
  entry of `replication.remotes` with only the keys that entry sets;
- mounts `/var/gerrit/.ssh` read-only with mode `0400`, holding every
  key of the `replication.sshKeySecret` Secret and, as `known_hosts2`,
  GitHub's published SSH host keys plus `replication.knownHosts`. The
  SSH client reads both `known_hosts` and `known_hosts2`, so the Secret
  may carry its own `known_hosts` too;
- allows NetworkPolicy egress to the port of each URL a remote's `url`
  and `adminUrl` give, one or a list: the port a URL names, or else 22
  for `ssh://`, `gerrit+ssh://` and `user@host:path` URLs, 80 for
  `http://` and `gerrit+http://`, 9418 for `git://`, and 443 for HTTPS,
  which is always open.

The plugin's ref-update queue in `data/replication/` stays on the site
volume, so pending pushes survive a restart.

```yaml
replication:
  enabled: true
  sshKeySecret: gerrit-replication-ssh
  remotes:
    - name: github
      url: git@github.com:example-org/${name}.git
      authGroup: GitHub Replication
      push:
        - +refs/heads/*:refs/heads/*
        - +refs/tags/*:refs/tags/*
      remoteNameStyle: dash
      replicationDelay: 15
```

Create the Secret from a deploy key that may push to the remotes:

```sh
kubectl create secret generic gerrit-replication-ssh \
  --from-file=id_ed25519=./replication_ed25519
```

Each remote key other than `name` is the `replication.config` key of
the same name, as the
[replication plugin documentation](https://gerrit.googlesource.com/plugins/replication/+/refs/heads/master/src/main/resources/Documentation/config.md)
describes it. A list repeats the key, and `null`, `""` or an empty
list leaves it out. A remote without `name` or `url`, or two remotes of
one name, fail the render.

### Service account and authGroup

The plugin pushes as the Gerrit server, but `authGroup` limits each
remote to the refs that group may read: a ref the group cannot see is
never pushed, so hidden projects and restricted branches stay out of
the mirror. Create a group for the remote, for example
`GitHub Replication`, grant it `Read` on the refs to mirror in
`All-Projects` or the projects concerned, and name it in `authGroup`.
A remote without `authGroup` replicates every ref. The SSH key, not
the group, is what the remote authenticates: give the key's public
half write access there, such as a GitHub deploy key or a machine
user in the organisation.

### Adding a mirror remote

Append an entry to `replication.remotes`. `mirror: true` deletes refs
on the remote that no longer exist in Gerrit, so give a mirror remote
its own repositories:

```yaml
    - name: mirror
      url: ssh://git@git.example.org:2222/mirrors/${name}.git
      push:
        - +refs/*:refs/*
      mirror: true
      projects:
        - ^example/.*
```

Add the remote's host key to `replication.knownHosts`, or to the
Secret's `known_hosts`; the plugin refuses a host it does not know.
A port other than 22 in the URL also needs a matching `[host]` entry,
`[git.example.org]:2222 ssh-ed25519 ...`. A `helm upgrade` that
changes the remotes changes the config map's checksum, which restarts
the pod, and `site-init` installs the new `replication.config`.

### Verifying replication

An administrator lists the configured remotes, and can start a run,
over Gerrit's SSH port:

```sh
ssh -p 29418 <user>@<ssh.host> replication list
ssh -p 29418 <user>@<ssh.host> replication start --all --wait
```

`logs/replication_log` on the site volume records each push.

## Authentication

The chart has no default authentication: the render fails until values
choose one of:

- **SAML**, with `auth.saml.enabled: true`: single sign-on through an
  identity provider, as [SAML](#saml) describes. Production sites,
  which are migrations of existing SAML sites, use this.
- **Another Gerrit auth type** in `auth.type`, such as `LDAP` or
  `OAUTH`, with the keys it needs in `auth` and the other sections
  ([config-gerrit: auth.type](https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#auth.type)).
- **Development accounts**, with `auth.type:
  DEVELOPMENT_BECOME_ANY_ACCOUNT` and `auth.allowDevelopmentAccounts:
  true`. Anyone who reaches the site can sign in as any account, the
  administrator included, without a password. They serve CI, such as
  the kind smoke test and `values-local-smoke.yaml`, and throwaway
  sandboxes; never a site with real users or data.

```yaml
auth:
  type: DEVELOPMENT_BECOME_ANY_ACCOUNT
  allowDevelopmentAccounts: true
```

The render fails with an error naming the fix when values:

- set neither SAML nor `auth.type`: `choose authentication: ...`;
- set `auth.type: DEVELOPMENT_BECOME_ANY_ACCOUNT` without
  `auth.allowDevelopmentAccounts: true`, in any case or with hyphens
  for underscores, as Gerrit reads it: `auth.type
  DEVELOPMENT_BECOME_ANY_ACCOUNT lets anyone sign in as any account
  ...`;
- set `auth.allowDevelopmentAccounts: true` with `auth.saml.enabled`:
  `auth.allowDevelopmentAccounts cannot be combined with
  auth.saml.enabled ...`;
- set `auth.allowDevelopmentAccounts` to anything but a boolean, such
  as the quoted `"false"` or `--set-string` gives:
  `auth.allowDevelopmentAccounts must be a boolean ...`. Only boolean
  `true` opts in;
- set `auth.type` to anything but a string, such as a list:
  `auth.type must be a string ...`;
- carry an `[auth]` section header in `gerrit.extraConfig`, whatever
  the choice above and whatever keys follow it, none included:
  `gerrit.extraConfig cannot carry an [auth] section ...`;
- set `auth.cookieSecure`, in any case: `auth.cookieSecure was removed
  in Gerrit 3.13 ...`. Gerrit now marks the session cookie secure
  itself whenever a request arrives over HTTPS
  ([3.13 release notes](https://www.gerritcodereview.com/3.13.html)),
  so the key would be silently ignored. `null`, `""` or an empty list
  renders no key and passes.

`gerrit.extraConfig` cannot carry an `[auth]` section. Every `[auth]`
setting is set through `auth` values, which render every key they
hold. The chart appends `extraConfig` after the `[auth]` section it
renders, and Gerrit reads the last value of a repeated key, so a
`type` there would replace the authentication values choose. With
`DEVELOPMENT_BECOME_ANY_ACCOUNT`, it would let anyone sign in as any
account on a SAML site, without the opt-in. The chart refuses the
section header itself rather than looking for `type` under it, so a
difference between its scan and Gerrit's parser cannot become an
`auth.type` override. A line that starts, after whitespace, with
`[auth]` in any case counts, even one that continues a value, with a
carriage return or Unicode whitespace treated as a space or tab.
Comments, named subsections such as `[auth "x"]`, and `[auth]` later
in a line pass.

`helm lint` reports these failures at `INFO` level and skips the
templates concerned, so lint with values that choose authentication.
`auth.allowDevelopmentAccounts` is a chart value only and never
reaches `gerrit.config`.

After each install or upgrade, Helm prints the chart's notes: a
prominent warning while development accounts are enabled, otherwise
the authentication in effect.

**`HTTP` and `HTTP_LDAP` need an authenticating proxy.** Without SAML,
these types trust the identity headers on each request, such as
`Authorization` or the header `auth.httpHeader` names. They are safe
only behind an authenticating proxy that removes those headers from
client requests and sets them itself; the chart's own Ingress does
not, so behind it any client can claim any account. SAML is safe here
because its filter discards client-supplied `X-SAML-*` headers before
it sets them from the verified assertion.

## SAML

`auth.saml.enabled` turns on SAML single sign-on through Gerrit's
`saml` library module, the authentication production sites use.
Enabling it:

- adds `saml` to the libraries `site-init` copies from the image
  catalogue, so the image must provide
  `<pluginCatalogue.path>/lib/saml.jar`; `site-init` fails before
  Gerrit starts when it does not, and removes the library from the
  site again once SAML is disabled;
- renders `auth.type = HTTP`, the `auth.http*Header` keys the module
  fills, `gerrit.installModule =
  com.googlesource.gerrit.plugins.saml.Module`, `httpd.filterClass =
  com.googlesource.gerrit.plugins.saml.SamlWebFilter` and a `[saml]`
  section in `gerrit.config`. These win over the same keys in values,
  so `auth.type` from values no longer applies;
- fails the render with `auth.allowDevelopmentAccounts: true`, as
  [Authentication](#authentication) describes;
- fails the render when `auth.httpDisplaynameHeader` is set in `auth`
  values or `gerrit.extraConfig`, since the SAML filter refuses to
  start with it; `null`, `""` or an empty list in `auth` renders no
  key and passes. The `extraConfig` check is best effort only: an
  active entry in the unqualified `[auth]` section counts, with a
  carriage return or Unicode whitespace treated as a space or tab,
  while comments, values and named subsections such as `[auth "x"]`
  pass. Any other `[auth]` section in `extraConfig` fails the render
  anyway, as [Authentication](#authentication) describes, and the
  SAML plugin refuses to start, with a clear error, if the header is
  set anywhere;
- mounts the Secret's `samlKeystore.jks` read-only at
  `etc/samlKeystore.jks`, the `[saml] keystorePath`, and, with
  `metadataFromSecret`, its `idp-metadata.xml` at
  `etc/saml-idp-metadata.xml`, the `[saml] metadataPath`;
- writes `keystore-password` and `private-key-password` to
  `saml.keystorePassword` and `saml.privateKeyPassword` in
  `etc/secure.config` through `render-config`;
- with `metadataUrl` set, allows NetworkPolicy egress to the port of
  that URL: the port it names, or 80 for `http://`. HTTPS on port 443
  is always open.

The module writes its service provider metadata to `data/saml/` on the
site volume, so it survives restarts.

```yaml
auth:
  saml:
    enabled: true
    existingSecret: gerrit-saml
    metadataUrl: https://idp.example.org/app/gerrit/sso/saml/metadata
    userNameAttr: uid
    displayNameAttr: displayName
    emailAddressAttr: mail
```

Set exactly one of `metadataUrl` and `metadataFromSecret`; neither or
both fail the render, as does a `metadataUrl` that is not an `http://`
or `https://` URL. `serviceProviderEntityId` defaults to
`gerrit.canonicalWebUrl`. The attribute keys, `computedDisplayName`
and `maxAuthLifetime` are the `[saml]` keys of the same name; `null`
or `""` leaves a key out, so the module's default applies.

Create the Secret from a Java keystore holding the service provider's
signing key, and the two passwords:

```sh
kubectl create secret generic gerrit-saml \
  --from-file=samlKeystore.jks=./samlKeystore.jks \
  --from-literal=keystore-password='...' \
  --from-literal=private-key-password='...'
```

Add `--from-file=idp-metadata.xml=./idp-metadata.xml` with
`metadataFromSecret`. Without `auth.saml.existingSecret` the chart
creates `<fullname>-saml` with every key empty for you to fill in, and
keeps the values on upgrade. Under Argo CD, which renders without
cluster access, name a pre-created Secret in `existingSecret` instead,
as for the other Secrets.

### Group membership sync

`memberOfAttr` names the assertion attribute that lists the user's
groups, and turns on the module's group membership sync; `null` or
`""` leaves it out and the sync off. Each group the attribute names
becomes a Gerrit group `saml/<name>`, created the first time it
appears, and at each web login the module adds the user to the groups
the assertion names and removes them from the other `saml/` groups.
Access rules grant rights to these groups as to any other.

```yaml
auth:
  saml:
    memberOfAttr: memberOf
```

Membership changes only at a web login. Someone removed from a group
at the identity provider keeps its rights over SSH and git until they
next log in to the web UI.

When a site that uses the sync is imported, its `saml/` groups, and
the access rules that name them, carry over with the import. The
module finds groups by name, so the identity provider must keep
sending the same group names and user names. A renamed group becomes a
new `saml/` group that does not inherit the old one's access grants.
Each user moves from the old group to the new one only at their next
web login; until then they keep the rights the old group's rules
grant.

### Identity provider checklist

- **Assertion consumer service URL:**
  `<gerrit.canonicalWebUrl>plugins/saml/callback`, that is
  `https://<gerrit.host><gerrit.urlPrefix>/plugins/saml/callback`. It
  stays the same for as long as `gerrit.host` and `gerrit.urlPrefix`
  do; changing either means updating the identity provider.
- **Service provider entity ID:** `serviceProviderEntityId`, by default
  `gerrit.canonicalWebUrl`.
- **Signing certificate:** the public half of the key in
  `samlKeystore.jks`, or the service provider metadata the module
  writes to `data/saml/`.
- **Attributes:** release the user name, display name and email
  address under the names `userNameAttr`, `displayNameAttr` and
  `emailAddressAttr` give, or first and last name with
  `computedDisplayName`. The user name becomes the Gerrit account's
  user name, so it must be stable and unique.
- **Groups:** with the group sync, release the user's groups under the
  name `memberOfAttr` gives.

### Rotating the keystore

Add the new certificate to the identity provider alongside the old
one, replace `samlKeystore.jks`, and the passwords if they changed, in
the Secret, and restart the pod: the keystore mount and
`secure.config` take the new values at start. Remove the stale service
provider metadata from `data/saml/` first, for example from the
[admin toolbox](#admin-toolbox), so the module writes it again with
the new certificate. Remove the old certificate from the identity
provider once logins work.

### Importing a site that uses SAML

When an existing site is imported, its keystore and service provider
metadata come from the source site and must not be regenerated: the
identity provider trusts the source's key, and a new keystore breaks
every login until the identity provider is updated. Fill the Secret
with the source's keystore and passwords before the first start;
`site-import` refuses an export whose keystore differs from the
Secret's. It compares the keystore the export's `saml.keystorePath`
names, `etc/samlKeystore.jks` when unset, relative to the site or
absolute within the exported site directory or `/var/gerrit`. It
refuses a path outside the site, and a path other than
`etc/samlKeystore.jks` that names no file in the export, since it
cannot then tell whether the Secret keeps the site's identity.

## Hooks

`hooks.enabled` adds the `hooks` plugin to the jars `site-init` copies
from `pluginCatalogue.path` and renders `[hooks]` with `path` set to
`/var/gerrit/<hooks.path>` and `syncHookTimeout`. The image must hold
`<pluginCatalogue.path>/plugins/hooks.jar`; the official image ships
it in `/var/gerrit/plugins`. `hooks.path` is a relative path that may
not start with a site directory such as `etc`, `git` or `plugins`.

The chart owns the hooks directory. At every start `site-init` removes
the files and links at its top level, then installs:

- each `hooks.scripts` entry, a hook name such as `ref-updated` mapped
  to the script's text, with mode `0755`;
- with `hooks.g2p.enabled`, a link from each hook in `hooks.g2p.hooks`
  to the gerrit-to-platform entry point of the same name in
  `/opt/gerrit-tools/bin`. A hook may not be both a script and a g2p
  hook, and an entry point missing from the image fails `site-init`.

gerrit-to-platform reads
`$XDG_CONFIG_HOME/gerrit_to_platform/gerrit_to_platform.ini`. With g2p
the chart sets `XDG_CONFIG_HOME=/var/gerrit/.config` in the `gerrit`
container, and `site-init` installs the Secret's
`gerrit_to_platform.ini` there with mode `0600` in a `0700` directory,
beside a link to `etc/replication.config`, which g2p reads for the
remotes. g2p finds the platform and repositories there, so
`hooks.g2p.enabled` needs `replication.enabled` with at least one
remote in `replication.remotes`, and the render fails without one.
The generated `<fullname>-g2p` Secret holds an empty
`gerrit_to_platform.ini`; fill it in the cluster, or under Argo CD
name a pre-created Secret in `hooks.g2p.existingSecret`, since an
offline render would replace it on each sync. Disabling g2p removes
the file and the link.

```yaml
replication:
  enabled: true
  remotes:
    - name: github
      url: git@github.com:example-org/${name}.git
hooks:
  enabled: true
  scripts:
    ref-updated: |
      #!/bin/sh
      logger -t gerrit-hook "ref-updated $*"
  g2p:
    enabled: true
    existingSecret: gerrit-g2p
```

## Issue tracker

`plugins.its-jira.enabled` adds `its-jira` to the jars `site-init`
copies, so the image must hold `its-jira.jar` under
`<pluginCatalogue.path>/plugins/`. `its-base` is compiled into that
jar, not installed beside it. The chart renders `[its-jira] url`
from `plugins.its-jira.url` and `[plugin "its-jira"] association` in
`gerrit.config`, and `render-config` writes the Secret's `username` and
`password` (or an API token as the password) to `its-jira.username` and
`its-jira.password` in `secure.config`. The generated
`<fullname>-its-jira` Secret has both keys empty; fill them in the
cluster, or under Argo CD name a pre-created Secret in
`plugins.its-jira.existingSecret`.

Each `commentOn*` toggle adds a rule to
`etc/its/actions-its-jira.config` that comments on the associated
issues: `commentOnChangeCreated` on the first patch set,
`commentOnPatchSetCreated` on every patch set, and
`commentOnChangeMerged` and `commentOnChangeAbandoned` on those
status changes. `extraConfig` is appended verbatim, for other rules
and actions. `site-init` replaces the file at each start and removes
it when the plugin is disabled.

The plugin acts only on projects that enable it, through
`plugin.its-jira.enabled` in a project's `project.config` (or
`All-Projects` for every project). It finds issue keys in commit
messages through the `[commentlink "its-jira"]` section, whose `match`
and `link` the chart has no typed value for: set them in
`gerrit.extraConfig`, as below, or in a project's `project.config`.
Without that section the plugin associates no issue, whatever the
`association` and comment rules say. `trackingid` renders only
`[trackingid]` sections, which index the keys for search, and does not
configure the plugin. With a Jira URL on a port other than 443, the
`NetworkPolicy` adds an egress rule for it.

```yaml
gerrit:
  extraConfig: |
    [commentlink "its-jira"]
      match = ([A-Z][A-Z0-9]+-[0-9]+)
      link = https://jira.example.org/browse/$1
plugins:
  its-jira:
    enabled: true
    url: https://jira.example.org
    existingSecret: gerrit-its-jira
    association: SUGGESTED
    commentOnChangeMerged: true
```

Disabling `hooks` or `its-jira` removes the jars at the next start but
leaves any data the plugins wrote under the site's `data/`.

## Observability

A release reports its health, metrics and logs to cluster-native
tooling, with no vendor agent in the pod. The Gerrit pod, the admin
toolbox and the chart's CronJobs carry the
`instrumentation.opentelemetry.io/inject-*: "false"` annotations, so
the OpenTelemetry operator's mutating webhook does not inject an
agent container that the `restricted` Pod Security level would
reject.

### Health checks

`plugins.healthcheck.enabled`, on by default, adds `healthcheck` to
the jars `site-init` copies, so the image must hold `healthcheck.jar`
under `<pluginCatalogue.path>/plugins/`. The published image's
catalogue holds it. `site-init` fails on a missing jar, so turn the
plugin off for an image built without the catalogue.

With the plugin enabled, the readiness probe calls
`<urlPrefix>/config/server/healthcheck~status`, which answers 200 only
while every enabled check passes, instead of
`<urlPrefix>/config/server/version`, so a failing check takes the pod
out of service. The liveness probe keeps the version endpoint: a
failing check does not restart the JVM, which would not fix it. The
startup probe keeps the version endpoint and its ten minutes, since a
first start loads the caches and indexes before it serves anything.
With the plugin disabled, every probe calls the version endpoint.

`plugins.healthcheck.config` renders `etc/healthcheck.config`: keys
such as `timeout` go under `[healthcheck]`, and a map per check, such
as `querychanges`, renders its `[healthcheck "<check>"]` subsection
with `enabled`, `timeout` and the check's own keys. The default turns
off two checks. The `auth` check logs in with a fixed account a site
rarely has; enable it with an account the site holds. The
`querychanges` check fails unless a query for open changes returns
its `limit`, ten by default, so it measures how much data a site holds
rather than whether Gerrit is healthy, and fails on a new site; see
the plugin's
[configuration](https://github.com/GerritCodeReview/plugins_healthcheck/blob/52ba0d27ff869e3a0d0ac09529b8e12b81b75c85/src/main/resources/Documentation/config.md).
`site-init`
replaces the file at each start and removes it when the plugin is
disabled.

```yaml
plugins:
  healthcheck:
    enabled: true
    config:
      timeout: 2s
      auth:
        enabled: false
      querychanges:
        enabled: false
        timeout: 5s
```

### Metrics

`plugins.metrics-reporter-prometheus.enabled` adds the
`metrics-reporter-prometheus` jar, which the image catalogue must
hold, and serves Gerrit's metrics in the Prometheus format at
`<urlPrefix>/plugins/metrics-reporter-prometheus/metrics`. Gerrit's
own metrics include the JVM's, under `proc/jvm/`, so they are on
whenever the plugin is.

The endpoint asks for a bearer token. `render-config` writes the
`token` key of `metrics.existingSecret` to
`plugin.metrics-reporter-prometheus.prometheusBearerToken` in
`secure.config`. With the value empty, the chart creates
`<fullname>-metrics` with a random token and keeps it on upgrade;
under Argo CD name a pre-created Secret instead, or each sync replaces
the token (see the [Secret contract](#secret-contract)).

`metrics.serviceMonitor.enabled` renders a Prometheus Operator
`ServiceMonitor` for the `<fullname>-http` Service, presenting the
same Secret's `token` as its bearer token, with
`metrics.serviceMonitor.labels` added, for the label a Prometheus
selects its monitors by. It renders only where the cluster serves
`monitoring.coreos.com/v1`, so a cluster without the operator's CRDs
gets none; `helm template` sees that API only with
`--api-versions monitoring.coreos.com/v1`. The toggle needs the
metrics plugin. With `networkPolicy.ingressNamespaceSelector` set,
admit the Prometheus namespace through
`networkPolicy.metricsNamespaceSelector`; see
[Network policy](#network-policy).

```yaml
plugins:
  metrics-reporter-prometheus:
    enabled: true
metrics:
  existingSecret: gerrit-metrics
  serviceMonitor:
    enabled: true
    labels:
      release: prometheus
```

### Logs

The chart renders `etc/log4j.properties`, which `site-init` installs
at every start, and names it in `container.javaOptions` with
`-Dlog4j.configuration`, as Gerrit's
[`container.javaOptions`](https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#container.javaOptions)
documents. Gerrit then skips its built-in loggers:

- The error log goes only to the container's stderr, as JSON with
  Gerrit's `ErrorLogJsonLayout`, one object per line, so the
  cluster's log pipeline collects structured entries. `logs.level`
  sets its lowest level, `INFO` by default. There is no `error_log`
  file in `logs/`. The `--console-log` flag `bin/gerrit.sh run`
  passes has no effect, since Gerrit acts on it only when it sets up
  logging itself, and then writes plain text.
- `httpd_log` and `sshd_log`, the request logs, stay in `logs/` on
  the site volume for audit, in Gerrit's text layouts. Gerrit attaches
  them by appender name, so the file defines appenders with those
  names; without them it would silently disable request logging.

The `log` values render the `[log]` section of `gerrit.config`:

| Value          | Default | Notes                                                       |
| -------------- | ------- | ----------------------------------------------------------- |
| `log.rotate`   | `true`  | Rotates each request log daily, adding the date to its name |
| `log.compress` | `true`  | Compresses each rotated log with gzip                       |

Gerrit ignores `log.rotate` for appenders a log4j file defines, so the
chart applies it in `etc/log4j.properties`: a daily
`DailyRollingFileAppender` when it is on, a plain `FileAppender` when
it is off. The chart refuses `log.jsonLogging` and `log.textLogging`.
Gerrit ignores both for its error log under a log4j file, yet they
still decide which request log appenders it looks up: `jsonLogging`
adds `httpd_log.json` and `sshd_log.json`, which the file does not
define, and with `textLogging` off as well Gerrit drops `httpd_log`
and `sshd_log`, silently disabling request logging. A
`-Dlog4j.configuration` in `container.javaOptions`
from values comes later on the command line and replaces the chart's
file.

To bound the request logs, set `logs.retentionDays`: the
`<fullname>-log-cleanup` `CronJob` then runs
`files/scripts/log-cleanup.sh` on `logs.schedule`, 04:30 daily by
default, and deletes each file at the top of `logs/` whose name
carries a rotation date, such as `httpd_log.2026-11-01.gz`, and that
was last written more than that many days ago. A log Gerrit still
writes has no date in its name, so the cleanup never touches it. The
Job runs as the Gerrit UID on the Gerrit pod's node, with the site
volume mounted, no service account token and, with
`networkPolicy.enabled`, a `NetworkPolicy` that denies it all traffic,
as the [git gc CronJob](#garbage-collection) does. With `log.rotate`
off nothing carries a date, so there is nothing to delete.

### What to alert on

The metric names below are Gerrit's, as the plugin exports them with
`/` and `-` turned into `_`; check them against the endpoint of the
Gerrit version you run before relying on an alert. The first and last
come from kube-state-metrics and the kubelet rather than Gerrit.
Replace the namespace and pod names with the release's.

| Condition | PromQL |
| --------- | ------ |
| Readiness flapping | `changes(kube_pod_status_ready{namespace="gerrit", pod="gerrit-0", condition="true"}[30m]) > 4` |
| JVM heap pressure | `proc_jvm_memory_heap_used / proc_jvm_memory_heap_committed > 0.9` |
| Replication queue growing | `sum({__name__=~"(?i)queue_replicateto.*_scheduled_tasks"}) > 100` |
| SSH error rate | `rate({__name__=~"sshd_sessions_authentication_failures.*"}[5m]) > 1` |
| HTTP error rate | `sum(rate({__name__=~"http_server_rest_api_error_count.*"}[5m])) > 1` |
| Site volume filling | `kubelet_volume_stats_used_bytes{persistentvolumeclaim="site-gerrit-0"} / kubelet_volume_stats_capacity_bytes{persistentvolumeclaim="site-gerrit-0"} > 0.85` |

## Maintenance

A long-running site on a fixed-size volume needs its repositories
packed and its persistent caches bounded.

### Garbage collection

Gerrit's built-in scheduler runs `git gc` over every repository at
`gc.startTime`, then every `gc.interval`: weekly at 03:00 on Sunday,
server time, by default. `gc.aggressive` turns on aggressive mode.
`receive.autogc` stays `false` in `jgit.config`, so a push never
starts a gc of its own. Set `gc.startTime` and `gc.interval` to `null`
to turn the scheduler off.

To run gc outside Gerrit's JVM instead, set `gc.cronJob.enabled`. The
two modes are exclusive: the chart then leaves `gc.startTime` and
`gc.interval` out of `gerrit.config`, and the `<fullname>-git-gc`
`CronJob` runs `files/scripts/git-gc.sh` on `gc.cronJob.schedule`,
Sunday at 03:00 by default. The script runs `git gc` with
`gc.cronJob.args`, `--auto` by default, over every repository under
`git/`. Each list entry reaches git as one argument, so
`--prune=2 weeks ago` needs no quoting:

- `etc/jgit.config` is its system git config, so the `[gc]`, `[pack]`
  and `[core]` settings Gerrit uses apply, and `gc.aggressive` adds
  `--aggressive`;
- `gc.autoDetach` is off, so `git gc --auto` finishes before the Job
  does and a failure counts against it;
- a lock on `chart-gc.lock` in each repository skips one that another
  run still holds; a lock that fails for any other reason, such as a
  volume that does not support locking, counts as a failed repository;
- a repository is a directory ending `.git` that holds `HEAD`,
  `objects` and `refs`; another directory ending `.git` with
  repositories below it is a namespace, and one without counts as a
  failed repository;
- a repository that fails does not stop the rest, and the Job fails
  after the last one.

With `--auto`, git packs a repository only once it holds more loose
objects than `gc.auto` allows; set `gc.cronJob.args` to `[]` for a
full gc every run. The Job runs as the Gerrit UID with
`concurrencyPolicy: Forbid`, mounts the site volume read-write, has no
service account token and, with `networkPolicy.enabled`, a
`NetworkPolicy` that denies it all traffic. It runs only on the node
the Gerrit pod runs on, where a `ReadWriteOnce` volume is already
attached; while Gerrit is down the Job waits in `Pending` and starts
once the Gerrit pod is back. The chart cuts the `<fullname>` prefix of
the `CronJob` name to 45 characters, within the 52 that Kubernetes
allows a `CronJob`.

### Cache limits

Gerrit keeps its persistent caches as H2 files under `cache/`:
`*.mv.db` with the H2 2.x that Gerrit 3.13 uses, and `*.h2.db` from
older releases. Once a day, at 1 AM server time, it trims each cache
over its `diskLimit`, and the limit is 128 MiB for a cache that sets
none. On a busy site the diff caches can outgrow the git data, so the
chart sets a limit for each of these caches in `cache.limits`:

| Cache                | `diskLimit` |
| -------------------- | ----------- |
| `comment_context`    | `128m`      |
| `conflicts`          | `128m`      |
| `diff_intraline`     | `512m`      |
| `diff_summary`       | `1g`        |
| `gerrit_file_diff`   | `1g`        |
| `git_file_diff`      | `1g`        |
| `git_modified_files` | `512m`      |
| `modified_files`     | `1g`        |

Each entry renders `diskLimit` into the `[cache "<name>"]`
subsection. Override one in `cache.limits`, or drop it with `null` or
`""`; a `diskLimit` set in the subsection itself, as in
`cache.git_file_diff.diskLimit`, wins over `cache.limits`. A limit of
`0` turns disk storage off for that cache.

### Purging caches and stale indexes

Trimming keeps a cache within its limit, but an H2 file never shrinks
on disk, and every index version an upgrade leaves behind stays in
`index/` as `<name>_NNNN`. To reclaim the space, stop Gerrit and run
`gerrit-cache-purge` and `gerrit-index-prune` from the
[admin toolbox](#admin-toolbox). Gerrit rebuilds the caches as it
needs them.

`index/gerrit_index.config` names the version of each index Gerrit
uses, as `[index "changes_0084"]` with `ready = true`.
`gerrit-index-prune` keeps those versions, and every version of an
index that has none marked ready, and removes each other
`index/<name>_NNNN` directory. It refuses to run without the file or
with no version marked ready: Gerrit would need an offline reindex to
rebuild a version it uses.

## Admin toolbox

`toolbox.enabled` adds the `<fullname>-toolbox` `Deployment`, at zero
replicas, on the Gerrit image. It mounts the site volume at
`/var/gerrit`, as the Gerrit pod does, and carries the Gerrit
command-line programs in `bin/gerrit.war`, `git`, `ssh`, the site
export tool as `gerrit-site-export`, and these helpers on its `PATH`:

| Helper               | Does                                                                   |
| -------------------- | ---------------------------------------------------------------------- |
| `gerrit-cache-purge` | Deletes the persistent cache files, `cache/*.h2.db` and `cache/*.mv.db` |
| `gerrit-index-prune` | Removes the `index/<name>_NNNN` versions Gerrit no longer uses         |
| `gerrit-gc-now`      | Runs `git-gc.sh` from [Garbage collection](#garbage-collection) once; its arguments go to `git gc`, and none runs a full gc |

Each helper refuses to run while the Gerrit pod exists, unless given
`--force`. The check, `gerrit-require-offline`, looks up the pod's
fully qualified name,
`<fullname>-0.<fullname>-headless.<namespace>.svc.<clusterDomain>.`.
The headless Service publishes not-ready addresses, so the name
resolves while the pod starts, serves and shuts down, and does not
exist once Gerrit is scaled to zero. An answer refuses, a definite
"no such name" proceeds, and anything else, such as a resolver
timeout, refuses. The check is a point-in-time test, not a lock, and a
DNS answer can outlive a scale-down by a few seconds: retry a refusal
shortly after Gerrit stops.

The export tool's own check that Gerrit has stopped reads `/proc`,
which in the toolbox shows only the toolbox's processes, so
`gerrit-site-export final` runs the same check first and refuses while
the Gerrit pod exists. `--i-stopped-writes` skips both checks, for
writes stopped another way; `gerrit-site-export seed` runs while
Gerrit serves.

The offline workflow:

```sh
ns=gerrit
kubectl -n "$ns" scale deploy/gerrit-toolbox --replicas=1
kubectl -n "$ns" scale sts/gerrit --replicas=0
kubectl -n "$ns" wait --for=delete pod/gerrit-0 --timeout=5m
kubectl -n "$ns" rollout status deploy/gerrit-toolbox --timeout=5m
kubectl -n "$ns" exec deploy/gerrit-toolbox -- gerrit-cache-purge
kubectl -n "$ns" exec deploy/gerrit-toolbox -- gerrit-index-prune
kubectl -n "$ns" exec deploy/gerrit-toolbox -- gerrit-gc-now
kubectl -n "$ns" exec -it deploy/gerrit-toolbox -- \
  java -jar /var/gerrit/bin/gerrit.war reindex -d /var/gerrit
kubectl -n "$ns" scale sts/gerrit --replicas=1
kubectl -n "$ns" scale deploy/gerrit-toolbox --replicas=0
```

The site volume is usually `ReadWriteOnce`, so the toolbox prefers,
but does not require, the node the Gerrit pod runs on: a required
affinity to a pod scaled to zero would leave the toolbox
unschedulable. Scale the toolbox up before Gerrit down, as above, and
it lands beside Gerrit. While Gerrit runs, a toolbox placed on
another node cannot attach the volume and stays in
`ContainerCreating`; scale Gerrit to zero and it starts, so wait for
the toolbox rollout before the first `exec`, as above. A Gerrit pod
starting on another node while the toolbox holds the volume waits in
the same way, so scale the toolbox back to zero when done.

With `toolbox.existingSecret`, the `ssh-privatekey` key of that Secret
is mounted at `/etc/gerrit-toolbox/ssh/id`, named by
`$GERRIT_SSH_IDENTITY`, for administrative commands to a running
Gerrit:

```sh
ssh -i "$GERRIT_SSH_IDENTITY" -p 29418 admin@"$GERRIT_POD_DNS" gerrit version
```

The toolbox runs as the Gerrit UID and meets the `restricted` Pod
Security Standard. It has no service account token, and the chart
renders no ServiceAccount, Role or RoleBinding for it. With
`networkPolicy.enabled`, its `NetworkPolicy` denies all ingress and
allows egress only to DNS and to port 29418 of the Gerrit pod. For
Argo CD, the `Deployment` sits in sync wave `1` and its `ConfigMap`
and `NetworkPolicy` in wave `-1`.

## Quick validation

Build the chart image from this repository's `Dockerfile` and
`Dockerfile.chart`, as `values-local-smoke.yaml` describes, then:

```sh
docker build -t gerrit-local-base:dev .
bash scripts/fetch-plugin-catalogue.sh build/plugin-catalogue
docker build -f Dockerfile.chart \
  --build-arg BASE_IMAGE=gerrit-local-base:dev -t gerrit-local:dev .
helm lint K8S/charts/gerrit -f K8S/charts/gerrit/values-local-smoke.yaml
helm template gerrit K8S/charts/gerrit --namespace gerrit-smoke \
  -f K8S/charts/gerrit/values-local-smoke.yaml
kind create cluster
kind load docker-image gerrit-local:dev
helm install gerrit K8S/charts/gerrit -n gerrit-smoke --create-namespace \
  -f K8S/charts/gerrit/values-local-smoke.yaml --wait --timeout 10m
kubectl -n gerrit-smoke port-forward svc/gerrit-http 8080 &
curl -s http://localhost:8080/r/config/server/version   # prints the release
git clone http://localhost:8080/r/All-Projects          # an empty checkout
kubectl -n gerrit-smoke delete pod gerrit-0
kubectl -n gerrit-smoke rollout status sts/gerrit       # site survives
```

`All-Projects` holds only `refs/meta/config`, so the clone succeeds
with an "empty repository" warning.

`files/scripts/verify-site.py` runs the last four checks itself, port
forwarding the Service and proving persistence with a marker project
it creates before deleting the pod. It also checks that
`<urlPrefix>/config/server/info` reports the authentication type
given with `--auth-type`, and each download scheme given with
`--download-scheme`, by the name server info reports (`ssh`, `http`,
`anonymous http`). Download schemes come from the `download-commands`
plugin, so name them only when `pluginCatalogue` installs it. The
script needs only python3, `kubectl` and `git`, and stays out of the
scripts `ConfigMap`, which takes `*.sh` alone:

```sh
python3 K8S/charts/gerrit/files/scripts/verify-site.py \
  --namespace gerrit-smoke --fullname gerrit --url-prefix /r \
  --auth-type DEVELOPMENT_BECOME_ANY_ACCOUNT
```

`values-local-smoke.yaml` enables `hooks` with a stub
`patchset-created` script that appends its arguments to
`/var/gerrit/logs/smoke-hook.log`. `files/scripts/verify-hook.py`
creates a project with a change for review over REST, which fires the
same event as a push to `refs/for/main`, and waits for the stub to log
that project. It reuses the helpers of `verify-site.py` and needs the
same development authentication:

```sh
python3 K8S/charts/gerrit/files/scripts/verify-hook.py \
  --namespace gerrit-smoke --fullname gerrit --url-prefix /r \
  --hook-log /var/gerrit/logs/smoke-hook.log
```

`files/scripts/verify-logs.py` reads the Gerrit container's output
and checks that the error log reaches it as JSON, with no line in
Gerrit's text layout and no complaint from log4j, then checks that
`logs/httpd_log` holds the probes' requests and that `logs/sshd_log`
exists:

```sh
python3 K8S/charts/gerrit/files/scripts/verify-logs.py \
  --namespace gerrit-smoke --fullname gerrit
```

With `plugins.healthcheck.enabled`, the default,
`files/scripts/verify-health.py` checks that
`<urlPrefix>/config/server/healthcheck~status` answers 200 with every
check passed or disabled, and names any that failed. It also reuses
the helpers of `verify-site.py`:

```sh
python3 K8S/charts/gerrit/files/scripts/verify-health.py \
  --namespace gerrit-smoke --fullname gerrit --url-prefix /r
```

The `chart-smoke` job in `.github/workflows/testing.yaml` runs this
sequence on every pull request. It then
upgrades the release with `gc.cronJob.enabled`, runs the CronJob once with
`kubectl create job --from` on a fixture repository of loose objects,
and checks that the Job succeeds and leaves the objects packed. It
also lints and renders the chart
with each values file under `examples/`, adding an `image.tag`. With `image.digest` set, it
parses each render with `scripts/lib/chart_digests.py` and checks
that every container, init container and ephemeral container image
ends in that digest.

`tests/chart/` holds golden tests: each values file under `examples/`
must render the `gerrit.config`, `jgit.config`, `notedb.config`,
`log4j.properties` and `healthcheck.config` in
`tests/chart/golden/<example>/`, and `templates/ingress.yaml` as a
plain Ingress and as Traefik objects in `ingress-plain.golden` and
`ingress-traefik.golden` there, and `render-config.sh` must write the
`secure.config` there with mode `0600`. With `gc.cronJob.enabled`,
`templates/gc-cronjob.yaml` must render `gc-cronjob.golden`, and with
`toolbox.enabled`, `templates/admin-toolbox.yaml` must render
`admin-toolbox.golden`. With the metrics plugin and the
`ServiceMonitor` toggle on and `monitoring.coreos.com/v1` available,
`templates/servicemonitor.yaml` must render `servicemonitor.golden`,
and without that API nothing; with `logs.retentionDays` set,
`templates/log-cleanup-cronjob.yaml` must render
`log-cleanup-cronjob.golden`; the toolbox helpers run against fixture sites
with the DNS lookup stubbed. After an intended change to
the rendered output, regenerate the goldens and review the diff:

```sh
UPDATE_GOLDEN=1 uv run --extra test pytest tests/chart
```
