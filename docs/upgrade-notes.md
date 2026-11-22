# Upgrade notes

How to move an existing install to a newer version. Newest first. (The release notes are generated from merged PR
titles and cannot carry this; see the release notes.)

## The upgrade procedure

1. **Back up** the store and the key folder together (`runbooks/backup-restore.md`).
2. If any migration you are about to apply is **not additive** (the notes below start with "Not additive": today
   migrations 7, 8, 11, 15, 22, 24 and 27), stop every process that uses the database first (servers, workers, the CLI,
   schedulers; Compose: `docker compose stop api worker retention`) while still on the old version. If you move
   across several versions, one non-additive migration in the range is enough. Never run old and new versions
   against one database during such a migration. (Compose runs `migrate` before it recreates `api` and `worker`, so an old process left running would
   write to rows or columns the migration is changing.) The list of migrations that cannot be undone is the
   one `nuramesh db migrate --plan` reads.
3. Get the new version (Compose: `git pull`, rebuild the images). **Read the plan first:** `nuramesh db migrate --plan`
   (with `NURAMESH_DATABASE_URL` set) prints the pending migrations, marks the not-additive ones, and names any right
   the connecting role lacks; it changes nothing. Then apply: the services (`serve`, `worker`, the retention and
   purge commands) never migrate: they refuse to start while the schema is behind and tell you to run
   `nuramesh db migrate`. That command needs the table owner's rights (`docs/store-maintenance.md`), applies only what
   is pending, in order, and prints `schema at version N`. While a not-additive migration is pending it refuses
   unless you acknowledge the backup of step 1: `nuramesh db migrate --i-have-a-backup <dump file or timestamp>` or
   `NURAMESH_MIGRATE_ACK=<highest pending number>` (Compose: set it in `.env`; the `migrate` service passes it on).
   The acknowledgement is recorded in the ledger. SQLite stores migrate when the new version first opens them.
4. Run `nuramesh ledger verify --team <team>`; it must exit 0.
5. **Roll back = restore the backup** and start the old version. Downgrade is not supported: the old code refuses a
   newer schema. The ledger is never rewritten by a migration (the notes below say so wherever it matters).

The migration list is `src/nuramesh/stores/migration_names.txt`. A test (`tests/test_docs_operator.py`) fails when a
migration in that list has no `### Migration N (name)` note below. Add the note in the same change that adds the
migration, and say plainly whether it is additive.

## Compose: refresh grace is now on

Compose: refresh grace is now on. The `api` service now runs the 30 second refresh grace window, with its key as a
Docker secret.

1. Rebuild the images first (`sh scripts/images/build.sh api worker`): a stale image has the old wrapper, which
   ignores the `_FILE` variable (it fails closed, but the message misleads).
2. Before `up`, run `sh deploy/compose/init-secrets.sh` once. It prints `created refresh_successor_key` and
   `kept    <name>` for the rest (padded as in the script). Without it `docker compose up` stops on the missing secret.
3. The window hides a refresh reuse inside 30 seconds (T109 option b accepted). Compose was stricter than the
   product default; this relaxes it. `NURAMESH_REFRESH_GRACE_SECONDS=0` in `deploy/compose/.env` restores strict
   detection (the key file is still required by Compose and validated).

Not additive-migration related: there is no migration.

## Compose: the refresh key and the grace rules

The guidance below is the Compose operator text for the refresh grace window and its key (moved here from the self-hosting page).

* `api` runs with the refresh grace window on (30 seconds; `NURAMESH_REFRESH_GRACE_SECONDS=0` in `.env` turns it
  off). Two browser tabs that refresh the same session within 30 seconds get the same new tokens. The window
  needs a server key: `sh deploy/compose/init-secrets.sh` creates it as the secret file
  `deploy/compose/secrets/refresh_successor_key`, mounted in `api` only; `nuramesh-env` reads it, so the key is
  never in the compose file or `docker inspect`. The window also hides a refresh reuse inside those 30 seconds.
  A missing or malformed key file stops `api` at start, even with the window off.
* **Rotating the refresh key:** `rm deploy/compose/secrets/refresh_successor_key && sh deploy/compose/init-secrets.sh
  && docker compose -f deploy/compose/compose.yml up -d --force-recreate api`. Tokens already issued stay valid;
  only a refresh replayed inside the 30 seconds after the swap ends that one session. Rotation alone does not end
  sessions whose tokens were computed with a leaked key. After a suspected leak of the key file together with a
  database dump, rotate AND end every session. No single command does that; the routes are: for other people,
  `POST /v1/auth/users/{id}/disable` then `enable` (no mail needed; it refuses your own account and the last
  active owner) or `.../password-reset` (needs mail); for your own account, turn MFA on or off
  (`POST /v1/auth/mfa/disable` ends your other sessions), then sign out (`POST /v1/auth/logout`), or use the
  forgot-password flow when mail is set up. A session's absolute lifetime (`absolute_expires_at`; the absolute
  lifetime) is the outer limit on exposure.
* Keep database dumps and the secrets folder in separate stores (or encrypt one of them): a dump plus the key
  file lets the holder compute the live tokens of sessions whose refresh token was spent.

## Setting changes (configuration freeze)

Before the first public release three settings changed with no compatibility alias. Check your `.env`, unit files and shell profiles.

