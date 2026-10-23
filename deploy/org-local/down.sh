#!/usr/bin/env bash
# Stop the local org stack, then the gateway's LITE stack. Data volumes are kept.
#
#   TOKENLEAN_DIR=/path/to/token-optimisation ./down.sh [--purge] [--keep-lite]
#
#   --purge      also delete the volumes (control-plane database, Grafana; and the LITE
#                stack's). .env.org is kept, so the next ./up.sh reuses its passwords.
#   --keep-lite  leave the gateway's LITE stack running
#
# Engine: CONTAINER_ENGINE=docker (default) or podman.
set -euo pipefail
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

PURGE=0
KEEP_LITE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --keep-lite) KEEP_LITE=1 ;;
    -h | --help)
      org_usage "$0"
      exit 0
      ;;
    *) org_die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

org_check_engine
[ -f "$ORG_ENV_FILE" ] || org_die ".env.org not found - nothing was started from here"

DOWN_ARGS=(down --remove-orphans)
if [ "$PURGE" = 1 ]; then DOWN_ARGS+=(--volumes); fi
org_compose "${DOWN_ARGS[@]}"
echo "org: stopped$([ "$PURGE" = 1 ] && echo ' and purged')"

if [ "$KEEP_LITE" = 0 ]; then
  org_check_tokenlean
  LITE_ARGS=()
  if [ "$PURGE" = 1 ]; then LITE_ARGS+=(--purge); fi
  CONTAINER_ENGINE="$ORG_ENGINE" bash "$TOKENLEAN_DIR/deploy/lite/local/down.sh" \
    ${LITE_ARGS[@]+"${LITE_ARGS[@]}"}
fi
