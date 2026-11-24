# The `nuramesh` command line

Commands: `nuramesh --help`. Model governance commands (`models ...`, `run`, `eval run`) are specified
in the internal specification section 10. This page is the one place that lists every exit
code; an internal test reads the table below and proves, with a real invocation, that
each code is produced (and that the table lists no code the CLI cannot produce).

## Exit codes

| exit | meaning |
|---|---|
| 0 | success: every task is done and the ledger verified, or the command did what it was asked |
| 1 | the work ended but not all is done: a gate failed, an `otel` check or export failed (it was 3 before R1.43, OD-7b), a task is `needs_human` (a blocked model call included), a ledger chain is broken, or a connection test failed |
| 2 | not started, invalid request: bad arguments, configuration, team module, plan or run cap (nothing was run; at most the catalog seed was written) |
| 3 | refused: no usable model catalog or connection for a bare `try` (add `--fake` for the demo), model governance refused the start, `models check` found a block, or a refused `models` / `stop` action |
| 4 | the organization halt is in force (at the start, or it stopped the run or the eval), or a human stopped the run: a halt in force at the start wins over exit 3 |
| 4 | role paused: `eval run` for an agent whose role is paused in the org stream stops with the same code as a halt, and the message names the role (`role 'dev' is paused`); a `try` only cancels the paused role's tasks (the other roles go on, so it ends 1 with tasks not done, or 4 if a halt or a human stop ends it); the `stop`, `org halt` and `roles pause` commands themselves always succeed (exit 0): they write only to the ledger and need no content key, so a tombstoned or key-unavailable organization can still be stopped |
| 5 | unavailable, retry may work: the store is busy or locked, or an `otel` connection check could not reach its target (new in R1.43, OD-12; the hidden alias of a renamed command still exits with the old code, 2, for one release) |
| 6 | done with warnings: `minter anchor-verify` found a STALE or EPOCH warning or a shared key (it was 2 before R1.43, OD-7a: update monitors that test for 2) |
| 130 | Ctrl-C: `nuramesh try` aborts on the second one (the ledger says `aborted`); every other command stops at the prompt or the step and writes nothing (P8) |

A mistyped command or sub-command exits 2 and prints `unknown command '...'` with a `did you mean '...'?` line when a real command (or an old name that was renamed) is close; nothing is suggested or run otherwise.

**The halt is real.** `cli_models.org_halted` and the server's model routes read the
organization's halt state with `HaltWatch` over the `org:<id>` stream; a state that cannot be read
counts as a halt (fail closed). An internal test guards that no never-halted stub
comes back.

## Command names, the team folder and shell completion (R1.43)

The names follow one rule: a group is a noun and a verb follows (`autonomy grants list`), countable
groups are plural (`scorecards`, `ceilings`, `grants`), and a command that needs a team folder
finds it instead of asking for a positional argument. The old names are hidden aliases for one
release: they run the new command, print one line on stderr (`... was renamed to ...`) and keep
their exit codes and `--json` output.

| Old name or form | New name |
|---|---|
| `run TEAM` | `try TEAM` |
| `report TEAM --run-id R` | `report R` |
| `stop TEAM --run-id R` | `stop R` |
| `migrate TEAM` | `db migrate` |
| `expire-content TEAM` | `content expire` |
| `verify-ledger TEAM` | `ledger verify` |
| `export-ledger TEAM --out F` | `ledger export --out F` |
| `scorecard show` | `scorecards show` |
| `autonomy grants`, `grant`, `revoke` | `autonomy grants list`, `grants create`, `grants revoke` |
| `autonomy ceiling set`, `autonomy settings` | `autonomy ceilings set`, `autonomy settings set` |
| `content shred ORG`, `keys status\|reconcile\|adopt-legacy ORG` | the same with `--org ORG` |
| `roles pause TEAM ROLE` (every other command with a positional TEAM) | `roles pause ROLE --team DIR` |

