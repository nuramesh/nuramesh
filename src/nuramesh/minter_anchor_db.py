"""The token minter's only door to the ledger (R1.36, ND223 point 1).

The minter's database role holds NO table privilege. It can execute one `SECURITY DEFINER`
function, `ledger_minter_anchor(after, limit, entry)`, owned by a separate role that holds only
INSERT and SELECT on `ledger_events` and SELECT, INSERT and UPDATE on `stream_heads`. The function
has a fixed `search_path` (`pg_catalog`, the schema, `pg_temp`), names every object with its
schema, and does exactly two things:

* with `entry` NULL it reads: the head of the anchor stream `org:minter` and its anchor entries
  after `after` (at most `limit`, capped at 500);
* with an `entry` it appends exactly one anchor entry (`minter.anchor_recorded` or
  `minter.anchor_conflict`, actor `service:minter-anchor`, stream `org:minter`, an exact key set
  for the entry and its payload) and moves that stream's head in the same transaction, under a
  row lock on the head, only if the entry follows the head (sequence and previous hash).

The caller chooses neither the time nor the hash (ND233): the function sets `at` from the
database clock (`clock_timestamp()`, UTC, the ledger's text form with microseconds and `Z`, never
earlier than the head) and computes the entry hash itself, in SQL, over the canonical JSON the
ledger hashes (`core/canonical.py`, `LedgerEvent.hashed_content`). That is byte-exact because the
function only accepts a payload whose members it can write canonically: keys from a fixed list,
integers in the JavaScript-safe range, strings of printable ASCII without a quote or backslash (so
JSON escaping changes nothing), the 64-hex digests exactly. It stores the canonical payload text it
hashed. A caller that sends `at` or `hash` is refused. It cannot write any other type or stream.

The epoch rule is enforced here, under the row lock on the head (R1.30 review 2, M1): a
`minter.anchor_recorded` entry must carry the newest epoch already recorded for its host (1 for a
host never seen). The role therefore cannot start a new epoch or go back to an old one; the owner
starts a new epoch with their own store access (`minter anchor-record --new-epoch`).
"""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from nuramesh.core.canonical import canonical_json, thaw
from nuramesh.core.ledger import GENESIS_HASH, EventType, LedgerEvent
from nuramesh.stores.base import (
    Clock,
    ConcurrencyError,
    EventDraft,
    StoreError,
    StreamHead,
    utc_now,
)

