# Troubleshooting

Symptom, cause, fix. Every API error carries a stable `code`; match on the code, never on the
`detail` text. The full list of codes is in the error codes reference. Known limits (things that are
not bugs) are in the known-issues page. If nothing here fits, see the support page.

Every error code on this page is in the catalogue, and every `nuramesh` command in its code blocks
is checked against the real command line.

## A run is refused

**Symptom.** `nuramesh try my-team` ends at once with exit 3, or says it was not started.

**Cause.** A bare `run` (no `--fake`) needs an approved model catalog and a usable connection to a
model provider. Without them the run is refused before anything starts. Exit 2 is a different
problem: bad arguments, configuration or plan, and nothing was run. The exit codes are in
[the command reference](/reference/cli/).

**Fix.** Check the configuration first, then decide whether you want the fake model.

```sh
nuramesh validate my-team/nuramesh.yaml
nuramesh try my-team --fake --approve-as human:you --allow-unsandboxed
```

`--fake` can only run agents whose model is a `fake/*` model; an agent configured with a real
model is refused under `--fake`, by design. Setting up a real provider needs credentials and spends
money; see demo/first-real-run.md.

## The model is not set up

**Symptom.** The message says `--fake can only run agents whose model is fake/*`, or the team
"has no model provider", or `eval run` says an agent "has no model".

**Cause.** The agent's model in `nuramesh.yaml` is a real model (or none), and no connection for it
exists. Real models are not available in the quickstart.

**Fix.** For the demo, give the agents `fake/*` models (the `nuramesh init` templates do) and run
with `--fake`. For a real run, an approved catalog entry and a connection for the provider must
exist; the models are managed with `nuramesh models` and the `/v1/models` routes
(see the quickstart templates and model governance pages in the repository docs).

## 401 and 403 from the API

**Symptom.** A call answers 401 or 403.

| Code | Cause | Fix |
| --- | --- | --- |
| `session_invalid` | Not signed in, or the session expired. | Sign in again. |
| `authentication_required` | The route needs a signed-in person; the server was started with `--auth off`, so every write is refused. | Restart the server without `--auth off` and sign in. |
| `forbidden` | The role, token scope or credential scope does not allow the action. | Use an account or token with the needed role or scope. |
| `csrf_invalid` | A state-changing call with a session cookie lacks the `X-CSRF-Token` header. | Send the `csrf_token` from sign-in. |
| `invalid_host` | The `Host` header is not one the server answers for. | Call it by loopback, or add an allowed host (see [the API reference](/reference/api/)). |
| `rate_limited` | Too many calls or failed attempts. | Wait for the `Retry-After` time. |

The server listens on loopback only, unless `--auth`, `--trusted-proxy` (with
`NURAMESH_TRUSTED_PROXIES`, the proxy addresses) and `--allowed-host` are all given. The exact rules
are in [the API reference](/reference/api/).

## 409 and 503 on a retried call (idempotency)

**Symptom.** A retry of a POST or PUT that carries an `Idempotency-Key` answers 409 or 503.

| Code | HTTP | Cause | Fix |
| --- | --- | --- | --- |
| `idempotency_in_progress` | 409 | The first attempt is still running, or crashed and holds its 120-second lease. | Wait, then retry with the same key. |
| `idempotency_key_reused` | 422 | The key was used for a different request. | Use a new key for a different request. |
| `idempotency_key_invalid` | 400 | The key is not exactly one value of 16 to 128 letters, digits, `.`, `_` or `-`. | Send one well-formed key. |
| `idempotency_response_unavailable` | 409 | The request was carried out but its stored response cannot be shown. | Look at the current state before repeating it. |
| `idempotency_unavailable` | 503 | The content key ring or the store behind keys cannot be used. The request was not run. | Retry after `Retry-After` (5 seconds), or send it without a key; operators see "Key ring" below. |

Keys are stored hashed for 24 hours and a running attempt's lease lapses after 120 seconds; a key reused with a different request is recorded in the security log as `idempotency.key_reused`. The operator view is the idempotency runbook in the repository docs.

## The server refuses to listen on a network address

**Symptom.** `nuramesh serve --host 0.0.0.0` (or any address that is not loopback) exits 2 at once.

Usually one of three texts appears, by what is missing.

**Message.** `a network address needs TLS in front` (no --trusted-proxy; this is what the default command prints)

**Message.** `a network address needs --allowed-host NAME` (no --allowed-host: the host name browsers use)

**Message.** `refusing to listen on a network address without authentication` (only with --auth off)

