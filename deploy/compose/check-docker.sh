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
#   No container of the project may mount a container socket (docker.sock, podman.sock,
#   containerd.sock, anything under /run/user/): the real-models spec R49. Checked first, on what
#   `docker inspect` reports for every container of the project; if the containers cannot be
#   listed or read, the check FAILS.
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

# R49 (real-models spec): a container that mounts a container socket controls the host's engine.
containers=$(docker ps -a -q --filter "label=com.docker.compose.project=$project" 2>/dev/null) ||
  fail "the containers of project $project cannot be listed, so 'no container socket is mounted' cannot be proved (R49)"
docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || docker_root=
case $docker_root in
  /*) docker_root=${docker_root%/} ;;
  *) fail "the engine's root folder (DockerRootDir) cannot be read, so 'no engine data folder is mounted' cannot be proved (R49)" ;;
esac
[ -n "$docker_root" ] || fail "the engine's root folder is /, which no mount may hold (R49)"

# refuse_source RAW: one bind source (or a volume's device) through the normalising and the rules
refuse_source() {
  # normalise first (pure shell): squeeze repeated slashes, drop /./ and a trailing /. or /
  src=$1
  while :; do
    case $src in
      *//*) src="${src%%//*}/${src#*//}" ;;
      */./*) src="${src%%/./*}/${src#*/./}" ;;
      */.) src=${src%/.} ;;
      */) src=${src%/} ;;
      *) break ;;
    esac
  done
  [ -n "$src" ] || src=/
  # the engine's own root folder (docker info DockerRootDir), every folder above it and
  # everything below it except a named volume's own folder (volumes/<name>/_data)
  case $src in
    "$docker_root"/volumes/*/_data | "$docker_root"/volumes/*/_data/*) ;;
    "$docker_root" | "$docker_root"/*)
      fail "a container mounts $1, which is inside the engine's root folder $docker_root and gives the control a container socket gives ($mounted): refused (R49)"
      ;;
  esac
  case $docker_root/ in
    "$src"/*)
      fail "a container mounts $1, which holds the engine's root folder $docker_root and gives the control a container socket gives ($mounted): refused (R49)"
      ;;
  esac
  # every engine's data folder, whichever engine runs (R49, #1164): podman storage, another
  # user's rootless docker folder, and /var/lib/docker, wholesale (children included) except a
  # named volume's own volumes/<name>/_data folder
  # the volume exception is exactly volumes/<one name>/_data: the name cannot hold a slash
  vrest=${src#*/volumes/}
  vname=${vrest%%/*}
  vol_ok=0
  case $vrest in
    "$vname"/_data | "$vname"/_data/*) vol_ok=1 ;;
  esac
  case $src in
    /var/lib/docker | /var/lib/containers/storage | \
      /home/*/.local/share/docker | /root/.local/share/docker | \
      /home/*/.local/share/containers/storage | /root/.local/share/containers/storage)
      fail "a container mounts $1, which is an engine's data folder and gives the control a container socket gives ($mounted): refused (R49)"
      ;;
    /var/lib/docker/* | /var/lib/containers/storage/* | \
      /home/*/.local/share/docker/* | /root/.local/share/docker/* | \
      /home/*/.local/share/containers/storage/* | /root/.local/share/containers/storage/*)
      case $src in
        /var/lib/docker/volumes/* | /var/lib/containers/storage/volumes/* | \
          /home/*/.local/share/docker/volumes/* | /root/.local/share/docker/volumes/* | \
          /home/*/.local/share/containers/storage/volumes/* | \
          /root/.local/share/containers/storage/volumes/*)
          [ "$vol_ok" = 1 ] && vol_ok=2
          ;;
      esac
      [ "$vol_ok" = 2 ] ||
        fail "a container mounts $1, which is inside an engine's data folder and gives the control a container socket gives ($mounted): refused (R49)"
      ;;
  esac
  case $src in
    / | /run | /var | /var/run | /run/user | /var/run/user | \
      /run/docker | /run/docker/* | /run/containerd | /run/containerd/* | \
      /run/podman | /run/podman/* | /run/crio | /run/crio/* | \
      /var/run/docker | /var/run/docker/* | /var/run/containerd | /var/run/containerd/* | \
      /var/run/podman | /var/run/podman/* | /var/run/crio | /var/run/crio/* | *crio.sock | \
      /run/k3s | /run/k3s/* | /var/run/k3s | /var/run/k3s/* | /run/*.sock | /var/run/*.sock | \
      /run/user/* | /var/run/user/*)
      fail "a container mounts $1, which is or contains a container socket ($mounted): refused (R49)"
      ;;
    # the host's process tree (/proc/1/root is the host's root) and the engine's own data
    # folders give the same control as a container socket: the rootful default data folder
    # (/var/lib/docker, /var/lib/containers, containerd), the folders that hold every
    # volume, container and layer, and the rootless data folders (~/.local/share/docker and
    # /containers). A named volume's own folder BELOW them (.../volumes/<name>/_data) is an
    # ordinary mount and passes.
    /proc | /proc/* | /var/lib | /var/lib/docker | /var/lib/containers | \
      /var/lib/docker/volumes | /var/lib/docker/containers | /var/lib/docker/overlay2 | \
      /var/lib/docker/overlay2/* | /var/lib/docker/containers/* | /var/lib/containers/storage | \
      /var/lib/containerd | /var/lib/containerd/* | \
      /home/*/.local/share/docker | /home/*/.local/share/containers | \
      /root/.local/share/docker | /root/.local/share/containers | \
      /home/*/.local/share/docker/volumes | /home/*/.local/share/docker/overlay2 | \
      /home/*/.local/share/docker/overlay2/* | /root/.local/share/docker/volumes | \
      /root/.local/share/docker/overlay2 | /root/.local/share/docker/overlay2/* | \
      /var/lib/containers/storage/overlay | /var/lib/containers/storage/overlay/* | \
      /var/lib/containers/storage/volumes | \
      /var/lib/docker/image | /var/lib/docker/image/* | /var/lib/docker/buildkit | \
      /var/lib/docker/buildkit/* | /var/lib/docker/plugins | /var/lib/docker/plugins/* | \
      /var/lib/docker/swarm | /var/lib/docker/swarm/*)
      fail "a container mounts $1, which gives the control a container socket gives ($mounted): refused (R49)"
      ;;
    # a `..` segment can climb out of any allowed folder (a name such as a..b is no segment)
    */../* | */..)
      fail "a container mounts $1, which gives the control a container socket gives ($mounted): refused (R49)"
      ;;
  esac
}