FUNCTION = "ledger_minter_anchor"
SIGNATURE = f"{FUNCTION}(bigint, int, jsonb)"
STREAM = "org:minter"
ACTOR = "service:minter-anchor"
PAGE = 500
_SIMPLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_BODY = r"""
CREATE OR REPLACE FUNCTION {s}.ledger_minter_anchor(p_after bigint, p_limit int, p_entry jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, {s}, pg_temp
AS $fn$
DECLARE
    c_stream CONSTANT text := 'org:minter';
    c_zero CONSTANT text := repeat('0', 64);
    c_safe CONSTANT text := '[^ -~]';
    v_head {s}.stream_heads%ROWTYPE;
    v_have boolean;
    v_type text;
    v_pj jsonb;
    v_keys text[];
    v_k text;
    v_v jsonb;
    v_txt text;
    v_canon text;
    v_seq bigint;
    v_newest bigint;
    v_prev text;
    v_at text;
    v_hash text;
    v_entry jsonb;
    v_limit int := least(greatest(coalesce(p_limit, 0), 0), 500);
BEGIN
    IF p_entry IS NOT NULL THEN
        IF jsonb_typeof(p_entry) <> 'object' THEN
            RAISE EXCEPTION 'anchor entry must be an object';
        END IF;
        SELECT array_agg(k ORDER BY k) INTO v_keys FROM jsonb_object_keys(p_entry) AS k;
        IF v_keys IS DISTINCT FROM
            ARRAY['actor', 'payload', 'prev_hash', 'seq', 'stream', 'type'] THEN
            RAISE EXCEPTION 'anchor entry has the wrong fields';
        END IF;
        IF jsonb_typeof(p_entry -> 'seq') <> 'number'
            OR jsonb_typeof(p_entry -> 'stream') <> 'string'
            OR jsonb_typeof(p_entry -> 'type') <> 'string'
            OR jsonb_typeof(p_entry -> 'actor') <> 'string'
            OR jsonb_typeof(p_entry -> 'payload') <> 'string'
            OR jsonb_typeof(p_entry -> 'prev_hash') <> 'string' THEN
            RAISE EXCEPTION 'anchor entry has a field of the wrong type';
        END IF;
        IF p_entry ->> 'stream' <> c_stream THEN
            RAISE EXCEPTION 'only the anchor stream may be written';
        END IF;
        v_type := p_entry ->> 'type';
        IF v_type NOT IN ('minter.anchor_recorded', 'minter.anchor_conflict') THEN
            RAISE EXCEPTION 'only anchor entries may be written';
        END IF;
        IF p_entry ->> 'actor' <> 'service:minter-anchor' THEN
            RAISE EXCEPTION 'wrong actor';
        END IF;
        IF p_entry ->> 'prev_hash' !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'malformed hash';
        END IF;
        IF length(p_entry ->> 'payload') > 4096 THEN
            RAISE EXCEPTION 'malformed payload';
        END IF;
        BEGIN
            v_pj := (p_entry ->> 'payload')::jsonb;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'malformed payload';
        END;
        IF jsonb_typeof(v_pj) <> 'object' THEN
            RAISE EXCEPTION 'malformed payload';
        END IF;
        SELECT array_agg(k ORDER BY k) INTO v_keys FROM jsonb_object_keys(v_pj) AS k;
        IF v_type = 'minter.anchor_recorded' AND v_keys IS DISTINCT FROM
            ARRAY['epoch', 'hash', 'host', 'key_fp', 'seq'] THEN
            RAISE EXCEPTION 'wrong payload fields';
        END IF;
        IF v_type = 'minter.anchor_conflict' AND v_keys IS DISTINCT FROM
            ARRAY['epoch', 'host', 'presented_hash', 'reason', 'recorded_hash', 'seq'] THEN
            RAISE EXCEPTION 'wrong payload fields';
        END IF;
        -- the canonical payload text: keys in byte order, integers as digits, safe strings
        v_canon := '{';
        FOR v_k, v_v IN SELECT e.key, e.value FROM jsonb_each(v_pj) AS e ORDER BY e.key COLLATE "C"
        LOOP
            IF v_k IN ('epoch', 'seq') THEN
                IF jsonb_typeof(v_v) <> 'number' OR v_v::text !~ '^(0|[1-9][0-9]{0,15})$'
                    OR v_v::text::bigint > 9007199254740991 THEN
                    RAISE EXCEPTION 'wrong payload value';
                END IF;
                v_txt := v_v::text;
            ELSE
                IF jsonb_typeof(v_v) <> 'string' THEN
                    RAISE EXCEPTION 'wrong payload value';
                END IF;
                v_txt := v_v #>> '{}';
                IF length(v_txt) > 256 OR v_txt ~ c_safe OR position(chr(34) in v_txt) > 0
                    OR position(chr(92) in v_txt) > 0 THEN
                    RAISE EXCEPTION 'wrong payload value';
                END IF;
                IF v_k IN ('hash', 'presented_hash', 'recorded_hash')
                    AND v_txt !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'wrong payload value';
                END IF;
                v_txt := chr(34) || v_txt || chr(34);
            END IF;
            v_canon := v_canon || (CASE WHEN length(v_canon) > 1 THEN ',' ELSE '' END)
                || chr(34) || v_k || chr(34) || ':' || v_txt;
        END LOOP;
        v_canon := v_canon || '}';
        v_seq := (p_entry ->> 'seq')::bigint;
        SELECT * INTO v_head FROM {s}.stream_heads WHERE stream = c_stream FOR UPDATE;
        v_have := FOUND;
        v_at := to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
        IF v_have THEN
            v_prev := v_head.hash;
            IF v_seq <> v_head.seq + 1 THEN
                RAISE EXCEPTION 'the head moved' USING ERRCODE = '40001';
            END IF;
            IF v_head.at > v_at THEN
                v_at := v_head.at;  -- the clock stepped back: time never goes backwards
            END IF;
        ELSE
            v_prev := c_zero;
            IF v_seq <> 1 THEN
                RAISE EXCEPTION 'the head moved' USING ERRCODE = '40001';
            END IF;
        END IF;
        IF p_entry ->> 'prev_hash' <> v_prev THEN
            RAISE EXCEPTION 'the head moved' USING ERRCODE = '40001';
        END IF;
        -- the epoch rule (R1.30 review 2, M1): this role records anchors only in the newest epoch
        -- of the host (epoch 1 for a host never seen). A new epoch is the owner's act, written
        -- with the owner's own store access, never through this function. The head row is locked
        -- above, so two callers cannot both pass this check against the same state.
        IF v_type = 'minter.anchor_recorded' THEN
            SELECT coalesce(max((e.payload::jsonb ->> 'epoch')::bigint), 0) INTO v_newest
            FROM {s}.ledger_events e
            WHERE e.stream = c_stream AND e.type = 'minter.anchor_recorded'
                AND e.payload::jsonb ->> 'host' = v_pj ->> 'host';
            IF (v_pj ->> 'epoch')::bigint <> greatest(v_newest, 1) THEN
                RAISE EXCEPTION 'wrong epoch: only the newest epoch of a host may be recorded';
            END IF;
        END IF;
        -- the ledger's canonical content (keys in order), hashed as UTF-8 with SHA-256
        v_hash := encode(sha256(convert_to(
            '{"actor":"service:minter-anchor","at":"' || v_at
            || '","cost":null,"cost_basis":null,"payload":' || v_canon
            || ',"prev_hash":"' || v_prev || '","run_id":null,"seq":' || v_seq::text
            || ',"stream":"org:minter","task_id":null,"type":"' || v_type || '"}', 'UTF8')),
            'hex');
        INSERT INTO {s}.ledger_events (stream, seq, type, actor, at, run_id, task_id, payload,
            cost_amount, cost_currency, cost_basis, prev_hash, hash)
        VALUES (c_stream, v_seq, v_type, 'service:minter-anchor', v_at, NULL, NULL,
            v_canon, NULL, NULL, NULL, v_prev, v_hash);
        IF v_have THEN
            UPDATE {s}.stream_heads SET seq = v_seq, hash = v_hash, at = v_at
            WHERE stream = c_stream;
        ELSE
            INSERT INTO {s}.stream_heads (stream, seq, hash, at)
            VALUES (c_stream, v_seq, v_hash, v_at);
        END IF;
        PERFORM pg_notify('nuramesh_ledger', c_stream || ':' || v_seq);
        v_entry := jsonb_build_object('stream', c_stream, 'seq', v_seq, 'type', v_type,
            'actor', 'service:minter-anchor', 'at', v_at, 'payload', v_canon,
            'prev_hash', v_prev, 'hash', v_hash);
    END IF;
    RETURN jsonb_build_object(
        'entry', v_entry,
        'head', (SELECT to_jsonb(h) FROM (SELECT seq, hash, at FROM {s}.stream_heads
                                          WHERE stream = c_stream) AS h),
        'events', coalesce((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.seq)
                            FROM (SELECT * FROM {s}.ledger_events
                                  WHERE stream = c_stream AND seq > coalesce(p_after, 0)
                                    AND type IN ('minter.anchor_recorded',
                                                 'minter.anchor_conflict')
                                  ORDER BY seq LIMIT v_limit) AS e), '[]'::jsonb));
END;
$fn$
"""


