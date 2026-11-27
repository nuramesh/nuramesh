#!/bin/sh
# Build the NuraMesh images LOCALLY (api, worker, anchor; sandbox and sandbox-pull by name). No push, no registry, no cloud.
# Usage: sh scripts/images/build.sh [api|worker|anchor|sandbox|sandbox-pull ...]   (default: api worker anchor)
# Needs: uv, docker. Tags: nuramesh-<name>:local
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
out=build/images
rm -rf "$out"
mkdir -p "$out/wheel"
cp scripts/images/nuramesh-env "$out/nuramesh-env"
cp scripts/images/nuramesh-sandbox-pull "$out/nuramesh-sandbox-pull"

# The wheel, and per image the locked, hash-pinned third-party requirements (no dev group).
uv build --wheel --out-dir "$out/wheel" >/dev/null
export_reqs() {
  name=$1
  shift
  uv export --frozen --no-dev --no-emit-project --no-header --format requirements-txt "$@" \
    --output-file "$out/$name.requirements.txt" >/dev/null
}
export_reqs api --extra yaml --extra server --extra postgres --extra content --extra azure
export_reqs worker --extra yaml --extra temporal --extra postgres --extra langgraph \
  --extra content --extra azure
export_reqs anchor --extra yaml --extra anchors --extra postgres --extra azure
export_reqs sandbox

targets=${*:-api worker anchor}
for name in $targets; do
  case $name in api | worker | anchor | sandbox | sandbox-pull) ;; *) echo "unknown image: $name" >&2; exit 2 ;; esac
  docker build --pull=false -f "scripts/images/$name.Dockerfile" -t "nuramesh-$name:local" .
done
