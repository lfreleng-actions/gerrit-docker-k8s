#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Import one fixture export into a new release on the current cluster
#
# Copies <fixture>/export into a PersistentVolumeClaim, installs the
# chart with site-import reading it, and then either verifies the
# import with verify-import.py or, for a release older than site
# import supports, checks that site-import refused it. An import whose
# pod cannot start fails as soon as watch_pod.py sees it, with each
# container's logs.
#
# Run it from the repository root, after kind loads the chart image;
# arguments after the third go to verify-import.py:
#
#   bash tests/k8s/import-leg.sh <fixture-dir> <import|refuse> <out-dir> \
#     [--expect-warning <text>]...
#
# Environment: CHART (default K8S/charts/gerrit), IMAGE and TAG (the
# chart image, default gerrit-local:import) and NAMESPACE (default
# gerrit-import).

set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "Usage: bash $0 <fixture-dir> <import|refuse> <out-dir> [verify-import args]" >&2
  exit 2
fi
fixture=$1
expect=$2
out=$3
shift 3
chart=${CHART:-K8S/charts/gerrit}
image=${IMAGE:-gerrit-local}
tag=${TAG:-import}
ns=${NAMESPACE:-gerrit-import}
case "$expect" in
  import | refuse) ;;
  *)
    echo "Expected import or refuse, not: $expect" >&2
    exit 2
    ;;
esac
mkdir -p "$out"

# Place the export on a claim the release mounts, as a user the
# chart's image runs as, so site-import can read its 0600 archives
kubectl create namespace "$ns"
kubectl apply -n "$ns" -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: gerrit-export
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: export-loader
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 1001
    runAsGroup: 1001
    fsGroup: 1001
  containers:
    - name: loader
      image: ${image}:${tag}
      imagePullPolicy: Never
      command: [sleep, '3600']
      volumeMounts:
        - {name: export, mountPath: /export}
  volumes:
    - name: export
      persistentVolumeClaim: {claimName: gerrit-export}
EOF
kubectl wait -n "$ns" pod/export-loader --for=condition=Ready --timeout=5m
kubectl exec -n "$ns" export-loader -- mkdir /export/fixture
for name in manifest.json git.tar.zst site.tar.zst; do
  kubectl cp "$fixture/export/$name" "$ns/export-loader:/export/fixture/$name"
done
kubectl delete pod -n "$ns" export-loader --wait

install=(
  helm install gerrit "$chart"
  --namespace "$ns"
  -f "$chart/values-local-smoke.yaml"
  --set "image.repository=$image"
  --set "image.tag=$tag"
  --set "image.digest="
  --set siteImport.source.type=pvc
  --set siteImport.source.url=/mnt/import/fixture
  --set-json
  'siteImport.source.volume={"persistentVolumeClaim":{"claimName":"gerrit-export"}}'
)

if [ "$expect" = refuse ]; then
  "${install[@]}"
  # site-import must fail on the release's first start, before Gerrit
  status=""
  for _ in $(seq 300); do
    status=$(kubectl get pod -n "$ns" gerrit-0 --no-headers --ignore-not-found \
      | awk '{print $3}') || true
    if [ "$status" = "Init:Error" ]; then
      break
    fi
    sleep 2
  done
  kubectl logs -n "$ns" gerrit-0 -c site-import > "$out/site-import.log" 2>&1 \
    || kubectl logs -n "$ns" gerrit-0 -c site-import --previous \
      > "$out/site-import.log" 2>&1 || true
  if [ "$status" != "Init:Error" ]; then
    echo "::error::gerrit-0 is ${status:-missing}, not Init:Error ❌"
    exit 1
  fi
  if ! grep -F "older than 3.7.0" "$out/site-import.log"; then
    cat "$out/site-import.log"
    echo "::error::site-import failed, but not on the version floor ❌"
    exit 1
  fi
  echo "site-import refused the export of an unsupported release ✅"
  exit 0
fi

# Each container's log, and its previous run's, in out and on stdout
dump_logs() {
  local c run
  local -a flag
  for c in site-import site-init render-config gerrit; do
    for run in current previous; do
      flag=()
      if [ "$run" = previous ]; then
        flag=(--previous)
      fi
      echo "::group::$c ($run)"
      kubectl logs -n "$ns" gerrit-0 -c "$c" "${flag[@]}" 2>&1 \
        | tee "$out/$c-$run.log" || true
      echo "::endgroup::"
    done
  done
}

# Watch the pod rather than wait for helm: an init container that fails
# or a Gerrit that crash-loops ends the leg at once, with its logs
"${install[@]}"
if ! python3 "$(dirname "${BASH_SOURCE[0]}")/watch_pod.py" \
  --namespace "$ns" --pod gerrit-0 --timeout 1200; then
  dump_logs
  echo "::error::gerrit-0 did not start after the import ❌"
  exit 1
fi
kubectl logs -n "$ns" gerrit-0 -c site-import > "$out/site-import.log" 2>&1
python3 "$chart/files/scripts/verify-import.py" \
  --namespace "$ns" --fullname gerrit --url-prefix /r \
  --manifest "$fixture/export/manifest.json" \
  --expected "$fixture/expected.json" \
  --password-file "$fixture/password" \
  --report "$out/verify-import.json" \
  "$@"
echo "The import kept everything the fixture holds ✅"