def expected_body(schema: str = "public") -> str:
    """The text of the function body this version installs (`pg_proc.prosrc`), for the role
    check to compare against: a replaced body is then detected."""
    text = _BODY.replace("{s}", _ident(schema))
    return text.split("$fn$", 2)[1]


def _ident(name: str) -> str:
    """A role or schema name, quoted only when it is not a plain lower-case identifier."""
    if not name or len(name) > 63 or "\x00" in name:
        raise ValueError("invalid database identifier")
    return name if _SIMPLE.match(name) else '"' + name.replace('"', '""') + '"'


def install_anchor_function(
    db: Any, *, owner_role: str, minter_role: str, schema: str = "public"
) -> None:
    """Create (or replace) the function and its grants. Run by the database administrator, who must
    be able to `SET ROLE` to `owner_role`; the owner and the minter role must already exist. After
    it runs, the minter role has no table, sequence or schema-CREATE privilege and EXECUTE on this
    one function only; the owner role holds only the table privileges the function needs."""
    o, m, s = _ident(owner_role), _ident(minter_role), _ident(schema)
    if o == m:
        raise ValueError("the function owner and the minter role must be different roles")
    for sql in (
        f"GRANT USAGE ON SCHEMA {s} TO {o}, {m}",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM {m}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM {m}",
        f"REVOKE CREATE ON SCHEMA {s} FROM {m}",
        f"GRANT SELECT, INSERT ON {s}.ledger_events TO {o}",
        f"GRANT SELECT, INSERT, UPDATE ON {s}.stream_heads TO {o}",
        f"GRANT CREATE ON SCHEMA {s} TO {o}",
    ):
        db.execute(sql)
    try:
        db.execute(f"SET ROLE {o}")
        db.execute(_BODY.replace("{s}", s))
    finally:
        db.execute("RESET ROLE")
        db.execute(f"REVOKE CREATE ON SCHEMA {s} FROM {o}")
    db.execute(f"REVOKE ALL ON FUNCTION {s}.{SIGNATURE} FROM PUBLIC")
    db.execute(f"GRANT EXECUTE ON FUNCTION {s}.{SIGNATURE} TO {m}")


