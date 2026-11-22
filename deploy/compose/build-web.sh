#!/bin/sh
# Build the nuramesh-web image locally (R1.29): the single-page app, then the nginx image. No push.
#   sh deploy/compose/build-web.sh        needs: node 24, pnpm, docker
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
(cd apps/app && pnpm install --frozen-lockfile && VITE_API_SOURCE=server pnpm build)
rm -rf build/web
mkdir -p build/web
cp -R apps/app/dist build/web/dist
find build/web/dist -name '*.map' -delete
docker build --pull=false -f scripts/images/web.Dockerfile \
  -t "nuramesh-web:${NURAMESH_COMPOSE_IMAGE_TAG:-local}" .