- **One boolean grammar.** Every on/off setting takes `1`, `true`, `yes` or `on` (on) and `0`, `false`, `no` or `off` (off), any case, whitespace ignored. An empty or whitespace-only value (for example `NURAMESH_HOSTED=`) means unset and takes the setting's default: **empty = unset**, it never stops the start. Any other word now stops `serve` and `worker` at start with a line naming the variable (never the value). The same holds for the `*_FILE` twins: an empty `NAME_FILE` counts as not set; `NAME` and `NAME_FILE` both non-empty is still refused. `NURAMESH_OPERATOR=true` used to be silently off and is now on; `NURAMESH_REQUIRE_DATABASE`, `NURAMESH_KEYS_DPA_PUBLISHED` and `NURAMESH_SMTP_ALLOW_INSECURE` also accept the full set now.
- **`_FILE` twins.** `NURAMESH_DATABASE_URL_FILE`, `NURAMESH_REFRESH_SUCCESSOR_KEY_FILE` and `NURAMESH_OTEL_HEADERS_FILE` hold a path to a file (mode `0600`). Setting a variable and its twin together, or a file readable by group or others, stops the process at start. The plain variables still work.
- **Compose names.** In `deploy/compose/.env` rename `NURAMESH_VERSION` to `NURAMESH_COMPOSE_IMAGE_TAG` and `NURAMESH_TEAM_PATH` to `NURAMESH_COMPOSE_TEAM_PATH`. The old names are no longer read: an old `.env` makes Compose stop with "set NURAMESH_COMPOSE_TEAM_PATH in .env" (the image tag falls back to `local`).

## Notes by migration

Newest first. "Additive" means the migration only adds tables, columns, indexes or constraints, inserts rows only into
tables it creates, and deletes, updates or drops nothing that already exists. "Not additive" means it updates or
moves existing rows or drops a column or table. Each note was checked against the DDL and the in-application steps of
both stores (PostgreSQL and SQLite).

### Migration 32 (approvals_sweep)

Additive: an index `approvals_anchor_day` on the day a decided or consumed approval ended (so the
approvals sweep reads only rows that can be due) and, on PostgreSQL, the `SECURITY DEFINER` function
`approval_delete_if_due` owned by `nuramesh_definer` with SELECT and DELETE on `approvals` for that role.
No existing row is read, rewritten or deleted; the sweep itself deletes approved, denied and consumed
approvals 396 days after they were decided (or consumed) and never touches the ledger. After upgrading, run
`nuramesh db migrate --grant-to ROLE` again so the service role gets EXECUTE on the function (it has no
DELETE on `approvals`).

### Migration 30 (api_token_names)

Additive: a new table `api_token_retired_names` (the keyed hash of a renamed or deleted token name,
per organization and key epoch), and on PostgreSQL three `SECURITY DEFINER` functions owned by
`nuramesh_definer` (`api_token_delete`, `api_token_erase_issuer`, `api_token_normalise_issuer`) with
narrow grants for that role: SELECT and DELETE on `api_tokens` and `api_token_revoke_links`, UPDATE
on `api_tokens` (`name`, `created_by`, `revoked_by`) and SELECT on `users` (`org_id`, `id`,
`erased_at`). No existing row is read, rewritten or deleted. After upgrading, run
`nuramesh db migrate --grant-to ROLE` again so the service role gets the column grant
`UPDATE (name)` on `api_tokens`, INSERT and SELECT on the new table and EXECUTE on the three
functions.

### Migration 31 (rbac)

Additive. Adds one nullable column, `step_up_at`, to the `auth_sessions` table (R2.13: the last strong authentication on this session (any method)). No existing row is rewritten or deleted; every existing session reads as never stepped up and is asked once. The same on PostgreSQL and SQLite.

### Migration 29 (solo_approval)

Additive: adds one nullable column to `auth_sessions` (`password_confirmed_at`: when a session last
re-confirmed the password) and three nullable columns with one index to `approvals` (`approval_mode`,
`decided_by`, `decided_at`: who could have decided, for decisions made from now on). No existing row is
read, rewritten or deleted: older sessions read as "not confirmed" and older approvals have no mode
(solo approval). Nothing to do before or after.

### Migration 28 (extension_records)

Additive: adds the `extension_records` table (records an installed plugin keeps), its indexes and the
definer function `extension_records_erase`; no existing row is read, rewritten or deleted. It needs the
`nuramesh_definer` role to exist first (E8):
Applies to every database that is upgraded to schema 28 or later (existing Docker Compose stacks,
and managed PostgreSQL such as Azure Flexible Server or RDS). A fresh Compose stack gets the role
from `deploy/compose/initdb/10-roles.sh`; an **existing** stack's initdb scripts never run again, so
do this once **before** you upgrade, as a database superuser (Compose: `postgres`; managed: the
cloud admin role), replacing `nuramesh_owner` with the role that runs `migrate`:

```sql
CREATE ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT nuramesh_definer TO nuramesh_owner;
```

