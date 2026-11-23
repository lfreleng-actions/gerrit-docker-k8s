<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Gerrit chart runbooks

Step-by-step procedures for operating a release of this chart. They
assume you know Kubernetes and Gerrit but not this chart. The chart
[README](README.md) is the reference for every value, Secret and
behaviour named here, and `K8S/docs/DESIGN.md` records why the chart
works the way it does; these runbooks link to both rather than repeat
them.

## Conventions

The commands use shell variables for the names that differ between
deployments. Set them once per shell:

```sh
ns=gerrit                    # namespace of the release
release=gerrit               # Helm release name
fullname=gerrit              # the chart's <fullname>; see below
chart=oci://ghcr.io/lfreleng-actions/gerrit-docker-k8s/charts/gerrit
version=1.2.3                # chart version in use
values=my-values.yaml        # the release's values file
url_prefix=/r                # gerrit.urlPrefix; "" for a root-served site
```

The StatefulSet, its pod and the site claim take their names from
`<fullname>`: `sts/<fullname>`, `<fullname>-0` and
`site-<fullname>-0`. The chart sets it in `templates/_helpers.tpl`
to `fullnameOverride` when the values set one. Otherwise it takes the
release name when that name contains the chart name (`nameOverride`,
or `gerrit` by default), and `<release>-<name>` when it does not.
The chart then cuts the result to 52 characters and drops a trailing
`-`. Read it from the release rather than working it out:

```sh
fullname=$(kubectl -n "$ns" get sts \
  -l "app.kubernetes.io/instance=$release" \
  -o jsonpath='{.items[0].metadata.name}')
```

Before the release exists, render it from the values instead:

```sh
fullname=$(helm template "$release" "$chart" --version "$version" \
  -n "$ns" -f "$values" -s templates/statefulset.yaml |
  awk '$1 == "name:" { print $2; exit }')
```

Every name in these runbooks, such as `gerrit.example.org`,
`gerrit-backups` or `eu-west-1`, is a placeholder.

