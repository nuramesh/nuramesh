#!/bin/sh
# Fail-closed Docker checks (R1.29, spec 7.3, ND188 N1).
#   sh deploy/compose/check-docker.sh [--preflight] [--file COMPOSE_FILE] [--project-name NAME]
# --preflight: only "Docker and Compose v2 are present" (used by init-secrets.sh before the
#   network exists). Without it the `app` network of the project (default `nuramesh`) must exist
#   (fixtures are chosen by --project-name, never by exported COMPOSE_* variables; --file only
#   has to name an existing compose file, the network is looked up by project name)
#   and be isolated:
#   - `internal: true` and the bridge option gateway_mode_ipv4=isolated (docker network inspect);
#   - on a Linux Engine host, by EFFECT: the network's bridge has no IPv4 address inside
#     172.29.40.0/24 and `ip route get 172.29.40.10` does not go via it. If the bridge cannot be
#     found or `ip` is missing, the check FAILS (it never passes silently).
#   Docker Desktop (docker info says so) runs the bridge inside a VM: only the inspect checks apply.
#   Rootless Docker keeps the bridge in the daemon's namespace: the inspect checks plus a host-side
#   probe (no route to, and no TCP answer from, 172.29.40.10) replace the bridge-address check;
#   PASS on rootless is therefore weaker than on a rootful Linux host.
# Exit 0 when everything required holds, else 1 with a message naming what is missing.
set -u

subnet_prefix="172.29.40."
api_ip="172.29.40.10"
project=nuramesh
compose_file=
preflight=0
while [ "$#" -gt 0 ]; do
  case $1 in
    --preflight) preflight=1 ;;
    --project-name)
      shift
      project=${1:-}
      ;;
    --file)
      shift
      compose_file=${1:-}
      [ -f "$compose_file" ] || { echo "check-docker: no such compose file: $compose_file" >&2; exit 1; }
      ;;
    *)
      echo "usage: check-docker.sh [--preflight] [--file COMPOSE_FILE] [--project-name NAME]" >&2
      exit 1
      ;;
  esac
  shift
done

fail() {
  echo "check-docker: $*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker was not found on PATH"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 ('docker compose') is required"
[ "$preflight" = 1 ] && exit 0

net="${project}_app"
info=$(docker network inspect -f '{{.Internal}}|{{with index .Options "com.docker.network.bridge.gateway_mode_ipv4"}}{{.}}{{end}}|{{.Id}}|{{with index .Options "com.docker.network.bridge.name"}}{{.}}{{end}}' "$net" 2>/dev/null) ||
  fail "the network $net does not exist yet (run 'docker compose up -d' first, then this check again)"
internal=$(printf '%s' "$info" | cut -d'|' -f1)
mode=$(printf '%s' "$info" | cut -d'|' -f2)
id=$(printf '%s' "$info" | cut -d'|' -f3)
bridge=$(printf '%s' "$info" | cut -d'|' -f4)

[ "$internal" = "true" ] || fail "network $net lacks 'internal: true'"
[ "$mode" = "isolated" ] || fail "network $net lacks the driver option com.docker.network.bridge.gateway_mode_ipv4: isolated"

os=$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)
case $os in
  *"Docker Desktop"*)
    echo "check-docker: Docker Desktop: the bridge is inside the VM; only the network options were checked"
    exit 0
    ;;
esac
# Rootless Engine: the bridge lives in the daemon's own network namespace, invisible to the host's
# `ip`, so the bridge-address check cannot run (a known, accepted deviation). It is
# NOT skipped silently: the host-side effect is probed instead. The host must have no route to the
# api address except a default route, and a TCP connect from the host to the api must neither
# succeed nor be refused (a refusal is an answer from a reachable host). Rootful Linux gets the
# full bridge check below. Needs python3 for the probe; without it the check fails.
case $(docker info --format '{{.SecurityOptions}}' 2>/dev/null || true) in
  *rootless*)
    command -v python3 >/dev/null 2>&1 || fail "rootless Docker: python3 is needed to probe $api_ip from the host"
    if command -v ip >/dev/null 2>&1; then
      route=$(ip route get "$api_ip" 2>/dev/null)
      rc=$?
      if [ "$rc" -eq 0 ]; then
        case $route in
          *"$api_ip dev "*) fail "the host has a direct route to $api_ip ($route): the api address is reachable from the host" ;;
        esac
      fi
    fi
    if python3 -I -c '
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
except ConnectionRefusedError:
    sys.exit(0)
except OSError:
    sys.exit(1)
' "$api_ip" 8765; then
      fail "the host reached $api_ip:8765: the api address is reachable from the host"
    fi
    echo "check-docker: rootless Docker: network options ok; $api_ip is not reachable from the host (the bridge address check does not apply)"
    exit 0
    ;;
esac
if [ "$(uname -s)" != "Linux" ]; then
  echo "check-docker: not a Linux Engine host; only the network options were checked"
  exit 0
fi

command -v ip >/dev/null 2>&1 || fail "the 'ip' command is not installed, so the isolation of $net cannot be proved"
[ -n "$bridge" ] || bridge="br-$(printf '%s' "$id" | cut -c1-12)"
addrs=$(ip -4 -o addr show dev "$bridge" 2>/dev/null) ||
  fail "the bridge interface $bridge of $net was not found, so the isolation cannot be proved"
case $addrs in
  *" ${subnet_prefix}"*) fail "the bridge $bridge has an IPv4 address inside ${subnet_prefix}0/24: the api address is reachable from the host" ;;
esac
route=$(ip route get "$api_ip" 2>/dev/null)
rc=$?
if [ "$rc" -eq 0 ]; then
  case $route in
    *"dev $bridge"*) fail "the host routes $api_ip via $bridge: the api address is reachable from the host" ;;
  esac
elif [ "$rc" -ne 2 ]; then
  fail "'ip route get $api_ip' failed (exit $rc), so the isolation of $net cannot be proved"
fi
# rc 2: "Network is unreachable" (no default route): not via the bridge, a pass
echo "check-docker: ok ($net is isolated; bridge $bridge has no address in ${subnet_prefix}0/24)"
exit 0
