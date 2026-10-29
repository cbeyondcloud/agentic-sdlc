#!/bin/sh
# The kind smoke, run inside the tool container (smoke.sh / smoke.ps1 start it with the
# engine's socket and copy deploy/helm into /w). Steps:
#   1. a kind cluster on the pinned node image; this container joins its network
#   2. the control-plane image (built on the host) loaded into the cluster
#   3. a throw-away Postgres (postgres.yaml) and the chart's Secret, random passwords
#   4. helm install (the pre-install migration hook), helm test (/readyz)
#   5. helm upgrade (the pre-upgrade hook on a migrated database), helm test again
# The cluster is deleted at the end unless KEEP=1. Passwords never leave the cluster.
set -eu

: "${KIND_CLUSTER:?}" "${CP_IMAGE:?}"
# Kubernetes 1.34: the newest node image of kind 0.33.0 that still runs on cgroup v1 hosts
# (Docker Desktop on WSL2 kernels 5.15; kubelet 1.35+ refuses cgroup v1). KIND_NODE_IMAGE
# overrides it on cgroup v2 hosts.
NODE_IMAGE=${KIND_NODE_IMAGE:-kindest/node:v1.34.11@sha256:44e222ee2132dab25ff87301682f89eb82c7880ea3a1bf543bfe9708fd08d67d}
NS=agentic

say() { echo "kind-smoke: $*"; }
random() { head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
cleanup() {
  if [ "${KEEP:-0}" != 1 ]; then
    kind delete cluster --name "$KIND_CLUSTER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

say "creating cluster $KIND_CLUSTER"
kind create cluster --name "$KIND_CLUSTER" --image "$NODE_IMAGE" --wait 180s
docker network connect kind "$(hostname)"
kind get kubeconfig --internal --name "$KIND_CLUSTER" > /tmp/kubeconfig
export KUBECONFIG=/tmp/kubeconfig

say "loading $CP_IMAGE"
kind load docker-image --name "$KIND_CLUSTER" "$CP_IMAGE"

say "starting a throw-away Postgres"
kubectl create namespace "$NS"
# passwords go through owner-only files, never a command line
umask 077
owner=$(random); app=$(random)
printf 'superuser=%s\nowner=%s\n' "$(random)" "$owner" > /tmp/pg.env
printf 'database-url=postgresql://cp_app:%s@postgres:5432/agentic\n' "$app" > /tmp/cp.env
printf 'owner-database-url=postgresql://cp_owner:%s@postgres:5432/agentic\n' "$owner" >> /tmp/cp.env
kubectl -n "$NS" create secret generic pg-passwords --from-env-file=/tmp/pg.env >/dev/null
kubectl -n "$NS" create configmap pg-init --from-file=10-agentic.sh=/w/kind/pg-init.sh >/dev/null
kubectl -n "$NS" apply -f /w/kind/postgres.yaml >/dev/null
kubectl -n "$NS" rollout status deploy/postgres --timeout=180s
kubectl -n "$NS" create secret generic agentic-platform --from-env-file=/tmp/cp.env >/dev/null
rm -f /tmp/pg.env /tmp/cp.env

repo=${CP_IMAGE%:*}; tag=${CP_IMAGE##*:}
set -- --namespace "$NS" --set "image.repository=$repo" --set "image.tag=$tag" \
  --set image.pullPolicy=Never --wait --timeout 10m

say "helm install"
helm install agentic /w/agentic-platform "$@"
kubectl -n "$NS" get pods
helm test agentic --namespace "$NS" --logs

say "helm upgrade (the migration hook again, on a migrated database)"
helm upgrade agentic /w/agentic-platform "$@" --set worker.replicas=1
helm test agentic --namespace "$NS" --logs
kubectl -n "$NS" get deploy,hpa,pdb,svc
say "passed"