**Cause.** A server open to a network needs sign-in on (it is on by default; `--auth off` turns it
off), a TLS proxy in front (`--trusted-proxy`) and the host name people use (`--allowed-host`).
Otherwise anyone who can reach it could act on your organization, or credentials would cross the
network in clear text, so the command refuses before it opens a port. The checks run in that
order: `--auth off` is refused first, then a missing `--trusted-proxy`, then a missing `--allowed-host`.

**Fix.** Use `127.0.0.1`, or keep sign-in on and name the proxy and the host people use:
`--trusted-proxy` (with `NURAMESH_TRUSTED_PROXIES` set to the proxy addresses) and
`--allowed-host`. Never use `--auth off` on a network address. The Docker Compose stack already
does this: only its web service publishes a port, on `127.0.0.1`.

## A budget stopped the run

**Symptom.** A run ends with a `BREACH ALERT: hard stop on budget ...` line, and the task is
`needs_human`.

**Cause.** A budget is a hard stop: the next model call would pass the limit, so NuraMesh recorded
`budget.exceeded` and made no call. The alert names the budget, its window and when it resets (a
lifetime limit is lifted only by a person).

**Fix.** Read the report, then let a person decide. Over the API a person raises or unblocks the
budget with `POST /v1/budgets/{budget_id}/unblock`; a send with a bad limit answers `invalid_amount`
(send a plain decimal such as `20.50`), and a budget that cannot be unblocked answers
`unblock_refused` with the reason.

```sh
nuramesh report <run-id> --team my-team
```

## A price is unknown

**Symptom.** A model call is blocked, the run stops and the task goes to a person, although the
budget has room.

**Message.** `no price for ... the call is blocked`

**Cause.** NuraMesh never guesses a price and never counts an unknown one as zero: a model with no
price in effect at the time of the call is blocked before it is called, and the block is recorded.

**Fix.** Add a price for that model (in the app or the API; `nuramesh models price` works only for an
openai-compatible model), or a price override with provisional prices so budgets still stop correctly
([price overrides](/guides/price-overrides/)). A local model is priced with an explicit free price,
never by default.

## A gate failed

**Symptom.** A task is not `done`; the report shows a failed gate, or the task is `needs_human`.

**Cause.** Nothing is done without recorded gate results. A rule, test or independent review
failed, or a gate that needs a person is waiting. A test gate also fails closed when it cannot run
(see "The sandbox").

**Fix.** The report names the gate and why. For a human gate, the person the gate allows decides;
`decider_not_allowed` means this person is not allowed to, `already_decided` means the round already
has a decision, and `stale_round` means the decision was for an earlier round (fetch the task and
decide the current one).

## The sandbox

**Symptom.** The test gate refuses, or the task goes to a human, with no test result.

**Cause.** Test commands run in a container sandbox. With neither Podman nor Docker working, the gate
refuses; it never falls back to your machine. No sandbox is bundled.

**Fix.** Install Podman, or Colima with Docker, and check that its `info` works
([the sandbox guide](/guides/sandbox/)). For a local experiment only, `--allow-unsandboxed` runs the gate on your
machine in a copy of the workspace and records `sandbox.opt_out` in the ledger. Use a throwaway folder.

## Postgres

**Symptom.** `store_busy` (503), or the stack cannot connect.

**Cause and fix.** `store_busy`: the database is busy; retry. For the Compose stack, `up` is safe to
repeat; `down -v` deletes the volumes, including the ledger and the key ring. PostgreSQL 16 is
pinned, and a major upgrade needs a dump and a restore ([the self-hosting guide](/guides/self-hosting/),
the store maintenance notes in the repository docs). With `NURAMESH_DATABASE_URL` set, the stores open
that database.

```sh
nuramesh ledger verify --team my-team
```

## Key ring

**Symptom.** Keyed requests answer 503 `idempotency_unavailable`, or reasons cannot be kept
(`content_store_unavailable`, `content_unavailable`).

**Cause.** The content store needs the `content` extra and a readable key ring (the key directory
set for the server). Without it, requests that need to keep text, or that carry an idempotency key,
are not run. Requests without a key, and halt or role pause, are not affected.

**Fix.** Install the extra and configure the key directory, or fix its permissions; see
the idempotency runbook in the repository docs and [the API reference](/reference/api/). Never delete the key
ring to "reset" it: stored content then cannot be read (see the key tombstone runbook in the repository docs).

## The ledger reports a broken chain

**Symptom.** `ledger verify` ends with `BROKEN at seq N` and exit 1.

**Cause.** An entry was changed, removed from inside the chain, or the database is damaged. Without
`--expected-head`, deletion of the newest entries is not detectable.

**Fix.** Do not edit anything. Keep the output and the database, and report it (see the support page).
