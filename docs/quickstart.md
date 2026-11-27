# Quickstart: from a wheel to a verified run in 10 minutes

This path is **local only**: you install NuraMesh from a wheel file, create a team, run it on the
**fake model** (no keys, no cost, and no network connection during the run, which a test checks) and check that the ledger is intact.

> **The package is published.** The simplest install is `pip install "nuramesh[local]"` in a
> virtual environment (see the README). This guide installs a wheel built from a checkout of this
> repository (`uv build`) instead, so you can follow it from a clone.

## Before you start

- A Mac or Linux shell with **Python 3.12 or newer** and [`uv`](https://docs.astral.sh/uv/).
- A wheel file in a folder called `dist/` next to where you work (`dist/nuramesh-<version>-py3-none-any.whl`).
- The fake-model demo runs the project's tests on your machine, so this guide uses
  `--allow-unsandboxed` (see "What is not included"). With Podman or Docker installed the sandbox runs without the flag (macOS and Linux); without one, add `--allow-unsandboxed`: the commands then run on this machine without isolation, printed and recorded in the ledger (never a default). Use a throwaway folder.

Every command below is run by an internal test against a freshly built wheel, so the
guide cannot drift from the product.

## 1. Install (about 1 minute)

Create an isolated environment, install the wheel with the `local` extra (the sample team is a YAML
file; the extra also carries the API server and the content store that approvals need) and put the environment on your `PATH` for this shell:

```sh quickstart
uv venv --python 3.12 nuramesh-env
WHEEL=$(ls -t dist/nuramesh-*.whl | head -1)
uv pip install --python nuramesh-env/bin/python "$WHEEL[local]"
export PATH="$PWD/nuramesh-env/bin:$PATH"
nuramesh --help
```

Why the `PATH` line: the sample team's test gate runs `python -m unittest`, and it finds `python`
through `PATH`. With only the core install (no extra) NuraMesh needs just `pydantic`.

## 2. Create a team (about 1 minute)

```sh quickstart
nuramesh init my-team
ls my-team
```

`my-team/` holds `nuramesh.yaml` (the organization, roles, policies, gates and budgets),
`team.py` (the worker scripts) and `repo/` (a tiny project with a failing test for the team to fix).
`init` sets the organization id to the folder name (`my-team` here), so two teams never share an id:
an erased organization id is retired for good and cannot be reused.

## 3. Validate the configuration (under 1 minute)

```sh quickstart
nuramesh validate my-team/nuramesh.yaml
```

Expect `my-team/nuramesh.yaml: valid`. A mistake prints the file and line instead.

## 4. Run it on the fake model (about 2 minutes)

```sh quickstart
nuramesh try my-team --fake --approve-as human:you --allow-unsandboxed --run-id first-run
```

Three tasks run: fix the failing test, run the whole suite, deploy to staging. Expect
`Tasks: 3 done` and a last line `Ledger: run:first-run ... (verified)`.

- `--fake` uses scripted model answers. Budgets, prices, policies and gates are real.
- `--approve-as human:you` is a **scripted** approval so the run finishes on its own; the ledger
  records that it was scripted. Use `--interactive` to answer the deploy approval yourself.
- `--allow-unsandboxed` is LOCAL ONLY: the test gate runs on your machine without a container. The
  run prints a warning and records `sandbox.opt_out` in the ledger. Without it (and without
  Podman or Colima, see [sandbox.md](sandbox.md)) the test gate refuses and the task goes to a
  human.

Using the web app against a server with accounts? A new owner's first screen, `/start`, is a
four-step checklist (create the owner account, start the demo run, decide the first approval,
invite a teammate). It ticks a step only when the server shows it happened, and you can hide it
from the page.

## 5. Read the report (1 minute)

```sh quickstart
nuramesh report first-run --team my-team
```

Each task shows its gates, cheapest first: a rule, the tests, an independent reviewer who never
sees the developer's reasoning, and the human approval. The report is rebuilt from the ledger
alone; nothing is "done" without recorded gate results.

## 6. Verify the ledger (1 minute)

```sh quickstart
nuramesh ledger verify --team my-team
```

Expect `0 broken`. Every action, model call, cost, gate result and approval is an entry in a hash
chain; editing any entry breaks it. To also catch deleted newest entries, keep a head hash
elsewhere and pass `--expected-head run:first-run=<hash>` (the hash is on the run's last line).

## 7. Status, stop and report commands

`nuramesh stop` asks a **running** run to stop and records who asked; start your own long run in
one terminal and stop it from another. On our finished run it only says so and exits with code 3:

```sh quickstart
nuramesh stop first-run --team my-team --human human:you || echo "exit code $? (3 = already ended)"
```

`nuramesh report <id> --team my-team --output json` gives the same report as JSON.

## 8. Stop everything (halt, pause a role, breakers)

`nuramesh stop` stops one run. To stop a whole organization, or one role, use these (each needs `--human human:<id>`, your own claim of who you are; the command line can only restrict, never lift):

```sh quickstart
nuramesh status first-run --team my-team
nuramesh org status --team my-team
nuramesh breakers list --team my-team
```

- `nuramesh org halt --team my-team --human human:you` halts the organization: every open run is asked to stop, no task starts and no model call is made until a person resumes. A call already running finishes (stop is cooperative).
- `nuramesh roles pause <role> --team my-team --human human:you` stops that role's running tasks and refuses its model calls.
- `nuramesh breakers trip <name> --team my-team --human human:you` trips one rule by hand (a drill).
- **Resuming needs the app or the API**, signed in as a person: `POST /v1/org/resume`, `POST /v1/org/roles/{role}/resume`, `POST /v1/org/breakers/{name}/reset`. There is no command-line way to lift a halt, on purpose.

**If the server halted the organization by itself** (`cause: evaluation_unavailable`, because the breaker evaluation failed several ticks in a row):

1. Read why: the app's status bar and `GET /v1/org/status` name the phase, the error class and the failing rule; the server log has the traceback.
2. Fix the cause: bring the store back, or repair the failing rule or remove it (a dry-run rule is evaluated too), then restart the server (the config is read at start). If one rule is the cause, a person can instead disable it in the app: it is skipped, the next tick succeeds, and no restart is needed. Repair or remove it too, because it stays off until a person enables it.
3. A person resumes the organization in the app or with `POST /v1/org/resume`. It works at once, fixed or not; if the fault is still there the next failed tick halts again, so fix first.

## 9. Get a first approval into the app

So far the approval was scripted (`--approve-as`). Here a run waits for a person, and you see the request on the app's **Approvals** page. You need the `server` and `content` extras (install the wheel as `"$WHEEL[yaml,server,content]"`), the web app and step 4 done: `try` also records the team's budgets, and a run without a budget is refused.

Terminal 1, the API with sign-in on and a runner for runs started through it (fake model only; `NURAMESH_REFRESH_GRACE_SECONDS=0` turns off the refresh grace window, which otherwise needs a server key):

```sh first-approval
NURAMESH_REFRESH_GRACE_SECONDS=0 nuramesh serve my-team --auth --with-runner
```

Terminal 2, the first owner's setup token (shown once, on a terminal only):

```sh first-approval
nuramesh auth new-setup-token --team my-team
```

Open the app, paste the token on the setup page, choose your email and password and sign in. Then open **Runs**, press **Start a run** and **Start**. The dialog is filled with this plan (one task for the DevOps role, whose policy says `merge: ask`):

```json first-approval
{
  "data_class": "synthetic",
  "tasks": [
    {
      "ref": "deploy",
      "role": "devops",
      "title": "Deploy the fix to staging",
      "definition_of_done": "The deploy targets staging and a person approved it"
    }
  ]
}
```

The same from a terminal on the host: save the plan as `first-approval.json` and name yourself (your id, `human:u_...`, is `user.id` in the answer of the sign-in, `POST /v1/auth/login`; only an active owner may):

```sh first-approval
nuramesh start my-team --plan first-approval.json --human "$NURAMESH_HUMAN"
```

Within a few seconds the run reads "Waiting for you" and **Approvals** shows one pending request: the DevOps engineer asks to `merge` to `staging` (`GET /v1/approvals` lists it with `"status": "pending"`).

- **Deciding your own run.** You are the organization's only person who can decide, so approving this request is a solo approval: the app asks you to acknowledge it, and the API needs `acknowledge_solo: true` and the `action_digest` it showed you (without them, 403 `acknowledge_required`; denying needs neither). Once a second person holds the approver, admin or owner role, you cannot decide a request of a run you started: Approve and Deny both answer 403 `self_approval`, "you cannot approve your own work", and that other person decides it (Settings, Invites; [api.md](api.md)).
- There is no approve command: a decision is made by a signed-in person, in the app or with `POST /v1/approvals/{id}/approve` or `/reject`.
- A refused start is visible, not silent: the dialog (or `nuramesh start`) names the code, for example `run_budget_missing` (step 4 was skipped) or `engine_unavailable` (no `--with-runner`).

## Give an agent its own credential

An agent that calls the API from outside the process can have a credential of its own (`nma_...`), so the ledger and a revoke apply to that agent alone. Only an owner signed in to the app (Agents, Access, Issue) or through `POST /v1/agents/<id>/credentials` can make one; the CLI cannot issue or renew it. The secret is shown once. Send it as `Authorization: Bearer nma_...`. It may read runs and, with the `run` scope, stop a run the agent takes part in; nothing else. To look or end one from a terminal:

```text
nuramesh agents credentials list --team dev-team --agent developer-1
nuramesh agents credentials revoke agc_<id> --team dev-team
nuramesh agents credentials revoke-all --team dev-team --agent developer-1
```

An agent you remove from the configuration (or change: role, model, checks, reporting line) loses its credentials the next time the server starts.

## Safe retries with an Idempotency-Key (API)

When you call the API yourself (a script, a proxy that retries), add `Idempotency-Key: <16 to 128 characters (letters, digits, `.`, `_`, `-`), one new value per operation>` to a POST or PUT that changes something, such as a run stop, an approval or a model connection. If the answer is lost, send the **same request with the same key**: you get the first answer back (header `Idempotent-Replayed: true`) and nothing runs twice. A different request with the same key is refused (422), and a key is forgotten after 24 hours. The key needs the server's `content` extra and key ring; the app sends one on its state-changing calls (run start and stop, approvals, gate decisions, halt, resume and breakers), the command line sends none. Details: `docs/api.md`.

## Where files live

| What | Where |
| --- | --- |
| The team (config, scripts, project) | the folder you created, `my-team/` |
| Run database and ledger | `my-team/.nuramesh/runs.db` (SQLite) |
| Signing keys, sandbox copies, temporary files | `~/.nuramesh/` |
| The virtual environment | `nuramesh-env/` (delete it to uninstall) |

None of these belong in Git; `nuramesh init` never writes secrets, and NuraMesh settings come from
`NURAMESH_`-prefixed environment variables.

## What is not included

- **No hosted service.** Nothing here talks to a server of ours; the API (`nuramesh serve`) is
  localhost only and starts with authentication on (the first run prints a one-time setup link),
  but it is not a hosted service, so keep real data out of it.
  With `serve --auth` on this machine (loopback, no proxy), the refresh grace window is off:
  two browser tabs refreshing at the same moment revoke the session, so sign in again.
- **No real models.** A run without `--fake` is refused unless you set up a provider. That needs
  credentials and spends money.
- **No container sandbox in this guide.** Install Podman or Colima and drop `--allow-unsandboxed`
  to run the test gate isolated.

## Next

- [first-run.md](first-run.md): start `nuramesh serve` (sign-in is on) and create the owner from the one-time setup link.
- [quickstart-templates.md](quickstart-templates.md): all nine templates, `ledger export` with the offline verifier, and `nuramesh models`.
