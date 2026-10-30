#!/usr/bin/env bash
# Install the umbrella chart on a throw-away kind cluster and run its smoke (M3-02).
#   deploy/helm/kind/smoke.sh            build the control-plane image, then the smoke
#   SKIP_BUILD=1 CP_IMAGE=... smoke.sh   use an image already built on this machine
#   KEEP=1 smoke.sh                      keep the cluster afterwards (kind delete cluster)
#   ENVIRONMENT=staging smoke.sh         install with environments/staging.yaml and run the
#                                        promotion dry run (M3-04, owner decision 114)
# kind, kubectl and helm run in a pinned tool container (tools.Dockerfile) that drives this
# machine's Docker through its socket: nothing native is installed. Docker only (kind's
# Podman provider is experimental). Containers and the cluster carry KIND_CLUSTER's name.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
ENGINE="${CONTAINER_ENGINE:-docker}"
export KIND_CLUSTER="${KIND_CLUSTER:-agentic-kind-smoke}"
export CP_IMAGE="${CP_IMAGE:-agentic-control-plane:kind-smoke}"
export KEEP="${KEEP:-0}"
export ENVIRONMENT="${ENVIRONMENT:-}"

if [ "${SKIP_BUILD:-0}" != 1 ]; then
  "$ENGINE" build -f "$ROOT/control-plane/Dockerfile" -t "$CP_IMAGE" "$ROOT"
fi
tools="agentic-kind-tools:$(sha256sum "$HERE/tools.Dockerfile" | cut -c1-12)"
"$ENGINE" build -q -t "$tools" -f "$HERE/tools.Dockerfile" "$HERE" >/dev/null

name="$KIND_CLUSTER-tools-$$"
# MSYS_NO_PATHCONV: Git Bash on Windows must not rewrite the socket path
MSYS_NO_PATHCONV=1 "$ENGINE" create --name "$name" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e KIND_CLUSTER -e CP_IMAGE -e KEEP -e KIND_NODE_IMAGE -e ENVIRONMENT "$tools" sh /w/kind/inside.sh >/dev/null
cleanup() {
  "$ENGINE" rm -f "$name" >/dev/null 2>&1 || true
  # an interrupted run leaves the cluster's node behind
  if [ "$KEEP" != 1 ]; then "$ENGINE" rm -f "$KIND_CLUSTER-control-plane" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
"$ENGINE" cp "$ROOT/deploy/helm/." "$name:/w/"
"$ENGINE" cp "$ROOT/deploy/org-local/cp-postgres/init.sh" "$name:/w/kind/pg-init.sh"
"$ENGINE" start -a "$name"
