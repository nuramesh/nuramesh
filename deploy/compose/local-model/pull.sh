#!/bin/sh
# ND266: pull one pinned local model into the `ollama-models` volume, verify it, and FAIL CLOSED:
# on any digest or licence mismatch the unverified model is removed from the volume and the script
# exits non-zero. Run by hand, never in CI:
#   docker compose -f deploy/compose/compose.yml --profile local-model-pull run --rm ollama-pull
# Needs a route out (service `ollama-pull` is on `edge`); the `ollama` service itself has none.
# The pins are in pins.sh; the checks are in verify.sh.
set -eu
here="$(dirname "$0")"
. "$here/pins.sh"

model="${1:-${NURAMESH_LOCAL_MODEL:-qwen3:8b}}"
if ! pin_of "$model" >/dev/null; then
  echo "pull.sh: '$model' is not a pinned model (qwen3:8b or qwen3:4b)" >&2
  exit 2
fi

dir="${OLLAMA_MODELS:-/models}"
name="${model%%:*}"
tag="${model#*:}"
export OLLAMA_HOST=127.0.0.1:11434
ollama serve >"${TMPDIR:-/tmp}/ollama-pull.log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
  ollama list >/dev/null 2>&1 && break
  sleep 1
done
ollama pull "$model"

code=0
VERIFY_BLOBS=all sh "$here/verify.sh" "$model" || code=$?
if [ "$code" -eq 0 ]; then
  echo "pulled $model (manifest and every blob verified against the pin, licence layer included)"
  exit 0
fi
echo "pull.sh: verification failed (see the verify.sh message above; the cause may be another entry in the volume, not this model): removing $model from the volume so nothing unverified is served" >&2
ollama rm "$model" >/dev/null 2>&1 || true # while the serve process is still up
# whatever ollama did, both manifest entries are gone: the service cannot serve the model
rm -f "$dir/manifests/registry.ollama.ai/library/$name/$tag" "$dir/manifests-v2/ollama.com/library/$name/$tag"
exit "$code"
