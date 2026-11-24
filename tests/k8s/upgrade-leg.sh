#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Upgrade a fixture site in place on the current cluster
#
# Copies <fixture>/site, a stopped site an older official image built
# (build_fixture_site.py), onto the claim the release's pod mounts,
# with the chart's .chart/initialised marker and a complete import
# state naming that older release. It then checks siteUpgrade:
#
# 1. with siteUpgrade.mode refuse, site-init stops the pod and leaves
#    the reason in its termination message;
# 2. with chart.siteUpgrade.mode=auto in the runtime overrides
#    ConfigMap, site-init runs init and the offline reindex, rewrites
#    the marker with upgradedFrom, and Gerrit serves the new release;
# 3. with the marker raised above the image's release, auto refuses
#    the downgrade.
#
# Run it from the repository root, after kind loads the chart image:
#
#   bash tests/k8s/upgrade-leg.sh <fixture-dir> <old-release> <out-dir>
#
# <old-release> is the Gerrit release of the fixture's image, such as
# 3.12.1. Environment: CHART (default K8S/charts/gerrit), IMAGE and
# TAG (the chart image, default gerrit-local:import) and NAMESPACE
# (default gerrit-upgrade).

set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "Usage: bash $0 <fixture-dir> <old-release> <out-dir>" >&2
  exit 2
fi
fixture=$1
old=$2
out=$3
chart=${CHART:-K8S/charts/gerrit}
image=${IMAGE:-gerrit-local}
tag=${TAG:-import}
ns=${NAMESPACE:-gerrit-upgrade}
here=$(dirname "${BASH_SOURCE[0]}")
mkdir -p "$out"

# The claim the StatefulSet's volumeClaimTemplate "site" names for
# gerrit-0, created first so the release adopts it with the site on it
kubectl create namespace "$ns"
kubectl apply -n "$ns" -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: site-gerrit-0
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: site-loader
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
        - {name: site, mountPath: /mnt/site}
  volumes:
    - name: site
      persistentVolumeClaim: {claimName: site-gerrit-0}
EOF
kubectl wait -n "$ns" pod/site-loader --for=condition=Ready --timeout=5m
tar -C "$fixture/site" -czf "$out/site.tar.gz" .
kubectl cp "$out/site.tar.gz" "$ns/site-loader:/tmp/site.tar.gz"
rm -f "$out/site.tar.gz"
state=$(printf '{"schema": 1, "state": "complete", "source": "fixture", %s}' \
  "\"gerritVersion\": \"$old\", \"forceToken\": null, \"manifestSha256\": null")
# shellcheck disable=SC2016 # $1 and $2 expand in the pod's shell
kubectl exec -n "$ns" site-loader -- sh -ec '
  tar -C /mnt/site -xzf /tmp/site.tar.gz --no-same-owner
  mkdir -p /mnt/site/.chart
  printf "%s\n" "$1" > /mnt/site/.chart/site-state.json
  printf "{\"gerritVersion\": \"%s\"}\n" "$2" > /mnt/site/.chart/initialised
' sh "$state" "$old"
kubectl delete pod -n "$ns" site-loader --wait

# Wait until site-init has exited non-zero, or time is up. The kubelet
# restarts a failed init container, so the pod shows Init:Error only
# briefly before Init:CrashLoopBackOff: read the exit code of
# site-init's current or last termination instead of the pod's status.
wait_init_error() {
  local codes="" status=""
  for _ in $(seq 300); do
    codes=$(kubectl get pod -n "$ns" gerrit-0 --ignore-not-found -o jsonpath='{range .status.initContainerStatuses[?(@.name=="site-init")]}{.state.terminated.exitCode} {.lastState.terminated.exitCode}{end}') || true
    for code in $codes; do
      if [ "$code" != 0 ]; then
        return 0
      fi
    done
    sleep 2
  done
  status=$(kubectl get pod -n "$ns" gerrit-0 --no-headers --ignore-not-found \
    | awk '{print $3}') || true
  echo "::error::site-init on gerrit-0 did not fail; the pod is ${status:-missing} ❌"
  return 1
}

