#!/bin/sh
# ND266: verify what Ollama will actually serve against the pins. Exit 0 only when, for the named
# model AND every other pinned model that has any entry in the volume:
#   - the name resolves through manifests-v2/ollama.com/library/<name>/<tag>, a symlink to exactly
#     blobs/sha256-<pinned registry digest>, and that file's sha256 is the pinned digest;
#   - every blob the manifest lists (config, weights, template, params, licence) is a plain file of
#     the listed size whose sha256 is the listed digest, and the pinned licence layer is listed;
#   - the legacy manifests/ copy, if present, is a plain file listing the same digests;
#   - nothing else is stored under manifests/ or manifests-v2/ (no other tag, host or layout).
# VERIFY_BLOBS=all hashes every blob (pull.sh and serve.sh set it); the default "small" hashes the
# blobs up to 1 MiB and only checks the size of the larger ones (the healthcheck runs every 15 s).
# Exit codes: 2 not a pinned model, 3 mismatch, 4 the model is not in the volume.
# Touches only the model volume (read only) and nothing outside it.
set -eu
. "$(dirname "$0")/pins.sh"
model="${1:-${NURAMESH_LOCAL_MODEL:-qwen3:8b}}"
if ! pin_of "$model" >/dev/null; then
  echo "verify.sh: '$model' is not a pinned model (qwen3:8b or qwen3:4b)" >&2
  exit 2
fi
dir="${OLLAMA_MODELS:-/models}"
mode="${VERIFY_BLOBS:-small}"

fail() {
  echo "verify.sh: $*" >&2
  exit 3
}
v2_of() { echo "$dir/manifests-v2/ollama.com/library/${1%%:*}/${1#*:}"; }
v1_of() { echo "$dir/manifests/registry.ollama.ai/library/${1%%:*}/${1#*:}"; }
present() { [ -e "$(v1_of "$1")" ] || [ -L "$(v1_of "$1")" ] || [ -e "$(v2_of "$1")" ] || [ -L "$(v2_of "$1")" ]; }
sum_of() { sha256sum "$1" | cut -d' ' -f1; }
# digests_of <manifest>: the sorted, unique sha256:<hex> strings in a manifest
digests_of() { grep -oE 'sha256:[0-9a-f]{64}' "$1" | sort -u || true; }

# 1. nothing but the pinned models' own entries
allowed=""
for m in $(pinned_models); do allowed="$allowed
$(v1_of "$m")
$(v2_of "$m")"; done
# find must read every directory: an unreadable one (for example execute-only) would hide entries,
# so a non-zero status or any error output is a refusal, never "nothing found"
roots=""
for r in "$dir/manifests" "$dir/manifests-v2"; do
  if [ -e "$r" ] || [ -L "$r" ]; then roots="$roots
$r"; fi
done
found=""
if [ -n "$roots" ]; then
  oldifs="$IFS"
  IFS="
"
  # shellcheck disable=SC2086
  found="$(find $roots ! -type d 2>&1)" || fail "cannot list the manifest directories: $found"
  IFS="$oldifs"
fi
case "$found" in
  *"find: "*) fail "cannot list the manifest directories: $found" ;;
esac
stray="$(printf '%s\n' "$found" | while IFS= read -r p; do
  [ -n "$p" ] || continue
  case "$allowed" in
    *"
$p
"* | *"
$p") ;;
    *) echo "$p" ;;
  esac
done)"
[ -z "$stray" ] || fail "unexpected manifest entry in the volume (only the pinned models may be stored): $stray"

check_model() {
  m="$1"
  want="$(pin_of "$m")"
  p2="$(v2_of "$m")"
  p1="$(v1_of "$m")"
  blobs="$dir/blobs"
  [ -L "$p2" ] || fail "$m: $p2 is not the symlink to the registry manifest"
  [ "$(readlink "$p2")" = "../../../../blobs/sha256-$want" ] ||
    fail "$m: $p2 points at $(readlink "$p2"), not at the pinned manifest sha256-$want"
  man="$blobs/sha256-$want"
  [ "$(readlink -f "$p2")" = "$(readlink -f "$blobs")/sha256-$want" ] || fail "$m: $p2 resolves elsewhere"
  [ -f "$man" ] && [ ! -L "$man" ] || fail "$m: the pinned manifest blob sha256-$want is missing"
  got="$(sum_of "$man")"
  [ "$got" = "$want" ] || fail "$m: manifest digest $got differs from the pinned $want"
  # every blob the manifest lists: config, weights, template, params, licence
  listed="$(tr '{' '\n' <"$man" | grep -oE 'sha256:[0-9a-f]{64}","size":[0-9]+' || true)"
  [ -n "$listed" ] || fail "$m: the manifest lists no blobs"
  for l in $listed; do
    d="${l%%\"*}"
    size="${l##*:}"
    f="$blobs/sha256-${d#sha256:}"
    [ -f "$f" ] && [ ! -L "$f" ] || fail "$m: blob $d is missing from the volume"
    [ "$(wc -c <"$f" | tr -d ' ')" = "$size" ] || fail "$m: blob $d is not $size bytes"
    if [ "$mode" = all ] || [ "$size" -le 1048576 ]; then
      [ "$(sum_of "$f")" = "${d#sha256:}" ] || fail "$m: blob $d does not hash to its digest"
    fi
  done
  case "$listed" in
    *"$licence"*) ;;
    *) fail "$m: the manifest does not reference the pinned licence layer $licence" ;;
  esac
  # the legacy copy Ollama also writes: if present it must agree, so no other layout serves anything else
  if [ -e "$p1" ] || [ -L "$p1" ]; then
    [ -f "$p1" ] && [ ! -L "$p1" ] || fail "$m: $p1 is not a plain file"
    [ "$(digests_of "$p1")" = "$( (digests_of "$man"; echo "sha256:$want") | sort -u)" ] ||
      fail "$m: $p1 lists other blobs than the pinned manifest"
  fi
}

if ! present "$model"; then
  echo "verify.sh: $model is not in the model volume; run the ollama-pull step first (docs/self-hosting.md)" >&2
  exit 4
fi
check_model "$model"
for m in $(pinned_models); do
  if [ "$m" != "$model" ] && present "$m"; then check_model "$m"; fi
done
