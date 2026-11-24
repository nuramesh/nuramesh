"""SQLite store (standard-library `sqlite3`): local and open-source mode, one file per workspace.

- Each unit of work is one `BEGIN IMMEDIATE` transaction: it takes the write lock before reading
  the stream head, so concurrent writers (threads or processes) can't claim the same sequence
  number. `UNIQUE(stream, seq)` backs this up.
- The ledger table is append-only **in the database**: triggers refuse every UPDATE and DELETE.
- WAL journal, `synchronous=FULL` (a committed event survives power loss), foreign keys on, a busy
  timeout so a waiting writer queues instead of failing at once.
- Payloads are stored as canonical JSON text and decimals as text, so loading gives back exactly
  what was hashed.
- The schema is versioned; migrations run in order in one transaction; a newer schema is refused.
- `stream_heads` has no protective triggers: the store's own heads only catch accidental damage;
  tamper evidence needs heads kept outside the database.
"""

import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

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
    next_erased_token_name,
    normalize_email,
)
from nuramesh.core.budgets import BudgetBalance, Reservation, ReservationStatus, org_key
from nuramesh.core.canonical import canonical_json, thaw
from nuramesh.core.content import ContentKind, ContentRecord, KeyTombstone
from nuramesh.core.email_change import (
    EMAIL_CHANGE_RETENTION,
    EmailChange,
)
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
    ExtensionRecord,
    NestedTransactionError,
    NotFoundError,
    ProjectionStreams,
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

