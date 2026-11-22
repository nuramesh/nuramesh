#!/bin/sh
# Starts a REAL `nuramesh serve --auth` on a fresh, empty team and store for the first-run end-to-end
# tests (`playwright.first-run.config.ts`). Run from apps/app. The state lives under test-results.
set -e
mkdir -p "$1"; DIR="$(cd "$1" && pwd)"
PORT="$2"
rm -rf "$DIR"
mkdir -p "$DIR"
cd ../..
uv run nuramesh init "$DIR/team" --template code-review --yes >/dev/null
export NURAMESH_REFRESH_GRACE_SECONDS=0  # no successor key to set up for a throwaway server
exec uv run nuramesh serve "$DIR/team" --auth --port "$PORT" --store "$DIR/store.db"