# site-init's termination message and log, saved under the given name
site_init_report() {
  kubectl get pod -n "$ns" gerrit-0 -o jsonpath='{range .status.initContainerStatuses[?(@.name=="site-init")]}{.state.terminated.message}{.lastState.terminated.message}{end}' \
    > "$out/$1-message.txt" || true
  kubectl logs -n "$ns" gerrit-0 -c site-init > "$out/$1.log" 2>&1 || true
  cat "$out/$1-message.txt" "$out/$1.log"
}

expect_refusal() {
  local name=$1 text=$2
  wait_init_error
  site_init_report "$name"
  if ! grep -qF "$text" "$out/$name-message.txt"; then
    echo "::error::site-init's termination message lacks: $text ❌"
    exit 1
  fi
}

# 1. refuse
helm install gerrit "$chart" --namespace "$ns" \
  -f "$chart/values-local-smoke.yaml" \
  --set "image.repository=$image" \
  --set "image.tag=$tag" \
  --set "image.digest=" \
  --set runtimeOverrides.enabled=true \
  --set siteUpgrade.mode=refuse
expect_refusal refuse "siteUpgrade.mode is refuse"
echo "refuse stopped the pod before Gerrit started ✅"

# 2. auto, switched on through the runtime overrides ConfigMap
kubectl create configmap -n "$ns" gerrit-overrides \
  --from-literal=chart.siteUpgrade.mode=auto
kubectl delete pod -n "$ns" gerrit-0 --wait
if ! python3 "$here/watch_pod.py" --namespace "$ns" --pod gerrit-0 --timeout 1800; then
  site_init_report auto
  echo "::error::gerrit-0 did not start after the upgrade ❌"
  exit 1
fi
kubectl logs -n "$ns" gerrit-0 -c site-init > "$out/auto.log" 2>&1
cat "$out/auto.log"
grep -F "gerrit.war reindex: done" "$out/auto.log"
marker=$(kubectl exec -n "$ns" gerrit-0 -c gerrit -- cat /var/gerrit/.chart/initialised)
echo "$marker" > "$out/initialised.json"
new=$(python3 - "$out/initialised.json" "$old" <<'EOF'
import json
import sys

marker = json.load(open(sys.argv[1], encoding="utf-8"))
if marker.get("upgradedFrom") != sys.argv[2]:
    sys.exit(f"the marker's upgradedFrom is not {sys.argv[2]}: {marker}")
print(marker["gerritVersion"])
EOF
)
kubectl port-forward -n "$ns" pod/gerrit-0 18080:8080 > "$out/port-forward.log" 2>&1 &
forward=$!
trap 'kill "$forward" 2> /dev/null || true' EXIT
served=""
for _ in $(seq 30); do
  if served=$(curl -fsS http://127.0.0.1:18080/r/config/server/version); then
    break
  fi
  sleep 2
done
kill "$forward" 2> /dev/null || true
echo "Gerrit serves $served"
if ! printf '%s' "$served" | grep -qF "\"$new"; then
  echo "::error::Gerrit serves $served, not $new ❌"
  exit 1
fi
echo "auto upgraded the site from $old to $new and Gerrit serves it ✅"

# 3. an image older than the marker's release, in auto
kubectl scale statefulset -n "$ns" gerrit --replicas=0
kubectl wait -n "$ns" --for=delete pod/gerrit-0 --timeout=5m
kubectl apply -n "$ns" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: marker-editor
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 1001
    runAsGroup: 1001
    fsGroup: 1001
  containers:
    - name: editor
      image: ${image}:${tag}
      imagePullPolicy: Never
      command: [sh, -c, 'printf "{\"gerritVersion\": \"99.0.0\"}\n" > /mnt/site/.chart/initialised']
      volumeMounts:
        - {name: site, mountPath: /mnt/site}
  volumes:
    - name: site
      persistentVolumeClaim: {claimName: site-gerrit-0}
EOF
kubectl wait -n "$ns" pod/marker-editor \
  --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m
kubectl delete pod -n "$ns" marker-editor --wait
kubectl scale statefulset -n "$ns" gerrit --replicas=1
expect_refusal downgrade "does not support downgrades"
echo "auto refused an image older than the site ✅"
