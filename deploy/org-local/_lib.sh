# shellcheck shell=bash
# Shared by up.sh, down.sh and smoke.sh. Bash 3.2+ (the macOS default), Linux, Git Bash.
# No jq, no GNU-only flags.

ORG_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ORG_ENV_FILE="$ORG_DIR/.env.org"
ORG_STATE_DIR="$ORG_DIR/.state"
ORG_COMPOSE_FILE="$ORG_DIR/docker-compose.yml"
# Every compose call names this project explicitly (-p), so nothing else can redirect it.
ORG_PROJECT=agentic-org
# docker (default) or podman, as in scripts/check.py. Both are driven as `<engine> compose`.
ORG_ENGINE="${CONTAINER_ENGINE:-docker}"
# The gateway's LITE dashboard set (spec 25), copied from its checkout into .state/.
ORG_LITE_DASHBOARDS="live requests billing latency-breakup sla trust-safety trends quarterly"

# Compose settings exported for another stack must never steer these scripts.
unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES

org_die() {
  echo "org: $*" >&2
  exit 1
}

# Print a script's header comment (the lines after the shebang, up to the first non-comment).
org_usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$1"
}

org_check_engine() {
  case "$ORG_ENGINE" in
    docker | podman) ;;
    *) org_die "CONTAINER_ENGINE=$ORG_ENGINE: use docker or podman" ;;
  esac
  command -v "$ORG_ENGINE" >/dev/null 2>&1 \
    || org_die "'$ORG_ENGINE' not found (set CONTAINER_ENGINE=docker or podman)"
  "$ORG_ENGINE" compose version >/dev/null 2>&1 \
    || org_die "'$ORG_ENGINE compose' is not available (Docker Compose v2, or podman with a compose provider)"
  command -v curl >/dev/null 2>&1 || org_die "curl is required"
}

# The gateway's checkout (read only): TOKENLEAN_DIR, holding deploy/lite/local (the LITE
# profile) and the usage tags (ST-1 team, ST-2 tags columns of usage_events).
ORG_TL_HINT="needs the LITE profile (deploy/lite/local) and the usage tags (ST-1 team, ST-2 tags) together - see README"
org_check_tokenlean() {
  [ -n "${TOKENLEAN_DIR:-}" ] \
    || org_die "set TOKENLEAN_DIR to a TokenLean checkout that $ORG_TL_HINT"
  [ -f "$TOKENLEAN_DIR/deploy/lite/local/up.sh" ] \
    || org_die "TOKENLEAN_DIR=$TOKENLEAN_DIR has no deploy/lite/local/up.sh: it $ORG_TL_HINT"
  ORG_LITE_ENV="$TOKENLEAN_DIR/deploy/lite/local/.env.lite"
}

# up only (down and smoke must work on any checkout): without the usage tags `db link-usage`
# refuses minutes into the first build, so this stops before anything is built.
org_check_usage_tags() {
  local billing="$TOKENLEAN_DIR/infra/migrations/billing.sql"
  { [ -f "$billing" ] && grep -qi 'column if not exists team ' "$billing" \
    && grep -qi 'tags jsonb' "$billing"; } \
    || org_die "TOKENLEAN_DIR=$TOKENLEAN_DIR has no usage_events team/tags columns in infra/migrations/billing.sql: it $ORG_TL_HINT"
}

# A value from the LITE stack's .env.lite (empty when missing).
org_lite_value() {
  [ -f "$ORG_LITE_ENV" ] || org_die "the LITE stack has no .env.lite yet - its up did not run"
  sed -n "s/^$1=//p" "$ORG_LITE_ENV" | head -n 1 | tr -d '\r'
}

# .env.org edited in a Windows editor gets CRLF line ends; drop the CRs.
org_normalize_env_file() {
  local tmp="$ORG_ENV_FILE.tmp"
  tr -d '\r' <"$ORG_ENV_FILE" >"$tmp"
  mv "$tmp" "$ORG_ENV_FILE"
}

# Set KEY in .env.org: `org_fill` only where it is empty or missing, `org_set` always. The
# value reaches awk through its environment, never as an argument (it may be a secret).
org_put() {
  local key=$1 value=$2 overwrite=$3 tmp="$ORG_ENV_FILE.tmp"
  if grep -q "^$key=" "$ORG_ENV_FILE"; then
    if [ "$overwrite" = 1 ] || grep -q "^$key=$" "$ORG_ENV_FILE"; then
      ORG_PUT_VALUE="$value" awk -v k="$key" \
        'index($0, k "=") == 1 { print k "=" ENVIRON["ORG_PUT_VALUE"]; next } { print }' \
        "$ORG_ENV_FILE" >"$tmp"
      mv "$tmp" "$ORG_ENV_FILE"
    fi
  else
    printf '%s=%s\n' "$key" "$value" >>"$ORG_ENV_FILE"
  fi
}
org_fill() { org_put "$1" "$2" 0; }
org_set() { org_put "$1" "$2" 1; }

# A value from .env.org (empty when missing).
org_value() {
  sed -n "s/^$1=//p" "$ORG_ENV_FILE" | head -n 1 | tr -d '\r'
}

org_compose() {
  "$ORG_ENGINE" compose -p "$ORG_PROJECT" -f "$ORG_COMPOSE_FILE" --env-file "$ORG_ENV_FILE" "$@"
}

org_url() {
  local port
  port=$(org_value "$1")
  echo "http://127.0.0.1:${port:-$2}"
}
