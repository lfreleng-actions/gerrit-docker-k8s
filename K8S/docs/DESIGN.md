<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Gerrit on Kubernetes: Design Record

This document records why the Gerrit Helm chart (`K8S/charts/gerrit/`)
has the shape it has. Pull requests that change the chart's
architecture point here for the reasoning and update the relevant
section in place; git history records how the design evolved, so this
document carries no changelog.

It describes the chart, not any deployment. Every name below is a
placeholder: `gerrit.example.org` for the canonical host, `<tenant>`
for a namespace or organisation, `<release>` for the Helm release name
and `<fullname>` for the chart's object name prefix, which is
`<release>-gerrit`, or `<release>` alone when the release name already
contains `gerrit`. Nothing here comes from a running system.

The chart skeleton (#232, merged in #254) is the starting point. The
sub-issues of #228 are normative for the parts they specify: this
record uses their names, keys and stages, and where it differs from
one it says so and names the issue. Tracking issue: #228. Section 7
maps the remaining work onto its sub-issues.

## 1. Architecture

```text
                 HTTPS                                SSH
                   |                                   |
          +--------v---------+               +---------v--------+
          | Ingress          |               | Service          |
          | path /<prefix>/  |               | <fullname>-ssh   |
          +--------+---------+               | LoadBalancer     |
                   |                         | port 29418       |
          +--------v---------+               +---------+--------+
          | Service          |                         |
          | <fullname>-http  |                         |
          | ClusterIP 8080   |                         |
          +--------+---------+                         |
                   |                                   |
          +--------v-----------------------------------v--------+
          | StatefulSet <fullname>, replicas: 1                  |
          |  init: volume-permissions (optional)                 |
          |        site-import  (adopt a site, state on volume)  |
          |        site-init    (gerrit init, binaries, plugins) |
          |        render-config (etc/secure.config)             |
          |  main: gerrit                                        |
          |  volume: site (PVC from volumeClaimTemplate)         |
          +------------------------------------------------------+
                         ^                     ^
                         |                     |
                  backup CronJob        maintenance CronJob
                  (archive, upload)     (housekeeping)
```

### 1.1 Single primary StatefulSet

Gerrit runs as one primary in a `StatefulSet` with `replicas: 1` and
`podManagementPolicy: OrderedReady`. Gerrit's own high-availability
and multi-site setups need shared or replicated storage, a shared
index and extra plugins that are out of scope for this chart. A single
primary keeps the data model simple: one writer owns the site
directory and the git store, so ref updates, NoteDb sequences and the
index never race another process.

The StatefulSet keeps the Kubernetes default update strategy,
`RollingUpdate`. With one replica the
controller stops the old pod before it starts the new one, which is
what a `ReadWriteOnce` volume needs. The same property means at most
one pod, and so at most one `site-import` init container, ever works
on the volume, which is why the import needs no lock (section 1.7).

### 1.2 Storage: one PVC from a volumeClaimTemplate

The site directory and the git store (`gerrit.basePath`) live on a
single persistent volume claimed through the `site`
`volumeClaimTemplate`. The StatefulSet controller creates that claim,
`site-<fullname>-0`; Helm never renders it as a release object and
Argo CD never sees it in the desired state. As a result:

- `helm uninstall` and an Argo CD application deletion leave the
  claim, and the data, in place.
- A chart change cannot recreate or shrink the volume by accident.
- Deleting the data is an explicit operator action
  (`kubectl delete pvc site-<fullname>-0`).

The chart leaves `persistentVolumeClaimRetentionPolicy` at its
Kubernetes default, `Retain` for both `whenDeleted` and `whenScaled`.

The official Gerrit image declares `cache`, `db`, `etc`, `git` and
`index` under `/var/gerrit` as image volumes. The `gerrit` and
`render-config` containers mount the claim at `/var/gerrit` and each
of those directories from it by `subPath`; without that, the container
runtime mounts empty anonymous volumes over them and the site
disappears. `site-init` mounts the claim at `/mnt/site` so that the
image's own `/var/gerrit/bin` stays visible to copy from.

### 1.3 Start-up sequence

Every start of the Gerrit pod runs these containers in order, and each
must succeed before the next starts:

1. `volume-permissions`, only with
   `persistence.volumePermissions.enabled`: chowns anything on the
   volume the `gerrit` user (UID 1001) does not own, for provisioners
   that ignore `fsGroup`.
2. `site-import`: adopts an existing site from an archive, or records
   that the site starts fresh, and keeps its state on the volume
   (section 1.7).
3. `site-init`: copies `gerrit.war` and `gerrit.sh` from the image,
   replaces the site's plugin and library jars with those named in
   `pluginCatalogue`, and installs the rendered `gerrit.config`,
   `jgit.config`, `notedb.config`, `log4j.properties`, with
   replication enabled `replication.config`, with its-jira enabled
   `etc/its/actions-its-jira.config`, with healthcheck enabled
   `etc/healthcheck.config`, and with events-log enabled
   `etc/events-log.config` (section 1.4). On a site without
   `.chart/initialised` it then runs
   `gerrit init --batch --no-auto-start` on that configuration,
   installs the rendered files again over whatever init added, and
   only then writes that marker, with the Gerrit version and the time,
   so a restart never re-initialises a site and one after an init that
   failed part way, even past creating `All-Projects`, runs it again.
   Right after the marker it creates the backup lock (section 1.8).
   With `hooks.enabled` it then resolves the hooks directory, and
   fails when a link on the site, such as an imported `alias -> .`,
   leads it out of the site, under a hidden or reserved top-level
   directory, or under where a reserved directory's link leads, or
   when it holds a link a reserved directory resolves through, such
   as `hooks/store` in `data -> hooks/store -> ../store`; it then
   replaces the files at the top of the hooks directory with the
   `hooks.scripts` entries and, with `hooks.g2p.enabled`, links to the
   gerrit-to-platform entry points in `/opt/gerrit-tools/bin`, and
   installs `gerrit_to_platform.ini` from its Secret with mode `0600`.
4. `render-config`: writes `etc/secure.config` from the Secrets
   (section 1.4).
5. `gerrit`: runs `bin/gerrit.sh run`. `container.javaOptions` names
   the chart's `etc/log4j.properties` with `-Dlog4j.configuration`, so
   Gerrit skips its built-in loggers: the error log goes to the
   container's stderr as JSON, one object per line, and appenders named
   `httpd_log` and `sshd_log` keep the request logs in `logs/` on the
   site volume. A startup probe allows ten minutes for
   `<prefix>/config/server/version`. The readiness probe calls
   `<prefix>/config/server/healthcheck~status` with
   `plugins.healthcheck.enabled`, and the version endpoint otherwise,
   so a failing check takes the pod out of service; the liveness probe
   always calls the version endpoint, so a failing check never
   restarts Gerrit.

An init container that fails stops the sequence, so Gerrit never
starts on a site that an import left half-written or a configuration
step could not complete.

### 1.4 Configuration rendered from values and Secrets

The volume holds state only. Configuration comes from chart values and
Secrets in two stages, as #234 specifies:

- **`gerrit.config` from Helm.** Helm renders the complete
  `gerrit.config` from `files/conf/gerrit.config.tpl` into the
  `<fullname>-config` ConfigMap, beside `jgit.config` and
  `notedb.config`, so the output of `helm template` is the
  configuration Gerrit runs with and the golden tests in `tests/chart/`
  compare it without a cluster. `site-init` replaces the files on the
  volume from the ConfigMap at every start. Typed values cover the
  settings the chart reasons about, one top-level value per
  `gerrit.config` section; `gerrit.extraConfig` is appended verbatim
  for the rest.
- **Plugin configuration files from Helm.** The same ConfigMap carries
  the files of the plugins the chart configures: `replication.config`,
  `its/actions-its-jira.config`, `healthcheck.config` and
  `events-log.config`, each only while its plugin is enabled.
  `site-init` owns each of them: it replaces the site's copy at every
  start and removes it once the plugin is disabled, so a stale or
  imported file never configures a plugin. It removes the site's entry
  before copying, so an imported relative symlink in its place, such
  as `etc/events-log.config` linked to `gerrit.config`, is replaced
  rather than written through. `events-log.config` points
  the plugin's store at the H2 database `data/<storeFile>` under the
  site, from `plugins.events-log.storeFile`: values name the file, one
  path component, but never the store's location or credentials,
  which includes a `urlOptions` entry naming a user or password. The
  default, `db`, is the plugin's own, so a site that ran on it keeps
  its history; an imported file typically names the source site's
  absolute path and often another file, such as `events-log-db`, and
  the `site-import` migration report names that file and, when its
  `storeUrl` names the exported site's own `data/`, at the site path
  the export's manifest records, or `/var/gerrit` for a `backup`
  manifest from the backup CronJob's `/mnt/site`, with no `..`
  component, and the archive holds its database there, the
  `storeFile` that keeps its history, since `site-init` replaces the
  imported file at every start.
- **`secure.config` from Secrets.** The `render-config` init
  container writes `etc/secure.config` at every start from individual
  environment variables, each filled from one Secret key with
  `secretKeyRef`. It creates the file empty with mode `0600` before
  writing any value into it, and replaces the whole file, so a value
  removed from a Secret leaves the site too. No Secret holds a whole
  `secure.config`; section 5 lists the keys.

A value change therefore takes effect on the next rollout, and nobody
edits configuration on the volume by hand. Configuration that an
import brings across is read once, compared with the values and
reported (section 4); the rendered files replace it, with the one
exception below.

`gerrit.serverId` is not configuration but site identity. Gerrit
records it in the change notes it writes and refuses to load changes
stamped with an ID it does not recognise
([config-gerrit: gerrit.serverId][gc-serverid]), so a render that
drops or changes it makes every existing change unreadable. This
record therefore treats `gerrit.serverId` as a value no render may
change, and:

- `site-import` reads `gerrit.serverId` from the archive's
  `site/etc/gerrit.config` and fails, before it changes the volume,
  when it differs from the value;
- on a fresh site, the rendered `gerrit.config`, value included, is
  in `etc/` before `gerrit init` runs, so the site takes the value as
  its ID instead of generating one;
- `site-init`, which installs the rendered `gerrit.config`, fails, and
  Gerrit does not start, when the site already holds a
  `gerrit.config` whose `gerrit.serverId` differs from the value, so
  no later render can change the ID either.

The value may be empty, so that a new site needs no ID chosen in
advance: Gerrit then generates one, and `site-init` reads the site's
ID before each render replaces the file and carries it into the
installed `gerrit.config`. A site `gerrit.config` it cannot read or
parse stops `site-init`, rather than reading as a site with no ID. An
import of an existing site sets the value from the source.

The rendered `gerrit.config` sets `gerrit.basePath` to the git store on
the volume, `gerrit.canonicalWebUrl` to
`<scheme>://<gerrit.host><gerrit.urlPrefix>/` (for example
`https://gerrit.example.org/r/`), and `httpd.listenUrl` to
`<scheme>://*:8080<gerrit.urlPrefix>/` (for example
`proxy-https://*:8080/r/`), so that Gerrit builds links with the
prefix the Ingress serves. Behind the chart's Ingress the schemes are
`https` and `proxy-https` with `ingress.tls.enabled`, and `http` and
`proxy-http` without; with no Ingress they are `gerrit.webScheme` and
`gerrit.listenScheme`
([config-gerrit: httpd.listenUrl][gc-listenurl]).

**Authentication is an explicit choice.** The chart has no default
`auth.type`: the render fails until values enable SAML, set another
Gerrit auth type, or set `DEVELOPMENT_BECOME_ANY_ACCOUNT` together with
`auth.allowDevelopmentAccounts: true` (#299). Production sites are
migrations of existing SAML sites, so they enable SAML; the Argo CD
example does. Development accounts let anyone sign in as any account,
the administrator included, so they stay behind that opt-in for CI,
such as the kind smoke test, and throwaway sandboxes, and the chart
refuses the opt-in together with SAML. It also refuses an `[auth]`
section in `gerrit.extraConfig` (#302): Gerrit reads the last value of
a repeated key, so a `type` there would replace the chosen type and
bypass the opt-in. Every `[auth]` setting has a route through `auth`
values, so the chart refuses the section header, whatever follows,
rather than trust a scan for `type` to match Gerrit's parser. Plain
`HTTP` and `HTTP_LDAP`
trust identity headers from whatever sits in front of Gerrit, so they
are safe only behind an authenticating proxy that strips those headers
from client requests; the chart's Ingress does not. SAML is safe there
because its filter discards client-supplied `X-SAML-*` headers.

**Group membership from the SSO.** With SAML and
`auth.saml.memberOfAttr` set, Gerrit takes group membership from the
SSO: the SAML module creates a `saml/<name>` group for each group the
assertion names and updates the user's membership at each web login
(#268). Access rules on the adopted sites name these groups, so an
import keeps them and the SSO must keep sending the same group names.
Gerrit is unaffected by changes to the identity provider's source of
group membership, as long as the group names it sends stay the same.

### 1.5 HTTP: ClusterIP Service behind an Ingress

HTTP reaches Gerrit through an Ingress and the `ClusterIP` Service
`<fullname>-http` on port 8080. The Ingress serves the path prefix in
`gerrit.urlPrefix` (`/r` by default, `""` for the root) and does not
rewrite it: Gerrit expects the prefix in the request path when
`httpd.listenUrl` carries one. TLS ends at the Ingress controller,
which is why `httpd.listenUrl` uses `proxy-https`. The headless
Service `<fullname>-headless` governs the StatefulSet and carries no
client traffic.

With `ingress.enabled` the chart renders a `networking.k8s.io/v1`
`Ingress` with `pathType: Prefix` on the prefix, and with
`ingress.tls.issuer` the cert-manager annotation that issues its
certificate. With `ingress.traefik.enabled` it renders a Traefik
`IngressRoute` instead, with `Middleware`s for the redirect from `/`
to the prefix and for security headers, and a cert-manager
`Certificate`. The chart README covers both and the controller
settings a large `git push` needs.

### 1.6 SSH: LoadBalancer Service

Gerrit's SSH daemon (port 29418) speaks a protocol an HTTP Ingress
cannot route, so it gets its own Service, `<fullname>-ssh`, rendered
when `ssh.service.enabled` is set (the default); `ssh.service.type`
defaults to `LoadBalancer`. `sshd.advertisedAddress` is `ssh.host`,
or `gerrit.host`, without any port, followed by the SSH port:
`ssh.port`, else `ssh.service.nodePort` on a `NodePort` Service, else
29418; a `NodePort` Service with neither fails to render. The Service
sets `externalTrafficPolicy: Local` (`ssh.service.externalTrafficPolicy`)
when its type is `LoadBalancer` or `NodePort`, so that Gerrit's SSH
log records the client address rather than a node's. With
`ssh.service.externalDNS` it carries the external-dns hostname
annotation for `ssh.host`, which then has to differ from `gerrit.host`
when the Ingress is enabled.

### 1.7 Site import

The `site-import` init container adopts an existing site, from an
archive in the layout of section 3, before Gerrit starts, as #240
specifies. It runs in the Gerrit pod, so it mounts the claim the
StatefulSet already owns: there is no import Job, no Helm or Argo CD
hook, no Lease, no marker Secret and no Kubernetes API access. One
replica and a `ReadWriteOnce` volume mean only one `site-import` runs
at a time (section 1.1).

`siteImport.source.type` selects where the archive comes from, or
`none` for a site that starts fresh. The archive's credentials come
from a Secret (section 5). The chart takes the `--pack` export of
section 3 from three sources: `pvc`, an export directory on a volume
mounted read-only at `/mnt/import`; `https`, a base URL or, from the
Secret, one URL per file, such as a pre-signed object-store URL; and
`s3`, an `s3://<bucket>/<prefix>` the chart image's rclone fetches.
rclone is configured only through the environment, and reads keys
from the Secret or the pod's workload identity, such as IRSA through
`serviceAccount` (section 1.11). A prefix ending in `/latest` names
the newest backup of section 1.8: `site-import` reads that object once
and pins the prefix to the timestamp it holds, so every file comes
from one backup. Decrypting `.age` or `.gpg` archives
is still to come (section 7); until then the chart refuses them.

**State file.** `site-import` records its state in
`<site>/.chart/site-state.json` on the volume, and writes it
atomically: it writes a temporary file in the same directory and
renames it over the old one, so a reader sees the old state or the
new, never a torn file. Its fields, with `schema: 1`:

- `state`: `in-progress`, `complete` or `failed`;
- `manifestSha256`: the SHA-256 of the imported archive's
  `manifest.json`, or `null` for a site that started fresh with
  `siteImport.source.type: none`, which is a valid completed state;
- `forceToken`: the `siteImport.forceToken` value of the run that
  wrote the state;
- `source`: `none`, or the source type and URL as `<type>:<url>`;
- `gerritVersion`: the version of the image's `gerrit.war`;
- `updated`: when the state was written, in UTC;
- `reason` on `failed`, and `warnings`, the migration report, on
  `complete`;
- `adopted: true` on a site an earlier chart version created, which
  `site-import` recorded as `complete` without importing it.

**Writes in order.** Before it changes anything on the volume,
`site-import` writes `in-progress`. It fetches, unpacks and checks
the archive in a separate scratch `emptyDir` volume, as #240
specifies, and empties that scratch area first, so no attempt builds
on what an earlier one left behind. The archive and its unpacked copy
never sit on the site volume: the claim only has to hold the restored
site and `.chart/`, and staging there could exhaust a claim otherwise
large enough for the site. The scratch volume comes from the node's
ephemeral storage, so it must have room for the archive and its
unpacked copy at once. `site-import` writes `complete` only
after the import has passed the checks of section 4 that it can run.
Any failure writes `failed` and exits non-zero, so the pod's sequence
stops and Gerrit never starts. An unexpected error counts too: its
type and message become the `reason`, and its traceback goes to the
log, never to the state file. A pod killed mid-import leaves
`in-progress` behind. No earlier `complete` state survives a re-import
to let Gerrit start on a partly restored site.

**The state directory is never imported.** As #240 specifies,
`site-import` never takes `.chart/` from an archive: it skips the
archive's `site/.chart/` while unpacking, in every mode, `auto`
included, and leaves the destination's `.chart/` as it is. An archive
of a chart-managed site, such as a backup (section 1.8), can carry
that site's `complete` state file; were it unpacked over this run's
`in-progress` state, a pod killed afterwards would restart, find
`complete` and start Gerrit on an unfinished restore. Only the running
`site-import` writes `<site>/.chart/site-state.json`.

**Modes.** `siteImport.mode` selects the behaviour; the `force` row
applies to a new token, as described below:

| Mode    | No state file                                                               | `complete`                 | `in-progress` or `failed`                  |
| ------- | --------------------------------------------------------------------------- | -------------------------- | ------------------------------------------ |
| `auto`  | Import, or with `none` record a fresh site or adopt a chart-created one; fail if the volume holds another site | Do nothing | Retry the import from a clean scratch area |
| `force` | Import, replacing everything on the volume but `.chart/`                    | Import, replacing the site | Import, replacing the site                 |
| `skip`  | Do nothing                                                                  | Do nothing                 | Do nothing                                 |

`auto` fails closed on a volume that holds a site no state file
accounts for, since the chart did not put that data there; an operator
decides, by repairing the volume or by forcing an import. One case is
the chart's own: every release installed from chart `0.6.0` or `0.6.1`
holds a site with no state file. With `auto` and source `none`, a
volume whose `etc/gerrit.config` exists and whose git store, at the
chart's `gerrit.basePath`, holds the `All-Projects` repository that
file names is such a site, and `site-import` adopts it: it writes
`complete` with `manifestSha256: null` and `adopted: true`, logs it and
lets the start continue, so a chart upgrade does not stop the
release. Any other unaccounted content still fails closed. An
`in-progress` or `failed` state is one `site-import` wrote itself, so
`auto` retries that import on every pod start, from a clean scratch
area, until it writes `complete`. Until then Gerrit stays blocked: each
failed retry writes `failed` and exits non-zero again, and the pod
restarts with back-off.

`force` replaces the site only for a `siteImport.forceToken` that is
not empty and differs from the `forceToken` in the state file. An
empty token is an error: `site-import` writes nothing, exits non-zero
and Gerrit does not start, so clearing a token can never authorise a
destructive import. A token equal to the recorded one is not a new
request, and `force` then acts as `auto`, so a pod restart never
repeats a completed forced import and an interrupted or failed one is
retried as `auto` retries it. Because the import runs in the
Gerrit pod's own init sequence, Gerrit is never running while it does.

With `siteImport.source.type: none` and an empty volume, `site-import`
writes `complete` with `manifestSha256: null`, and `site-init` then
initialises the site. With `none` and an `in-progress` or `failed`
state there is nothing to retry from, so `site-import` refuses rather
than start an empty site over a failed import.

**Import steps.** With the checksums matching the manifest and, when
set, `siteImport.source.expectedManifestSha256` matching the manifest
itself, `site-import` unpacks the archives in the scratch area. It
refuses an archive with a symlink or hard link that resolves outside
its own `site/` or `git/` tree, or into a path it never imports such
as `.chart/`, and checks every symlink again once all are in place,
so no link can lead a later write to the state file or off the
volume. It never takes the archive's `site/bin/`, `site/plugins/` or
`site/lib/`: `site-init` installs the image's war and the chart's
plugins and libraries, and a jar built for the source release, such
as a hidden `plugins/.jar` an official image left behind, can stop
the new release starting. Nor does it take `site/.ssh/` or
`site/.config/gerrit_to_platform/`, which a site that is also the
service account's home holds, logging once for each that it left it
out: they carry the replication plugin's private key and the
gerrit-to-platform token, which the chart supplies from Secrets
(`replication.sshKeySecret`, `hooks.g2p.existingSecret`), so an
imported copy is never needed. It refuses a symlink on the path to a
skipped directory, such as `site/.config` or a link that leads to
it, which would otherwise bring that directory's contents in under
another name. It refuses any other link at or in a skipped directory,
such as `site/.ssh` linked to `site/keys` or `/srv/gerrit/keys`, which
could bring credentials in under another name. In `site/.ssh/` it
accepts only links that provably stay inside, as a projected Secret's
do: a hard link within the directory or a relative symlink below its
top, either with no `..` component. In
`site/.config/gerrit_to_platform/` it accepts only the
`replication.config` link to `/var/gerrit/etc/replication.config`
that `site-init` makes, since any other link there could reach it. It leaves
out, and logs, the absolute links `site-init` recreates at every
start: a link in the site's
`hooks.path` to the gerrit-to-platform entry point of the same name in
`/opt/gerrit-tools/bin`. It refuses any other absolute link. With
`siteImport.verify` it checks the unpacked copy there, before the site
volume changes: the repositories are those the manifest lists, each
with its ref count and `refs/meta/config` head, and the SSH host keys
hash to the manifest's fingerprints. A `gerrit.serverId` that differs
from a set value always fails the import, as does a `gerrit.allProjects`
or `gerrit.allUsers` that differs from the chart's: the chart's
`gerrit.config` replaces the archive's, so `init` would create empty
repositories under the chart's names. With SAML enabled, the keystore
the archive's `saml.keystorePath` names must match the Secret's
(section 4); a path outside the site, or one other than
`etc/samlKeystore.jks` naming no file in the archive, fails the
import. It then moves the
copy onto the volume, runs `site-init` in a prepare mode that installs
the chart's configuration and returns, runs
`gerrit init --batch --no-auto-start` and the prepare mode again, and
with `siteImport.reindex` runs a full `gerrit reindex`, with
`--threads` set from the container's CPU limit when it has one, and
then writes `.chart/initialised`, which it removed before it began. Both
get the Gerrit container's heap: `container.heapLimit`, else 70% of
the container's memory limit. `init` may migrate
NoteDb and rewrite `refs/meta/config`, so after it `verify` checks
only that every repository the manifest lists is present, that the
exported SSH host keys are unchanged and that the server ID survived.
It compares no refs or `HEAD`s after init. The full comparison against
the manifest, in which a project repository must match its manifest
entry in every key the entry has, `head` included, is the separate
`verify-import.py`, which an operator runs against the imported
release from a host with `kubectl` access to it, given the source
site's REST snapshot (`tests/k8s/build_fixture_site.py` writes one as
`expected.json`) and its administrator's password:

```bash
python3 K8S/charts/gerrit/files/scripts/verify-import.py \
    --namespace <namespace> --fullname <fullname> \
    --manifest export/manifest.json --expected expected.json \
    --password-file password
```

### 1.8 Backup and maintenance CronJobs

- The **backup CronJob** (#242), `backup.enabled`, produces an archive
  in the layout of section 3 from the running deployment and uploads it,
  so a backup is also an import source for a new deployment. Runs are
  serialised by a `flock` on `.chart/backup-lock/lock` on the site
  volume, which `site-init` creates on every start right after it finds
  or writes `.chart/initialised`, and the backup opens read-only through
  its read-only mount of the volume: every backup pod shares the Gerrit
  pod's node and volume, so the lock covers manual Jobs too, and one
  that finds it held, or missing, skips; a site mid-initialisation has
  no lock yet. It runs only while `.chart/site-state.json` records
  `complete`, and discards
  an export during which that file changed, so a backup never overlaps
  an import. It runs the export tool's `backup` mode on the site volume,
  mounted read-only, reading the git store `gerrit.basePath` names on
  that volume (an absolute one must lie under `/var/gerrit`), uploads the archives and then `manifest.json` with
  rclone to `s3://<bucket>/<prefix>/<UTC timestamp>-<run id>/`, checks
  them with `rclone check`, reads its own `manifest.json` back and
  checks its archives again, and only then writes that prefix to
  `<prefix>/latest`, then a `published` marker recording `publishedAt`
  to the microsecond. The Job's `activeDeadlineSeconds`,
  `backup.maxRunHours`, bounds every run.
  Retention counts only published backups, in `publishedAt` order (the
  chart README's "Backups" section defines them): it keeps the one
  `latest` names, the newest published backup of each of the last
  `retention.keepDaily` days and `retention.keepWeekly` ISO weeks, and
  every published backup whose successor, the next published strictly
  after it, is missing or was published
  less than `retention.minAgeHours` ago, at least 1, so a restore from
  `latest` has at least that long to finish once its backup stops
  being the newest. Unpublished uploads, which only an interrupted run
  leaves, are deleted once they are more than `maxRunHours` + 1 hours
  old. A
  site import from `s3://<bucket>/<prefix>/latest` reads `latest` once
  and fetches every file from the backup it names, which is the restore
  path. Its own `NetworkPolicy` allows egress to DNS, 443 and the
  endpoint's port only.
- The **maintenance CronJob** runs housekeeping that Gerrit does not
  schedule itself, where the chart enables it. The first is the
  optional git gc CronJob (#243), `gc.cronJob.enabled`: it replaces
  Gerrit's built-in gc scheduler, which the chart turns off in
  `gerrit.config` while the CronJob is enabled. The second is the
  optional log cleanup CronJob (#244), rendered when
  `logs.retentionDays` is above zero: it deletes the rotated logs
  under `logs/` older than that many days, and leaves the logs Gerrit
  still writes alone. It has its own deny-all `NetworkPolicy`, as the
  gc CronJob does.

Both need the site volume, so they run on the node that holds it.

### 1.9 Admin toolbox

An optional `Deployment`, scaled to zero replicas by default, carries
the Gerrit command-line programs, `git`, `ssh` and the export tool. An
operator scales it to one for interactive work, such as an offline
`reindex` against a stopped site, and back to zero afterwards.

The toolbox (`toolbox.enabled`) mounts the site volume with a
preferred, not required, affinity to the Gerrit pod's node: its
helpers run while Gerrit is scaled to zero, and a required affinity to
an absent pod would leave it unschedulable. The helpers,
`gerrit-cache-purge`, `gerrit-index-prune` and `gerrit-gc-now`, refuse
to run while the Gerrit pod exists. They look up the pod's name under
the headless Service, which publishes not-ready addresses, so the name
exists exactly while the pod does; only a definite "no such name"
lets them proceed, and `--force` overrides a refusal. The toolbox has
no service account token or Role (section 1.11), and its own
`NetworkPolicy` allows egress only to DNS and to the Gerrit pod's SSH
port.

### 1.10 Network policies

With `networkPolicy.enabled` (the default), the chart installs a
default-deny `NetworkPolicy` for ingress and egress on the Gerrit pod,
then allows:

- ingress to port 8080 (HTTP), from the namespaces
  `networkPolicy.ingressNamespaceSelector` selects when it is set,
  such as the Ingress controller's, and from any source otherwise;
  with that selector and `plugins.metrics-reporter-prometheus.enabled`
  set, also from the namespaces
  `networkPolicy.metricsNamespaceSelector` selects, for the
  Prometheus that scrapes the metrics plugin;
- ingress to port 29418 (SSH) from any source;
- egress to cluster DNS, UDP and TCP port 53.

**Current default.** The skeleton (#254) also allows egress to TCP
port 443 at any address, so that site import and backup can reach an
object store. That rule is broader than the per-feature list below:
it admits any HTTPS endpoint, including the Kubernetes API wherever
the cluster serves it on port 443.

**Per-feature egress.** The design is that egress beyond DNS is per
feature: each rule exists because a feature the chart supports needs
it, and the list grows with the features. The mail rule is rendered
today: with `sendemail.smtpServer` set and `sendemail.enable` not
`false`, the chart allows TCP egress to `sendemail.smtpServerPort`,
or to Gerrit's default of 465 with `smtpEncryption: ssl` and 25
otherwise. So is the replication rule: with `replication.enabled`, the
chart allows TCP egress to the port of each URL in each remote's `url`
and `adminUrl`, or to its scheme's default (22 for SSH). So is the
SAML rule: with `auth.saml.enabled` and `auth.saml.metadataUrl` set,
the chart allows TCP egress to the port that URL names, or to 80 for
`http://`; an `https://` URL on the default port needs no rule beyond
TCP 443. So is the Jira rule: with `plugins.its-jira.enabled`, the
chart allows TCP egress to the port of `plugins.its-jira.url`, with
the same defaults. So is the site import rule: with
`siteImport.source.type: https`, the chart allows TCP egress to the
port of `siteImport.source.url` and to each port in
`siteImport.source.egressPorts`, which covers the URLs a Secret holds;
with `s3`, to the port of `siteImport.source.s3.endpoint`.

| Feature | Egress | Rendered when |
| ------- | ------ | ------------- |
| Site import and backup from an object store | HTTPS, TCP 443 | `siteImport` or `backup` reads or writes an object store |
| Site import over HTTPS | The port of `siteImport.source.url`, and `siteImport.source.egressPorts`; TCP 443 for an `https` URL on the default port | `siteImport.source.type: https` |
| Site import from S3 | The port of `siteImport.source.s3.endpoint`; TCP 443 for AWS or an `https` endpoint on the default port | `siteImport.source.type: s3` |
| SAML identity provider metadata | The port of `auth.saml.metadataUrl`; TCP 443 for an `https` URL on the default port | `auth.saml.metadataUrl` is set (#235) |
| Jira issue tracker | The port of `plugins.its-jira.url`; TCP 443 for an `https` URL on the default port | `plugins.its-jira.enabled` |
| Mail | The SMTP relay's port | Mail is configured |
| Replication | SSH or HTTPS to the replication targets | Replication is configured |
| Anything else | `networkPolicy.extraEgress`, a list of `NetworkPolicyEgressRule` objects appended as given | The operator sets it |

The list is not exhaustive: an operator adds a destination it does
not cover through `networkPolicy.extraEgress`, which the skeleton does
not render yet. That value can only add rules. `NetworkPolicy` allow
rules are additive, so a narrower rule next to the TCP 443 rule
changes nothing, and no value can restrict the default. A
`NetworkPolicy` also matches addresses and ports, not host names, so a
feature's rule cannot name the host in its URL.

Narrowing the default to the per-feature destinations, and excluding
the Kubernetes API, which no pod needs (section 1.11), is follow-up
chart work under #228. Until then, a deployment that needs tighter
egress enforces it outside the chart.

### 1.11 Pod Security and API access

The Gerrit pod runs as UID and GID 1001 with `fsGroup` and
`fsGroupChangePolicy: OnRootMismatch`, `runAsNonRoot`, a read-only
root filesystem, `allowPrivilegeEscalation: false`, every capability
dropped and `seccompProfile: RuntimeDefault`, so it meets the
Kubernetes `restricted` Pod Security Standard. The namespace carries
the `pod-security.kubernetes.io/enforce: restricted` label
(section 6.3).

The one exception is `volume-permissions`, which runs as root with
`CHOWN` and `DAC_OVERRIDE`. It stays within `baseline`, so a values
file enables it only where the namespace allows `baseline` or less,
as in local smoke tests.

The pod sets `automountServiceAccountToken: false`. Nothing in it
talks to the Kubernetes API: `site-import` keeps its state on the
volume (section 1.7), so the chart renders no Role or RoleBinding.
`serviceAccount` can give the pod a ServiceAccount, created by the
chart with `automountServiceAccountToken: false` or named, only so a
workload identity such as IRSA can bind a cloud role to it for the
`s3` import source; IRSA projects its own token for that.
The current egress default does not yet block the API on port 443
(section 1.10).

## 2. Adopting an existing site

### 2.1 Filesystem import is the complete path

Gerrit keeps its state in two places: the site directory and the git
store. Copying both at the filesystem level, with Gerrit stopped,
carries everything Gerrit knows. The new deployment then starts on the
same data, and Gerrit's own `init` and `reindex` programs handle any
schema or index migration ([pgm-init][pgm-init],
[pgm-reindex][pgm-reindex]).

### 2.2 Why replication-based mirroring is not

Mirroring the old server's repositories into a freshly initialised
Gerrit with the replication plugin looks attractive, because the old
server keeps running. It cannot produce a faithful copy, because much
of Gerrit's state is either outside git or inside refs that a live
target owns:

| State | Where Gerrit keeps it | Why replication cannot carry it |
| ----- | --------------------- | ------------------------------- |
| Global access rights | `All-Projects:refs/meta/config` ([access control][acl]) | The target's `init` creates its own `All-Projects` with its own administrator. Pushing the source's ref over it replaces the target's access rules with ones whose group UUIDs name the source's groups, displacing the local administrator; not pushing it loses the source's rules. |
| Change, account and group sequences | `refs/sequences/*` in `All-Projects` and `All-Users` ([NoteDb][notedb]) | The target allocates numbers from these refs as soon as anyone uses it. Copying them while it does risks handing out a number twice; skipping them lets new changes collide with imported ones. |
| SSH host keys | `etc/ssh_host_*_key` ([config-gerrit: sshd][gc-sshd]) | Files in the site directory, not refs. A new key changes the fingerprint every client has pinned in `known_hosts`. |
| Secrets | `etc/secure.config` ([config-gerrit: secure.config][gc-secure]) | A file in the site directory, not a ref. |
| Reviewed-file flags | The `db/account_patch_reviews` H2 database by default ([config-gerrit: accountPatchReviewDb][gc-apr]) | A database, not a ref. Every reviewer's "reviewed" marks are lost. |
| Plugin data | `data/<plugin>/` ([plugin data directory][plugin-data]) | Files in the site directory, not refs. |
| Hidden projects | `project.state = HIDDEN` ([project configuration][project-state]) | Visible only to the project's owners. A replication remote that sets `authGroup` replicates only what that group can see, so hidden projects fall out of the copy ([replication configuration][replication]). |
| Hooks configuration | The `[hooks]` section of `etc/gerrit.config` and the scripts under `hooks/` ([hooks plugin][hooks]) | Configuration and executables in the site directory, not refs. |

Replication remains the right tool for what it was built for: keeping
mirrors of repositories current. The chart configures it for that, and
section 4 checks that it resumes after an import; it is not an
adoption path.

## 3. Archive layout and manifest

The export tool, `scripts/gerrit-site-export.py` (#239), defines the
archive. The `site-import` init container (#240) reads it and the
backup CronJob (#242) writes it. This section describes what the tool
produces; where the tool and this section disagree, the tool is right
and this section needs a fix.

### 3.1 Directory layout

An export directory `<out>` holds:

```text
<out>/
  site/            the site directory, minus the excluded paths below
  git/             the repositories from gerrit.basePath
  manifest.json    written last; its presence marks a complete export
  site.tar.zst     --pack only: tar of site/, zstd-compressed, mode 0600
  git.tar.zst      --pack only: tar of git/, zstd-compressed, mode 0600
```

- `site/` leaves out `cache`, `index`, `logs`, `tmp`, `bin`,
  `.gerritcodereview/tmp`, `.chart`, `.ssh` and
  `.config/gerrit_to_platform`, plus the repository store when it
  sits inside the site. `--include-cache` keeps `cache`. The import
  rebuilds the index and caches and takes `bin/gerrit.war` from the
  image. `plugins/` and `lib/` stay in the export, for the record;
  `site-import` leaves them out (section 1.7).
- `.chart/` holds a chart-managed site's import state, which describes
  the source release rather than the export. `site-import` skips
  `site/.chart/` whatever the archive holds (section 1.7); leaving it
  out of the export as well keeps archives free of it.
- `.ssh/` and `.config/gerrit_to_platform/` appear at the top of a
  site that is also the Gerrit service account's home directory. They
  hold outbound credentials, the replication plugin's private key and
  the gerrit-to-platform token, which the chart supplies from Secrets
  (`replication.sshKeySecret`, `hooks.g2p.existingSecret`), so no
  export carries them in any mode, `backup` included. The export
  stops before writing when a credential could leave under another
  name, and follows the rule `site-import` applies (section 1.7): it
  accepts only links that cannot leave the directory, since where an
  absolute or `..` link leads depends on where the volume is mounted,
  and the backup Job mounts it at `/mnt/site`, not Gerrit's
  `/var/gerrit`. It stops on a symlink at either directory or on its
  path, such as `.config`; in `.ssh/` on any link but a relative
  symlink with no `..` component, as a projected Secret's are; in
  `.config/gerrit_to_platform/` on any link but `site-init`'s
  `replication.config` link to `/var/gerrit/etc/replication.config`;
  on a hard-linked file in either; and on a repository store that
  holds or sits in either directory, as with `gerrit.basePath = .`.
  `site-import` also skips both, so archives written before this rule
  are safe to import, and refuses links that would bring their
  contents in under another name (section 1.7).
- `git/` holds every repository under `gerrit.basePath` at the same
  relative path. A symlink inside the repository store stops the
  export.
- Each archive's members start with `site/` or `git/`, so unpacking
  both into one directory recreates the layout above.
- `--encrypt age:<recipient>` or `--encrypt gpg:<key-id>` replaces each
  archive with `site.tar.zst.age` and `git.tar.zst.age` (or `.gpg`)
  and deletes the plaintext archive.
- `--upload s3://<bucket>/<prefix>` removes the old `manifest.json`
  at that prefix, then copies the archives and the new
  `manifest.json` there with `aws s3 cp --sse aws:kms`.

The tool has three modes. `seed` copies while Gerrit runs and can
repeat; `final` refuses to run while Gerrit serves the site and copies
with `--checksum`; `backup`, which the backup CronJob (#242) runs,
copies a running site and always packs. Only `final` and `backup` may
pack, encrypt or upload. All three write a manifest.

`backup` copies the repositories through Git, as `rsync` cannot copy
a repository Gerrit writes to: a `pack-refs` or a `gc` during the copy
can hide refs or remove objects a copied ref names, and `rsync` would
pick up the lock files Git writes meanwhile. `rsync` copies the
store minus every `*.git` directory, then `scripts/gerrit_export_git.py`
runs `git bundle create --all` on each repository, fetches
`+refs/*:refs/*` from the bundle into a new bare repository and copies
`config`, `description`, `info/` and `HEAD`; no remote is ever
added. A detached `HEAD` stays detached at the same commit, which the
copy fetches from the source as well, since no ref need reach it; an
unborn one is left as `git init` makes it. Every mode sets umask `0077` before it writes anything,
makes the bundle in a `0700` directory from `tempfile.mkdtemp` under
`--out`, has `rsync` drop group and other permissions, and runs git
with `core.sharedRepository=umask`, so a copied `config` that shares a
repository with a group keeps that setting without widening the copy
written after it. All its output is owner-only: a bundle or a repository copy holds the full
history, and a `config` may hold credentials. `git rev-list --objects
--all` must find every object the copy's refs reach, its
`for-each-ref` must match the bundle's refs, and `HEAD` must read the
same before the bundle and after the copy, or the run fails before it
packs anything: a default branch made after the bundle would leave
the copy's `HEAD` dangling. (`git fsck` would
also reject a valid `HEAD` outside `refs/heads/`, as `All-Projects`'
is.) Git builds the bundle from one ref snapshot and
packs the objects it reaches as it reads them, so a concurrent `gc`
either leaves those objects or makes the bundle step fail. Each
bundle is consistent only within its repository, and Gerrit reserves
change numbers from `refs/sequences/changes` in `All-Projects` before
it creates a change in any repository. The repositories holding that
ref are therefore copied last, and after every copy the sequence must
exceed every `refs/changes/*/<number>/*` in the copies, or the run
fails and publishes nothing: a restored site would otherwise hand out
a number twice. `seed` and
`final` keep `rsync`, which is right for a stopped or seeding site,
and the site directory stays on `rsync` in every mode. All modes leave
out `*.lock` files, in the site and the repositories, and delete those
an earlier run left in the export directory: a stale lock
would block the ref it locks after an import, or the `git config`
that `render-config.sh` and `site-init.sh` run on `etc/`. The import
also drops any `*.lock` under `etc/` that an older archive holds.
In `backup` mode the files under `db/` and `data/` are copied from a
running site and may be internally inconsistent: `rsync` of a live H2
file is not a point-in-time copy. They hold the reviewed-file flags
(`db/account_patch_reviews`) and plugin data such as the events log
and the replication queue; code review data lives in the repositories
(NoteDb) and is exact. A crash-consistent copy of those files needs a
volume snapshot of the stopped or quiesced site, or a `final` export.
The manifest's `consistency` records which guarantee an export has.

`site-import` accepts a `final` or a `backup` manifest and refuses a
`seed` one. A `final` archive is the consistent copy for adopting a
site; a `backup` archive is the restore path of section 1.8, accepted
with those limits, and passes the same checks of section 4 before
`site-import` writes `complete`. A `seed` copy exists only to shorten
the `final` one and is never an import source.

### 3.2 manifest.json

`manifest.json` is a JSON object with these keys, in this order:

| Key | Type | Meaning |
| --- | ---- | ------- |
| `schema` | integer | Manifest format version, currently `1`. |
| `created` | string | UTC time the manifest was built, `YYYY-MM-DDTHH:MM:SSZ`. |
| `mode` | string | `seed`, `final` or `backup`, the export tool's mode; the backup CronJob (#242) writes `backup`. `site-import` accepts `final` and `backup`. |
| `consistency` | object | `final` and `backup` only. `git`: `verified`, the repositories are exact; `h2`: `stopped` for `final`, `best-effort` for `backup`, whose `db/` and `data/` files are copied from a running site. |
| `gerrit_version` | string or `null` | The release `bin/gerrit.war` in the source site records in its nested `com_google_gerrit_common_libversion.jar`, without the leading `v`; `null` when the site has no `bin/gerrit.war` or the war records no version there. |
| `site` | object | `path`: the source site directory, resolved; `bytes`: total size of `<out>/site`. |
| `git` | object | `path`: the source repository directory, resolved; `bytes`: total size of `<out>/git`; `repositories`: see below. |
| `ssh_host_keys` | object | Each regular file in the site's `etc/` named `ssh_host_*` and not ending `.pub`, mapped to its `SHA256:` fingerprint from `ssh-keygen -l -E sha256`. |
| `excluded` | array of strings | The site paths left out of `<out>/site`, in the order section 3.1 lists them, ending with the credential paths `.ssh` and `.config/gerrit_to_platform`; `cache` is absent with `--include-cache`. |
| `archives` | object | Each archive file name mapped to the hex SHA-256 of its contents; `{}` without `--pack`. |

`git.repositories` maps the path of each directory ending `.git` under
`<out>/git`, relative to it and including the suffix (for example
`All-Projects.git` or `<tenant>/project.git`), to an object with:

- `refs`: the number of refs `git for-each-ref` lists in it;
- `meta_config`: the commit `refs/meta/config` points to, peeled if it
  is a tag, or `null` when the repository has no such ref.
- `head`: the ref `HEAD` names, such as `refs/heads/master`, or the
  commit a detached `HEAD` points to, or `null` when it resolves to
  neither.

`site-import` checks each archive's SHA-256 against `archives` before
unpacking, and checks `refs` and `meta_config` against the unpacked
repositories before it writes `complete`.

## 4. Invariants an import must preserve

An import is complete when every invariant below holds. `site-import`
checks the ones it can before it writes `complete` to the state file
(section 1.7); the rest are the operator's acceptance test. Ref counts
and sequences change as soon as anyone uses the new server, so check
those before opening it to users.

| Invariant | How to verify |
| --------- | ------------- |
| Canonical URL and prefix | `gerrit.canonicalWebUrl` and the `httpd.listenUrl` path in the rendered `gerrit.config` equal the source's; `https://gerrit.example.org/<prefix>/config/server/info` answers and its links carry the prefix. |
| Server ID | `git config -f etc/gerrit.config gerrit.serverId` on the volume, the same in the archive's `site/etc/gerrit.config`, and the `gerrit.serverId` value are byte-identical. A mismatch fails the import; it is never reported as drift (section 1.4). |
| SSH host key fingerprints | `ssh-keyscan -p 29418 gerrit.example.org \| ssh-keygen -l -E sha256 -f -` lists the fingerprints in the manifest's `ssh_host_keys`. |
| Account count | `git --git-dir=All-Users.git for-each-ref refs/users/ \| wc -l` matches the source ([accounts][accounts]). |
| Group count | `git --git-dir=All-Users.git for-each-ref refs/groups/ \| wc -l` matches the source. |
| External-ID count | `git --git-dir=All-Users.git ls-tree -r --name-only refs/meta/external-ids \| wc -l` matches the source ([external IDs][extids]). |
| Project count, including hidden projects | The manifest's `git.repositories` has as many entries as the volume has repositories, and the project list with the `all` option, as an administrator, has the same number ([list projects][list-projects]). |
| Per-project ref counts | `git for-each-ref \| wc -l` in each repository equals its `refs` in the manifest. |
| Change sequence | `git --git-dir=All-Projects.git cat-file blob refs/sequences/changes` matches the source, and the next change created gets a number above every imported one. |
| `All-Projects` access configuration | `refs/meta/config` in `All-Projects.git` points to the commit in the manifest's `meta_config`. |
| Open changes work | An open change renders with its votes and comments, and accepts a new patchset over SSH and HTTP. |
| Replication resumes | `ssh -p 29418 gerrit.example.org replication list` lists the remotes, and after a push the replication log records it reaching each one. |
| Hooks fire | A push or comment runs the configured hook, and the hook's output or log records it. |
| Mail is delivered | A review comment on a test change sends mail through the configured relay to a test account. |

## 5. Secret contract

The chart never puts secret values in its values file. Each Secret
below follows the `existingSecret` pattern: when the value names a
Secret, the operator creates it and the chart only reads it. When the
value is empty, the chart renders `<fullname>-<purpose>` itself where
the table says so, generating each value once with `randAlphaNum` and
reusing it on upgrade through Helm's `lookup`; those Secrets carry
`helm.sh/resource-policy: keep` and
`argocd.argoproj.io/sync-options: Prune=false,Delete=false`. Generated
values are for plain Helm only; under Argo CD every `existingSecret`
is required (section 6.2).

This section lists key names and consumers only; values and their
sources belong to the deployment, not this document.

| Value | Keys | Consumer | Notes |
| ----- | ---- | -------- | ----- |
| `secrets.admin.existingSecret` | `username`, `password` | Not consumed yet | Generated as `<fullname>-admin` when empty. Later chart work bootstraps the administrator account from it. |
| `secrets.secureConfig.existingSecret` | `registerEmailPrivateKey`, and optionally `smtpPass` | `render-config` | Generated as `<fullname>-secure-config` when empty, with an empty `smtpPass` the operator fills in. Passed as `GERRIT_REGISTER_EMAIL_PRIVATE_KEY` and `GERRIT_SMTP_PASS` and written to `auth.registerEmailPrivateKey` and, when not empty, `sendemail.smtpPass` in `etc/secure.config`. Each later `secure.config` entry adds one key here and one environment variable. |
| `auth.saml.existingSecret` | `samlKeystore.jks`, `keystore-password`, `private-key-password`, and optionally `idp-metadata.xml` | Gerrit pod, `render-config` | As #235 specifies. Generated as `<fullname>-saml` when empty, with every key empty for the operator to fill in. The keystore, and with `auth.saml.metadataFromSecret` the IdP metadata, are mounted read-only into `etc/` as `samlKeystore.jks` and `saml-idp-metadata.xml`; the two passwords are passed as `GERRIT_SAML_KEYSTORE_PASSWORD` and `GERRIT_SAML_PRIVATE_KEY_PASSWORD` and written to `saml.keystorePassword` and `saml.privateKeyPassword` in `etc/secure.config`, never into values. The service provider metadata the module writes stays in `data/saml/` on the volume. To adopt a site that already uses SAML, the operator fills this Secret with the source's keystore, so the service provider keeps the key the identity provider trusts; a new keystore breaks every login until the identity provider is updated. |
| `replication.sshKeySecret` | `id_ed25519` (or `id_rsa`), and optionally `known_hosts` | Gerrit pod | SSH identity for the replication remotes, and their pinned host keys. Projected with mode `0400` into `/var/gerrit/.ssh`, beside the chart's `known_hosts2` of GitHub's published host keys and `replication.knownHosts`. |
| `siteImport.source.existingSecret` | `manifest.json`, `site.tar.zst`, `git.tar.zst` | `site-import` | With `source.type: https`, each key holds the full URL of that file, such as a pre-signed object-store URL, and wins over `<source.url>/<name>`. `site-import` passes the URLs to `curl` on standard input and never logs them. `age-identity` or `gpg-private-key`, for encrypted archives, are still to come (section 7). |
| `siteImport.source.existingSecret` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `site-import` | With `source.type: s3`, passed by `secretKeyRef` to `site-import` only, for rclone's `env_auth`; never mounted or logged. Optional where the pod has a workload identity through `serviceAccount`. |
| `backup.existingSecret` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Backup CronJob | Optional where the platform supplies workload identity. The backup does not encrypt the archives while `site-import` cannot decrypt them; rely on the bucket's server-side encryption. |
| `hooks.g2p.existingSecret` | `gerrit_to_platform.ini` | `site-init` | Generated as `<fullname>-g2p` when empty, with the key empty for the operator to fill in. Mounted with mode `0400` into `site-init`, which installs it with mode `0600` as `.config/gerrit_to_platform/gerrit_to_platform.ini` on the site, where the hooks gerrit-to-platform runs read it through `XDG_CONFIG_HOME`. |
| `plugins.its-jira.existingSecret` | `username`, `password` | `render-config` | Generated as `<fullname>-its-jira` when empty, with both keys empty for the operator to fill in. Passed as `GERRIT_ITS_JIRA_USERNAME` and `GERRIT_ITS_JIRA_PASSWORD` and written, when not empty, to `its-jira.username` and `its-jira.password` in `etc/secure.config`. The password may be a Jira API token. |
| `metrics.existingSecret` | `token` | `render-config`, Prometheus | Generated as `<fullname>-metrics` with a random `token` when empty, with `plugins.metrics-reporter-prometheus.enabled`. Passed as `GERRIT_METRICS_BEARER_TOKEN` and written to `plugin.metrics-reporter-prometheus.prometheusBearerToken` in `etc/secure.config`; the chart's `ServiceMonitor` names the same key as the bearer token Prometheus presents. Under Argo CD it must be an `existingSecret` (section 6.2), or each sync replaces the token. |
| `toolbox.existingSecret` | `ssh-privatekey` | Admin toolbox | Identity of an administrator account used from the toolbox. |

SSH host keys are not in this table: they are site state, not
configuration. They live in `etc/` on the volume, arrive with the
archive on an import (section 3) or come from `gerrit init` on a fresh
site, and keep their fingerprints across restarts and upgrades
because nothing renders over them.

## 6. Argo CD integration

The chart works with plain Helm; this section covers what Argo CD
needs on top. `K8S/argocd/example-gerrit.yaml` is the example
`AppProject` and `Application` it refers to: they deploy the chart
each release publishes, by version, with values from the deployment's
own Git repository.

### 6.1 Sync waves

| Wave | Objects |
| ---- | ------- |
| `-1` | NetworkPolicies, ConfigMaps, Secrets |
| `0` | StatefulSet, Services, Ingress |
| `1` | CronJobs, admin toolbox |

Argo CD applies resources wave by wave and waits for each wave to be
healthy before the next ([sync waves][argo-waves]), so the
configuration exists before the pod that reads it. The import needs
no wave or hook of its own: it runs inside the StatefulSet's pod
(section 1.7), and a failed import leaves the StatefulSet unhealthy,
which stops the sync from reporting success. Only the admin toolbox's
objects carry `argocd.argoproj.io/sync-wave` annotations yet; the Argo
CD sub-issue of #228 adds the rest (section 7).

### 6.2 existingSecret is required

The chart generates Secret values with `lookup` and `randAlphaNum`.
Argo CD renders charts without cluster access, so `lookup` returns
nothing and every sync renders new values and applies them over the
old ones. The keep annotations stop pruning and deletion, not updates.
A new `registerEmailPrivateKey`, for one, invalidates every
outstanding email verification link.

Under Argo CD every `existingSecret` value of section 5 is therefore
required: the operator creates the Secrets outside the application,
for example with an external secrets operator or sealed Secrets, and
the chart renders none. The generated values are for plain Helm only.

`ignoreDifferences` on the Secrets' `/data` with the
`RespectIgnoreDifferences=true` sync option would also stop the
overwrite ([diffing][argo-diff], [sync options][argo-sync-options]),
but every application would need to carry it, and a missing setting
fails silently by replacing the values. Naming external Secrets removes
the objects from the application instead.

### 6.3 managedNamespaceMetadata

With `CreateNamespace=true`, Argo CD creates the namespace before the
chart's objects and the chart cannot label it. The example
`Application` sets `managedNamespaceMetadata` to add the
`pod-security.kubernetes.io/enforce: restricted` label, so Pod Security
admission applies from the first pod ([sync options][argo-sync-options]).
A deployment that enables `persistence.volumePermissions.enabled`
needs `baseline` instead (section 1.11).

### 6.4 No automated sync by default

The example `Application` leaves `syncPolicy.automated` unset. A sync
that changes `siteImport.forceToken` replaces the site on the next pod
start, and an operator should decide when that happens. A deployment
may enable automated sync once the import has run and the import mode
is `skip` or `auto` with a `complete` state.

## 7. Open questions and roadmap

The sub-issues of #228 carry the work. This document is their common
reference; each updates the section it changes.

- #232: the chart skeleton (merged in #254); sections 1.1 to 1.6, 1.10
  and 1.11 describe it.
- #234: configuration rendering, `gerrit.config` from Helm and
  `secure.config` from Secrets (section 1.4).
- #235: SAML authentication, its Secret and its metadata egress
  (sections 1.10 and 5).
- #236: replication and its SSH key Secret (sections 1.10 and 5).
- #238: the Ingress, the SSH `LoadBalancer` and the ingress rule for
  the Ingress controller namespace (sections 1.5, 1.6 and 1.10).
- #239: the export tool (merged); section 3 describes its output.
- #240: the `site-import` init container and its state file
  (section 1.7), from a volume or HTTPS. Decrypting `.age` or `.gpg`
  archives remains open.
- #307: the `s3` import source, rclone in the chart image and the
  optional ServiceAccount for IRSA (sections 1.7, 1.11 and 5).
- #242: the backup CronJob to S3, its `backup` manifests and restore
  from `<prefix>/latest` through site import (sections 1.7, 1.8 and
  3).
- #256: the export tool leaving out `.chart/` (sections 1.7 and 3.1).
- #262: the admin toolbox and its maintenance helpers (section 1.9).
- #268: SAML group membership sync (section 1.4).
- #299: authentication as an explicit choice, with development
  accounts behind an opt-in (section 1.4).
- #244: health checks, metrics, JSON container logs and log retention
  (sections 1.3, 1.8, 1.10 and 5).
- The other sub-issues of #228 cover narrowing the egress default to
  the per-feature destinations, the maintenance CronJob and the Argo
  CD example.

Open questions:

- **Configuration drift on import.** Section 1.4 says the import
  reports differences between the imported `gerrit.config` and the
  values. A different `gerrit.serverId` always fails the import;
  whether any other difference fails it or only warns is open.
- **Maintenance scope.** Which tasks the maintenance CronJob runs, given
  what Gerrit already schedules itself, is open.

[gc-listenurl]: https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#httpd.listenUrl
[gc-serverid]: https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#gerrit.serverId
[gc-sshd]: https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#sshd
[gc-secure]: https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#secure.config
[gc-apr]: https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#accountPatchReviewDb
[pgm-init]: https://gerrit-review.googlesource.com/Documentation/pgm-init.html
[pgm-reindex]: https://gerrit-review.googlesource.com/Documentation/pgm-reindex.html
[acl]: https://gerrit-review.googlesource.com/Documentation/access-control.html
[notedb]: https://gerrit-review.googlesource.com/Documentation/note-db.html
[accounts]: https://gerrit-review.googlesource.com/Documentation/config-accounts.html
[extids]: https://gerrit-review.googlesource.com/Documentation/config-accounts.html#external-ids
[project-state]: https://gerrit-review.googlesource.com/Documentation/config-project-config.html#project-section
[list-projects]: https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#list-projects
[plugin-data]: https://gerrit-review.googlesource.com/Documentation/dev-plugins.html#data-directory
[replication]: https://gerrit.googlesource.com/plugins/replication/+/refs/heads/master/src/main/resources/Documentation/config.md
[hooks]: https://gerrit.googlesource.com/plugins/hooks/+/refs/heads/master/src/main/resources/Documentation/config.md
[argo-waves]: https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/
[argo-diff]: https://argo-cd.readthedocs.io/en/stable/user-guide/diffing/
[argo-sync-options]: https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/
