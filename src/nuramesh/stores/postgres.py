"""State and ledger in PostgreSQL (R1.19a, T35; install with `nuramesh[postgres]`).

The same contract as the other stores (`tests/stores/test_store_contract.py` runs on all of them):
one transaction per state change and its ledger event (A1), versioned rows with optimistic
concurrency, an append-only ledger enforced by triggers, hash chains built in Python
(`core.ledger.next_event`) so a ledger is identical across stores and can be re-verified anywhere.

- **Writes are serialized** with one transaction-level advisory lock (`pg_advisory_xact_lock`),
  exactly as SQLite serializes writers: simple, correct across processes (API, workers), and fast
  enough for R1. Per-stream locks are the scale path (`docs/architecture-platform.md`).
- **Reads** outside a unit of work see committed data (read committed); `snapshot` uses one
  `REPEATABLE READ` read-only transaction so a stream's events and head are consistent.
- **Live events:** every appended event runs `pg_notify('nuramesh_ledger', '<stream>:<seq>')`,
  delivered on commit; consumers (the WebSocket layer, R1.21) re-read the table, so a missed
  notification only delays an update.
- **Migrations** are plain SQL scripts applied under the same advisory lock; a database from a
  newer version is refused. Payload and state columns are `TEXT` holding the exact canonical JSON
  (so hashes never depend on how Postgres would normalize JSON). Tenancy (`org_id`) is added when
  multi-tenancy is built (R3.02): adding a column with a constant default is a metadata-only
  change in PostgreSQL, so nothing is lost by waiting.
- One connection per store, used under a lock, so a store is safe to share between threads; use
  one store per process. A connection lost **between** operations is re-opened automatically; a
  connection lost **during COMMIT** leaves the outcome unknown (the transaction may have
  committed): the error says so, and callers must check before retrying (appends aren't
  idempotent).
- **Append-only** is enforced by triggers against ordinary sessions. The table owner or a
  superuser can still disable triggers or change `session_replication_role`, and `stream_heads`
  has no triggers: the API (R1.19b) should connect as a role that owns nothing and may only
  INSERT/SELECT (and UPDATE `stream_heads`), and the anchors (R1.04a, T33) are what prove history.
- Database errors raised while connecting never include the connection string (it holds the
  password).
"""

import hashlib
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from pydantic import ValidationError

from nuramesh.core.auth import (
    AGENT_CREDENTIAL_ENDED_RETENTION,
    ERASED_ACTOR,
    ERASED_DETAIL_KEYS,
    HALT_PENDING_REASON,
    MAX_COUNT,
    AgentCredential,
    ApiToken,
    AuthFailure,
    AuthSession,
    AuthToken,
    Credential,
    Role,
    SecurityLogEntry,
    SetupToken,
    TokenKind,
    TokenScope,
    User,
    new_identifier_key,
    normalize_email,
)
from nuramesh.core.budgets import BudgetBalance, Reservation, ReservationStatus, org_key
from nuramesh.core.canonical import canonical_json, thaw
from nuramesh.core.content import ContentKind, ContentRecord, KeyTombstone
from nuramesh.core.email_change import EMAIL_CHANGE_RETENTION, EmailChange
from nuramesh.core.idempotency import Scope, Sealed, State
from nuramesh.core.identity import ERASED_CREATOR, INVITE_RETENTION, Invite
from nuramesh.core.ledger import EventType, LedgerEvent, format_time
from nuramesh.core.mfa import (
    MFA_CHALLENGE_KEEP,
    MFA_PENDING_LIFETIME,
    MfaChallenge,
    MfaFactor,
    OrgAuthSettings,
)
from nuramesh.core.money import Money
from nuramesh.core.org import STARTUP_STREAM_ID
from nuramesh.core.password_reset import (
    RESET_RETENTION,
    PasswordLoginBlock,
    ResetToken,
    RevokeLink,
)
from nuramesh.core.policy import Approval, ApprovalStatus
from nuramesh.core.projection import ALL_KINDS, RUN_STARTED, projection_key
from nuramesh.core.tasks import Task
from nuramesh.stores.base import (
    AlreadyExistsError,
    BadTimeError,
    Clock,
    ConcurrencyError,
    ContentSubjectRow,
    EventDraft,
    ExtensionRecord,
    NestedTransactionError,
    NotFoundError,
    ProjectionStreams,
    SchemaOutOfDate,
    Store,
    StoreBusyError,
    StoredApproval,
    StoredBalance,
    StoredIdempotency,
    StoredReservation,
    StoredTask,
    StoreError,
    StreamHead,
    SubjectBackfill,
    UnitOfWork,
    check_limit,
    clamped_expiry,
    derive_subjects,
    due_anchor_day,
    hash_org_pattern,
    keyed_ids_of,
    merged_expiry,
    projection_streams,
    run_stream,
    utc_now,
    verify_projected,
)
from nuramesh.stores.budget_org import (
    MigrationError,
    balance_row,
    reservation_data,
    resolve_owners,
)
from nuramesh.stores.run_queue import COLUMNS as RQ_COLUMNS
from nuramesh.stores.run_queue import KEYS as RQ_KEYS
from nuramesh.stores.tombstones import backfill_records, backfill_shreds, bumped, shred_entries

WRITE_LOCK = 7_283_410_001  # one advisory lock key for all write transactions
NOTIFY_CHANNEL = "nuramesh_ledger"

# Migrations that update, move or drop existing rows or columns (issue 897, review item m3). One
# source: `db migrate --plan` and the acknowledgement rule read it, and the sentence in
# docs/upgrade-notes.md is checked against it. A new non-additive migration adds its number here in
# the same change as its "Not additive" note.
NON_ADDITIVE_MIGRATIONS: frozenset[int] = frozenset({7, 8, 11, 15, 22, 24, 27})

