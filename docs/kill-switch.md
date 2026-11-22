# Stop an organization, a role or a rule from the command line

`nuramesh stop` ends one run. This page covers the controls above a run: `nuramesh org` (halt the
whole organization), `nuramesh roles` (pause one role) and `nuramesh breakers` (list and trip the
circuit-breaker rules in your team's configuration). The API routes are in the
[API reference](/reference/api/).

Every example below was run on a scratch team made with `nuramesh init my-team --template dev-team`
(fake model, SQLite store in `my-team/.nuramesh/`), with the `breakers:` section shown at the end of
this page added to its `nuramesh.yaml`. `<team>` is the team folder. The store is chosen as on
`docs/cli.md` ("Which store a command uses"): PostgreSQL when `NURAMESH_DATABASE_URL` is set, else
`--store`, else the team's `.nuramesh/runs.db`.

## What the command line can and cannot do

| Action | Command line | App / API |
|---|---|---|
| see the halt and paused roles | `org status` | `GET /v1/org/status` |
| see every breaker with its numbers | `breakers list` | `GET /v1/org/breakers` |
| halt the organization | `org halt` | yes |
| pause a role | `roles pause` | yes |
| trip a breaker by hand | `breakers trip` | no route (a command-line drill only) |
| resume the organization, resume a role, reset, disable or enable a breaker | **not possible** | signed-in person only |

The command line can only **restrict**. Lifting a halt, a pause or a tripped breaker needs a person
signed in to the app or the API (`POST /v1/org/resume`, `POST /v1/org/roles/{role}/resume`,
`POST /v1/org/breakers/{name}/reset`); there is no command-line way, on purpose. Stop is
cooperative: a model call or task that is already running finishes and is recorded; what stops is
new work and new model calls.

## `--human`: who is doing it

`org halt`, `roles pause` and `breakers trip` need `--human human:<id>`. It is your own claim (the
command line cannot prove who types it, as with `stop`) and is written to the ledger as the actor.
It must be an opaque id, never an email or a name, and it is checked before the store is opened:

```text
$ nuramesh org halt --team halt-team --human you
--human needs an opaque id like human:u_7k2x9q (no email or name)
[exit 2]
```

## `org status`

Read-only. Says whether the organization is halted and which roles are paused.

```text
$ nuramesh org status --team my-team
organization my-team: running
[exit 0]
```

`--output json` prints the same state as the API does (`halted`, `by`, `at`, `cause`, `rule`, `ledger_seq`,
one entry per role with `paused` and the same fields, and `can_act`, which is always `false` on the
command line). After a pause and a halt the text form reads:

```text
$ nuramesh org status --team my-team
organization my-team: HALTED by human:you at 2026-10-30T02:47:57.182186+00:00 (cause breaker, rule spend-spike; ledger entry 3)
role developer: PAUSED by human:you at 2026-10-30T02:47:55.721860+00:00 (cause human; ledger entry 1)
[exit 0]
```

`cause` is `human` for a person's action and `breaker` for a rule (with `rule` naming it).

## `org halt`

```text
$ nuramesh org halt --team halt-team --human human:you
organization my-team halted by human:you (ledger entry 1); stop requested for 0 run(s)
[exit 0]
```

After a halt no run starts, no task starts and no model call is made, and every open run is asked
to stop (the count is in the message). Halting again changes nothing:

```text
$ nuramesh org halt --team halt-team --human human:you
organization my-team is already halted by human:you (ledger entry 1); no change
[exit 0]
```

A run started while the halt is in force is refused with exit 4 (see the exit-code table in
`docs/cli.md`):

```text
$ nuramesh try halt-team --fake --approve-as human:you
the run was not started (model governance): the organization is halted: no run starts and no model is called until a human lifts it
[exit 4]
```

Halt, `roles pause` and `stop` always succeed (exit 0 when they did their work): they write only to
the ledger and need no content key, so even an organization whose key is destroyed can be stopped.

## `roles pause`

Pauses one role: its running tasks are asked to stop and its model calls are refused; the other
roles go on.

```text
$ nuramesh roles pause --team my-team developer --human human:you
role developer paused by human:you (ledger entry 1); stop requested for 0 running task(s)
[exit 0]
$ nuramesh roles pause --team my-team developer --human human:you
role developer is already paused by human:you (ledger entry 1); no change
[exit 0]
$ nuramesh roles pause --team my-team nosuch --human human:you
no role 'nosuch' in this team's config
[exit 2]
```

## `breakers list`

Read-only. Prints the organization line, then one row for each rule in the `breakers:` section of
the configuration with what it observes right now. Nothing is written.

```text
$ nuramesh breakers list --team my-team
organization my-team: running
NAME         SIGNAL         WINDOW  LIMIT  OBSERVED  STATE  MODE
denied-loop  policy_denied  600s    20     0         ok     pause developer
spend-spike  spend          3600s   25     0         ok     halt
[exit 0]
```

- `STATE` is `ok`, `TRIPPED` or `DISABLED`, with `(removed from config)` added for a rule that was
  tripped or disabled earlier and is no longer in the file.
- `MODE` is `halt` (the rule halts the organization), `pause <role>` or `dry-run` (it is counted but
  never acts).
- With no rules it prints `no breakers are configured`. `--output json` prints the same rows as the API.

A rule trips when the observed value is **greater than** its limit inside the window. The server
checks the rules every `tick_seconds` (default 10) while `nuramesh serve` runs; the command line
does not evaluate rules, it only shows the numbers now and trips a rule by hand.

## `breakers trip`

Trips one rule by hand, as a drill or to latch it. It does what an automatic trip does: halts the
organization or pauses the rule's role (nothing, if the rule is a dry run).

```text
$ nuramesh breakers trip --team my-team spend-spike --human human:you
breaker spend-spike tripped by human:you (ledger entry 2); organization halted
[exit 0]
$ nuramesh breakers trip --team trip-team denied-loop --human human:you
breaker denied-loop tripped by human:you (ledger entry 1); role developer paused
[exit 0]
$ nuramesh breakers trip --team my-team spend-spike --human human:you
breaker spend-spike is already tripped; no change
[exit 0]
$ nuramesh breakers trip --team my-team nosuch --human human:you
no breaker 'nosuch' in this team's config
[exit 2]
```

A rule a person has disabled in the app is refused with exit 3 (`breaker <name> is disabled; a
person must enable it in the app first`). After a trip the rule shows as `TRIPPED` in
`breakers list` until a person resets it in the app or with the API.

## The `breakers:` section used above

```yaml
breakers:
  rules:
    - id: denied-loop
      signal: policy_denied
      window_seconds: 600
      max_events: 20
      role: developer
      action: pause_role
    - id: spend-spike
      signal: spend
      window_seconds: 3600
      max_spend: "25"
```

Signals, limits, `dry_run`, `tick_seconds` and `halt_after_failed_ticks` are in the `breakers`
section of your team's configuration (see the [configuration reference](/reference/configuration/#breakersconfig)).
For what to do when the server halted the organization by itself, see
[`org halt`](#org-halt) and the table at the top: only a signed-in person can lift a halt.
