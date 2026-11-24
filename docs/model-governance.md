# Model governance: who may use which model, where, and why a run was refused (L5)

The normative text is the internal specification (with `cases.json`); the decision is an internal decision record. This page is the short reader's guide.

## The idea in one paragraph

A real model call happens only when four things are true, and every one of them is a recorded, human decision: the model is in the organization's **approved catalog**, it may receive the run's **data class** (synthetic or customer; processing region), the provider has an **active connection** (a keyless endpoint an admin made, Entra ID, no stored key), and a **price** is in effect. A verifier must also come from another **model family** than the worker it checks. The fake model needs none of this.

## Where the state lives

- **In the ledger.** Catalog, assignments and connections are the fold of events on the organization stream `org:<org>` (`model.catalog_*`, `model.assignment_set`, `model.connection_*`). Nothing is edited or deleted; a change is a new entry with the actor, the time and a `reason_hash` (the reason text is in the content store, never in the ledger).
- **Configuration only seeds it.** The `models:` part of `nuramesh.yaml` seeds the catalog once, on first open. Later edits to it are ignored with a warning (`config_drift` in the API). The ledger wins per scope: agent ledger, agent config, role ledger, role config.
- **Only people count.** The fold honors catalog and assignment events only from a `human:` actor (and the seed only from `system:catalog-seed`). The person's authentication is the API's: a cookie session of the `owner` role with the CSRF token on every write. API tokens never write (403 `human_required`).

## The three layers that catch a bad model

