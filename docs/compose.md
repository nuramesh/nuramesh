# Compose guide: the Temporal dev server (R1.20 PR 3)

**The Temporal dev server is for evaluation only; production needs a real Temporal cluster.**

Which engine to use (the light engine for self-hosting and evaluation, Temporal for production and hosted): see "Choosing the engine" in `apps/docs/src/content/docs/concepts/durable-runs.md`.

This guide starts a Temporal server with Postgres persistence on your own machine, so you can try
durable runs (coming soon, until a real crash-and-resume test passes; `nuramesh[temporal]`, `docs/quickstart-temporal.md`). It is not a deployment: nothing
here is created in any cloud, no container is started by NuraMesh, and no real customer data goes
through it. Hosting a real cluster is a separate decision and is not part of this repository.

## What the file starts

`infra/compose/temporal-dev.yml` (no secrets; the one password in it is a throwaway for a database
published to `127.0.0.1` only):

| Service | Image (pinned) | Port on your machine |
| --- | --- | --- |
| `postgres` | `postgres:16.11-alpine@sha256:4327b9fd…` | `127.0.0.1:5433` |
| `temporal` | `temporalio/auto-setup:1.29.7@sha256:f14912b6…` | `127.0.0.1:7233` (the address workers and clients use) |
| `temporal-ui` | `temporalio/ui:2.57.0@sha256:d944f959…` | `http://localhost:8233` |

## Commands

```bash
docker compose -f infra/compose/temporal-dev.yml up -d      # start (first start pulls images)
docker compose -f infra/compose/temporal-dev.yml ps         # all three should be running/healthy
docker compose -f infra/compose/temporal-dev.yml logs -f temporal
docker compose -f infra/compose/temporal-dev.yml down       # stop, keep the data
docker compose -f infra/compose/temporal-dev.yml down -v    # stop and delete the data
```

## What lives in Temporal, and what does not

Temporal keeps a history of each run's workflow in its database. NuraMesh puts **only ids,
statuses, counts and timestamps** there: the workflow input is the run id and the organization id;
activities pass task ids; a failure carries a code like `engine_error`, never an exception message.
Task titles, outputs, gate feedback, a person's written reason and anything else a model or a person
wrote stay in NuraMesh's own store (encrypted content store). A test searches a real
history for markers to prove it. See the internal specification (durable runs).

## Limits of the dev setup

- One node, no replication, no authentication, no TLS: anyone who can reach port 7233 can start or
  signal workflows. It is bound to `127.0.0.1` for that reason; do not change that.
- The data is a Docker volume on your machine. Back up nothing here that you need.
- Images are pinned by tag and digest (full digests in the file). The digests were read from Docker Hub, but this PR did not pull or start the containers, so the `temporal` healthcheck (`tctl cluster health`) is untested: if it fails on first start, fix the command in the file.
- The Temporal tests do not need any of this: they use a downloaded, checksum-pinned test server (see `tests/temporal/conftest.py`).
