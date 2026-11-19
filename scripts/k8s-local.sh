#!/usr/bin/env bash
# The pipeline on a local K3s cluster: k3d (K3s in Docker), 1 server + 2 agent nodes, kube
# context k3d-sde, namespace sde. Run through make (docs/KUBERNETES.md):
#
#   up       create the cluster if needed (start it if it exists), install KEDA, build +
#            import the image
#   deploy   rebuild + import the image, apply infra/k8s/overlays/local, wait for the rollouts
#   status   nodes, pods, services
#   logs     follow logs: SVC=api|control|validate|executor|generation|postgres|redis|s3|bootstrap
#   down     delete the cluster and everything in it
#
# The kubectl context is passed explicitly; your current context is never switched.
# Worker pods get Docker from a dind sidecar (variant B, infra/k8s): nodes mount nothing
# from the Mac.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

CLUSTER=sde
CTX="k3d-$CLUSTER"
NS=${SDE_K8S_NAMESPACE:-sde}
IMAGE=sde-pipeline:local
# Pinned: K1's dind sidecar is a native sidecar (k8s >= 1.29). Applies at cluster creation.
K3S_IMAGE=rancher/k3s:v1.35.5-k3s1
KEDA_URL=https://github.com/kedacore/keda/releases/download/v2.16.1/keda-2.16.1.yaml
OVERLAY=${SDE_K8S_OVERLAY:-infra/k8s/overlays/local}
K=(kubectl --context "$CTX" -n "$NS")
PLATFORM=linux/$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

exists() { k3d cluster list -o json | jq -e --arg n "$CLUSTER" 'any(.[]; .name == $n)' >/dev/null; }

create() {
  if exists; then
    say "cluster '$CLUSTER' exists; starting it"
    k3d cluster start "$CLUSTER" --wait >/dev/null
    return
  fi
  say "creating k3d cluster '$CLUSTER' (1 server + 2 agents)"
  # :8100 → the API (NodePort 30800, if the overlay exposes one); compose has :8000.
  k3d cluster create "$CLUSTER" \
    --image "$K3S_IMAGE" \
    --agents 2 \
    -p "8100:30800@loadbalancer" \
    --kubeconfig-switch-context=false \
    --wait
}

keda() {
  say "installing KEDA (the base's ScaledObject needs its CRDs)"
  # Server-side apply: KEDA's CRDs are too big for the client-side last-applied annotation.
  kubectl --context "$CTX" apply --server-side --force-conflicts -f "$KEDA_URL" >/dev/null
  kubectl --context "$CTX" -n keda rollout status deploy/keda-operator --timeout=300s
}

build() {
  say "building $IMAGE"
  # One platform, no attestations: a plain image that k3d's tarball import can load.
  # The runner target is a superset of control, so one image serves every Deployment.
  docker build -q --platform "$PLATFORM" --provenance=false --sbom=false \
    -f deploy/Dockerfile --target runner -t "$IMAGE" .
}

import() {
  say "importing $IMAGE into the cluster nodes"
  # k3d reports success even when a node rejects the tarball, so check every node.
  local tar
  tar=$(mktemp -t sde-images).tar
  docker save --platform "$PLATFORM" -o "$tar" "$IMAGE"
  k3d image import -c "$CLUSTER" "$tar" >/dev/null 2>&1 || true
  rm -f "$tar"
  local node n=0
  for node in $(k3d node list -o json | jq -r --arg c "$CLUSTER" \
      '.[] | select(.runtimeLabels["k3d.cluster"] == $c and (.role == "server" or .role == "agent")) | .name'); do
    docker exec "$node" crictl images -q "docker.io/library/$IMAGE" | grep -q . \
      || { echo "image $IMAGE missing on node $node after import" >&2; exit 1; }
    n=$((n + 1))
  done
  [ "$n" -gt 0 ] || { echo "no nodes found for cluster $CLUSTER" >&2; exit 1; }
  echo "imported $IMAGE into all $n nodes"
}

deploy() {
  say "deploying $OVERLAY"
  # Jobs are immutable: replace the bootstrap job so it runs again on every deploy.
  "${K[@]}" delete job bootstrap --ignore-not-found >/dev/null 2>&1 || true
  # The settings come from deploy/local.env, outside the overlay dir: kustomize needs
  # LoadRestrictionsNone, and the file must exist (it's optional, like compose's).
  touch deploy/local.env
  kubectl kustomize --load-restrictor LoadRestrictionsNone "$OVERLAY" \
    | kubectl --context "$CTX" apply -f -
  # Same tag, new image contents: restart running Deployments so they pick up the import.
  # (A just-created Deployment restarts harmlessly.)
  "${K[@]}" rollout restart deploy >/dev/null
  say "waiting for the rollouts"
  local sts d
  for sts in $("${K[@]}" get statefulset -o name); do "${K[@]}" rollout status "$sts" --timeout=300s; done
  if "${K[@]}" get job/bootstrap >/dev/null 2>&1; then
    "${K[@]}" wait job/bootstrap --for=condition=complete --timeout=300s
  fi
  for d in $("${K[@]}" get deploy -o name); do "${K[@]}" rollout status "$d" --timeout=300s; done
}

status() {
  kubectl --context "$CTX" get nodes -o wide
  echo
  "${K[@]}" get pods -o wide
  echo
  "${K[@]}" get svc
}

case "${1:-}" in
  up) create; keda; build; import ;;
  deploy) exists || { echo "no cluster '$CLUSTER'; run make k8s-up first" >&2; exit 1; }
          build; import; deploy; status ;;
  status) status ;;
  logs) "${K[@]}" logs -l "app.kubernetes.io/name=${SVC:-executor}" --all-containers --prefix -f --tail=50 ;;
  down) say "deleting cluster '$CLUSTER'"; k3d cluster delete "$CLUSTER" ;;
  *) sed -n '2,16p' "$0"; exit 1 ;;
esac