MIGRATIONS: tuple[str, ...] = (
    # 1 ledger_heads_tasks: ledger, stream heads, tasks
    """
    CREATE TABLE ledger_events (
        stream TEXT NOT NULL,
        seq BIGINT NOT NULL CHECK (seq >= 1),
        type TEXT NOT NULL,
        actor TEXT NOT NULL,
        at TEXT NOT NULL,
        run_id TEXT,
        task_id TEXT,
        payload TEXT NOT NULL,
        cost_amount TEXT,
        cost_currency TEXT,
        cost_basis TEXT,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL,
        PRIMARY KEY (stream, seq)
    );
    CREATE INDEX ledger_events_task ON ledger_events (task_id);
    CREATE INDEX ledger_events_run ON ledger_events (run_id);
    CREATE FUNCTION ledger_append_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'ledger is append-only';
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER ledger_events_no_update BEFORE UPDATE ON ledger_events
        FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
    CREATE TRIGGER ledger_events_no_delete BEFORE DELETE ON ledger_events
        FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
    CREATE TRIGGER ledger_events_no_truncate BEFORE TRUNCATE ON ledger_events
        FOR EACH STATEMENT EXECUTE FUNCTION ledger_append_only();
    CREATE TABLE stream_heads (
        stream TEXT PRIMARY KEY,
        seq BIGINT NOT NULL,
        hash TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL
    );
    CREATE INDEX tasks_run ON tasks (run_id);
    """,
    # 2 budget_balances: budget balances and reservations
    """
    CREATE TABLE budget_balances (
        id TEXT PRIMARY KEY,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL
    );
    CREATE TABLE reservations (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL
    );
    CREATE INDEX reservations_run_status ON reservations (run_id, status);
    """,
    # 3 approval_requests: approval requests
    """
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL
    );
    CREATE INDEX approvals_run_agent_status ON approvals (run_id, agent_id, status);
    """,
    # 4 content_records: content (ND25, T46): encrypted, expiring records keyed by their keyed hash,
    # and legal holds
    """
    CREATE TABLE content (
        org_id TEXT NOT NULL,
        id TEXT NOT NULL,
        kind TEXT NOT NULL,
        key_id TEXT NOT NULL,
        nonce BYTEA NOT NULL,
        ciphertext BYTEA NOT NULL,
        size INTEGER NOT NULL CHECK (size >= 0),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        PRIMARY KEY (org_id, id)
    );
    CREATE INDEX content_expiry ON content (expires_at);
    CREATE TABLE content_holds (
        org_id TEXT PRIMARY KEY
    );
    """,
    # 5 users_credentials: users, credentials and one-time setup tokens (R1.22, T64): additive. The
    # user row keeps the personal data apart from the opaque id; erasing clears it and keeps the
    # row.
    """
    CREATE TABLE users (
        org_id TEXT NOT NULL,
        id TEXT NOT NULL,
        email TEXT,
        display_name TEXT,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        erased_at TEXT,
        PRIMARY KEY (org_id, id),
        CHECK ((erased_at IS NULL) = (email IS NOT NULL)),
        CHECK (erased_at IS NULL OR display_name IS NULL)
    );
    CREATE UNIQUE INDEX users_email ON users (org_id, email);
    CREATE TABLE credentials (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (org_id, user_id)
    );
    CREATE TABLE setup_tokens (
        org_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        PRIMARY KEY (org_id, token_hash)
    );
    """,
    # 6 sessions_lockout: sessions, refresh families and lockout counters (R1.22 PR 2, T66):
    # additive. Only hashes of tokens are stored; failure keys are hashes too. Ended sessions are
    # purged after 90 days.
    """
    CREATE TABLE auth_sessions (
        org_id TEXT NOT NULL,
        id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        absolute_expires_at TEXT NOT NULL,
        revoked_at TEXT,
        revoke_reason TEXT,
        PRIMARY KEY (org_id, id)
    );
    CREATE TABLE auth_tokens (
        org_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        session_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        PRIMARY KEY (org_id, token_hash)
    );
    CREATE INDEX auth_tokens_session ON auth_tokens (org_id, session_id);
    CREATE TABLE auth_failures (
        key TEXT PRIMARY KEY,
        count INTEGER NOT NULL CHECK (count >= 1),
        last_failure_at TEXT NOT NULL,
        locked_until TEXT
    );
    """,
    # 7 security_log_identifier_keys: the security log and the identifier keys (ND60). The log holds
    # the raw details of security events (an email as typed, a client address, a user agent) apart
    # from the ledger; mutable on purpose: rows expire 90 days after they are written and go with
    # their user. Each user gets an identifier key (32 random bytes, deleted on erasure); each
    # organization one key per 90-day epoch, destroyed (the bytes deleted, a tombstone kept) when
    # the epoch ends. Additive.
    """
    CREATE TABLE security_log (
        org_id TEXT NOT NULL,
        id TEXT NOT NULL,
        at TEXT NOT NULL,
        type TEXT NOT NULL,
        actor TEXT,
        details TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (org_id, id)
    );
    CREATE INDEX security_log_expiry ON security_log (expires_at);
    CREATE INDEX security_log_actor ON security_log (org_id, actor);
    ALTER TABLE users ADD COLUMN identifier_key BYTEA;
    CREATE TABLE identifier_keys (
        org_id TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        key BYTEA,
        created_at TEXT NOT NULL,
        destroyed_at TEXT,
        PRIMARY KEY (org_id, epoch),
        CHECK ((key IS NULL) = (destroyed_at IS NOT NULL))
    );
    """,
    # 8 security_log_subject: the subject of a security-log row (ND60): the opaque
    # id of the user the row concerns, apart from the actor, so erasure can delete every row about a
    # user, system-actor rows included. Additive; existing rows are filled in the application
    # (`derive_subjects`).
    """
    ALTER TABLE security_log ADD COLUMN subject TEXT;
    CREATE INDEX security_log_subject ON security_log (org_id, subject);
    """,
    # 9 api_tokens: API tokens (R1.22 PR 3): additive. Only the SHA-256 of each secret is stored; a
    # name is unique in its organization for ever (revoked tokens keep theirs), so it addresses one
    # token. Since T85 the ledger names a token by its id (`service:<id>`), never by this name,
    # which is free text kept only here. Records are kept: they are what the ledger's actors refer
    # to.
    """
    CREATE TABLE api_tokens (
        org_id TEXT NOT NULL,
        id TEXT NOT NULL,
        name TEXT NOT NULL,
        scopes TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        revoked_by TEXT,
        PRIMARY KEY (org_id, id),
        UNIQUE (org_id, name),
        UNIQUE (org_id, token_hash)
    );
    """,
    # 10 budget_defined_index: an index of the `budget.defined` entries (T84, spec/auth/org-scope.md
    # R8): who owns a budget is read from these alone, never by walking organization streams that
    # anyone can grow (failed logins). Additive: an index only; no row changes.
    """
    CREATE INDEX ledger_events_budget_defined ON ledger_events (stream, seq)
        WHERE type = 'budget.defined';
    """,
    # 11 budgets_by_org: budgets are keyed by (organization, id) (R3.02a, ND90). This statement only
    # creates the new table; `_key_budgets_by_organization` copies every row into it, checks the
    # count, then drops the old table and renames this one (all in the migration's transaction).
    """
    CREATE TABLE budget_balances_org (
        org_id TEXT NOT NULL,
        id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL,
        PRIMARY KEY (org_id, id)
    );
    """,
    # 12 key_tombstone: the key tombstone and the store identity (R1.37, T120 to T130). A destroyed
    # organization key is never recreated: `key_tombstones` is permanent (no UPDATE, DELETE or
    # TRUNCATE) and `store_identity` names the store (the key folder of this database). The
    # application step `_tombstone_and_identify` makes the identity and backfills tombstones from
    # the ledger's `content_shred` entries. Additive.
    """
    CREATE OR REPLACE FUNCTION permanent_row() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'this table is permanent: rows are never changed or removed';
    END;
    $$ LANGUAGE plpgsql;
    CREATE TABLE store_identity (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        store_id TEXT NOT NULL CHECK (store_id ~ '^[0-9a-f]{32}$')
    );
    CREATE TRIGGER store_identity_no_update BEFORE UPDATE ON store_identity
        FOR EACH ROW EXECUTE FUNCTION permanent_row();
    CREATE TRIGGER store_identity_no_delete BEFORE DELETE ON store_identity
        FOR EACH ROW EXECUTE FUNCTION permanent_row();
    CREATE TRIGGER store_identity_no_truncate BEFORE TRUNCATE ON store_identity
        FOR EACH STATEMENT EXECUTE FUNCTION permanent_row();
    CREATE TABLE key_tombstones (
        org_id TEXT PRIMARY KEY,
        destroyed_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL CHECK (reason IN ('shred', 'erasure', 'backfilled', 'reconciled')),
        key_id TEXT CHECK (key_id IS NULL OR key_id ~ '^[0-9a-f]{16}$'),
        backfilled BOOLEAN NOT NULL,
        version INTEGER NOT NULL
    );
    CREATE TRIGGER key_tombstones_no_update BEFORE UPDATE ON key_tombstones
        FOR EACH ROW EXECUTE FUNCTION permanent_row();
    CREATE TRIGGER key_tombstones_no_delete BEFORE DELETE ON key_tombstones
        FOR EACH ROW EXECUTE FUNCTION permanent_row();
    CREATE TRIGGER key_tombstones_no_truncate BEFORE TRUNCATE ON key_tombstones
        FOR EACH STATEMENT EXECUTE FUNCTION permanent_row();
    """,
    # 13 agent_credentials: agent credentials (E2, T93, T140), additive. Only the
    # SHA-256 of each secret is stored; no name or free-text column exists (spec/agent-identity
    # 2.2). `team` is the server's team key.
    """
    CREATE TABLE agent_credentials (
        org_id TEXT NOT NULL,
        id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        team TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        scopes TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        revoked_by TEXT,
        PRIMARY KEY (org_id, id),
        UNIQUE (org_id, token_hash)
    );
    CREATE INDEX agent_credentials_agent ON agent_credentials (org_id, agent_id);
    """,
    # 14 idempotency_keys: R1.35, the idempotency table. A mutable, expiring table, never part of
    # the ledger.
    """
    CREATE TABLE idempotency_keys (
        org_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        method TEXT NOT NULL,
        route TEXT NOT NULL,
        key_hash TEXT NOT NULL CHECK (length(key_hash) = 64),
        fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
        state TEXT NOT NULL CHECK (state IN ('in_progress', 'done')),
        attempt INTEGER NOT NULL CHECK (attempt >= 1),
        fence TEXT CHECK (fence IS NULL OR length(fence) = 32),
        subject TEXT,
        created_at TEXT NOT NULL,
        lease_until TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status INTEGER CHECK (status IS NULL OR (status >= 200 AND status <= 299)),
        payload BYTEA CHECK (payload IS NULL OR octet_length(payload) <= 263188),
        nonce BYTEA CHECK (nonce IS NULL OR octet_length(nonce) = 12),
        key_id TEXT CHECK (key_id IS NULL OR length(key_id) = 16),
        PRIMARY KEY (org_id, actor, method, route, key_hash),
        CHECK ((state = 'in_progress') = (fence IS NOT NULL)),
        CHECK ((state = 'done') = (status IS NOT NULL AND payload IS NOT NULL
            AND nonce IS NOT NULL AND key_id IS NOT NULL))
    );
    CREATE INDEX idempotency_keys_expires ON idempotency_keys (expires_at);
    CREATE INDEX idempotency_keys_actor ON idempotency_keys (org_id, actor);
    CREATE INDEX idempotency_keys_subject ON idempotency_keys (org_id, subject)
        WHERE subject IS NOT NULL;
    """,
    # 15 idempotency_subject_set: R1.35 (ND148), the people a stored idempotent answer names, one
    # row per person; erasing any of them purges the answer (one indexed query). The old single
    # `subject` column moves into the set and goes. Additive for the rows, destructive only for
    # the column.
    """
    CREATE TABLE idempotency_subjects (
        org_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        method TEXT NOT NULL,
        route TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        subject TEXT NOT NULL,
        PRIMARY KEY (org_id, actor, method, route, key_hash, subject),
        FOREIGN KEY (org_id, actor, method, route, key_hash)
            REFERENCES idempotency_keys (org_id, actor, method, route, key_hash)
            ON DELETE CASCADE
    );
    CREATE INDEX idempotency_subjects_person ON idempotency_subjects (org_id, subject);
    INSERT INTO idempotency_subjects (org_id, actor, method, route, key_hash, subject)
        SELECT org_id, actor, method, route, key_hash, subject FROM idempotency_keys
        WHERE subject IS NOT NULL;
    DROP INDEX idempotency_keys_subject;
    ALTER TABLE idempotency_keys DROP COLUMN subject;
    """,
    # 16 ledger_commit_pos: the commit position (E13, T135): one increasing number across
    # streams, assigned inside the write transaction. A row from before this migration has
    # NULL and reads as 0 (it was
    # committed before any grant). Not part of any hash.
    """
    ALTER TABLE ledger_events ADD COLUMN IF NOT EXISTS commit_pos BIGINT;
    """,
    # 17 ledger_commit_pos_index: the index that makes "the latest position" and "after
    # position N" cheap, and that refuses a duplicate position (the writers are serialized,
    # so a duplicate is a bug).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ledger_events_commit_pos ON ledger_events (commit_pos)
        WHERE commit_pos IS NOT NULL;
    """,
    # 18 autonomy_projection: the autonomy projection (T136): one row per ledger entry whose
    # kind the earned-autonomy reads look at, written in the same transaction as the entry,
    # rebuildable from the ledger
    # (the application backfills it here). `ledger_projection_counts` is kept apart so a row that
    # goes missing or appears is detected on every read. `org` is set on `run.started` rows only.
    """
    CREATE TABLE IF NOT EXISTS ledger_projection (
        stream TEXT NOT NULL,
        kind TEXT NOT NULL,
        seq BIGINT NOT NULL CHECK (seq >= 1),
        org TEXT,
        PRIMARY KEY (stream, kind, seq)
    );
    CREATE INDEX IF NOT EXISTS ledger_projection_runs ON ledger_projection (org, stream)
        WHERE kind = 'run.started';
    CREATE TABLE IF NOT EXISTS ledger_projection_counts (
        stream TEXT NOT NULL,
        kind TEXT NOT NULL,
        n BIGINT NOT NULL CHECK (n >= 0),
        PRIMARY KEY (stream, kind)
    );
    """,
    # 19 ledger_at_check: R1.36 (ND233), defence in depth for every writer: the `at` of
    # `ledger_events` and `stream_heads` must be a real time in the ledger's text form
    # (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). The CHECK is added NOT VALID: it guards every new or
    # changed row; no existing row is read, rewritten or deleted. The final step then tries to
    # VALIDATE (so a clean database ends fully validated); if a bad row exists the validation is
    # skipped with a NOTICE and the row stays, flagged by the readers (they skip and report it).
    """
    CREATE OR REPLACE FUNCTION nuramesh_valid_at(t text) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog AS $f$
    BEGIN
        RETURN t ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]{6}Z$'
            AND to_char(substr(t, 1, 26)::timestamp, 'YYYY-MM-DD"T"HH24:MI:SS.US')
                = substr(t, 1, 26);
    EXCEPTION WHEN others THEN
        RETURN false;
    END
    $f$;
    ALTER TABLE ledger_events DROP CONSTRAINT IF EXISTS ledger_events_at_valid;
    ALTER TABLE ledger_events
        ADD CONSTRAINT ledger_events_at_valid CHECK (nuramesh_valid_at(at)) NOT VALID;
    ALTER TABLE stream_heads DROP CONSTRAINT IF EXISTS stream_heads_at_valid;
    ALTER TABLE stream_heads
        ADD CONSTRAINT stream_heads_at_valid CHECK (nuramesh_valid_at(at)) NOT VALID;
    DO $v$
    BEGIN
        ALTER TABLE ledger_events VALIDATE CONSTRAINT ledger_events_at_valid;
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ledger_events holds a row with an impossible time; left in place';
    END
    $v$;
    DO $v$
    BEGIN
        ALTER TABLE stream_heads VALIDATE CONSTRAINT stream_heads_at_valid;
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'stream_heads holds a row with an impossible time; left in place';
    END
    $v$;
    """,
    # 20 user_disabled: E1, an owner may disable a person (spec/e1 sections 9.1 and 12). One
    # nullable column, additive: no existing row is rewritten.
    """
    ALTER TABLE users ADD COLUMN disabled_at TEXT;
    """,
    # 21 content_subjects: R1.32 (spec/r132 S3, ND79, ND157): who a stored content row
    # names, so erasing a person finds the row. Additive: no existing row is read, changed or
    # deleted; existing content rows get no subject rows. Branches that add later migrations
    # (E8, the approvals sweep, R1.22c, the cost reader) renumber after this one (ND126).
    """
    CREATE TABLE content_subjects (
        org_id TEXT NOT NULL,
        content_id TEXT NOT NULL,
        subject TEXT NOT NULL,
        run_id TEXT,
        erase_pending BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (org_id, content_id, subject)
    );
    CREATE INDEX content_subjects_subject ON content_subjects (org_id, subject);
    CREATE INDEX content_subjects_run ON content_subjects (org_id, run_id);
    """,
    # 22 tombstone_approx_reason: T258 (ND233, ND277). Widens the `key_tombstones.reason` CHECK
    # with `backfilled_approximate` (a backfilled tombstone whose date is not the ledger's).
    # Migration 12 is released and never edited (owner rule), so this one drops and re-adds the
    # constraint; the application step `_backfill_approximate_tombstones` then adds the
    # tombstones migration 12 had to leave out. Number: the next after 21 on main.
    """
    ALTER TABLE key_tombstones DROP CONSTRAINT key_tombstones_reason_check;
    ALTER TABLE key_tombstones ADD CONSTRAINT key_tombstones_reason_check
        CHECK (reason IN ('shred', 'erasure', 'backfilled', 'backfilled_approximate',
            'reconciled'));
    """,
    # 23 light_engine: R1.20 PR A (ND347): start requests with an atomic claim, the
    # light engine's jobs (lease and epoch) and its leader lease. run_start_requests also
    # stores the plan text, plan_hash, started_by, data_class and idempotency_key_hash; the other
    # two tables hold ids, states, owners and times only. Additive: three new tables, no existing
    # row is read, changed or deleted.
    """
    CREATE TABLE run_start_requests (
        org_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'started', 'refused')),
        created_at TEXT NOT NULL,
        claim_owner TEXT,
        claim_until TEXT,
        refusal_code TEXT,
        engine TEXT NOT NULL,
        started_by TEXT NOT NULL,
        data_class TEXT NOT NULL,
        idempotency_key_hash TEXT,
        plan TEXT NOT NULL,
        PRIMARY KEY (org_id, run_id)
    );
    CREATE INDEX run_start_requests_state ON run_start_requests (state, created_at);
    CREATE TABLE light_jobs (
        org_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('ready', 'waiting_human', 'done')),
        due_at TEXT NOT NULL,
        lease_owner TEXT,
        lease_until TEXT,
        epoch INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (org_id, run_id)
    );
    CREATE INDEX light_jobs_due ON light_jobs (state, due_at);
    CREATE TABLE light_leader (
        name TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        lease_until TEXT NOT NULL,
        epoch INTEGER NOT NULL
    );
        """,
    # 24 invites: R1.37 (spec/identity/invites.md section 1). The invites table and the
    # verified state of an address on users. Additive: the one backfill (every not-erased user is
    # marked verified `legacy`, one clock reading) is applied by the store right after this
    # statement.
    """
    CREATE TABLE invites (
        org_id TEXT NOT NULL,
        id TEXT NOT NULL,
        email TEXT,
        role TEXT NOT NULL CHECK (role IN ('approver', 'viewer')),
        token_hash TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        resent_count INTEGER NOT NULL DEFAULT 0 CHECK (resent_count BETWEEN 0 AND 5),
        accepted_at TEXT,
        accepted_user_id TEXT,
        revoked_at TEXT,
        revoke_reason TEXT,
        PRIMARY KEY (org_id, id),
        CHECK (accepted_at IS NULL OR revoked_at IS NULL)
    );
    CREATE UNIQUE INDEX invites_token ON invites (org_id, token_hash);
    CREATE INDEX invites_email ON invites (org_id, email);
    ALTER TABLE users ADD COLUMN email_verified_at TEXT;
    ALTER TABLE users ADD COLUMN email_verified_via TEXT;
    """,
    # 25 email_verification: R1.40 (spec/identity/email-verification.md section 2).
    # Additive: one table, no row of any other table is read or written.
    """
    CREATE TABLE email_changes (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        new_email TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        deliverable INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        cancelled_at TEXT,
        PRIMARY KEY (org_id, token_hash)
    );
    CREATE INDEX email_changes_user ON email_changes (org_id, user_id);
    """,
    # 26 password_reset: R1.41 (spec/identity/password-reset.md sections 1 and 4a).
    # Reset tokens (a decoy has user_id NULL), API-token revoke links and the password-login block
    # (Q15). Additive; no row holds an email address. Number 26 is released; R1.38 MFA is 27.
    """
    CREATE TABLE password_reset_tokens (
        org_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        user_id TEXT,
        via TEXT NOT NULL CHECK (via IN ('self', 'owner', 'cli')),
        created_by TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        revoked_at TEXT,
        PRIMARY KEY (org_id, token_hash)
    );
    CREATE INDEX password_reset_user ON password_reset_tokens (org_id, user_id);
    CREATE TABLE password_login_blocks (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        blocked_at TEXT NOT NULL,
        via TEXT NOT NULL CHECK (via IN ('owner', 'cli')),
        PRIMARY KEY (org_id, user_id)
    );
    CREATE TABLE api_token_revoke_links (
        org_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        user_id TEXT NOT NULL,
        api_token_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        revoked_at TEXT,
        PRIMARY KEY (org_id, token_hash)
    );
    CREATE INDEX api_token_revoke_links_user ON api_token_revoke_links (org_id, user_id);
    """,
    # 27 mfa: R1.38 (spec/identity/mfa.md section 2): one TOTP factor per person
    # (the secret only
    # sealed), MFA recovery code hashes, login challenges (hash only), the organization's sign-in
    # policy (no row = MFA not required) and three session columns. `login_via` is backfilled to
    # `password`, then NOT NULL with a CHECK on the closed set (application code validates it on
    # insert too).
    """
    CREATE TABLE mfa_factors (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        id TEXT NOT NULL,
        secret_enc BYTEA NOT NULL CHECK (octet_length(secret_enc) = 48),
        key_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        confirmed_at TEXT,
        last_used_step BIGINT,
        password_checked_at TEXT NOT NULL,
        PRIMARY KEY (org_id, user_id)
    );
    CREATE TABLE mfa_recovery_codes (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        used_at TEXT,
        PRIMARY KEY (org_id, user_id, code_hash)
    );
    CREATE TABLE mfa_challenges (
        org_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        user_id TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK (purpose IN ('verify', 'enrol')),
        factor_id TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        consumed_at TEXT,
        PRIMARY KEY (org_id, token_hash)
    );
    CREATE TABLE org_auth_settings (
        org_id TEXT PRIMARY KEY,
        mfa_required INTEGER NOT NULL DEFAULT 0 CHECK (mfa_required IN (0, 1)),
        updated_at TEXT,
        updated_by TEXT
    );
    ALTER TABLE auth_sessions ADD COLUMN mfa_verified_at TEXT;
    ALTER TABLE auth_sessions ADD COLUMN mfa_method TEXT;
    ALTER TABLE auth_sessions ADD COLUMN login_via TEXT;
    UPDATE auth_sessions SET login_via = 'password' WHERE login_via IS NULL;
    ALTER TABLE auth_sessions ALTER COLUMN login_via SET NOT NULL;
    ALTER TABLE auth_sessions ADD CONSTRAINT auth_sessions_login_via
        CHECK (login_via IN ('password', 'oidc'));
    """,
    # 28 extension_records: records an installed plugin keeps (ND72). The store never
    # reads `body`.
    # A row is never edited; only a row marked `erasable` is ever deleted, together with the
    # organization's content key. A plugin's unique key is unique per organization, namespace and
    # kind (a partial unique index, so rows without one don't compete).
    """
    CREATE TABLE extension_records (
        org_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        kind TEXT NOT NULL,
        record_id TEXT NOT NULL,
        unique_key TEXT,
        group_key TEXT,
        sort_key TEXT NOT NULL,
        erasable BOOLEAN NOT NULL,
        body TEXT NOT NULL,
        PRIMARY KEY (org_id, namespace, kind, record_id)
    );
    CREATE UNIQUE INDEX extension_records_unique
        ON extension_records (org_id, namespace, kind, unique_key) WHERE unique_key IS NOT NULL;
    CREATE INDEX extension_records_group
        ON extension_records (org_id, namespace, kind, group_key, sort_key);

    -- ND314 item 3: the service role has no DELETE on extension_records; it executes this one
    -- function. `nuramesh_definer` is a NOLOGIN role that owns the SECURITY DEFINER functions;
    -- it holds only SELECT and DELETE on this table. This migration does NOT create it (the
    -- migration owner has no CREATEROLE, and a role anyone could pre-create with LOGIN must not
    -- be trusted): the operator creates it once (compose: initdb/10-roles.sh), and this check
    -- refuses a missing role, any role with a dangerous flag, and a migration owner that cannot
    -- SET ROLE to it (PostgreSQL 16 needs that to ALTER ... OWNER TO). Fixed search_path, no
    -- PUBLIC execute (the EXECUTE grant to the service role is `postgres_roles.grant_statements`).
    -- It deletes only `erasable` rows of the ONE organization it is given, as the store did before.
    DO $role$
    DECLARE
        r pg_catalog.pg_roles%ROWTYPE;
        fix text;
        me oid;
        bad text;
        provider text;
    BEGIN
        -- ND326: the one allowed ADMIN row on a managed service is granted by this role, named by
        -- the operator (NURAMESH_PG_PROVIDER_ROLE; the store sets it for this transaction)
        provider := coalesce(pg_catalog.current_setting('nuramesh.provider_role', true), '');
        fix := 'CREATE ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            || 'NOREPLICATION NOBYPASSRLS; GRANT nuramesh_definer TO '
            || pg_catalog.quote_ident(current_user) || ';';
        SELECT * INTO r FROM pg_catalog.pg_roles WHERE rolname = 'nuramesh_definer';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'this migration needs the role nuramesh_definer, which does not exist. '
                'As a database superuser (or the cloud admin role) run once: %', fix
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF r.rolcanlogin OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
           OR r.rolreplication OR r.rolbypassrls THEN
            RAISE EXCEPTION 'the role nuramesh_definer exists but is not a plain NOLOGIN role '
                '(canlogin=%, super=%, createdb=%, createrole=%, replication=%, bypassrls=%). '
                'Refusing to give it ownership of functions. As a database superuser run once: '
                'ALTER ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOREPLICATION NOBYPASSRLS; GRANT nuramesh_definer TO %;',
                r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication,
                r.rolbypassrls, pg_catalog.quote_ident(current_user)
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        -- who else can act as the role: ANY member other than the migration owner is refused,
        -- ADMIN-only included (on PostgreSQL 16 an ADMIN holder can grant itself SET, and the
        -- store re-checks this at every open, ND314c).
        SELECT oid INTO me FROM pg_catalog.pg_roles WHERE rolname = current_user;
        IF current_setting('server_version_num')::int >= 160000 THEN
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE nuramesh_definer FROM %I '
                       || 'GRANTED BY %I;', pg_catalog.pg_get_userbyid(m.member),
                       pg_catalog.pg_get_userbyid(m.grantor)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.member))
              INTO bad FROM pg_catalog.pg_auth_members m
             WHERE m.roleid = r.oid AND m.member <> me
               AND NOT (provider <> '' AND pg_catalog.pg_get_userbyid(m.grantor) = provider
                        AND m.admin_option AND NOT m.set_option AND NOT m.inherit_option);
        ELSE
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE nuramesh_definer FROM %I;',
                       pg_catalog.pg_get_userbyid(m.member)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.member))
              INTO bad FROM pg_catalog.pg_auth_members m
             WHERE m.roleid = r.oid AND m.member <> me;
        END IF;
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'the role nuramesh_definer has other members (any membership counts, '
                'ADMIN-only included, because an ADMIN holder can grant itself SET ROLE); only '
                'the migration owner may be one. Refusing to give it ownership of functions. On '
                'a managed service whose cloud admin holds an ADMIN-only row granted by the '
                'provider''s own role, name that role in NURAMESH_PG_PROVIDER_ROLE; every other '
                'member is still refused. As a database superuser or the role named after '
                'GRANTED BY run once: %', bad
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF current_setting('server_version_num')::int >= 160000 THEN
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE %I FROM nuramesh_definer '
                       || 'GRANTED BY %I;', pg_catalog.pg_get_userbyid(m.roleid),
                       pg_catalog.pg_get_userbyid(m.grantor)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.roleid))
              INTO bad FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid;
        ELSE
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE %I FROM nuramesh_definer;',
                       pg_catalog.pg_get_userbyid(m.roleid)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.roleid))
              INTO bad FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid;
        END IF;
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'the role nuramesh_definer is itself a member of other roles, so it '
                'would carry their rights. Refusing to give it ownership of functions. As a '
                'database superuser run once: %', bad
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NOT pg_catalog.pg_has_role(current_user, 'nuramesh_definer',
               CASE WHEN current_setting('server_version_num')::int >= 160000
                    THEN 'SET' ELSE 'MEMBER' END) THEN
            RAISE EXCEPTION 'the migration owner % cannot SET ROLE to nuramesh_definer, which '
                'PostgreSQL needs to hand it the function. As a database superuser run once: '
                'GRANT nuramesh_definer TO %;',
                pg_catalog.quote_ident(current_user), pg_catalog.quote_ident(current_user)
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END
    $role$;
    GRANT SELECT, DELETE ON public.extension_records TO nuramesh_definer;
    CREATE OR REPLACE FUNCTION public.extension_records_erase(p_org text)
    RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $fn$
    DECLARE
        v_count bigint;
    BEGIN
        IF p_org IS NULL THEN
            RAISE EXCEPTION 'extension_records_erase needs an organization';
        END IF;
        DELETE FROM public.extension_records WHERE org_id = p_org AND erasable = TRUE;
        GET DIAGNOSTICS v_count = ROW_COUNT;
        RETURN v_count;
    END
    $fn$;
    -- the new owner needs CREATE on the schema for the ownership change (PostgreSQL 15 and later)
    GRANT CREATE ON SCHEMA public TO nuramesh_definer;
    ALTER FUNCTION public.extension_records_erase(text) OWNER TO nuramesh_definer;
    REVOKE CREATE ON SCHEMA public FROM nuramesh_definer;
    REVOKE ALL ON FUNCTION public.extension_records_erase(text) FROM PUBLIC;
    """,
    # 29 solo_approval: solo approval (#942, spec/rbac/solo-approval.md section 4). Additive only:
    # nullable columns, nothing existing is rewritten. `password_confirmed_at` is when a session
    # last re-confirmed the password (SA-8; NULL = not confirmed). The approval columns repeat
    # three values of `data` for decisions made from now on (NULL on older rows, which have no
    # mode), so the solo rate limit (SA-9) counts by index instead of reading every row.
    """
    ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS password_confirmed_at TEXT;
    ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approval_mode TEXT;
    ALTER TABLE approvals ADD COLUMN IF NOT EXISTS decided_by TEXT;
    ALTER TABLE approvals ADD COLUMN IF NOT EXISTS decided_at TEXT;
    CREATE INDEX IF NOT EXISTS approvals_solo_window
        ON approvals (decided_by, approval_mode, decided_at);
    """,
    # 30 api_token_names: token rename and delete (#634,
    # spec/auth/token-lifecycle.md R16, R23).
    # Additive only: a new table of retired token names (keyed hashes under the organization key,
    # never a name) and three SECURITY DEFINER functions (delete an inactive token with its revoke
    # links, erase a person as issuer, normalise one row). No existing row is read, rewritten or
    # deleted. The grants of the definer role are the narrow ones listed in R23.
    """
    CREATE TABLE api_token_retired_names (
        org_id TEXT NOT NULL,
        epoch BIGINT NOT NULL,
        name_hash TEXT NOT NULL,
        retired_at TEXT NOT NULL,
        PRIMARY KEY (org_id, epoch, name_hash)
    );
    DO $role$
    DECLARE
        r pg_catalog.pg_roles%ROWTYPE;
        fix text;
        me oid;
        bad text;
        provider text;
    BEGIN
        -- ND326: the one allowed ADMIN row on a managed service is granted by this role, named by
        -- the operator (NURAMESH_PG_PROVIDER_ROLE; the store sets it for this transaction)
        provider := coalesce(pg_catalog.current_setting('nuramesh.provider_role', true), '');
        fix := 'CREATE ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            || 'NOREPLICATION NOBYPASSRLS; GRANT nuramesh_definer TO '
            || pg_catalog.quote_ident(current_user) || ';';
        SELECT * INTO r FROM pg_catalog.pg_roles WHERE rolname = 'nuramesh_definer';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'this migration needs the role nuramesh_definer, which does not exist. '
                'As a database superuser (or the cloud admin role) run once: %', fix
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF r.rolcanlogin OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
           OR r.rolreplication OR r.rolbypassrls THEN
            RAISE EXCEPTION 'the role nuramesh_definer exists but is not a plain NOLOGIN role '
                '(canlogin=%, super=%, createdb=%, createrole=%, replication=%, bypassrls=%). '
                'Refusing to give it ownership of functions. As a database superuser run once: '
                'ALTER ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOREPLICATION NOBYPASSRLS; GRANT nuramesh_definer TO %;',
                r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication,
                r.rolbypassrls, pg_catalog.quote_ident(current_user)
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        -- who else can act as the role: ANY member other than the migration owner is refused,
        -- ADMIN-only included (on PostgreSQL 16 an ADMIN holder can grant itself SET, and the
        -- store re-checks this at every open, ND314c).
        SELECT oid INTO me FROM pg_catalog.pg_roles WHERE rolname = current_user;
        IF current_setting('server_version_num')::int >= 160000 THEN
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE nuramesh_definer FROM %I '
                       || 'GRANTED BY %I;', pg_catalog.pg_get_userbyid(m.member),
                       pg_catalog.pg_get_userbyid(m.grantor)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.member))
              INTO bad FROM pg_catalog.pg_auth_members m
             WHERE m.roleid = r.oid AND m.member <> me
               AND NOT (provider <> '' AND pg_catalog.pg_get_userbyid(m.grantor) = provider
                        AND m.admin_option AND NOT m.set_option AND NOT m.inherit_option);
        ELSE
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE nuramesh_definer FROM %I;',
                       pg_catalog.pg_get_userbyid(m.member)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.member))
              INTO bad FROM pg_catalog.pg_auth_members m
             WHERE m.roleid = r.oid AND m.member <> me;
        END IF;
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'the role nuramesh_definer has other members (any membership counts, '
                'ADMIN-only included, because an ADMIN holder can grant itself SET ROLE); only '
                'the migration owner may be one. Refusing to give it ownership of functions. On '
                'a managed service whose cloud admin holds an ADMIN-only row granted by the '
                'provider''s own role, name that role in NURAMESH_PG_PROVIDER_ROLE; every other '
                'member is still refused. As a database superuser or the role named after '
                'GRANTED BY run once: %', bad
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF current_setting('server_version_num')::int >= 160000 THEN
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE %I FROM nuramesh_definer '
                       || 'GRANTED BY %I;', pg_catalog.pg_get_userbyid(m.roleid),
                       pg_catalog.pg_get_userbyid(m.grantor)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.roleid))
              INTO bad FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid;
        ELSE
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE %I FROM nuramesh_definer;',
                       pg_catalog.pg_get_userbyid(m.roleid)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.roleid))
              INTO bad FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid;
        END IF;
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'the role nuramesh_definer is itself a member of other roles, so it '
                'would carry their rights. Refusing to give it ownership of functions. As a '
                'database superuser run once: %', bad
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NOT pg_catalog.pg_has_role(current_user, 'nuramesh_definer',
               CASE WHEN current_setting('server_version_num')::int >= 160000
                    THEN 'SET' ELSE 'MEMBER' END) THEN
            RAISE EXCEPTION 'the migration owner % cannot SET ROLE to nuramesh_definer, which '
                'PostgreSQL needs to hand it the function. As a database superuser run once: '
                'GRANT nuramesh_definer TO %;',
                pg_catalog.quote_ident(current_user), pg_catalog.quote_ident(current_user)
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END
    $role$;

    GRANT SELECT, DELETE ON public.api_tokens TO nuramesh_definer;
    GRANT UPDATE (name, created_by, revoked_by) ON public.api_tokens TO nuramesh_definer;
    GRANT SELECT, DELETE ON public.api_token_revoke_links TO nuramesh_definer;
    GRANT SELECT (org_id, id, erased_at) ON public.users TO nuramesh_definer;
    -- delete an INACTIVE token of ONE organization and, in the same call, only when the token row
    -- went, its revoke links. The clock is `least(caller now, database now)`: a far-future caller
    -- time never makes a live token look expired. `expires_at` is TEXT NOT NULL (ledger time form).
    CREATE OR REPLACE FUNCTION public.api_token_delete(p_org text, p_id text, p_now timestamptz)
    RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $fn$
    DECLARE
        v_count bigint;
    BEGIN
        IF p_org IS NULL OR p_id IS NULL OR p_now IS NULL THEN
            RAISE EXCEPTION 'api_token_delete needs an organization, a token and a time';
        END IF;
        DELETE FROM public.api_tokens
         WHERE org_id = p_org AND id = p_id
           AND (revoked_at IS NOT NULL
                OR least(p_now, pg_catalog.now()) > expires_at::timestamptz);
        GET DIAGNOSTICS v_count = ROW_COUNT;
        IF v_count > 0 THEN
            DELETE FROM public.api_token_revoke_links
             WHERE org_id = p_org AND api_token_id = p_id;
        END IF;
        RETURN v_count;
    END
    $fn$;
    -- erasure of a person (#634 R2 to R4): the rows of ONE organization created or revoked by the
    -- user, in ascending id; a created row gets `erased-<24 hex>` (then -1, -2 ... to the first
    -- name no other row of the organization holds; a name already in its own exact form stays)
    -- and `created_by` erased-user; a revoked_by of the user becomes erased-user. Rows changed.
    CREATE OR REPLACE FUNCTION public.api_token_erase_issuer(p_org text, p_user text)
    RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $fn$
    DECLARE
        r record;
        base text;
        v_name text;
        n integer;
        changed bigint := 0;
    BEGIN
        IF p_org IS NULL OR p_user IS NULL THEN
            RAISE EXCEPTION 'api_token_erase_issuer needs an organization and a user';
        END IF;
        IF p_user !~ '^human:[A-Za-z0-9_-]{1,64}$' THEN
            RAISE EXCEPTION 'api_token_erase_issuer erases only a person (human:<id>)';
        END IF;
        FOR r IN SELECT id, name, created_by, revoked_by FROM public.api_tokens
                  WHERE org_id = p_org AND (created_by = p_user OR revoked_by = p_user)
                  ORDER BY id COLLATE "C" FOR UPDATE
        LOOP
            v_name := r.name;
            IF r.created_by = p_user THEN
                base := 'erased-' || pg_catalog.substr(r.id, 5);
                IF NOT (r.name = base OR r.name ~ ('^' || base || '-[1-9][0-9]*$')) THEN
                    v_name := base;
                    n := 0;
                    WHILE EXISTS (SELECT 1 FROM public.api_tokens
                                   WHERE org_id = p_org AND name = v_name AND id <> r.id) LOOP
                        n := n + 1;
                        v_name := base || '-' || n::text;
                    END LOOP;
                END IF;
                UPDATE public.api_tokens
                   SET name = v_name, created_by = 'erased-user',
                       revoked_by = CASE WHEN r.revoked_by = p_user THEN 'erased-user'
                                         ELSE r.revoked_by END
                 WHERE org_id = p_org AND id = r.id;
            ELSE
                UPDATE public.api_tokens SET revoked_by = 'erased-user'
                 WHERE org_id = p_org AND id = r.id;
            END IF;
            changed := changed + 1;
        END LOOP;
        RETURN changed;
    END
    $fn$;
    -- R9 for ONE row: only when created_by / revoked_by is a person with no users row in the
    -- organization or an erased one (re-checked here), each column only when it qualifies, the
    -- name only with created_by. A live owner's token is never changed through it.
    CREATE OR REPLACE FUNCTION public.api_token_normalise_issuer(p_org text, p_id text)
    RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $fn$
    DECLARE
        r record;
        base text;
        v_name text;
        n integer;
        gone_created boolean;
        gone_revoked boolean;
    BEGIN
        IF p_org IS NULL OR p_id IS NULL THEN
            RAISE EXCEPTION 'api_token_normalise_issuer needs an organization and a token';
        END IF;
        SELECT name, created_by, revoked_by INTO r FROM public.api_tokens
         WHERE org_id = p_org AND id = p_id FOR UPDATE;
        IF NOT FOUND THEN
            RETURN 0;
        END IF;
        gone_created := r.created_by LIKE 'human:%' AND NOT EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.org_id = p_org AND u.id = r.created_by AND u.erased_at IS NULL);
        gone_revoked := coalesce(r.revoked_by LIKE 'human:%', false) AND NOT EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.org_id = p_org AND u.id = r.revoked_by AND u.erased_at IS NULL);
        IF NOT (gone_created OR gone_revoked) THEN
            RETURN 0;
        END IF;
        v_name := r.name;
        IF gone_created THEN
            base := 'erased-' || pg_catalog.substr(p_id, 5);
            IF NOT (r.name = base OR r.name ~ ('^' || base || '-[1-9][0-9]*$')) THEN
                v_name := base;
                n := 0;
                WHILE EXISTS (SELECT 1 FROM public.api_tokens
                               WHERE org_id = p_org AND name = v_name AND id <> p_id) LOOP
                    n := n + 1;
                    v_name := base || '-' || n::text;
                END LOOP;
            END IF;
        END IF;
        UPDATE public.api_tokens
           SET name = v_name,
               created_by = CASE WHEN gone_created THEN 'erased-user' ELSE r.created_by END,
               revoked_by = CASE WHEN gone_revoked THEN 'erased-user' ELSE r.revoked_by END
         WHERE org_id = p_org AND id = p_id;
        RETURN 1;
    END
    $fn$;
    -- the new owner needs CREATE on the schema for the ownership change (PostgreSQL 15 and later)
    GRANT CREATE ON SCHEMA public TO nuramesh_definer;
    ALTER FUNCTION public.api_token_delete(text, text, timestamptz) OWNER TO nuramesh_definer;
    ALTER FUNCTION public.api_token_erase_issuer(text, text) OWNER TO nuramesh_definer;
    ALTER FUNCTION public.api_token_normalise_issuer(text, text) OWNER TO nuramesh_definer;
    REVOKE CREATE ON SCHEMA public FROM nuramesh_definer;
    REVOKE ALL ON FUNCTION public.api_token_delete(text, text, timestamptz) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.api_token_erase_issuer(text, text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.api_token_normalise_issuer(text, text) FROM PUBLIC;
    """,
    # 31 rbac: R2.13 (spec/rbac section 9): the last strong authentication on a session (any
    # method). One
    # nullable column, additive: no role or session is rewritten; NULL means "ask once".
    """
    ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS step_up_at TEXT;
    """,
    # 32 approvals_sweep: the approvals sweep (T226, ND195 a, spec/auth/token-lifecycle.md Part C).
    # An index on the day an approval ended (the consumption time if it has one, else the decision
    # time, date prefix of the stored text; decided and consumed approvals only), so the sweep finds
    # the due rows without reading the finished ones, and the SECURITY DEFINER function the sweep
    # deletes through (no DELETE grant on approvals; ND289 item 2, ND314). Additive: an index, a
    # function and its grants; no row is read, rewritten or deleted by the migration.
    """
    CREATE INDEX approvals_anchor_day ON approvals (
        (COALESCE(LEFT((data::jsonb) ->> 'consumed_at', 10),
                  LEFT((data::jsonb) ->> 'decided_at', 10), ''))
    ) WHERE status IN ('approved', 'denied', 'consumed');
    DO $role$
    DECLARE
        r pg_catalog.pg_roles%ROWTYPE;
        fix text;
        me oid;
        bad text;
        provider text;
    BEGIN
        -- ND326: the one allowed ADMIN row on a managed service is granted by this role, named by
        -- the operator (NURAMESH_PG_PROVIDER_ROLE; the store sets it for this transaction)
        provider := coalesce(pg_catalog.current_setting('nuramesh.provider_role', true), '');
        fix := 'CREATE ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            || 'NOREPLICATION NOBYPASSRLS; GRANT nuramesh_definer TO '
            || pg_catalog.quote_ident(current_user) || ';';
        SELECT * INTO r FROM pg_catalog.pg_roles WHERE rolname = 'nuramesh_definer';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'this migration needs the role nuramesh_definer, which does not exist. '
                'As a database superuser (or the cloud admin role) run once: %', fix
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF r.rolcanlogin OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
           OR r.rolreplication OR r.rolbypassrls THEN
            RAISE EXCEPTION 'the role nuramesh_definer exists but is not a plain NOLOGIN role '
                '(canlogin=%, super=%, createdb=%, createrole=%, replication=%, bypassrls=%). '
                'Refusing to give it ownership of functions. As a database superuser run once: '
                'ALTER ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOREPLICATION NOBYPASSRLS; GRANT nuramesh_definer TO %;',
                r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication,
                r.rolbypassrls, pg_catalog.quote_ident(current_user)
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        -- who else can act as the role: ANY member other than the migration owner is refused,
        -- ADMIN-only included (on PostgreSQL 16 an ADMIN holder can grant itself SET, and the
        -- store re-checks this at every open, ND314c).
        SELECT oid INTO me FROM pg_catalog.pg_roles WHERE rolname = current_user;
        IF current_setting('server_version_num')::int >= 160000 THEN
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE nuramesh_definer FROM %I '
                       || 'GRANTED BY %I;', pg_catalog.pg_get_userbyid(m.member),
                       pg_catalog.pg_get_userbyid(m.grantor)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.member))
              INTO bad FROM pg_catalog.pg_auth_members m
             WHERE m.roleid = r.oid AND m.member <> me
               AND NOT (provider <> '' AND pg_catalog.pg_get_userbyid(m.grantor) = provider
                        AND m.admin_option AND NOT m.set_option AND NOT m.inherit_option);
        ELSE
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE nuramesh_definer FROM %I;',
                       pg_catalog.pg_get_userbyid(m.member)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.member))
              INTO bad FROM pg_catalog.pg_auth_members m
             WHERE m.roleid = r.oid AND m.member <> me;
        END IF;
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'the role nuramesh_definer has other members (any membership counts, '
                'ADMIN-only included, because an ADMIN holder can grant itself SET ROLE); only '
                'the migration owner may be one. Refusing to give it ownership of functions. On '
                'a managed service whose cloud admin holds an ADMIN-only row granted by the '
                'provider''s own role, name that role in NURAMESH_PG_PROVIDER_ROLE; every other '
                'member is still refused. As a database superuser or the role named after '
                'GRANTED BY run once: %', bad
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF current_setting('server_version_num')::int >= 160000 THEN
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE %I FROM nuramesh_definer '
                       || 'GRANTED BY %I;', pg_catalog.pg_get_userbyid(m.roleid),
                       pg_catalog.pg_get_userbyid(m.grantor)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.roleid))
              INTO bad FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid;
        ELSE
            SELECT pg_catalog.string_agg(pg_catalog.format('REVOKE %I FROM nuramesh_definer;',
                       pg_catalog.pg_get_userbyid(m.roleid)), ' '
                       ORDER BY pg_catalog.pg_get_userbyid(m.roleid))
              INTO bad FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid;
        END IF;
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'the role nuramesh_definer is itself a member of other roles, so it '
                'would carry their rights. Refusing to give it ownership of functions. As a '
                'database superuser run once: %', bad
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NOT pg_catalog.pg_has_role(current_user, 'nuramesh_definer',
               CASE WHEN current_setting('server_version_num')::int >= 160000
                    THEN 'SET' ELSE 'MEMBER' END) THEN
            RAISE EXCEPTION 'the migration owner % cannot SET ROLE to nuramesh_definer, which '
                'PostgreSQL needs to hand it the function. As a database superuser run once: '
                'GRANT nuramesh_definer TO %;',
                pg_catalog.quote_ident(current_user), pg_catalog.quote_ident(current_user)
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END
    $role$;
    GRANT SELECT, DELETE ON public.approvals TO nuramesh_definer;
    -- Deletes ONE approval when, inside the caller's transaction, it is approved, denied or
    -- consumed, is still at the version the caller read (p_version, NULL = any), and its 396 days
    -- (core.policy.APPROVAL_RETENTION) from its anchor (consumed_at, else decided_at, UTC, whole
    -- seconds) are over at least(p_now, database now): a far-future caller time never deletes.
    CREATE OR REPLACE FUNCTION public.approval_delete_if_due(
        p_id text, p_version integer, p_now timestamptz)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    SET TimeZone = 'UTC'
    AS $fn$
    DECLARE
        v_version integer;
        v_data jsonb;
        v_anchor timestamp;
        v_count integer;
    BEGIN
        IF p_id IS NULL OR p_now IS NULL THEN
            RAISE EXCEPTION 'approval_delete_if_due needs an id and a time';
        END IF;
        SELECT version, data::jsonb INTO v_version, v_data
            FROM public.approvals WHERE id = p_id;
        IF NOT FOUND OR (p_version IS NOT NULL AND v_version <> p_version)
           OR v_data ->> 'status' NOT IN ('approved', 'denied', 'consumed') THEN
            RETURN false;
        END IF;
        v_anchor := date_trunc('second', coalesce(
            (v_data ->> 'consumed_at')::timestamptz, (v_data ->> 'decided_at')::timestamptz)
            AT TIME ZONE 'UTC');
        IF v_anchor IS NULL
           OR date_trunc('second', least(p_now, pg_catalog.now()) AT TIME ZONE 'UTC')
              < v_anchor + interval '396 days' THEN
            RETURN false;
        END IF;
        -- the version guard makes the delete conditional (no FOR UPDATE: the definer has no UPDATE)
        DELETE FROM public.approvals WHERE id = p_id AND version = v_version;
        GET DIAGNOSTICS v_count = ROW_COUNT;
        RETURN v_count = 1;
    END
    $fn$;
    -- the new owner needs CREATE on the schema for the ownership change (PostgreSQL 15 and later)
    GRANT CREATE ON SCHEMA public TO nuramesh_definer;
    ALTER FUNCTION public.approval_delete_if_due(text, integer, timestamptz)
        OWNER TO nuramesh_definer;
    REVOKE CREATE ON SCHEMA public FROM nuramesh_definer;
    REVOKE ALL ON FUNCTION public.approval_delete_if_due(text, integer, timestamptz) FROM PUBLIC;
    """,
)

# The expression of migration 32's index (the query must repeat it to use the index).
_ANCHOR_DAY = (
    "COALESCE(LEFT((data::jsonb) ->> 'consumed_at', 10), "
    "LEFT((data::jsonb) ->> 'decided_at', 10), '')"
)
_DUE_STATUSES = "status IN ('approved', 'denied', 'consumed')"

_RQ_UPDATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "run_start_requests": (
        "UPDATE run_start_requests SET state = %s, claim_owner = %s, claim_until = %s, "
        "refusal_code = %s WHERE org_id = %s AND run_id = %s",
        ("state", "claim_owner", "claim_until", "refusal_code", "org_id", "run_id"),
    ),
    "light_jobs": (
        "UPDATE light_jobs SET state = %s, due_at = %s, lease_owner = %s, lease_until = %s, "
        "epoch = %s WHERE org_id = %s AND run_id = %s",
        ("state", "due_at", "lease_owner", "lease_until", "epoch", "org_id", "run_id"),
    ),
    "light_leader": (
        "UPDATE light_leader SET owner = %s, lease_until = %s, epoch = %s WHERE name = %s",
        ("owner", "lease_until", "epoch", "name"),
    ),
}
"""The run queue's updates: only the columns that change (R1.20 PR A); the grants list the same."""