**The team folder (rule T).** A command takes `--team DIR`, else `NURAMESH_TEAM_DIR`, else the
nearest folder above the current one that holds `nuramesh.yaml` (`.yml`, `.json`). None found is
exit 2 naming the three ways. `try`, `serve`, `worker`, `init` and `validate` keep their positional
folder. The operator commands (`db migrate`, `content shred`, `auth purge-sessions`,
`auth erase-owner`, `auth close-org`) print `organization <id> (team folder <path>)` on stderr
first; the irreversible ones (`content shred`, `auth purge-sessions`, `auth erase-owner`,
`auth close-org`) accept the folder only from `--team`.

**The organization (rule O).** `--org X` that differs from the team folder's organization is exit 2
and nothing is written. `content shred` and `keys reconcile|adopt-legacy` take the organization they
act on from `--org` and have no default (`keys status` without `--org` lists every tombstoned one).

**`--dry-run`.** For the commands that write, it checks the arguments and the team folder, prints
`dry run: ...` and writes nothing. The commands that start work, send mail, show a secret or run
for long (`try`, `serve`, `worker`, `eval run`, `calibrate run`, `models test`, `otel export`,
`email check`, `auth tokens create`, `auth new-setup-token`, `auth recovery-code issue`,
`anchor keygen`, `minter anchor-record`, `minter anchor-install`, `otel rotate-pseudonym-key`) refuse it
with exit 2. A command that only reads accepts it and runs as usual.

**`serve` and authentication.** `nuramesh serve` starts with authentication on; `--auth on|off` (a bare `--auth` is
`on`; `--auth t` is a bare `--auth` and the team folder `t`). `--auth off` must be typed, no variable or setting turns
it off: loopback only (a network address, `--allowed-host`, `--trusted-proxy` or a hosted deployment is a usage
error), read-only (every write is 403 `authentication_required`), with a banner in capitals on standard error. With
authentication on, no owner ever recorded, a loopback bind, no proxy, not hosted, not in a container and standard output on a terminal,
`serve` issues a setup token valid 900 seconds after the port is bound and prints exactly one line with
`http://<host>:<port>/setup#token=<token>`; the token is never printed to a log, an error or the ledger, and a
restart or `nuramesh auth new-setup-token` (24 hours) retires the earlier one. Otherwise it prints only a hint.

**Shell completion.** `nuramesh completion bash|zsh|fish` prints a script built from the command
tree (no store, no network); the hidden aliases are not completed.

```sh
nuramesh completion bash > ~/.local/share/bash-completion/completions/nuramesh
nuramesh completion zsh  > "${fpath[1]}/_nuramesh"
nuramesh completion fish > ~/.config/fish/completions/nuramesh.fish
```

`nuramesh --version` prints the package version. Every command's `--help` ends with an `Examples:`
block.

## Everyday commands, prompts and confirmations (R1.43 A2)

`nuramesh` with no command prints the organization's state (halted or not, paused roles), the
unfinished runs, the items waiting for a person (id, kind, role, age; never `asked_because`, plan
text or a reason) and the everyday commands, in at most 20 lines, exit 0. It opens the store
read-only: the SQLite file is opened with `mode=ro` and `query_only`, so no byte of the database or
its `-wal` changes, even with a writer connected (SQLite may create the empty `-shm` and `-wal`
helper files next to a WAL database); a missing database is not created and one that
needs a migration is not migrated (it says `nuramesh db migrate`). Runs with no recorded
organization, and other organizations' runs, are invisible to every team through the CLI (as in the
API); `report RUN` and `stop RUN` treat both the same way (exit 2 "no run" and exit 3).
`reject --dry-run` runs the same checks as the real command, opens the store read-only (no file is
created, no schema migrated) and makes no encryption key: it writes nothing. Without a team folder it prints only the commands.

| Command | What it does |
|---|---|
| `status [RUN]` | the unfinished runs and what waits for a person, or one run (`--pick` chooses on a terminal) |
| `reject TASK --human H --reason T [--criterion ID]...` | rejects the one item waiting for a person on TASK (a pending approval or a human gate), or the approval named by id. Two waiting items: a numbered pick on a terminal, else exit 2 listing them. Nothing waiting: exit 2. A human gate takes a decision on every criterion, and the CLI cannot approve, so every criterion is rejected |
| `approve` | **does not exist on the command line** (OD-11): approving widens, a typed `--human` is only a claim. Use the app or `POST /v1/approvals/{id}/approve` or `/reject` as a signed-in person |