Why: the migration owner has no CREATEROLE, and PostgreSQL 16 lets a role hand a function to another
role only when it can SET ROLE to it (hence the `GRANT`). The migration no longer creates the role;
it checks that the role exists, that none of login, superuser, createdb, createrole, replication or
bypassrls is set, and that the migration owner can SET ROLE to it. If not, it stops with a message
that names these two statements and changes nothing (one transaction). **Never keep a role that
another login created.** If `CREATE ROLE` reports "already exists" and you did not create it, look at
`SELECT pg_get_userbyid(member), pg_get_userbyid(grantor) FROM pg_auth_members WHERE roleid =
'nuramesh_definer'::regrole;`, then `DROP ROLE nuramesh_definer;` and create it again as above; do
not repair it with `ALTER ROLE` (the other login may still hold ADMIN on it). Only the migration
owner may be a member of the role: any other member, ADMIN-only included, makes `migrate` stop and
makes **every store open refuse to start** (the check runs again at each open, with the
role and the `REVOKE ... GRANTED BY ...` statement in the message). **Managed PostgreSQL.** The cloud admin login (Azure Flexible Server, RDS) has CREATEROLE but is
not a superuser, so after it runs the two statements above PostgreSQL 16 records an ADMIN-only row for
it on `nuramesh_definer`, granted by the provider's bootstrap superuser (`azuresu` on Azure Database for
PostgreSQL Flexible Server, `rdsadmin` on AWS RDS; PostgreSQL 16 records every superuser's grant under that
one role). You cannot run the printed `REVOKE ... GRANTED BY <that role>` yourself (no customer holds it).
So, for this one case, name that role in `NURAMESH_PG_PROVIDER_ROLE` (no default; it is listed in the
environment table in `configuration-env.md`; never set it on self-hosted PostgreSQL): a member's row is then allowed only when it is ADMIN-only (no SET, no INHERIT) **and** its
grantor is exactly that role, which allows any ADMIN-only row that any superuser grants. Every other
member, and every other ADMIN row, is still refused. Each start that relies on this writes a
`decision.recorded` ledger entry (`kind` `provider_role_exception`, `role`, `grantor`, `member`) on the
stream `org:store-startup` (the id `store-startup` is reserved: no organization can have it), every
start. A check-only open (`migrate=False`) writes it too, so a read-only database role without INSERT on
the ledger tables is refused while the exception is in use (fail closed; open the store with the service
role, not a read-only one). With the setting unset the refusal stays, and its message names the setting.
**Be plain about what this costs:** an ADMIN holder can grant itself SET and become the role, and the
provider's superuser can take the role as well; the exception trusts the provider and the cloud admin
login, and the ledger entry only makes each start visible, it does not prevent it.
**What every open enforces (the code, exactly).** The definer is not found by name: the store reads the
owner of the definer functions (`public.extension_records_erase` from schema 28) and refuses to start
unless (1) every such function exists and they all have one owner, (2) that owner is named exactly
`nuramesh_definer`, (3) it has no LOGIN, SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS (the
migration's attribute check; a role password cannot be read without superuser rights, but a password
without LOGIN opens nothing), (4) it is a member of no other role, and (5) no role other than the migration
owner is a member of it (plus the one ADMIN-only exception above). So a rename (with or without a new
empty role of the old name), a login made of it, or a SET grant on a renamed copy is refused at the next
start. The check also stops every start when any login that holds ADMIN on one of its own roles makes the
definer a member of that role (a CREATEROLE login can use this to stop all store opens; it is fail-closed
and intended). Printed fix statements quote every role name with `format('%I', ...)`.
Residual risk: whoever created the role holds ADMIN on it and, on PostgreSQL 16, can become it between
two starts and act before the next open; the open check detects a membership still present at the next
start, it does not prevent it. A SET grant that is made and revoked again between two starts leaves no trace. Use a role created by your own operator, never by a shared login.
Compose example:
`docker compose ... exec postgres psql -U postgres -d nuramesh -c "..."`.

### Migration 27 (mfa)

Not additive, but small (R1.38; migration 27). Stop every process first (step 2). Creates four tables (`mfa_factors`, `mfa_recovery_codes`,
`mfa_challenges`, `org_auth_settings`) and adds three columns to `auth_sessions` (`mfa_verified_at`, `mfa_method`,
`login_via`). It then UPDATES every existing session row once, setting `login_via` to `password` (the only kinds of
session that existed before were password and setup-token sessions). PostgreSQL afterwards makes `login_via` NOT NULL
with a CHECK on `password` and `oidc`; SQLite keeps it nullable and the application refuses a missing or unknown value.
With no `org_auth_settings` row an organization does not require MFA, so nothing changes for anyone until an owner
turns it on. No ledger entry is touched. Old sessions have no `mfa_verified_at`.

### Migration 26 (password_reset)

Additive (R1.41). Creates three tables and one index: `password_reset_tokens` (reset tokens; a decoy has no user), `password_login_blocks` (the password-login block) and `api_token_revoke_links` (links that revoke an API token). No row holds an email address. No existing row is read, changed or deleted. The same on PostgreSQL and SQLite.

### Migration 25 (email_verification)

Additive. Creates the `email_changes` table (the pending new address of an email change, kept at most 7 days after it ends) and one index (R1.40). No existing row is read, changed or deleted. The same on PostgreSQL and SQLite.

### Migration 24 (invites)

Not additive. Adds the `invites` table and two nullable columns to `users`, `email_verified_at` and `email_verified_via`,
and then UPDATES every existing person that has an email: they are marked verified (`legacy`) at the time of the migration, so nobody is
locked out by the new rule. No row is deleted. The same on PostgreSQL and SQLite. Back up first; the way back is to restore the backup.

### Migration 23 (light_engine)

