#!/bin/sh
# Rotate ONE database secret on a running stack (R1.29, spec 5.5).
#   sh deploy/compose/rotate-secret.sh postgres_password|nuramesh_owner_password|nuramesh_app_password|temporal_db_password [--resume]
#
# POSTGRES_PASSWORD_FILE and the role script are read only by initdb, so replacing a file alone
# would lock the stack out. The script (1) writes secrets/<name>.new, (2) stops the services that
# use the role, (3) sends `ALTER ROLE ... PASSWORD '<client-side SCRAM-SHA-256 verifier>'` to
# psql through STDIN (never argv; the verifier means a failing statement logs no plaintext),
# authenticating with the CURRENT password read from the HOST file (the container's bind mount
# keeps the old inode), (4) only after psql succeeds, renames .new over the old file, (5) recreates
# the consumers (`up -d --force-recreate --wait`; for the owner secret also re-runs `migrate`).
# A failure INSIDE (3) means the server may not have the new value: secrets/<name>.new is
# deleted, the stopped services are started again and --resume is never suggested. If it fails
# after (3) succeeded and before (4), secrets/<name>.new is kept; --resume first PROVES the
# server accepts the .new value (a psql login as the role) and refuses otherwise, then does
# (4) and (5).
# Prints names only. Needs python3 (SCRAM verifier) and `docker compose`; run from anywhere.
set -eu
umask 077

here=$(cd "$(dirname "$0")" && pwd)
secrets="$here/secrets"
name=${1:-}
resume=0
[ "${2:-}" = "--resume" ] && resume=1
compose="docker compose -f $here/compose.yml"

case $name in
  postgres_password) role=postgres; service=postgres; admin=postgres; admin_file=postgres_password; db=${NURAMESH_DB_NAME:-nuramesh}
    stop="api runner worker retention migrate"; recreate="postgres api runner worker retention" ;;
  nuramesh_owner_password) role=nuramesh_owner; service=postgres; admin=postgres; admin_file=postgres_password; db=${NURAMESH_DB_NAME:-nuramesh}
    stop="api runner worker retention migrate"; recreate="" ;;
  nuramesh_app_password) role=nuramesh_app; service=postgres; admin=postgres; admin_file=postgres_password; db=${NURAMESH_DB_NAME:-nuramesh}
    stop="api runner worker retention migrate"; recreate="api runner worker retention" ;;
  temporal_db_password) role=temporal; service=temporal-db; admin=temporal; admin_file=temporal_db_password; db=temporal
    stop="temporal worker"; recreate="temporal-db temporal worker" ;;
  *)
    echo "usage: rotate-secret.sh postgres_password|nuramesh_owner_password|nuramesh_app_password|temporal_db_password [--resume]" >&2
    exit 2 ;;
esac
# Temporal is an opt-in profile (ND399 OD-3): name its services only when this stack has them, or
# `up` would start a Temporal nobody asked for (and `stop` would refuse an inactive service).
if [ -n "$($compose --profile temporal ps -aq worker 2>/dev/null)" ]; then
  export COMPOSE_PROFILES=temporal
else
  drop_temporal() { for s in $1; do case $s in worker | temporal | temporal-db) ;; *) printf '%s ' "$s" ;; esac; done; }
  stop=$(drop_temporal "$stop")
  recreate=$(drop_temporal "$recreate")
  if [ "$name" = temporal_db_password ]; then
    echo "rotate-secret: the temporal profile is not in use on this stack; nothing runs that secret" >&2
    exit 2
  fi
fi
# the postgres superuser's own rotation: after it, the new file is also the admin credential
fail() { echo "rotate-secret: $*" >&2; exit 1; }