IDENTIFIER_KEY_MIGRATION = 7  # migration: security_log_identifier_keys
SUBJECT_MIGRATION = 8  # migration: security_log_subject
BUDGET_ORG_MIGRATION = 11  # migration: budgets_by_org
INVITES_MIGRATION = 24  # migration: invites
EMAIL_VERIFICATION_MIGRATION = 25  # migration: email_verification
PASSWORD_RESET_MIGRATION = 26  # migration: password_reset
MFA_MIGRATION = 27  # migration: mfa
TOMBSTONE_MIGRATION = 12  # migration: key_tombstone
TOMBSTONE_APPROX_MIGRATION = 22  # migration: tombstone_approx_reason
COMMIT_POS_MIGRATION = 16  # migration: ledger_commit_pos
DEFINER_ROLE = "nuramesh_definer"
# the SECURITY DEFINER functions handed to the definer role, each with the first schema version
# that has it (MA1: the open check finds the role as their owner)
DEFINER_FUNCTIONS: tuple[tuple[str, int], ...] = (
    ("extension_records_erase", 28),
    ("api_token_delete", 30),
    ("api_token_erase_issuer", 30),
    ("api_token_normalise_issuer", 30),
    ("approval_delete_if_due", 32),
)
PROVIDER_ROLE_ENV = "NURAMESH_PG_PROVIDER_ROLE"  # ND326: no default
# ledger stream for facts about opening the store; its id is reserved (`RESERVED_ORG_IDS`), so no
# organization can own it
STARTUP_STREAM = "org:" + STARTUP_STREAM_ID
PROJECTION_MIGRATION = 18  # migration: autonomy_projection
AT_CHECK_MIGRATION = 19  # migration: ledger_at_check
SOLO_MIGRATION = 29  # migration: solo_approval
STEP_UP_MIGRATION = 31  # migration: rbac


def _key_budgets_by_organization(db: Any) -> None:
    """Migration 11, in the application (see `budget_org`): copy each balance under its
    (organization, id), give reservations their organization, verify, replace the old table."""
    old = db.execute("SELECT id, version, data FROM budget_balances ORDER BY id").fetchall()
    owners = resolve_owners(
        [
            (r["stream"], r["payload"])
            for r in db.execute(
                "SELECT stream, payload FROM ledger_events WHERE type = 'budget.defined' "
                "AND substr(stream, 1, 4) = 'org:' "
                'ORDER BY at COLLATE "C", stream COLLATE "C", seq'
            ).fetchall()
        ],
        [r["data"] for r in old],
        [
            (r["stream"], r["payload"])
            for r in db.execute(
                "SELECT stream, payload FROM ledger_events WHERE type = 'run.started' "
                "AND substr(stream, 1, 4) = 'run:' AND seq = 1"
            ).fetchall()
        ],
    )
    for row in old:
        org, budget_id, data = balance_row(row["data"], owners)
        db.execute(
            "INSERT INTO budget_balances_org (org_id, id, version, data) VALUES (%s, %s, %s, %s)",
            (org, budget_id, row["version"], data),
        )
    for row in db.execute("SELECT id, data FROM reservations").fetchall():
        db.execute(
            "UPDATE reservations SET data = %s WHERE id = %s",
            (reservation_data(row["data"], owners), row["id"]),
        )
    copied = db.execute("SELECT COUNT(*) AS n FROM budget_balances_org").fetchone()["n"]
    if copied != len(old):
        raise MigrationError(f"budget migration copied {copied} of {len(old)} balances")
    db.execute("DROP TABLE budget_balances")
    db.execute("ALTER TABLE budget_balances_org RENAME TO budget_balances")
    db.execute("ALTER INDEX budget_balances_org_pkey RENAME TO budget_balances_pkey")


_KEYED_SQL = (
    "SELECT payload FROM ledger_events WHERE payload LIKE %s ESCAPE '\\' "
    "AND payload LIKE '%%\"hash_scheme\"%%'"
)


def _shred_row(r: Any) -> tuple[str, datetime | BadTimeError, str, str]:
    """`(stream, at, actor, payload)` of a `decision.recorded` row; a bad `at` becomes the error
    to raise only if the entry turns out to be a shred (ND277)."""
    at = _ledger_time_or_none(r["at"])
    return (
        r["stream"],
        at if at is not None else BadTimeError(r["stream"], r["seq"]),
        r["actor"],
        r["payload"],
    )


def _shred_rows(db: Any) -> list[tuple[str, datetime | BadTimeError, str, str]]:
    return [
        _shred_row(r)
        for r in db.execute(
            "SELECT stream, seq, at, actor, payload FROM ledger_events "
            "WHERE type = 'decision.recorded' "
            "AND substr(stream, 1, 4) = 'org:' ORDER BY at, stream, seq"
        ).fetchall()
    ]


def _tombstone_and_identify(db: Any) -> None:
    """Migration 12, in the application: give the store its identity and backfill a tombstone for
    every organization the ledger says was shredded with a date the ledger can give (no chain
    verification, no ledger entry). The approximate ones (T258) wait for migration 22, which is
    the first to accept their reason: migration 12 stays as released."""
    if db.execute("SELECT 1 FROM store_identity").fetchone() is None:
        db.execute(
            "INSERT INTO store_identity (singleton, store_id) VALUES (1, %s)",
            (secrets.token_hex(16),),
        )
    unused = datetime.fromtimestamp(0, UTC)  # approximate records are dropped here, never dated
    for record in backfill_records(backfill_shreds(_shred_rows(db), lambda: unused)):
        if record.reason != "backfilled_approximate":
            _insert_tombstone(db, record)


def _backfill_approximate_tombstones(db: Any, clock: Clock) -> None:
    """Migration 22, in the application: the tombstones migration 12 could not write (a shred
    whose time is unreadable, a decision that cannot be read: fail closed, T258). A shred whose
    time was exact keeps its tombstone; a store that already has one for the org is untouched
    (`INSERT OR IGNORE`). The store clock dates an undatable one, read only when needed."""
    for record in backfill_records(backfill_shreds(_shred_rows(db), clock)):
        if record.reason == "backfilled_approximate":
            _insert_tombstone(db, record)


def _insert_tombstone(db: Any, record: KeyTombstone) -> bool:
    cursor = db.execute(
        "INSERT INTO key_tombstones (org_id, destroyed_at, actor, reason, key_id, backfilled, "
        "version) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (
            record.org_id,
            format_time(record.destroyed_at),
            record.actor,
            record.reason,
            record.key_id,
            record.backfilled,
            record.version,
        ),
    )
    return bool(cursor.rowcount > 0)


def _extension(row: Any) -> ExtensionRecord:
    return ExtensionRecord(
        org_id=row["org_id"],
        namespace=row["namespace"],
        kind=row["kind"],
        record_id=row["record_id"],
        unique_key=row["unique_key"],
        group_key=row["group_key"],
        sort_key=row["sort_key"],
        erasable=bool(row["erasable"]),
        body=row["body"],
    )


def _tombstone(row: dict[str, Any]) -> KeyTombstone:
    return KeyTombstone(
        org_id=row["org_id"],
        destroyed_at=datetime.fromisoformat(row["destroyed_at"]),
        actor=row["actor"],
        reason=row["reason"],
        key_id=row["key_id"],
        backfilled=bool(row["backfilled"]),
        version=row["version"],
    )


def _give_users_identifier_keys(db: Any) -> None:
    """Migration 7, in the application: every existing, not-erased user gets an identifier key of
    32 bytes from `secrets.token_bytes` (ND60; no database random function or extension)."""
    rows = db.execute(
        "SELECT org_id, id FROM users WHERE erased_at IS NULL AND identifier_key IS NULL"
    ).fetchall()
    for row in rows:
        db.execute(
            "UPDATE users SET identifier_key = %s WHERE org_id = %s AND id = %s",
            (new_identifier_key(), row["org_id"], row["id"]),
        )


def _backfill_security_subjects(db: Any) -> SubjectBackfill:
    """Migration 8, in the application: fill `subject` on the rows from before the column where
    the row identifies a user (`derive_subjects`); the rest are returned for the log, never
    guessed."""
    rows = [
        (r["org_id"], r["id"], r["type"], r["actor"], json.loads(r["details"]).get("email"))
        for r in db.execute("SELECT org_id, id, type, actor, details FROM security_log").fetchall()
    ]
    users = [
        (u["org_id"], u["id"], u["email"] or None)
        for u in db.execute("SELECT org_id, id, email FROM users").fetchall()
    ]
    found = derive_subjects(rows, users)
    for (org, row_id), user_id in found.linked.items():
        db.execute(
            "UPDATE security_log SET subject = %s WHERE org_id = %s AND id = %s",
            (user_id, org, row_id),
        )
    return found


_EVENT_COLUMNS = (
    "stream, seq, type, actor, at, run_id, task_id, payload, cost_amount, cost_currency, "
    "cost_basis, prev_hash, hash"
)


def _wrap(exc: psycopg.Error) -> StoreError:
    """A database error as a `StoreError`; lock waits that time out become `StoreBusyError`."""
    if isinstance(
        exc, errors.LockNotAvailable | errors.DeadlockDetected | errors.SerializationFailure
    ):
        return StoreBusyError(str(exc))
    if isinstance(exc, errors.UniqueViolation):
        return ConcurrencyError(str(exc))
    return StoreError(f"{type(exc).__name__}: {exc}")


def _event_row(event: LedgerEvent) -> tuple[Any, ...]:
    cost = event.cost.to_json() if event.cost is not None else None
    return (
        event.stream,
        event.seq,
        event.type,
        event.actor,
        format_time(event.at),
        event.run_id,
        event.task_id,
        canonical_json(thaw(event.payload)).decode("utf-8"),
        None if cost is None else cost["amount"],
        None if cost is None else cost["currency"],
        event.cost_basis,
        event.prev_hash,
        event.hash,
    )


_LOG = logging.getLogger("nuramesh.stores")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CLEAN_CACHE = 100_000  # streams remembered as free of a damaged time