MIGRATIONS: tuple[str, ...] = (
    # 1 ledger_heads_tasks: ledger, stream heads, tasks
    """
    CREATE TABLE ledger_events (
        stream TEXT NOT NULL,
        seq INTEGER NOT NULL CHECK (seq >= 1),
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
    ) WITHOUT ROWID;
    CREATE INDEX ledger_events_task ON ledger_events (task_id);
    CREATE INDEX ledger_events_run ON ledger_events (run_id);
    CREATE TRIGGER ledger_events_no_update BEFORE UPDATE ON ledger_events
        BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
    CREATE TRIGGER ledger_events_no_delete BEFORE DELETE ON ledger_events
        BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
    CREATE TABLE stream_heads (
        stream TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        hash TEXT NOT NULL,
        at TEXT NOT NULL
    ) WITHOUT ROWID;
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL
    ) WITHOUT ROWID;
    CREATE INDEX tasks_run ON tasks (run_id);
    """,
    # 2 budget_balances: budget balances and reservations (new tables only; nothing existing
    # changes)
    """
    CREATE TABLE budget_balances (
        id TEXT PRIMARY KEY,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL
    ) WITHOUT ROWID;
    CREATE TABLE reservations (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL
    ) WITHOUT ROWID;
    CREATE INDEX reservations_run_status ON reservations (run_id, status);
    """,
    # 3 approval_requests: approval requests (new table only; nothing existing changes)
    """
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        data TEXT NOT NULL
    ) WITHOUT ROWID;
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
        nonce BLOB NOT NULL,
        ciphertext BLOB NOT NULL,
        size INTEGER NOT NULL CHECK (size >= 0),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        PRIMARY KEY (org_id, id)
    ) WITHOUT ROWID;
    CREATE INDEX content_expiry ON content (expires_at);
    CREATE TABLE content_holds (
        org_id TEXT PRIMARY KEY
    ) WITHOUT ROWID;
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
    ) WITHOUT ROWID;
    CREATE UNIQUE INDEX users_email ON users (org_id, email);
    CREATE TABLE credentials (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (org_id, user_id)
    ) WITHOUT ROWID;
    CREATE TABLE setup_tokens (
        org_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        PRIMARY KEY (org_id, token_hash)
    ) WITHOUT ROWID;
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
    ) WITHOUT ROWID;
    CREATE TABLE auth_tokens (
        org_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        session_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        PRIMARY KEY (org_id, token_hash)
    ) WITHOUT ROWID;
    CREATE INDEX auth_tokens_session ON auth_tokens (org_id, session_id);
    CREATE TABLE auth_failures (
        key TEXT PRIMARY KEY,
        count INTEGER NOT NULL CHECK (count >= 1),
        last_failure_at TEXT NOT NULL,
        locked_until TEXT
    ) WITHOUT ROWID;
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
    ) WITHOUT ROWID;
    CREATE INDEX security_log_expiry ON security_log (expires_at);
    CREATE INDEX security_log_actor ON security_log (org_id, actor);
    ALTER TABLE users ADD COLUMN identifier_key BLOB;
    CREATE TABLE identifier_keys (
        org_id TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        key BLOB,
        created_at TEXT NOT NULL,
        destroyed_at TEXT,
        PRIMARY KEY (org_id, epoch),
        CHECK ((key IS NULL) = (destroyed_at IS NOT NULL))
    ) WITHOUT ROWID;
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
    ) WITHOUT ROWID;
    """,
    # 10 budget_defined_index: an index of the `budget.defined` entries (T84,
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
    ) WITHOUT ROWID;
    """,
    # 12 key_tombstone: the key tombstone and the store identity (R1.37, T120 to T130). A destroyed
    # organization key is never recreated: `key_tombstones` is permanent (no UPDATE, no DELETE) and
    # `store_identity` names the store (the key folder of this database). The application step
    # `_tombstone_and_identify` makes the identity and backfills tombstones from the ledger's
    # `content_shred` entries. Additive.
    """
    CREATE TABLE store_identity (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        store_id TEXT NOT NULL CHECK (length(store_id) = 32 AND store_id NOT GLOB '*[^0-9a-f]*')
    );
    CREATE TRIGGER store_identity_no_update BEFORE UPDATE ON store_identity
        BEGIN SELECT RAISE(ABORT, 'store identity is permanent'); END;
    CREATE TRIGGER store_identity_no_delete BEFORE DELETE ON store_identity
        BEGIN SELECT RAISE(ABORT, 'store identity is permanent'); END;
    CREATE TABLE key_tombstones (
        org_id TEXT PRIMARY KEY,
        destroyed_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL CHECK (reason IN ('shred', 'erasure', 'backfilled', 'reconciled')),
        key_id TEXT CHECK (key_id IS NULL OR
            (length(key_id) = 16 AND key_id NOT GLOB '*[^0-9a-f]*')),
        backfilled INTEGER NOT NULL CHECK (backfilled IN (0, 1)),
        version INTEGER NOT NULL
    ) WITHOUT ROWID;
    CREATE TRIGGER key_tombstones_no_update BEFORE UPDATE ON key_tombstones
        BEGIN SELECT RAISE(ABORT, 'key tombstones are permanent'); END;
    CREATE TRIGGER key_tombstones_no_delete BEFORE DELETE ON key_tombstones
        BEGIN SELECT RAISE(ABORT, 'key tombstones are permanent'); END;
    """,
    # 13 agent_credentials: agent credentials (E2, T93, T140), additive. Only the
    # SHA-256 of each secret is stored; no name or free-text column exists.
    # `team` is the server's team key.
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
    ) WITHOUT ROWID;
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
        payload BLOB CHECK (payload IS NULL OR length(payload) <= 263188),
        nonce BLOB CHECK (nonce IS NULL OR length(nonce) = 12),
        key_id TEXT CHECK (key_id IS NULL OR length(key_id) = 16),
        PRIMARY KEY (org_id, actor, method, route, key_hash),
        CHECK ((state = 'in_progress') = (fence IS NOT NULL)),
        CHECK ((state = 'done') = (status IS NOT NULL AND payload IS NOT NULL
            AND nonce IS NOT NULL AND key_id IS NOT NULL))
    ) WITHOUT ROWID;
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
    ) WITHOUT ROWID;
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
    ALTER TABLE ledger_events ADD COLUMN commit_pos INTEGER;
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
        seq INTEGER NOT NULL CHECK (seq >= 1),
        org TEXT,
        PRIMARY KEY (stream, kind, seq)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS ledger_projection_runs ON ledger_projection (org, stream)
        WHERE kind = 'run.started';
    CREATE TABLE IF NOT EXISTS ledger_projection_counts (
        stream TEXT NOT NULL,
        kind TEXT NOT NULL,
        n INTEGER NOT NULL CHECK (n >= 0),
        PRIMARY KEY (stream, kind)
    ) WITHOUT ROWID;
    """,
    # 19 ledger_at_check: R1.36 (ND233), defence in depth for every writer: the `at` of
    # `ledger_events` and `stream_heads` must be a real time in the ledger's text form
    # (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). SQLite cannot add a CHECK to a table without rebuilding it
    # (which would rewrite the data), so three triggers guard every new or changed row; no existing
    # row is read, rewritten or deleted (a bad one stays and the readers skip and report it).
    """
    CREATE TRIGGER IF NOT EXISTS ledger_events_at_valid_insert BEFORE INSERT ON ledger_events
        WHEN (length(NEW.at) <> 27 OR NEW.at NOT GLOB (
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T' ||
            '[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
            OR substr(NEW.at, 1, 4) = '0000' OR substr(NEW.at, 12, 2) > '23'
            OR substr(NEW.at, 15, 2) > '59' OR substr(NEW.at, 18, 2) > '59'
            OR strftime('%Y-%m-%dT%H:%M:%S', substr(NEW.at, 1, 19)) IS NOT substr(NEW.at, 1, 19))
        BEGIN SELECT RAISE(ABORT, 'at is not a valid time'); END;
    CREATE TRIGGER IF NOT EXISTS stream_heads_at_valid_insert BEFORE INSERT ON stream_heads
        WHEN (length(NEW.at) <> 27 OR NEW.at NOT GLOB (
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T' ||
            '[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
            OR substr(NEW.at, 1, 4) = '0000' OR substr(NEW.at, 12, 2) > '23'
            OR substr(NEW.at, 15, 2) > '59' OR substr(NEW.at, 18, 2) > '59'
            OR strftime('%Y-%m-%dT%H:%M:%S', substr(NEW.at, 1, 19)) IS NOT substr(NEW.at, 1, 19))
        BEGIN SELECT RAISE(ABORT, 'at is not a valid time'); END;
    CREATE TRIGGER IF NOT EXISTS stream_heads_at_valid_update BEFORE UPDATE ON stream_heads
        WHEN (length(NEW.at) <> 27 OR NEW.at NOT GLOB (
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T' ||
            '[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
            OR substr(NEW.at, 1, 4) = '0000' OR substr(NEW.at, 12, 2) > '23'
            OR substr(NEW.at, 15, 2) > '59' OR substr(NEW.at, 18, 2) > '59'
            OR strftime('%Y-%m-%dT%H:%M:%S', substr(NEW.at, 1, 19)) IS NOT substr(NEW.at, 1, 19))
        BEGIN SELECT RAISE(ABORT, 'at is not a valid time'); END;
    """,
    # 20 user_disabled: E1, an owner may disable a person. One
    # nullable column, additive: no existing row is rewritten.
    """
    ALTER TABLE users ADD COLUMN disabled_at TEXT;
    """,
    # 21 content_subjects: R1.32 (S3, ND79, ND157): who a stored content row
    # names, so erasing a person finds the row. Additive: no existing row is read, changed or
    # deleted; existing content rows get no subject rows. Branches that add later migrations
    # (E8, the approvals sweep, R1.22c, the cost reader) renumber after this one (ND126).
    """
    CREATE TABLE content_subjects (
        org_id TEXT NOT NULL,
        content_id TEXT NOT NULL,
        subject TEXT NOT NULL,
        run_id TEXT,
        erase_pending INTEGER NOT NULL DEFAULT 0 CHECK (erase_pending IN (0, 1)),
        PRIMARY KEY (org_id, content_id, subject)
    ) WITHOUT ROWID;
    CREATE INDEX content_subjects_subject ON content_subjects (org_id, subject);
    CREATE INDEX content_subjects_run ON content_subjects (org_id, run_id);
    """,
    # 22 tombstone_approx_reason: T258 (ND233, ND277). Widens the `key_tombstones.reason` CHECK
    # with `backfilled_approximate` (a backfilled tombstone whose date is not the ledger's).
    # Migration 12 is released and never edited (owner rule), so this one rewrites the CHECK:
    # SQLite cannot alter a CHECK, so the table is rebuilt (rows copied unchanged, the
    # permanence triggers recreated); the application step `_backfill_approximate_tombstones`
    # then adds the tombstones migration 12 had to leave out. Number: the next after 21 on main.
    """
    CREATE TABLE key_tombstones_new (
        org_id TEXT PRIMARY KEY,
        destroyed_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL CHECK (reason IN ('shred', 'erasure', 'backfilled',
            'backfilled_approximate', 'reconciled')),
        key_id TEXT CHECK (key_id IS NULL OR
            (length(key_id) = 16 AND key_id NOT GLOB '*[^0-9a-f]*')),
        backfilled INTEGER NOT NULL CHECK (backfilled IN (0, 1)),
        version INTEGER NOT NULL
    ) WITHOUT ROWID;
    INSERT INTO key_tombstones_new SELECT org_id, destroyed_at, actor, reason, key_id,
        backfilled, version FROM key_tombstones;
    DROP TABLE key_tombstones;
    ALTER TABLE key_tombstones_new RENAME TO key_tombstones;
    CREATE TRIGGER key_tombstones_no_update BEFORE UPDATE ON key_tombstones
        BEGIN SELECT RAISE(ABORT, 'key tombstones are permanent'); END;
    CREATE TRIGGER key_tombstones_no_delete BEFORE DELETE ON key_tombstones
        BEGIN SELECT RAISE(ABORT, 'key tombstones are permanent'); END;
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
    # 24 invites: R1.37. The invites table and the
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
    # 25 email_verification: R1.40.
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
    ) WITHOUT ROWID;
    CREATE INDEX email_changes_user ON email_changes (org_id, user_id);
    """,
    # 26 password_reset: R1.41.
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
    ) WITHOUT ROWID;
    CREATE INDEX password_reset_user ON password_reset_tokens (org_id, user_id);
    CREATE TABLE password_login_blocks (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        blocked_at TEXT NOT NULL,
        via TEXT NOT NULL CHECK (via IN ('owner', 'cli')),
        PRIMARY KEY (org_id, user_id)
    ) WITHOUT ROWID;
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
    ) WITHOUT ROWID;
    CREATE INDEX api_token_revoke_links_user ON api_token_revoke_links (org_id, user_id);
    """,
    # 27 mfa: R1.38, one TOTP factor per person
    # (the secret only
    # sealed), MFA recovery code hashes, login challenges (hash only), the organization's sign-in
    # policy (no row = MFA not required) and three session columns. `login_via` is nullable here
    # (SQLite cannot add NOT NULL to a column) and backfilled to `password`; the application
    # validates it on every insert and a NULL or unknown value is refused by refresh.
    """
    CREATE TABLE mfa_factors (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        id TEXT NOT NULL,
        secret_enc BLOB NOT NULL CHECK (length(secret_enc) = 48),
        key_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        confirmed_at TEXT,
        last_used_step INTEGER,
        password_checked_at TEXT NOT NULL,
        PRIMARY KEY (org_id, user_id)
    ) WITHOUT ROWID;
    CREATE TABLE mfa_recovery_codes (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        used_at TEXT,
        PRIMARY KEY (org_id, user_id, code_hash)
    ) WITHOUT ROWID;
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
    ) WITHOUT ROWID;
    CREATE TABLE org_auth_settings (
        org_id TEXT PRIMARY KEY,
        mfa_required INTEGER NOT NULL DEFAULT 0 CHECK (mfa_required IN (0, 1)),
        updated_at TEXT,
        updated_by TEXT
    ) WITHOUT ROWID;
    ALTER TABLE auth_sessions ADD COLUMN mfa_verified_at TEXT;
    ALTER TABLE auth_sessions ADD COLUMN mfa_method TEXT;
    ALTER TABLE auth_sessions ADD COLUMN login_via TEXT;
    UPDATE auth_sessions SET login_via = 'password' WHERE login_via IS NULL;
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
        erasable INTEGER NOT NULL,
        body TEXT NOT NULL,
        PRIMARY KEY (org_id, namespace, kind, record_id)
    );
    CREATE UNIQUE INDEX extension_records_unique
        ON extension_records (org_id, namespace, kind, unique_key) WHERE unique_key IS NOT NULL;
    CREATE INDEX extension_records_group
        ON extension_records (org_id, namespace, kind, group_key, sort_key);
    """,
    # 29 solo_approval: solo approval (#942). Additive only:
    # nullable columns, nothing existing is rewritten. `password_confirmed_at` is when a session
    # last re-confirmed the password (SA-8; NULL = not confirmed). The approval columns repeat
    # three values of `data` for decisions made from now on (NULL on older rows, which have no
    # mode), so the solo rate limit (SA-9) counts by index instead of reading every row.
    # The runner adds only the columns that are missing (SQLite has no ADD COLUMN IF NOT EXISTS).
    """
    ALTER TABLE auth_sessions ADD COLUMN password_confirmed_at TEXT;
    ALTER TABLE approvals ADD COLUMN approval_mode TEXT;
    ALTER TABLE approvals ADD COLUMN decided_by TEXT;
    ALTER TABLE approvals ADD COLUMN decided_at TEXT;
    CREATE INDEX IF NOT EXISTS approvals_solo_window
        ON approvals (decided_by, approval_mode, decided_at);
    """,
    # 30 api_token_names: token rename and delete (#634,
    # R16).
    # Additive only: a table of retired token names (keyed hashes under the organization key,
    # never a name). No existing row is read, rewritten or deleted.
    """
    CREATE TABLE api_token_retired_names (
        org_id TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        name_hash TEXT NOT NULL,
        retired_at TEXT NOT NULL,
        PRIMARY KEY (org_id, epoch, name_hash)
    );
    """,
    # 31 rbac: R2.13, the last strong authentication on a session (any
    # method). One
    # nullable column, additive: no role or session is rewritten; NULL means "ask once". The
    # runner adds it only if missing (SQLite has no ADD COLUMN IF NOT EXISTS).
    """
    ALTER TABLE auth_sessions ADD COLUMN step_up_at TEXT;
    """,
    # 32 approvals_sweep: the approvals sweep (T226, ND195 a, Part C).
    # An index on the day an approval ended (the consumption time if it has one, else the decision
    # time, date prefix of the stored text; decided and consumed approvals only), so the sweep
    # finds the due rows without reading the finished ones. Additive: an index only; no row is read
    # or rewritten by the migration.
    """
    CREATE INDEX approvals_anchor_day ON approvals (
        coalesce(substr(json_extract(data, '$.consumed_at'), 1, 10),
                 substr(json_extract(data, '$.decided_at'), 1, 10), '')
    ) WHERE status IN ('approved', 'denied', 'consumed');
    """,
)

# The expression of migration 32's index (the query must repeat it to use the index).
_ANCHOR_DAY = (
    "coalesce(substr(json_extract(data, '$.consumed_at'), 1, 10), "
    "substr(json_extract(data, '$.decided_at'), 1, 10), '')"
)
_DUE_WHERE = "status IN ('approved', 'denied', 'consumed')"

_RQ_UPDATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "run_start_requests": (
        "UPDATE run_start_requests SET state = ?, claim_owner = ?, claim_until = ?, "
        "refusal_code = ? WHERE org_id = ? AND run_id = ?",
        ("state", "claim_owner", "claim_until", "refusal_code", "org_id", "run_id"),
    ),
    "light_jobs": (
        "UPDATE light_jobs SET state = ?, due_at = ?, lease_owner = ?, lease_until = ?, "
        "epoch = ? WHERE org_id = ? AND run_id = ?",
        ("state", "due_at", "lease_owner", "lease_until", "epoch", "org_id", "run_id"),
    ),
    "light_leader": (
        "UPDATE light_leader SET owner = ?, lease_until = ?, epoch = ? WHERE name = ?",
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
                "AND substr(stream, 1, 4) = 'org:' ORDER BY at, stream, seq"
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
            "INSERT INTO budget_balances_org (org_id, id, version, data) VALUES (?, ?, ?, ?)",
            (org, budget_id, row["version"], data),
        )
    for row in db.execute("SELECT id, data FROM reservations").fetchall():
        db.execute(
            "UPDATE reservations SET data = ? WHERE id = ?",
            (reservation_data(row["data"], owners), row["id"]),
        )
    copied = db.execute("SELECT COUNT(*) AS n FROM budget_balances_org").fetchone()["n"]
    if copied != len(old):
        raise MigrationError(f"budget migration copied {copied} of {len(old)} balances")
    db.execute("DROP TABLE budget_balances")
    db.execute("ALTER TABLE budget_balances_org RENAME TO budget_balances")


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
            "INSERT INTO store_identity (singleton, store_id) VALUES (1, ?)",
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
        "INSERT OR IGNORE INTO key_tombstones (org_id, destroyed_at, actor, reason, key_id, "
        "backfilled, version) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            record.org_id,
            format_time(record.destroyed_at),
            record.actor,
            record.reason,
            record.key_id,
            int(record.backfilled),
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


def _tombstone(row: Any) -> KeyTombstone:
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
            "UPDATE users SET identifier_key = ? WHERE org_id = ? AND id = ?",
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
            "UPDATE security_log SET subject = ? WHERE org_id = ? AND id = ?",
            (user_id, org, row_id),
        )
    return found


_EVENT_COLUMNS = (
    "stream, seq, type, actor, at, run_id, task_id, payload, cost_amount, cost_currency, "
    "cost_basis, prev_hash, hash"
)


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


# True for a stored `at` that is not a real time in the ledger's form: the test of the migration 19
# triggers, applied to a column instead of NEW.at (ND238).
_BAD_AT = (
    "(length(at) <> 27 OR at NOT GLOB ("
    "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T' || "
    "'[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z') "
    "OR substr(at, 1, 4) = '0000' OR substr(at, 12, 2) > '23' "
    "OR substr(at, 15, 2) > '59' OR substr(at, 18, 2) > '59' "
    "OR strftime('%Y-%m-%dT%H:%M:%S', substr(at, 1, 19)) IS NOT substr(at, 1, 19))"
)


def _owner_of(kind: str, payload: str) -> str | None:
    """The `org` of a `run.started` payload read as raw text (no time is parsed)."""
    if kind != EventType.RUN_STARTED.value:
        return None
    try:
        org = json.loads(payload).get("org")
    except (ValueError, AttributeError):
        return None
    return org if isinstance(org, str) else None


def _event_from_row(row: sqlite3.Row, *, key_only: bool = False) -> LedgerEvent:
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


def _stored_task(row: sqlite3.Row) -> StoredTask:
    return StoredTask(task=Task.model_validate_json(row["data"]), version=row["version"])


def _stored_balance(row: sqlite3.Row) -> StoredBalance:
    return StoredBalance(
        balance=BudgetBalance.model_validate_json(row["data"]), version=row["version"]
    )


def _content_record(row: sqlite3.Row) -> ContentRecord:
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


def _user(row: sqlite3.Row) -> User:
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


def _credential(row: sqlite3.Row) -> Credential:
    return Credential(
        org_id=row["org_id"],
        user_id=row["user_id"],
        password_hash=row["password_hash"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _setup_token(row: sqlite3.Row) -> SetupToken:
    consumed = row["consumed_at"]
    return SetupToken(
        org_id=row["org_id"],
        token_hash=row["token_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        consumed_at=None if consumed is None else datetime.fromisoformat(consumed),
    )


def _auth_session(row: sqlite3.Row) -> AuthSession:
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


def _mfa_factor(row: sqlite3.Row) -> MfaFactor:
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


def _mfa_challenge(row: sqlite3.Row) -> MfaChallenge:
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


def _org_auth_settings(row: sqlite3.Row) -> OrgAuthSettings:
    updated = row["updated_at"]
    return OrgAuthSettings(
        org_id=row["org_id"],
        mfa_required=bool(row["mfa_required"]),
        updated_at=None if updated is None else datetime.fromisoformat(updated),
        updated_by=row["updated_by"],
    )


def _auth_token(row: sqlite3.Row) -> AuthToken:
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


def _auth_failure(row: sqlite3.Row) -> AuthFailure:
    locked = row["locked_until"]
    return AuthFailure(
        key=row["key"],
        count=row["count"],
        last_failure_at=datetime.fromisoformat(row["last_failure_at"]),
        locked_until=None if locked is None else datetime.fromisoformat(locked),
    )


def _security_log(row: sqlite3.Row) -> SecurityLogEntry:
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


def _api_token(row: sqlite3.Row) -> ApiToken:
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
        "UPDATE users SET email_verified_at = ?, email_verified_via = 'legacy' "
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


def _stored_approval(row: sqlite3.Row) -> StoredApproval:
    return StoredApproval(
        approval=Approval.model_validate_json(row["data"]), version=row["version"]
    )


def _stored_reservation(row: sqlite3.Row) -> StoredReservation:
    return StoredReservation(
        reservation=Reservation.model_validate_json(row["data"]), version=row["version"]
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
    db: sqlite3.Connection,
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
        marks = ", ".join("?" for _ in columns)
        try:
            db.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", values)
        except sqlite3.IntegrityError as exc:
            raise ConcurrencyError(f"{table} '{key}' already exists") from exc
        return 1
    sets = ", ".join(["version = version + 1", "data = ?", *(f"{c} = ?" for c in extra)])
    cursor = db.execute(
        f"UPDATE {table} SET {sets} WHERE id = ? AND version = ?",
        (data, *extra.values(), key, expected_version),
    )
    if cursor.rowcount != 1:
        row = db.execute(f"SELECT version FROM {table} WHERE id = ?", (key,)).fetchone()
        found = None if row is None else row["version"]
        raise ConcurrencyError(f"{table} '{key}' is at version {found}, not {expected_version}")
    return expected_version + 1


def _put_balance(
    db: sqlite3.Connection, balance: BudgetBalance, expected_version: int | None
) -> int:
    """Insert or update a balance row by (organization, id); ConcurrencyError if stale."""
    org, key = org_key(balance.budget.org_id), balance.budget.id
    data = balance.model_dump_json()
    if expected_version is None:
        try:
            db.execute(
                "INSERT INTO budget_balances (org_id, id, version, data) VALUES (?, ?, 1, ?)",
                (org, key, data),
            )
        except sqlite3.IntegrityError as exc:
            raise ConcurrencyError(f"budget '{key}' already exists") from exc
        return 1
    cursor = db.execute(
        "UPDATE budget_balances SET version = version + 1, data = ? "
        "WHERE org_id = ? AND id = ? AND version = ?",
        (data, org, key, expected_version),
    )
    if cursor.rowcount != 1:
        row = db.execute(
            "SELECT version FROM budget_balances WHERE org_id = ? AND id = ?", (org, key)
        ).fetchone()
        found = None if row is None else row["version"]
        raise ConcurrencyError(f"budget '{key}' is at version {found}, not {expected_version}")
    return expected_version + 1


def _idempotency(row: sqlite3.Row, subjects: frozenset[str]) -> StoredIdempotency:
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


class _SqliteUnitOfWork(UnitOfWork):
    def __init__(
        self,
        connection: sqlite3.Connection,
        clock: Clock,
        schema: int | None = None,
        clean: set[str] | None = None,
    ) -> None:
        self._db = connection
        self._clock = clock
        self.erased = False
        self._clean = clean if clean is not None else set()
        self._at = clock()  # read once, inside the write lock (ND234)
        # a store opened on a schema older than a column or table (a build whose migration list
        # ends before it) writes events as that build did. This reduced path only runs when a
        # test patches MIGRATIONS: production always migrates, or refuses a newer schema.
        schema = len(MIGRATIONS) if schema is None else schema
        self._writes_commit_pos = schema >= COMMIT_POS_MIGRATION
        self._projects = schema >= PROJECTION_MIGRATION

    def _refuse_damaged(self, stream: str) -> None:
        if stream in self._clean:
            return
        row = self._db.execute(
            f"SELECT 1 FROM ledger_events WHERE stream = ? AND {_BAD_AT} LIMIT 1", (stream,)
        ).fetchone()
        head = self._db.execute(
            "SELECT at FROM stream_heads WHERE stream = ?", (stream,)
        ).fetchone()
        if row is not None or (head is not None and _ledger_time_or_none(head["at"]) is None):
            raise BadTimeError(stream, None)
        if len(self._clean) >= CLEAN_CACHE:
            self._clean.clear()  # only a cache of checks already made
        self._clean.add(stream)

    def last_event(self, stream: str) -> LedgerEvent | None:
        row = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = ? "
            "ORDER BY seq DESC LIMIT 1",
            (stream,),
        ).fetchone()
        return None if row is None else _event_from_row(row)

    def head(self, stream: str) -> StreamHead | None:
        return _read_head(self._db, stream)

    def events_after(self, stream: str, after_seq: int, limit: int) -> list[LedgerEvent]:
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (stream, after_seq, limit),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def events_after_positioned(
        self, stream: str, after_seq: int, limit: int
    ) -> list[tuple[int, LedgerEvent]]:
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS}, COALESCE(commit_pos, 0) AS commit_pos FROM ledger_events "
            "WHERE stream = ? AND seq > ? ORDER BY seq LIMIT ?",
            (stream, after_seq, limit),
        ).fetchall()
        return [(row["commit_pos"], _event_from_row(row)) for row in rows]

    def commit_position(self) -> int:
        row = self._db.execute("SELECT MAX(commit_pos) AS p FROM ledger_events").fetchone()
        return int(row["p"] or 0)

    def entries_after_position(
        self, pos: int, limit: int | None = None
    ) -> list[tuple[int, LedgerEvent]]:
        check_limit(limit)
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS}, COALESCE(commit_pos, 0) AS commit_pos FROM ledger_events "
            "WHERE commit_pos > ? ORDER BY commit_pos LIMIT ?",
            (pos, -1 if limit is None else limit),
        ).fetchall()
        return [(row["commit_pos"], _event_from_row(row)) for row in rows]

    def has_event(self, stream: str, type: EventType) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM ledger_events WHERE stream = ? AND type = ? LIMIT 1",
            (stream, type.value),
        ).fetchone()
        return row is not None

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = ? AND task_id = ? "
            "ORDER BY seq",
            (run_stream(run_id), task_id),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def _insert_event(self, event: LedgerEvent) -> None:
        try:
            if self._writes_commit_pos:
                self._db.execute(
                    f"INSERT INTO ledger_events ({_EVENT_COLUMNS}, commit_pos) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "(SELECT COALESCE(MAX(commit_pos), 0) + 1 FROM ledger_events))",
                    _event_row(event),
                )
            else:
                self._db.execute(
                    f"INSERT INTO ledger_events ({_EVENT_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _event_row(event),
                )
        except sqlite3.IntegrityError as exc:
            raise ConcurrencyError(f"event {event.stream}#{event.seq} already exists") from exc
        self._db.execute(
            "INSERT INTO stream_heads (stream, seq, hash, at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (stream) DO UPDATE SET seq = excluded.seq, hash = excluded.hash, "
            "at = excluded.at",
            (event.stream, event.seq, event.hash, format_time(event.at)),
        )
        if self._projects:
            _project(self._db, event)

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
            "SELECT data, version FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return None if row is None else _stored_task(row)

    def tasks_for_run(self, run_id: str) -> list[StoredTask]:
        rows = self._db.execute(
            "SELECT data, version FROM tasks WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [_stored_task(row) for row in rows]

    def put_task(self, task: Task, *, expected_version: int | None) -> int:
        data = task.model_dump_json()
        if expected_version is None:
            try:
                self._db.execute(
                    "INSERT INTO tasks (id, run_id, status, version, data) VALUES (?, ?, ?, 1, ?)",
                    (task.id, task.run_id, task.status.value, data),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrencyError(f"task '{task.id}' already exists") from exc
            return 1
        cursor = self._db.execute(
            "UPDATE tasks SET run_id = ?, status = ?, version = version + 1, data = ? "
            "WHERE id = ? AND version = ?",
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
            "SELECT data, version FROM budget_balances WHERE org_id = ? AND id = ?",
            (org_id, budget_id),
        ).fetchone()
        return None if row is None else _stored_balance(row)

    def list_balances(self, org_id: str | None) -> list[StoredBalance]:
        rows = self._db.execute(
            "SELECT data, version FROM budget_balances WHERE (? IS NULL OR org_id = ?) "
            "ORDER BY org_id, id",
            (org_id, org_id),
        ).fetchall()
        return [_stored_balance(row) for row in rows]

    def put_balance(self, balance: BudgetBalance, *, expected_version: int | None) -> int:
        return _put_balance(self._db, balance, expected_version)

    def get_content(self, org_id: str, content_id: str) -> ContentRecord | None:
        row = self._db.execute(
            "SELECT * FROM content WHERE org_id = ? AND id = ?", (org_id, content_id)
        ).fetchone()
        return None if row is None else _content_record(row)

    def put_content(self, record: ContentRecord) -> None:
        row = self._db.execute(
            "SELECT expires_at FROM content WHERE org_id = ? AND id = ?",
            (record.org_id, record.id),
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO content (org_id, id, kind, key_id, nonce, ciphertext, size, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "UPDATE content SET expires_at = ? WHERE org_id = ? AND id = ?",
            (None if merged is None else format_time(merged), record.org_id, record.id),
        )

    def set_run_content_expiry(
        self, org_id: str, content_id: str, expires_at: datetime | None
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE content SET expires_at = ? WHERE org_id = ? AND id = ? "
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
        sql = "SELECT org_id, id, created_at, expires_at FROM content WHERE kind = ?"
        params: list[Any] = [kind.value]
        if org_id is not None:
            sql += " AND org_id = ?"
            params.append(org_id)
        if content_id is not None:
            sql += " AND id = ?"
            params.append(content_id)
        changed: dict[str, int] = {}
        for row in self._db.execute(sql, params).fetchall():
            new = clamped_expiry(
                since or datetime.fromisoformat(row["created_at"]),
                None if row["expires_at"] is None else datetime.fromisoformat(row["expires_at"]),
                max_days,
            )
            if new is not None:
                self._db.execute(
                    "UPDATE content SET expires_at = ? WHERE org_id = ? AND id = ?",
                    (format_time(new), row["org_id"], row["id"]),
                )
                changed[row["org_id"]] = changed.get(row["org_id"], 0) + 1
        return changed

    def delete_content(self, org_id: str, content_id: str) -> bool:
        self.erased = True  # the store truncates the WAL after this commit
        cursor = self._db.execute(
            "DELETE FROM content WHERE org_id = ? AND id = ?", (org_id, content_id)
        )
        self._drop_subjects(org_id, content_id)
        return cursor.rowcount > 0

    def _drop_subjects(self, org_id: str, content_id: str) -> None:
        self._db.execute(
            "DELETE FROM content_subjects WHERE org_id = ? AND content_id = ?",
            (org_id, content_id),
        )

    def _put_content_subject(
        self, org_id: str, content_id: str, subject: str, run_id: str | None
    ) -> bool:
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO content_subjects (org_id, content_id, subject, run_id) "
            "VALUES (?, ?, ?, ?)",
            (org_id, content_id, subject, run_id),
        )
        return cursor.rowcount > 0

    # -- the run queue (R1.20 PR A): primitives of RunQueueOps ----------------------------------

    def _rq_get(self, table: str, key: tuple[str, ...]) -> dict[str, Any] | None:
        where = " AND ".join(f"{c} = ?" for c in RQ_KEYS[table])
        row = self._db.execute(f"SELECT * FROM {table} WHERE {where}", key).fetchone()
        return None if row is None else dict(row)

    def _rq_insert(self, table: str, row: dict[str, Any]) -> bool:
        cols = RQ_COLUMNS[table]
        marks = ", ".join("?" for _ in cols)
        cursor = self._db.execute(
            f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({marks})",
            tuple(row[c] for c in cols),
        )
        return cursor.rowcount > 0

    def _rq_update(self, table: str, row: dict[str, Any]) -> None:
        sql, columns = _RQ_UPDATES[table]
        self._db.execute(sql, tuple(row[c] for c in columns))

    def _rq_scan(self, table: str, state_in: tuple[str, ...] | None) -> list[dict[str, Any]]:
        if state_in is None:
            rows = self._db.execute(f"SELECT * FROM {table}").fetchall()
        else:
            marks = ", ".join("?" for _ in state_in)
            rows = self._db.execute(
                f"SELECT * FROM {table} WHERE state IN ({marks})", state_in
            ).fetchall()
        return [dict(r) for r in rows]

    def _rq_count(self, table: str, org_id: str, state_in: tuple[str, ...] | None) -> int:
        sql = f"SELECT COUNT(*) FROM {table} WHERE org_id = ?"
        args: tuple[str, ...] = (org_id,)
        if state_in is not None:
            sql += f" AND state IN ({', '.join('?' for _ in state_in)})"
            args += state_in
        row = self._db.execute(sql, args).fetchone()
        return int(row[0])

    def content_subject_rows(self, org_id: str) -> list[ContentSubjectRow]:
        rows = self._db.execute(
            "SELECT content_id, subject, run_id, erase_pending FROM content_subjects "
            "WHERE org_id = ? ORDER BY content_id, subject",
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
                "SELECT DISTINCT content_id FROM content_subjects WHERE org_id = ? AND subject = ?",
                (org_id, subject),
            ).fetchall()
        ]
        if self.legal_hold(org_id):
            self._db.execute(
                "UPDATE content_subjects SET erase_pending = 1 WHERE org_id = ? AND subject = ?",
                (org_id, subject),
            )
            return 0, [], len(mine)
        return (*self._erase_rows(org_id, mine), 0)

    def _erase_rows(self, org_id: str, content_ids: list[str]) -> tuple[int, list[str]]:
        kinds: set[str] = set()
        count = 0
        for cid in content_ids:
            row = self._db.execute(
                "SELECT kind FROM content WHERE org_id = ? AND id = ?", (org_id, cid)
            ).fetchone()
            if row is not None:
                kinds.add(row["kind"])
                count += 1
            self._db.execute("DELETE FROM content WHERE org_id = ? AND id = ?", (org_id, cid))
            self._drop_subjects(org_id, cid)
        return count, sorted(kinds)

    def pending_erasure_orgs(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT org_id FROM content_subjects WHERE erase_pending = 1 ORDER BY org_id"
        ).fetchall()
        return [r["org_id"] for r in rows]

    def erase_pending_content(self, org_id: str) -> tuple[int, list[str]]:
        due = [
            r["content_id"]
            for r in self._db.execute(
                "SELECT DISTINCT content_id FROM content_subjects "
                "WHERE org_id = ? AND erase_pending = 1",
                (org_id,),
            ).fetchall()
        ]
        return self._erase_rows(org_id, due)

    def legal_hold(self, org_id: str) -> bool:
        row = self._db.execute("SELECT 1 FROM content_holds WHERE org_id = ?", (org_id,)).fetchone()
        return row is not None

    def delete_if_expired(self, org_id: str, content_id: str, now: datetime) -> bool:
        self.erased = True  # the store truncates the WAL after this commit
        cursor = self._db.execute(
            "DELETE FROM content WHERE org_id = ? AND id = ? AND expires_at IS NOT NULL "
            "AND expires_at <= ? AND org_id NOT IN (SELECT org_id FROM content_holds)",
            (org_id, content_id, format_time(now)),
        )
        if cursor.rowcount > 0:
            self._drop_subjects(org_id, content_id)
        return cursor.rowcount > 0

    def set_legal_hold(self, org_id: str, on: bool) -> None:
        if on:
            self._db.execute("INSERT OR IGNORE INTO content_holds (org_id) VALUES (?)", (org_id,))
        else:
            self._db.execute("DELETE FROM content_holds WHERE org_id = ?", (org_id,))

    def key_tombstone(self, org_id: str) -> KeyTombstone | None:
        row = self._db.execute(
            "SELECT * FROM key_tombstones WHERE org_id = ?", (org_id,)
        ).fetchone()
        return None if row is None else _tombstone(row)

    def put_key_tombstone(self, record: KeyTombstone) -> bool:
        return _insert_tombstone(self._db, record)

    def org_has_content(self, org_id: str) -> bool:
        return (
            self._db.execute("SELECT 1 FROM content WHERE org_id = ? LIMIT 1", (org_id,)).fetchone()
            is not None
        )

    def org_content_key_ids(self, org_id: str) -> frozenset[str]:
        rows = self._db.execute(
            "SELECT DISTINCT key_id FROM content WHERE org_id = ?", (org_id,)
        ).fetchall()
        return frozenset(r["key_id"] for r in rows)

    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]:
        rows = self._db.execute(
            "SELECT payload FROM ledger_events WHERE payload LIKE ? ESCAPE '\\' "
            "AND payload LIKE '%\"hash_scheme\"%'",
            (hash_org_pattern(org_id),),
        ).fetchall()
        return keyed_ids_of((json.loads(r["payload"]) for r in rows), org_id)

    def upsert_security_log_count(self, entry: SecurityLogEntry) -> int:
        row = self._db.execute(
            "SELECT details FROM security_log WHERE org_id = ? AND id = ?",
            (entry.org_id, entry.id),
        ).fetchone()
        if row is None:
            self.append_security_log(entry)
            return int(entry.details.get("count", 1) or 1)
        details = json.loads(row["details"])
        details["count"] = bumped(int(details.get("count", 1)))
        self._db.execute(
            "UPDATE security_log SET details = ? WHERE org_id = ? AND id = ?",
            (json.dumps(details, sort_keys=True, separators=(",", ":")), entry.org_id, entry.id),
        )
        return int(details["count"])

    def create_user(self, user: User) -> None:
        if self.get_user(user.org_id, user.id) is not None:
            raise AlreadyExistsError(f"user '{user.id}' already exists")
        if user.email is not None and self.get_user_by_email(user.org_id, user.email) is not None:
            raise AlreadyExistsError("a user with this email already exists")
        self._db.execute(
            "INSERT INTO users (org_id, id, email, display_name, role, created_at, erased_at, "
            "identifier_key, email_verified_at, email_verified_via) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "SELECT * FROM users WHERE org_id = ? AND id = ?", (org_id, user_id)
        ).fetchone()
        return None if row is None else _user(row)

    def list_users(self, org_id: str) -> list[User]:
        rows = self._db.execute(
            "SELECT * FROM users WHERE org_id = ? ORDER BY id", (org_id,)
        ).fetchall()
        return [_user(row) for row in rows]

    def get_user_by_email(self, org_id: str, email: str) -> User | None:
        try:
            wanted = normalize_email(email)
        except ValueError:
            return None
        row = self._db.execute(
            "SELECT * FROM users WHERE org_id = ? AND email = ?", (org_id, wanted)
        ).fetchone()
        return None if row is None else _user(row)

    def erase_user(self, org_id: str, user_id: str, at: datetime) -> bool:
        self.erased = True  # the store truncates the WAL after this commit
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self._db.execute(
            "UPDATE users SET email = NULL, display_name = NULL, erased_at = ?, "
            "identifier_key = NULL, email_verified_at = NULL, email_verified_via = NULL "
            "WHERE org_id = ? AND id = ?",
            (format_time(at), org_id, user_id),
        )
        self._db.execute(
            "DELETE FROM credentials WHERE org_id = ? AND user_id = ?", (org_id, user_id)
        )
        self.purge_idempotency_actor(org_id, user_id)  # a user id is the actor id (T42)
        return True

    def set_user_role(self, org_id: str, user_id: str, role: Role) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self._db.execute(
            "UPDATE users SET role = ? WHERE org_id = ? AND id = ?",
            (role.value, org_id, user_id),
        )
        return True

    def set_user_email_verified(self, org_id: str, user_id: str, at: datetime, via: str) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self._db.execute(
            "UPDATE users SET email_verified_at = ?, email_verified_via = ? "
            "WHERE org_id = ? AND id = ?",
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
            "UPDATE users SET email = ?, email_verified_at = ?, email_verified_via = 'link' "
            "WHERE org_id = ? AND id = ?",
            (email, format_time(at), org_id, user_id),
        )

    def put_email_change(self, change: EmailChange) -> None:
        if self.get_email_change(change.org_id, change.token_hash) is not None:
            raise AlreadyExistsError("this email change token already exists")
        self._db.execute(
            "INSERT INTO email_changes (org_id, user_id, new_email, token_hash, deliverable, "
            "created_at, expires_at, consumed_at, cancelled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "SELECT * FROM email_changes WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        ).fetchone()
        return None if row is None else _email_change(row)

    def list_email_changes(self, org_id: str, user_id: str) -> list[EmailChange]:
        rows = self._db.execute(
            "SELECT * FROM email_changes WHERE org_id = ? AND user_id = ? "
            "ORDER BY created_at, token_hash",
            (org_id, user_id),
        ).fetchall()
        return [_email_change(r) for r in rows]

    def cancel_email_changes(
        self, org_id: str, user_id: str, at: datetime, keep_hash: str | None = None
    ) -> int:
        cursor = self._db.execute(
            "UPDATE email_changes SET cancelled_at = ? WHERE org_id = ? AND user_id = ? "
            "AND consumed_at IS NULL AND cancelled_at IS NULL AND token_hash IS NOT ?",
            (format_time(at), org_id, user_id, keep_hash),
        )
        return int(cursor.rowcount)

    def consume_email_change(self, org_id: str, token_hash: str, at: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE email_changes SET consumed_at = ? WHERE org_id = ? AND token_hash = ? "
            "AND consumed_at IS NULL AND cancelled_at IS NULL",
            (format_time(at), org_id, token_hash),
        )
        return int(cursor.rowcount) == 1

    def purge_email_changes(self, now: datetime) -> int:
        cutoff = format_time(now - EMAIL_CHANGE_RETENTION)
        cursor = self._db.execute(
            "DELETE FROM email_changes WHERE "
            "MAX(COALESCE(consumed_at, ''), COALESCE(cancelled_at, ''), expires_at) <= ?",
            (cutoff,),
        )
        return int(cursor.rowcount)

    def delete_email_changes_for_user(self, org_id: str, user_id: str) -> int:
        cursor = self._db.execute(
            "DELETE FROM email_changes WHERE org_id = ? AND user_id = ?", (org_id, user_id)
        )
        return int(cursor.rowcount)

    def put_invite(self, invite: Invite) -> None:
        clash = self._db.execute(
            "SELECT 1 FROM invites WHERE org_id = ? AND (id = ? OR token_hash = ?)",
            (invite.org_id, invite.id, invite.token_hash),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("an invite with this id or token already exists")
        self._db.execute(
            "INSERT INTO invites (org_id, id, email, role, token_hash, created_by, created_at, "
            "expires_at, resent_count, accepted_at, accepted_user_id, revoked_at, revoke_reason) "
            "VALUES (" + ", ".join(["?"] * 13) + ")",
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
            "SELECT * FROM invites WHERE org_id = ? AND id = ?", (org_id, invite_id)
        ).fetchone()
        return None if row is None else _invite(row)

    def get_invite_by_token_hash(self, org_id: str, token_hash: str) -> Invite | None:
        row = self._db.execute(
            "SELECT * FROM invites WHERE org_id = ? AND token_hash = ?", (org_id, token_hash)
        ).fetchone()
        return None if row is None else _invite(row)

    def list_invites(self, org_id: str) -> list[Invite]:
        rows = self._db.execute(
            "SELECT * FROM invites WHERE org_id = ? ORDER BY created_at DESC, id DESC", (org_id,)
        ).fetchall()
        return [_invite(r) for r in rows]

    def consume_invite(
        self, org_id: str, invite_id: str, token_hash: str, at: datetime, user_id: str
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE invites SET accepted_at = ?, accepted_user_id = ?, email = NULL "
            "WHERE org_id = ? AND id = ? AND token_hash = ? "
            "AND accepted_at IS NULL AND revoked_at IS NULL",
            (format_time(at), user_id, org_id, invite_id, token_hash),
        )
        return int(cursor.rowcount) == 1

    def revoke_invite(self, org_id: str, invite_id: str, at: datetime, reason: str) -> bool:
        cursor = self._db.execute(
            "UPDATE invites SET revoked_at = ?, revoke_reason = ? "
            "WHERE org_id = ? AND id = ? AND accepted_at IS NULL AND revoked_at IS NULL",
            (format_time(at), reason, org_id, invite_id),
        )
        return int(cursor.rowcount) == 1

    def resend_invite(
        self, org_id: str, invite_id: str, token_hash: str, expires_at: datetime
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE invites SET token_hash = ?, expires_at = ?, resent_count = resent_count + 1 "
            "WHERE org_id = ? AND id = ? AND accepted_at IS NULL AND revoked_at IS NULL",
            (token_hash, format_time(expires_at), org_id, invite_id),
        )
        return int(cursor.rowcount) == 1

    def purge_invites(self, now: datetime) -> int:
        cutoff = format_time(now - INVITE_RETENTION)
        cursor = self._db.execute(
            "DELETE FROM invites WHERE "
            "MAX(COALESCE(accepted_at, ''), COALESCE(revoked_at, ''), expires_at) <= ?",
            (cutoff,),
        )
        return int(cursor.rowcount)

    def detach_user_from_invites(self, org_id: str, user_id: str) -> int:
        first = self._db.execute(
            "UPDATE invites SET accepted_user_id = NULL WHERE org_id = ? AND accepted_user_id = ?",
            (org_id, user_id),
        )
        second = self._db.execute(
            "UPDATE invites SET created_by = ? WHERE org_id = ? AND created_by = ?",
            (ERASED_CREATOR, org_id, user_id),
        )
        return int(first.rowcount) + int(second.rowcount)

    # -- password reset (R1.41) ----------------------------------------------------------

    def put_reset_token(self, token: ResetToken) -> None:
        if self._db.execute(
            "SELECT 1 FROM password_reset_tokens WHERE org_id = ? AND token_hash = ?",
            (token.org_id, token.token_hash),
        ).fetchone():
            raise AlreadyExistsError("a reset token with this hash already exists")
        self._db.execute(
            "INSERT INTO password_reset_tokens (org_id, token_hash, user_id, via, created_by, "
            "created_at, expires_at, consumed_at, revoked_at) VALUES (?, ?, ?, ?, ?, "
            "?, ?, ?, ?)",
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
            "UPDATE password_reset_tokens SET consumed_at = ? WHERE org_id = ? "
            "AND token_hash = ? AND consumed_at IS NULL AND revoked_at IS NULL "
            "AND expires_at > ?",
            (format_time(at), org_id, token_hash, format_time(at)),
        )
        return int(cursor.rowcount) == 1

    def revoke_reset_tokens(
        self, org_id: str, user_id: str, at: datetime, *, keep: str | None = None
    ) -> int:
        cursor = self._db.execute(
            "UPDATE password_reset_tokens SET revoked_at = ? WHERE org_id = ? "
            "AND user_id = ? AND token_hash <> ? AND consumed_at IS NULL "
            "AND revoked_at IS NULL",
            (format_time(at), org_id, user_id, keep or ""),
        )
        return int(cursor.rowcount)

    def put_revoke_link(self, link: RevokeLink) -> None:
        if self._db.execute(
            "SELECT 1 FROM api_token_revoke_links WHERE org_id = ? AND token_hash = ?",
            (link.org_id, link.token_hash),
        ).fetchone():
            raise AlreadyExistsError("a revoke link with this hash already exists")
        self._db.execute(
            "INSERT INTO api_token_revoke_links (org_id, token_hash, user_id, api_token_id, "
            "created_at, expires_at, consumed_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
            "UPDATE api_token_revoke_links SET consumed_at = ? WHERE org_id = ? "
            "AND token_hash = ? AND consumed_at IS NULL AND revoked_at IS NULL "
            "AND expires_at > ?",
            (format_time(at), org_id, token_hash, format_time(at)),
        )
        return int(cursor.rowcount) == 1

    def revoke_revoke_links(self, org_id: str, user_id: str, at: datetime) -> int:
        cursor = self._db.execute(
            "UPDATE api_token_revoke_links SET revoked_at = ? WHERE org_id = ? "
            "AND user_id = ? AND consumed_at IS NULL AND revoked_at IS NULL",
            (format_time(at), org_id, user_id),
        )
        return int(cursor.rowcount)

    def set_password_login_block(self, org_id: str, user_id: str, at: datetime, via: str) -> None:
        self._db.execute(
            "DELETE FROM password_login_blocks WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        )
        self._db.execute(
            "INSERT INTO password_login_blocks (org_id, user_id, blocked_at, via) "
            "VALUES (?, ?, ?, ?)",
            (org_id, user_id, format_time(at), via),
        )

    def clear_password_login_block(self, org_id: str, user_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM password_login_blocks WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        )
        return int(cursor.rowcount) > 0

    def purge_password_reset(self, now: datetime) -> int:
        cutoff = format_time(now - RESET_RETENTION)
        first = self._db.execute(
            "DELETE FROM password_reset_tokens WHERE "
            "MAX(expires_at, COALESCE(consumed_at, ''), COALESCE(revoked_at, '')) <= ?",
            (cutoff,),
        )
        second = self._db.execute(
            "DELETE FROM api_token_revoke_links WHERE "
            "MAX(expires_at, COALESCE(consumed_at, '')) <= ?",
            (cutoff,),
        )
        return int(first.rowcount) + int(second.rowcount)

    def erase_password_reset(self, org_id: str, user_id: str) -> int:
        changed = 0
        for sql, params in (
            (
                "DELETE FROM password_reset_tokens WHERE org_id = ? AND user_id = ?",
                (org_id, user_id),
            ),
            (
                "DELETE FROM api_token_revoke_links WHERE org_id = ? AND user_id = ?",
                (org_id, user_id),
            ),
            (
                "DELETE FROM password_login_blocks WHERE org_id = ? AND user_id = ?",
                (org_id, user_id),
            ),
            (
                "UPDATE password_reset_tokens SET created_by = ? "
                "WHERE org_id = ? AND created_by = ?",
                (ERASED_CREATOR, org_id, user_id),
            ),
        ):
            changed += int(self._db.execute(sql, params).rowcount)
        return changed

    def get_reset_token(self, org_id: str, token_hash: str) -> ResetToken | None:
        row = self._db.execute(
            "SELECT * FROM password_reset_tokens WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        ).fetchone()
        return None if row is None else _reset_token(row)

    def list_reset_tokens(self, org_id: str) -> list[ResetToken]:
        rows = self._db.execute(
            "SELECT * FROM password_reset_tokens WHERE org_id = ? ORDER BY created_at, token_hash",
            (org_id,),
        ).fetchall()
        return [_reset_token(r) for r in rows]

    def get_revoke_link(self, org_id: str, token_hash: str) -> RevokeLink | None:
        row = self._db.execute(
            "SELECT * FROM api_token_revoke_links WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        ).fetchone()
        return None if row is None else _revoke_link(row)

    def list_revoke_links(self, org_id: str) -> list[RevokeLink]:
        rows = self._db.execute(
            "SELECT * FROM api_token_revoke_links WHERE org_id = ? ORDER BY created_at, token_hash",
            (org_id,),
        ).fetchall()
        return [_revoke_link(r) for r in rows]

    def get_password_login_block(self, org_id: str, user_id: str) -> PasswordLoginBlock | None:
        row = self._db.execute(
            "SELECT * FROM password_login_blocks WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        ).fetchone()
        return None if row is None else _login_block(row)

    def adopt_security_log_rows(self, org_id: str, email: str, user_id: str) -> int:
        cursor = self._db.execute(
            "UPDATE security_log SET subject = ? WHERE org_id = ? AND subject IS NULL "
            "AND json_extract(details, '$.email') = ?",
            (user_id, org_id, email),
        )
        return int(cursor.rowcount)

    def set_user_disabled(self, org_id: str, user_id: str, at: datetime | None) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self._db.execute(
            "UPDATE users SET disabled_at = ? WHERE org_id = ? AND id = ?",
            (None if at is None else format_time(at), org_id, user_id),
        )
        return True

    def put_credential(self, credential: Credential) -> None:
        user = self.get_user(credential.org_id, credential.user_id)
        if user is None or user.erased:
            raise NotFoundError(f"no active user '{credential.user_id}'")
        self._db.execute(
            "INSERT INTO credentials (org_id, user_id, password_hash, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (org_id, user_id) DO UPDATE SET "
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
            "SELECT * FROM credentials WHERE org_id = ? AND user_id = ?", (org_id, user_id)
        ).fetchone()
        return None if row is None else _credential(row)

    def put_setup_token(self, token: SetupToken) -> None:
        if self.get_setup_token(token.org_id, token.token_hash) is not None:
            raise AlreadyExistsError("this setup token already exists")
        self._db.execute(
            "INSERT INTO setup_tokens (org_id, token_hash, created_at, expires_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?)",
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
            "SELECT * FROM setup_tokens WHERE org_id = ? AND token_hash = ?", (org_id, token_hash)
        ).fetchone()
        return None if row is None else _setup_token(row)

    def consume_setup_token(self, org_id: str, token_hash: str, now: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE setup_tokens SET consumed_at = ? WHERE org_id = ? AND token_hash = ? "
            "AND consumed_at IS NULL AND expires_at > ?",
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
            "VALUES (" + ", ".join(["?"] * 13) + ")",
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
            "UPDATE auth_sessions SET step_up_at = ? WHERE org_id = ? AND id = ? "
            "AND revoked_at IS NULL",
            (format_time(at), org_id, session_id),
        )
        return cursor.rowcount == 1

    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        row = self._db.execute(
            "SELECT * FROM auth_sessions WHERE org_id = ? AND id = ?", (org_id, session_id)
        ).fetchone()
        return None if row is None else _auth_session(row)

    def touch_auth_session(self, org_id: str, session_id: str, at: datetime) -> None:
        self._db.execute(
            "UPDATE auth_sessions SET last_active_at = ? WHERE org_id = ? AND id = ?",
            (format_time(at), org_id, session_id),
        )

    def revoke_auth_session(self, org_id: str, session_id: str, at: datetime, reason: str) -> bool:
        cursor = self._db.execute(
            "UPDATE auth_sessions SET revoked_at = ?, revoke_reason = ? "
            "WHERE org_id = ? AND id = ? AND revoked_at IS NULL",
            (format_time(at), reason, org_id, session_id),
        )
        return cursor.rowcount == 1

    def put_auth_token(self, token: AuthToken) -> None:
        if self.get_auth_token(token.org_id, token.token_hash) is not None:
            raise AlreadyExistsError("this token already exists")
        self._db.execute(
            "INSERT INTO auth_tokens (org_id, token_hash, session_id, kind, created_at, "
            "expires_at, used_at) VALUES (" + ", ".join(["?"] * 7) + ")",
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
            "SELECT * FROM auth_tokens WHERE org_id = ? AND token_hash = ?", (org_id, token_hash)
        ).fetchone()
        return None if row is None else _auth_token(row)

    def use_auth_token(self, org_id: str, token_hash: str, at: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE auth_tokens SET used_at = ? WHERE org_id = ? AND token_hash = ? "
            "AND used_at IS NULL",
            (format_time(at), org_id, token_hash),
        )
        return cursor.rowcount == 1

    def put_auth_failure(self, failure: AuthFailure) -> None:
        self._db.execute(
            "INSERT INTO auth_failures (key, count, last_failure_at, locked_until) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET count = excluded.count, "
            "last_failure_at = excluded.last_failure_at, locked_until = excluded.locked_until",
            (
                failure.key,
                failure.count,
                format_time(failure.last_failure_at),
                None if failure.locked_until is None else format_time(failure.locked_until),
            ),
        )

    def get_auth_failure(self, key: str) -> AuthFailure | None:
        row = self._db.execute("SELECT * FROM auth_failures WHERE key = ?", (key,)).fetchone()
        return None if row is None else _auth_failure(row)

    def delete_auth_failure(self, key: str) -> None:
        self._db.execute("DELETE FROM auth_failures WHERE key = ?", (key,))

    def purge_auth_records(self, sessions_ended_before: datetime, failures_before: datetime) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        ended = format_time(sessions_ended_before)
        # a session's end is its revocation or its absolute end, whichever is earlier
        gone = (
            "SELECT org_id, id FROM auth_sessions WHERE "
            "MIN(COALESCE(revoked_at, absolute_expires_at), absolute_expires_at) <= ?"
        )
        tokens = self._db.execute(
            "DELETE FROM auth_tokens WHERE (org_id, session_id) IN (" + gone + ")", (ended,)
        ).rowcount
        sessions = self._db.execute(
            "DELETE FROM auth_sessions WHERE (org_id, id) IN (" + gone + ")", (ended,)
        ).rowcount
        failures = self._db.execute(
            "DELETE FROM auth_failures WHERE MAX(last_failure_at, COALESCE(locked_until, "
            "last_failure_at)) <= ?",
            (format_time(failures_before),),
        ).rowcount
        return int(tokens + sessions + failures)

    def set_auth_session_password_confirmed(
        self, org_id: str, session_id: str, at: datetime
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE auth_sessions SET password_confirmed_at = ? WHERE org_id = ? AND id = ?",
            (format_time(at), org_id, session_id),
        )
        return bool(cursor.rowcount == 1)

    def clear_session_proofs(self, org_id: str, user_id: str) -> int:
        cursor = self._db.execute(
            "UPDATE auth_sessions SET password_confirmed_at = NULL, step_up_at = NULL "
            "WHERE org_id = ? AND user_id = ? "
            "AND (password_confirmed_at IS NOT NULL OR step_up_at IS NOT NULL)",
            (org_id, user_id),
        )
        return int(cursor.rowcount)

    # -- MFA (R1.38) --

    def set_auth_session_mfa(self, org_id: str, session_id: str, at: datetime, method: str) -> bool:
        cursor = self._db.execute(
            "UPDATE auth_sessions SET mfa_verified_at = ?, mfa_method = ? "
            "WHERE org_id = ? AND id = ?",
            (format_time(at), method, org_id, session_id),
        )
        return bool(cursor.rowcount == 1)

    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        row = self._db.execute(
            "SELECT * FROM mfa_factors WHERE org_id = ? AND user_id = ?", (org_id, user_id)
        ).fetchone()
        return None if row is None else _mfa_factor(row)

    def put_mfa_factor(self, factor: MfaFactor) -> None:
        cursor = self._db.execute(
            "INSERT INTO mfa_factors (org_id, user_id, id, secret_enc, key_id, created_at, "
            "confirmed_at, last_used_step, password_checked_at) VALUES ("
            + ", ".join(["?"] * 9)
            + ") ON CONFLICT (org_id, user_id) DO UPDATE SET id = excluded.id, "
            "secret_enc = excluded.secret_enc, key_id = excluded.key_id, "
            "created_at = excluded.created_at, confirmed_at = excluded.confirmed_at, "
            "last_used_step = excluded.last_used_step, "
            "password_checked_at = excluded.password_checked_at "
            "WHERE mfa_factors.confirmed_at IS NULL",
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
            "UPDATE mfa_factors SET confirmed_at = ?, last_used_step = ? "
            "WHERE org_id = ? AND user_id = ? AND id = ? AND confirmed_at IS NULL",
            (format_time(at), step, org_id, user_id, factor_id),
        )
        return bool(cursor.rowcount == 1)

    def advance_mfa_step(self, org_id: str, user_id: str, factor_id: str, step: int) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_factors SET last_used_step = ? WHERE org_id = ? AND user_id = ? "
            "AND id = ? AND (last_used_step IS NULL OR last_used_step < ?)",
            (step, org_id, user_id, factor_id, step),
        )
        return bool(cursor.rowcount == 1)

    def delete_mfa_factor(self, org_id: str, user_id: str) -> bool:
        self.erased = True  # the store truncates the WAL after this commit
        gone = self._db.execute(
            "DELETE FROM mfa_factors WHERE org_id = ? AND user_id = ?", (org_id, user_id)
        ).rowcount
        self._db.execute(
            "DELETE FROM mfa_recovery_codes WHERE org_id = ? AND user_id = ?",
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
            "UPDATE mfa_factors SET secret_enc = ?, key_id = ? "
            "WHERE org_id = ? AND user_id = ? AND id = ? AND key_id = ?",
            (secret_enc, key_id, org_id, user_id, factor_id, from_key_id),
        )
        return bool(cursor.rowcount == 1)

    def delete_mfa_challenges(self, org_id: str, user_id: str) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        return int(
            self._db.execute(
                "DELETE FROM mfa_challenges WHERE org_id = ? AND user_id = ?", (org_id, user_id)
            ).rowcount
        )

    def erase_mfa_user(self, org_id: str, user_id: str) -> bool:
        self.erased = True  # the store truncates the WAL after this commit
        existed = self.delete_mfa_factor(org_id, user_id)
        self._db.execute(
            "DELETE FROM mfa_challenges WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        )
        self._db.execute(
            "UPDATE org_auth_settings SET updated_by = 'erased-user' "
            "WHERE org_id = ? AND updated_by = ?",
            (org_id, user_id),
        )
        return existed

    def delete_all_mfa(self, org_id: str) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        removed = self._db.execute("DELETE FROM mfa_factors WHERE org_id = ?", (org_id,)).rowcount
        self._db.execute("DELETE FROM mfa_recovery_codes WHERE org_id = ?", (org_id,))
        self._db.execute("DELETE FROM mfa_challenges WHERE org_id = ?", (org_id,))
        return int(removed)

    def count_mfa_recovery_codes(self, org_id: str, user_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM mfa_recovery_codes "
            "WHERE org_id = ? AND user_id = ? AND used_at IS NULL",
            (org_id, user_id),
        ).fetchone()
        return int(row["n"])

    def replace_mfa_recovery_codes(
        self, org_id: str, user_id: str, code_hashes: Sequence[str], at: datetime
    ) -> None:
        self.erased = True  # the store truncates the WAL after this commit
        self._db.execute(
            "DELETE FROM mfa_recovery_codes WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        )
        for code_hash in code_hashes:
            self._db.execute(
                "INSERT INTO mfa_recovery_codes (org_id, user_id, code_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (org_id, user_id, code_hash, format_time(at)),
            )

    def use_mfa_recovery_code(
        self, org_id: str, user_id: str, code_hash: str, at: datetime
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_recovery_codes SET used_at = ? WHERE org_id = ? AND user_id = ? "
            "AND code_hash = ? AND used_at IS NULL",
            (format_time(at), org_id, user_id, code_hash),
        )
        return bool(cursor.rowcount == 1)

    def get_mfa_challenge(self, org_id: str, token_hash: str) -> MfaChallenge | None:
        row = self._db.execute(
            "SELECT * FROM mfa_challenges WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        ).fetchone()
        return None if row is None else _mfa_challenge(row)

    def put_mfa_challenge(self, challenge: MfaChallenge) -> None:
        if self.get_mfa_challenge(challenge.org_id, challenge.token_hash) is not None:
            raise AlreadyExistsError("this challenge already exists")
        self._db.execute(
            "INSERT INTO mfa_challenges (org_id, token_hash, user_id, purpose, factor_id, "
            "created_at, expires_at, attempts, consumed_at) VALUES (" + ", ".join(["?"] * 9) + ")",
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
            "UPDATE mfa_challenges SET consumed_at = ? WHERE org_id = ? AND token_hash = ? "
            "AND consumed_at IS NULL AND expires_at > ?",
            (format_time(now), org_id, token_hash, format_time(now)),
        )
        return bool(cursor.rowcount == 1)

    def add_mfa_challenge_attempt(self, org_id: str, token_hash: str) -> int | None:
        cursor = self._db.execute(
            "UPDATE mfa_challenges SET attempts = attempts + 1 WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        )
        if cursor.rowcount != 1:
            return None
        row = self._db.execute(
            "SELECT attempts FROM mfa_challenges WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        ).fetchone()
        return int(row["attempts"])

    def set_mfa_challenge_factor(self, org_id: str, token_hash: str, factor_id: str) -> bool:
        cursor = self._db.execute(
            "UPDATE mfa_challenges SET factor_id = ? WHERE org_id = ? AND token_hash = ? "
            "AND factor_id IS NULL",
            (factor_id, org_id, token_hash),
        )
        return bool(cursor.rowcount == 1)

    def get_org_auth_settings(self, org_id: str) -> OrgAuthSettings:
        row = self._db.execute(
            "SELECT * FROM org_auth_settings WHERE org_id = ?", (org_id,)
        ).fetchone()
        return OrgAuthSettings(org_id=org_id) if row is None else _org_auth_settings(row)

    def put_org_auth_settings(self, settings: OrgAuthSettings) -> None:
        self._db.execute(
            "INSERT INTO org_auth_settings (org_id, mfa_required, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (org_id) DO UPDATE SET "
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
        self.erased = True  # the store truncates the WAL after this commit
        challenges = self._db.execute(
            "DELETE FROM mfa_challenges WHERE expires_at <= ?",
            (format_time(now - MFA_CHALLENGE_KEEP),),
        ).rowcount
        factors = self._db.execute(
            "DELETE FROM mfa_factors WHERE confirmed_at IS NULL AND created_at <= ?",
            (format_time(now - MFA_PENDING_LIFETIME),),
        ).rowcount
        return int(challenges + factors)

    # -- idempotency keys (R1.35) --

    def idempotency_get(self, scope: Scope, key_hash: str) -> StoredIdempotency | None:
        row = self._db.execute(
            "SELECT * FROM idempotency_keys WHERE org_id = ? AND actor = ? AND method = ? "
            "AND route = ? AND key_hash = ?",
            (scope.org_id, scope.actor, scope.method, scope.route, key_hash),
        ).fetchone()
        if row is None:
            return None
        found = self._db.execute(
            "SELECT subject FROM idempotency_subjects WHERE org_id = ? AND actor = ? "
            "AND method = ? AND route = ? AND key_hash = ?",
            (scope.org_id, scope.actor, scope.method, scope.route, key_hash),
        ).fetchall()
        return _idempotency(row, frozenset(r["subject"] for r in found))

    def idempotency_put(self, row: StoredIdempotency) -> None:
        sealed = row.sealed
        self._db.execute(
            "INSERT OR REPLACE INTO idempotency_keys (org_id, actor, method, route, key_hash, "
            "fingerprint, state, attempt, fence, created_at, lease_until, expires_at, "
            "status, payload, nonce, key_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "DELETE FROM idempotency_subjects WHERE org_id = ? AND actor = ? "
            "AND method = ? AND route = ? AND key_hash = ?",
            key,
        )
        for person in sorted(row.subjects):
            self._db.execute(
                "INSERT INTO idempotency_subjects (org_id, actor, method, route, key_hash, "
                "subject) VALUES (?, ?, ?, ?, ?, ?)",
                (*key, person),
            )

    def idempotency_delete(self, scope: Scope, key_hash: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM idempotency_keys WHERE org_id = ? AND actor = ? AND method = ? "
            "AND route = ? AND key_hash = ?",
            (scope.org_id, scope.actor, scope.method, scope.route, key_hash),
        )
        return bool(cursor.rowcount)

    def purge_idempotency_org(self, org_id: str) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        cursor = self._db.execute("DELETE FROM idempotency_keys WHERE org_id = ?", (org_id,))
        return int(cursor.rowcount)

    def purge_idempotency_actor(self, org_id: str, person: str) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        cursor = self._db.execute(
            "DELETE FROM idempotency_keys WHERE org_id = ? AND (actor = ? OR "
            "(actor, method, route, key_hash) IN (SELECT actor, method, route, key_hash "
            "FROM idempotency_subjects WHERE org_id = ? AND subject = ?))",
            (org_id, person, org_id, person),
        )
        return int(cursor.rowcount)

    def purge_idempotency_expired(self, now: datetime, limit: int) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        cursor = self._db.execute(
            "DELETE FROM idempotency_keys WHERE (org_id, actor, method, route, key_hash) IN ("
            "SELECT org_id, actor, method, route, key_hash FROM idempotency_keys "
            "WHERE expires_at <= ? ORDER BY expires_at LIMIT ?)",
            (format_time(now), limit),
        )
        return int(cursor.rowcount)

    def idempotency_rows(self, org_id: str) -> list[StoredIdempotency]:
        rows = self._db.execute(
            "SELECT * FROM idempotency_keys WHERE org_id = ? "
            "ORDER BY actor, method, route, key_hash",
            (org_id,),
        ).fetchall()
        people: dict[tuple[str, str, str, str], set[str]] = {}
        for r in self._db.execute(
            "SELECT actor, method, route, key_hash, subject FROM idempotency_subjects "
            "WHERE org_id = ?",
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
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
            "SELECT * FROM security_log WHERE org_id = ? AND id = ?", (org_id, row_id)
        ).fetchone()
        return None if row is None else _security_log(row)

    def take_security_log_count(self, org_id: str, row_id: str) -> SecurityLogEntry | None:
        found = self.get_security_log(org_id, row_id)
        if found is not None:
            self.erased = True  # the store truncates the WAL after this commit
            self._db.execute(
                "DELETE FROM security_log WHERE org_id = ? AND id = ?", (org_id, row_id)
            )
        return found

    def list_expired_security_log(self, now: datetime, reason: str) -> list[SecurityLogEntry]:
        rows = self._db.execute(
            "SELECT * FROM security_log WHERE expires_at <= ? "
            "AND COALESCE(json_extract(details, '$.reason'), '') = ? ORDER BY at, org_id, id",
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
            "UPDATE security_log SET details = ? WHERE org_id = ? AND id = ?",
            (json.dumps(details, sort_keys=True, separators=(",", ":")), org_id, row_id),
        )
        return count

    def purge_security_log(self, now: datetime) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        cursor = self._db.execute(
            "DELETE FROM security_log WHERE expires_at <= ? "
            "AND COALESCE(json_extract(details, '$.reason'), '') <> '" + HALT_PENDING_REASON + "'",
            (format_time(now),),
        )
        return int(cursor.rowcount)

    def user_identifier_key(self, org_id: str, user_id: str) -> bytes | None:
        row = self._db.execute(
            "SELECT identifier_key, erased_at FROM users WHERE org_id = ? AND id = ?",
            (org_id, user_id),
        ).fetchone()
        if row is None or row["erased_at"] is not None:
            return None
        if row["identifier_key"] is not None:
            return bytes(row["identifier_key"])
        key = new_identifier_key()  # a user from before migration 7
        self._db.execute(
            "UPDATE users SET identifier_key = ? WHERE org_id = ? AND id = ?",
            (key, org_id, user_id),
        )
        return key

    def org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        found = self._db.execute(
            "SELECT key FROM identifier_keys WHERE org_id = ? AND epoch = ?",
            (org_id, epoch),
        ).fetchone()
        if found is not None:  # a key, or the tombstone of a destroyed one (None)
            return None if found["key"] is None else bytes(found["key"])
        newer = self._db.execute(
            "SELECT 1 FROM identifier_keys WHERE org_id = ? AND epoch > ?", (org_id, epoch)
        ).fetchone()
        if newer is not None:
            raise StoreError("no identifier key is made for an epoch older than the newest")
        self._db.execute(
            "INSERT INTO identifier_keys (org_id, epoch, key, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (org_id, epoch) DO NOTHING",
            (org_id, epoch, new_identifier_key(), format_time(self._clock())),
        )
        row = self._db.execute(
            "SELECT key FROM identifier_keys WHERE org_id = ? AND epoch = ?",
            (org_id, epoch),
        ).fetchone()
        return None if row is None or row["key"] is None else bytes(row["key"])

    def destroy_org_identifier_keys(self, before_epoch: int) -> int:
        cursor = self._db.execute(
            "UPDATE identifier_keys SET key = NULL, destroyed_at = ? "
            "WHERE epoch < ? AND key IS NOT NULL",
            (format_time(self._clock()), before_epoch),
        )
        return int(cursor.rowcount)

    def get_org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        row = self._db.execute(
            "SELECT key FROM identifier_keys WHERE org_id = ? AND epoch = ?",
            (org_id, epoch),
        ).fetchone()
        return None if row is None or row["key"] is None else bytes(row["key"])

    def erase_security_log(self, org_id: str, user_id: str, email: str | None) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        cursor = self._db.execute(
            "DELETE FROM security_log WHERE org_id = ? AND (subject = ? OR "
            "json_extract(details, '$.email') = ?)",
            (org_id, user_id, email),
        )
        self._db.execute(
            "UPDATE security_log SET actor = ?, details = json_remove(details, "
            + ", ".join("?" for _ in ERASED_DETAIL_KEYS)
            + ") WHERE org_id = ? AND actor = ?",
            (ERASED_ACTOR, *(f"$.{k}" for k in ERASED_DETAIL_KEYS), org_id, user_id),
        )
        return int(cursor.rowcount)

    def put_api_token(self, token: ApiToken) -> None:
        clash = self._db.execute(
            "SELECT 1 FROM api_tokens WHERE org_id = ? AND (id = ? OR name = ? OR token_hash = ?)",
            (token.org_id, token.id, token.name, token.token_hash),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("this token already exists")
        self._db.execute(
            "INSERT INTO api_tokens (org_id, id, name, scopes, token_hash, created_by, created_at, "
            "expires_at, revoked_at, revoked_by) VALUES (" + ", ".join(["?"] * 10) + ")",
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
            "SELECT * FROM api_tokens WHERE org_id = ? AND id = ?", (org_id, token_id)
        ).fetchone()
        return None if row is None else _api_token(row)

    def revoke_api_token(self, org_id: str, token_id: str, at: datetime, by: str) -> bool:
        cursor = self._db.execute(
            "UPDATE api_tokens SET revoked_at = ?, revoked_by = ? "
            "WHERE org_id = ? AND id = ? AND revoked_at IS NULL",
            (format_time(at), by, org_id, token_id),
        )
        return cursor.rowcount == 1

    def rename_api_token(self, org_id: str, token_id: str, name: str) -> bool:
        if (
            self._db.execute(
                "SELECT 1 FROM api_tokens WHERE org_id = ? AND id = ?", (org_id, token_id)
            ).fetchone()
            is None
        ):
            return False
        clash = self._db.execute(
            "SELECT 1 FROM api_tokens WHERE org_id = ? AND name = ? AND id <> ?",
            (org_id, name, token_id),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("a token with this name exists")
        self._db.execute(
            "UPDATE api_tokens SET name = ? WHERE org_id = ? AND id = ?", (name, org_id, token_id)
        )
        return True

    def delete_api_token(self, org_id: str, token_id: str, now: datetime) -> bool:
        cursor = self._db.execute(
            "DELETE FROM api_tokens WHERE org_id = ? AND id = ? "
            "AND (revoked_at IS NOT NULL OR expires_at < ?)",
            (org_id, token_id, format_time(now)),
        )
        if cursor.rowcount != 1:
            return False
        self._db.execute(
            "DELETE FROM api_token_revoke_links WHERE org_id = ? AND api_token_id = ?",
            (org_id, token_id),
        )
        return True

    def _rewrite_issuer(
        self, org_id: str, row: sqlite3.Row, *, created: bool, revoked: bool
    ) -> None:
        name = row["name"]
        if created:
            taken = {
                r["name"]
                for r in self._db.execute(
                    "SELECT name FROM api_tokens WHERE org_id = ? AND id <> ?", (org_id, row["id"])
                )
            }
            name = next_erased_token_name(row["id"], taken, current=row["name"])
        self._db.execute(
            "UPDATE api_tokens SET name = ?, created_by = ?, revoked_by = ? "
            "WHERE org_id = ? AND id = ?",
            (
                name,
                ERASED_ACTOR if created else row["created_by"],
                ERASED_ACTOR if revoked else row["revoked_by"],
                org_id,
                row["id"],
            ),
        )

    def erase_api_token_issuer(self, org_id: str, user_id: str) -> int:
        rows = self._db.execute(
            "SELECT * FROM api_tokens WHERE org_id = ? AND (created_by = ? OR revoked_by = ?) "
            "ORDER BY id",
            (org_id, user_id, user_id),
        ).fetchall()
        for row in rows:
            self._rewrite_issuer(
                org_id,
                row,
                created=row["created_by"] == user_id,
                revoked=row["revoked_by"] == user_id,
            )
        return len(rows)

    def _person_gone(self, org_id: str, who: str | None) -> bool:
        if who is None or not who.startswith("human:"):
            return False
        found = self._db.execute(
            "SELECT 1 FROM users WHERE org_id = ? AND id = ? AND erased_at IS NULL", (org_id, who)
        ).fetchone()
        return found is None

    def normalise_api_token_issuer(self, org_id: str, token_id: str) -> int:
        row = self._db.execute(
            "SELECT * FROM api_tokens WHERE org_id = ? AND id = ?", (org_id, token_id)
        ).fetchone()
        if row is None:
            return 0
        created = self._person_gone(org_id, row["created_by"])
        revoked = self._person_gone(org_id, row["revoked_by"])
        if not (created or revoked):
            return 0
        self._rewrite_issuer(org_id, row, created=created, revoked=revoked)
        return 1

    def api_token_name_retired(self, org_id: str, keyed: Sequence[tuple[int, str]]) -> bool:
        for epoch, name_hash in keyed:
            found = self._db.execute(
                "SELECT 1 FROM api_token_retired_names "
                "WHERE org_id = ? AND epoch = ? AND name_hash = ?",
                (org_id, epoch, name_hash),
            ).fetchone()
            if found is not None:
                return True
        return False

    def retire_api_token_name(
        self, org_id: str, name_hash: str, epoch: int, retired_at: datetime
    ) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO api_token_retired_names (org_id, epoch, name_hash, retired_at) "
            "VALUES (?, ?, ?, ?)",
            (org_id, epoch, name_hash, format_time(retired_at)),
        )

    def ext_get(
        self, org_id: str, namespace: str, kind: str, record_id: str
    ) -> ExtensionRecord | None:
        row = self._db.execute(
            "SELECT * FROM extension_records WHERE org_id = ? AND namespace = ? AND kind = ? "
            "AND record_id = ?",
            (org_id, namespace, kind, record_id),
        ).fetchone()
        return None if row is None else _extension(row)

    def ext_find(
        self, org_id: str, namespace: str, kind: str, unique_key: str
    ) -> ExtensionRecord | None:
        row = self._db.execute(
            "SELECT * FROM extension_records WHERE org_id = ? AND namespace = ? AND kind = ? "
            "AND unique_key = ?",
            (org_id, namespace, kind, unique_key),
        ).fetchone()
        return None if row is None else _extension(row)

    def ext_put(self, record: ExtensionRecord) -> None:
        clash = self._db.execute(
            "SELECT 1 FROM extension_records WHERE org_id = ? AND namespace = ? AND kind = ? "
            "AND (record_id = ? OR (unique_key IS NOT NULL AND unique_key = ?))",
            (record.org_id, record.namespace, record.kind, record.record_id, record.unique_key),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("this record already exists")
        self._db.execute(
            "INSERT INTO extension_records (org_id, namespace, kind, record_id, unique_key, "
            "group_key, sort_key, erasable, body) VALUES (" + ", ".join(["?"] * 9) + ")",
            (
                record.org_id,
                record.namespace,
                record.kind,
                record.record_id,
                record.unique_key,
                record.group_key,
                record.sort_key,
                int(record.erasable),
                record.body,
            ),
        )

    def ext_erase(self, org_id: str) -> int:
        cur = self._db.execute(
            "DELETE FROM extension_records WHERE org_id = ? AND erasable = 1", (org_id,)
        )
        return int(cur.rowcount)

    def put_agent_credential(self, credential: AgentCredential) -> None:
        clash = self._db.execute(
            "SELECT 1 FROM agent_credentials WHERE org_id = ? AND (id = ? OR token_hash = ?)",
            (credential.org_id, credential.id, credential.token_hash),
        ).fetchone()
        if clash is not None:
            raise AlreadyExistsError("this credential already exists")
        self._db.execute(
            "INSERT INTO agent_credentials (org_id, id, agent_id, team, fingerprint, scopes, "
            "token_hash, created_by, created_at, expires_at, revoked_at, revoked_by) VALUES ("
            + ", ".join(["?"] * 12)
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
            "SELECT * FROM agent_credentials WHERE org_id = ? AND id = ?",
            (org_id, credential_id),
        ).fetchone()
        return None if row is None else _agent_credential(row)

    def list_agent_credentials(self, org_id: str) -> list[AgentCredential]:
        rows = self._db.execute(
            "SELECT * FROM agent_credentials WHERE org_id = ? ORDER BY created_at, id", (org_id,)
        ).fetchall()
        return [_agent_credential(row) for row in rows]

    def revoke_agent_credential(
        self, org_id: str, credential_id: str, at: datetime, by: str
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE agent_credentials SET revoked_at = ?, revoked_by = ? "
            "WHERE org_id = ? AND id = ? AND revoked_at IS NULL",
            (format_time(at), by, org_id, credential_id),
        )
        return cursor.rowcount == 1

    def shorten_agent_credential(
        self, org_id: str, credential_id: str, expires_at: datetime
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE agent_credentials SET expires_at = ? "
            "WHERE org_id = ? AND id = ? AND expires_at > ?",
            (format_time(expires_at), org_id, credential_id, format_time(expires_at)),
        )
        return cursor.rowcount == 1

    def count_live_agent_credentials(self, org_id: str, agent_id: str, now: datetime) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM agent_credentials WHERE org_id = ? AND agent_id = ? "
            "AND revoked_at IS NULL AND expires_at > ?",
            (org_id, agent_id, format_time(now)),
        ).fetchone()
        return int(row["n"])

    def erase_agent_credential_user(
        self, org_id: str, user_id: str, now: datetime
    ) -> list[AgentCredential]:
        self.erased = True  # the store truncates the WAL after this commit
        rows = self._db.execute(
            "SELECT * FROM agent_credentials WHERE org_id = ? AND created_by = ? "
            "AND revoked_at IS NULL AND expires_at > ? ORDER BY created_at, id",
            (org_id, user_id, format_time(now)),
        ).fetchall()
        live = [_agent_credential(row) for row in rows]
        self._db.execute(
            "UPDATE agent_credentials SET created_by = ? WHERE org_id = ? AND created_by = ?",
            (ERASED_ACTOR, org_id, user_id),
        )
        self._db.execute(
            "UPDATE agent_credentials SET revoked_by = ? WHERE org_id = ? AND revoked_by = ?",
            (ERASED_ACTOR, org_id, user_id),
        )
        return live

    def purge_agent_credentials(self, now: datetime) -> int:
        self.erased = True  # the store truncates the WAL after this commit
        cutoff = format_time(now - AGENT_CREDENTIAL_ENDED_RETENTION)
        cursor = self._db.execute(
            "DELETE FROM agent_credentials "
            "WHERE MIN(COALESCE(revoked_at, expires_at), expires_at) <= ?",
            (cutoff,),
        )
        return int(cursor.rowcount)

    def get_approval(self, approval_id: str) -> StoredApproval | None:
        row = self._db.execute(
            "SELECT data, version FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return None if row is None else _stored_approval(row)

    def delete_approval_if_due(
        self, approval_id: str, now: datetime, *, expected_version: int | None = None
    ) -> bool:
        row = self._db.execute(
            "SELECT data, version FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            return False
        stored = _stored_approval(row)
        if not stored.approval.retention_over(now):
            return False
        if expected_version is not None and stored.version != expected_version:
            return False
        cursor = self._db.execute(
            "DELETE FROM approvals WHERE id = ? AND version = ?", (approval_id, stored.version)
        )
        return cursor.rowcount == 1

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
            "SELECT data, version FROM approvals WHERE decided_by = ? "
            "AND approval_mode = 'solo' AND decided_at > ? "
            "AND status IN ('approved', 'consumed') ORDER BY decided_at, id",
            (decided_by, format_time(after)),
        ).fetchall()
        return [_stored_approval(row).approval for row in rows]

    def approvals_matching(
        self, run_id: str, agent_id: str, status: ApprovalStatus
    ) -> list[StoredApproval]:
        rows = self._db.execute(
            "SELECT data, version FROM approvals WHERE run_id = ? AND agent_id = ? AND status = ? "
            "ORDER BY id",
            (run_id, agent_id, status.value),
        ).fetchall()
        return [_stored_approval(row) for row in rows]

    def get_reservation(self, reservation_id: str) -> StoredReservation | None:
        row = self._db.execute(
            "SELECT data, version FROM reservations WHERE id = ?", (reservation_id,)
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


def _read_head(db: sqlite3.Connection, stream: str) -> StreamHead | None:
    row = db.execute(
        "SELECT stream, seq, hash, at FROM stream_heads WHERE stream = ?", (stream,)
    ).fetchone()
    if row is None:
        return None
    return StreamHead(
        stream=row["stream"],
        seq=row["seq"],
        hash=row["hash"],
        at=_ledger_time(row["at"], row["stream"], None),
    )


_R = TypeVar("_R")


def _wrap(exc: sqlite3.Error) -> StoreError:
    """A database error as a `StoreError`; "locked"/"busy" become `StoreBusyError`."""
    text = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return StoreBusyError(str(exc))
    return StoreError(f"{type(exc).__name__}: {exc}")


class SqliteStore(Store):
    """State and ledger in one SQLite file (or `":memory:"` for tests).

    Opening is safe when several processes open the same new file at once: switching to WAL is
    retried while another process holds the lock, and migrations run only if the schema is behind
    (re-checked under the write lock).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock = utc_now,
        busy_timeout_ms: int = 10_000,
        read_only: bool = False,
    ) -> None:
        super().__init__(clock=clock)
        self.subject_backfill: SubjectBackfill | None = None  # set by migration 8
        self._lock = threading.RLock()
        self._clean_streams: set[str] = set()  # streams checked for a damaged time (ND238)
        self._local = threading.local()
        self._busy_timeout = busy_timeout_ms / 1000
        self._wal_dirty = False  # an erasure's WAL truncate was busy and is still owed
        self._wal_next_try = 0.0  # monotonic time before which a busy truncate is not retried
        self.wal_retry_seconds = 5.0  # a busy truncate is retried at most this often
        self.read_retry_seconds = 2.0  # how long a read waits out a 'locked' answer
        self._read_only = read_only and str(path) != ":memory:"
        if read_only and str(path) != ":memory:":
            self._open_read_only(path, busy_timeout_ms)
            return
        try:
            self._db = sqlite3.connect(
                str(path), isolation_level=None, check_same_thread=False, timeout=self._busy_timeout
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._db.execute("PRAGMA foreign_keys = ON")
            # erased rows (email, names, old ciphertext) are overwritten, not left in free pages,
            # whatever this SQLite build defaults to (R1.30 review 3, m3)
            self._db.execute("PRAGMA secure_delete = ON")
            if str(path) != ":memory:":
                self._retry_while_locked(lambda: self._db.execute("PRAGMA journal_mode = WAL"))
            self._db.execute("PRAGMA synchronous = FULL")
            if self._current_version() < len(MIGRATIONS):
                self._retry_while_locked(self._migrate)
            elif self._current_version() > len(MIGRATIONS):
                self._refuse_newer(self._current_version())
            self._schema = self._current_version()
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc
        # a crash or a busy close can leave erased rows in `<file>-wal`; the flag does not
        # survive a restart, so a log that is not empty at open is truncated (R1.30 m5)
        wal = Path(f"{path}-wal")
        if str(path) != ":memory:" and wal.exists() and wal.stat().st_size > 0:
            self._truncate_wal()

    def _open_read_only(self, path: str | Path, busy_timeout_ms: int) -> None:
        """A strictly read-only open (`mode=ro`, `query_only`): no WAL switch, no migration, no
        checkpoint or truncate; the file's bytes (and its `-wal`) are left as they are."""
        try:
            self._db = sqlite3.connect(
                f"{Path(path).absolute().as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=self._busy_timeout,
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._db.execute("PRAGMA query_only = ON")
            version = self._current_version()
            if version > len(MIGRATIONS):
                self._refuse_newer(version)
            if version < len(MIGRATIONS):
                raise StoreError("the database needs a migration; this open is read-only")
            self._schema = version
        except sqlite3.Error as exc:
            self._close_failed_open()
            raise _wrap(exc) from exc
        except BaseException:
            self._close_failed_open()  # a refused open leaks nothing
            raise

    def _close_failed_open(self) -> None:
        db = self.__dict__.get("_db")
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def _retry_while_locked(self, action: Callable[[], object]) -> None:
        deadline = time.monotonic() + self._busy_timeout
        delay = 0.01
        while True:
            try:
                action()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.25)

    def _current_version(self) -> int:
        exists = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if exists is None:
            return 0
        row = self._db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    @staticmethod
    def _refuse_newer(current: int) -> None:
        raise StoreError(
            f"database schema {current} is newer than this version ({len(MIGRATIONS)})"
        )

    def _migrate(self) -> None:
        backfill: SubjectBackfill | None = None
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
                )
                current = self._current_version()  # re-checked under the write lock
                if current > len(MIGRATIONS):
                    self._refuse_newer(current)
                for version in range(current + 1, len(MIGRATIONS) + 1):
                    if version == COMMIT_POS_MIGRATION and _has_column(
                        self._db, "ledger_events", "commit_pos"
                    ):
                        pass  # SQLite has no ADD COLUMN IF NOT EXISTS: a re-run keeps the column
                    elif version in (SOLO_MIGRATION, STEP_UP_MIGRATION):
                        _add_missing_columns(self._db, MIGRATIONS[version - 1])
                    else:
                        for statement in _split_sql(MIGRATIONS[version - 1]):
                            self._db.execute(statement)
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
                    self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
        if backfill is not None:  # reported only once the migration is committed
            self.subject_backfill = backfill
            backfill.report()

    def _rollback(self) -> None:
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")

    @property
    def schema_version(self) -> int:
        """The applied schema version."""
        with self._lock:
            return self._current_version()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            if self._wal_dirty:
                self._wal_next_try = 0.0
                self._truncate_wal()
            self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[UnitOfWork]:
        if getattr(self._local, "active", False):
            raise NestedTransactionError("units of work don't nest")
        with self._lock:
            self._local.active = True
            try:
                try:
                    # a read-only open can only read: a deferred transaction, and any write fails
                    self._db.execute("BEGIN" if self._read_only else "BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    raise _wrap(exc) from exc
                try:
                    uow = _SqliteUnitOfWork(
                        self._db, self._clock, self._schema, self._clean_streams
                    )
                    yield uow
                    self._db.execute("COMMIT")
                    if uow.erased or self._wal_dirty:  # the commit stands; this never raises
                        self._truncate_wal()
                except sqlite3.Error as exc:
                    self._rollback()
                    raise _wrap(exc) from exc
                except BaseException:
                    self._rollback()
                    raise
            finally:
                self._local.active = False

    @property
    def wal_truncate_pending(self) -> bool:
        """True while rows that were erased may still sit in the write-ahead log (a truncate
        found a busy reader). Callers that report an erasure say "erased, WAL truncate pending"."""
        return self._wal_dirty

    def _truncate_wal(self) -> None:
        """After an erasure (or any retention delete) the removed rows must not linger in the
        write-ahead log: `secure_delete` clears pages in the main file, a TRUNCATE checkpoint
        empties the log (R1.30). The erasure is already committed, so this never raises: one
        attempt waits at most one busy timeout; if a reader keeps the log busy the store stays
        `_wal_dirty` (`wal_truncate_pending`), logs an error, and tries again at the next
        transaction no sooner than `wal_retry_seconds` later (so a long reader does not stall every
        transaction), at close and when the store is next opened."""
        self._wal_dirty = True
        if time.monotonic() < self._wal_next_try:
            _LOG.error("erased rows may still be in the write-ahead log (truncate pending)")
            return
        try:
            row = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            busy = bool(row[0]) if row is not None else False
        except sqlite3.Error:
            busy = True
        if not busy:
            self._wal_dirty = False
            self._wal_next_try = 0.0
            return
        self._wal_next_try = time.monotonic() + self.wal_retry_seconds
        _LOG.error(
            "the write-ahead log could not be truncated (busy reader): the data is erased, "
            "the WAL truncate is pending and will be retried"
        )

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._read_retrying(lambda: self._db.execute(sql, params).fetchall())
            except sqlite3.Error as exc:
                raise _wrap(exc) from exc

    def _read_retrying(self, read: Callable[[], _R]) -> _R:
        """Run a read, waiting out a transient lock for up to `read_retry_seconds`. SQLite's busy
        timeout does not cover every 'locked' answer (a table lock, for one), and a caller that
        reads a halt must not mistake a passing lock for an unreadable ledger. A lock that does not
        clear raises as before (`StoreBusyError` via `_wrap`)."""
        deadline = time.monotonic() + self.read_retry_seconds
        delay = 0.005
        while True:
            try:
                return read()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    def read_stream(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterator[LedgerEvent]:
        check_limit(limit)
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (stream, after_seq, -1 if limit is None else limit),
        )
        return iter([_event_from_row(row) for row in rows])

    def read_stream_positioned(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterator[tuple[int, LedgerEvent]]:
        check_limit(limit)
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS}, COALESCE(commit_pos, 0) AS commit_pos FROM ledger_events "
            "WHERE stream = ? AND seq > ? ORDER BY seq LIMIT ?",
            (stream, after_seq, -1 if limit is None else limit),
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
            "WHERE commit_pos > ? ORDER BY commit_pos LIMIT ?",
            (pos, -1 if limit is None else limit),
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
                self._db.execute("BEGIN")  # one read transaction: rows and counts agree
                try:
                    return _read_projection(self._db, org_id, kinds, projection_streams(streams))
                finally:
                    self._rollback()
            except sqlite3.Error as exc:
                raise _wrap(exc) from exc

    def projection_problems(self) -> list[str]:
        with self._lock:
            try:
                self._db.execute("BEGIN")
                try:
                    return _projection_problems(self._db)
                finally:
                    self._rollback()
            except sqlite3.Error as exc:
                raise _wrap(exc) from exc

    def rebuild_projection(self) -> int:
        with self.transaction() as unit:
            assert isinstance(unit, _SqliteUnitOfWork)
            return _rebuild_projection(unit._db)

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = ? AND task_id = ? "
            "ORDER BY seq",
            (run_stream(run_id), task_id),
        )
        return [_event_from_row(row) for row in rows]

    def snapshot(self, stream: str) -> tuple[list[LedgerEvent], StreamHead | None]:
        if getattr(self._local, "active", False):
            raise NestedTransactionError("verify or snapshot outside a unit of work")
        with self._lock:
            try:
                self._db.execute("BEGIN")  # one read transaction: one consistent view
                try:
                    rows = self._db.execute(
                        f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE stream = ? ORDER BY seq",
                        (stream,),
                    ).fetchall()
                    head = _read_head(self._db, stream)
                finally:
                    self._rollback()
            except sqlite3.Error as exc:
                raise _wrap(exc) from exc
        return [_event_from_row(row) for row in rows], head

    def head(self, stream: str) -> StreamHead | None:
        with self._lock:
            try:
                return self._read_retrying(lambda: _read_head(self._db, stream))
            except sqlite3.Error as exc:
                raise _wrap(exc) from exc

    def budget_definitions(self) -> list[LedgerEvent]:
        rows = self._query(  # the literal `type` term lets SQLite use the partial index
            f"SELECT {_EVENT_COLUMNS} FROM ledger_events WHERE type = 'budget.defined' "
            "AND substr(stream, 1, 4) = 'org:' "
            "ORDER BY at, stream, seq"  # the key of `definition_order`; `at` is fixed-width text
        )
        return [_event_from_row(row) for row in rows]

    def streams(self) -> list[str]:
        rows = self._query("SELECT stream FROM stream_heads ORDER BY stream")
        return [row["stream"] for row in rows]

    def heads(self, only: Collection[str] | None = None) -> dict[str, StreamHead]:
        if only is None:
            rows = self._query("SELECT stream, seq, hash, at FROM stream_heads ORDER BY stream")
        else:
            names = sorted(set(only))
            if not names:
                return {}
            marks = ", ".join(["?"] * len(names))
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
        """Heads and every entry (ND238); the same test the triggers apply to a new row."""
        found = set(self.damaged_heads())
        found.update(
            r["stream"]
            for r in self._query(f"SELECT DISTINCT stream FROM ledger_events WHERE {_BAD_AT}")
        )
        return sorted(found)

    def run_owner(self, stream: str) -> str | None:
        rows = self._query(
            "SELECT type, payload FROM ledger_events WHERE stream = ? ORDER BY seq LIMIT 1",
            (stream,),
        )
        return _owner_of(rows[0]["type"], rows[0]["payload"]) if rows else None

    def get_task(self, task_id: str) -> StoredTask | None:
        rows = self._query("SELECT data, version FROM tasks WHERE id = ?", (task_id,))
        return _stored_task(rows[0]) if rows else None

    def tasks_for_run(self, run_id: str) -> list[StoredTask]:
        rows = self._query(
            "SELECT data, version FROM tasks WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [_stored_task(row) for row in rows]

    def get_balance(self, org_id: str, budget_id: str) -> StoredBalance | None:
        rows = self._query(
            "SELECT data, version FROM budget_balances WHERE org_id = ? AND id = ?",
            (org_id, budget_id),
        )
        return _stored_balance(rows[0]) if rows else None

    def get_reservation(self, reservation_id: str) -> StoredReservation | None:
        rows = self._query("SELECT data, version FROM reservations WHERE id = ?", (reservation_id,))
        return _stored_reservation(rows[0]) if rows else None

    def has_content(self, org_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM content WHERE org_id = ? LIMIT 1", (org_id,)))

    def org_content_key_ids(self, org_id: str) -> frozenset[str]:
        rows = self._query("SELECT DISTINCT key_id FROM content WHERE org_id = ?", (org_id,))
        return frozenset(r["key_id"] for r in rows)

    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]:
        rows = self._query(
            "SELECT payload FROM ledger_events WHERE payload LIKE ? ESCAPE '\\' "
            "AND payload LIKE '%\"hash_scheme\"%'",
            (hash_org_pattern(org_id),),
        )
        return keyed_ids_of((json.loads(r["payload"]) for r in rows), org_id)

    def get_content(self, org_id: str, content_id: str) -> ContentRecord | None:
        rows = self._query(
            "SELECT * FROM content WHERE org_id = ? AND id = ?", (org_id, content_id)
        )
        return _content_record(rows[0]) if rows else None

    def get_user(self, org_id: str, user_id: str) -> User | None:
        rows = self._query("SELECT * FROM users WHERE org_id = ? AND id = ?", (org_id, user_id))
        return _user(rows[0]) if rows else None

    def get_user_by_email(self, org_id: str, email: str) -> User | None:
        try:
            wanted = normalize_email(email)
        except ValueError:
            return None
        rows = self._query("SELECT * FROM users WHERE org_id = ? AND email = ?", (org_id, wanted))
        return _user(rows[0]) if rows else None

    def list_users(self, org_id: str) -> list[User]:
        rows = self._query("SELECT * FROM users WHERE org_id = ? ORDER BY id", (org_id,))
        return [_user(row) for row in rows]

    def get_credential(self, org_id: str, user_id: str) -> Credential | None:
        rows = self._query(
            "SELECT * FROM credentials WHERE org_id = ? AND user_id = ?", (org_id, user_id)
        )
        return _credential(rows[0]) if rows else None

    def get_setup_token(self, org_id: str, token_hash: str) -> SetupToken | None:
        rows = self._query(
            "SELECT * FROM setup_tokens WHERE org_id = ? AND token_hash = ?", (org_id, token_hash)
        )
        return _setup_token(rows[0]) if rows else None

    def list_setup_tokens(self, org_id: str) -> list[SetupToken]:
        rows = self._query(
            "SELECT * FROM setup_tokens WHERE org_id = ? ORDER BY created_at, token_hash",
            (org_id,),
        )
        return [_setup_token(row) for row in rows]

    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        rows = self._query(
            "SELECT * FROM auth_sessions WHERE org_id = ? AND id = ?", (org_id, session_id)
        )
        return _auth_session(rows[0]) if rows else None

    def list_auth_sessions(self, org_id: str) -> list[AuthSession]:
        rows = self._query("SELECT * FROM auth_sessions WHERE org_id = ? ORDER BY id", (org_id,))
        return [_auth_session(row) for row in rows]

    def get_auth_token(self, org_id: str, token_hash: str) -> AuthToken | None:
        rows = self._query(
            "SELECT * FROM auth_tokens WHERE org_id = ? AND token_hash = ?", (org_id, token_hash)
        )
        return _auth_token(rows[0]) if rows else None

    def list_auth_tokens(self, org_id: str, session_id: str) -> list[AuthToken]:
        rows = self._query(
            "SELECT * FROM auth_tokens WHERE org_id = ? AND session_id = ? "
            "ORDER BY created_at, kind",
            (org_id, session_id),
        )
        return [_auth_token(row) for row in rows]

    def get_auth_failure(self, key: str) -> AuthFailure | None:
        rows = self._query("SELECT * FROM auth_failures WHERE key = ?", (key,))
        return _auth_failure(rows[0]) if rows else None

    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        rows = self._query(
            "SELECT * FROM mfa_factors WHERE org_id = ? AND user_id = ?", (org_id, user_id)
        )
        return _mfa_factor(rows[0]) if rows else None

    def list_mfa_factors(self, org_id: str) -> list[MfaFactor]:
        rows = self._query("SELECT * FROM mfa_factors WHERE org_id = ? ORDER BY user_id", (org_id,))
        return [_mfa_factor(row) for row in rows]

    def count_mfa_recovery_codes(self, org_id: str, user_id: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM mfa_recovery_codes "
            "WHERE org_id = ? AND user_id = ? AND used_at IS NULL",
            (org_id, user_id),
        )
        return int(rows[0]["n"])

    def get_mfa_challenge(self, org_id: str, token_hash: str) -> MfaChallenge | None:
        rows = self._query(
            "SELECT * FROM mfa_challenges WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        )
        return _mfa_challenge(rows[0]) if rows else None

    def get_org_auth_settings(self, org_id: str) -> OrgAuthSettings:
        rows = self._query("SELECT * FROM org_auth_settings WHERE org_id = ?", (org_id,))
        return _org_auth_settings(rows[0]) if rows else OrgAuthSettings(org_id=org_id)

    def get_user_identifier_key(self, org_id: str, user_id: str) -> bytes | None:
        rows = self._query(
            "SELECT identifier_key FROM users WHERE org_id = ? AND id = ?", (org_id, user_id)
        )
        found = rows[0]["identifier_key"] if rows else None
        return None if found is None else bytes(found)

    def get_org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        rows = self._query(
            "SELECT key FROM identifier_keys WHERE org_id = ? AND epoch = ?", (org_id, epoch)
        )
        found = rows[0]["key"] if rows else None
        return None if found is None else bytes(found)

    def list_security_log(self, org_id: str) -> list[SecurityLogEntry]:
        rows = self._query("SELECT * FROM security_log WHERE org_id = ? ORDER BY at, id", (org_id,))
        return [_security_log(row) for row in rows]

    def get_agent_credential_by_hash(
        self, org_id: str, team: str, token_hash: str
    ) -> AgentCredential | None:
        rows = self._query(
            "SELECT * FROM agent_credentials WHERE org_id = ? AND team = ? AND token_hash = ?",
            (org_id, team, token_hash),
        )
        return _agent_credential(rows[0]) if rows else None

    def get_email_change(self, org_id: str, token_hash: str) -> EmailChange | None:
        rows = self._query(
            "SELECT * FROM email_changes WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        )
        return _email_change(rows[0]) if rows else None

    def list_email_changes(self, org_id: str, user_id: str) -> list[EmailChange]:
        rows = self._query(
            "SELECT * FROM email_changes WHERE org_id = ? AND user_id = ? "
            "ORDER BY created_at, token_hash",
            (org_id, user_id),
        )
        return [_email_change(r) for r in rows]

    def get_reset_token(self, org_id: str, token_hash: str) -> ResetToken | None:
        rows = self._query(
            "SELECT * FROM password_reset_tokens WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        )
        return _reset_token(rows[0]) if rows else None

    def list_reset_tokens(self, org_id: str) -> list[ResetToken]:
        rows = self._query(
            "SELECT * FROM password_reset_tokens WHERE org_id = ? ORDER BY created_at, token_hash",
            (org_id,),
        )
        return [_reset_token(r) for r in rows]

    def get_revoke_link(self, org_id: str, token_hash: str) -> RevokeLink | None:
        rows = self._query(
            "SELECT * FROM api_token_revoke_links WHERE org_id = ? AND token_hash = ?",
            (org_id, token_hash),
        )
        return _revoke_link(rows[0]) if rows else None

    def list_revoke_links(self, org_id: str) -> list[RevokeLink]:
        rows = self._query(
            "SELECT * FROM api_token_revoke_links WHERE org_id = ? ORDER BY created_at, token_hash",
            (org_id,),
        )
        return [_revoke_link(r) for r in rows]

    def get_password_login_block(self, org_id: str, user_id: str) -> PasswordLoginBlock | None:
        rows = self._query(
            "SELECT * FROM password_login_blocks WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        )
        return _login_block(rows[0]) if rows else None

    def get_invite(self, org_id: str, invite_id: str) -> Invite | None:
        rows = self._query("SELECT * FROM invites WHERE org_id = ? AND id = ?", (org_id, invite_id))
        return _invite(rows[0]) if rows else None

    def get_invite_by_token_hash(self, org_id: str, token_hash: str) -> Invite | None:
        rows = self._query(
            "SELECT * FROM invites WHERE org_id = ? AND token_hash = ?", (org_id, token_hash)
        )
        return _invite(rows[0]) if rows else None

    def list_invites(self, org_id: str) -> list[Invite]:
        rows = self._query(
            "SELECT * FROM invites WHERE org_id = ? ORDER BY created_at DESC, id DESC", (org_id,)
        )
        return [_invite(r) for r in rows]

    def get_agent_credential(self, org_id: str, credential_id: str) -> AgentCredential | None:
        rows = self._query(
            "SELECT * FROM agent_credentials WHERE org_id = ? AND id = ?",
            (org_id, credential_id),
        )
        return _agent_credential(rows[0]) if rows else None

    def list_agent_credentials(self, org_id: str) -> list[AgentCredential]:
        rows = self._query(
            "SELECT * FROM agent_credentials WHERE org_id = ? ORDER BY created_at, id", (org_id,)
        )
        return [_agent_credential(row) for row in rows]

    def get_api_token_by_hash(self, org_id: str, token_hash: str) -> ApiToken | None:
        rows = self._query(
            "SELECT * FROM api_tokens WHERE org_id = ? AND token_hash = ?", (org_id, token_hash)
        )
        return _api_token(rows[0]) if rows else None

    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None:
        rows = self._query(
            "SELECT * FROM api_tokens WHERE org_id = ? AND id = ?", (org_id, token_id)
        )
        return _api_token(rows[0]) if rows else None

    def ext_get(
        self, org_id: str, namespace: str, kind: str, record_id: str
    ) -> ExtensionRecord | None:
        rows = self._query(
            "SELECT * FROM extension_records WHERE org_id = ? AND namespace = ? AND kind = ? "
            "AND record_id = ?",
            (org_id, namespace, kind, record_id),
        )
        return _extension(rows[0]) if rows else None

    def ext_find(
        self, org_id: str, namespace: str, kind: str, unique_key: str
    ) -> ExtensionRecord | None:
        rows = self._query(
            "SELECT * FROM extension_records WHERE org_id = ? AND namespace = ? AND kind = ? "
            "AND unique_key = ?",
            (org_id, namespace, kind, unique_key),
        )
        return _extension(rows[0]) if rows else None

    def ext_list(
        self, org_id: str, namespace: str, kind: str, group_key: str
    ) -> list[ExtensionRecord]:
        rows = self._query(
            "SELECT * FROM extension_records WHERE org_id = ? AND namespace = ? AND kind = ? "
            "AND group_key = ? ORDER BY sort_key DESC, record_id ASC",
            (org_id, namespace, kind, group_key),
        )
        return [_extension(row) for row in rows]

    def count_security_log(
        self, org_id: str, type: str, actor: str | None, start: datetime, end: datetime
    ) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM security_log WHERE org_id = ? AND type = ? "
            "AND actor IS ? AND at >= ? AND at < ?",
            (org_id, type, actor, format_time(start), format_time(end)),
        )
        return int(rows[0]["n"])

    def list_api_tokens(self, org_id: str) -> list[ApiToken]:
        rows = self._query(
            "SELECT * FROM api_tokens WHERE org_id = ? ORDER BY created_at, id", (org_id,)
        )
        return [_api_token(row) for row in rows]

    def api_token_retired_rows(self, org_id: str) -> list[tuple[int, str, datetime]]:
        rows = self._query(
            "SELECT epoch, name_hash, retired_at FROM api_token_retired_names "
            "WHERE org_id = ? ORDER BY retired_at, epoch, name_hash",
            (org_id,),
        )
        return [
            (int(r["epoch"]), r["name_hash"], datetime.fromisoformat(r["retired_at"])) for r in rows
        ]

    def content_subject_schema(self) -> dict[str, list[Any]]:
        columns = [
            r["name"] for r in self._db.execute("PRAGMA table_info(content_subjects)").fetchall()
        ]
        indexes: list[list[str]] = []
        for index in self._db.execute("PRAGMA index_list(content_subjects)").fetchall():
            if index["origin"] != "c":
                continue  # the primary key is not one of the two named indexes
            info = self._db.execute(f"PRAGMA index_info({index['name']})").fetchall()
            indexes.append([r["name"] for r in sorted(info, key=lambda r: r["seqno"])])
        return {"columns": columns, "indexes": sorted(indexes)}

    def expired_content(self, now: datetime, *, limit: int = 500) -> list[ContentRecord]:
        check_limit(limit)
        rows = self._query(
            "SELECT * FROM content WHERE expires_at IS NOT NULL AND expires_at <= ? "
            "AND org_id NOT IN (SELECT org_id FROM content_holds) "
            "ORDER BY expires_at, org_id, id LIMIT ?",
            (format_time(now), limit),
        )
        return [_content_record(row) for row in rows]

    def expiring_content(
        self, kind: ContentKind, after: datetime, until: datetime
    ) -> list[ContentRecord]:
        rows = self._query(
            "SELECT * FROM content WHERE kind = ? AND expires_at IS NOT NULL "
            "AND expires_at > ? AND expires_at <= ? "
            "AND org_id NOT IN (SELECT org_id FROM content_holds) "
            "ORDER BY expires_at, org_id, id",
            (kind.value, format_time(after), format_time(until)),
        )
        return [_content_record(row) for row in rows]

    def legal_hold(self, org_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM content_holds WHERE org_id = ?", (org_id,)))

    def key_tombstone(self, org_id: str) -> KeyTombstone | None:
        rows = self._query("SELECT * FROM key_tombstones WHERE org_id = ?", (org_id,))
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
        rows = self._query("SELECT data, version FROM approvals WHERE id = ?", (approval_id,))
        return _stored_approval(rows[0]) if rows else None

    def due_approvals(self, now: datetime, *, after_id: str, limit: int) -> list[StoredApproval]:
        found: list[StoredApproval] = []
        cursor = after_id
        while len(found) < limit:  # pages by id; the index bounds what is read (R13)
            rows = self._query(
                "SELECT data, version FROM approvals INDEXED BY approvals_anchor_day "
                "WHERE " + _DUE_WHERE + " AND id > ? AND " + _ANCHOR_DAY + " <= ? "
                "ORDER BY id LIMIT ?",
                (cursor, due_anchor_day(now), min(200, limit - len(found) + 10)),
            )
            for row in rows:
                stored = _stored_approval(row)
                cursor = stored.approval.id
                if stored.approval.retention_over(now):
                    found.append(stored)
                    if len(found) == limit:
                        break
            if len(rows) < min(200, limit - len(found) + 10):
                break
        return found

    def approvals(
        self, *, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[StoredApproval]:
        sql, params = "SELECT data, version FROM approvals WHERE 1 = 1", []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        return [_stored_approval(row) for row in self._query(sql + " ORDER BY id", tuple(params))]

    def list_balances(self, org_id: str | None) -> list[StoredBalance]:
        rows = self._query(
            "SELECT data, version FROM budget_balances WHERE (? IS NULL OR org_id = ?) "
            "ORDER BY org_id, id",
            (org_id, org_id),
        )
        return [_stored_balance(row) for row in rows]

    def reservations_for_run(
        self, run_id: str, status: ReservationStatus | None = None
    ) -> list[StoredReservation]:
        sql = "SELECT data, version FROM reservations WHERE run_id = ?"
        params: tuple[str, ...] = (run_id,)
        if status is not None:
            sql += " AND status = ?"
            params = (run_id, status.value)
        return [_stored_reservation(row) for row in self._query(sql + " ORDER BY id", params)]


def _add_missing_columns(db: Any, migration: str) -> None:
    """Run an additive migration, skipping an `ADD COLUMN` whose column is already there (SQLite
    has no ADD COLUMN IF NOT EXISTS; a schema replayed from an older version keeps its columns)."""
    for statement in _split_sql(migration):
        words = statement.split()
        if words[:2] == ["ALTER", "TABLE"] and words[3:5] == ["ADD", "COLUMN"]:
            if _has_column(db, words[2], words[5]):
                continue
        db.execute(statement)


def _has_column(db: Any, table: str, column: str) -> bool:
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})").fetchall())


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies (`BEGIN … END;`) whole."""
    statements: list[str] = []
    current: list[str] = []
    for line in script.strip().splitlines():
        current.append(line)
        joined = "\n".join(current).strip()
        if joined.endswith(";") and sqlite3.complete_statement(joined):
            statements.append(joined)
            current = []
    if "\n".join(current).strip():
        raise ValueError("migration ends with an incomplete statement")
    return statements


def _project(db: Any, event: LedgerEvent) -> None:
    """Index `event` if the projection holds it (same transaction as the entry)."""
    key = projection_key(event)
    if key is None:
        return
    db.execute(
        "INSERT INTO ledger_projection (stream, kind, seq, org) VALUES (?, ?, ?, ?)",
        (event.stream, key.kind, event.seq, key.org),
    )
    db.execute(
        "INSERT INTO ledger_projection_counts (stream, kind, n) VALUES (?, ?, 1) "
        "ON CONFLICT (stream, kind) DO UPDATE SET n = n + 1",
        (event.stream, key.kind),
    )


def _read_projection(
    db: Any, org_id: str, kinds: Collection[str] | None, streams: ProjectionStreams = "all"
) -> list[tuple[int, LedgerEvent]]:
    wanted = sorted(ALL_KINDS if kinds is None else frozenset(kinds) & ALL_KINDS)
    if not wanted:
        return []
    marks = ", ".join("?" * len(wanted))
    if streams == "org":
        # the organization stream alone: a primary key prefix (stream, kind) range, no scan
        scope = "p.stream = ?"
        params: tuple[str, ...] = (*wanted, f"org:{org_id}")
    else:
        scope = (
            "(p.stream = ? OR p.stream IN (SELECT stream FROM ledger_projection "
            f"WHERE kind = '{RUN_STARTED}' AND org = ?))"
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
