# NuraMesh

An open-core platform for building and running AI agent teams you can trust. Define an organization of
agents as a graph; NuraMesh adds verification gates, evals per role, budgets with hard stops, a shared canon
from Git, an append-only ledger of every action, and default-deny autonomy policies. It builds on existing
frameworks (LangGraph first) and does not replace them.

By AI Collective Labs Inc. Licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`). This repository is
the open core only. It sends no telemetry and never checks a licence over the network.

## Self-host in a few steps

You need Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/). For the Docker stack you also need
Docker with Compose v2, Node 24 with `pnpm`, and `openssl`.

1. Install from the lockfile: `uv sync --locked`
2. Create a team and run it on the built-in fake model (no keys, no cost): `uv run nuramesh init my-team`,
   then follow `docs/quickstart.md`.
3. Start the API server: `uv run nuramesh serve`. Sign-in is on by default; the first start prints a
   one-time setup link to create the owner (`docs/first-run.md`).
4. For a small team's own server, use Docker Compose with PostgreSQL and the web app:
   `docs/self-hosting.md` (secrets script, local image build, `docker compose up`).

More: `docs/cli.md`, `docs/api.md`, `docs/configuration-env.md`, `docs/model-governance.md`,
`docs/telemetry.md` (what leaves your machine: nothing unless you set an endpoint).

## Security

Report vulnerabilities as described in `SECURITY.md`. Contributing: `CONTRIBUTING.md`.