**Prompts.** A missing RUN (`stop`, `report`), TEAM (`try`), TASK or `--reason` (`reject`),
`--human` (`stop`, `reject`) and an optional `stop --reason` are asked only when stdin AND stderr are
terminals, `CI` is not set and `--yes` was not passed. Questions and pick lists go to stderr (stdout
carries only the result); a pick list shows ids, states and ages only. With `--yes`, no terminal or
`CI` set, a missing argument is exit 2 (`missing RUN: pass it, or run on a terminal`). A typed
reason is not written anywhere but where `--reason` would have put it (the content store; the
ledger keeps a hash). Ctrl-C at a question writes nothing and exits 130. No secret is ever an
argument: a secret shown once goes to a terminal's stderr and never to stdout, even with
`--output json`.

**Confirmations (BC9, OD-14, OD-15).** `content shred` asks you to type the organization id; `--yes`
skips the question; with no terminal and no `--yes` it exits 2 and shreds nothing. *This is a
behaviour change: a script without a terminal that ran `content shred` now needs `--yes`.*
`--confirm ORG` is an optional extra check that must equal `--org`. `auth factor-key destroy`
gets the same rule in the passkeys PR. `auth erase-owner` and `auth close-org` stay terminal-only
(stdin and stdout) with no skip flag at all: they take no `--yes` and no `--confirm` (either is
exit 2 at parse time), only a person at a terminal answers. `models local-model` without `--yes` asks on a terminal and exits 2 without one (it used
to print the proposal and exit 0); `--dry-run` prints the proposal and writes nothing.

## Output formats

**`--output json` (BC4, freeze B).** `--output table|json` is accepted by every command, and it is
never a silent no-op: a command either honours `json` or refuses it with exit 2 and says so on
stderr (`--output json is not supported by `<command>``); `--output table` is always accepted.
Honoured, printing the keys of the route the command maps to: `status` (and `status RUN`),
`report`, `stop`, `reject`, `org status`, `breakers list`, `auth tokens list`, `models catalog`,
`models key status`, `doctor` (its own schema) and `demo` (not with `--stack`), see the doctor section below.
Honoured as the command's own JSON (the same output as its older `--json` flag): `try`,
`scorecards show`, `autonomy propose`, `autonomy grants list`, `autonomy grants create`,
`autonomy grants revoke`, `autonomy ceilings set`, `autonomy settings set`, `savings tokens`,
`anchor verify`, `models connections`, `models presets`, `models check`. Every other command prints text only and
refuses `--output json`; a command that shows a secret once (`auth tokens create`) has no JSON
form on purpose. A bare `nuramesh` prints a status for people: its text is not a format to parse (`--output json`
gives the `status` keys). An internal test enumerates
every command, so a new command must be listed here or it is refused.

**`--json` is hidden.** It is kept for one more release on the twelve commands that had it
and prints `` `--json` was renamed to `--output json` `` on stderr; it is not in any `--help`.
Use `--output json`. Both spellings are removed after 2027-03-01 (the same date as the API's
`Sunset`).

**`models presets` and `models connect --preset ID`.** `models presets` lists the built-in presets for the
`openai-compatible` provider (Mistral, Gemini, Groq, Together, OpenAI, and the local servers Ollama, vLLM,
LM Studio, llama.cpp and TGI): a base URL and a model-name hint each, no price and no key. `models connect
openai-compatible --preset ollama` prefills the endpoint, the connection id and `--auth`; any flag you pass
wins. A hosted preset needs `--key NAME`, the name of a key binding in the operator's binding file (never the
key). A local preset needs the server to run with `NURAMESH_ALLOW_PRIVATE_NETWORK=1` before `models test`
or a call can reach it. An unknown price stays unknown: set one with `models price`.

**`models price`.** The prices are `--input-price N` and `--output-price N` (US$ per million
tokens). The old `--input N` and `--output N` still work, hidden, until the same date; `--output`
on this command is never a format, so `--output json` here is refused.

**Renamed commands and positional forms.** Every old spelling (13 renamed commands, a positional
team or organization, `stop/report TEAM --run-id R`) keeps working and prints one stderr line
saying it was renamed and that the old form is removed after 2027-03-01. The date is defined in
`nuramesh.cli_aliases` and in `nuramesh.server.deprecations`, and a test keeps them equal (it also
checks that the `--json` warning is built from the CLI one).

