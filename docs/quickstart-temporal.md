# Quickstart: a durable run on Temporal (optional extra)

**The Temporal dev server is for evaluation only; production needs a real Temporal cluster.**

Which engine to use (the light engine for self-hosting and evaluation, Temporal for production and hosted): see "Choosing the engine" in `apps/docs/src/content/docs/concepts/durable-runs.md`.
Durable runs are optional. The library, the CLI and the in-process engine need no Temporal and
import nothing from it.

## 1. Install the extra and start the dev server

```bash
uv sync --extra temporal          # or, from the wheel: uv pip install "$WHEEL[temporal]" (WHEEL as in the quickstart)
docker compose -f infra/compose/temporal-dev.yml up -d     # see docs/compose.md
```

## 2. Run a worker (your process; it holds the store, the workers and the content key)

```python
import asyncio
from nuramesh.temporal import build_worker, connect


def engine_factory(org_id, run_id):  # a fresh RunEngine per call: store, workers, content service
    return make_engine(org_id, run_id)  # your code: RunEngine(..., content=ContentService(...))


async def main():
    client = await connect("localhost:7233")
    await build_worker(client, engine_factory).run()


asyncio.run(main())
```

The factory gets the organization id **and the run id** each time an activity runs, so a team
that builds per-run state in `setup` can do it once per run. For a team folder you don't need to
write this: `nuramesh worker TEAM --fake` (see `docs/containers.md`) does it with the settings
`NURAMESH_TEMPORAL_ADDRESS`, `NURAMESH_TEAM_DIR` and `NURAMESH_KEY_DIR`, and exits with a plain
message and a code if the server, namespace or team is wrong.

**Transport security.** A Temporal address on this machine (`localhost`, `127.0.0.0/8`, `::1`) may
be plain. Any other address needs TLS and credentials, or the worker and `nuramesh serve` refuse
to start (exit 2, before any connection): set `NURAMESH_TEMPORAL_TLS=1` and either a client
certificate and key (`NURAMESH_TEMPORAL_TLS_CERT_FILE` with `NURAMESH_TEMPORAL_TLS_KEY_FILE`, mutual
TLS) or an API key (`NURAMESH_TEMPORAL_API_KEY`, or `NURAMESH_TEMPORAL_API_KEY_FILE`). Optional:
`NURAMESH_TEMPORAL_TLS_CA_FILE` (a private CA) and `NURAMESH_TEMPORAL_TLS_SERVER_NAME`. Key and
credential files must be regular files you own with mode 0600 (no symlink); a missing or malformed
setting is refused with a message that names the setting and never a value. The Compose dev stack
reaches Temporal as `temporal:7233`, which is not loopback, so its worker sets the explicit dev
allowance `NURAMESH_TEMPORAL_ALLOW_PLAINTEXT=1`: a plaintext connection to a non-loopback address
is then accepted only after the process has written the ledger entry
`network.temporal_plaintext_allowed` (never with an API key). Without the allowance, publish Temporal to
`127.0.0.1` and run the worker on the host, or put TLS in front of it. An address must be written
exactly `host:port` or `[v6]:port`; a scheme, user name, path or space is refused.

A durable engine must have a `content` service (task outputs and a person's decision are kept
there, encrypted). Run as many workers as you like over the same store; restarting one is
safe.

## 3. Start a run, answer a human gate, stop

```python
from nuramesh.temporal import connect, start_run, decide, stop

client = await connect("localhost:7233")
handle = await start_run(
    client, engine_factory("acme", "run-1"), "run-1", plan
)  # begins the run once
# days later, a person decides (recorded in the content store first, then the workflow wakes):
await decide(
    client,
    engine_factory("acme", "run-1"),
    "run-1",
    "t1",
    "deploy",
    decided_by="human:alice",
    findings={"approved": (True, "Checked; fine.")},
)
result = await handle.result()  # counts per status; `stopped` if a Stop applied
# await stop(client, engine_factory("acme", "run-1"), "run-1", actor="human:alice")
```

## What to know

- **Waiting for a person is not polling.** The workflow sleeps on a durable timer and wakes on the
  decision or Stop. After a decision the **non-human gates run again** (a verifier is paid once more
  per wait; option A until the owner chooses).
- **90 days:** a task's output lives at most 90 days from capture. The approver is warned 14 and 3
  days before (ledger entries `human_wait_warning`), and at 90 days the task is stopped and
  `cancelled` (`human_wait_expired`). The lead times are `EngineSettings.human_wait_warn_days`.
- A step cut short by a crash runs again in a new epoch; calls it already made may be made again
. A finished step is never repeated.
- Tests use Temporal's time-skipping test server, pinned to version 1.40.0 with a SHA-256 in
  `tests/temporal/conftest.py`; they skip with a reason if it can't be downloaded.

## Switching the engine

**Switching the run engine is not supported in this release.** Do not change `NURAMESH_RUN_ENGINE` (light <-> temporal) while runs are unfinished. A run belongs to the engine that started it; after a switch the new engine does not pick it up, so the run is stranded (it stays unfinished and does nothing). There is no startup check that warns you. Finish or stop all runs first, then switch. There is also no supported way to move runs between engines yet.