new="$secrets/$name.new"
# a psql login as the role with the password on stdin (never argv): exit 0 only when the server
# accepts it; the output is kept in $proof_out
proof_out=
prove_new() {
  proof_out=$(
    { cat "$new"; printf '\n'; } | $compose exec -T "$service" sh -c \
      'IFS= read -r PGPASSWORD; export PGPASSWORD; psql -h 127.0.0.1 -v ON_ERROR_STOP=1 -q -t -A -U '"$role"' -d '"$db"' -c "select 1"' 2>&1
  )
}
start_stopped() {
  # shellcheck disable=SC2086
  $compose start $stop >/dev/null 2>&1 || true
}
if [ "$resume" = 1 ]; then
  [ -f "$new" ] || fail "nothing to resume: secrets/$name.new does not exist"
  if ! prove_new; then
    case $proof_out in
      *"password authentication failed"*)
        rm -f "$new"
        fail "the server does not accept secrets/$name.new (ALTER ROLE was never applied); it was deleted and secrets/$name is unchanged: run again without --resume" ;;
      *) fail "could not check secrets/$name.new against the server (is $service running?); nothing was renamed, secrets/$name.new is kept: fix the cause and run with --resume again" ;;
    esac
  fi
  echo "step 3: the server accepts secrets/$name.new"
else
  [ -r "$secrets/$name" ] || fail "cannot read secrets/$name (run init-secrets.sh first)"
  [ -r "$secrets/$admin_file" ] || fail "cannot read secrets/$admin_file: the current $admin password is needed to authenticate; nothing was changed"
  rm -f "$new"
  openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_' >"$new"
  chmod 644 "$new"
  echo "step 1: wrote secrets/$name.new"
  # (2) stop the users of the role
  # shellcheck disable=SC2086
  $compose stop $stop >/dev/null 2>&1 || true
  echo "step 2: stopped $stop"
  # (3) ALTER ROLE through stdin. Line 1 is the admin password for `sh -c`, the rest is SQL.
  verifier=$(python3 -I -c '
import base64, hashlib, hmac, os, sys
pw = open(sys.argv[1]).read().strip().encode()
salt = os.urandom(16); it = 4096
sp = hashlib.pbkdf2_hmac("sha256", pw, salt, it)
ck = hmac.new(sp, b"Client Key", hashlib.sha256).digest()
sk = hmac.new(sp, b"Server Key", hashlib.sha256).digest()
b = lambda x: base64.b64encode(x).decode()
print(f"SCRAM-SHA-256${it}:{b(salt)}${b(hashlib.sha256(ck).digest())}:{b(sk)}")
' "$new")
  out=$(
    {
      cat "$secrets/$admin_file"
      printf '\n'
      printf "SET log_min_error_statement = panic;\nALTER ROLE %s PASSWORD '%s';\n" "$role" "$verifier"
    } | $compose exec -T "$service" sh -c \
      'IFS= read -r PGPASSWORD; export PGPASSWORD; psql -v ON_ERROR_STOP=1 -q -U '"$admin"' -d '"$db"' -f -' 2>&1
  ) || {
    # psql can fail after ALTER ROLE committed (a dropped connection): ask the server before
    # deleting the only copy of the new value
    if prove_new; then
      echo "step 3: role $role updated (psql reported a failure, but the server accepts secrets/$name.new)"
    else
      # the server does not have the new value: never keep it for --resume, start the services again
      rm -f "$new"
      start_stopped
      case $out in
        *"password authentication failed"*) fail "could not authenticate with secrets/$admin_file at step 3; nothing was changed (services started again)" ;;
        *) fail "step 3 (ALTER ROLE $role) failed; secrets/$name is unchanged, secrets/$name.new was deleted and the services were started again: fix the cause and run again from the start" ;;
      esac
    fi
  }
  echo "step 3: role $role updated"
fi

step5_failed() {
  fail "step 5 failed: secrets/$name was already replaced (step 4); run \`docker compose -f $here/compose.yml up -d --wait\` and check \`docker compose logs\`"
}
# (4) atomic rename of the file; (5) recreate the consumers
mv "$new" "$secrets/$name"
echo "step 4: secrets/$name replaced"
if [ -n "$recreate" ]; then
  # shellcheck disable=SC2086
  $compose up -d --force-recreate --wait $recreate >/dev/null 2>&1 || step5_failed
fi
$compose up -d --wait >/dev/null 2>&1 || step5_failed
if [ "$name" = "nuramesh_owner_password" ]; then
  $compose run --rm migrate >/dev/null || fail "migrate failed after the owner password rotation"
fi
echo "step 5: stack healthy with the new $name"