## `nuramesh doctor` and `nuramesh demo` (#991)

`nuramesh doctor [--host H] [--port P] [--network-checks] [--output json]` checks this machine, one line each:
`PASS`, `WARN` or `FAIL`, a fixed message and a `fix:` hint. Checks: Python 3.12 or newer, the
`serve` port is free (default 8765), the database (`NURAMESH_DATABASE_URL` reachable; SQLite when
unset), the auth key file (`NURAMESH_AUTH_KEY_FILE`: unset is a warning, a missing or too-open file
fails), a container runtime (Podman or Docker: a warning in local fake-model use, a failure when a team database or real-model setting is set: `NURAMESH_DATABASE_URL[_FILE]`, `NURAMESH_REQUIRE_DATABASE` on or malformed, `NURAMESH_ANTHROPIC_KEY_FILE`, `NURAMESH_OPENAI_COMPATIBLE_KEYS`, `NURAMESH_SECRET_DIR`, `NURAMESH_SECRET_STORE` other than `fake`), and the installed extras (a warning
listing what is missing), email (`NURAMESH_SMTP_HOST` unset is a warning; a host without a
sender or `NURAMESH_PUBLIC_URL`, a bad `NURAMESH_SMTP_SECURITY`, or `none` to a remote host fails; nothing is sent), TLS and
proxy (a `SSL_CERT_FILE` or `REQUESTS_CA_BUNDLE` naming a missing file fails; a proxy address is
never shown) and free disk here (under 1 GiB warns, under 100 MiB fails). Not yet checked: the
squash-only merge setting (waits for Merge Gate). Exit 0 when nothing failed (warnings do
not fail), 1 when a check failed.
**No network by default (H-19).** Plain `nuramesh doctor` opens no outbound connection (the
port check only binds the local port). `--network-checks` turns on two probes. `database`: when `NURAMESH_DATABASE_URL` is set, connect
read-only and look for the ledger tables (without the flag it shows `SKIPPED`, hint `not run: add
--network-checks to test the database connection`; with no URL set it still passes and opens no
connection). `email_connect`: a TCP connect to the mail server, plus a TLS handshake only for
`NURAMESH_SMTP_SECURITY=tls` (for `starttls` and `none` it is a TCP connect only: no SMTP
conversation, no STARTTLS negotiation), through the same guarded connector the model calls use, so
a private or loopback mail host is refused and reported as `FAIL`. Nothing is sent. Without the flag
a skipped probe shows `SKIPPED` (not run, never `PASS`). The `tls_proxy` check only reads
environment variables (a proxy is set, a named CA file exists) and never connects, with or without
the flag.
`--output json` prints `{"ok", "summary": {"pass", "warn", "fail", "skipped"}, "checks": [{"id", "status",
"message", "hint"}]}`. It never prints a secret, a database URL or a path from the environment.

`nuramesh demo [--dir DIR] [--allow-unsandboxed]` creates the `dev-team` template in a new folder
(a temp folder by default) and runs it as `nuramesh try DIR --fake --approve-as human:demo`: the
fake model only, no key, no cost, no network call. With no container
runtime it exits 1 and says how to install one, unless `--allow-unsandboxed` is given: then the
demo prints one line that it runs unsandboxed, and the run prints the `SANDBOX OFF` banner and
records it in the ledger. When a runtime is present the flag has no effect (the demo says so) and
the tests run in a container. `--dry-run` is refused. The demo refuses (exit 2, nothing created) when
`NURAMESH_DATABASE_URL` is set (non-empty) or `NURAMESH_REQUIRE_DATABASE=1`: it would write a throwaway
run into that database, and the ledger is append-only. Unset the variable to run it.