1. **Config load** (`nuramesh validate`, exit 2, no ledger entry): the file by itself is consistent.
2. **Run start** (exit 3, `model.blocked` on the org stream, stage `start`): the effective state, the data class, the adapter, the connections, the price in effect and the team's gate pairings.
3. **Each call** (first step after E19's halt, role-pause and breaker checks, stage `call`): the same resolver again, so a pause or a repoint takes effect on the agent's next call.

The resolver (`core/model_governance.py`, pure) tries these in order and the first failure wins: org halted, catalog present, listed, status, adapter, residency, connection exists, connection active, endpoint accepted, price in effect, verifier family. Reason codes: `org_halted`, `no_catalog`, `model_not_in_catalog`, `model_paused`, `model_retired`, `provider_unsupported`, `residency_refused`, `no_connection`, `connection_disabled`, `endpoint_invalid`, `unknown_price`, `family_conflict`, `family_unknown`, `plain_provider_for_real_model`. Exact texts: spec section 9.

## Restricting is easy, widening is an admin's

Pausing, retiring, disabling and disconnecting only restrict, so the CLI has them (`nuramesh models pause|retire|disable|disconnect`, with `--human human:<id>`). Connecting, repointing, approving, adding or editing an entry, setting a price or a region, and assigning a model all widen what runs may use or where data may go, so they are the signed-in API and the app only. Changes that could send data somewhere new (a widening `processing` edit, a region added to the customer regions, a repoint) pause the affected approved entries in the same transaction, and each needs a fresh recorded approval.

## Surfaces

| Surface | Read | Change |
| --- | --- | --- |
| CLI | `nuramesh models catalog\|assignments\|connections\|check` | `pause`, `retire`, `disable`, `disconnect`, `test` |
| API | `GET /v1/models/catalog`, `/assignments`, `/connections`, `/resolution` (`docs/api.md`) | admin routes: entries, edit, approve, pause, retire, remove, prices, regions, assignments, connections (PUT, remove, test) |
| App | `/models/catalog`, `/assignments`, `/connections`, `/resolution` | the same actions, each with a required reason |

`nuramesh models check --data customer --team <team>` is the read-only "would this run start?" answer; it exits 3 with the block texts (4 when the organization is halted).

## Plain providers are refused

A run whose provider is a fake or plain one (not a connection-based provider) cannot answer a real model: any effective model that is not a local `fake/*` one is refused at run start and at the call with `plain_provider_for_real_model` (exit 3). The same rule applies to `nuramesh try --fake`, `nuramesh eval run` and the durable worker's judge, which all judge the effective (ledger-over-file) refs.

## The halt is real

The organization halt and role pause (E19) are wired through every model path. `call_model` checks them first (a halted org or a paused role writes `model.blocked` and raises before the resolver runs), the start stage and the connection test read the same state through `HaltWatch`, and a halt state that cannot be read counts as a halt (fail closed). `cli_models.org_halted`, `create_app(halted=None)` and `Governance(halted=None)` all default to the real watch; an internal test keeps a never-halted stub from coming back. Exit 4 for a halt and for a paused role: `docs/cli.md`.

## The Anthropic direct provider (R1.14b, the internal specification, T239)

`anthropic/<model>` entries go through the same catalog, connection, resolver and ledger as the Azure ones. Differences, all enforced in code and tests:

- **Connection:** `PUT /v1/models/connections/anthropic` with `{}` (or only a `reason`). `auth` is `key_file`, the endpoint is fixed (`https://api.anthropic.com`); a body that carries an `endpoint` is refused (422 `invalid_request`). Nothing about a key is stored: no key, path or name.
- **Key file:** one environment variable, `NURAMESH_ANTHROPIC_KEY_FILE`, names a file outside every repository, owned by the service user (not the agent account `nuramesh-agent`), mode `0400` or `0600` (no group or other bit). The owner creates it; the agent never sees it. Self-hosted or local only: with `NURAMESH_HOSTED=1` every use is refused with "hosted needs a secret store (a later item)". `nuramesh serve` refuses to start when the variable is set and the file is not usable; with the variable unset nothing is checked and no Anthropic call can happen.
- **Resolution:** the direct API is processed globally, so an entry cannot declare another scope and an organization with a chosen data location is refused (`residency_refused`); new step 8a gives `credential_unavailable` (after the endpoint, before the price) from the same `check_key_file` the probe and the adapter use. Detail texts never hold the path or the key. A model without a price entry (for example one whose price is tiered) is `unknown_price`.
- **Test connection:** one body-less `GET /v1/models?limit=1` with the key; no tokens, no spend. It ignores proxy environment variables (the key is never offered to a proxy) and never follows a redirect.
- The example `examples/dev-team/real-anthropic.yaml` pairs an Azure worker with a Claude verifier (different families). It is an EXAMPLE, verify before use: its price is copied from a read of the public pricing page on 2026-10-22 and must be re-read before the first real run. The code has no built-in or guessed price: without an explicit price entry for the model, the model is blocked (`unknown_price`).

### Owner steps for the key file

No command here puts a key on a command line or into the environment (spec r114b rule 4.7).

**Precondition:** your agents run as a separate operating-system user (called `nuramesh-agent` below). Nothing real runs until these steps are done and the first real run is the owner's.

1. Create a **dedicated server user** (for example `nuramesh-server`) that is **not** `nuramesh-agent`. Only this user starts a server or CLI that makes a real call. An agent-started server or test never has `NURAMESH_ANTHROPIC_KEY_FILE` set.
2. As that user, store the key in a password manager and write the file without echoing it: `umask 077; mkdir -p ~/.config/nuramesh; IFS= read -rs k; printf '%s' "$k" > ~/.config/nuramesh/anthropic.key; unset k` (the `read -rs` prompt does not echo and is not saved to history), or save it from the password manager into that path. The file is outside every repository, owned by that user, mode `0600` (or `0400`); the directory `~/.config/nuramesh` is `0700`.
3. Verify without printing the key: the file is `-rw-------` (0600) or `-r--------` (0400) and owned by the server user; `sudo -u nuramesh-agent test -r <path> && echo AGENT-CAN-READ || echo ok` prints `ok`; `sudo -u nuramesh-agent test -w <install dir> && echo AGENT-CAN-WRITE || echo ok` prints `ok` for the install directory and the venv. `nuramesh-agent` must never be able to read the file or write the code that reads it.
4. **The server user runs only code and a venv that `nuramesh-agent` cannot write:** its own clone at a merged main commit, or a built wheel in its own venv, owned by the server user. No `nuramesh-agent`-writable path is on `sys.path` (no `.pth` file, `sitecustomize` or `PYTHONPATH` entry), no `PYTHON*` or `UV_*` variable comes from an agent shell, and `nuramesh-agent` cannot `sudo` or `su` to the server user. The server never runs from the agent's checkout: the process holds the key in memory, so agent-writable code could write it out. The code cannot check this; it is the one control that keeps the key from agent-writable code.
5. The server user starts the server with `NURAMESH_ANTHROPIC_KEY_FILE=<absolute path>` (a path, never the key). The agent never runs a real call. The key never appears in logs, environment dumps (`env`, `printenv`, process listings, CI output) or chat.

### Stored keys (R1.14c)

An owner can instead store the Anthropic key in the server's secret store (`NURAMESH_SECRET_DIR` for self-hosted; a vault adapter exists in the enterprise layer): set, rotate and revoke through the owner-only key routes, never returned, never in the ledger (fingerprints only).

## Not built (stated, not silent)

A secret store for hosted use (the Anthropic key file is self-hosted only), roles other than the owner, and stripping connection details from the resolution report for a lower role.