def _ledger_time_or_none(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def _ledger_time(text: str, stream: str, seq: int | None) -> datetime:
    """A stored ledger `at`, or `BadTimeError` when the text is not a real time (ND233)."""
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        raise BadTimeError(stream, seq) from None


def _owner_of(kind: str, payload: Any) -> str | None:
    """The `org` of a `run.started` payload read without parsing any time (ND238)."""
    if kind != EventType.RUN_STARTED.value:
        return None
    try:
        data = json.loads(payload) if isinstance(payload, str | bytes) else payload
        org = data.get("org")
    except (ValueError, AttributeError):
        return None
    return org if isinstance(org, str) else None


def _event_from_row(row: dict[str, Any], *, key_only: bool = False) -> LedgerEvent:
    """The stored event. `key_only` is for the projection rebuild only: a time that is not real
    is replaced by the epoch, because the projection key needs no time (ND277); the event is never
    returned to a caller, and a read of the row still raises `BadTimeError`."""
    cost = None
    if row["cost_amount"] is not None:
        cost = Money.model_validate(
            {"amount": Decimal(row["cost_amount"]), "currency": row["cost_currency"]}
        )
    try:
        return LedgerEvent.model_validate(
            {
                "stream": row["stream"],
                "seq": row["seq"],
                "type": row["type"],
                "actor": row["actor"],
                "at": (
                    _ledger_time_or_none(row["at"]) or _EPOCH
                    if key_only
                    else _ledger_time(row["at"], row["stream"], row["seq"])
                ),
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "payload": json.loads(row["payload"]),
                "cost": cost,
                "cost_basis": row["cost_basis"],
                "prev_hash": row["prev_hash"],
                "hash": row["hash"],
            }
        )
    except ValidationError as exc:
        raise StoreError(
            f"stored event {row['stream']}#{row['seq']} no longer validates "
            f"({exc.error_count()} problem(s)); for example a person recorded as an email or "
            "dotted name before T42"
        ) from exc


def _stored_task(row: dict[str, Any]) -> StoredTask:
    return StoredTask(task=Task.model_validate_json(row["data"]), version=row["version"])


def _stored_balance(row: dict[str, Any]) -> StoredBalance:
    return StoredBalance(
        balance=BudgetBalance.model_validate_json(row["data"]), version=row["version"]
    )


def _content_record(row: dict[str, Any]) -> ContentRecord:
    return ContentRecord(
        org_id=row["org_id"],
        id=row["id"],
        kind=ContentKind(row["kind"]),
        key_id=row["key_id"],
        nonce=bytes(row["nonce"]),
        ciphertext=bytes(row["ciphertext"]),
        size=row["size"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=None if row["expires_at"] is None else datetime.fromisoformat(row["expires_at"]),
    )


def _user(row: dict[str, Any]) -> User:
    return User(
        org_id=row["org_id"],
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        role=Role(row["role"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        erased_at=None if row["erased_at"] is None else datetime.fromisoformat(row["erased_at"]),
        disabled_at=(
            None if row["disabled_at"] is None else datetime.fromisoformat(row["disabled_at"])
        ),
        email_verified_at=(
            None
            if row["email_verified_at"] is None
            else datetime.fromisoformat(row["email_verified_at"])
        ),
        email_verified_via=row["email_verified_via"],
    )


def _credential(row: dict[str, Any]) -> Credential:
    return Credential(
        org_id=row["org_id"],
        user_id=row["user_id"],
        password_hash=row["password_hash"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _setup_token(row: dict[str, Any]) -> SetupToken:
    consumed = row["consumed_at"]
    return SetupToken(
        org_id=row["org_id"],
        token_hash=row["token_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        consumed_at=None if consumed is None else datetime.fromisoformat(consumed),
    )


def _auth_session(row: dict[str, Any]) -> AuthSession:
    revoked = row["revoked_at"]
    verified = row["mfa_verified_at"]
    confirmed = row["password_confirmed_at"]
    step_up = row["step_up_at"]
    return AuthSession.from_stored(
        {
            "org_id": row["org_id"],
            "id": row["id"],
            "user_id": row["user_id"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "last_active_at": datetime.fromisoformat(row["last_active_at"]),
            "absolute_expires_at": datetime.fromisoformat(row["absolute_expires_at"]),
            "revoked_at": None if revoked is None else datetime.fromisoformat(revoked),
            "revoke_reason": row["revoke_reason"],
            "login_via": row["login_via"],  # None or unknown is kept for refresh to refuse (T68)
            "mfa_verified_at": None if verified is None else datetime.fromisoformat(verified),
            "mfa_method": row["mfa_method"],
            "password_confirmed_at": (
                None if confirmed is None else datetime.fromisoformat(confirmed)
            ),
            "step_up_at": None if step_up is None else datetime.fromisoformat(step_up),
        }
    )


def _mfa_factor(row: dict[str, Any]) -> MfaFactor:
    confirmed = row["confirmed_at"]
    return MfaFactor(
        org_id=row["org_id"],
        user_id=row["user_id"],
        id=row["id"],
        secret_enc=bytes(row["secret_enc"]),
        key_id=row["key_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        confirmed_at=None if confirmed is None else datetime.fromisoformat(confirmed),
        last_used_step=row["last_used_step"],
        password_checked_at=datetime.fromisoformat(row["password_checked_at"]),
    )


def _mfa_challenge(row: dict[str, Any]) -> MfaChallenge:
    consumed = row["consumed_at"]
    return MfaChallenge(
        org_id=row["org_id"],
        token_hash=row["token_hash"],
        user_id=row["user_id"],
        purpose=row["purpose"],
        factor_id=row["factor_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        attempts=row["attempts"],
        consumed_at=None if consumed is None else datetime.fromisoformat(consumed),
    )


def _org_auth_settings(row: dict[str, Any]) -> OrgAuthSettings:
    updated = row["updated_at"]
    return OrgAuthSettings(
        org_id=row["org_id"],
        mfa_required=bool(row["mfa_required"]),
        updated_at=None if updated is None else datetime.fromisoformat(updated),
        updated_by=row["updated_by"],
    )


def _auth_token(row: dict[str, Any]) -> AuthToken:
    used = row["used_at"]
    return AuthToken(
        org_id=row["org_id"],
        token_hash=row["token_hash"],
        session_id=row["session_id"],
        kind=TokenKind(row["kind"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        used_at=None if used is None else datetime.fromisoformat(used),
    )


def _auth_failure(row: dict[str, Any]) -> AuthFailure:
    locked = row["locked_until"]
    return AuthFailure(
        key=row["key"],
        count=row["count"],
        last_failure_at=datetime.fromisoformat(row["last_failure_at"]),
        locked_until=None if locked is None else datetime.fromisoformat(locked),
    )


def _security_log(row: dict[str, Any]) -> SecurityLogEntry:
    return SecurityLogEntry.load(
        org_id=row["org_id"],
        id=row["id"],
        at=datetime.fromisoformat(row["at"]),
        type=row["type"],
        actor=row["actor"],
        subject=row["subject"],
        details=json.loads(row["details"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )


def _api_token(row: dict[str, Any]) -> ApiToken:
    revoked = row["revoked_at"]
    return ApiToken(
        org_id=row["org_id"],
        id=row["id"],
        name=row["name"],
        scopes=frozenset(TokenScope(s) for s in row["scopes"].split(",")),
        token_hash=row["token_hash"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        revoked_at=None if revoked is None else datetime.fromisoformat(revoked),
        revoked_by=row["revoked_by"],
    )


def _invite(row: Any) -> Invite:
    def when(name: str) -> datetime | None:
        return None if row[name] is None else datetime.fromisoformat(row[name])

    return Invite(
        org_id=row["org_id"],
        id=row["id"],
        email=row["email"],
        role=row["role"],
        token_hash=row["token_hash"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        resent_count=row["resent_count"],
        accepted_at=when("accepted_at"),
        accepted_user_id=row["accepted_user_id"],
        revoked_at=when("revoked_at"),
        revoke_reason=row["revoke_reason"],
    )


def _email_change(row: Any) -> EmailChange:
    def when(name: str) -> datetime | None:
        return None if row[name] is None else datetime.fromisoformat(row[name])

    return EmailChange(
        org_id=row["org_id"],
        user_id=row["user_id"],
        new_email=row["new_email"],
        token_hash=row["token_hash"],
        deliverable=bool(row["deliverable"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        consumed_at=when("consumed_at"),
        cancelled_at=when("cancelled_at"),
    )


def _reset_token(row: Any) -> ResetToken:
    def when(name: str) -> datetime | None:
        return None if row[name] is None else datetime.fromisoformat(row[name])

    return ResetToken(
        org_id=row["org_id"],
        token_hash=row["token_hash"],
        user_id=row["user_id"],
        via=row["via"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        consumed_at=when("consumed_at"),
        revoked_at=when("revoked_at"),
    )


def _revoke_link(row: Any) -> RevokeLink:
    def when(name: str) -> datetime | None:
        return None if row[name] is None else datetime.fromisoformat(row[name])

    return RevokeLink(
        org_id=row["org_id"],
        token_hash=row["token_hash"],
        user_id=row["user_id"],
        api_token_id=row["api_token_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        consumed_at=when("consumed_at"),
        revoked_at=when("revoked_at"),
    )


def _login_block(row: Any) -> PasswordLoginBlock:
    return PasswordLoginBlock(
        org_id=row["org_id"],
        user_id=row["user_id"],
        blocked_at=datetime.fromisoformat(row["blocked_at"]),
        via=row["via"],
    )


def _verify_legacy_users(db: Any, clock: Callable[[], datetime]) -> None:
    """Migration 24, in the application: every not-erased user is verified `legacy` at one clock
    reading (the migration's run time; `created_at` is not a time of proof). The clock is read
    only when there is someone to verify, so a new database consumes no reading."""
    if not db.execute(
        "SELECT 1 FROM users WHERE erased_at IS NULL AND email_verified_at IS NULL LIMIT 1"
    ).fetchone():
        return
    at = clock()
    db.execute(
        "UPDATE users SET email_verified_at = %s, email_verified_via = 'legacy' "
        "WHERE erased_at IS NULL AND email_verified_at IS NULL",
        (format_time(at),),
    )


def _agent_credential(row: Any) -> AgentCredential:
    revoked = row["revoked_at"]
    return AgentCredential(
        org_id=row["org_id"],
        id=row["id"],
        agent_id=row["agent_id"],
        team=row["team"],
        fingerprint=row["fingerprint"],
        scopes=frozenset(TokenScope(s) for s in row["scopes"].split(",")),
        token_hash=row["token_hash"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        revoked_at=None if revoked is None else datetime.fromisoformat(revoked),
        revoked_by=row["revoked_by"],
    )


def _stored_approval(row: dict[str, Any]) -> StoredApproval:
    return StoredApproval(
        approval=Approval.model_validate_json(row["data"]), version=row["version"]
    )


def _stored_reservation(row: dict[str, Any]) -> StoredReservation:
    return StoredReservation(
        reservation=Reservation.model_validate_json(row["data"]), version=row["version"]
    )


def _read_head(db: psycopg.Connection[dict[str, Any]], stream: str) -> StreamHead | None:
    row = db.execute(
        "SELECT stream, seq, hash, at FROM stream_heads WHERE stream = %s", (stream,)
    ).fetchone()
    if row is None:
        return None
    return StreamHead(
        stream=row["stream"],
        seq=row["seq"],
        hash=row["hash"],
        at=_ledger_time(row["at"], row["stream"], None),
    )


def _approval_columns(approval: Approval) -> dict[str, str | None]:
    """The columns that repeat values of an approval's `data` (the last three since migration
    `solo_approval`: who decided, when and in which mode; NULL while pending)."""
    return {
        "run_id": approval.run_id,
        "agent_id": approval.agent_id,
        "status": approval.status.value,
        "approval_mode": None if approval.approval_mode is None else approval.approval_mode.value,
        "decided_by": approval.decided_by,
        "decided_at": None if approval.decided_at is None else format_time(approval.decided_at),
    }


def _put_versioned(
    db: psycopg.Connection[dict[str, Any]],
    table: str,
    key: str,
    data: str,
    expected_version: int | None,
    extra: dict[str, str | None] | None = None,
) -> int:
    """Insert (expected_version None) or update a versioned row; ConcurrencyError if stale."""
    extra = extra or {}
    if expected_version is None:
        columns = ["id", "version", "data", *extra]
        values = [key, 1, data, *extra.values()]
        marks = ", ".join("%s" for _ in columns)
        cursor = db.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks}) "
            "ON CONFLICT (id) DO NOTHING",
            values,
        )
        if cursor.rowcount != 1:
            raise ConcurrencyError(f"{table} '{key}' already exists")
        return 1
    sets = ", ".join(["version = version + 1", "data = %s", *(f"{c} = %s" for c in extra)])
    cursor = db.execute(
        f"UPDATE {table} SET {sets} WHERE id = %s AND version = %s",
        (data, *extra.values(), key, expected_version),
    )
    if cursor.rowcount != 1:
        row = db.execute(f"SELECT version FROM {table} WHERE id = %s", (key,)).fetchone()
        found = None if row is None else row["version"]
        raise ConcurrencyError(f"{table} '{key}' is at version {found}, not {expected_version}")
    return expected_version + 1


def _put_balance(
    db: psycopg.Connection[dict[str, Any]], balance: BudgetBalance, expected_version: int | None
) -> int:
    """Insert or update a balance row by (organization, id); ConcurrencyError if stale."""
    org, key = org_key(balance.budget.org_id), balance.budget.id
    data = balance.model_dump_json()
    if expected_version is None:
        cursor = db.execute(
            "INSERT INTO budget_balances (org_id, id, version, data) VALUES (%s, %s, 1, %s) "
            "ON CONFLICT (org_id, id) DO NOTHING",
            (org, key, data),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyError(f"budget '{key}' already exists")
        return 1
    cursor = db.execute(
        "UPDATE budget_balances SET version = version + 1, data = %s "
        "WHERE org_id = %s AND id = %s AND version = %s",
        (data, org, key, expected_version),
    )
    if cursor.rowcount != 1:
        row = db.execute(
            "SELECT version FROM budget_balances WHERE org_id = %s AND id = %s", (org, key)
        ).fetchone()
        found = None if row is None else row["version"]
        raise ConcurrencyError(f"budget '{key}' is at version {found}, not {expected_version}")
    return expected_version + 1


def _idempotency(row: dict[str, Any], subjects: frozenset[str]) -> StoredIdempotency:
    sealed = (
        None
        if row["payload"] is None
        else Sealed(bytes(row["payload"]), bytes(row["nonce"]), row["key_id"])
    )
    return StoredIdempotency(
        scope=Scope(row["org_id"], row["actor"], row["method"], row["route"]),
        key_hash=row["key_hash"],
        fingerprint=row["fingerprint"],
        state=State(row["state"]),
        attempt=row["attempt"],
        fence=row["fence"],
        subjects=subjects,
        created_at=datetime.fromisoformat(row["created_at"]),
        lease_until=datetime.fromisoformat(row["lease_until"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        status=row["status"],
        sealed=sealed,
    )


# Keepalives on the lock connection only. The libpq `keepalives*` arguments set the CLIENT socket
# (this process notices a dead server). What frees the lock when this host vanishes is the SERVER
# backend noticing the dead client, which is governed by the server-side `tcp_keepalives_*` GUCs, so
# they are also SET on this connection after it opens (a SET, never an `options=` argument, which
# would replace the operator's own DSN options). With both applied a vanished leader's lock frees
# after about idle + interval * count = 10 + 5 * 3 = 25 s instead of the OS default (~2 h). That
# holds only over TCP: a Unix-socket connection never sends keepalive probes.
# That 25 s is the keepalive arithmetic. `tcp_user_timeout` is set on both sides (the client keyword
# below, the server GUC via the SET): on Linux it overrides keepalives_count, so detection of a dead
# peer, this process noticing a dead server and the server freeing a vanished client's lock, takes
# about 30-35 s, and a blocked unlock/close is bounded at about 30 s. The libpq keyword needs libpq
# 12 or newer (the bundled psycopg binary has it; a non-binary psycopg on an older system libpq
# would refuse it with "invalid connection option"). It is ignored on Unix sockets and where the OS
# has no TCP_USER_TIMEOUT (macOS).
LEADER_LOCK_CONNECT_ARGS: dict[str, int] = {
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
    "tcp_user_timeout": 30_000,  # ms, client socket (libpq 12+): bounds a blocked unlock/close
}
LEADER_LOCK_SERVER_SETTINGS: tuple[tuple[str, int], ...] = (
    ("tcp_keepalives_idle", 10),
    ("tcp_keepalives_interval", 5),
    ("tcp_keepalives_count", 3),
)
# milliseconds; bounds a blocked unlock/close during a network partition (PostgreSQL 12 or newer)
LEADER_LOCK_TCP_USER_TIMEOUT_MS = 30_000


def _apply_lock_session_settings(conn: "psycopg.Connection[dict[str, Any]]") -> None:
    """Set the server-side TCP keepalive GUCs (and tcp_user_timeout where the server knows it) on
    the lock connection. Values are fixed integers, so the SQL holds no caller data."""
    for name, value in LEADER_LOCK_SERVER_SETTINGS:
        conn.execute(f"SET {name} = {int(value)}")  # noqa: S608 - fixed name and integer
    try:
        conn.execute(f"SET tcp_user_timeout = {int(LEADER_LOCK_TCP_USER_TIMEOUT_MS)}")
    except psycopg.Error:
        pass  # PostgreSQL older than 12 has no such setting (autocommit: nothing to roll back)


class _LeaderLocks:
    """The light engine's leader lock (spec E27): a Postgres SESSION advisory lock on a dedicated,
    non-pooled connection of its own (never the store's write connection, never shared), held for
    as long as the process leads. If the connection is lost the database frees the lock and the
    next `take_leader` finds it gone, so the engine steps down before its next step. This sits
    next to the lease row (which a restarted or clock-skewed runner could read as expired) and
    never replaces it."""

    def __init__(self, connect: Callable[[], "psycopg.Connection[dict[str, Any]]"]) -> None:
        self._connect = connect
        # lease name -> (owner the lock is held for, connection): a refused caller that is not that
        # owner never drops the holder's lock
        self._held: dict[str, tuple[str, psycopg.Connection[dict[str, Any]]]] = {}
        self._guard = threading.Lock()

    @staticmethod
    def _key(name: str) -> int:
        digest = hashlib.sha256(b"nuramesh-light-leader\x00" + name.encode()).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    def hold(self, name: str, owner: str) -> bool:
        """True if this store holds the lock for `owner` now (still alive) or just took it."""
        with self._guard:
            entry = self._held.get(name)
            if entry is not None:
                try:
                    entry[1].execute("SELECT 1")
                    return True  # held already (for the current holder or a successor of it)
                except psycopg.Error:
                    # the lock went with the session; pass the key anyway: if the failure was
                    # something else and the session is still usable, the lock is freed before
                    # the close (on a dead session the unlock errors and is swallowed)
                    self._held.pop(name, None)
                    self._close(entry[1], self._key(name))
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT pg_try_advisory_lock(%s) AS got", (self._key(name),)
                ).fetchone()
            except psycopg.Error:
                self._close(conn, None)
                return False
            if row is None or not row["got"]:
                self._close(conn, None)
                return False
            self._held[name] = (owner, conn)
            return True

    def bind(self, name: str, owner: str) -> None:
        """The lease row now says `owner`: the lock is held for it (a takeover after expiry).
        A no-op if the entry is gone (another owner of the same store dropped it between the
        hold and this call): the row compare-and-set stays exclusive, so there is never a second
        leader, and the next renewal's `hold` takes the lock again. Production runs one owner per
        store."""
        with self._guard:
            entry = self._held.get(name)
            if entry is not None:
                self._held[name] = (owner, entry[1])

    def drop(self, name: str, owner: str) -> None:
        """Release the lock, but only if it is held for `owner`: a caller that was refused must
        not free the current holder's lock."""
        with self._guard:
            entry = self._held.get(name)
            if entry is None or entry[0] != owner:
                return
            del self._held[name]
        self._close(entry[1], self._key(name))

    def close(self) -> None:
        with self._guard:
            held = [(n, e[1]) for n, e in self._held.items()]
            self._held = {}
        for name, conn in held:
            self._close(conn, self._key(name))

    @staticmethod
    def _close(conn: "psycopg.Connection[dict[str, Any]]", key: int | None) -> None:
        """Free the lock on the server BEFORE closing: closing alone returns at once, while the
        backend releases its session locks only when it exits, so a new connection could still be
        refused for a moment. `key` is None for a connection that never held the lock."""
        if key is not None:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except psycopg.Error:
                pass  # a lost session already lost the lock
        try:
            conn.close()
        except psycopg.Error:
            pass


class _PostgresUnitOfWork(UnitOfWork):
    def __init__(
        self,
        connection: "psycopg.Connection[dict[str, Any]]",
        clock: Clock,
        schema: int | None = None,
        clean: set[str] | None = None,
        leader_locks: _LeaderLocks | None = None,
        fence_clock: Clock | None = None,
    ) -> None:
        self._db = connection
        self._leader_locks = leader_locks
        self._clock = clock
        self._fence_clock = fence_clock
        self._fence_time: datetime | None = None
        self._clean = clean if clean is not None else set()
        self._at = clock()  # read once, inside the write lock (ND234)
        # same rule as SQLite: a store opened on an older schema writes events as that build did
        # (only reachable when a test patches MIGRATIONS; production always migrates)
        schema = len(MIGRATIONS) if schema is None else schema
        self._writes_commit_pos = schema >= COMMIT_POS_MIGRATION
        self._projects = schema >= PROJECTION_MIGRATION
        self._has_at_check = schema >= AT_CHECK_MIGRATION

    def _refuse_damaged(self, stream: str) -> None:
        if not self._has_at_check or stream in self._clean:
            return
        row = self._db.execute(
            "SELECT 1 FROM ledger_events WHERE stream = %s AND NOT nuramesh_valid_at(at) LIMIT 1",
            (stream,),
        ).fetchone()
        head = self._db.execute(
            "SELECT 1 FROM stream_heads WHERE stream = %s AND NOT nuramesh_valid_at(at)", (stream,)
        ).fetchone()
        if row is not None or head is not None:
            raise BadTimeError(stream, None)
        if len(self._clean) >= CLEAN_CACHE:
            self._clean.clear()  # only a cache of checks already made
        self._clean.add(stream)

    def last_event(self, stream: str) -> LedgerEvent | None:
        row = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = %s "
            "ORDER BY seq DESC LIMIT 1",
            (stream,),
        ).fetchone()
        return None if row is None else _event_from_row(row)

    def head(self, stream: str) -> StreamHead | None:
        return _read_head(self._db, stream)

    def events_after(self, stream: str, after_seq: int, limit: int) -> list[LedgerEvent]:
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = %s AND seq > %s "
            "ORDER BY seq LIMIT %s",
            (stream, after_seq, limit),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def events_after_positioned(
        self, stream: str, after_seq: int, limit: int
    ) -> list[tuple[int, LedgerEvent]]:
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS}, COALESCE(commit_pos, 0) AS commit_pos FROM ledger_events "
            "WHERE stream = %s AND seq > %s ORDER BY seq LIMIT %s",
            (stream, after_seq, limit),
        ).fetchall()
        return [(row["commit_pos"], _event_from_row(row)) for row in rows]

    def commit_position(self) -> int:
        row = self._db.execute("SELECT MAX(commit_pos) AS p FROM ledger_events").fetchone()
        return int((row or {}).get("p") or 0)

    def entries_after_position(
        self, pos: int, limit: int | None = None
    ) -> list[tuple[int, LedgerEvent]]:
        check_limit(limit)
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS}, COALESCE(commit_pos, 0) AS commit_pos FROM ledger_events "
            "WHERE commit_pos > %s ORDER BY commit_pos LIMIT %s",
            (pos, limit),
        ).fetchall()
        return [(row["commit_pos"], _event_from_row(row)) for row in rows]

    def has_event(self, stream: str, type: EventType) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM ledger_events WHERE stream = %s AND type = %s LIMIT 1",
            (stream, type.value),
        ).fetchone()
        return row is not None

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = %s AND task_id = %s "
            "ORDER BY seq",
            (run_stream(run_id), task_id),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def _insert_event(self, event: LedgerEvent) -> None:
        if self._writes_commit_pos:
            cursor = self._db.execute(
                f"INSERT INTO ledger_events ({_EVENT_COLUMNS}, commit_pos) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "(SELECT COALESCE(MAX(commit_pos), 0) + 1 FROM ledger_events)) "
                "ON CONFLICT (stream, seq) DO NOTHING",
                _event_row(event),
            )
        else:
            cursor = self._db.execute(
                f"INSERT INTO ledger_events ({_EVENT_COLUMNS}) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (stream, seq) DO NOTHING",
                _event_row(event),
            )
        if cursor.rowcount != 1:
            raise ConcurrencyError(f"event {event.stream}#{event.seq} already exists")
        self._db.execute(
            "INSERT INTO stream_heads (stream, seq, hash, at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (stream) DO UPDATE SET seq = excluded.seq, hash = excluded.hash, "
            "at = excluded.at",
            (event.stream, event.seq, event.hash, format_time(event.at)),
        )
        if self._projects:
            _project(self._db, event)
        self._db.execute(
            "SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, f"{event.stream}:{event.seq}")
        )

    def autonomy_events(
        self,
        org_id: str,
        kinds: Collection[str] | None = None,
        *,
        streams: ProjectionStreams = "all",
    ) -> list[tuple[int, LedgerEvent]]:
        return _read_projection(self._db, org_id, kinds, projection_streams(streams))

    def get_task(self, task_id: str) -> StoredTask | None:
        row = self._db.execute(
            "SELECT data, version FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        return None if row is None else _stored_task(row)

    def tasks_for_run(self, run_id: str) -> list[StoredTask]:
        rows = self._db.execute(
            "SELECT data, version FROM tasks WHERE run_id = %s ORDER BY id", (run_id,)
        ).fetchall()
        return [_stored_task(row) for row in rows]

    def put_task(self, task: Task, *, expected_version: int | None) -> int:
        data = task.model_dump_json()
        if expected_version is None:
            cursor = self._db.execute(
                "INSERT INTO tasks (id, run_id, status, version, data) "
                "VALUES (%s, %s, %s, 1, %s) ON CONFLICT (id) DO NOTHING",
                (task.id, task.run_id, task.status.value, data),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError(f"task '{task.id}' already exists")
            return 1
        cursor = self._db.execute(
            "UPDATE tasks SET run_id = %s, status = %s, version = version + 1, data = %s "
            "WHERE id = %s AND version = %s",
            (task.run_id, task.status.value, data, task.id, expected_version),
        )
        if cursor.rowcount != 1:
            current = self.get_task(task.id)
            found = None if current is None else current.version
            raise ConcurrencyError(
                f"task '{task.id}' is at version {found}, not {expected_version}"
            )
        return expected_version + 1

    def get_balance(self, org_id: str, budget_id: str) -> StoredBalance | None:
        row = self._db.execute(
            "SELECT data, version FROM budget_balances WHERE org_id = %s AND id = %s",
            (org_id, budget_id),
        ).fetchone()
        return None if row is None else _stored_balance(row)

    def list_balances(self, org_id: str | None) -> list[StoredBalance]:
        rows = self._db.execute(
            "SELECT data, version FROM budget_balances WHERE (%s::text IS NULL OR org_id = %s) "
            'ORDER BY org_id COLLATE "C", id COLLATE "C"',
            (org_id, org_id),
        ).fetchall()
        return [_stored_balance(row) for row in rows]

    def put_balance(self, balance: BudgetBalance, *, expected_version: int | None) -> int:
        return _put_balance(self._db, balance, expected_version)

    def get_content(self, org_id: str, content_id: str) -> ContentRecord | None:
        row = self._db.execute(
            "SELECT * FROM content WHERE org_id = %s AND id = %s", (org_id, content_id)
        ).fetchone()
        return None if row is None else _content_record(row)

    def put_content(self, record: ContentRecord) -> None:
        row = self._db.execute(
            "SELECT expires_at FROM content WHERE org_id = %s AND id = %s FOR UPDATE",
            (record.org_id, record.id),
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO content (org_id, id, kind, key_id, nonce, ciphertext, size, "
                "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.org_id,
                    record.id,
                    record.kind.value,
                    record.key_id,
                    record.nonce,
                    record.ciphertext,
                    record.size,
                    format_time(record.created_at),
                    None if record.expires_at is None else format_time(record.expires_at),
                ),
            )
            return
        old = None if row["expires_at"] is None else datetime.fromisoformat(row["expires_at"])
        merged = merged_expiry(old, record.expires_at)
        self._db.execute(
            "UPDATE content SET expires_at = %s WHERE org_id = %s AND id = %s",
            (None if merged is None else format_time(merged), record.org_id, record.id),
        )

    def set_run_content_expiry(
        self, org_id: str, content_id: str, expires_at: datetime | None
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE content SET expires_at = %s WHERE org_id = %s AND id = %s "
            "AND kind IN ('task_output', 'gate_evidence', 'plan_text')",
            (None if expires_at is None else format_time(expires_at), org_id, content_id),
        )
        return cursor.rowcount > 0

    def shorten_content_expiry(
        self,
        kind: ContentKind,
        max_days: int,
        *,
        org_id: str | None = None,
        content_id: str | None = None,
        since: datetime | None = None,
    ) -> dict[str, int]:
        sql = "SELECT org_id, id, created_at, expires_at FROM content WHERE kind = %s"
        params: list[Any] = [kind.value]
        if org_id is not None:
            sql += " AND org_id = %s"
            params.append(org_id)
        if content_id is not None:
            sql += " AND id = %s"
            params.append(content_id)
        changed: dict[str, int] = {}
        for row in self._db.execute(sql + " FOR UPDATE", params).fetchall():
            new = clamped_expiry(
                since or datetime.fromisoformat(row["created_at"]),
                None if row["expires_at"] is None else datetime.fromisoformat(row["expires_at"]),
                max_days,
            )
            if new is not None:
                self._db.execute(
                    "UPDATE content SET expires_at = %s WHERE org_id = %s AND id = %s",
                    (format_time(new), row["org_id"], row["id"]),
                )
                changed[row["org_id"]] = changed.get(row["org_id"], 0) + 1
        return changed

    def delete_content(self, org_id: str, content_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM content WHERE org_id = %s AND id = %s", (org_id, content_id)
        )
        self._drop_subjects(org_id, content_id)
        return cursor.rowcount > 0

    def _drop_subjects(self, org_id: str, content_id: str) -> None:
        self._db.execute(
            "DELETE FROM content_subjects WHERE org_id = %s AND content_id = %s",
            (org_id, content_id),
        )

    def _put_content_subject(
        self, org_id: str, content_id: str, subject: str, run_id: str | None
    ) -> bool:
        cursor = self._db.execute(
            "INSERT INTO content_subjects (org_id, content_id, subject, run_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (org_id, content_id, subject, run_id),
        )
        return cursor.rowcount > 0

    # -- the run queue (R1.20 PR A): primitives of RunQueueOps ----------------------------------

    @property
    def fence_at(self) -> datetime:
        """The database's own clock (ND347 5.3a/5.4): runners on different hosts share it, so a
        skewed host clock cannot change who holds a lease. Read once per unit of work, inside the
        write lock and before the row locks of the compare-and-set are used; `fence_clock` (tests
        only) replaces the database read with a shared fake database clock."""
        if self._fence_time is None:
            if self._fence_clock is not None:
                self._fence_time = self._fence_clock()
            else:
                row = self._db.execute("SELECT clock_timestamp() AS t").fetchone()
                assert row is not None
                self._fence_time = row["t"].astimezone(UTC)
        return self._fence_time

    def _rq_get(self, table: str, key: tuple[str, ...]) -> dict[str, Any] | None:
        where = " AND ".join(f"{c} = %s" for c in RQ_KEYS[table])
        row = self._db.execute(f"SELECT * FROM {table} WHERE {where} FOR UPDATE", key).fetchone()
        return None if row is None else dict(row)

    def _rq_insert(self, table: str, row: dict[str, Any]) -> bool:
        cols = RQ_COLUMNS[table]
        marks = ", ".join("%s" for _ in cols)
        cursor = self._db.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks}) ON CONFLICT DO NOTHING",
            tuple(row[c] for c in cols),
        )
        return cursor.rowcount > 0

    def _rq_update(self, table: str, row: dict[str, Any]) -> None:
        sql, columns = _RQ_UPDATES[table]
        self._db.execute(sql, tuple(row[c] for c in columns))

    def _rq_scan(self, table: str, state_in: tuple[str, ...] | None) -> list[dict[str, Any]]:
        if state_in is None:
            rows = self._db.execute(f"SELECT * FROM {table} FOR UPDATE").fetchall()
        else:
            marks = ", ".join("%s" for _ in state_in)
            rows = self._db.execute(
                f"SELECT * FROM {table} WHERE state IN ({marks}) FOR UPDATE", state_in
            ).fetchall()
        return [dict(r) for r in rows]

    def take_leader(self, name: str, owner: str, lease_for: timedelta) -> int | None:
        locks = self._leader_locks
        if locks is None:
            return super().take_leader(name, owner, lease_for)
        if not locks.hold(name, owner):
            return None  # another live session leads, whatever the lease row says (E27)
        epoch = super().take_leader(name, owner, lease_for)
        if epoch is None:
            locks.drop(name, owner)  # only if held for this owner, never the holder's lock
        else:
            locks.bind(name, owner)
        return epoch

    def release_leader(self, name: str, owner: str) -> bool:
        released = super().release_leader(name, owner)
        if released and self._leader_locks is not None:
            self._leader_locks.drop(name, owner)
        return released

    def _rq_count(self, table: str, org_id: str, state_in: tuple[str, ...] | None) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {table} WHERE org_id = %s"
        args: tuple[str, ...] = (org_id,)
        if state_in is not None:
            sql += f" AND state IN ({', '.join('%s' for _ in state_in)})"
            args += state_in
        row = self._db.execute(sql, args).fetchone()
        assert row is not None
        return int(row["n"])

    def content_subject_rows(self, org_id: str) -> list[ContentSubjectRow]:
        rows = self._db.execute(
            "SELECT content_id, subject, run_id, erase_pending FROM content_subjects "
            "WHERE org_id = %s ORDER BY content_id, subject",
            (org_id,),
        ).fetchall()
        return [
            ContentSubjectRow(r["content_id"], r["subject"], r["run_id"], bool(r["erase_pending"]))
            for r in rows
        ]

    def erase_content_subject(self, org_id: str, subject: str) -> tuple[int, list[str], int]:
        mine = [
            r["content_id"]
            for r in self._db.execute(
                "SELECT DISTINCT content_id FROM content_subjects "
                "WHERE org_id = %s AND subject = %s",
                (org_id, subject),
            ).fetchall()
        ]
        if self.legal_hold(org_id):
            self._db.execute(
                "UPDATE content_subjects SET erase_pending = TRUE "
                "WHERE org_id = %s AND subject = %s",
                (org_id, subject),
            )
            return 0, [], len(mine)
        return (*self._erase_rows(org_id, mine), 0)

    def _erase_rows(self, org_id: str, content_ids: list[str]) -> tuple[int, list[str]]:
        kinds: set[str] = set()
        count = 0
        for cid in content_ids:
            row = self._db.execute(
                "DELETE FROM content WHERE org_id = %s AND id = %s RETURNING kind", (org_id, cid)
            ).fetchone()
            if row is not None:
                kinds.add(row["kind"])
                count += 1
            self._drop_subjects(org_id, cid)
        return count, sorted(kinds)

    def pending_erasure_orgs(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT org_id FROM content_subjects WHERE erase_pending ORDER BY org_id"
        ).fetchall()
        return [r["org_id"] for r in rows]

    def erase_pending_content(self, org_id: str) -> tuple[int, list[str]]:
        due = [
            r["content_id"]
            for r in self._db.execute(
                "SELECT DISTINCT content_id FROM content_subjects "
                "WHERE org_id = %s AND erase_pending",
                (org_id,),
            ).fetchall()
        ]
        return self._erase_rows(org_id, due)

    def legal_hold(self, org_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM content_holds WHERE org_id = %s", (org_id,)
        ).fetchone()
        return row is not None

    def delete_if_expired(self, org_id: str, content_id: str, now: datetime) -> bool:
        cursor = self._db.execute(
            "DELETE FROM content WHERE org_id = %s AND id = %s AND expires_at IS NOT NULL "
            "AND expires_at <= %s AND org_id NOT IN (SELECT org_id FROM content_holds)",
            (org_id, content_id, format_time(now)),
        )
        if cursor.rowcount > 0:
            self._drop_subjects(org_id, content_id)
        return cursor.rowcount > 0

    def key_tombstone(self, org_id: str) -> KeyTombstone | None:
        row = self._db.execute(
            "SELECT * FROM key_tombstones WHERE org_id = %s", (org_id,)
        ).fetchone()
        return None if row is None else _tombstone(row)

    def put_key_tombstone(self, record: KeyTombstone) -> bool:
        return _insert_tombstone(self._db, record)

    def org_has_content(self, org_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM content WHERE org_id = %s LIMIT 1", (org_id,)
        ).fetchone()
        return row is not None

    def org_content_key_ids(self, org_id: str) -> frozenset[str]:
        rows = self._db.execute(
            "SELECT DISTINCT key_id FROM content WHERE org_id = %s", (org_id,)
        ).fetchall()
        return frozenset(r["key_id"] for r in rows)

    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]:
        rows = self._db.execute(_KEYED_SQL, (hash_org_pattern(org_id),)).fetchall()
        return keyed_ids_of((json.loads(r["payload"]) for r in rows), org_id)

    def upsert_security_log_count(self, entry: SecurityLogEntry) -> int:
        row = self._db.execute(
            "SELECT details FROM security_log WHERE org_id = %s AND id = %s",
            (entry.org_id, entry.id),
        ).fetchone()
        if row is None:
            self.append_security_log(entry)
            return int(entry.details.get("count", 1) or 1)
        details = json.loads(row["details"])
        details["count"] = bumped(int(details.get("count", 1)))
        self._db.execute(
            "UPDATE security_log SET details = %s WHERE org_id = %s AND id = %s",
            (json.dumps(details, sort_keys=True, separators=(",", ":")), entry.org_id, entry.id),
        )
        return int(details["count"])

    def set_legal_hold(self, org_id: str, on: bool) -> None:
        if on:
            self._db.execute(
                "INSERT INTO content_holds (org_id) VALUES (%s) ON CONFLICT DO NOTHING", (org_id,)
            )
        else:
            self._db.execute("DELETE FROM content_holds WHERE org_id = %s", (org_id,))

    def create_user(self, user: User) -> None:
        if self.get_user(user.org_id, user.id) is not None:
            raise AlreadyExistsError(f"user '{user.id}' already exists")
        if user.email is not None and self.get_user_by_email(user.org_id, user.email) is not None:
            raise AlreadyExistsError("a user with this email already exists")
        self._db.execute(
            "INSERT INTO users (org_id, id, email, display_name, role, created_at, erased_at, "
            "identifier_key, email_verified_at, email_verified_via) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user.org_id,
                user.id,
                user.email,
                user.display_name,
                user.role.value,
                format_time(user.created_at),
                None if user.erased_at is None else format_time(user.erased_at),
                new_identifier_key(),
                None if user.email_verified_at is None else format_time(user.email_verified_at),
                user.email_verified_via,
            ),
        )

    def get_user(self, org_id: str, user_id: str) -> User | None:
        row = self._db.execute(
            "SELECT * FROM users WHERE org_id = %s AND id = %s", (org_id, user_id)
        ).fetchone()
        return None if row is None else _user(row)

    def list_users(self, org_id: str) -> list[User]:
        rows = self._db.execute(
            "SELECT * FROM users WHERE org_id = %s ORDER BY id", (org_id,)
        ).fetchall()
        return [_user(row) for row in rows]

    def get_user_by_email(self, org_id: str, email: str) -> User | None:
        try:
            wanted = normalize_email(email)
        except ValueError:
            return None
        row = self._db.execute(
            "SELECT * FROM users WHERE org_id = %s AND email = %s", (org_id, wanted)
        ).fetchone()
        return None if row is None else _user(row)

    def erase_user(self, org_id: str, user_id: str, at: datetime) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self._db.execute(
            "UPDATE users SET email = NULL, display_name = NULL, erased_at = %s, "
            "identifier_key = NULL, email_verified_at = NULL, email_verified_via = NULL "
            "WHERE org_id = %s AND id = %s",
            (format_time(at), org_id, user_id),
        )
        self._db.execute(
            "DELETE FROM credentials WHERE org_id = %s AND user_id = %s", (org_id, user_id)
        )
        self.purge_idempotency_actor(org_id, user_id)  # a user id is the actor id (T42)
        return True

    def set_user_role(self, org_id: str, user_id: str, role: Role) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self._db.execute(
            "UPDATE users SET role = %s WHERE org_id = %s AND id = %s",
            (role.value, org_id, user_id),
        )
        return True

    def set_user_email_verified(self, org_id: str, user_id: str, at: datetime, via: str) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self._db.execute(
            "UPDATE users SET email_verified_at = %s, email_verified_via = %s "
            "WHERE org_id = %s AND id = %s",
            (format_time(at), via, org_id, user_id),
        )
        return True

    def set_user_email(self, org_id: str, user_id: str, email: str, at: datetime) -> None:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            raise NotFoundError(f"no active user '{user_id}'")
        holder = self.get_user_by_email(org_id, email)
        if holder is not None and holder.id != user_id:
            raise AlreadyExistsError("a user with this email already exists")
        self._db.execute(
            "UPDATE users SET email = %s, email_verified_at = %s, email_verified_via = 'link' "
            "WHERE org_id = %s AND id = %s",
            (email, format_time(at), org_id, user_id),
        )

    def put_email_change(self, change: EmailChange) -> None:
        if self.get_email_change(change.org_id, change.token_hash) is not None:
            raise AlreadyExistsError("this email change token already exists")
        self._db.execute(
            "INSERT INTO email_changes (org_id, user_id, new_email, token_hash, deliverable, "
            "created_at, expires_at, consumed_at, cancelled_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                change.org_id,
                change.user_id,
                change.new_email,
                change.token_hash,
                1 if change.deliverable else 0,
                format_time(change.created_at),
                format_time(change.expires_at),
                None if change.consumed_at is None else format_time(change.consumed_at),
                None if change.cancelled_at is None else format_time(change.cancelled_at),
            ),
        )

    def get_email_change(self, org_id: str, token_hash: str) -> EmailChange | None:
        row = self._db.execute(
            "SELECT * FROM email_changes WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        ).fetchone()
        return None if row is None else _email_change(row)

    def list_email_changes(self, org_id: str, user_id: str) -> list[EmailChange]:
        rows = self._db.execute(
            "SELECT * FROM email_changes WHERE org_id = %s AND user_id = %s "
            "ORDER BY created_at, token_hash",
            (org_id, user_id),
        ).fetchall()
        return [_email_change(r) for r in rows]

    def cancel_email_changes(
        self, org_id: str, user_id: str, at: datetime, keep_hash: str | None = None
    ) -> int:
        cursor = self._db.execute(
            "UPDATE email_changes SET cancelled_at = %s WHERE org_id = %s AND user_id = %s "
            "AND consumed_at IS NULL AND cancelled_at IS NULL AND token_hash IS DISTINCT FROM %s",
            (format_time(at), org_id, user_id, keep_hash),
        )
        return int(cursor.rowcount)

    def consume_email_change(self, org_id: str, token_hash: str, at: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE email_changes SET consumed_at = %s WHERE org_id = %s AND token_hash = %s "
            "AND consumed_at IS NULL AND cancelled_at IS NULL",
            (format_time(at), org_id, token_hash),
        )
        return int(cursor.rowcount) == 1

    def purge_email_changes(self, now: datetime) -> int:
        cutoff = format_time(now - EMAIL_CHANGE_RETENTION)
        cursor = self._db.execute(
            "DELETE FROM email_changes WHERE "
            "GREATEST(COALESCE(consumed_at, ''), COALESCE(cancelled_at, ''), expires_at) <= %s",
            (cutoff,),
        )
        return int(cursor.rowcount)

    def delete_email_changes_for_user(self, org_id: str, user_id: str) -> int:
        cursor = self._db.execute(
            "DELETE FROM email_changes WHERE org_id = %s AND user_id = %s", (org_id, user_id)
        )
        return int(cursor.rowcount)

    def put_invite(self, invite: Invite) -> None:
        clash = self._db.execute(
            "SELECT 1 FROM invites WHERE org_id = %s AND (id = %s OR token_hash = %s)",
            (invite.org_id, invite.id, invite.token_hash),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("an invite with this id or token already exists")
        self._db.execute(
            "INSERT INTO invites (org_id, id, email, role, token_hash, created_by, created_at, "
            "expires_at, resent_count, accepted_at, accepted_user_id, revoked_at, revoke_reason) "
            "VALUES (" + ", ".join(["%s"] * 13) + ")",
            (
                invite.org_id,
                invite.id,
                invite.email,
                invite.role,
                invite.token_hash,
                invite.created_by,
                format_time(invite.created_at),
                format_time(invite.expires_at),
                invite.resent_count,
                None if invite.accepted_at is None else format_time(invite.accepted_at),
                invite.accepted_user_id,
                None if invite.revoked_at is None else format_time(invite.revoked_at),
                invite.revoke_reason,
            ),
        )

    def get_invite(self, org_id: str, invite_id: str) -> Invite | None:
        row = self._db.execute(
            "SELECT * FROM invites WHERE org_id = %s AND id = %s", (org_id, invite_id)
        ).fetchone()
        return None if row is None else _invite(row)

    def get_invite_by_token_hash(self, org_id: str, token_hash: str) -> Invite | None:
        row = self._db.execute(
            "SELECT * FROM invites WHERE org_id = %s AND token_hash = %s", (org_id, token_hash)
        ).fetchone()
        return None if row is None else _invite(row)

    def list_invites(self, org_id: str) -> list[Invite]:
        rows = self._db.execute(
            "SELECT * FROM invites WHERE org_id = %s ORDER BY created_at DESC, id DESC", (org_id,)
        ).fetchall()
        return [_invite(r) for r in rows]

    def consume_invite(
        self, org_id: str, invite_id: str, token_hash: str, at: datetime, user_id: str
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE invites SET accepted_at = %s, accepted_user_id = %s, email = NULL "
            "WHERE org_id = %s AND id = %s AND token_hash = %s "
            "AND accepted_at IS NULL AND revoked_at IS NULL",
            (format_time(at), user_id, org_id, invite_id, token_hash),
        )
        return int(cursor.rowcount) == 1

    def revoke_invite(self, org_id: str, invite_id: str, at: datetime, reason: str) -> bool:
        cursor = self._db.execute(
            "UPDATE invites SET revoked_at = %s, revoke_reason = %s "
            "WHERE org_id = %s AND id = %s AND accepted_at IS NULL AND revoked_at IS NULL",
            (format_time(at), reason, org_id, invite_id),
        )
        return int(cursor.rowcount) == 1

    def resend_invite(
        self, org_id: str, invite_id: str, token_hash: str, expires_at: datetime
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE invites SET token_hash = %s, expires_at = %s, resent_count = resent_count + 1 "
            "WHERE org_id = %s AND id = %s AND accepted_at IS NULL AND revoked_at IS NULL",
            (token_hash, format_time(expires_at), org_id, invite_id),
        )
        return int(cursor.rowcount) == 1

    def purge_invites(self, now: datetime) -> int:
        cutoff = format_time(now - INVITE_RETENTION)
        cursor = self._db.execute(
            "DELETE FROM invites WHERE "
            "GREATEST(COALESCE(accepted_at, ''), COALESCE(revoked_at, ''), expires_at) <= %s",
            (cutoff,),
        )
        return int(cursor.rowcount)

    def detach_user_from_invites(self, org_id: str, user_id: str) -> int:
        first = self._db.execute(
            "UPDATE invites SET accepted_user_id = NULL "
            "WHERE org_id = %s AND accepted_user_id = %s",
            (org_id, user_id),
        )
        second = self._db.execute(
            "UPDATE invites SET created_by = %s WHERE org_id = %s AND created_by = %s",
            (ERASED_CREATOR, org_id, user_id),
        )
        return int(first.rowcount) + int(second.rowcount)

    # -- password reset (R1.41) ----------------------------------------------------------

    def put_reset_token(self, token: ResetToken) -> None:
        if self._db.execute(
            "SELECT 1 FROM password_reset_tokens WHERE org_id = %s AND token_hash = %s",
            (token.org_id, token.token_hash),
        ).fetchone():
            raise AlreadyExistsError("a reset token with this hash already exists")
        self._db.execute(
            "INSERT INTO password_reset_tokens (org_id, token_hash, user_id, via, created_by, "
            "created_at, expires_at, consumed_at, revoked_at) VALUES (%s, %s, %s, %s, %s, "
            "%s, %s, %s, %s)",
            (
                token.org_id,
                token.token_hash,
                token.user_id,
                token.via,
                token.created_by,
                format_time(token.created_at),
                format_time(token.expires_at),
                None if token.consumed_at is None else format_time(token.consumed_at),
                None if token.revoked_at is None else format_time(token.revoked_at),
            ),
        )

    def consume_reset_token(self, org_id: str, token_hash: str, at: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE password_reset_tokens SET consumed_at = %s WHERE org_id = %s "
            "AND token_hash = %s AND consumed_at IS NULL AND revoked_at IS NULL "
            "AND expires_at > %s",
            (format_time(at), org_id, token_hash, format_time(at)),
        )
        return int(cursor.rowcount) == 1

    def revoke_reset_tokens(
        self, org_id: str, user_id: str, at: datetime, *, keep: str | None = None
    ) -> int:
        cursor = self._db.execute(
            "UPDATE password_reset_tokens SET revoked_at = %s WHERE org_id = %s "
            "AND user_id = %s AND token_hash <> %s AND consumed_at IS NULL "
            "AND revoked_at IS NULL",
            (format_time(at), org_id, user_id, keep or ""),
        )
        return int(cursor.rowcount)

    def put_revoke_link(self, link: RevokeLink) -> None:
        if self._db.execute(
            "SELECT 1 FROM api_token_revoke_links WHERE org_id = %s AND token_hash = %s",
            (link.org_id, link.token_hash),
        ).fetchone():
            raise AlreadyExistsError("a revoke link with this hash already exists")
        self._db.execute(
            "INSERT INTO api_token_revoke_links (org_id, token_hash, user_id, api_token_id, "
            "created_at, expires_at, consumed_at, revoked_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                link.org_id,
                link.token_hash,
                link.user_id,
                link.api_token_id,
                format_time(link.created_at),
                format_time(link.expires_at),
                None if link.consumed_at is None else format_time(link.consumed_at),
                None if link.revoked_at is None else format_time(link.revoked_at),
            ),
        )

    def consume_revoke_link(self, org_id: str, token_hash: str, at: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE api_token_revoke_links SET consumed_at = %s WHERE org_id = %s "
            "AND token_hash = %s AND consumed_at IS NULL AND revoked_at IS NULL "
            "AND expires_at > %s",
            (format_time(at), org_id, token_hash, format_time(at)),
        )
        return int(cursor.rowcount) == 1

    def revoke_revoke_links(self, org_id: str, user_id: str, at: datetime) -> int:
        cursor = self._db.execute(
            "UPDATE api_token_revoke_links SET revoked_at = %s WHERE org_id = %s "
            "AND user_id = %s AND consumed_at IS NULL AND revoked_at IS NULL",
            (format_time(at), org_id, user_id),
        )
        return int(cursor.rowcount)

    def set_password_login_block(self, org_id: str, user_id: str, at: datetime, via: str) -> None:
        # the service role has no UPDATE on this table, so replace = DELETE + INSERT; ON CONFLICT
        # DO NOTHING makes a concurrent second reset a no-op (the row exists) instead of a 500
        self._db.execute(
            "DELETE FROM password_login_blocks WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        )
        self._db.execute(
            "INSERT INTO password_login_blocks (org_id, user_id, blocked_at, via) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (org_id, user_id) DO NOTHING",
            (org_id, user_id, format_time(at), via),
        )

    def clear_password_login_block(self, org_id: str, user_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM password_login_blocks WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        )
        return int(cursor.rowcount) > 0

    def purge_password_reset(self, now: datetime) -> int:
        cutoff = format_time(now - RESET_RETENTION)
        first = self._db.execute(
            "DELETE FROM password_reset_tokens WHERE "
            "GREATEST(expires_at, COALESCE(consumed_at, ''), COALESCE(revoked_at, '')) <= %s",
            (cutoff,),
        )
        second = self._db.execute(
            "DELETE FROM api_token_revoke_links WHERE "
            "GREATEST(expires_at, COALESCE(consumed_at, '')) <= %s",
            (cutoff,),
        )
        return int(first.rowcount) + int(second.rowcount)

    def erase_password_reset(self, org_id: str, user_id: str) -> int:
        changed = 0
        for sql, params in (
            (
                "DELETE FROM password_reset_tokens WHERE org_id = %s AND user_id = %s",
                (org_id, user_id),
            ),
            (
                "DELETE FROM api_token_revoke_links WHERE org_id = %s AND user_id = %s",
                (org_id, user_id),
            ),
            (
                "DELETE FROM password_login_blocks WHERE org_id = %s AND user_id = %s",
                (org_id, user_id),
            ),
            (
                "UPDATE password_reset_tokens SET created_by = %s "
                "WHERE org_id = %s AND created_by = %s",
                (ERASED_CREATOR, org_id, user_id),
            ),
        ):
            changed += int(self._db.execute(sql, params).rowcount)
        return changed

    def get_reset_token(self, org_id: str, token_hash: str) -> ResetToken | None:
        row = self._db.execute(
            "SELECT * FROM password_reset_tokens WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        ).fetchone()
        return None if row is None else _reset_token(row)

    def list_reset_tokens(self, org_id: str) -> list[ResetToken]:
        rows = self._db.execute(
            "SELECT * FROM password_reset_tokens WHERE org_id = %s ORDER BY created_at, token_hash",
            (org_id,),
        ).fetchall()
        return [_reset_token(r) for r in rows]

    def get_revoke_link(self, org_id: str, token_hash: str) -> RevokeLink | None:
        row = self._db.execute(
            "SELECT * FROM api_token_revoke_links WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        ).fetchone()
        return None if row is None else _revoke_link(row)

    def list_revoke_links(self, org_id: str) -> list[RevokeLink]:
        rows = self._db.execute(
            "SELECT * FROM api_token_revoke_links WHERE org_id = %s "
            "ORDER BY created_at, token_hash",
            (org_id,),
        ).fetchall()
        return [_revoke_link(r) for r in rows]

    def get_password_login_block(self, org_id: str, user_id: str) -> PasswordLoginBlock | None:
        row = self._db.execute(
            "SELECT * FROM password_login_blocks WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        ).fetchone()
        return None if row is None else _login_block(row)

    def adopt_security_log_rows(self, org_id: str, email: str, user_id: str) -> int:
        cursor = self._db.execute(
            "UPDATE security_log SET subject = %s WHERE org_id = %s AND subject IS NULL "
            "AND (details::jsonb ->> 'email') = %s",
            (user_id, org_id, email),
        )
        return int(cursor.rowcount)

    def set_user_disabled(self, org_id: str, user_id: str, at: datetime | None) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self._db.execute(
            "UPDATE users SET disabled_at = %s WHERE org_id = %s AND id = %s",
            (None if at is None else format_time(at), org_id, user_id),
        )
        return True

    def put_credential(self, credential: Credential) -> None:
        user = self.get_user(credential.org_id, credential.user_id)
        if user is None or user.erased:
            raise NotFoundError(f"no active user '{credential.user_id}'")
        self._db.execute(
            "INSERT INTO credentials (org_id, user_id, password_hash, updated_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (org_id, user_id) DO UPDATE SET "
            "password_hash = excluded.password_hash, updated_at = excluded.updated_at",
            (
                credential.org_id,
                credential.user_id,
                credential.password_hash,
                format_time(credential.updated_at),
            ),
        )

    def get_credential(self, org_id: str, user_id: str) -> Credential | None:
        row = self._db.execute(
            "SELECT * FROM credentials WHERE org_id = %s AND user_id = %s", (org_id, user_id)
        ).fetchone()
        return None if row is None else _credential(row)

    def put_setup_token(self, token: SetupToken) -> None:
        if self.get_setup_token(token.org_id, token.token_hash) is not None:
            raise AlreadyExistsError("this setup token already exists")
        self._db.execute(
            "INSERT INTO setup_tokens (org_id, token_hash, created_at, expires_at, consumed_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                token.org_id,
                token.token_hash,
                format_time(token.created_at),
                format_time(token.expires_at),
                None if token.consumed_at is None else format_time(token.consumed_at),
            ),
        )

    def get_setup_token(self, org_id: str, token_hash: str) -> SetupToken | None:
        row = self._db.execute(
            "SELECT * FROM setup_tokens WHERE org_id = %s AND token_hash = %s", (org_id, token_hash)
        ).fetchone()
        return None if row is None else _setup_token(row)

    def consume_setup_token(self, org_id: str, token_hash: str, now: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE setup_tokens SET consumed_at = %s WHERE org_id = %s AND token_hash = %s "
            "AND consumed_at IS NULL AND expires_at > %s",
            (format_time(now), org_id, token_hash, format_time(now)),
        )
        return cursor.rowcount == 1

    def put_auth_session(self, session: AuthSession) -> None:
        session.check_login_via()
        if self.get_auth_session(session.org_id, session.id) is not None:
            raise AlreadyExistsError("this session already exists")
        self._db.execute(
            "INSERT INTO auth_sessions (org_id, id, user_id, created_at, last_active_at, "
            "absolute_expires_at, revoked_at, revoke_reason, mfa_verified_at, mfa_method, "
            "login_via, password_confirmed_at, step_up_at) "
            "VALUES (" + ", ".join(["%s"] * 13) + ")",
            (
                session.org_id,
                session.id,
                session.user_id,
                format_time(session.created_at),
                format_time(session.last_active_at),
                format_time(session.absolute_expires_at),
                None if session.revoked_at is None else format_time(session.revoked_at),
                session.revoke_reason,
                None if session.mfa_verified_at is None else format_time(session.mfa_verified_at),
                session.mfa_method,
                session.login_via,
                (
                    None
                    if session.password_confirmed_at is None
                    else format_time(session.password_confirmed_at)
                ),
                None if session.step_up_at is None else format_time(session.step_up_at),
            ),
        )

    def set_auth_session_step_up(self, org_id: str, session_id: str, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("step-up time must carry a timezone")
        cursor = self._db.execute(
            "UPDATE auth_sessions SET step_up_at = %s WHERE org_id = %s AND id = %s "
            "AND revoked_at IS NULL",
            (format_time(at), org_id, session_id),
        )
        return cursor.rowcount == 1

    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        row = self._db.execute(
            "SELECT * FROM auth_sessions WHERE org_id = %s AND id = %s", (org_id, session_id)
        ).fetchone()
        return None if row is None else _auth_session(row)

    def touch_auth_session(self, org_id: str, session_id: str, at: datetime) -> None:
        self._db.execute(
            "UPDATE auth_sessions SET last_active_at = %s WHERE org_id = %s AND id = %s",
            (format_time(at), org_id, session_id),
        )

    def revoke_auth_session(self, org_id: str, session_id: str, at: datetime, reason: str) -> bool:
        cursor = self._db.execute(
            "UPDATE auth_sessions SET revoked_at = %s, revoke_reason = %s "
            "WHERE org_id = %s AND id = %s AND revoked_at IS NULL",
            (format_time(at), reason, org_id, session_id),
        )
        return cursor.rowcount == 1

    def put_auth_token(self, token: AuthToken) -> None:
        if self.get_auth_token(token.org_id, token.token_hash) is not None:
            raise AlreadyExistsError("this token already exists")
        self._db.execute(
            "INSERT INTO auth_tokens (org_id, token_hash, session_id, kind, created_at, "
            "expires_at, used_at) VALUES (" + ", ".join(["%s"] * 7) + ")",
            (
                token.org_id,
                token.token_hash,
                token.session_id,
                token.kind.value,
                format_time(token.created_at),
                format_time(token.expires_at),
                None if token.used_at is None else format_time(token.used_at),
            ),
        )

    def get_auth_token(self, org_id: str, token_hash: str) -> AuthToken | None:
        row = self._db.execute(
            "SELECT * FROM auth_tokens WHERE org_id = %s AND token_hash = %s", (org_id, token_hash)
        ).fetchone()
        return None if row is None else _auth_token(row)

    def use_auth_token(self, org_id: str, token_hash: str, at: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE auth_tokens SET used_at = %s WHERE org_id = %s AND token_hash = %s "
            "AND used_at IS NULL",
            (format_time(at), org_id, token_hash),
        )
        return cursor.rowcount == 1

    def put_auth_failure(self, failure: AuthFailure) -> None:
        self._db.execute(
            "INSERT INTO auth_failures (key, count, last_failure_at, locked_until) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET count = EXCLUDED.count, "
            "last_failure_at = EXCLUDED.last_failure_at, locked_until = EXCLUDED.locked_until",
            (
                failure.key,
                failure.count,
                format_time(failure.last_failure_at),
                None if failure.locked_until is None else format_time(failure.locked_until),
            ),
        )

    def get_auth_failure(self, key: str) -> AuthFailure | None:
        row = self._db.execute("SELECT * FROM auth_failures WHERE key = %s", (key,)).fetchone()
        return None if row is None else _auth_failure(row)

    def delete_auth_failure(self, key: str) -> None:
        self._db.execute("DELETE FROM auth_failures WHERE key = %s", (key,))

    def purge_auth_records(self, sessions_ended_before: datetime, failures_before: datetime) -> int:
        ended = format_time(sessions_ended_before)
        # a session's end is its revocation or its absolute end, whichever is earlier
        gone = (
            "SELECT org_id, id FROM auth_sessions WHERE "
            "LEAST(COALESCE(revoked_at, absolute_expires_at), absolute_expires_at) <= %s"
        )
        tokens = self._db.execute(
            "DELETE FROM auth_tokens WHERE (org_id, session_id) IN (" + gone + ")", (ended,)
        ).rowcount
        sessions = self._db.execute(
            "DELETE FROM auth_sessions WHERE (org_id, id) IN (" + gone + ")", (ended,)
        ).rowcount
        failures = self._db.execute(
            "DELETE FROM auth_failures WHERE GREATEST(last_failure_at, COALESCE(locked_until, "
            "last_failure_at)) <= %s",
            (format_time(failures_before),),
        ).rowcount
        return int(tokens + sessions + failures)

    def set_auth_session_password_confirmed(
        self, org_id: str, session_id: str, at: datetime
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE auth_sessions SET password_confirmed_at = %s WHERE org_id = %s AND id = %s",
            (format_time(at), org_id, session_id),
        )
        return bool(cursor.rowcount == 1)

    def clear_session_proofs(self, org_id: str, user_id: str) -> int:
        cursor = self._db.execute(
            "UPDATE auth_sessions SET password_confirmed_at = NULL, step_up_at = NULL "
            "WHERE org_id = %s AND user_id = %s "
            "AND (password_confirmed_at IS NOT NULL OR step_up_at IS NOT NULL)",
            (org_id, user_id),
        )
        return int(cursor.rowcount)

    # -- MFA (R1.38) --

    def set_auth_session_mfa(self, org_id: str, session_id: str, at: datetime, method: str) -> bool:
        cursor = self._db.execute(
            "UPDATE auth_sessions SET mfa_verified_at = %s, mfa_method = %s "
            "WHERE org_id = %s AND id = %s",
            (format_time(at), method, org_id, session_id),
        )
        return bool(cursor.rowcount == 1)

    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        row = self._db.execute(
            "SELECT * FROM mfa_factors WHERE org_id = %s AND user_id = %s", (org_id, user_id)
        ).fetchone()
        return None if row is None else _mfa_factor(row)

    def put_mfa_factor(self, factor: MfaFactor) -> None:
        # A pending factor is replaced by deleting it and inserting the new row: the service role
        # may update only (secret_enc, key_id, confirmed_at, last_used_step), and PostgreSQL checks
        # UPDATE on every DO UPDATE column even when nothing conflicts (R1.38 B1).
        self._db.execute(
            "DELETE FROM mfa_factors WHERE org_id = %s AND user_id = %s AND confirmed_at IS NULL",
            (factor.org_id, factor.user_id),
        )
        cursor = self._db.execute(
            "INSERT INTO mfa_factors (org_id, user_id, id, secret_enc, key_id, created_at, "
            "confirmed_at, last_used_step, password_checked_at) VALUES ("
            + ", ".join(["%s"] * 9)
            + ") ON CONFLICT (org_id, user_id) DO NOTHING",
            (
                factor.org_id,
                factor.user_id,
                factor.id,
                factor.secret_enc,
                factor.key_id,
                format_time(factor.created_at),
                None if factor.confirmed_at is None else format_time(factor.confirmed_at),
                factor.last_used_step,
                format_time(factor.password_checked_at),
            ),
        )
        if cursor.rowcount != 1:
            raise AlreadyExistsError("this person already has an active factor")

    def activate_mfa_factor(
        self, org_id: str, user_id: str, factor_id: str, at: datetime, step: int | None
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_factors SET confirmed_at = %s, last_used_step = %s "
            "WHERE org_id = %s AND user_id = %s AND id = %s AND confirmed_at IS NULL",
            (format_time(at), step, org_id, user_id, factor_id),
        )
        return bool(cursor.rowcount == 1)

    def advance_mfa_step(self, org_id: str, user_id: str, factor_id: str, step: int) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_factors SET last_used_step = %s WHERE org_id = %s AND user_id = %s "
            "AND id = %s AND (last_used_step IS NULL OR last_used_step < %s)",
            (step, org_id, user_id, factor_id, step),
        )
        return bool(cursor.rowcount == 1)

    def delete_mfa_factor(self, org_id: str, user_id: str) -> bool:
        gone = self._db.execute(
            "DELETE FROM mfa_factors WHERE org_id = %s AND user_id = %s", (org_id, user_id)
        ).rowcount
        self._db.execute(
            "DELETE FROM mfa_recovery_codes WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        )
        return bool(gone == 1)

    def reseal_mfa_factor(
        self,
        org_id: str,
        user_id: str,
        factor_id: str,
        from_key_id: str,
        secret_enc: bytes,
        key_id: str,
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_factors SET secret_enc = %s, key_id = %s "
            "WHERE org_id = %s AND user_id = %s AND id = %s AND key_id = %s",
            (secret_enc, key_id, org_id, user_id, factor_id, from_key_id),
        )
        return bool(cursor.rowcount == 1)

    def delete_mfa_challenges(self, org_id: str, user_id: str) -> int:
        return int(
            self._db.execute(
                "DELETE FROM mfa_challenges WHERE org_id = %s AND user_id = %s", (org_id, user_id)
            ).rowcount
        )

    def erase_mfa_user(self, org_id: str, user_id: str) -> bool:
        existed = self.delete_mfa_factor(org_id, user_id)
        self._db.execute(
            "DELETE FROM mfa_challenges WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        )
        self._db.execute(
            "UPDATE org_auth_settings SET updated_by = 'erased-user' "
            "WHERE org_id = %s AND updated_by = %s",
            (org_id, user_id),
        )
        return existed

    def delete_all_mfa(self, org_id: str) -> int:
        removed = self._db.execute("DELETE FROM mfa_factors WHERE org_id = %s", (org_id,)).rowcount
        self._db.execute("DELETE FROM mfa_recovery_codes WHERE org_id = %s", (org_id,))
        self._db.execute("DELETE FROM mfa_challenges WHERE org_id = %s", (org_id,))
        return int(removed)

    def count_mfa_recovery_codes(self, org_id: str, user_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM mfa_recovery_codes "
            "WHERE org_id = %s AND user_id = %s AND used_at IS NULL",
            (org_id, user_id),
        ).fetchone()
        assert row is not None
        return int(row["n"])

    def replace_mfa_recovery_codes(
        self, org_id: str, user_id: str, code_hashes: Sequence[str], at: datetime
    ) -> None:
        self._db.execute(
            "DELETE FROM mfa_recovery_codes WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        )
        for code_hash in code_hashes:
            self._db.execute(
                "INSERT INTO mfa_recovery_codes (org_id, user_id, code_hash, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (org_id, user_id, code_hash, format_time(at)),
            )

    def use_mfa_recovery_code(
        self, org_id: str, user_id: str, code_hash: str, at: datetime
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_recovery_codes SET used_at = %s WHERE org_id = %s AND user_id = %s "
            "AND code_hash = %s AND used_at IS NULL",
            (format_time(at), org_id, user_id, code_hash),
        )
        return bool(cursor.rowcount == 1)

    def get_mfa_challenge(self, org_id: str, token_hash: str) -> MfaChallenge | None:
        row = self._db.execute(
            "SELECT * FROM mfa_challenges WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        ).fetchone()
        return None if row is None else _mfa_challenge(row)

    def put_mfa_challenge(self, challenge: MfaChallenge) -> None:
        if self.get_mfa_challenge(challenge.org_id, challenge.token_hash) is not None:
            raise AlreadyExistsError("this challenge already exists")
        self._db.execute(
            "INSERT INTO mfa_challenges (org_id, token_hash, user_id, purpose, factor_id, "
            "created_at, expires_at, attempts, consumed_at) VALUES (" + ", ".join(["%s"] * 9) + ")",
            (
                challenge.org_id,
                challenge.token_hash,
                challenge.user_id,
                challenge.purpose,
                challenge.factor_id,
                format_time(challenge.created_at),
                format_time(challenge.expires_at),
                challenge.attempts,
                None if challenge.consumed_at is None else format_time(challenge.consumed_at),
            ),
        )

    def consume_mfa_challenge(self, org_id: str, token_hash: str, now: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_challenges SET consumed_at = %s WHERE org_id = %s AND token_hash = %s "
            "AND consumed_at IS NULL AND expires_at > %s",
            (format_time(now), org_id, token_hash, format_time(now)),
        )
        return bool(cursor.rowcount == 1)

    def add_mfa_challenge_attempt(self, org_id: str, token_hash: str) -> int | None:
        cursor = self._db.execute(
            "UPDATE mfa_challenges SET attempts = attempts + 1 "
            "WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        )
        if cursor.rowcount != 1:
            return None
        row = self._db.execute(
            "SELECT attempts FROM mfa_challenges WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        ).fetchone()
        assert row is not None
        return int(row["attempts"])

    def set_mfa_challenge_factor(self, org_id: str, token_hash: str, factor_id: str) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_challenges SET factor_id = %s WHERE org_id = %s AND token_hash = %s "
            "AND factor_id IS NULL",
            (factor_id, org_id, token_hash),
        )
        return bool(cursor.rowcount == 1)

    def get_org_auth_settings(self, org_id: str) -> OrgAuthSettings:
        row = self._db.execute(
            "SELECT * FROM org_auth_settings WHERE org_id = %s", (org_id,)
        ).fetchone()
        return OrgAuthSettings(org_id=org_id) if row is None else _org_auth_settings(row)

    def put_org_auth_settings(self, settings: OrgAuthSettings) -> None:
        self._db.execute(
            "INSERT INTO org_auth_settings (org_id, mfa_required, updated_at, updated_by) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (org_id) DO UPDATE SET "
            "mfa_required = excluded.mfa_required, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (
                settings.org_id,
                int(settings.mfa_required),
                None if settings.updated_at is None else format_time(settings.updated_at),
                settings.updated_by,
            ),
        )

    def purge_mfa_records(self, now: datetime) -> int:
        challenges = self._db.execute(
            "DELETE FROM mfa_challenges WHERE expires_at <= %s",
            (format_time(now - MFA_CHALLENGE_KEEP),),
        ).rowcount
        factors = self._db.execute(
            "DELETE FROM mfa_factors WHERE confirmed_at IS NULL AND created_at <= %s",
            (format_time(now - MFA_PENDING_LIFETIME),),
        ).rowcount
        return int(challenges + factors)

    # -- idempotency keys (R1.35) --

    def idempotency_get(self, scope: Scope, key_hash: str) -> StoredIdempotency | None:
        row = self._db.execute(
            "SELECT * FROM idempotency_keys WHERE org_id = %s AND actor = %s AND method = %s "
            "AND route = %s AND key_hash = %s FOR UPDATE",
            (scope.org_id, scope.actor, scope.method, scope.route, key_hash),
        ).fetchone()
        if row is None:
            return None
        found = self._db.execute(
            "SELECT subject FROM idempotency_subjects WHERE org_id = %s AND actor = %s "
            "AND method = %s AND route = %s AND key_hash = %s",
            (scope.org_id, scope.actor, scope.method, scope.route, key_hash),
        ).fetchall()
        return _idempotency(row, frozenset(r["subject"] for r in found))

    def idempotency_put(self, row: StoredIdempotency) -> None:
        sealed = row.sealed
        self._db.execute(
            "INSERT INTO idempotency_keys (org_id, actor, method, route, key_hash, "
            "fingerprint, state, attempt, fence, created_at, lease_until, expires_at, "
            "status, payload, nonce, key_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (org_id, actor, method, route, key_hash) DO UPDATE SET "
            "fingerprint = excluded.fingerprint, state = excluded.state, "
            "attempt = excluded.attempt, fence = excluded.fence, "
            "created_at = excluded.created_at, lease_until = excluded.lease_until, "
            "expires_at = excluded.expires_at, status = excluded.status, "
            "payload = excluded.payload, nonce = excluded.nonce, key_id = excluded.key_id",
            (
                row.scope.org_id,
                row.scope.actor,
                row.scope.method,
                row.scope.route,
                row.key_hash,
                row.fingerprint,
                row.state.value,
                row.attempt,
                row.fence,
                format_time(row.created_at),
                format_time(row.lease_until),
                format_time(row.expires_at),
                row.status,
                None if sealed is None else sealed.payload,
                None if sealed is None else sealed.nonce,
                None if sealed is None else sealed.key_id,
            ),
        )
        key = (row.scope.org_id, row.scope.actor, row.scope.method, row.scope.route, row.key_hash)
        self._db.execute(
            "DELETE FROM idempotency_subjects WHERE org_id = %s AND actor = %s "
            "AND method = %s AND route = %s AND key_hash = %s",
            key,
        )
        for person in sorted(row.subjects):
            self._db.execute(
                "INSERT INTO idempotency_subjects (org_id, actor, method, route, key_hash, "
                "subject) VALUES (%s, %s, %s, %s, %s, %s)",
                (*key, person),
            )

    def idempotency_delete(self, scope: Scope, key_hash: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM idempotency_keys WHERE org_id = %s AND actor = %s AND method = %s "
            "AND route = %s AND key_hash = %s",
            (scope.org_id, scope.actor, scope.method, scope.route, key_hash),
        )
        return bool(cursor.rowcount)

    def purge_idempotency_org(self, org_id: str) -> int:
        cursor = self._db.execute("DELETE FROM idempotency_keys WHERE org_id = %s", (org_id,))
        return int(cursor.rowcount)

    def purge_idempotency_actor(self, org_id: str, person: str) -> int:
        cursor = self._db.execute(
            "DELETE FROM idempotency_keys WHERE org_id = %s AND (actor = %s OR "
            "(actor, method, route, key_hash) IN (SELECT actor, method, route, key_hash "
            "FROM idempotency_subjects WHERE org_id = %s AND subject = %s))",
            (org_id, person, org_id, person),
        )
        return int(cursor.rowcount)

    def purge_idempotency_expired(self, now: datetime, limit: int) -> int:
        cursor = self._db.execute(
            "DELETE FROM idempotency_keys WHERE (org_id, actor, method, route, key_hash) IN ("
            "SELECT org_id, actor, method, route, key_hash FROM idempotency_keys "
            "WHERE expires_at <= %s ORDER BY expires_at LIMIT %s)",
            (format_time(now), limit),
        )
        return int(cursor.rowcount)

    def idempotency_rows(self, org_id: str) -> list[StoredIdempotency]:
        rows = self._db.execute(
            "SELECT * FROM idempotency_keys WHERE org_id = %s "
            "ORDER BY actor, method, route, key_hash",
            (org_id,),
        ).fetchall()
        people: dict[tuple[str, str, str, str], set[str]] = {}
        for r in self._db.execute(
            "SELECT actor, method, route, key_hash, subject FROM idempotency_subjects "
            "WHERE org_id = %s",
            (org_id,),
        ).fetchall():
            people.setdefault((r["actor"], r["method"], r["route"], r["key_hash"]), set()).add(
                r["subject"]
            )
        return [
            _idempotency(
                r, frozenset(people.get((r["actor"], r["method"], r["route"], r["key_hash"]), ()))
            )
            for r in rows
        ]

    def append_security_log(self, entry: SecurityLogEntry) -> None:
        self._db.execute(
            "INSERT INTO security_log (org_id, id, at, type, actor, subject, details, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                entry.org_id,
                entry.id,
                format_time(entry.at),
                entry.type,
                entry.actor,
                entry.subject,
                json.dumps(entry.details, sort_keys=True, separators=(",", ":")),
                format_time(entry.expires_at),
            ),
        )

    def get_security_log(self, org_id: str, row_id: str) -> SecurityLogEntry | None:
        row = self._db.execute(
            "SELECT * FROM security_log WHERE org_id = %s AND id = %s", (org_id, row_id)
        ).fetchone()
        return None if row is None else _security_log(row)

    def take_security_log_count(self, org_id: str, row_id: str) -> SecurityLogEntry | None:
        found = self.get_security_log(org_id, row_id)
        if found is not None:
            self._db.execute(
                "DELETE FROM security_log WHERE org_id = %s AND id = %s", (org_id, row_id)
            )
        return found

    def list_expired_security_log(self, now: datetime, reason: str) -> list[SecurityLogEntry]:
        rows = self._db.execute(
            "SELECT * FROM security_log WHERE expires_at <= %s "
            "AND COALESCE((details::json ->> 'reason'), '') = %s ORDER BY at, org_id, id",
            (format_time(now), reason),
        ).fetchall()
        return [_security_log(row) for row in rows]

    def add_security_log_count(
        self, org_id: str, row_id: str, n: int, *, create: SecurityLogEntry | None = None
    ) -> int:
        if n < 1:
            raise StoreError("the count to add must be at least 1")
        stored = self.get_security_log(org_id, row_id)
        if stored is None:
            if create is None:
                raise StoreError("no such security log row to add to")
            count = min(n, MAX_COUNT)
            self.append_security_log(
                create.model_copy(update={"details": {**create.details, "count": count}})
            )
            return count
        count = min(int(stored.details.get("count", 1) or 1) + n, MAX_COUNT)
        details = {**stored.details, "count": count}
        self._db.execute(
            "UPDATE security_log SET details = %s WHERE org_id = %s AND id = %s",
            (json.dumps(details, sort_keys=True, separators=(",", ":")), org_id, row_id),
        )
        return count

    def purge_security_log(self, now: datetime) -> int:
        cursor = self._db.execute(
            "DELETE FROM security_log WHERE expires_at <= %s "
            "AND COALESCE((details::json ->> 'reason'), '') <> '" + HALT_PENDING_REASON + "'",
            (format_time(now),),
        )
        return int(cursor.rowcount)

    def user_identifier_key(self, org_id: str, user_id: str) -> bytes | None:
        row = self._db.execute(
            "SELECT identifier_key, erased_at FROM users WHERE org_id = %s AND id = %s",
            (org_id, user_id),
        ).fetchone()
        if row is None or row["erased_at"] is not None:
            return None
        if row["identifier_key"] is not None:
            return bytes(row["identifier_key"])
        key = new_identifier_key()  # a user from before migration 7
        self._db.execute(
            "UPDATE users SET identifier_key = %s WHERE org_id = %s AND id = %s",
            (key, org_id, user_id),
        )
        return key

    def org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        found = self._db.execute(
            "SELECT key FROM identifier_keys WHERE org_id = %s AND epoch = %s",
            (org_id, epoch),
        ).fetchone()
        if found is not None:  # a key, or the tombstone of a destroyed one (None)
            return None if found["key"] is None else bytes(found["key"])
        newer = self._db.execute(
            "SELECT 1 FROM identifier_keys WHERE org_id = %s AND epoch > %s", (org_id, epoch)
        ).fetchone()
        if newer is not None:
            raise StoreError("no identifier key is made for an epoch older than the newest")
        self._db.execute(
            "INSERT INTO identifier_keys (org_id, epoch, key, created_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (org_id, epoch) DO NOTHING",
            (org_id, epoch, new_identifier_key(), format_time(self._clock())),
        )
        row = self._db.execute(
            "SELECT key FROM identifier_keys WHERE org_id = %s AND epoch = %s",
            (org_id, epoch),
        ).fetchone()
        return None if row is None or row["key"] is None else bytes(row["key"])

    def destroy_org_identifier_keys(self, before_epoch: int) -> int:
        cursor = self._db.execute(
            "UPDATE identifier_keys SET key = NULL, destroyed_at = %s "
            "WHERE epoch < %s AND key IS NOT NULL",
            (format_time(self._clock()), before_epoch),
        )
        return int(cursor.rowcount)

    def get_org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        row = self._db.execute(
            "SELECT key FROM identifier_keys WHERE org_id = %s AND epoch = %s",
            (org_id, epoch),
        ).fetchone()
        return None if row is None or row["key"] is None else bytes(row["key"])

    def erase_security_log(self, org_id: str, user_id: str, email: str | None) -> int:
        cursor = self._db.execute(
            "DELETE FROM security_log WHERE org_id = %s AND (subject = %s OR "
            "(details::jsonb ->> 'email') = %s)",
            (org_id, user_id, email),
        )
        self._db.execute(
            "UPDATE security_log SET actor = %s, details = "
            "(details::jsonb - %s::text[])::text WHERE org_id = %s AND actor = %s",
            (ERASED_ACTOR, list(ERASED_DETAIL_KEYS), org_id, user_id),
        )
        return int(cursor.rowcount)

    def put_api_token(self, token: ApiToken) -> None:
        clash = self._db.execute(
            "SELECT 1 FROM api_tokens WHERE org_id = %s "
            "AND (id = %s OR name = %s OR token_hash = %s)",
            (token.org_id, token.id, token.name, token.token_hash),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("this token already exists")
        self._db.execute(
            "INSERT INTO api_tokens (org_id, id, name, scopes, token_hash, created_by, created_at, "
            "expires_at, revoked_at, revoked_by) VALUES (" + ", ".join(["%s"] * 10) + ")",
            (
                token.org_id,
                token.id,
                token.name,
                ",".join(sorted(s.value for s in token.scopes)),
                token.token_hash,
                token.created_by,
                format_time(token.created_at),
                format_time(token.expires_at),
                None if token.revoked_at is None else format_time(token.revoked_at),
                token.revoked_by,
            ),
        )

    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None:
        row = self._db.execute(
            "SELECT * FROM api_tokens WHERE org_id = %s AND id = %s", (org_id, token_id)
        ).fetchone()
        return None if row is None else _api_token(row)

    def revoke_api_token(self, org_id: str, token_id: str, at: datetime, by: str) -> bool:
        cursor = self._db.execute(
            "UPDATE api_tokens SET revoked_at = %s, revoked_by = %s "
            "WHERE org_id = %s AND id = %s AND revoked_at IS NULL",
            (format_time(at), by, org_id, token_id),
        )
        return cursor.rowcount == 1

    def rename_api_token(self, org_id: str, token_id: str, name: str) -> bool:
        if (
            self._db.execute(
                "SELECT 1 FROM api_tokens WHERE org_id = %s AND id = %s", (org_id, token_id)
            ).fetchone()
            is None
        ):
            return False
        clash = self._db.execute(
            "SELECT 1 FROM api_tokens WHERE org_id = %s AND name = %s AND id <> %s",
            (org_id, name, token_id),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("a token with this name exists")
        self._db.execute(
            "UPDATE api_tokens SET name = %s WHERE org_id = %s AND id = %s",
            (name, org_id, token_id),
        )
        return True

    def delete_api_token(self, org_id: str, token_id: str, now: datetime) -> bool:
        row = self._db.execute(
            "SELECT api_token_delete(%s, %s, %s) AS n", (org_id, token_id, now)
        ).fetchone()
        assert row is not None
        return int(row["n"]) == 1

    def erase_api_token_issuer(self, org_id: str, user_id: str) -> int:
        row = self._db.execute(
            "SELECT api_token_erase_issuer(%s, %s) AS n", (org_id, user_id)
        ).fetchone()
        assert row is not None
        return int(row["n"])

    def normalise_api_token_issuer(self, org_id: str, token_id: str) -> int:
        row = self._db.execute(
            "SELECT api_token_normalise_issuer(%s, %s) AS n", (org_id, token_id)
        ).fetchone()
        assert row is not None
        return int(row["n"])

    def api_token_name_retired(self, org_id: str, keyed: Sequence[tuple[int, str]]) -> bool:
        for epoch, name_hash in keyed:
            found = self._db.execute(
                "SELECT 1 FROM api_token_retired_names "
                "WHERE org_id = %s AND epoch = %s AND name_hash = %s",
                (org_id, epoch, name_hash),
            ).fetchone()
            if found is not None:
                return True
        return False

    def retire_api_token_name(
        self, org_id: str, name_hash: str, epoch: int, retired_at: datetime
    ) -> None:
        self._db.execute(
            "INSERT INTO api_token_retired_names (org_id, epoch, name_hash, retired_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (org_id, epoch, name_hash, format_time(retired_at)),
        )

    def ext_get(
        self, org_id: str, namespace: str, kind: str, record_id: str
    ) -> ExtensionRecord | None:
        row = self._db.execute(
            "SELECT * FROM extension_records WHERE org_id = %s AND namespace = %s AND kind = %s "
            "AND record_id = %s",
            (org_id, namespace, kind, record_id),
        ).fetchone()
        return None if row is None else _extension(row)

    def ext_find(
        self, org_id: str, namespace: str, kind: str, unique_key: str
    ) -> ExtensionRecord | None:
        row = self._db.execute(
            "SELECT * FROM extension_records WHERE org_id = %s AND namespace = %s AND kind = %s "
            "AND unique_key = %s",
            (org_id, namespace, kind, unique_key),
        ).fetchone()
        return None if row is None else _extension(row)

    def ext_put(self, record: ExtensionRecord) -> None:
        clash = self._db.execute(
            "SELECT 1 FROM extension_records WHERE org_id = %s AND namespace = %s AND kind = %s "
            "AND (record_id = %s OR (unique_key IS NOT NULL AND unique_key = %s))",
            (record.org_id, record.namespace, record.kind, record.record_id, record.unique_key),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("this record already exists")
        self._db.execute(
            "INSERT INTO extension_records (org_id, namespace, kind, record_id, unique_key, "
            "group_key, sort_key, erasable, body) VALUES (" + ", ".join(["%s"] * 9) + ")",
            (
                record.org_id,
                record.namespace,
                record.kind,
                record.record_id,
                record.unique_key,
                record.group_key,
                record.sort_key,
                record.erasable,
                record.body,
            ),
        )

    def ext_erase(self, org_id: str) -> int:
        # no DELETE grant on extension_records (ND314): the SECURITY DEFINER function deletes
        # only the erasable rows of this one organization
        row = self._db.execute("SELECT extension_records_erase(%s) AS n", (org_id,)).fetchone()
        return int(row["n"]) if row is not None else 0

    def put_agent_credential(self, credential: AgentCredential) -> None:
        clash = self._db.execute(
            "SELECT 1 FROM agent_credentials WHERE org_id = %s AND (id = %s OR token_hash = %s)",
            (credential.org_id, credential.id, credential.token_hash),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("this credential already exists")
        self._db.execute(
            "INSERT INTO agent_credentials (org_id, id, agent_id, team, fingerprint, scopes, "
            "token_hash, created_by, created_at, expires_at, revoked_at, revoked_by) VALUES ("
            + ", ".join(["%s"] * 12)
            + ")",
            (
                credential.org_id,
                credential.id,
                credential.agent_id,
                credential.team,
                credential.fingerprint,
                ",".join(sorted(s.value for s in credential.scopes)),
                credential.token_hash,
                credential.created_by,
                format_time(credential.created_at),
                format_time(credential.expires_at),
                None if credential.revoked_at is None else format_time(credential.revoked_at),
                credential.revoked_by,
            ),
        )

    def get_agent_credential(self, org_id: str, credential_id: str) -> AgentCredential | None:
        row = self._db.execute(
            "SELECT * FROM agent_credentials WHERE org_id = %s AND id = %s",
            (org_id, credential_id),
        ).fetchone()
        return None if row is None else _agent_credential(row)

    def list_agent_credentials(self, org_id: str) -> list[AgentCredential]:
        rows = self._db.execute(
            "SELECT * FROM agent_credentials WHERE org_id = %s ORDER BY created_at, id", (org_id,)
        ).fetchall()
        return [_agent_credential(row) for row in rows]

    def revoke_agent_credential(
        self, org_id: str, credential_id: str, at: datetime, by: str
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE agent_credentials SET revoked_at = %s, revoked_by = %s "
            "WHERE org_id = %s AND id = %s AND revoked_at IS NULL",
            (format_time(at), by, org_id, credential_id),
        )
        return cursor.rowcount == 1

    def shorten_agent_credential(
        self, org_id: str, credential_id: str, expires_at: datetime
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE agent_credentials SET expires_at = %s "
            "WHERE org_id = %s AND id = %s AND expires_at > %s",
            (format_time(expires_at), org_id, credential_id, format_time(expires_at)),
        )
        return cursor.rowcount == 1

    def count_live_agent_credentials(self, org_id: str, agent_id: str, now: datetime) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM agent_credentials WHERE org_id = %s AND agent_id = %s "
            "AND revoked_at IS NULL AND expires_at > %s",
            (org_id, agent_id, format_time(now)),
        ).fetchone()
        assert row is not None
        return int(row["n"])

    def erase_agent_credential_user(
        self, org_id: str, user_id: str, now: datetime
    ) -> list[AgentCredential]:
        rows = self._db.execute(
            "SELECT * FROM agent_credentials WHERE org_id = %s AND created_by = %s "
            "AND revoked_at IS NULL AND expires_at > %s ORDER BY created_at, id",
            (org_id, user_id, format_time(now)),
        ).fetchall()
        live = [_agent_credential(row) for row in rows]
        self._db.execute(
            "UPDATE agent_credentials SET created_by = %s WHERE org_id = %s AND created_by = %s",
            (ERASED_ACTOR, org_id, user_id),
        )
        self._db.execute(
            "UPDATE agent_credentials SET revoked_by = %s WHERE org_id = %s AND revoked_by = %s",
            (ERASED_ACTOR, org_id, user_id),
        )
        return live

    def purge_agent_credentials(self, now: datetime) -> int:
        cutoff = format_time(now - AGENT_CREDENTIAL_ENDED_RETENTION)
        cursor = self._db.execute(
            "DELETE FROM agent_credentials "
            "WHERE LEAST(COALESCE(revoked_at, expires_at), expires_at) <= %s",
            (cutoff,),
        )
        return int(cursor.rowcount)

    def get_approval(self, approval_id: str) -> StoredApproval | None:
        row = self._db.execute(
            "SELECT data, version FROM approvals WHERE id = %s", (approval_id,)
        ).fetchone()
        return None if row is None else _stored_approval(row)

    def delete_approval_if_due(
        self, approval_id: str, now: datetime, *, expected_version: int | None = None
    ) -> bool:
        row = self._db.execute(
            "SELECT data, version FROM approvals WHERE id = %s", (approval_id,)
        ).fetchone()
        if row is None:
            return False
        stored = _stored_approval(row)
        if not stored.approval.retention_over(now):
            return False
        if expected_version is not None and stored.version != expected_version:
            return False
        # no DELETE grant on approvals (ND289): the SECURITY DEFINER function re-checks the status,
        # the retention window and the version, then deletes the row in this transaction
        found = self._db.execute(
            "SELECT approval_delete_if_due(%s, %s, %s) AS gone", (approval_id, stored.version, now)
        ).fetchone()
        return bool(found is not None and found["gone"])

    def put_approval(self, approval: Approval, *, expected_version: int | None) -> int:
        return _put_versioned(
            self._db,
            "approvals",
            approval.id,
            approval.model_dump_json(),
            expected_version,
            _approval_columns(approval),
        )

    def solo_approvals_after(self, decided_by: str, after: datetime) -> list[Approval]:
        rows = self._db.execute(
            "SELECT data, version FROM approvals WHERE decided_by = %s "
            "AND approval_mode = 'solo' AND decided_at > %s "
            "AND status IN ('approved', 'consumed') ORDER BY decided_at, id",
            (decided_by, format_time(after)),
        ).fetchall()
        return [_stored_approval(row).approval for row in rows]

    def approvals_matching(
        self, run_id: str, agent_id: str, status: ApprovalStatus
    ) -> list[StoredApproval]:
        rows = self._db.execute(
            "SELECT data, version FROM approvals WHERE run_id = %s AND agent_id = %s "
            "AND status = %s ORDER BY id",
            (run_id, agent_id, status.value),
        ).fetchall()
        return [_stored_approval(row) for row in rows]

    def get_reservation(self, reservation_id: str) -> StoredReservation | None:
        row = self._db.execute(
            "SELECT data, version FROM reservations WHERE id = %s", (reservation_id,)
        ).fetchone()
        return None if row is None else _stored_reservation(row)

    def put_reservation(self, reservation: Reservation, *, expected_version: int | None) -> int:
        return _put_versioned(
            self._db,
            "reservations",
            reservation.id,
            reservation.model_dump_json(),
            expected_version,
            {"run_id": reservation.context.run_id, "status": reservation.status.value},
        )


def _provider_role(given: str | None) -> str:
    """The provider role of ND326: the argument, else `NURAMESH_PG_PROVIDER_ROLE`, else none
    (no default). A role name is at most 63 bytes and holds no control character."""
    name = (os.environ.get(PROVIDER_ROLE_ENV, "") if given is None else given).strip()
    if len(name.encode()) > 63 or any(ord(c) < 32 for c in name):
        raise StoreError(f"{PROVIDER_ROLE_ENV} must be a PostgreSQL role name (63 bytes at most)")
    return name


class PostgresStore(Store):
    """State and ledger in one PostgreSQL database (module docstring).

    `migrate=False` is the **check-only** open (R1.29, spec 3.2): it never runs DDL (the service
    role cannot) and raises `SchemaOutOfDate` unless the schema version equals the code's."""

    def __init__(
        self,
        dsn: str,
        *,
        clock: Clock = utc_now,
        lock_timeout_ms: int = 10_000,
        migrate: bool = True,
        provider_role: str | None = None,
        fence_clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self._provider_role = _provider_role(provider_role)
        self._fence_clock = fence_clock  # None: leases use the database clock (tests may inject)
        self.subject_backfill: SubjectBackfill | None = None  # set by migration 8
        self._lock = threading.RLock()
        self._clean_streams: set[str] = set()  # streams checked for a damaged time (ND238)
        self._local = threading.local()
        self._lock_timeout = int(lock_timeout_ms)
        self._dsn = dsn
        self._leader_locks = _LeaderLocks(self._connect_lock)
        self._db: psycopg.Connection[dict[str, Any]] = self._connect()
        try:
            if migrate:
                self._migrate()
            else:
                self._check_schema()
            self._schema = self._current_version()
            self._record_provider_role_use(self._check_definer_role())
        except psycopg.Error as exc:
            self._db.close()
            raise _wrap(exc) from exc
        except BaseException:
            self._db.close()
            raise

    @staticmethod
    def has_schema(dsn: str) -> bool:
        """Whether the database already holds the ledger tables. Read-only: never creates or
        migrates anything (a read-only command uses it to refuse an empty database)."""
        try:
            with psycopg.connect(dsn, autocommit=True) as db:
                row = db.execute("SELECT to_regclass('ledger_events') IS NOT NULL").fetchone()
        except psycopg.Error as exc:
            raise StoreError(f"could not connect to the database ({type(exc).__name__})") from None
        return bool(row and row[0])

    def _connect(self, **extra: Any) -> "psycopg.Connection[dict[str, Any]]":
        try:
            return psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row, **extra)
        except psycopg.Error as exc:
            # libpq messages can quote the connection string, which holds the password
            raise StoreError(f"could not connect to the database ({type(exc).__name__})") from None

    def _connect_lock(self) -> "psycopg.Connection[dict[str, Any]]":
        """A new dedicated connection for the leader lock: client and server keepalives."""
        conn = self._connect(**LEADER_LOCK_CONNECT_ARGS)
        try:
            _apply_lock_session_settings(conn)
        except psycopg.Error:
            conn.close()
            raise StoreError("could not prepare the leader-lock connection") from None
        return conn

    def _ensure_connection(self) -> None:
        """Re-open the connection if it was lost between operations (not inside a unit of work)."""
        if self._db.closed or self._db.broken:
            self._db = self._connect()

    # -- schema ------------------------------------------------------------------------------

    def _current_version(self) -> int:
        exists = self._db.execute("SELECT to_regclass('schema_version') AS t").fetchone()
        if exists is None or exists["t"] is None:
            return 0
        row = self._db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int((row or {}).get("v") or 0)

    def _definer_oid(self) -> int | None:
        """MA1 (ND326 amendment): the definer role is found as the OWNER of the SECURITY DEFINER
        functions the migrations create, never by name, and must pass the migration's attribute
        check. Returns its oid, or None before the schema that creates the functions. Refuses
        when a function is missing, the functions have more than one owner, the owner is not
        exactly `nuramesh_definer`, or the owner can log in or holds a dangerous privilege
        (a decoy that took the name, or a real definer renamed or altered, cannot pass)."""
        due = [name for name, since in DEFINER_FUNCTIONS if self._schema >= since]
        if not due:
            return None
        owners = self._db.execute(
            "SELECT p.proname AS fn, p.proowner::bigint AS oid, "
            "pg_catalog.pg_get_userbyid(p.proowner) AS name "
            "FROM pg_catalog.pg_proc p "
            "WHERE p.pronamespace = 'public'::regnamespace AND p.proname = ANY(%s) "
            "ORDER BY 1, 2",
            (due,),
        ).fetchall()
        missing = sorted(set(due) - {r["fn"] for r in owners})
        if missing:
            raise StoreError(
                "refusing to start: the database is at a schema that has the functions "
                f"{', '.join(missing)}, which are missing (dropped or replaced?). Restore them "
                "with `nuramesh db migrate` on a restored backup, or see docs/upgrade-notes.md."
            )
        names = sorted({r["name"] for r in owners})
        if names != [DEFINER_ROLE]:
            raise StoreError(
                f"refusing to start: the functions that delete data must be owned by exactly "
                f"{DEFINER_ROLE}, but their owner(s): {', '.join(names)} (was the role renamed, "
                "or a role of the same name created in its place?). Do not trust a role that "
                "holds SET on the real owner; see docs/upgrade-notes.md."
            )
        oid = int(owners[0]["oid"])
        r = self._db.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE oid = %s::oid",
            (oid,),
        ).fetchone()
        if r is None or any(
            r[k]
            for k in (
                "rolcanlogin",
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise StoreError(
                "refusing to start: the role nuramesh_definer owns the functions that delete "
                "data but is not a plain NOLOGIN role (canlogin, super, createdb, createrole, "
                "replication or bypassrls is set). Check who changed it (pg_auth_members), then "
                "as a database superuser: DROP ROLE nuramesh_definer after moving the "
                "functions, or ALTER ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS (docs/upgrade-notes.md)."
            )
        return oid

    def _check_definer_role(self) -> list[dict[str, str]]:
        """Every open (ND314c): nobody but the migration owner may act as `nuramesh_definer`.

        The migration checks this once; a member added later (an ADMIN holder grants itself SET on
        PostgreSQL 16) would otherwise go unseen. Any member other than the owner of the schema
        tables is refused, whatever its options, and so is a definer that belongs to another role.
        One exception (ND326): on PostgreSQL 16 an ADMIN-only row (no SET, no INHERIT) whose
        grantor is exactly the role named in `NURAMESH_PG_PROVIDER_ROLE` (no default) is allowed
        and returned, so the caller records it. The role is found as the owner of
        the definer functions and checked by `_definer_oid` (a rename, a decoy or a changed
        attribute is refused). The fix text is built in SQL
        with `format('%I', ...)`, so a role name cannot end the statement."""
        definer = self._definer_oid()
        if definer is None:
            return []
        pg16 = (
            int(
                (self._db.execute("SHOW server_version_num").fetchone() or {"v": "0"})[
                    "server_version_num"
                ]
            )
            >= 160000
        )
        flags = (
            "m.admin_option AND NOT m.set_option AND NOT m.inherit_option AS admin_only, "
            "pg_catalog.format('REVOKE nuramesh_definer FROM %%I GRANTED BY %%I;', "
            "pg_catalog.pg_get_userbyid(m.member), pg_catalog.pg_get_userbyid(m.grantor)) AS fix, "
            if pg16
            else "false AS admin_only, "
            "pg_catalog.format('REVOKE nuramesh_definer FROM %%I;', "
            "pg_catalog.pg_get_userbyid(m.member)) AS fix, "
        )
        rows = self._db.execute(
            "SELECT pg_catalog.pg_get_userbyid(m.member) AS member, "
            "pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(m.member)) AS qmember, "
            "pg_catalog.pg_get_userbyid(m.grantor) AS grantor, " + flags.rstrip(", ") + " "
            "FROM pg_catalog.pg_auth_members m "
            "WHERE m.roleid = %s::oid "
            "AND m.member <> COALESCE((SELECT relowner FROM pg_catalog.pg_class "
            "WHERE oid = pg_catalog.to_regclass('public.schema_version')), 0) "
            "ORDER BY 1, 3",
            (definer,),
        ).fetchall()
        parent_fix = (
            "pg_catalog.format('REVOKE %%I FROM nuramesh_definer GRANTED BY %%I;', "
            "pg_catalog.pg_get_userbyid(m.roleid), pg_catalog.pg_get_userbyid(m.grantor))"
            if pg16
            else "pg_catalog.format('REVOKE %%I FROM nuramesh_definer;', "
            "pg_catalog.pg_get_userbyid(m.roleid))"
        )
        parents = self._db.execute(
            f"SELECT {parent_fix} AS fix "
            "FROM pg_catalog.pg_auth_members m "
            "WHERE m.member = %s::oid ORDER BY 1",
            (definer,),
        ).fetchall()
        provider = self._provider_role
        allowed = [r for r in rows if provider and r["admin_only"] and r["grantor"] == provider]
        refused = [r for r in rows if r not in allowed]
        if not refused and not parents:
            return [
                {
                    "kind": "provider_role_exception",
                    "role": provider,
                    "grantor": r["grantor"],
                    "member": r["member"],
                }
                for r in allowed
            ]
        fixes = [r["fix"] for r in refused] + [r["fix"] for r in parents]
        who = ", ".join(r["qmember"] for r in refused) or "(none)"
        hint = (
            f" If this is a managed PostgreSQL whose cloud admin holds an ADMIN-only row granted "
            f"by the provider's own role, set {PROVIDER_ROLE_ENV} to that role (the provider's "
            "bootstrap superuser: azuresu on Azure Flexible Server, rdsadmin on AWS RDS; "
            "it has no default; "
            "docs/configuration-env.md); every other member is still refused."
            if not provider
            else ""
        )
        raise StoreError(
            "refusing to start: the role nuramesh_definer owns the functions that delete data, "
            f"and a role other than the migration owner can act as it (members: {who}; "
            f"parent roles: {len(parents)}). Any membership counts, ADMIN-only included, because "
            "an ADMIN holder can grant itself SET. As a database superuser run: "
            + " ".join(fixes)
            + hint
            + " If a login other than your operator created this role, do not keep it: "
            "DROP ROLE nuramesh_definer after checking pg_auth_members, then create it again "
            "(docs/upgrade-notes.md)."
        )

    def record_migration_acknowledgement(self, payload: dict[str, Any]) -> None:
        """Issue 897: the backup acknowledgement given for a non-additive migration is one
        `DECISION_RECORDED` entry in the startup stream (like the provider-role entry)."""
        with self.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=STARTUP_STREAM,
                    type=EventType.DECISION_RECORDED,
                    actor="system",
                    payload=payload,
                )
            )

    def _record_provider_role_use(self, used: list[dict[str, str]]) -> None:
        """ND326: every start that relies on the provider-role exception writes one ledger entry
        per allowed member (role, grantor, member), before the store is handed out."""
        if not used:
            return
        with self.transaction() as uow:
            for payload in used:
                uow.append(
                    EventDraft(
                        stream=STARTUP_STREAM,
                        type=EventType.DECISION_RECORDED,
                        actor="system",
                        payload=payload,
                    )
                )

    def _check_schema(self) -> None:
        current = self._current_version()
        if current > len(MIGRATIONS):
            raise SchemaOutOfDate(
                f"database schema {current} is newer than this version ({len(MIGRATIONS)})"
            )
        if current < len(MIGRATIONS):
            raise SchemaOutOfDate(
                f"database schema {current} is behind this version ({len(MIGRATIONS)}); "
                "run `nuramesh db migrate`"
            )

    def _migrate(self) -> None:
        backfill: SubjectBackfill | None = None
        if self._current_version() == len(MIGRATIONS):
            return  # up to date: a role without DDL rights (R1.36 anchor role) can open the store
        with self._lock, self._db.transaction():
            self._db.execute(f"SET LOCAL lock_timeout = {self._lock_timeout}")
            self._db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            # the migration's member check reads it; always set, so a per-role default of the same
            # name in the database cannot stand in for the operator's choice
            self._db.execute(
                "SELECT pg_catalog.set_config('nuramesh.provider_role', %s, true)",
                (self._provider_role,),
            )
            self._db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            current = self._current_version()  # re-checked under the lock
            if current > len(MIGRATIONS):
                raise StoreError(
                    f"database schema {current} is newer than this version ({len(MIGRATIONS)})"
                )
            for version in range(current + 1, len(MIGRATIONS) + 1):
                self._db.execute(MIGRATIONS[version - 1].encode())
                if version == PROJECTION_MIGRATION:
                    _rebuild_projection(self._db)
                if version == IDENTIFIER_KEY_MIGRATION:
                    _give_users_identifier_keys(self._db)
                if version == SUBJECT_MIGRATION:
                    backfill = _backfill_security_subjects(self._db)
                if version == BUDGET_ORG_MIGRATION:
                    _key_budgets_by_organization(self._db)
                if version == TOMBSTONE_MIGRATION:
                    _tombstone_and_identify(self._db)
                if version == TOMBSTONE_APPROX_MIGRATION:
                    _backfill_approximate_tombstones(self._db, self._clock)
                if version == INVITES_MIGRATION:
                    _verify_legacy_users(self._db, self._clock)
                self._db.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))
        if backfill is not None:  # reported only once the migration is committed
            self.subject_backfill = backfill
            backfill.report()

    @property
    def schema_version(self) -> int:
        """The applied schema version."""
        with self._lock:
            return self._current_version()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._leader_locks.close()
            self._db.close()

    # -- writes ------------------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[UnitOfWork]:
        if getattr(self._local, "active", False):
            raise NestedTransactionError("units of work don't nest")
        with self._lock:
            self._local.active = True
            try:
                try:
                    self._ensure_connection()
                    with self._db.transaction():
                        # the design relies on READ COMMITTED: each statement after the lock sees
                        # what the previous writer committed (a database default may differ)
                        self._db.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                        self._db.execute(f"SET LOCAL lock_timeout = {self._lock_timeout}")
                        self._db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
                        yield _PostgresUnitOfWork(
                            self._db,
                            self._clock,
                            self._schema,
                            self._clean_streams,
                            self._leader_locks,
                            self._fence_clock,
                        )
                except psycopg.OperationalError as exc:
                    if self._db.closed or self._db.broken:
                        raise StoreError(
                            "the database connection was lost; if it happened while committing, "
                            "the outcome is unknown: check before retrying"
                        ) from exc
                    raise _wrap(exc) from exc
                except psycopg.Error as exc:
                    raise _wrap(exc) from exc
            finally:
                self._local.active = False

    # -- reads -------------------------------------------------------------------------------

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            try:
                self._ensure_connection()
                return self._db.execute(sql, params).fetchall()
            except psycopg.Error as exc:
                raise _wrap(exc) from exc

    def org_content_key_ids(self, org_id: str) -> frozenset[str]:
        rows = self._query("SELECT DISTINCT key_id FROM content WHERE org_id = %s", (org_id,))
        return frozenset(r["key_id"] for r in rows)

    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]:
        rows = self._query(_KEYED_SQL, (hash_org_pattern(org_id),))
        return keyed_ids_of((json.loads(r["payload"]) for r in rows), org_id)

    def read_stream(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterator[LedgerEvent]:
        check_limit(limit)
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = %s AND seq > %s "
            "ORDER BY seq LIMIT %s",
            (stream, after_seq, limit),
        )
        return iter([_event_from_row(row) for row in rows])

    def read_stream_positioned(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterator[tuple[int, LedgerEvent]]:
        check_limit(limit)
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS}, COALESCE(commit_pos, 0) AS commit_pos FROM ledger_events "
            "WHERE stream = %s AND seq > %s ORDER BY seq LIMIT %s",
            (stream, after_seq, limit),
        )
        return iter([(row["commit_pos"], _event_from_row(row)) for row in rows])

    def commit_position(self) -> int:
        rows = self._query("SELECT MAX(commit_pos) AS p FROM ledger_events")
        return int(rows[0]["p"] or 0)

    def entries_after_position(
        self, pos: int, limit: int | None = None
    ) -> list[tuple[int, LedgerEvent]]:
        check_limit(limit)
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS}, COALESCE(commit_pos, 0) AS commit_pos FROM ledger_events "
            "WHERE commit_pos > %s ORDER BY commit_pos LIMIT %s",
            (pos, limit),
        )
        return [(row["commit_pos"], _event_from_row(row)) for row in rows]

    def autonomy_events(
        self,
        org_id: str,
        kinds: Collection[str] | None = None,
        *,
        streams: ProjectionStreams = "all",
    ) -> list[tuple[int, LedgerEvent]]:
        with self._lock:
            try:
                self._ensure_connection()
                with self._db.transaction():  # one read transaction: rows and counts agree
                    self._db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    return _read_projection(self._db, org_id, kinds, projection_streams(streams))
            except psycopg.Error as exc:
                raise _wrap(exc) from exc

    def projection_problems(self) -> list[str]:
        with self._lock:
            try:
                self._ensure_connection()
                with self._db.transaction():
                    self._db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    return _projection_problems(self._db)
            except psycopg.Error as exc:
                raise _wrap(exc) from exc

    def rebuild_projection(self) -> int:
        with self.transaction() as unit:
            assert isinstance(unit, _PostgresUnitOfWork)
            return _rebuild_projection(unit._db)

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = %s AND task_id = %s "
            "ORDER BY seq",
            (run_stream(run_id), task_id),
        )
        return [_event_from_row(row) for row in rows]

    def budget_definitions(self) -> list[LedgerEvent]:
        rows = self._query(  # the literal `type` term matches the partial index
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE type = 'budget.defined' "
            "AND substr(stream, 1, 4) = 'org:' "
            # the key of `definition_order`; `at` is fixed-width text; "C" = byte order like Python
            'ORDER BY at COLLATE "C", stream COLLATE "C", seq'
        )
        return [_event_from_row(row) for row in rows]

    def snapshot(self, stream: str) -> tuple[list[LedgerEvent], StreamHead | None]:
        if getattr(self._local, "active", False):
            raise NestedTransactionError("verify or snapshot outside a unit of work")
        with self._lock:
            try:
                with self._db.transaction():  # one read transaction: one consistent view
                    self._db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    rows = self._db.execute(
                        f"SELECT {_EVENT_COLUMNS} FROM ledger_events "
                        "WHERE stream = %s ORDER BY seq",
                        (stream,),
                    ).fetchall()
                    head = _read_head(self._db, stream)
            except psycopg.Error as exc:
                raise _wrap(exc) from exc
        return [_event_from_row(row) for row in rows], head

    def head(self, stream: str) -> StreamHead | None:
        with self._lock:
            try:
                return _read_head(self._db, stream)
            except psycopg.Error as exc:
                raise _wrap(exc) from exc

    def heads(self, only: Collection[str] | None = None) -> dict[str, StreamHead]:
        if only is None:
            rows = self._query("SELECT stream, seq, hash, at FROM stream_heads ORDER BY stream")
        else:
            names = sorted(set(only))
            if not names:
                return {}
            marks = ", ".join(["%s"] * len(names))
            rows = self._query(
                f"SELECT stream, seq, hash, at FROM stream_heads WHERE stream IN ({marks}) "
                "ORDER BY stream",
                tuple(names),
            )
        found: dict[str, StreamHead] = {}
        for r in rows:
            try:
                at = _ledger_time(r["at"], r["stream"], None)
            except BadTimeError:  # one bad row never fails the listing for everyone
                _LOG.warning(
                    "the head of stream %s holds a time that is not real; skipped", r["stream"]
                )
                continue
            found[r["stream"]] = StreamHead(stream=r["stream"], seq=r["seq"], hash=r["hash"], at=at)
        return found

    def damaged_heads(self) -> list[str]:
        out = []
        for r in self._query("SELECT stream, at FROM stream_heads ORDER BY stream"):
            try:
                _ledger_time(r["at"], r["stream"], None)
            except BadTimeError:
                out.append(r["stream"])
        return out

    def damaged_streams(self) -> list[str]:
        """Heads and every entry (ND238). When the database itself has validated every row of
        `ledger_events` (migration 19) no entry can be bad and the scan is skipped."""
        found = set(self.damaged_heads())
        validated = self._query(
            "SELECT convalidated FROM pg_constraint WHERE conname = 'ledger_events_at_valid' "
            "AND conrelid = 'ledger_events'::regclass"
        )
        if not (validated and validated[0]["convalidated"]):
            found.update(
                r["stream"]
                for r in self._query(
                    "SELECT DISTINCT stream FROM ledger_events WHERE NOT nuramesh_valid_at(at)"
                )
            )
        return sorted(found)

    def run_owner(self, stream: str) -> str | None:
        rows = self._query(
            "SELECT type, payload FROM ledger_events WHERE stream = %s ORDER BY seq LIMIT 1",
            (stream,),
        )
        return _owner_of(rows[0]["type"], rows[0]["payload"]) if rows else None

    def streams(self) -> list[str]:
        return [
            row["stream"] for row in self._query("SELECT stream FROM stream_heads ORDER BY stream")
        ]

    def get_task(self, task_id: str) -> StoredTask | None:
        rows = self._query("SELECT data, version FROM tasks WHERE id = %s", (task_id,))
        return _stored_task(rows[0]) if rows else None

    def tasks_for_run(self, run_id: str) -> list[StoredTask]:
        rows = self._query(
            "SELECT data, version FROM tasks WHERE run_id = %s ORDER BY id", (run_id,)
        )
        return [_stored_task(row) for row in rows]

    def get_balance(self, org_id: str, budget_id: str) -> StoredBalance | None:
        rows = self._query(
            "SELECT data, version FROM budget_balances WHERE org_id = %s AND id = %s",
            (org_id, budget_id),
        )
        return _stored_balance(rows[0]) if rows else None

    def get_reservation(self, reservation_id: str) -> StoredReservation | None:
        rows = self._query(
            "SELECT data, version FROM reservations WHERE id = %s", (reservation_id,)
        )
        return _stored_reservation(rows[0]) if rows else None

    def get_content(self, org_id: str, content_id: str) -> ContentRecord | None:
        rows = self._query(
            "SELECT * FROM content WHERE org_id = %s AND id = %s", (org_id, content_id)
        )
        return _content_record(rows[0]) if rows else None

    def get_user(self, org_id: str, user_id: str) -> User | None:
        rows = self._query("SELECT * FROM users WHERE org_id = %s AND id = %s", (org_id, user_id))
        return _user(rows[0]) if rows else None

    def get_user_by_email(self, org_id: str, email: str) -> User | None:
        try:
            wanted = normalize_email(email)
        except ValueError:
            return None
        rows = self._query("SELECT * FROM users WHERE org_id = %s AND email = %s", (org_id, wanted))
        return _user(rows[0]) if rows else None

    def list_users(self, org_id: str) -> list[User]:
        rows = self._query("SELECT * FROM users WHERE org_id = %s ORDER BY id", (org_id,))
        return [_user(row) for row in rows]

    def get_credential(self, org_id: str, user_id: str) -> Credential | None:
        rows = self._query(
            "SELECT * FROM credentials WHERE org_id = %s AND user_id = %s", (org_id, user_id)
        )
        return _credential(rows[0]) if rows else None

    def get_setup_token(self, org_id: str, token_hash: str) -> SetupToken | None:
        rows = self._query(
            "SELECT * FROM setup_tokens WHERE org_id = %s AND token_hash = %s", (org_id, token_hash)
        )
        return _setup_token(rows[0]) if rows else None

    def list_setup_tokens(self, org_id: str) -> list[SetupToken]:
        rows = self._query(
            "SELECT * FROM setup_tokens WHERE org_id = %s ORDER BY created_at, token_hash",
            (org_id,),
        )
        return [_setup_token(row) for row in rows]

    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        rows = self._query(
            "SELECT * FROM auth_sessions WHERE org_id = %s AND id = %s", (org_id, session_id)
        )
        return _auth_session(rows[0]) if rows else None

    def list_auth_sessions(self, org_id: str) -> list[AuthSession]:
        rows = self._query("SELECT * FROM auth_sessions WHERE org_id = %s ORDER BY id", (org_id,))
        return [_auth_session(row) for row in rows]

    def get_auth_token(self, org_id: str, token_hash: str) -> AuthToken | None:
        rows = self._query(
            "SELECT * FROM auth_tokens WHERE org_id = %s AND token_hash = %s", (org_id, token_hash)
        )
        return _auth_token(rows[0]) if rows else None

    def list_auth_tokens(self, org_id: str, session_id: str) -> list[AuthToken]:
        rows = self._query(
            "SELECT * FROM auth_tokens WHERE org_id = %s AND session_id = %s "
            "ORDER BY created_at, kind",
            (org_id, session_id),
        )
        return [_auth_token(row) for row in rows]

    def get_auth_failure(self, key: str) -> AuthFailure | None:
        rows = self._query("SELECT * FROM auth_failures WHERE key = %s", (key,))
        return _auth_failure(rows[0]) if rows else None

    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        rows = self._query(
            "SELECT * FROM mfa_factors WHERE org_id = %s AND user_id = %s", (org_id, user_id)
        )
        return _mfa_factor(rows[0]) if rows else None

    def list_mfa_factors(self, org_id: str) -> list[MfaFactor]:
        rows = self._query(
            "SELECT * FROM mfa_factors WHERE org_id = %s ORDER BY user_id", (org_id,)
        )
        return [_mfa_factor(row) for row in rows]

    def count_mfa_recovery_codes(self, org_id: str, user_id: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM mfa_recovery_codes "
            "WHERE org_id = %s AND user_id = %s AND used_at IS NULL",
            (org_id, user_id),
        )
        return int(rows[0]["n"])

    def get_mfa_challenge(self, org_id: str, token_hash: str) -> MfaChallenge | None:
        rows = self._query(
            "SELECT * FROM mfa_challenges WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        )
        return _mfa_challenge(rows[0]) if rows else None

    def get_org_auth_settings(self, org_id: str) -> OrgAuthSettings:
        rows = self._query("SELECT * FROM org_auth_settings WHERE org_id = %s", (org_id,))
        return _org_auth_settings(rows[0]) if rows else OrgAuthSettings(org_id=org_id)

    def get_user_identifier_key(self, org_id: str, user_id: str) -> bytes | None:
        rows = self._query(
            "SELECT identifier_key FROM users WHERE org_id = %s AND id = %s", (org_id, user_id)
        )
        found = rows[0]["identifier_key"] if rows else None
        return None if found is None else bytes(found)

    def get_org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        rows = self._query(
            "SELECT key FROM identifier_keys WHERE org_id = %s AND epoch = %s", (org_id, epoch)
        )
        found = rows[0]["key"] if rows else None
        return None if found is None else bytes(found)

    def list_security_log(self, org_id: str) -> list[SecurityLogEntry]:
        rows = self._query(
            "SELECT * FROM security_log WHERE org_id = %s ORDER BY at, id", (org_id,)
        )
        return [_security_log(row) for row in rows]

    def get_agent_credential_by_hash(
        self, org_id: str, team: str, token_hash: str
    ) -> AgentCredential | None:
        rows = self._query(
            "SELECT * FROM agent_credentials WHERE org_id = %s AND team = %s AND token_hash = %s",
            (org_id, team, token_hash),
        )
        return _agent_credential(rows[0]) if rows else None

    def get_email_change(self, org_id: str, token_hash: str) -> EmailChange | None:
        rows = self._query(
            "SELECT * FROM email_changes WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        )
        return _email_change(rows[0]) if rows else None

    def list_email_changes(self, org_id: str, user_id: str) -> list[EmailChange]:
        rows = self._query(
            "SELECT * FROM email_changes WHERE org_id = %s AND user_id = %s "
            "ORDER BY created_at, token_hash",
            (org_id, user_id),
        )
        return [_email_change(r) for r in rows]

    def get_reset_token(self, org_id: str, token_hash: str) -> ResetToken | None:
        rows = self._query(
            "SELECT * FROM password_reset_tokens WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        )
        return _reset_token(rows[0]) if rows else None

    def list_reset_tokens(self, org_id: str) -> list[ResetToken]:
        rows = self._query(
            "SELECT * FROM password_reset_tokens WHERE org_id = %s ORDER BY created_at, token_hash",
            (org_id,),
        )
        return [_reset_token(r) for r in rows]

    def get_revoke_link(self, org_id: str, token_hash: str) -> RevokeLink | None:
        rows = self._query(
            "SELECT * FROM api_token_revoke_links WHERE org_id = %s AND token_hash = %s",
            (org_id, token_hash),
        )
        return _revoke_link(rows[0]) if rows else None

    def list_revoke_links(self, org_id: str) -> list[RevokeLink]:
        rows = self._query(
            "SELECT * FROM api_token_revoke_links WHERE org_id = %s "
            "ORDER BY created_at, token_hash",
            (org_id,),
        )
        return [_revoke_link(r) for r in rows]

    def get_password_login_block(self, org_id: str, user_id: str) -> PasswordLoginBlock | None:
        rows = self._query(
            "SELECT * FROM password_login_blocks WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        )
        return _login_block(rows[0]) if rows else None

    def get_invite(self, org_id: str, invite_id: str) -> Invite | None:
        rows = self._query(
            "SELECT * FROM invites WHERE org_id = %s AND id = %s", (org_id, invite_id)
        )
        return _invite(rows[0]) if rows else None

    def get_invite_by_token_hash(self, org_id: str, token_hash: str) -> Invite | None:
        rows = self._query(
            "SELECT * FROM invites WHERE org_id = %s AND token_hash = %s", (org_id, token_hash)
        )
        return _invite(rows[0]) if rows else None

    def list_invites(self, org_id: str) -> list[Invite]:
        rows = self._query(
            "SELECT * FROM invites WHERE org_id = %s ORDER BY created_at DESC, id DESC", (org_id,)
        )
        return [_invite(r) for r in rows]

    def get_agent_credential(self, org_id: str, credential_id: str) -> AgentCredential | None:
        rows = self._query(
            "SELECT * FROM agent_credentials WHERE org_id = %s AND id = %s",
            (org_id, credential_id),
        )
        return _agent_credential(rows[0]) if rows else None

    def list_agent_credentials(self, org_id: str) -> list[AgentCredential]:
        rows = self._query(
            "SELECT * FROM agent_credentials WHERE org_id = %s ORDER BY created_at, id", (org_id,)
        )
        return [_agent_credential(row) for row in rows]

    def get_api_token_by_hash(self, org_id: str, token_hash: str) -> ApiToken | None:
        rows = self._query(
            "SELECT * FROM api_tokens WHERE org_id = %s AND token_hash = %s", (org_id, token_hash)
        )
        return _api_token(rows[0]) if rows else None

    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None:
        rows = self._query(
            "SELECT * FROM api_tokens WHERE org_id = %s AND id = %s", (org_id, token_id)
        )
        return _api_token(rows[0]) if rows else None

    def ext_get(
        self, org_id: str, namespace: str, kind: str, record_id: str
    ) -> ExtensionRecord | None:
        rows = self._query(
            "SELECT * FROM extension_records WHERE org_id = %s AND namespace = %s AND kind = %s "
            "AND record_id = %s",
            (org_id, namespace, kind, record_id),
        )
        return _extension(rows[0]) if rows else None

    def ext_find(
        self, org_id: str, namespace: str, kind: str, unique_key: str
    ) -> ExtensionRecord | None:
        rows = self._query(
            "SELECT * FROM extension_records WHERE org_id = %s AND namespace = %s AND kind = %s "
            "AND unique_key = %s",
            (org_id, namespace, kind, unique_key),
        )
        return _extension(rows[0]) if rows else None

    def ext_list(
        self, org_id: str, namespace: str, kind: str, group_key: str
    ) -> list[ExtensionRecord]:
        rows = self._query(
            "SELECT * FROM extension_records WHERE org_id = %s AND namespace = %s AND kind = %s "
            "AND group_key = %s ORDER BY sort_key DESC, record_id ASC",
            (org_id, namespace, kind, group_key),
        )
        return [_extension(row) for row in rows]

    def count_security_log(
        self, org_id: str, type: str, actor: str | None, start: datetime, end: datetime
    ) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM security_log WHERE org_id = %s AND type = %s "
            "AND actor IS NOT DISTINCT FROM %s AND at >= %s AND at < %s",
            (org_id, type, actor, format_time(start), format_time(end)),
        )
        return int(rows[0]["n"])

    def list_api_tokens(self, org_id: str) -> list[ApiToken]:
        rows = self._query(
            "SELECT * FROM api_tokens WHERE org_id = %s ORDER BY created_at, id", (org_id,)
        )
        return [_api_token(row) for row in rows]

    def api_token_retired_rows(self, org_id: str) -> list[tuple[int, str, datetime]]:
        rows = self._query(
            "SELECT epoch, name_hash, retired_at FROM api_token_retired_names "
            "WHERE org_id = %s ORDER BY retired_at, epoch, name_hash",
            (org_id,),
        )
        return [
            (int(r["epoch"]), r["name_hash"], datetime.fromisoformat(r["retired_at"])) for r in rows
        ]

    def content_subject_schema(self) -> dict[str, list[Any]]:
        columns = [
            r["column_name"]
            for r in self._query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'content_subjects' "
                "ORDER BY ordinal_position",
                (),
            )
        ]
        indexes: list[list[str]] = []
        for row in self._query(
            "SELECT i.relname AS name, array_agg(a.attname ORDER BY k.ord) AS cols "
            "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
            "WHERE t.relname = 'content_subjects' "
            "AND t.relnamespace = current_schema()::regnamespace "
            "AND NOT x.indisprimary GROUP BY i.relname",
            (),
        ):
            indexes.append(list(row["cols"]))
        return {"columns": columns, "indexes": sorted(indexes)}

    def expired_content(self, now: datetime, *, limit: int = 500) -> list[ContentRecord]:
        check_limit(limit)
        rows = self._query(
            "SELECT * FROM content WHERE expires_at IS NOT NULL AND expires_at <= %s "
            "AND org_id NOT IN (SELECT org_id FROM content_holds) "
            "ORDER BY expires_at, org_id, id LIMIT %s",
            (format_time(now), limit),
        )
        return [_content_record(row) for row in rows]

    def expiring_content(
        self, kind: ContentKind, after: datetime, until: datetime
    ) -> list[ContentRecord]:
        rows = self._query(
            "SELECT * FROM content WHERE kind = %s AND expires_at IS NOT NULL "
            "AND expires_at > %s AND expires_at <= %s "
            "AND org_id NOT IN (SELECT org_id FROM content_holds) "
            "ORDER BY expires_at, org_id, id",
            (kind.value, format_time(after), format_time(until)),
        )
        return [_content_record(row) for row in rows]

    def legal_hold(self, org_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM content_holds WHERE org_id = %s", (org_id,)))

    def key_tombstone(self, org_id: str) -> KeyTombstone | None:
        rows = self._query("SELECT * FROM key_tombstones WHERE org_id = %s", (org_id,))
        return _tombstone(rows[0]) if rows else None

    def key_tombstones(self) -> list[KeyTombstone]:
        return [_tombstone(r) for r in self._query("SELECT * FROM key_tombstones ORDER BY org_id")]

    def shredded_orgs(self) -> list[tuple[str, datetime, str]]:
        rows = self._query(
            "SELECT stream, seq, at, actor, payload FROM ledger_events "
            "WHERE type = 'decision.recorded' "
            "AND substr(stream, 1, 4) = 'org:' ORDER BY at, stream, seq"
        )
        # a damaged entry is never dropped silently: `shred_entries` reads the payload kind first
        # and raises only for a shred (or an unreadable payload) with a bad time (ND277, T256)
        return shred_entries([_shred_row(r) for r in rows])

    def store_id(self) -> str:
        rows = self._query("SELECT store_id FROM store_identity")
        return str(rows[0]["store_id"])

    def get_approval(self, approval_id: str) -> StoredApproval | None:
        rows = self._query("SELECT data, version FROM approvals WHERE id = %s", (approval_id,))
        return _stored_approval(rows[0]) if rows else None

    def due_approvals(self, now: datetime, *, after_id: str, limit: int) -> list[StoredApproval]:
        found: list[StoredApproval] = []
        cursor = after_id
        # Pages by id. `ORDER BY id || ''` sorts the same but cannot be served by walking the
        # primary key, so the planner reads only the rows the anchor-day index selects (R13).
        while len(found) < limit:
            page = min(200, limit - len(found) + 10)
            rows = self._query(
                "SELECT data, version FROM approvals WHERE " + _DUE_STATUSES + " AND id > %s "
                "AND " + _ANCHOR_DAY + " <= %s ORDER BY id || '' LIMIT %s",
                (cursor, due_anchor_day(now), page),
            )
            for row in rows:
                stored = _stored_approval(row)
                cursor = stored.approval.id
                if stored.approval.retention_over(now):
                    found.append(stored)
                    if len(found) == limit:
                        break
            if len(rows) < page:
                break
        return found

    def approvals(
        self, *, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[StoredApproval]:
        sql, params = "SELECT data, version FROM approvals WHERE TRUE", []
        if run_id is not None:
            sql += " AND run_id = %s"
            params.append(run_id)
        if status is not None:
            sql += " AND status = %s"
            params.append(status.value)
        return [_stored_approval(row) for row in self._query(sql + " ORDER BY id", tuple(params))]

    def list_balances(self, org_id: str | None) -> list[StoredBalance]:
        rows = self._query(
            "SELECT data, version FROM budget_balances WHERE (%s::text IS NULL OR org_id = %s) "
            'ORDER BY org_id COLLATE "C", id COLLATE "C"',
            (org_id, org_id),
        )
        return [_stored_balance(row) for row in rows]

    def reservations_for_run(
        self, run_id: str, status: ReservationStatus | None = None
    ) -> list[StoredReservation]:
        sql = "SELECT data, version FROM reservations WHERE run_id = %s"
        params: tuple[str, ...] = (run_id,)
        if status is not None:
            sql += " AND status = %s"
            params = (run_id, status.value)
        return [_stored_reservation(row) for row in self._query(sql + " ORDER BY id", params)]


def _project(db: Any, event: LedgerEvent) -> None:
    """Index `event` if the projection holds it (same transaction as the entry)."""
    key = projection_key(event)
    if key is None:
        return
    db.execute(
        "INSERT INTO ledger_projection (stream, kind, seq, org) VALUES (%s, %s, %s, %s)",
        (event.stream, key.kind, event.seq, key.org),
    )
    db.execute(
        "INSERT INTO ledger_projection_counts (stream, kind, n) VALUES (%s, %s, 1) "
        "ON CONFLICT (stream, kind) DO UPDATE SET n = ledger_projection_counts.n + 1",
        (event.stream, key.kind),
    )


def _read_projection(
    db: Any, org_id: str, kinds: Collection[str] | None, streams: ProjectionStreams = "all"
) -> list[tuple[int, LedgerEvent]]:
    wanted = sorted(ALL_KINDS if kinds is None else frozenset(kinds) & ALL_KINDS)
    if not wanted:
        return []
    marks = ", ".join(["%s"] * len(wanted))
    if streams == "org":
        # the organization stream alone: a primary key prefix (stream, kind) range, no scan
        scope = "p.stream = %s"
        params: tuple[str, ...] = (*wanted, f"org:{org_id}")
    else:
        scope = (
            "(p.stream = %s OR p.stream IN (SELECT stream FROM ledger_projection "
            f"WHERE kind = '{RUN_STARTED}' AND org = %s))"
        )
        params = (*wanted, f"org:{org_id}", org_id)
    found = db.execute(
        "SELECT e.*, COALESCE(e.commit_pos, 0) AS cp, p.kind AS pkind "
        f"FROM ledger_projection p JOIN ledger_events e ON e.stream = p.stream AND e.seq = p.seq "
        f"WHERE p.kind IN ({marks}) AND {scope}",
        params,
    ).fetchall()
    counts = db.execute(
        f"SELECT p.stream, p.kind, p.n FROM ledger_projection_counts p "
        f"WHERE p.kind IN ({marks}) AND {scope}",
        params,
    ).fetchall()
    return verify_projected(
        ((r["stream"], r["pkind"], r["cp"], _event_from_row(r)) for r in found),
        {(r["stream"], r["kind"]): r["n"] for r in counts},
    )


def _rebuild_projection(db: Any) -> int:
    """Drop and rebuild the projection from the ledger (migration 18, repairs)."""
    db.execute("DELETE FROM ledger_projection")
    db.execute("DELETE FROM ledger_projection_counts")
    total = 0
    damaged: set[str] = set()
    for row in db.execute(_LEDGER_SCAN).fetchall():
        if _ledger_time_or_none(row["at"]) is None:
            damaged.add(row["stream"])  # ND233/ND277: kept in the projection, so reads refuse
        event = _event_from_row(row, key_only=True)
        if projection_key(event) is not None:
            _project(db, event)
            total += 1
    for name in sorted(damaged):
        _LOG.warning("projection rebuild kept a row of damaged stream %s (time not real)", name)
    return total


_LEDGER_SCAN = (
    f"SELECT {_EVENT_COLUMNS} FROM ledger_events "
    "WHERE type = 'decision.recorded' OR type IN ("
    + ", ".join(f"'{t}'" for t in sorted(k for k in ALL_KINDS if "." in k))
    + ") ORDER BY stream, seq"
)


def _projection_problems(db: Any) -> list[str]:
    """Compare the projection with what the ledger implies (the full audit)."""
    want: set[tuple[str, str, int, str | None]] = set()
    want_counts: dict[tuple[str, str], int] = {}
    damaged: set[str] = set()  # streams with a row whose time is not real (ND233/ND276)
    for row in db.execute(_LEDGER_SCAN).fetchall():
        if _ledger_time_or_none(row["at"]) is None:
            damaged.add(row["stream"])
            continue
        event = _event_from_row(row)
        key = projection_key(event)
        if key is not None:
            want.add((event.stream, key.kind, event.seq, key.org))
            want_counts[(event.stream, key.kind)] = want_counts.get((event.stream, key.kind), 0) + 1
    have = {
        (r["stream"], r["kind"], r["seq"], r["org"])
        for r in db.execute("SELECT stream, kind, seq, org FROM ledger_projection").fetchall()
    }
    have_counts = {
        (r["stream"], r["kind"]): r["n"]
        for r in db.execute("SELECT stream, kind, n FROM ledger_projection_counts").fetchall()
    }
    # a damaged stream is flagged by damaged_streams() and refused; the audit leaves it out
    want = {x for x in want if x[0] not in damaged}
    have = {x for x in have if x[0] not in damaged}
    want_counts = {k: v for k, v in want_counts.items() if k[0] not in damaged}
    have_counts = {k: v for k, v in have_counts.items() if k[0] not in damaged}
    problems = [f"missing from the index: {x[0]}#{x[2]} '{x[1]}'" for x in sorted(want - have)]
    problems += [f"not in the ledger: {x[0]}#{x[2]} '{x[1]}'" for x in sorted(have - want)]
    for pair in sorted(want_counts.keys() | have_counts.keys()):
        if want_counts.get(pair, 0) != have_counts.get(pair, 0):
            problems.append(f"count of {pair[0]} '{pair[1]}' differs from the ledger")
    return problems