class _Uow:
    """The slice of a unit of work the anchor commands use, over the function."""

    def __init__(self, db: Any, schema: str = "public") -> None:
        self._db = db
        self._fn = f"{_ident(schema)}.{FUNCTION}"

    def _call(self, after: int, limit: int, entry: dict[str, Any] | None) -> dict[str, Any]:
        import psycopg

        try:
            row = self._db.execute(
                f"SELECT {self._fn}(%s, %s, %s::jsonb)",
                (after, limit, None if entry is None else json.dumps(entry)),
            ).fetchone()
        except (psycopg.errors.SerializationFailure, psycopg.errors.UniqueViolation):
            raise ConcurrencyError("the anchor stream head moved") from None
        except psycopg.Error as exc:  # a message can quote the connection: the type only
            raise StoreError(f"the anchor function failed ({type(exc).__name__})") from None
        assert row is not None
        result: dict[str, Any] = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        return result

    @staticmethod
    def _check(stream: str) -> None:
        if stream != STREAM:
            raise StoreError("the minter role can only reach the anchor stream")

    def events_after(self, stream: str, after_seq: int, limit: int) -> list[LedgerEvent]:
        from nuramesh.stores.postgres import _event_from_row

        self._check(stream)
        out = self._call(after_seq, min(limit, PAGE), None)
        return [_event_from_row(row) for row in out["events"]]

    def head(self, stream: str) -> StreamHead | None:
        """The anchor stream's head as the function reports it (so a truncated tail is visible)."""
        self._check(stream)
        row = self._call(0, 0, None)["head"]
        if row is None:
            return None
        return StreamHead(
            stream=stream,
            seq=row["seq"],
            hash=row["hash"],
            at=datetime.fromisoformat(row["at"]),
        )

    def append(self, draft: EventDraft) -> LedgerEvent:
        """Append one anchor entry. The database sets the time and the hash; the event returned
        is rebuilt from what it stored, and its hash is recomputed here as a cross-check."""
        self._check(draft.stream)
        head = self._call(0, 0, None)["head"]
        seq, prev = (1, GENESIS_HASH) if head is None else (head["seq"] + 1, head["hash"])
        out = self._call(
            0,
            0,
            {
                "stream": draft.stream,
                "seq": seq,
                "type": draft.type.value if isinstance(draft.type, EventType) else draft.type,
                "actor": draft.actor,
                "payload": canonical_json(thaw(draft.payload)).decode("utf-8"),
                "prev_hash": prev,
            },
        )["entry"]
        event = LedgerEvent.model_validate(
            {
                **out,
                "at": datetime.fromisoformat(out["at"]),
                "payload": json.loads(out["payload"]),
            }
        )
        if event.computed_hash() != event.hash:  # the SQL and Python canonical forms must agree
            raise StoreError("the anchor function returned an entry whose hash does not match")
        return event


class AnchorFunctionStore:
    """Store-shaped wrapper for the anchor commands: `transaction()` yields the function's unit.
    Nothing here opens a table; the database URL is only used to connect and never printed."""

    def __init__(self, url: str, schema: str = "public", *, clock: Clock = utc_now) -> None:
        self._url = url
        self._schema = schema
        self._clock = clock

    def now(self) -> datetime:
        """The store clock (the same default as every store; ND243): the application's injected
        clock, NOT the database's. The anchor times in the ledger come from `clock_timestamp()` in
        the function, so the 3-hour staleness test compares two real UTC clocks that can differ by
        the skew between this host and the database (seconds, against a 3-hour limit)."""
        return self._clock()

    @contextmanager
    def transaction(self) -> Iterator[_Uow]:
        import psycopg

        try:
            db = psycopg.connect(self._url, autocommit=True, connect_timeout=15)
        except psycopg.Error as exc:
            raise StoreError(f"could not connect ({type(exc).__name__})") from None
        try:
            yield _Uow(db, self._schema)
        finally:
            db.close()