# a local volume whose driver_opts bind a host path (type none, o bind, device) shows only
# .../volumes/<name>/_data in the mounts, so the device is read from the volume itself
check_volume() {
  vinfo=$(docker volume inspect -f '{{.Driver}}|{{with .Options}}{{index . "device"}}{{end}}' "$1" 2>/dev/null) ||
    fail "the volume $1 cannot be inspected, so 'no container socket is mounted' cannot be proved (R49)"
  [ "${vinfo%%|*}" = "local" ] ||
    fail "the volume $1 uses the driver '${vinfo%%|*}', not 'local': its host path cannot be proved (R49)"
  device=${vinfo#*|}
  case $device in
    "") ;;
    /*) refuse_source "$device" ;;
    *) fail "the volume $1 has the relative device '$device' (a bind of a path the engine resolves from its own folder, so it cannot be proved safe): refused (R49)" ;;
  esac
}

for c in $containers; do
  # one mount per line, "source|destination": a tmpfs mount has an EMPTY source, so a
  # whitespace-separated list would shift every following pair
  mounted=$(docker inspect -f '{{.Name}}{{range .Mounts}}{{"\n"}}{{.Source}}|{{.Destination}}{{end}}' "$c" 2>/dev/null) ||
    fail "the mounts of container $c cannot be read, so 'no container socket is mounted' cannot be proved (R49)"
  case $mounted in
    *docker.sock* | *podman.sock* | *containerd.sock* | *crio.sock* | *"|/run/user/"* | *"|/var/run/user/"* | */run/user/*)
      fail "a container mounts a container socket or a path under /run/user ($mounted): refused (R49)"
      ;;
  esac
  # a bind source that is the socket's directory (or a parent of it) gives the same control
  first=1
  while IFS='|' read -r rawsrc _dest; do
    if [ "$first" = 1 ]; then
      first=0
      continue
    fi
    [ -n "$rawsrc" ] || continue # tmpfs or other source-less mount
    refuse_source "$rawsrc"
  done <<EOF_MOUNTS
$mounted
EOF_MOUNTS
  volumes=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\n"}}{{end}}{{end}}' "$c" 2>/dev/null) ||
    fail "the volumes of container $c cannot be read (R49)"
  for v in $volumes; do
    check_volume "$v"
  done
done

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