Each `helm upgrade` below passes the release's own values file and,
where a runbook changes values for a while, a small overlay file
after it. Helm drops any values an upgrade does not pass, so keep
passing an overlay for as long as it applies, on every upgrade, the
toolbox's included: `isolate.yaml` last on a
[scratch release](#scratch-releases), and `hold-dns.yaml` during a
[cut-over](#cut-over) until DNS moves, and `pause.yaml` until you
[resume maintenance](#pause-maintenance). Under Argo CD, commit the
same change to the values the `Application` reads and sync it
instead; the example `Application` leaves automated sync off so that
an operator decides when a sync replaces a site (DESIGN section 6.4).

## Scratch releases

The import and upgrade rehearsals install a copy of production in a
scratch namespace, with production's values. Those values name
production's hosts, DNS records and integrations, and the copied site
carries queued replication events under `data/replication/`. Set `ns`
to the scratch namespace, create it, and create in it each Secret the
values name, as the [Secret contract](README.md#secret-contract)
describes; the release's pod does not start without them. Isolate
every scratch release with this overlay, last on every `helm install`
and `helm upgrade` of that release, recovery and toolbox upgrades
included:

```yaml
# isolate.yaml
ingress:
  enabled: false
ssh:
  service:
    type: ClusterIP
    externalDNS: false
replication:
  enabled: false
hooks:
  enabled: false
  g2p:
    enabled: false
plugins:
  its-jira:
    enabled: false
sendemail:
  enable: false
backup:
  enabled: false
```

It removes the Ingress and its certificate, the SSH load balancer and
its DNS record, the replication, hooks and its-jira plugins, mail
and the backup CronJob, which would otherwise write to production's
bucket. It turns gerrit-to-platform off too: the chart refuses to
render it without hooks and replication. Remove from
`pluginCatalogue.plugins` any other plugin that calls out, such as
`webhooks`. Reach the scratch release through port forwards
instead:

```sh
kubectl -n "$ns" port-forward "svc/$fullname-http" 8080 &
kubectl -n "$ns" port-forward "svc/$fullname-ssh" 29418 &
```

## Adopt an existing site

Moves a Gerrit site that runs outside this chart into a new release.
The `site-import` init container fetches an export, checks it,
replaces the empty site volume with it, runs `gerrit init` and a full
reindex, and lets Gerrit start after that. The chart README's
[Site import](README.md#site-import) section describes each step, and
DESIGN section 4 lists the invariants an import must keep.

### Prerequisites

Work through this list before the cut-over, and again before the
rehearsal:

- [ ] The source runs Gerrit 3.7.0 or newer. `site-import` refuses an
  older export; upgrade the source first.
- [ ] The cross-release test covers the source's release, or you have
  run it for that release; see [Skipping releases](#skipping-releases).
- [ ] `gerrit.host` and `gerrit.urlPrefix` equal the source's. Gerrit
  bakes the canonical URL into change links, emails, SAML metadata,
  the identity provider's callback URL and every client's
  configuration, so a different value breaks them all.
- [ ] `gerrit.serverId` equals `gerrit.serverId` in the source's
  `etc/gerrit.config`, or stays empty. `site-import` refuses an export
  whose ID differs from a set value.
- [ ] A non-default `gerrit.allProjects` or `gerrit.allUsers` on the
  source appears in `gerrit.extraConfig`.
- [ ] `ssh.host` and the SSH port clients use stay the same, so that
  the host keys the export carries match every client's
  `known_hosts`.
- [ ] Each Secret the values name exists in the namespace, holding the
  keys the [Secret contract](README.md#secret-contract) lists. Copy
  `auth.registerEmailPrivateKey` from the source's `etc/secure.config`
  into the secure-config Secret's `registerEmailPrivateKey`, so that
  outstanding email verification links keep working.
- [ ] With SAML, the SAML Secret holds the source's keystore and its
  two passwords, not a new keystore; see
  [Importing a site that uses SAML](README.md#importing-a-site-that-uses-saml).
- [ ] The replication key, the gerrit-to-platform configuration, the
  issue tracker credentials and the LFS keys sit in their Secrets.
  The export leaves out `.ssh/`, `.config/gerrit_to_platform/` and
  `etc/lfs.secure.config`, and `render-config` writes
  `etc/secure.config` from Secrets at every start.
- [ ] `siteImport.resources` gives the reindex enough memory, and
  `siteImport.scratchSizeLimit` and the node's ephemeral storage hold
  both archives and their unpacked copy.
- [ ] A DNS plan: the records for `gerrit.host` and `ssh.host`, their
  TTL lowered a day ahead, and who changes them during the cut-over.
- [ ] A host with room for the export, as the chart README's
  [Exporting from a host the chart did not build](README.md#exporting-from-a-host-the-chart-did-not-build)
  describes, and a way to move it to the cluster: an S3 bucket, an
  HTTPS server or a volume.

### Export the site

`scripts/gerrit-site-export.py` needs its two modules,
`gerrit_export_git.py` and `gerrit_export_host.py`, in the same
directory. Copy all three to the source host. The root `README.md`,
"Exporting a Gerrit Site", describes every option; the ones a
migration uses:

<!-- markdownlint-disable MD013 -->

| Option | Use |
| ------ | --- |
| `seed` | Copies the site while Gerrit serves; repeat it to shorten the final copy |
| `final` | The last copy, with Gerrit stopped; refuses while a daemon serves the site |
| `backup` | A packed copy of a serving site, consistent for git; for rehearsals |
| `--pack` | Writes `site.tar.zst` and `git.tar.zst`, the form `site-import` needs |
| `--exclude`, `--exclude-from` | Leave out host files: shell history, agent state, dated config copies |
| `--drop-foreign-links` | Leave out links `site-import` refuses, listed in `dropped_links` |
| `--upload s3://<bucket>/<prefix>` | Sends the export with the `aws` CLI; `--sse` picks the encryption |
| `--upload-urls <file>` | Sends the export to pre-signed HTTPS `PUT` URLs |
| `--i-stopped-writes` | `final` with writes stopped another way than stopping the daemon |

<!-- markdownlint-enable MD013 -->

The tool never exports credentials the chart supplies from Secrets:
`.ssh/`, `.config/gerrit_to_platform/` and `etc/lfs.secure.config`.
It stops before writing anything when a path the import needs, such as
`etc/gerrit.config`, `git` or the SAML keystore, is a link or lies
under one, when an `--exclude` pattern names site structure or
`All-Users` data, and when it finds a link `site-import` would refuse
and `--drop-foreign-links` is absent. Each refusal names the path.

Keep the host's exclusions in one reviewed file, one pattern a line:

```text
# host-excludes.txt
.bash_history
.viminfo
.ansible
etc/gerrit.config.*
```

### Rehearse

Import into a scratch namespace before the cut-over.
The rehearsal proves the Secrets, the values and the export, and
measures how long the import takes, without touching the source.

1. Take a packed export of the serving site:

   ```sh
   python3 gerrit-site-export.py backup --site /srv/gerrit \
     --out /srv/export/rehearsal --exclude-from host-excludes.txt \
     --upload s3://gerrit-exports/rehearsal
   ```

2. Install a release from it in the scratch namespace, with the same
   values and Secrets as the real release, this overlay and the
   [isolation overlay](#scratch-releases):

   ```yaml
   # import.yaml
   siteImport:
     source:
       type: s3
       url: s3://gerrit-exports/rehearsal
       s3:
         region: eu-west-1
   ```

   ```sh
   helm install "$release" "$chart" --version "$version" \
     -n "$ns" --create-namespace -f "$values" -f import.yaml \
     -f isolate.yaml
   ```

3. Follow [Watch the import](#watch-the-import) and
   [Verify](#verify), then uninstall the scratch release and delete
   its data as [Destroy a release's data](#destroy-a-releases-data)
   describes, stopping after step 3: the scratch release has no
   backups of its own, and step 4 would delete production's.

### Cut over

1. Pre-copy the site while it serves, as often as you like:

   ```sh
   python3 gerrit-site-export.py seed --site /srv/gerrit \
     --out /srv/export/final --exclude-from host-excludes.txt
   ```

2. Freeze the source: announce the window and stop the daemon.

   ```sh
   /srv/gerrit/bin/gerrit.sh stop
   ```

   Where something else stops writes, such as the `readonly` plugin,
   pass `--i-stopped-writes` to the next step instead.

3. Take the final export and upload it:

   ```sh
   python3 gerrit-site-export.py final --site /srv/gerrit \
     --out /srv/export/final --pack --exclude-from host-excludes.txt \
     --upload s3://gerrit-exports/final
   sha256sum /srv/export/final/manifest.json
   ```

   With `--upload-urls`, pre-sign each `PUT` under a fresh prefix. To
   copy the directory by hand, copy the archives first and
   `manifest.json` last.

4. Install the release with the source and the manifest's checksum,
   holding DNS back until step 6:

   ```yaml
   # import.yaml
   siteImport:
     source:
       type: s3
       url: s3://gerrit-exports/final
       s3:
         region: eu-west-1
       expectedManifestSha256: <sha256 of manifest.json>
   ```

   ```yaml
   # hold-dns.yaml
   ingress:
     enabled: false
   ssh:
     service:
       externalDNS: false
   ```

   ```sh
   helm upgrade --install "$release" "$chart" --version "$version" \
     -n "$ns" --create-namespace -f "$values" -f import.yaml \
     -f hold-dns.yaml
   ```

   Where ExternalDNS publishes the cluster's Ingresses or the SSH
   Service's `external-dns.alpha.kubernetes.io/hostname`, a release
   installed with them would move production DNS before you verify
   it, and users could write to it before step 7 opens it to them.
   `hold-dns.yaml` leaves the Ingress out and the SSH record off; the
   SSH load balancer still gets its address. Reach the release
   through the port forwards in [Scratch releases](#scratch-releases)
   until step 6.

5. [Watch the import](#watch-the-import) and [verify](#verify) it.

6. Switch DNS: upgrade without `hold-dns.yaml`. That creates the
   Ingress and, with `ssh.service.externalDNS`, the SSH record, which
   ExternalDNS then publishes. `site-import` finds the import
   `complete` and does nothing. Where you manage the records by hand,
   point `gerrit.host` at the Ingress and `ssh.host` at the address
   of the `<fullname>-ssh` Service.

   ```sh
   helm upgrade "$release" "$chart" --version "$version" -n "$ns" \
     -f "$values" -f import.yaml
   kubectl -n "$ns" get svc "$fullname-ssh" \
     -o jsonpath='{.status.loadBalancer.ingress[0]}'
   ```

   cert-manager issues the Ingress's certificate once the Ingress
   exists; with an HTTP-01 issuer, that waits for the new record.

7. Open the new release to users. Keep the source stopped for the
   rollback window.

8. Once satisfied, drop the import source from the values, upgrade,
   wait for the new pod to become Ready, and then delete the
   export, its bucket copy and any URL or key Secret. A pod that still
   names a deleted source volume or Secret never starts.

   ```yaml
   siteImport:
     mode: auto
     source:
       type: none
   ```

### Watch the import

`site-import` runs before Gerrit starts, so the pod stays in `Init`
while it works. A large site takes a while: the full reindex
dominates.

```sh
kubectl -n "$ns" get pod "$fullname-0" -w
kubectl -n "$ns" logs -f "$fullname-0" -c site-import
```

The log opens with the decision, such as
`site-import: mode auto, source s3, state none: import`, then names
each download, the files and links it left out, the verification, the
`init` and the reindex, and ends with the migration report. When the
container fails, Kubernetes restarts the pod with back-off and
`site-import` retries the import. Read the reason from the pod status,
which keeps the container's termination message after kubelet drops
its log:

```sh
kubectl -n "$ns" describe pod "$fullname-0"
kubectl -n "$ns" logs "$fullname-0" -c site-import --previous
```

### When site-import refuses

Each refusal starts with `site-import: error:` in the log and the
termination message. A refusal before the import begins leaves the
volume as it stands; one during the import records `failed` with the
reason in `.chart/site-state.json`.

<!-- markdownlint-disable MD013 -->

| The message contains | Cause | Fix |
| -------------------- | ----- | --- |
| `mode force needs a siteImport.forceToken` | `force` with an empty token | Set a new `forceToken` |
| `mode force needs a siteImport.source to import` | `force` with `source.type: none` | Name the source |
| `the volume holds a site no state file accounts for` | The claim holds data the chart did not write | Repair the volume, set `mode: skip` to keep it, or force an import |
| `an earlier import left state failed` | `source.type: none` after a failed import | Name the source again to retry, or force another import |
| `manifest.json has sha256 ..., not the expected ...` | `expectedManifestSha256` does not match the export | Check which export you meant, then fix the pin |
| `is a 'seed' export` | The export came from `seed` | Export with `final` or `backup` |
| `export with --pack` | The export lacks `--pack` | Export again with `--pack` |
| `encrypted archives are not supported yet` | The export used `--encrypt` | Decrypt it and serve plaintext archives |
| `older than 3.7.0` | The source runs an older Gerrit | Upgrade the source and export again |
| `names no Gerrit version site import recognises` | The manifest lacks `gerrit_version` | Confirm the source runs 3.7.0 or newer, then set `allowUnknownSourceVersion` |
| `has sha256 ..., but manifest.json records ...` | An archive changed or arrived truncated | Upload it again, manifest last |
| `does not exist`, `failed with rclone exit status`, `failed with curl exit status` | Wrong URL, missing permission or blocked egress | Check the URL, `s3:ListBucket` and `s3:GetObject`, and `source.egressPorts` |
| `names no backup` | The `latest` object holds no backup name | Name a dated backup prefix instead |
| `symlinks to absolute paths` | The export carries links the import refuses | Export again with `--exclude` or `--drop-foreign-links` |
| `gerrit.serverId ... does not match the archive's` | `gerrit.serverId` differs from the source's | Copy the source's ID, or leave the value empty |
| `the archive's gerrit.allProjects is` | The source renamed a system repository | Set the source's names in `gerrit.extraConfig` |
| `differs from the SAML Secret's samlKeystore.jks` | The SAML Secret holds another keystore | Load the source's keystore into the Secret |
| `saml.keystorePath ... lies outside its site` | The source keeps its keystore outside the site | Move the keystore into the site and export again |
| `SSH host key fingerprints ... do not match` | The host keys differ from the manifest | Export again from the stopped source |
| `verification failed:` | A repository's refs differ from the manifest | Export again with `final` from the stopped source |
| `gerrit.war init failed`, `gerrit.war reindex failed` | `init` or the reindex failed | Read the log above the error; raise `siteImport.resources` on an out-of-memory kill |

<!-- markdownlint-enable MD013 -->

### Recovery

`site-import` retries a failed import on every pod start. Fix the
cause, then start the pod again. Pass the overlays the install used:
`hold-dns.yaml` during a cut-over, as here, and on a rehearsal
`-f isolate.yaml` last in its place, or the upgrade recreates what
the overlay removed:

```sh
helm upgrade "$release" "$chart" --version "$version" -n "$ns" \
  -f "$values" -f import.yaml -f hold-dns.yaml
kubectl -n "$ns" delete pod "$fullname-0"
```

To read the state file while Gerrit cannot start, use the
[admin toolbox](#admin-toolbox):

```sh
kubectl -n "$ns" exec "deploy/$fullname-toolbox" -- \
  cat /var/gerrit/.chart/site-state.json
```

To give up on the import and start over, follow
[Reset a site](#reset-a-site).

### Verify

Read the state file and the migration report it carries in
`warnings`:

```sh
kubectl -n "$ns" exec "$fullname-0" -c gerrit -- \
  cat /var/gerrit/.chart/site-state.json
```

Run the chart's site checks without the pod restart, which needs
development accounts. Set `auth_type` to the type server info
reports, which is the values' `auth.type`, and `HTTP` under SAML; the
script fails on any other:

```sh
auth_type=HTTP
python3 K8S/charts/gerrit/files/scripts/verify-site.py \
  --namespace "$ns" --fullname "$fullname" \
  --url-prefix "$url_prefix" --auth-type "$auth_type" --skip-restart
```

`--url-prefix` takes the release's `gerrit.urlPrefix`, which an
adopted site keeps from its source. The
check clones `All-Projects` anonymously, which fails on a site that
denies anonymous reads; treat that failure as expected there.

Then check the invariants of DESIGN section 4 against the source: the
SSH host key fingerprints, the account, group and project counts, the
change sequence, and an open change that renders and takes a new
patch set. Do this before users arrive, since every new change moves
the counts. Until DNS moves, `ssh.host` still names the source, so
scan the new release through a port forward to its SSH Service, and
compare the result with the fingerprints of the source's
`etc/ssh_host_*_key.pub`:

```sh
kubectl -n "$ns" port-forward "svc/$fullname-ssh" 29418 &
ssh-keyscan -p 29418 localhost | ssh-keygen -l -E sha256 -f -
kill %1
```

On the source host:

```sh
for key in /srv/gerrit/etc/ssh_host_*_key.pub; do
  ssh-keygen -l -E sha256 -f "$key"
done
```

`verify-import.py` compares an imported release with a REST snapshot
of the source, which `tests/k8s/build_fixture_site.py` takes of a
fixture site. It pushes a patch set at the end, so run it against a
rehearsal release, never production:

```sh
python3 K8S/charts/gerrit/files/scripts/verify-import.py \
  --namespace "$ns" --fullname "$fullname" --pod "$fullname-0" \
  --url-prefix "$url_prefix" \
  --manifest export/manifest.json --expected expected.json \
  --password-file password
```

Pass `--pod` and `--url-prefix` every time: their defaults, `gerrit-0`
and `/r`, do not follow `--fullname`.

### Rollback window

Until users write to the new release, rolling back means pointing DNS
at the source again and starting it. Once they write, the source lacks
their changes: to go back, export the new release with a backup and
import it on the source's side, or accept the loss. Decide the length
of the window before the cut-over, keep the source stopped
throughout it, and keep the source's data until it ends.

## Pause maintenance

The git gc, log cleanup and backup CronJobs and the admin toolbox
mount the site volume next to Gerrit, and outlive the Gerrit pod's
replacement. Their locks do not coordinate with `site-import`: a git
gc Job can rewrite repositories while a forced import deletes and
replaces them, and a backup of a half-imported site can move
`latest`. A pod still using the claim also leaves
`kubectl delete pvc` waiting in `Terminating`. Before a forced import
or dropping the volume, pause them all, and keep them paused until
you have [verified](#verify) the site.

1. Suspend the release's CronJobs and stop the toolbox:

   ```sh
   kubectl -n "$ns" get cronjob \
     -l "app.kubernetes.io/instance=$release" -o name |
     xargs -r -n1 kubectl -n "$ns" patch \
       -p '{"spec":{"suspend":true}}'
   kubectl -n "$ns" get deploy "$fullname-toolbox" \
     --ignore-not-found -o name |
     xargs -r kubectl -n "$ns" scale --replicas=0
   ```

2. Wait until no maintenance or toolbox pod runs:

   ```sh
   sel="app.kubernetes.io/instance=$release"
   sel="$sel,app.kubernetes.io/component in"
   sel="$sel (git-gc,log-cleanup,backup,admin-toolbox)"
   drained=
   while pods=$(kubectl -n "$ns" get pod -l "$sel" -o name \
       --field-selector=status.phase!=Succeeded,status.phase!=Failed); do
     if [ -z "$pods" ]; then drained=yes; break; fi
     printf '%s\n' "$pods"
     sleep 10
   done
   echo "drained: ${drained:-no, the pod query failed}"
   ```

   Go on once it prints `drained: yes`, and not before. A failed
   query, such as an API timeout or a denied request, ends the loop
   with `drained: no`: fix the cause and run the loop again.

   To end a long Job sooner, delete it with
   `kubectl -n "$ns" delete job <job>`; what it leaves behind goes
   with the site it worked on. Let a backup you still need finish.

3. Write an overlay that keeps the backup CronJob suspended, and pass
   it, with the other overlays in force, on every upgrade until you
   resume. The chart renders that CronJob's `spec.suspend` from
   `backup.suspend`, so an upgrade without it resumes backups; the
   chart renders no `suspend` for the git gc and log cleanup
   CronJobs, and an upgrade leaves their patched value alone.

   ```yaml
   # pause.yaml
   backup:
     suspend: true
   ```

To resume, once the site verifies, clear the suspension and upgrade
without `pause.yaml`, which sets the backup CronJob's `suspend` from
the values again:

```sh
kubectl -n "$ns" get cronjob \
  -l "app.kubernetes.io/instance=$release" -o name |
  xargs -r -n1 kubectl -n "$ns" patch \
    -p '{"spec":{"suspend":false}}'
```

## Restore from a backup

The backup CronJob writes each backup to
`s3://<bucket>/<prefix>/<backup>/` and the newest backup's name to
`s3://<bucket>/<prefix>/latest`; see [Backups](README.md#backups). A
backup is an import source, so a restore is a forced import.

1. Pick the backup. `latest` names the newest; list the others to
   restore an older one:

   ```sh
   aws s3 cp s3://gerrit-backups/gerrit/gerrit/latest -
   aws s3 ls s3://gerrit-backups/gerrit/gerrit/
   ```

   `gerrit/gerrit` is the default prefix, `<namespace>/<fullname>`.

2. Write an overlay with a token you have not used before:

   ```yaml
   # restore.yaml
   siteImport:
     mode: force
     forceToken: restore-20260105
     source:
       type: s3
       url: s3://gerrit-backups/gerrit/gerrit/latest
       s3:
         region: eu-west-1
   ```

   Name a dated prefix, such as
   `s3://gerrit-backups/gerrit/gerrit/20260105T020000Z-0123abcd`, in
   place of `latest` to restore that backup. Give the pod read access
   to the bucket as [S3 credentials](README.md#s3-credentials)
   describes.

3. [Pause maintenance](#pause-maintenance).

4. Upgrade the release with both overlays and
   [watch the import](#watch-the-import):

   ```sh
   helm upgrade "$release" "$chart" --version "$version" -n "$ns" \
     -f "$values" -f restore.yaml -f pause.yaml
   ```

5. [Verify](#verify) the site, then set `siteImport.mode: auto` and
   `source.type: none` in the values, so a later change to the token
   cannot replace the site by accident, and
   [resume maintenance](#pause-maintenance) with that upgrade.

What the restore replaces and what it keeps:

<!-- markdownlint-disable MD013 -->

| Replaced from the backup | Kept or rebuilt |
| ------------------------ | --------------- |
| Every repository, so changes, comments, votes, accounts and groups | The release's values: the chart renders `gerrit.config` again |
| The SSH host keys and `etc/` files the chart does not render | The Secrets: `render-config` writes `etc/secure.config` again |
| `db/` and `data/`, copied from a serving site at best effort | `.chart/`, which `site-import` alone writes |
| | Plugins and libraries, from the image |
| | Indexes and caches, rebuilt by the reindex and by Gerrit |

<!-- markdownlint-enable MD013 -->

The restore loses everything written after the backup started. Paths the backup
left out through `backup.exclude` start empty; the migration report
names them.

## Reset a site

Discards the site on the volume and imports one again. Both forms
destroy data: take a backup first unless you mean to lose it.

### Force a re-import over the volume

Keeps the claim and replaces everything on it but `.chart/`. Follow
[Restore from a backup](#restore-from-a-backup) with any source: a
backup, or a new export of the site you adopted. A new `forceToken`
is what makes it happen; a token equal to the recorded one does
nothing.

### Drop the volume

Deletes the claim, so the StatefulSet creates an empty one and
`site-import` starts from no state. Use this when the volume holds a
state you cannot repair. The StatefulSet recreates the claim from its
own `volumeClaimTemplates`, which Kubernetes does not let an upgrade
change, so this keeps `persistence.size` and
`persistence.storageClassName`; to change either, follow
[Change the volume's size or class](#change-the-volumes-size-or-class)
as well.

1. [Pause maintenance](#pause-maintenance), so no Job or toolbox
   pod keeps the claim in use, then stop Gerrit and note the bound
   volume:

   ```sh
   kubectl -n "$ns" scale sts "$fullname" --replicas=0
   kubectl -n "$ns" wait --for=delete pod "$fullname-0" --timeout=5m
   pv=$(kubectl -n "$ns" get pvc "site-$fullname-0" \
     -o jsonpath='{.spec.volumeName}')
   kubectl get pv "$pv" \
     -o jsonpath='{.spec.persistentVolumeReclaimPolicy}{"\n"}'
   ```

2. When the policy reads anything but `Delete`, change it to
   `Delete` so the storage goes with the claim:

   ```sh
   kubectl patch pv "$pv" \
     -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'
   ```

3. Delete the claim. It waits in `Terminating` while any pod still
   uses it; `kubectl describe pvc` names them under `Used By`.

   ```sh
   kubectl -n "$ns" delete pvc "site-$fullname-0"
   ```

4. Set the source to import, or `source.type: none` for an empty
   site, and upgrade the release, or sync it under Argo CD. The
   upgrade scales the StatefulSet back to one replica, which creates
   the claim again.

   ```sh
   helm upgrade "$release" "$chart" --version "$version" -n "$ns" \
     -f "$values" -f import.yaml -f pause.yaml
   ```

5. [Verify](#verify) the site and
   [resume maintenance](#pause-maintenance).

### Change the volume's size or class

A new `persistence.size` or `persistence.storageClassName` changes the
StatefulSet's `volumeClaimTemplates`, which Kubernetes refuses to
update, so the upgrade fails until the StatefulSet goes too. Follow
steps 1 to 3 of [Drop the volume](#drop-the-volume), then delete the
stopped StatefulSet, set the new values and upgrade, or sync under
Argo CD, which creates the StatefulSet and its claim again:

```sh
kubectl -n "$ns" delete sts "$fullname"
helm upgrade "$release" "$chart" --version "$version" -n "$ns" \
  -f "$values" -f import.yaml -f pause.yaml
```

The site starts empty, or from the import source, as in step 4;
finish with step 5.

## Upgrade Gerrit

The image tag is a release of this repository, not a Gerrit version:
each release's image carries one Gerrit version, which the chart's
`appVersion` records. Upgrading Gerrit means moving the chart version
to a release that carries the new Gerrit. Each published chart pins
`image.tag` and `image.digest` to its own release's image, and a
digest takes precedence over the tag, so a values file that sets
`image.tag` without `image.digest` changes nothing. Leave `image` out
of the values and let the chart version choose it. Where the values
must override the image, set both `image.tag` and `image.digest` to
the release's, which the published chart of that release lists, and
drop the override when you next change the chart version:

```sh
helm show values "$chart" --version <new version> | grep -A8 '^image:'
```

`site-init` skips `gerrit init` on a site that holds
`.chart/initialised`, the marker it writes after its first `init`. A
new image alone starts the new Gerrit on the old site
without `init`. That works within a release line, but Gerrit refuses
to start on a site whose NoteDb schema predates the release, so a
major upgrade has to remove the marker first. The chart has no value
that does this.

### Before upgrading

1. Read the release notes of each Gerrit release between the two.
   Note any schema migration, any index that needs an offline
   reindex, and any plugin that changed or left.
2. Check [Skipping releases](#skipping-releases).
3. Rehearse in a scratch namespace: install a release from the latest
   backup with the new chart version, the `restore.yaml` of
   [Restore from a backup](#restore-from-a-backup) and the
   [isolation overlay](#scratch-releases) last. Create the namespace
   and the Secrets the values name first, the
   [S3 credentials](README.md#s3-credentials) included. `site-import`
   runs the new `init` and a full reindex there, and its log shows
   what the migration does to your data. Then
   [verify](#verify) it and
   [destroy its data](#destroy-a-releases-data), stopping after
   step 3: the rehearsal restores from production's backups, which
   step 4 would delete with the rollback point.

   ```sh
   ns=gerrit-upgrade-rehearsal
   kubectl create namespace "$ns"
   helm install "$release" "$chart" --version <new version> -n "$ns" \
     -f "$values" -f restore.yaml -f isolate.yaml
   ```

   Set `ns` back to the release's namespace before the next step.
4. Take a backup and note its name, the rollback point. The Job can
   complete without a backup, as when another backup holds the lock
   or the site's import state is not `complete`, and `latest` then
   still names an older one. Read the Job's log: it ends with
   `.../latest names <backup>` and `done` after a new backup, and says
   `skipping this run:` and why otherwise. Fix the cause and run
   another Job under a new name until one publishes, and record that
   dated prefix. The chart cuts `<fullname>` to 45 characters before
   it appends `-backup`, so find the CronJob by its labels rather than
   by name:

   ```sh
   sel="app.kubernetes.io/instance=$release"
   sel="$sel,app.kubernetes.io/component=backup"
   cronjob=$(kubectl -n "$ns" get cronjob -l "$sel" -o name)
   echo "${cronjob:?no backup CronJob; set backup.enabled}"
   job="pre-upgrade-backup-$(date -u +%Y%m%d%H%M%S)"
   kubectl -n "$ns" create job --from="$cronjob" "$job"
   kubectl -n "$ns" wait --for=condition=complete "job/$job" \
     --timeout=6h
   kubectl -n "$ns" logs "job/$job" | grep -E 'latest names|skipping'
   ```

### Upgrade

1. For a major upgrade, or any upgrade whose notes call for `init`,
   remove the marker with Gerrit stopped, from the
   [admin toolbox](#admin-toolbox):

   ```sh
   kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=1
   kubectl -n "$ns" scale sts "$fullname" --replicas=0
   kubectl -n "$ns" wait --for=delete pod "$fullname-0" --timeout=5m
   kubectl -n "$ns" rollout status "deploy/$fullname-toolbox" \
     --timeout=5m
   kubectl -n "$ns" exec "deploy/$fullname-toolbox" -- \
     rm /var/gerrit/.chart/initialised
   kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=0
   ```

   `init` leaves an initialised site unchanged, so running it on a
   minor upgrade does no harm either.

2. Set `version` to the new chart version, removing or updating any
   `image` override in the values as [Upgrade Gerrit](#upgrade-gerrit)
   describes, and upgrade. The upgrade sets the StatefulSet back to
   one replica.

   ```sh
   version=<new version>
   helm upgrade "$release" "$chart" --version "$version" -n "$ns" \
     -f "$values"
   kubectl -n "$ns" logs -f "$fullname-0" -c site-init
   ```

   Without the marker, the log shows `Initialising the Gerrit site`
   and the output of `init`. Gerrit then starts and, by default,
   upgrades each index in the background when the release brings a new
   index version.

3. [Verify](#verify) the release, and check
   `<urlPrefix>/config/server/version` reports the new Gerrit.

### Offline reindex

Run one when the release notes call for it, or when Gerrit logs that
no version of an index is ready. Stop Gerrit and reindex from the
toolbox, which runs the site's `bin/gerrit.war`, the one `site-init`
installed from the new image:

```sh
kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=1
kubectl -n "$ns" scale sts "$fullname" --replicas=0
kubectl -n "$ns" wait --for=delete pod "$fullname-0" --timeout=5m
kubectl -n "$ns" rollout status "deploy/$fullname-toolbox" --timeout=5m
kubectl -n "$ns" exec -it "deploy/$fullname-toolbox" -- \
  java -Duser.home=/tmp \
    --add-opens java.base/java.net=ALL-UNNAMED \
    --add-opens java.base/java.lang.invoke=ALL-UNNAMED \
    -jar /var/gerrit/bin/gerrit.war reindex -d /var/gerrit
kubectl -n "$ns" scale sts "$fullname" --replicas=1
kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=0
```

The options are those `site-init` and `site-import` pass to their
own offline commands: the launcher unpacks into the home directory,
which allows no writes in the image, and Gerrit needs the two
packages opened under the image's Java. The toolbox adds none of
them itself.

Give the toolbox the memory a reindex needs through
`toolbox.resources`. A forced re-import from a fresh backup also
reindexes before Gerrit starts, with `siteImport.reindex`, on
by default, and `siteImport.resources`; it loses what users write
after that backup.

### Roll back

Gerrit cannot migrate a site back to an older release. Roll back by
importing the pre-upgrade backup with the old chart version, whose
pinned image carries the old Gerrit. Restore any `image` override the
values carried before the upgrade, with both its tag and digest; an
override that keeps the new digest runs the new Gerrit, which
migrates the backup again.

```yaml
# rollback.yaml
siteImport:
  mode: force
  forceToken: rollback-20260105
  source:
    type: s3
    url: s3://gerrit-backups/gerrit/gerrit/<pre-upgrade backup>
    s3:
      region: eu-west-1
```

[Pause maintenance](#pause-maintenance) first, as for any forced
import, then upgrade with both overlays and
[watch the import](#watch-the-import):

```sh
version=<old version>
helm upgrade "$release" "$chart" --version "$version" -n "$ns" \
  -f "$values" -f rollback.yaml -f pause.yaml
```

Name the dated prefix, not `latest`, since a backup taken after the
upgrade would move `latest` to a site the old Gerrit cannot read.
The rollback loses everything written since that backup.
[Verify](#verify) the site, then set `siteImport.mode: auto` and
`source.type: none` as after a restore, and
[resume maintenance](#pause-maintenance) with that upgrade.

### Skipping releases

`init` migrates a site from any release it supports in one step, but
Gerrit does not promise that for every pair of releases, and release
notes sometimes require a stop on an intermediate release.
`.github/workflows/site-import-test.yaml` tells you whether this
chart's image takes a given source release in one step: it builds a
site with the official `gerritcodereview/gerrit` image of that
release, fills it, exports it and imports it into a kind release. Its
default legs cover 3.7, 3.8, 3.9 and 3.12, and refuse 3.6. To test
another release, run the workflow with its tag in `source_tags`:

```sh
gh workflow run site-import-test.yaml \
  -f source_tags=3.10.6-ubuntu22
```

When a leg fails, upgrade the source through an intermediate release
the test passes, then export it again.

## Routine maintenance

### Enable or disable a plugin

`plugins.allowRemoteAdmin` is `false`, so `gerrit plugin install` and
`gerrit plugin enable` over SSH refuse. Plugins come from the image's
catalogue instead:

1. List what the image carries:

   ```sh
   kubectl -n "$ns" exec "$fullname-0" -c gerrit -- \
     ls /var/gerrit-catalogue/plugins /var/gerrit-catalogue/lib
   ```

2. Add the jar's name, without `.jar`, to `pluginCatalogue.plugins`
   or `pluginCatalogue.libs`, or remove it, and upgrade. Plugins the
   chart manages through their own values, such as `replication`,
   `hooks`, `its-jira`, `lfs`, `healthcheck`, `events-log` and the
   metrics plugin, take their `enabled` value instead.
3. The upgrade restarts the pod, and `site-init` replaces `plugins/`
   and `lib/` with the jars named. Check the result:

   ```sh
   ssh -p 29418 <admin>@<ssh.host> gerrit plugin ls
   ```

Disabling a plugin removes its jar but leaves its data under `data/`.

### Purge caches and prune indexes

Gerrit flushes its in-memory caches online:

```sh
ssh -p 29418 <admin>@<ssh.host> gerrit flush-caches --all
```

To shrink the H2 cache files, or to remove index versions an upgrade
left behind, stop Gerrit and run the toolbox helpers, as
[Purging caches and stale indexes](README.md#purging-caches-and-stale-indexes)
describes:

```sh
kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=1
kubectl -n "$ns" scale sts "$fullname" --replicas=0
kubectl -n "$ns" wait --for=delete pod "$fullname-0" --timeout=5m
kubectl -n "$ns" rollout status "deploy/$fullname-toolbox" --timeout=5m
kubectl -n "$ns" exec "deploy/$fullname-toolbox" -- gerrit-cache-purge
kubectl -n "$ns" exec "deploy/$fullname-toolbox" -- gerrit-index-prune
kubectl -n "$ns" scale sts "$fullname" --replicas=1
kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=0
```

Each helper refuses while the Gerrit pod exists. The check uses DNS,
which can lag a scale-down; wait a moment and retry a refusal.

### Rotate SSH host keys

Avoid it. Every client, CI system and replication peer pins these
keys in its `known_hosts`, and a new key makes each of them refuse the
server, with a warning that reads like an attack, until someone
updates it. Rotate when a key leaked or uses an algorithm you
must retire, and announce the new fingerprints first.

The keys are site state in `etc/` on the volume; the chart renders
nothing over them. Replace them with Gerrit stopped:

```sh
kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=1
kubectl -n "$ns" scale sts "$fullname" --replicas=0
kubectl -n "$ns" wait --for=delete pod "$fullname-0" --timeout=5m
kubectl -n "$ns" rollout status "deploy/$fullname-toolbox" --timeout=5m
kubectl -n "$ns" exec "deploy/$fullname-toolbox" -- \
  ls /var/gerrit/etc
kubectl -n "$ns" exec "deploy/$fullname-toolbox" -- sh -c '
  cd /var/gerrit/etc &&
  mkdir -p old-host-keys &&
  mv ssh_host_ed25519_key ssh_host_ed25519_key.pub old-host-keys/ &&
  ssh-keygen -q -t ed25519 -N "" -f ssh_host_ed25519_key'
kubectl -n "$ns" scale sts "$fullname" --replicas=1
kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=0
```

Repeat the `mv` and `ssh-keygen` for each `ssh_host_*_key` the `ls`
lists, then move `old-host-keys/` off the volume. Check the new
fingerprints with `ssh-keyscan` as in [Verify](#verify). Backups taken
before the rotation carry the old keys, and a restore from one brings
them back.

### Rotate the SAML keystore

Follow [Rotating the keystore](README.md#rotating-the-keystore): add
the new certificate to the identity provider beside the old one,
remove the stale metadata from `data/saml/` through the toolbox,
update the Secret, restart, and remove the old certificate once logins
work. To update the Secret in place:

```sh
kubectl -n "$ns" create secret generic gerrit-saml \
  --from-file=samlKeystore.jks=./samlKeystore.jks \
  --from-literal=keystore-password='...' \
  --from-literal=private-key-password='...' \
  --dry-run=client -o yaml | kubectl -n "$ns" apply -f -
kubectl -n "$ns" rollout restart sts "$fullname"
```

Add `--from-file=idp-metadata.xml=...` with `metadataFromSecret`.

### Rotate replication keys and tracker credentials

The pod reads its Secrets at start, so each rotation is a Secret
update and a restart:

1. Give the new credential access at the far end beside the old one:
   the new public key as a deploy key or on the machine user, or a new
   Jira API token.
2. Update the Secret, keeping its keys:

   ```sh
   kubectl -n "$ns" create secret generic gerrit-replication-ssh \
     --from-file=id_ed25519=./replication_ed25519_new \
     --dry-run=client -o yaml | kubectl -n "$ns" apply -f -
   kubectl -n "$ns" create secret generic gerrit-its-jira \
     --from-literal=username=gerrit-bot --from-literal=password='...' \
     --dry-run=client -o yaml | kubectl -n "$ns" apply -f -
   ```

   Include `known_hosts` again when the replication Secret carries
   one. The same pattern rotates `smtpPass`, the LFS keys and the
   metrics token in their Secrets.

3. Restart and check:

   ```sh
   kubectl -n "$ns" rollout restart sts "$fullname"
   kubectl -n "$ns" rollout status sts "$fullname" --timeout=15m
   ssh -p 29418 <admin>@<ssh.host> replication start --all --wait
   ```

4. Revoke the old credential at the far end.

Under Argo CD, `rollout restart` leaves a `restartedAt` annotation the
`Application` reports as drift; deleting the pod with
`kubectl delete pod "$fullname-0"` restarts it without one.

## Uninstall and reinstall

### What survives an uninstall

`helm uninstall` removes the StatefulSet, Services, ConfigMaps,
CronJobs and toolbox, and keeps:

- the Secrets, which carry `helm.sh/resource-policy: keep` and Argo
  CD's `Prune=false,Delete=false`, and any Secret you created;
- the claim `site-<fullname>-0`, which Kubernetes never deletes with a
  StatefulSet, and its volume, with `.chart/site-state.json`;
- the backups in S3.

A reinstall with the same release name in the same namespace resumes
rather than starts afresh: the StatefulSet binds the same claim, the
chart reuses the Secrets, and `site-import` finds a `complete` state
and does nothing, whatever the source values say. A `force` with the
token the state file records does nothing either; a new token
replaces the site.

```sh
helm uninstall "$release" -n "$ns"
helm install "$release" "$chart" --version "$version" -n "$ns" \
  -f "$values"
```

A reinstall under another release name starts afresh when it renders
another `<fullname>` and no claim of that name exists, and not
otherwise. A `fullnameOverride` in the values keeps the old name
under any release name, and the 52-character cut can give two long
release names the same one. Render the new name, as in
[Conventions](#conventions), and check both before relying on an
empty site:

```sh
new_release=gerrit-fresh
new_fullname=$(helm template "$new_release" "$chart" \
  --version "$version" -n "$ns" -f "$values" \
  -s templates/statefulset.yaml |
  awk '$1 == "name:" { print $2; exit }')
echo "old: $fullname new: $new_fullname"
kubectl -n "$ns" get pvc "site-$new_fullname-0"
```

The two names must differ and the `get pvc` must answer `NotFound`.
Otherwise the new release binds the existing claim and resumes the
site on it.

### Destroy a release's data

For a tenant whose data must go, tear down in this order, so nothing
recreates what you already deleted:

1. The release. Under Argo CD, delete the `Application` with
   cascading deletion instead.

   ```sh
   pv=$(kubectl -n "$ns" get pvc "site-$fullname-0" \
     -o jsonpath='{.spec.volumeName}')
   helm uninstall "$release" -n "$ns"
   ```

2. The Secrets and the claim. The chart's Secrets carry the release's
   instance label; delete those you created by name. The chart keeps
   no lock object in the cluster: the backup lock lives on the volume.
   Set the volume's reclaim policy to `Delete` before deleting the
   claim, so the provisioner deletes the disk with it, as in
   [Drop the volume](#drop-the-volume).

   ```sh
   kubectl -n "$ns" get secret -l "app.kubernetes.io/instance=$release"
   kubectl -n "$ns" delete secret -l "app.kubernetes.io/instance=$release"
   kubectl -n "$ns" delete secret gerrit-saml gerrit-replication-ssh
   kubectl get pv "$pv" -o jsonpath='{.spec.csi.volumeHandle}{"\n"}'
   kubectl patch pv "$pv" \
     -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'
   kubectl -n "$ns" delete pvc "site-$fullname-0"
   ```

   Note the volume handle the `get pv` prints: it names the disk at
   the provider.

3. The volume, its disk and its snapshots. These belong to this
   release alone, a scratch release's included, so clean them up in
   every case. Check that the volume has gone; a volume provisioned
   by hand, or a provisioner that cannot delete, leaves it
   `Released`. When `get pv` still lists it, delete its object, and
   delete the disk the handle names at the provider: deleting the
   object alone leaves a copy of the site on a billable disk.

   ```sh
   kubectl get pv "$pv"
   kubectl delete pv "$pv"
   ```

   Then the snapshots. A `VolumeSnapshotContent` whose
   `deletionPolicy` says to keep the snapshot leaves it at the
   provider when its `VolumeSnapshot` goes, and deleting the content
   object then removes the Kubernetes object and nothing else. List
   the contents taken from the volume, whose source handle is the one
   step 2 printed, and note each snapshot handle. Set each content's
   policy to `Delete`, then delete its `VolumeSnapshot`, or the
   content itself when its `VolumeSnapshot` has already gone.

   ```sh
   cols=NAME:.metadata.name,VOLUME:.spec.source.volumeHandle
   cols=$cols,SNAPSHOT:.spec.volumeSnapshotRef.name
   cols=$cols,POLICY:.spec.deletionPolicy,HANDLE:.status.snapshotHandle
   kubectl get volumesnapshotcontent -o "custom-columns=$cols"
   kubectl patch volumesnapshotcontent <content> --type merge \
     -p '{"spec":{"deletionPolicy":"Delete"}}'
   kubectl -n "$ns" delete volumesnapshot <snapshot>
   # or, when the VolumeSnapshot has already gone:
   kubectl delete volumesnapshotcontent <content>
   kubectl get volumesnapshotcontent <content>
   ```

   The last command must answer `NotFound`. Check at the provider
   that no snapshot the noted handles name remains, and delete any
   that does: a snapshot left there is a recoverable copy of the
   site.

4. The shared storage: the backup prefix, and any export left in a
   bucket or on a volume.

   Skip this step for a scratch release. It runs without backups, and
   its values and import source name production's bucket and
   prefixes; changing `ns` does not scope an S3 command. For any
   release, confirm before deleting a prefix that it belongs to the
   tenant you are destroying and to no one else. The prefix must be
   the release's own `backup.s3.prefix`, or `<namespace>/<fullname>`
   when that is empty; no other release may write to or import from
   it; and no rollback plan may name it.

   Check whether the bucket keeps versions or locks objects. On a
   versioned bucket, `aws s3 rm` adds a delete marker in front of each
   object and removes nothing, so every archive stays recoverable though
   `aws s3 ls` shows an empty prefix. Keep the trailing `/` on the
   prefix, so it matches no other tenant's.

   ```sh
   bucket=gerrit-backups
   prefix=gerrit/gerrit/
   aws s3api get-bucket-versioning --bucket "$bucket"
   aws s3api get-object-lock-configuration --bucket "$bucket"
   aws s3 ls "s3://$bucket/$prefix"
   aws s3 rm --recursive "s3://$bucket/$prefix"
   ```

   When `get-bucket-versioning` prints a `Status`, `Enabled` or
   `Suspended`, delete every version and every delete marker under the
   prefix by its `VersionId`, then list them again:

   ```sh
   tab=$(printf '\t')
   aws s3api list-object-versions --bucket "$bucket" \
     --prefix "$prefix" --output text \
     --query '[Versions, DeleteMarkers][][].[Key, VersionId]' |
     while IFS=$tab read -r key id; do
       aws s3api delete-object --bucket "$bucket" --key "$key" \
         --version-id "$id" >/dev/null ||
         echo "not deleted: $key $id"
     done
   aws s3api list-object-versions --bucket "$bucket" \
     --prefix "$prefix" --output json \
     --query 'length([Versions, DeleteMarkers][][])'
   ```

   The last command must print `0`. Any `not deleted:` line, a count
   above zero, or an error means the teardown is incomplete, not done.
   `get-object-lock-configuration` fails with
   `ObjectLockConfigurationNotFoundError` on a bucket without Object
   Lock. On one with it, a retention period or legal hold refuses the
   deletion until it ends or someone lifts it, and MFA delete refuses it
   without the bucket owner's MFA device: record what remains and
   when it can go. Delete the same prefix from any bucket this one
   replicates to.

## Admin toolbox

The toolbox is a `Deployment` on the Gerrit image with the site
volume at `/var/gerrit`, at zero replicas until you need it; the
chart README's [Admin toolbox](README.md#admin-toolbox) section
describes what it carries and how it schedules.

### Start a session

Enable it once in the values, with an administrator's SSH key for
commands to a running Gerrit, create the key's Secret, and apply the
values. Without `toolbox.enabled` the chart renders no Deployment,
and none exists to scale until the upgrade or sync has run.

```yaml
toolbox:
  enabled: true
  existingSecret: gerrit-toolbox-ssh
```

```sh
kubectl -n "$ns" create secret generic gerrit-toolbox-ssh \
  --from-file=ssh-privatekey=./admin_ed25519
helm upgrade "$release" "$chart" --version "$version" -n "$ns" \
  -f "$values"
kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=1
kubectl -n "$ns" rollout status "deploy/$fullname-toolbox" --timeout=5m
```

Later sessions start with the `scale` command alone.

Scale the toolbox up before stopping Gerrit, so it lands on Gerrit's
node and can attach a `ReadWriteOnce` volume.

### Run SSH commands

The toolbox reaches port 29418 of the Gerrit pod and nothing else:

```sh
kubectl -n "$ns" exec -it "deploy/$fullname-toolbox" -- \
  sh -c 'ssh -i "$GERRIT_SSH_IDENTITY" -p 29418 \
    admin@"$GERRIT_POD_DNS" gerrit version'
```

Confirm the host key fingerprint the first time `ssh` asks. Replace
`admin` with the account the key belongs to, and `gerrit version`
with any SSH command, such as `gerrit show-caches` or
`replication list`.

### Make REST calls

The toolbox's network policy blocks HTTP, so call the REST API from a
workstation through a port forward, with an administrator's HTTP
password from the user settings page. The chart's `<fullname>-admin`
Secret holds no usable credential yet.

```sh
kubectl -n "$ns" port-forward "svc/$fullname-http" 8080 &
curl -su "admin:$(cat admin-http-password)" \
  "http://localhost:8080$url_prefix/a/config/server/version"
```

### End the session

Scale the toolbox back to zero: while it holds the volume, a Gerrit
pod scheduled on another node cannot start.

```sh
kubectl -n "$ns" scale "deploy/$fullname-toolbox" --replicas=0
```

To remove it, set `toolbox.enabled: false` and upgrade.
