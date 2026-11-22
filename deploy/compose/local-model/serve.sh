#!/bin/sh
# ND266: the `ollama` service's entrypoint. It serves only a model that verify.sh accepts; otherwise
# it exits non-zero and serves nothing (fail closed).
set -eu
model="${NURAMESH_LOCAL_MODEL:-qwen3:8b}"
# hashes the weights once (VERIFY_BLOBS=all, about 10 to 20 s for 5.2 GB); the healthcheck then re-checks sizes and small blobs
if ! VERIFY_BLOBS=all sh "$(dirname "$0")/verify.sh" "$model"; then
  echo "serve.sh: refusing to serve an unverified or missing model; run the ollama-pull step" >&2
  exit 3
fi
exec ollama serve
