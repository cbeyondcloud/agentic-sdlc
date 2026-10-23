#!/usr/bin/env bash
# Smoke-test a running local org stack. Exit 0 when every check passes.
#
#   TOKENLEAN_DIR=/path/to/token-optimisation ./smoke.sh
#
# Checks: the control plane is live and ready (database migrated, isolation enforced);
# Grafana is healthy, both datasources answer, the seven framework dashboards and the
# gateway's LITE set are provisioned; the gateway answers; and one tagged call through
# the gateway (mock mode: no provider, no spend) shows up in the tenant's dash_calls view,
# read through Grafana's datasource as the tenant's reader. HTTP only, engine-independent.
set -euo pipefail
umask 077   # the header files below hold credentials
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

case "${1:-}" in
  -h | --help)
    org_usage "$0"
    exit 0
    ;;
esac

command -v curl >/dev/null 2>&1 || org_die "curl is required"
[ -f "$ORG_ENV_FILE" ] || org_die ".env.org not found - run ./up.sh first"
org_check_tokenlean

CP=$(org_url ORG_CP_PORT 8090)
GRAFANA=$(org_url ORG_GRAFANA_PORT 3300)
LITE_PORT=$(org_lite_value LITE_PROXY_PORT)
GATEWAY="http://127.0.0.1:${LITE_PORT:-4100}"
MODE=$(sed -n 's/^LITE_MODE=//p' "$TOKENLEAN_DIR/deploy/lite/local/.state/stack.env" 2>/dev/null \
  | tr -d '\r' || true)
[ "$MODE" = mock ] || org_die "the LITE stack is not in mock mode - start it with ./up.sh"

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t org-smoke)
trap 'rm -rf "$TMP"' EXIT
# Credentials go to curl in header files (curl -H @file), never as arguments.
printf 'Authorization: Basic %s\n' \
  "$(printf 'admin:%s' "$(org_value GRAFANA_ADMIN_PASSWORD)" | base64 | tr -d '\n')" \
  >"$TMP/grafana-auth"
printf 'Authorization: Bearer %s\n' "$(org_lite_value TOKENLEAN_API_KEY)" >"$TMP/gateway-auth"
RUN="smoke-$(date +%s)-$$"
FAILS=0

pass() { echo "PASS  $1"; }
fail() {
  echo "FAIL  $1${2:+ ($2)}"
  FAILS=$((FAILS + 1))
}
get() { curl -sS "$@" 2>/dev/null || true; }
grafana() { get -H @"$TMP/grafana-auth" "$@"; }
count() { { grep -o "$1" || true; } | wc -l | tr -d ' '; }

echo "org smoke: control plane $CP  grafana $GRAFANA  gateway $GATEWAY  run $RUN"

# 1. control plane
case "$(get "$CP/healthz")" in *'"status":"ok"'*) pass "control plane live" ;; *) fail "control plane live" ;; esac
BODY=$(get "$CP/readyz")
case "$BODY" in
  *'"db_enforced":true'*) pass "control plane ready (migrated, isolation enforced)" ;;
  *) fail "control plane ready" "$BODY" ;;
esac

# 2. Grafana, its datasources and dashboards
case "$(get "$GRAFANA/api/health")" in *'"database"'*'"ok"'*) pass "grafana healthy" ;; *) fail "grafana healthy" ;; esac
for uid in agentic-postgres app-postgres; do
  BODY=$(grafana "$GRAFANA/api/datasources/uid/$uid/health")
  case "$BODY" in *'"status":"OK"'*) pass "datasource $uid answers" ;; *) fail "datasource $uid answers" "$BODY" ;; esac
done
N=$(grafana "$GRAFANA/api/search?tag=agentic&type=dash-db" | count '"uid":"agentic-')
if [ "$N" = 7 ]; then pass "7 framework dashboards provisioned"; else fail "7 framework dashboards provisioned" "found $N"; fi
N=$(grafana "$GRAFANA/api/search?type=dash-db&query=" | count '"folderTitle":"Gateway"')
EXPECTED=$(echo "$ORG_LITE_DASHBOARDS" | wc -w | tr -d ' ')
if [ "$N" = "$EXPECTED" ]; then pass "$EXPECTED gateway LITE dashboards provisioned"; else fail "gateway LITE dashboards provisioned" "found $N of $EXPECTED"; fi

# 3. the gateway, and one tagged call through it
case "$(get "$GATEWAY/health")" in *'"status":"ok"'*) pass "gateway healthy" ;; *) fail "gateway healthy" ;; esac
CALL="{\"model\":\"lite-mock-1\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"ping $RUN\"}]}"
STATUS=$(curl -sS -o "$TMP/call" -w '%{http_code}' -H @"$TMP/gateway-auth" \
  -H "Content-Type: application/json" -H "X-Agentic-Run-Id: $RUN" -H "X-Agentic-Step: smoke" \
  -H "X-Agentic-Agent: smoke@1.0.0" -d "$CALL" "$GATEWAY/v1/chat/completions" 2>/dev/null || true)
if [ "$STATUS" = 200 ]; then pass "tagged call through the gateway (mock)"; else fail "tagged call through the gateway" "HTTP $STATUS"; fi

# 4. the call in the tenant's dashboard view, through Grafana's datasource (the reader)
QUERY="{\"from\":\"now-1h\",\"to\":\"now\",\"queries\":[{\"refId\":\"A\",\"format\":\"table\",\"rawQuery\":true,\"datasource\":{\"uid\":\"agentic-postgres\"},\"rawSql\":\"SELECT count(*) AS n FROM dash_calls WHERE run_id = '$RUN'\"}]}"
SEEN=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  BODY=$(grafana -H "Content-Type: application/json" -d "$QUERY" "$GRAFANA/api/ds/query")
  case "$BODY" in *'"values":[[1]]'*) SEEN=1 && break ;; esac
  sleep 3
done
if [ -n "$SEEN" ]; then pass "the call is in dash_calls for the tenant"; else fail "the call is in dash_calls" "${BODY:0:300}"; fi

if [ "$FAILS" -gt 0 ]; then
  echo "org smoke: $FAILS check(s) failed"
  exit 1
fi
echo "org smoke: all checks passed"