`nuramesh demo --stack [--port N]` runs the same demo and then serves the folder as a throwaway local
stack until Ctrl-C: `serve --auth` on `127.0.0.1` only (default port 8765), sign-in required, fake
model, nothing leaves the machine and nothing is created in a cloud. It needs a
terminal. Only one setup token is ever made: at a terminal `serve` prints its one-time setup link
(`/setup#token=...`, it works once and for 15 minutes) and the demo makes none of its own, because
`serve` retires every earlier token; where `serve` prints no link (for example in a container) the
demo shows a token (the same as `nuramesh auth new-setup-token`) on stderr only, never on stdout
or in the ledger, to paste on the setup page (`/setup`, or `POST /v1/auth/setup/redeem`). Redeeming
it makes that person the owner. Without a terminal (or with `--port` outside 1 to 65535, or
`--port` without `--stack`) it creates and serves nothing and exits 2 with a message. A temp folder
is removed when the stack stops (Ctrl-C, `kill` or a closed terminal); a folder given
with `--dir` is kept. `--stack` has no `--output json` (it serves until stopped).

## Which store a command uses

Every command that reads or writes the ledger (`try`, `report`, `ledger export`, `ledger verify`,
`anchor export`, `anchor rotate`, `eval run`, `eval status`, `validate` with a store) uses the same selection as
`serve` and `auth`: PostgreSQL when `NURAMESH_DATABASE_URL` is set (it wins over `--store`, and it
is read from the environment only, never printed), else `--store`, else the team's
`.nuramesh/runs.db`.

- **Read-only commands never create a store.** A missing SQLite file, or a PostgreSQL database
  that holds no ledger tables yet, is refused with exit 2 and nothing is created (`eval status`
  treats it as "nothing recorded yet" and answers from an empty store). `try` and `eval run` create
  the store when it is missing.
- **`ledger verify` fails closed on an empty ledger.** A ledger with no streams proves nothing
  (usually the wrong database), so it exits 1. Pass `--allow-empty` when an empty ledger is
  expected (for example a first check in a new deployment). `--expected-head` for a stream that is
  absent is still reported as broken.

## Where each command is documented

| Command | Page |
|---|---|
| `org`, `roles`, `breakers` | [kill-switch.md](kill-switch.md) |
| `calibrate run`, `calibrate status` | `nuramesh calibrate run --team <team> --suite S --verifier V --fake` judges every labelled sample of calibration suite S with verifier V on the fake model (one metered call per sample, through the same prompt builder the real gate uses) and records ONE `calibration.scored` event (catch rate, false-accept rate, accept rate, agreement, cost; ids and counts only). Exit 0 passed, 1 failed (also a budget or policy stop, recorded), 2 usage (unknown suite or agent, not a verifier, no model, a real model, the samples answered by more than one model), 3 refused (a lost key, also mid-run: recorded), 4 halted or paused. `calibrate status <team> --verifier V [--suite S]` exits 0 if every suite is calibrated, else 1. A verifier gate may list `calibration_suites`: an uncalibrated verifier makes that gate return `error` (`verifier_not_calibrated: <suite>: <reason>`) before any model call, and the task goes to a human. |
| `db migrate`, `content expire` | [store-maintenance.md](store-maintenance.md) |
| `otel` | [telemetry.md](telemetry.md) |
| `try`, `report`, `stop`, `init`, `validate` | [quickstart.md](quickstart.md) |

## Stored model keys (`models key`)

`nuramesh models key set|rotate|revoke|status PROVIDER [--endpoint-id ID] [--server URL]` (R1.14c). It
is an API client with an **owner login**: it opens no store and reaches no vault; the server does the
work, ledgers it (`model.key_*`) and applies the owner-only, MFA and hosted rules of the app. It asks
for the owner's email and password on the terminal without echo (and an MFA code if the owner has
one); with no terminal it exits 2, so a script or an agent, which has no owner session, cannot use it.
`set` and `rotate` read the key from `--stdin`, from an inherited descriptor (`--fd N`) or a no-echo
prompt; there is no `--value`, and the key is never in an argument, the environment, an output or an
error text. `set` is for a connection with no live key (it needs `--reason`, 3 to 500 characters);
`rotate` adds a version (`--grace-seconds`); `revoke` needs `--reason` and the organization id
(`--confirm ORG`, typed at a terminal, or `--yes`). The server is `--server` or `NURAMESH_API_URL`
(https, or http to this machine only; no proxy, no redirect). `--dry-run` checks the arguments only.
Exit codes: 0 done, 2 usage, 3 refused by the login or the server (fixed code printed).
