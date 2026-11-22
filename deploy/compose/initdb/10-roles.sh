#!/bin/sh
# Creates the NuraMesh database roles once, on an empty data directory (R1.29, spec 5.6, ND188 N2).
# Run by the postgres image entrypoint (as the superuser, through the temporary bootstrap server).
# Reads the two role passwords from their secret files; no password is in this text or any log
# (log_min_error_statement is set to panic for this session so a failing statement never echoes it).
#
#   nuramesh_owner: owns the database, schema, tables, functions and triggers; used by `migrate` only.
#   nuramesh_definer: NOLOGIN, owns the SECURITY DEFINER functions (migration 22 and later check it
#                   and refuse a missing, login-capable or privileged role); the owner is made a
#                   member (GRANT) because PostgreSQL 16 lets a role hand an object to another role
#                   only if it can SET ROLE to it. Existing stacks and managed Postgres: run the same
#                   two statements once (docs/upgrade-notes.md, docs/self-hosting.md).
#   nuramesh_app:   owns nothing; the grants are applied by `nuramesh db migrate --grant-to` once the
#                   tables exist; used by api, worker and retention.
set -eu

db=${POSTGRES_DB:?POSTGRES_DB is not set}
secrets=${NURAMESH_INITDB_SECRETS:-/run/secrets} # a test points this at a temporary folder
export secrets # psql runs the cat commands below in a child shell
for f in "$secrets/nuramesh_owner_password" "$secrets/nuramesh_app_password"; do
  [ -r "$f" ] || { echo "10-roles: cannot read $f" >&2; exit 1; }
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" -v dbname="$db" <<'SQL'
SET log_min_error_statement = panic;
SET password_encryption = 'scram-sha-256';
\set owner_pw `cat "$secrets/nuramesh_owner_password"`
\set app_pw `cat "$secrets/nuramesh_app_password"`
CREATE ROLE nuramesh_owner LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS PASSWORD :'owner_pw';
CREATE ROLE nuramesh_app LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS PASSWORD :'app_pw';
CREATE ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT nuramesh_definer TO nuramesh_owner;
\unset owner_pw
\unset app_pw
REVOKE TEMPORARY ON DATABASE :"dbname" FROM PUBLIC;
ALTER DATABASE :"dbname" OWNER TO nuramesh_owner;
ALTER SCHEMA public OWNER TO nuramesh_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
echo "10-roles: roles nuramesh_owner, nuramesh_app and nuramesh_definer created"
