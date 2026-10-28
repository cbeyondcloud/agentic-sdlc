#!/bin/sh
# One-shot setup of the local org stack's control plane (cp-init service). Safe to run on
# every `up`: each step creates or re-aligns, and the tenant is created only once.
#   1. migrate the database (owner role) and create or re-align the runtime role
#   2. create the tenant ORG_TENANT, bound to the gateway tenant GATEWAY_TENANT
#   3. point the dashboards' usage views at the gateway's database (postgres_fdw)
#   4. bind the tenant's Grafana reader role (the dash_* views, its own rows only)
# Passwords stay in the environment: the commands read them by variable name.
set -eu

agentic_cp() { python -m agentic_cp "$@"; }

agentic_cp migrate
agentic_cp db grant-runtime

if agentic_cp admin tenant list | cut -f1 | grep -qx "$ORG_TENANT"; then
  echo "cp-init: tenant $ORG_TENANT exists"
else
  # the first admin's token is printed once; it is not kept in the logs (issue a new one
  # with `admin issue-token`, see README)
  agentic_cp admin tenant create --slug "$ORG_TENANT" --name "Local org" \
    --admin-email "$ORG_ADMIN_EMAIL" --gateway-tenant "$GATEWAY_TENANT" >/dev/null
  echo "cp-init: tenant $ORG_TENANT created (gateway tenant $GATEWAY_TENANT)"
fi

agentic_cp db link-usage --host "$GATEWAY_DB_HOST" --dbname "$GATEWAY_DB_NAME" \
  --user "$GATEWAY_DB_USER" --password-env GATEWAY_DB_PASSWORD
agentic_cp db dashboard-role --tenant "$ORG_TENANT" --role "$AGENTIC_DASH_READER" \
  --password-env AGENTIC_DASH_READER_PASSWORD
echo "cp-init: done"
