# Store maintenance commands: `db migrate` and `content expire`

Two commands for the person who runs the store. Both go through the same store selection as the
rest of the command line (`docs/cli.md`, "Which store a command uses"), except where noted. In a
self-hosted deployment they run as services (`migrate` once at start, `retention` hourly,
`docs/self-hosting.md`); this page is for running them by hand.

## `nuramesh db migrate [--team DIR] [--grant-to ROLE]`

Applies the pending PostgreSQL migrations and exits. It **needs `NURAMESH_DATABASE_URL` and never
falls back to SQLite**. It is idempotent and safe next to another process opening the same
database (an advisory lock). The `<team>` argument is required but this command does not read the
team's files. The URL is never printed.

**Upgrade safety.** `nuramesh db migrate --plan` prints what is pending (not-additive
migrations marked) and any right the connecting role lacks, and changes nothing. While a
not-additive migration is pending, `db migrate` refuses unless you pass `--i-have-a-backup REF` or
set `NURAMESH_MIGRATE_ACK` to the highest pending number; the acknowledgement is recorded in the
ledger. The services (`serve`, `worker`, retention) never migrate: behind the code they refuse to
start. See `docs/upgrade-notes.md`.

The examples ran against a fresh scratch database on a local PostgreSQL 16 (dropped afterwards):

```text
$ nuramesh db migrate --team my-team
schema at version 32
[exit 0]
$ nuramesh db migrate --team my-team
schema at version 32
[exit 0]
```

The version number is the schema version of the build you run. Back up and stop old processes
before moving to a newer schema (`docs/upgrade-notes.md`).

Without the variable:

```text
$ nuramesh db migrate --team my-team
migrate needs NURAMESH_DATABASE_URL (it never uses SQLite)
[exit 2]
```

If the database cannot be reached, it exits 1 with `couldn't migrate the database: ...` and the
error class, never the URL.

### `--grant-to ROLE`

After the tables exist, gives that service role its least-privilege rights (the role owns nothing;
ledger tables stay append-only for it). Run it as the table owner. Its name must be a plain lower-case name (the example
used a role created beforehand in the scratch database):

```text
$ nuramesh db migrate --team my-team --grant-to docs_cli_svc
schema at version 32
[exit 0]
$ nuramesh db migrate --team my-team --grant-to "Bad Role"
--grant-to needs a plain lower-case role name
[exit 2]
```

If the grants cannot be applied it prints `couldn't grant to <role> (<error class>)` and exits 1.

## `nuramesh content expire [--team DIR]`

Deletes stored content whose retention has passed, never for an organization on legal hold, and
writes one `content.expired` ledger entry for each organization with counts only. A run does this
itself while it works; this command is for a store with no run active (cron, or by hand).

It also finishes an erasure of a person's content that a legal hold put off: once the hold is lifted
the rows marked for that person are deleted and one `content.erased` entry is written (counts and
kind names only, `deferred: true`, actor `system`). If that step fails for an organization the
failure is logged and recorded as a system ledger entry (the error class only) and the command
carries on with the expiry; the rows stay marked and the next run retries.

```text
$ nuramesh content expire --team my-team
expired 0 content record(s)
re-captured 0 configuration(s) before expiry
[exit 0]
```

With `NURAMESH_DATABASE_URL` set it works on PostgreSQL; otherwise on the SQLite store. It does not
read the team's configuration. A content store that is not available is exit 2
(`the content store isn't available: ...`).

## Related commands that already have a page

| Command | Page |
|---|---|
| `nuramesh content shred`, `nuramesh keys status`, `nuramesh keys reconcile`, `nuramesh keys adopt-legacy` | `runbooks/key-tombstone.md` |
| `nuramesh otel check / export / rotate-pseudonym-key` | `telemetry.md` |
| `nuramesh ledger export` and `verifier/nuramesh_verify.py` | `auditor-guide.md` |
| `nuramesh org`, `roles`, `breakers` | `kill-switch.md` |
| `nuramesh agents credentials` | `agent-credentials.md` |
