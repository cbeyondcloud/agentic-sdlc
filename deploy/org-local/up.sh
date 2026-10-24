#!/usr/bin/env bash
# Start the local org stack: the gateway's LITE stack (mock mode: no provider key, no
# spend), then the control plane, its Postgres and Grafana.
#
#   TOKENLEAN_DIR=/path/to/token-optimisation ./up.sh [--no-build]
#
#   --no-build  do not rebuild the control-plane and gateway proxy images
#
# TOKENLEAN_DIR is a TokenLean checkout with deploy/lite/local; it is only read (its own up
# writes its git-ignored .env.lite and .state). First run: creates .env.org from
# .env.org.template and generates its passwords. Engine: CONTAINER_ENGINE=docker (default)
# or podman.
set -euo pipefail
umask 077   # .env.org holds secrets: owner-only from its first byte
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

BUILD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0 ;;
    -h | --help)
      org_usage "$0"
      exit 0
      ;;
    *) org_die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

org_check_engine
org_check_tokenlean

# 1. The gateway's LITE stack, started the way it documents (ruling S1).
LITE_ARGS=(--mock)
if [ "$BUILD" = 0 ]; then LITE_ARGS+=(--no-build); fi
echo "org: starting the gateway's LITE stack (mock mode) from $TOKENLEAN_DIR"
# Git Bash on Windows rewrites variables that look like POSIX paths when it starts a
# Windows program; the LITE stack's overlay list holds container paths, so keep it as is.
# (MSYS only; ignored on macOS and Linux.)
export MSYS2_ENV_CONV_EXCL="LITE_CONFIG_OVERLAYS${MSYS2_ENV_CONV_EXCL:+;$MSYS2_ENV_CONV_EXCL}"
CONTAINER_ENGINE="$ORG_ENGINE" bash "$TOKENLEAN_DIR/deploy/lite/local/up.sh" "${LITE_ARGS[@]}"

# 2. Settings and secrets.
if [ ! -f "$ORG_ENV_FILE" ]; then
  cp "$ORG_DIR/.env.org.template" "$ORG_ENV_FILE"
  echo "org: created .env.org"
fi
chmod 600 "$ORG_ENV_FILE" 2>/dev/null || true
org_normalize_env_file
rand_hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }
for key in CP_SUPERUSER_PASSWORD CP_OWNER_PASSWORD CP_APP_PASSWORD \
  AGENTIC_DASH_READER_PASSWORD GRAFANA_ADMIN_PASSWORD; do
  org_fill "$key" "$(rand_hex 24)"
done
GATEWAY_TENANT=$(org_lite_value LITE_TENANT_ID)
org_set GATEWAY_TENANT "${GATEWAY_TENANT:-LITE-LOC-01}"
org_set GATEWAY_DB_PASSWORD "$(org_lite_value DB_PASSWORD)"

# 3. The gateway's LITE dashboards, copied (never mounted from its checkout).
mkdir -p "$ORG_STATE_DIR/gateway-dashboards"
chmod 755 "$ORG_STATE_DIR" "$ORG_STATE_DIR/gateway-dashboards"   # read by Grafana's user
rm -f "$ORG_STATE_DIR/gateway-dashboards/"*.json
for name in $ORG_LITE_DASHBOARDS; do
  cp "$TOKENLEAN_DIR/dashboard/dashboards/$name.json" "$ORG_STATE_DIR/gateway-dashboards/$name.json"
  chmod 644 "$ORG_STATE_DIR/gateway-dashboards/$name.json"
done

# 4. Our services.
UP_ARGS=(up -d --remove-orphans)
if [ "$BUILD" = 1 ]; then UP_ARGS+=(--build); fi
echo "org: starting the control plane, its Postgres and Grafana with $ORG_ENGINE ..."
if ! org_compose "${UP_ARGS[@]}"; then
  org_compose logs --tail 60 cp-init || true
  org_die "compose up failed (cp-init's log is above)"
fi

CP=$(org_url ORG_CP_PORT 8280)
GRAFANA=$(org_url ORG_GRAFANA_PORT 3300)
TIMEOUT=${ORG_HEALTH_TIMEOUT:-240}
wait_for() {   # URL, text the answer must contain
  local start body
  start=$(date +%s)
  printf 'org: waiting for %s ' "$1"
  while :; do
    body=$(curl -fsS "$1" 2>/dev/null || true)
    case "$body" in *"$2"*) break ;; esac
    if [ $(($(date +%s) - start)) -ge "$TIMEOUT" ]; then
      echo
      org_compose ps || true
      org_die "$1 did not answer within ${TIMEOUT}s (last: ${body:-no answer})"
    fi
    printf '.'
    sleep 3
  done
  echo " ok"
}
wait_for "$CP/readyz" '"db_enforced":true'
wait_for "$GRAFANA/api/health" '"database"'

echo "org: control plane  $CP  (tenant $(org_value ORG_TENANT))"
echo "org: grafana        $GRAFANA  (admin; password GRAFANA_ADMIN_PASSWORD in $ORG_ENV_FILE)"
echo "org: admin token    see README (admin issue-token --tenant $(org_value ORG_TENANT))"
echo "org: next           ./smoke.sh"