Additive (R1.20 PR A). Creates three tables and two indexes: `run_start_requests` (a start request with an atomic claim), `light_jobs` (the light engine's jobs with lease and epoch) and `light_leader` (its leader lease). `run_start_requests` also stores the plan text, its plan hash, who started the run (`started_by`), the data class and the hash of the idempotency key; the other two tables hold ids, states, owners and times only. No existing row is read, changed or deleted; the ledger is not touched. Roll back by restoring the backup.

### Migration 22 (tombstone_approx_reason)

Not additive (T258). Widens the allowed `reason` values of `key_tombstones` with `backfilled_approximate`
(a tombstone backfilled from the ledger whose date is only approximate). Stop every process first (step 2). SQLite
cannot alter a CHECK, so the table is rebuilt: rows are copied unchanged, then the permanence triggers (no update, no
delete) are recreated. PostgreSQL drops and re-adds the `reason` CHECK constraint (a short lock, no row is rewritten).
After the widening, the application adds the approximate tombstones migration 12 had to leave out. No downtime beyond
the restart; no existing tombstone is changed and the ledger is not touched. Roll back by restoring the backup.

### Migration 21 (content_subjects)

Additive. Creates the `content_subjects` table and two indexes (R1.32): who a stored content row names, so erasing a person finds it. No existing row is read, changed or deleted; existing content rows get no subject rows. The ledger is not touched.

### Migration 20 (user_disabled)

Additive. Adds one nullable column, `disabled_at`, to the `users` table (a person an owner has disabled). No existing row
is rewritten or deleted; every existing person stays enabled. The same on PostgreSQL and SQLite.

### Migration 19 (ledger_at_check)

Additive. Guards the time on every ledger entry and stream head so that it is a real time in the ledger's text form.
It applies to new and changed rows; no existing row is rewritten or deleted. PostgreSQL: adds a CHECK constraint, then
tries to validate it, which scans the whole ledger (slow on a big one, and a lock is held meanwhile); if one old row
has an impossible time it leaves that row and prints a notice, and a clean database ends fully validated. SQLite:
adds three triggers instead (a table cannot get a CHECK without being rebuilt); old rows are not scanned.

### Migration 18 (autonomy_projection)

Additive. Adds two tables that index ledger entries for the earned-autonomy reads. They are rebuildable from the
ledger; the migration fills them from the existing ledger. Nothing in the ledger changes. Expect the migration to
take longer on a large ledger.

### Migration 17 (ledger_commit_pos_index)

Additive. Adds a unique index on the commit position of ledger entries. No row changes.

### Migration 16 (ledger_commit_pos)

Additive. Adds a nullable commit-position column to ledger entries. Rows from before the migration keep an empty
value, which reads as position 0. The column is not part of any entry hash, so `verify-ledger` gives the same answer.

### Migration 15 (idempotency_subject_set)

Not additive. Moves data and drops a column. Stop every process first (step 2). Idempotency answers are expiring, mutable rows (never the ledger). The migration creates a
table of the people each stored answer names, copies the old single `subject` column into it, then drops that column
from `idempotency_keys`. All rows are kept. Roll back by restoring the backup.

### Migration 14 (idempotency_keys)

Additive. Adds the table behind `Idempotency-Key` (`runbooks/idempotency.md`). Rows expire after 24 hours.

### Migration 13 (agent_credentials)

Additive. Adds the table of agent credentials. Only the SHA-256 of each secret is stored. No existing row changes.

### Migration 12 (key_tombstone)

Additive. Adds `key_tombstones` (permanent: no update, delete or truncate) and `store_identity`. The application then
creates the store's identity and backfills tombstones from the ledger's `content_shred` entries. Keys are kept in a
per-store folder from this version on (`runbooks/key-tombstone.md`, including keys made before it).

### Migration 11 (budgets_by_org)

Not additive: see "Schema 11" below. Back up and stop everything first (step 2); the migration rebuilds `budget_balances` and
rewrites every reservation.

### Migration 10 (budget_defined_index)

Additive. Adds an index over `budget.defined` ledger entries. No row changes.

### Migration 9 (api_tokens)

Additive. Adds the API token table. Only the SHA-256 of each secret is stored.

### Migration 8 (security_log_subject)

Not additive. Adds a `subject` column and index to the security log, then the application UPDATES existing
`security_log` rows to fill the column where a row identifies a user (the rest stay empty and are logged). Stop every
process first (step 2). The ledger is not touched.

### Migration 7 (security_log_identifier_keys)

Not additive. Adds the security log (mutable on purpose: rows expire after 90 days) and the identifier keys table,
adds an `identifier_key` column to `users`, and then the application UPDATES every existing user who is not erased
with a new random identifier key. No row is deleted. Stop every process first (step 2). The ledger is not touched.

### Migration 6 (sessions_lockout)

Additive. Adds the tables for sessions, refresh families and lockout counters.

### Migration 5 (users_credentials)

Additive. Adds the tables for users, credentials and one-time setup tokens.

### Migration 4 (content_records)

Additive. Adds the encrypted content table and legal holds. Content is unreadable without the key folder
(`runbooks/backup-restore.md`).

### Migration 3 (approval_requests)

Additive. Adds the approvals table.

### Migration 2 (budget_balances)

Additive. Adds budget balances and reservations. (Migration 11 later rekeys them by organization.)

### Migration 1 (ledger_heads_tasks)

The first schema: the ledger (append-only: updates and deletes are refused by triggers), stream heads and tasks.

## Renamed commands (R1.43)

Each old command below still runs, with the same result and exit code, until 1 March 2027. It prints
one line on stderr: `` `nuramesh <old>` was renamed to `nuramesh <new>`; the old form is removed after
2027-03-01 ``. Scripts and cron entries should move to the new form before that date.

| Old command | New command |
|---|---|
| `nuramesh run` | `nuramesh try` |
| `nuramesh migrate` | `nuramesh db migrate` |
| `nuramesh expire-content` | `nuramesh content expire` |
| `nuramesh scorecard` | `nuramesh scorecards` |
| `nuramesh autonomy grants` (the list) | `nuramesh autonomy grants list` |
| `nuramesh autonomy grant` | `nuramesh autonomy grants create` |
| `nuramesh autonomy revoke` | `nuramesh autonomy grants revoke` |
| `nuramesh autonomy ceiling` | `nuramesh autonomy ceilings` |
| `nuramesh autonomy settings` | `nuramesh autonomy settings set` |
| `nuramesh auth key` | `nuramesh auth factor-key` |
| `nuramesh auth mfa-reset` | `nuramesh auth mfa reset` |
| `nuramesh verify-ledger` | `nuramesh ledger verify` |
| `nuramesh export-ledger` | `nuramesh ledger export` |

The team folder, which many commands took as the first word after the command, is now the option
`--team DIR` (for example `nuramesh content expire --team /data/team`); the organization acted on
by `content shred` and `keys status|reconcile|adopt-legacy` is now `--org`; `stop` and `report`
take the run id as their argument (`nuramesh stop RUN`). The old positional forms are rewritten to the
new ones with the same notice. The Compose `retention` service already runs the new names.

## Renamed routes (R1.43)

Each old route below is served by the same handler until the Sunset date in its `Sunset` header
(1 March 2027); it answers with `Deprecation`, `Sunset`, `Link: <successor>; rel="successor-version"`
and a `rel="deprecation"` link to its section here. The new routes carry none of these.

### Renamed route org-halt (R1.43)

`GET /v1/org/halt` is now `GET /v1/org/status`.

### Renamed route approval-decision (R1.43)

`POST /v1/approvals/{approval_id}/decision` is now `POST /v1/approvals/{approval_id}/approve` or
`.../reject`.

### Renamed route gate-decision (R1.43)

`POST /v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision` is now `.../decisions`.

### Renamed routes catalog-entry-actions (R1.43)

`POST /v1/models/catalog/{edit,approve,pause,retire,remove}` are now
`POST /v1/models/catalog/entries/{edit,approve,pause,retire,remove}`.

### Renamed route catalog-regions (R1.43)

`POST /v1/models/catalog/regions` is now `PUT /v1/models/catalog/regions` (it replaces the set).

### Renamed route model-assignments (R1.43)

`POST /v1/models/assignments` is now `PUT /v1/models/assignments/{scope}/{subject}`.

### The `NuraMesh-Version` header (R1.43)

Every `/v1` response carries `NuraMesh-Version: 2026-12`. A request may send the same header; any
other value is refused with 400 `version_unsupported`. Without it nothing changes.

### List routes page with a cursor (R1.43)

The list routes take `limit` (1 to 200; the ledger routes keep their 500 and the model catalog events their 1000) and an opaque `cursor`. Send `NuraMesh-Version: 2026-12`
and they answer `{"items": [...], "next_cursor": "..." or null}` (`null` is the end). Without the
header a route keeps its named key (`tokens`, `users`, `credentials`, `agents`, `invites`) and
answers with `Deprecation`, `Sunset` and a link to this section; with no `limit` and no `cursor`
the whole list comes back as before, with `limit` or `cursor` the named key holds one page and
`next_cursor` is added. An unreadable cursor, or one of another route, is 400 `cursor_invalid`.

The same holds for the run, ledger, approval, budget, cost-call, model-event and grant lists
(`runs`, `events`, `streams`, `calls`, `rows`; the bare arrays of tasks, task timelines,
approvals and budgets have no name, so they stay bare without `limit` and `cursor` and answer the
`items` form when either is sent). On `runs` it is `limit` and `cursor`; on the ledger, events and
history routes `cursor` replaces `after_seq`, `after` and `before` (both together is 400
`invalid_request`; the old parameter alone works for one more release). A page for an agent
credential holds only the rows page one shows it. `GET /v1/ledger/verify` answers its streams as
`items`, and `GET /v1/autonomy/grants` keeps `items` (no deprecation there).

## Compatibility note: prompt-cache route (before the public launch)

The prompt-caching opt-in route was never released and changed before the launch freeze: `POST /v1/orgs/{org_id}/prompt-cache` is now `PUT /v1/org/prompt-cache` (body `{"enabled": bool}`). The organization is the signed-in caller's own and is never in the path (spec rule R9, OD-9e). There is no alias: the old path answers 404/405. The CLI (`nuramesh org prompt-cache on|off`) is unchanged. The SDK method is `setOrgPromptCache` (was `setPromptCache`; the rule below).

### Contract freeze PR C: the API's cross-cutting promises

- `docs/api.md` now states the `NuraMesh-Version` header, the one page shape, "key on `code`" and the status rule in one section ("Versioning, pages and errors"); nothing changed on the wire for them (built in R1.43 C).
- The published OpenAPI text carries no internal references any more (task ids, decision numbers, release names were in 18 descriptions); `info.x-nuramesh-contract` names the current header value (`2026-12`). `info.version` stays `1`.
- `POST /v1/models/catalog/entries`, `POST /v1/models/catalog/prices` and `POST /v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions` always answered 201 on a new record and 200 on a repeat; the schema said 200 only. The schema now declares both. No runtime change; a client generated from the old schema that checks `=== 200` on a create was already wrong.
- `PUT /v1/models/connections/{provider}/key` and the deprecated `POST .../gates/{gate_id}/decision` also answered 201 (a new key, a new decision) and 200 (a rotate, a repeat) while the schema said 200 only; the schema now declares both. No runtime change.
- The page-size `limit` is 1 to 200 (default 50) everywhere except four routes, now named in `docs/api.md`: the ledger stream and the run ledger (1 to 500, default 100), catalog events (1 to 1000, default 500) and ledger verify (1 to 100). No runtime change.

### SDK method names: one rule

Every method of the TypeScript SDK (`apps/sdk-ts`) is named by one rule, applied before the public launch with no aliases (the SDK is unpublished): the server's OpenAPI `operationId` is **verb-first snake_case**, `<verb>_<noun phrase>`, and the SDK method is its lowerCamelCase (`set_org_prompt_cache` -> `setOrgPromptCache`). The verb is one of a closed list (`accept`, `approve`, `confirm`, `create`, `decide`, `delete`, `disable`, `enable`, `enroll`, `get`, `halt`, `inspect`, `issue`, `list`, `login`, `logout`, `pause`, `quote`, `redeem`, `refresh`, `regenerate`, `reject`, `request`, `resend`, `reset`, `resume`, `retire`, `reveal`, `revoke`, `rotate`, `set`, `start`, `stop`, `test`, `unblock`, `update`, `validate`, `verify`); `get` is one resource or one computed view, `list` a collection (plural noun), `create` a POST that makes a resource, `set` a PUT of the whole value, `update` a change of an existing one, `delete` a DELETE or a `.../remove` action, and an action route takes its own verb (`approveApproval`, `rotateAgentCredential`, `stopRun`). The noun phrase is the resource as a developer says it: singular for one item, plural for a collection, the parent kept where it reads naturally (`listRunTasks`, `getTaskTimeline`, `revokeAllAgentCredentials`). A deprecated twin of a renamed route ends in `Deprecated` and is tagged `@deprecated`. The one source is the table `src/nuramesh/server/operation_ids.py`: FastAPI takes every operation id from it, a route without a row cannot be served, and `tests/test_sdk_names.py` checks the grammar, the generated client and the table below. A new verb is a rule change recorded there and in `docs/sdk-typescript.md`. This rule supersedes the earlier noun-verb id rule (kept routes keep their generated names).

### SDK method names: the rename table

134 operations, 109 renamed (25 unchanged: the bare verbs `login` and `logout`, and names that already followed the rule).

| Route | Old SDK method | New SDK method |
| --- | --- | --- |
| `GET /v1/agent-credentials` | `listAll` | `listAllAgentCredentials` |
| `GET /v1/agents` | `listAgents` | `listAgents` |
| `GET /v1/agents/{agent_id}/credentials` | `listForAgent` | `listAgentCredentials` |
| `POST /v1/agents/{agent_id}/credentials` | `issue` | `issueAgentCredential` |
| `POST /v1/agents/{agent_id}/credentials/revoke-all` | `revokeAll` | `revokeAllAgentCredentials` |
| `POST /v1/agents/{agent_id}/credentials/{credential_id}/revoke` | `revoke` | `revokeAgentCredential` |
| `POST /v1/agents/{agent_id}/credentials/{credential_id}/rotate` | `rotate` | `rotateAgentCredential` |
| `GET /v1/agents/{agent_id}/eligibility` | `eligibility` | `getAgentEligibility` |
| `GET /v1/approvals` | `approvals` | `listApprovals` |
| `GET /v1/approvals/{approval_id}` | `approval` | `getApproval` |
| `POST /v1/approvals/{approval_id}/approve` | `approvalsApprove` | `approveApproval` |
| `POST /v1/approvals/{approval_id}/decision` | `decide` | `decideApprovalDeprecated` |
| `POST /v1/approvals/{approval_id}/reject` | `approvalsReject` | `rejectApproval` |
| `GET /v1/auth/email/status` | `emailStatus` | `getEmailStatus` |
| `POST /v1/auth/email/verify` | `verifyEmailChange` | `verifyEmailChange` |
| `GET /v1/auth/invites` | `listInvites` | `listInvites` |
| `POST /v1/auth/invites` | `createInvite` | `createInvite` |
| `POST /v1/auth/invites/accept` | `acceptInvite` | `acceptInvite` |
| `POST /v1/auth/invites/inspect` | `inspectInvite` | `inspectInvite` |
| `POST /v1/auth/invites/{invite_id}/resend` | `resendInvite` | `resendInvite` |
| `POST /v1/auth/invites/{invite_id}/revoke` | `revokeInvite` | `revokeInvite` |
| `POST /v1/auth/login` | `login` | `login` |
| `POST /v1/auth/login/mfa` | `loginMfa` | `loginWithMfa` |
| `POST /v1/auth/login/mfa/enroll` | `loginMfaEnroll` | `enrollMfaAtLogin` |
| `POST /v1/auth/login/mfa/enroll/confirm` | `loginMfaConfirm` | `confirmMfaEnrollmentAtLogin` |
| `POST /v1/auth/logout` | `logout` | `logout` |
| `POST /v1/auth/me/email` | `startEmailChange` | `startEmailChange` |
| `GET /v1/auth/mfa` | `status` | `getMfaStatus` |
| `POST /v1/auth/mfa/disable` | `disable` | `disableMfa` |
| `POST /v1/auth/mfa/enroll` | `enroll` | `enrollMfa` |
| `POST /v1/auth/mfa/enroll/confirm` | `confirm` | `confirmMfaEnrollment` |
| `POST /v1/auth/mfa/recovery-codes` | `regenerate` | `regenerateMfaRecoveryCodes` |
| `POST /v1/auth/password-reset/redeem` | `redeemReset` | `redeemPasswordReset` |
| `POST /v1/auth/password-reset/request` | `requestReset` | `requestPasswordReset` |
| `POST /v1/auth/password/confirm` | `-` | `confirmPassword` |
| `POST /v1/auth/refresh` | `refresh` | `refreshSession` |
| `GET /v1/auth/session` | `whoami` | `getSession` |
| `GET /v1/auth/settings` | `settings` | `getAuthSettings` |
| `PUT /v1/auth/settings/mfa` | `setPolicy` | `setMfaPolicy` |
| `POST /v1/auth/setup/redeem` | `redeem` | `redeemSetupToken` |
| `GET /v1/auth/setup/status` | `setupStatus` | `getSetupStatus` |
| `GET /v1/auth/tokens` | `listTokens` | `listTokens` |
| `POST /v1/auth/tokens` | `createToken` | `createToken` |
| `POST /v1/auth/tokens/revoke-by-link` | `revokeByLink` | `revokeTokensByLink` |
| `POST /v1/auth/tokens/{token_id}/revoke` | `revokeToken` | `revokeToken` |
| `GET /v1/auth/users` | `listUsers` | `listUsers` |
| `GET /v1/auth/users/{user_id}/agent-credentials` | `issuedCredentials` | `listUserAgentCredentials` |
| `POST /v1/auth/users/{user_id}/disable` | `disableUser` | `disableUser` |
| `POST /v1/auth/users/{user_id}/enable` | `enableUser` | `enableUser` |
| `POST /v1/auth/users/{user_id}/mfa/reset` | `reset` | `resetUserMfa` |
| `POST /v1/auth/users/{user_id}/password-reset` | `ownerReset` | `resetUserPassword` |
| `PUT /v1/auth/users/{user_id}/role` | `setRole` | `setUserRole` |
| `PUT /v1/autonomy/ceilings/{role}/{kind}` | `putCeiling` | `setAutonomyCeiling` |
| `GET /v1/autonomy/grants` | `getGrants` | `listAutonomyGrants` |
| `POST /v1/autonomy/grants` | `postGrant` | `createAutonomyGrant` |
| `POST /v1/autonomy/grants/{grant_id}/revoke` | `postRevoke` | `revokeAutonomyGrant` |
| `GET /v1/autonomy/proposals` | `getProposals` | `listAutonomyProposals` |
| `PUT /v1/autonomy/settings/{name}` | `putSetting` | `setAutonomySetting` |
| `GET /v1/budgets` | `budgets` | `listBudgets` |
| `GET /v1/budgets/{budget_id}` | `budget` | `getBudget` |
| `POST /v1/budgets/{budget_id}/unblock` | `unblock` | `unblockBudget` |
| `POST /v1/config/validate` | `validate` | `validateConfig` |
| `GET /v1/content/{org_id}/{content_hash}` | `revealContent` | `revealContent` |
| `GET /v1/costs/calls` | `callsList` | `listModelCalls` |
| `GET /v1/costs/per-result` | `perResult` | `getCostPerResult` |
| `POST /v1/costs/quote` | `quoteFor` | `quoteCost` |
| `GET /v1/costs/spend` | `spend` | `getSpend` |
| `GET /v1/dashboards/approvals` | `dashboardApprovals` | `getApprovalsDashboard` |
| `GET /v1/dashboards/cost-latency` | `dashboardCostLatency` | `getCostLatencyDashboard` |
| `GET /v1/dashboards/verification` | `dashboardVerification` | `getVerificationDashboard` |
| `GET /v1/dashboards/verifiers` | `dashboardVerifiers` | `getVerifiersDashboard` |
| `GET /v1/health` | `health` | `getHealth` |
| `GET /v1/ledger` | `ledgerStreams` | `listLedgerStreams` |
| `GET /v1/ledger/verify` | `verifyAll` | `verifyLedger` |
| `GET /v1/ledger/{stream}` | `ledger` | `listLedgerEvents` |
| `GET /v1/ledger/{stream}/verify` | `verify` | `verifyLedgerStream` |
| `GET /v1/models/assignments` | `getAssignments` | `listModelAssignments` |
| `POST /v1/models/assignments` | `assign` | `setModelAssignmentDeprecated` |
| `PUT /v1/models/assignments/{scope}/{subject}` | `modelsAssignmentsSet` | `setModelAssignment` |
| `GET /v1/models/catalog` | `getCatalog` | `getModelCatalog` |
| `POST /v1/models/catalog/approve` | `approveEntry` | `approveCatalogEntryDeprecated` |
| `POST /v1/models/catalog/edit` | `editEntry` | `updateCatalogEntryDeprecated` |
| `POST /v1/models/catalog/entries` | `addEntry` | `createCatalogEntry` |
| `POST /v1/models/catalog/entries/approve` | `modelsCatalogEntriesApprove` | `approveCatalogEntry` |
| `POST /v1/models/catalog/entries/edit` | `modelsCatalogEntriesEdit` | `updateCatalogEntry` |
| `POST /v1/models/catalog/entries/pause` | `modelsCatalogEntriesPause` | `pauseCatalogEntry` |
| `POST /v1/models/catalog/entries/remove` | `modelsCatalogEntriesRemove` | `deleteCatalogEntry` |
| `POST /v1/models/catalog/entries/retire` | `modelsCatalogEntriesRetire` | `retireCatalogEntry` |
| `GET /v1/models/catalog/events` | `getCatalogEvents` | `listCatalogEvents` |
| `POST /v1/models/catalog/pause` | `pauseEntry` | `pauseCatalogEntryDeprecated` |
| `POST /v1/models/catalog/prices` | `addPrice` | `createCatalogPrice` |
| `POST /v1/models/catalog/regions` | `setRegions` | `setCatalogRegionsDeprecated` |
| `PUT /v1/models/catalog/regions` | `modelsCatalogRegionsSet` | `setCatalogRegions` |
| `POST /v1/models/catalog/remove` | `removeEntry` | `deleteCatalogEntryDeprecated` |
| `POST /v1/models/catalog/retire` | `retireEntry` | `retireCatalogEntryDeprecated` |
| `GET /v1/models/connections` | `listConnections` | `listModelConnections` |
| `GET /v1/models/connections/events` | `getConnectionEvents` | `listConnectionEvents` |
| `PUT /v1/models/connections/{provider}` | `putConnection` | `setModelConnection` |
| `DELETE /v1/models/connections/{provider}/key` | `deleteKey` | `deleteModelConnectionKey` |
| `GET /v1/models/connections/{provider}/key` | `getKey` | `getModelConnectionKey` |
| `PUT /v1/models/connections/{provider}/key` | `putKey` | `setModelConnectionKey` |
| `POST /v1/models/connections/{provider}/remove` | `removeConnection` | `deleteModelConnection` |
| `POST /v1/models/connections/{provider}/test` | `testConnection` | `testModelConnection` |
| `GET /v1/models/keys/health` | `keysHealth` | `getModelKeysHealth` |
| `GET /v1/models/resolution` | `getResolution` | `getModelResolution` |
| `GET /v1/org/breakers` | `breakersStatus` | `listOrgBreakers` |
| `POST /v1/org/breakers/{name}/disable` | `disableOrgBreaker` | `disableOrgBreaker` |
| `POST /v1/org/breakers/{name}/enable` | `enableOrgBreaker` | `enableOrgBreaker` |
| `POST /v1/org/breakers/{name}/reset` | `resetOrgBreaker` | `resetOrgBreaker` |
| `GET /v1/org/halt` | `haltStatus` | `getOrgHaltDeprecated` |
| `POST /v1/org/halt` | `haltOrganization` | `haltOrg` |
| `PUT /v1/org/prompt-cache` | `setPromptCache` | `setOrgPromptCache` |
| `POST /v1/org/resume` | `resumeOrganization` | `resumeOrg` |
| `POST /v1/org/roles/{role}/pause` | `pauseOrgRole` | `pauseOrgRole` |
| `POST /v1/org/roles/{role}/resume` | `resumeOrgRole` | `resumeOrgRole` |
| `GET /v1/org/status` | `orgStatus` | `getOrgStatus` |
| `GET /v1/reports/downgrade` | `downgradeReport` | `getDowngradeReport` |
| `GET /v1/runs` | `runs` | `listRuns` |
| `POST /v1/runs` | `startRunRoute` | `startRun` |
| `GET /v1/runs/{run_id}` | `runDetail` | `getRun` |
| `GET /v1/runs/{run_id}/ledger` | `runLedger` | `listRunLedgerEvents` |
| `GET /v1/runs/{run_id}/ledger/verify` | `runVerify` | `verifyRunLedger` |
| `GET /v1/runs/{run_id}/report` | `report` | `getRunReport` |
| `GET /v1/runs/{run_id}/stop` | `stopStatus` | `getRunStopStatus` |
| `POST /v1/runs/{run_id}/stop` | `stopRun` | `stopRun` |
| `GET /v1/runs/{run_id}/tasks` | `tasks` | `listRunTasks` |
| `GET /v1/runs/{run_id}/tasks/{task_id}/audit` | `taskAudit` | `getTaskAudit` |
| `POST /v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision` | `decideGate` | `decideGateDeprecated` |
| `POST /v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions` | `runsTasksGatesDecisionsCreate` | `createGateDecision` |
| `GET /v1/runs/{run_id}/tasks/{task_id}/timeline` | `timeline` | `getTaskTimeline` |
| `GET /v1/savings/tokens` | `tokens` | `getTokenSavings` |
| `GET /v1/schema/config` | `schema` | `getConfigSchema` |
| `GET /v1/scorecards` | `getScorecards` | `listScorecards` |
| `GET /v1/verifiers/{agent_id}/calibration` | `verifierCalibration` | `getVerifierCalibration` |


## Other behaviour changes

### Key files: links refused, legacy adoption is crash-safe (R1.30 review-2)

No schema change. Behaviour changes for key files on disk:

- An anchor private key (`load_private_key`) that is a symbolic link, is not a regular file, is not
  owned by the current user, or is readable by others is now refused. If you keep the key behind a
  symlink, point the setting at the real file (mode 600).
- A content key file that is a link is refused the same way.
- `keys adopt-legacy` moves a flat `<org>.key` into the store folder with a hard link, then unlinks
  the flat name. One adoption runs at a time (a lock file `.adopt.lock` in the key directory). If the
  process stops between the two steps, rerun it: the same store finishes the move.
- The command refuses, and moves nothing, when: the flat key has other hard links ("other hard
  links": a backup made with `ln`, or a store whose adoption never finished); the store folder
  already holds a key for that organization ("already holds a key"); the key is shared by more
  than two names, or changed or was linked by another store while being moved ("shared" or
  "changed"); it is not owned by you or is readable by others. Fix: remove the extra hard link, or
  move the flat key into the store folder by hand (mode 600).
- The adoption lock is an `flock`. If the lock file is a symlink or the filesystem has no locks (some
  NFS mounts) the command fails with a clear message and moves nothing; move the key by hand. On a
  non-POSIX system (Windows) no lock is taken (the hard-link and one-inode checks still apply), so run
  one adoption at a time there.
- If the filesystem has no hard links the command fails with a clear message; copy the key into the
  store folder by hand (mode 600) and remove the flat file.

### CLI store selection and `verify-ledger` on an empty ledger (fix/cli-store-selection)

No schema change. Behaviour changes for scripts that call the CLI:

- `report`, `export-ledger`, `verify-ledger`, `anchor export`, `eval run`, `eval status` and
  `validate` now use `NURAMESH_DATABASE_URL` when it is set, as `serve` does. Before, they read the
  team's SQLite file even when the server used PostgreSQL. Unset the variable (or pass nothing) to
  keep reading SQLite.
- `verify-ledger` now exits 1 with "no streams" on an empty ledger (before: exit 0 and "0 streams,
  0 broken"). A script that checks a ledger that may be empty must pass `--allow-empty`.
- A read-only command on a missing SQLite file, or on an empty PostgreSQL database, exits 2 and
  creates nothing.

### Schema 11: budgets are keyed by organization

Applies to SQLite and PostgreSQL stores that already hold budgets. Approved 2026-10-26.

**Before you upgrade**

1. **Back up the database** (copy the SQLite file, or `pg_dump`). The migration rewrites data and
   the way back is to restore this backup: the old code refuses a newer schema.
2. **Stop ALL processes that use the database** (servers, workers, the CLI, schedulers) while still
   on the old version, then start the new version. Never run old and new versions against the
   same database during the migration (no rolling deploy, no mixed versions).

**What happens**

- Migration 11 is NOT additive. It drops and rebuilds `budget_balances` (primary key
  `(org_id, id)`) and rewrites EVERY `reservations` row, open and closed, in one transaction (all
  or nothing; a row count check stops it if anything is lost). Every amount and version is kept.
- The ledger is not touched. Ledger rows, hashes and anchors are unchanged, and
  `nuramesh ledger verify` and anchor verification give the same answers before and after
  (`tests/stores/test_budget_org_migration_ledger.py`).
- The owner of a budget is the organization of its earliest `budget.defined` entry. A run-scoped
  budget with no such entry belongs to its run's organization (from `run.started`). A budget with
  neither is unowned: no call matches it (a call is refused with `no_budget`), it stays readable by
  the local inspector.
- The migration refuses to run (nothing changes) if a reservation still OPEN holds money in
  budgets of more than one organization. Settle or release it with the old version first.

**Known limit (for later):** an old-version process that reads budgets by bare id during or after
the migration could read another organization's row. That is why step 2 is required. A safe
rolling-deploy path is not built.

**Roll back:** stop everything, restore the backup, start the old version.
