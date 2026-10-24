#!/bin/sh
# First start of the control plane's Postgres (run once by the image's entrypoint, as the
# superuser, on an empty data directory). The one-time DBA steps the control plane needs:
#   - cp_owner: owns the database and schema, runs migrations, creates the runtime and
#     reader roles (CREATEROLE; never SUPERUSER or BYPASSRLS)
#   - the database, and postgres_fdw in it for the gateway usage link (`db link-usage`)
#   - SET on app.tenant_id for cp_owner, so it can give a reader role its tenant
# psql reads the password from the environment (\getenv), never from an argument.
set -eu

psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
\getenv owner_password CP_OWNER_PASSWORD
\getenv database CP_DATABASE
CREATE ROLE cp_owner LOGIN CREATEROLE PASSWORD :'owner_password';
CREATE DATABASE :"database" OWNER cp_owner;
\connect :"database"
CREATE EXTENSION IF NOT EXISTS postgres_fdw;
GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO cp_owner;
GRANT SET ON PARAMETER app.tenant_id TO cp_owner;
SQL
