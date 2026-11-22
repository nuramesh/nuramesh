#!/bin/sh
# Generates the stack's secrets on first start (R1.29, spec 5.1). Idempotent: it never overwrites
# an existing file and prints names only, never a value. Run it once before `docker compose up`:
#   sh deploy/compose/init-secrets.sh
#
# secrets/ is mode 0700 (nobody but the operator and root can traverse it on the host); each file
# is 0644 because Compose bind-mounts secret files individually and the services run as different
# uids (10001, 70, 101): see spec 5.1 "Ownership scheme". Nothing here is ever committed
# (.gitignore); the passwords are 64 URL-safe characters from /dev/urandom via openssl.
set -eu
umask 077

here=$(cd "$(dirname "$0")" && pwd)
secrets="$here/secrets"

if ! command -v openssl >/dev/null 2>&1; then
  echo "init-secrets: openssl is required and was not found" >&2
  exit 1
fi

# Docker preflight (does not abort the secret generation below; the exit status reports it).
status=0
sh "$here/check-docker.sh" --preflight || status=$?

mkdir -p "$secrets"
chmod 700 "$secrets"

new_password() { # one secret file: 48 random bytes, URL-safe base64 (64 characters)
  name=$1
  if [ -e "$secrets/$name" ]; then
    echo "kept    $name"
    return
  fi
  openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_' >"$secrets/$name.tmp"
  chmod 644 "$secrets/$name.tmp"
  mv "$secrets/$name.tmp" "$secrets/$name"
  echo "created $name"
}

for name in postgres_password nuramesh_owner_password nuramesh_app_password temporal_db_password refresh_successor_key; do
  new_password "$name"
done

if [ -e "$secrets/web_tls_key" ] && [ -e "$secrets/web_tls_cert" ]; then
  echo "kept    web_tls_key"
  echo "kept    web_tls_cert"
elif [ -e "$secrets/web_tls_key" ] || [ -e "$secrets/web_tls_cert" ]; then
  echo "init-secrets: only one of web_tls_key and web_tls_cert exists; remove both to make a new pair" >&2
  exit 1
else
  # local self-signed certificate: 825 days, SAN localhost and 127.0.0.1 (canonical URL is the IP)
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 \
    -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    -keyout "$secrets/web_tls_key.tmp" -out "$secrets/web_tls_cert.tmp" >/dev/null 2>&1
  chmod 644 "$secrets/web_tls_key.tmp" "$secrets/web_tls_cert.tmp"
  mv "$secrets/web_tls_key.tmp" "$secrets/web_tls_key"
  mv "$secrets/web_tls_cert.tmp" "$secrets/web_tls_cert"
  echo "created web_tls_key"
  echo "created web_tls_cert"
fi

if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" = "Enforcing" ]; then
  echo "SELinux is enforcing: run  chcon -t container_file_t $secrets/*" >&2
fi
exit "$status"
