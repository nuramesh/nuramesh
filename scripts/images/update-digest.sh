#!/bin/sh
# Refresh the pinned base-image digest in the three Dockerfiles. Read-only against the registry
# (docker pull of a public base; no login, no push). Review the diff, build, then commit.
# Usage: sh scripts/images/update-digest.sh [tag]   (default python:3.12-slim-bookworm)
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
tag=${1:-python:3.12-slim-bookworm}
docker pull -q "$tag" >/dev/null
digest=$(docker image inspect "$tag" --format '{{index .RepoDigests 0}}' | sed 's/.*@//')
case $digest in sha256:*) ;; *) echo "no digest found for $tag" >&2; exit 1 ;; esac
for f in api worker anchor; do
  sed -i.bak -E "s#^FROM ${tag}@sha256:[0-9a-f]{64}#FROM ${tag}@${digest}#" \
    "$root/scripts/images/$f.Dockerfile"
  rm -f "$root/scripts/images/$f.Dockerfile.bak"
done
# The Debian snapshot the base image was built from: the anchor image's apt reads the same one.
stamp=$(docker run --rm --entrypoint sh "$tag" -c 'cat /etc/apt/sources.list.d/*.sources' \
  | sed -n 's#.*snapshot.debian.org/archive/debian/\([0-9]\{8\}T[0-9]\{6\}Z\).*#\1#p' | head -1)
case $stamp in [0-9]*T*Z) ;; *) echo "no snapshot timestamp found in $tag" >&2; exit 1 ;; esac
echo "$stamp" > "$root/scripts/images/base-snapshot.txt"
sed -i.bak -E "s#(snapshot.debian.org/archive/debian(-security)?/)[0-9]{8}T[0-9]{6}Z#\1${stamp}#" \
  "$root/scripts/images/anchor.Dockerfile"
rm -f "$root/scripts/images/anchor.Dockerfile.bak"
echo "pinned $tag@$digest (Debian snapshot $stamp)"
