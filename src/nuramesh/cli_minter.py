"""The token minter's anchor commands (R1.36, spec 13.1 X3): `minter anchor-record` writes one
`minter.anchor_recorded` (or `minter.anchor_conflict`) ledger entry for the hourly export of a
minter host's audit-file anchor, and `minter anchor-verify` checks an audit file against every
anchor recorded for its host. Neither touches a token, a key or the GitHub API."""

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nuramesh.core.ledger import EventType, verify_chain
from nuramesh.env_bool import EnvBoolError, env_bool
from nuramesh.secret_env import database_url
from nuramesh.stores.base import EventDraft, StoreError

ACTOR = "service:minter-anchor"
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
STALE_AFTER = timedelta(hours=3)
OK, FAIL, WARN = 0, 1, 6  # WARN was 2 (usage); 6 is "done with warnings" (OD-7a)


def _recorded(uow: Any, stream: str, host: str) -> list[dict[str, Any]]:
    """Every `minter.anchor_recorded` payload of `host`, oldest first (conflicts never count)."""
    found: list[dict[str, Any]] = []
    after = 0
    while True:
        page = uow.events_after(stream, after, 500)
        if not page:
            return found
        for event in page:
            if (
                event.type == EventType.MINTER_ANCHOR_RECORDED.value
                and event.payload["host"] == host
            ):
                found.append({**event.payload, "_at": event.at})
        after = page[-1].seq


def _conflict(
    uow: Any, stream: str, host: str, epoch: int, seq: int, recorded: str, presented: str, why: str
) -> None:
    uow.append(
        EventDraft(
            stream=stream,
            type=EventType.MINTER_ANCHOR_CONFLICT,
            actor=ACTOR,
            payload={
                "host": host,
                "epoch": epoch,
                "seq": seq,
                "recorded_hash": recorded,
                "presented_hash": presented,
                "reason": why,
            },
        )
    )


def _operator_or_off() -> bool:
    """`NURAMESH_OPERATOR` for store selection; a malformed value is off here and the command
    itself then refuses it by name (`cmd_anchor_record`)."""
    try:
        return env_bool("NURAMESH_OPERATOR")
    except EnvBoolError:
        return False


def cmd_anchor_record(args: argparse.Namespace, open_store: Callable[..., Any]) -> int:
    """Record one anchor; the rules and their order are spec 13.1 X3."""
    host, seq, digest, epoch = args.host, args.seq, args.hash, args.epoch
    if not HOST_RE.match(host) or not HASH_RE.match(digest) or seq < 1 or epoch < 1:
        print("refused: host, seq, hash or epoch is malformed", file=sys.stderr)
        return FAIL
    try:
        store = open_store(Path(args.team).resolve(), args.store)
    except Exception as exc:  # the URL and paths never appear in the message
        print(f"couldn't open the store: {type(exc).__name__}", file=sys.stderr)
        return 2
    stream = f"org:{args.org}"
    try:
        operator = env_bool("NURAMESH_OPERATOR")
    except EnvBoolError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    with store.transaction() as uow:
        rows = _recorded(uow, stream, host)
        newest_epoch = max((r["epoch"] for r in rows), default=0)
        in_epoch = [r for r in rows if r["epoch"] == newest_epoch]
        top = max(in_epoch, key=lambda r: r["seq"]) if in_epoch else None
        why = None
        if newest_epoch and epoch < newest_epoch:  # X3 rule 1 wins whatever else is presented
            assert top is not None
            _conflict(uow, stream, host, epoch, seq, top["hash"], digest, "epoch_regressed")
            print("conflict: epoch_regressed", file=sys.stderr)
            return FAIL
        if newest_epoch == 0 and (epoch != 1 or args.new_epoch):
            why = "the first anchor of a host must be epoch 1 without --new-epoch"
        elif args.new_epoch and epoch != newest_epoch + 1:
            why = "--new-epoch needs the newest recorded epoch + 1"
        elif args.new_epoch and not operator:
            why = "--new-epoch is for the owner (NURAMESH_OPERATOR=1)"
        elif epoch > newest_epoch and newest_epoch and not args.new_epoch:
            why = "a higher epoch needs --new-epoch"
        if why:
            print(f"refused: {why}", file=sys.stderr)
            return FAIL
        if top is not None and epoch == newest_epoch:
            if seq < top["seq"]:
                _conflict(uow, stream, host, epoch, seq, top["hash"], digest, "regressed")
                print("conflict: regressed", file=sys.stderr)
                return FAIL
            if seq == top["seq"]:
                if digest == top["hash"]:
                    return OK
                _conflict(uow, stream, host, epoch, seq, top["hash"], digest, "hash_mismatch")
                print("conflict: hash_mismatch", file=sys.stderr)
                return FAIL
        try:
            uow.append(
                EventDraft(
                    stream=stream,
                    type=EventType.MINTER_ANCHOR_RECORDED,
                    actor=ACTOR,
                    payload={
                        "host": host,
                        "seq": seq,
                        "hash": digest,
                        "key_fp": args.key_fp,
                        "epoch": epoch,
                    },
                )
            )
        except StoreError as exc:  # the database function's own epoch rule (one place, atomic)
            print(f"refused by the store: {exc}", file=sys.stderr)
            return FAIL
    return OK


def _lines(path: Path) -> tuple[list[bytes], str | None]:
    """The audit file's complete lines and the first problem with its chain (None if intact)."""
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        return [], "torn last line"
    lines = raw.split(b"\n")[:-1] if raw else []
    prev = "0" * 64
    for index, line in enumerate(lines, 1):
        try:
            rec = json.loads(line)
        except ValueError:
            return lines, f"seq {index}"
        if rec.get("seq") != index or rec.get("prev") != prev:
            return lines, f"seq {rec.get('seq', index)}"
        prev = hashlib.sha256(line).hexdigest()
    return lines, None


def _stream_problem(uow: Any, stream: str) -> str | None:
    """The first problem with the anchor stream's own hash chain (None if intact): numbering,
    links and every hash recomputed from content, from the first entry."""
    head = uow.head(stream)  # read first: an entry appended while we page is not a truncation
    events: list[Any] = []
    after = 0
    while page := uow.events_after(stream, after, 500):
        events.extend(page)
        after = page[-1].seq
    if head is not None:
        events = [e for e in events if e.seq <= head.seq]
    # expected_head catches entries removed from the end (owner-level database access)
    report = verify_chain(
        events, expected_stream=stream, expected_head=None if head is None else head.hash
    )
    if report.ok:
        return None
    return f"{report.problem.value if report.problem else 'broken'} at entry {report.at_seq}"


def _epoch_problem(rows: list[dict[str, Any]]) -> str | None:
    """Epochs of one host must run 1, 2, ... N with no gap, and within an epoch the sequence
    numbers never go down (the ledger order is the order of recording)."""
    epochs = sorted({r["epoch"] for r in rows})
    if epochs != list(range(1, len(epochs) + 1)):
        return f"epochs {','.join(str(e) for e in epochs)} are not 1..{len(epochs)}"
    last: dict[int, int] = {}
    for r in rows:
        if r["seq"] < last.get(r["epoch"], 0):
            return f"epoch {r['epoch']} seq {r['seq']} follows seq {last[r['epoch']]}"
        last[r["epoch"]] = r["seq"]
    return None


def _verify_host(
    uow: Any, stream: str, host: str, audit: Path, now: datetime, accepted_epoch: int = 1
) -> tuple[int, list[str], list[dict[str, Any]]]:
    """Check the whole record of a host (R1.30 review 2): the anchor stream's own chain, the epoch
    history, every anchor of the newest epoch against the audit file, and report each epoch change
    above `accepted_epoch` (the epoch the owner has already acknowledged) as a warning."""
    broken = _stream_problem(uow, stream)
    if broken:
        return FAIL, [f"FAIL anchor stream chain: {broken}"], []
    rows = _recorded(uow, stream, host)
    if not rows:
        return WARN, [f"no anchors for host {host}"], rows
    bad_epochs = _epoch_problem(rows)
    if bad_epochs:
        return FAIL, [f"FAIL epoch history: {bad_epochs}"], rows
    try:
        lines, problem = _lines(audit)
    except OSError as exc:
        return FAIL, [f"FAIL cannot read audit file: {type(exc).__name__}"], rows
    if problem:
        return FAIL, [f"FAIL chain: {problem}"], rows
    # newest first: the mismatch X5 names is the one at the newest recorded seq
    for row in sorted(rows, key=lambda r: (r["epoch"], r["seq"]), reverse=True):
        if row["epoch"] != max(r["epoch"] for r in rows):
            continue
        if row["seq"] > len(lines):
            top = max(r["seq"] for r in rows if r["epoch"] == row["epoch"])
            return (
                FAIL,
                [f"FAIL tail missing: newest anchor seq {top}, file ends at {len(lines)}"],
                rows,
            )
        if hashlib.sha256(lines[row["seq"] - 1]).hexdigest() != row["hash"]:
            return FAIL, [f"FAIL anchor seq {row['seq']} hash mismatch"], rows
    current = [r for r in rows if r["epoch"] == max(x["epoch"] for x in rows)]
    newest = max(current, key=lambda r: r["seq"])
    # an epoch change means the audit file was replaced: the owner confirms each one by name
    changes = [
        f"WARN EPOCH {e} started at {min(r['_at'] for r in rows if r['epoch'] == e).isoformat()}"
        for e in sorted({r["epoch"] for r in rows})
        if e > max(accepted_epoch, 1)
    ]
    age = now - newest["_at"]
    if age > STALE_AFTER:
        return WARN, [*changes, f"STALE newest anchor {age} old"], rows
    if changes:
        return WARN, changes, rows
    return (
        OK,
        [f"OK {len(current)} anchors, newest seq {newest['seq']} at {newest['_at'].isoformat()}"],
        rows,
    )


def _accepted_epochs(args: argparse.Namespace) -> dict[str, int] | None:
    """`--accepted-epoch` per host: `HOST=N`, or a bare `N` together with `--host` (not `--all`).
    None if a value is malformed or a bare number has no single host to belong to."""
    out: dict[str, int] = {}
    for value in getattr(args, "accepted_epoch", None) or []:
        host, sep, number = value.rpartition("=")
        if not sep:
            if not args.host or args.all:
                return None
            host = args.host
        if not HOST_RE.match(host) or not number.isdecimal() or int(number) < 1:
            return None
        out[host] = int(number)
    return out


def cmd_anchor_verify(args: argparse.Namespace, open_store: Callable[..., Any]) -> int:
    """Check an audit file (or, with --all, every host's) against the recorded anchors."""
    try:
        store = open_store(Path(args.team).resolve(), args.store)
    except Exception as exc:
        print(f"couldn't open the store: {type(exc).__name__}", file=sys.stderr)
        return 2
    accepted = _accepted_epochs(args)
    if accepted is None:
        print(
            "--accepted-epoch is HOST=N (or a bare N together with --host, not --all)",
            file=sys.stderr,
        )
        return 2
    # the staleness test compares ledger times with the store's clock (ND243), not the wall clock
    now = datetime.fromisoformat(args.now) if args.now else store.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    stream = f"org:{args.org}"
    with store.transaction() as uow:
        if not args.all:
            if not args.host or not args.audit:
                print("anchor-verify needs --host and --audit (or --all)", file=sys.stderr)
                return 2
            code, out, _ = _verify_host(
                uow, stream, args.host, Path(args.audit), now, accepted.get(args.host, 1)
            )
            print("\n".join(out))
            return code
        by_host: dict[str, set[str]] = {}
        hosts: set[str] = set()
        after = 0
        while page := uow.events_after(stream, after, 500):
            for event in page:
                if event.type == EventType.MINTER_ANCHOR_RECORDED.value:
                    hosts.add(event.payload["host"])
                    by_host.setdefault(event.payload["key_fp"], set()).add(event.payload["host"])
            after = page[-1].seq
        code = OK
        for shared in [by_host[k] for k in sorted(by_host)]:
            if len(shared) > 1:
                print(f"WARN key_fp shared by hosts {','.join(sorted(shared))}")
                code = WARN
        if args.audit_dir:
            for host in sorted(hosts):
                path = Path(args.audit_dir) / f"{host}.jsonl"
                if not path.is_file():
                    print(f"FAIL audit file missing for host {host}")
                    return FAIL
                result, out, _ = _verify_host(uow, stream, host, path, now, accepted.get(host, 1))
                print("\n".join(out))
                code = max(code, result) if result != FAIL else FAIL
                if result == FAIL:
                    return FAIL
        if code == OK:
            print(f"OK key_fp unique across {len(hosts)} hosts")
        return code


ROLE_FUNCTION = "ledger_minter_anchor"

# One row about the connected role (ND223 point 1): it must hold no privilege on any table, view or
# sequence of any user schema, no CREATE anywhere, EXECUTE on the one function and on no other
# callable function, no elevated attribute and no membership in another role; and the function
# must be SECURITY DEFINER with a pinned search_path, owned by a different, non-elevated role.
ROLE_SQL = """
WITH fn AS (SELECT * FROM pg_proc
            WHERE proname = %(f)s AND pronamespace = %(s)s::regnamespace)
SELECT current_user::text AS role,
  (SELECT count(*) FROM fn p) AS fn_count,
  COALESCE((SELECT has_function_privilege(current_user, p.oid, 'EXECUTE')
            FROM fn p LIMIT 1), false) AS fn_exec,
  COALESCE((SELECT p.prosecdef FROM fn p LIMIT 1), false)
    AS fn_secdef,
  COALESCE((SELECT p.proconfig IS NOT NULL
              AND EXISTS (SELECT 1 FROM unnest(p.proconfig) c
                          WHERE c LIKE 'search_path=pg_catalog, %%, pg_temp')
            FROM fn p LIMIT 1), false) AS fn_path,
  COALESCE((SELECT p.prosrc = %(src)s FROM fn p LIMIT 1), false) AS fn_body,
  COALESCE((SELECT pg_has_role(current_user, p.proowner, 'MEMBER')
            FROM fn p LIMIT 1), true) AS fn_owner_is_me,
  COALESCE((SELECT r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb
            OR r.rolreplication OR r.rolcanlogin
            FROM fn p JOIN pg_roles r ON r.oid = p.proowner LIMIT 1), true) AS fn_owner_elevated,
  COALESCE((SELECT array_agg(n.nspname || '.' || c.relname ORDER BY n.nspname, c.relname)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%%'
              AND (has_table_privilege(current_user, c.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                   OR has_any_column_privilege(current_user, c.oid,
                     'SELECT,INSERT,UPDATE,REFERENCES'))), '{}') AS tables,
  COALESCE((SELECT array_agg(n.nspname || '.' || c.relname ORDER BY n.nspname, c.relname)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'S' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND CASE WHEN c.relkind = 'S'
                   THEN has_sequence_privilege(current_user, c.oid, 'USAGE,SELECT,UPDATE') END),
           '{}') AS sequences,
  COALESCE((SELECT array_agg(n.nspname ORDER BY n.nspname) FROM pg_namespace n
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%'
              AND has_schema_privilege(current_user, n.oid, 'CREATE')), '{}') AS schemas,
  COALESCE((SELECT array_agg(n.nspname || '.' || p.proname ORDER BY n.nspname, p.proname)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%%'
              AND NOT (p.proname = %(f)s AND p.pronamespace = %(s)s::regnamespace)
              -- the pure time check of the CHECK constraints, in the store's schema only
              AND NOT (p.proname = 'nuramesh_valid_at' AND p.pronamespace = %(s)s::regnamespace)
              AND p.prokind IN ('f', 'p') AND p.prorettype <> 'pg_catalog.trigger'::regtype
              AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
              AND has_function_privilege(current_user, p.oid, 'EXECUTE')), '{}') AS functions,
  COALESCE((SELECT array_agg(n.nspname || '.' || c.relname || ' (' || x.extra || ')'
                              ORDER BY n.nspname, c.relname)
            FROM fn p
            CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL (
              SELECT string_agg(priv, ', ' ORDER BY priv) AS extra
              FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES',
                                'TRIGGER']) priv
              WHERE has_table_privilege(p.proowner, c.oid, priv)
                AND NOT (priv = ANY (CASE
                  WHEN n.nspname = %(s)s::text AND c.relname = 'ledger_events'
                    THEN ARRAY['SELECT', 'INSERT']
                  WHEN n.nspname = %(s)s::text AND c.relname = 'stream_heads'
                    THEN ARRAY['SELECT', 'INSERT', 'UPDATE']
                  ELSE ARRAY[]::text[] END))) x
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%%' AND x.extra IS NOT NULL), '{}') AS owner_extra,
  has_database_privilege(current_user, current_database(), 'CREATE') AS db_create,
  COALESCE((SELECT r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolbypassrls
            OR r.rolreplication FROM pg_roles r WHERE r.rolname = current_user), true) AS elevated,
  (SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
   WHERE r.rolname = current_user) AS memberships
"""


def anchor_schema(given: str | None = None) -> str:
    """The schema of the anchor function: the `--schema` option, else `NURAMESH_ANCHOR_SCHEMA`
    (set in anchor.env; record, verify and the role check all read it), else `public`."""
    return given or os.environ.get("NURAMESH_ANCHOR_SCHEMA") or "public"


def role_problems(row: dict[str, Any]) -> list[str]:
    """What is wrong with the minter's database role, from one `ROLE_SQL` row (empty = fine)."""
    if not row.get("fn_count"):
        return [f"the function {ROLE_FUNCTION} is not installed (run minter anchor-install)"]
    if row["fn_count"] != 1:  # an overload could be the one the verdict below is not about
        return [f"more than one function named {ROLE_FUNCTION} exists in the schema"]
    bad: list[str] = []

    def some(label: str, key: str) -> None:
        found = list(row.get(key) or [])
        if found:
            bad.append(f"{label}: " + ", ".join(found[:8]) + (" ..." if len(found) > 8 else ""))

    some("has a table privilege on", "tables")
    some("has a sequence privilege on", "sequences")
    some("can CREATE in schema", "schemas")
    some("can EXECUTE another function", "functions")
    some(f"the owner of {ROLE_FUNCTION} holds more than its two grants on", "owner_extra")
    if row.get("db_create"):
        bad.append("can CREATE in the database")
    if not row.get("fn_exec"):
        bad.append(f"lacks EXECUTE on {ROLE_FUNCTION}")
    if not row.get("fn_secdef"):
        bad.append(f"{ROLE_FUNCTION} is not SECURITY DEFINER")
    if not row.get("fn_path"):
        bad.append(f"{ROLE_FUNCTION} has no pinned search_path (pg_catalog first, pg_temp last)")
    if row.get("fn_owner_is_me"):
        bad.append(f"is (or is a member of) the owner of {ROLE_FUNCTION}")
    if row.get("fn_owner_elevated"):
        bad.append(
            f"the owner of {ROLE_FUNCTION} is a superuser, can log in, create roles or databases, "
            "replicate or bypass security (it must be a NOLOGIN role)"
        )
    if not row.get("fn_body"):
        bad.append(f"{ROLE_FUNCTION} is not the function this version installs (body differs)")
    if row.get("elevated"):
        bad.append("is a superuser, can create roles or databases, or bypasses row security")
    if row.get("memberships"):
        bad.append("is a member of another role")
    return bad


def cmd_anchor_role_check(args: argparse.Namespace, open_store: Callable[..., Any]) -> int:
    """Fail closed unless the database role in `NURAMESH_DATABASE_URL` holds no table privilege and
    only EXECUTE on the one anchor function (ND223). Exit 0 prints `OK`; else `FAIL <why>`."""
    url = database_url()
    schema = anchor_schema(getattr(args, "schema", None))
    if not url:
        print("FAIL no PostgreSQL store: NURAMESH_DATABASE_URL is not set (SQLite is refused)")
        return FAIL
    try:
        import psycopg
        from psycopg.rows import dict_row

        from nuramesh.minter_anchor_db import expected_body

        with psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=15) as db:
            row = db.execute(
                ROLE_SQL, {"f": ROLE_FUNCTION, "s": schema, "src": expected_body(schema)}
            ).fetchone()
    except Exception as exc:  # the URL (a secret) never appears in a message
        print(f"FAIL cannot check the role ({type(exc).__name__})")
        return FAIL
    problems = role_problems(dict(row or {}))
    if problems:
        print("FAIL the database role is wider than allowed: " + "; ".join(problems))
        return FAIL
    print(
        f"OK role {row['role'] if row else '?'} has no table privileges, only EXECUTE on "
        f"{ROLE_FUNCTION}"
    )
    return OK


def cmd_anchor_install(args: argparse.Namespace, open_store: Callable[..., Any]) -> int:
    """Create the anchor function and its grants (run by the database administrator, who must be
    allowed to SET ROLE to the owner role). Idempotent; prints no URL."""
    url = database_url()
    if not url:
        print("FAIL NURAMESH_DATABASE_URL is not set (an administrator's PostgreSQL URL)")
        return FAIL
    try:
        import psycopg

        from nuramesh.minter_anchor_db import install_anchor_function

        with psycopg.connect(url, autocommit=True, connect_timeout=15) as db:
            install_anchor_function(
                db,
                owner_role=args.owner_role,
                minter_role=args.minter_role,
                schema=anchor_schema(args.schema),
            )
    except Exception as exc:
        print(f"FAIL cannot install ({type(exc).__name__})")
        return FAIL
    print(f"OK {ROLE_FUNCTION} installed; {args.minter_role} may only execute it")
    return OK


def anchor_store(factory: Callable[..., Any], *, owner: bool = False) -> Callable[..., Any]:
    """With a PostgreSQL URL in the environment the anchor commands go only through the anchor
    function (the minter role has no table privilege); otherwise the ordinary store (dev, tests).

    `owner` is the owner's epoch bump (`anchor-record --new-epoch` with NURAMESH_OPERATOR=1): the
    function refuses any epoch above the newest by design, so the owner uses their own store
    access (the ordinary store, with the owner's URL). The function is not weakened: the minter
    role has no table privilege and cannot take this path."""

    def open_anchor(team: Path, store: str | None = None) -> Any:
        url = database_url()
        if url and store is None and not owner:
            from nuramesh.minter_anchor_db import AnchorFunctionStore

            return AnchorFunctionStore(url, anchor_schema())
        return factory(team, store)

    return open_anchor


def add_parsers(
    sub: Any,
    store_factory: Callable[..., Any],
    read_store_factory: Callable[..., Any] | None = None,
) -> None:
    """Register `minter anchor-record`, `anchor-verify` and `anchor-role-check`. The read-only
    command opens the store through `read_store_factory` (it must never create one)."""

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--team", default=".", help="the team folder (default: this folder)")
        p.add_argument("--store")
        p.add_argument("--org", default="minter", help="the organization stream (default: minter)")

    minter = sub.add_parser("minter", help="the agent token minter's audit anchors (R1.36)")
    msub = minter.add_subparsers(dest="minter_command", required=True)
    rec = msub.add_parser("anchor-record", help="record one audit-file anchor in the ledger")
    common(rec)
    rec.add_argument("--host", required=True)
    rec.add_argument("--seq", type=int, required=True)
    rec.add_argument("--hash", required=True)
    rec.add_argument("--key-fp", required=True)
    rec.add_argument("--epoch", type=int, required=True)
    rec.add_argument("--new-epoch", action="store_true")
    rec.set_defaults(
        func=lambda a: _guard(
            cmd_anchor_record,
            a,
            anchor_store(
                store_factory,
                owner=bool(a.new_epoch) and _operator_or_off(),
            ),
        )
    )
    ver = msub.add_parser("anchor-verify", help="check an audit file against its recorded anchors")
    common(ver)
    ver.add_argument("--host")
    ver.add_argument("--audit")
    ver.add_argument("--now")
    ver.add_argument("--all", action="store_true")
    ver.add_argument("--audit-dir")
    ver.add_argument(
        "--accepted-epoch",
        action="append",
        metavar="HOST=N",
        help="the newest epoch the owner has acknowledged for a host (repeat per host; a bare N "
        "works with --host); a later epoch is reported as a warning",
    )
    ver.set_defaults(
        func=lambda a: _guard(
            cmd_anchor_verify, a, anchor_store(read_store_factory or store_factory)
        )
    )
    chk = msub.add_parser(
        "anchor-role-check",
        help="fail unless the database role has no table privilege, only EXECUTE on the anchor "
        "function",
    )
    chk.add_argument(
        "--schema",
        help="the schema the function lives in (default: NURAMESH_ANCHOR_SCHEMA, else public)",
    )
    chk.set_defaults(func=lambda a: _guard(cmd_anchor_role_check, a, store_factory))
    inst = msub.add_parser(
        "anchor-install",
        help="create the anchor function and grant the minter role EXECUTE on it (administrator)",
    )
    inst.add_argument("--owner-role", required=True, help="the role that owns the function")
    inst.add_argument("--minter-role", required=True, help="the role the minter connects as")
    inst.add_argument("--schema", help="the schema (default: NURAMESH_ANCHOR_SCHEMA, else public)")
    inst.set_defaults(func=lambda a: _guard(cmd_anchor_install, a, store_factory))


def _guard(fn: Callable[..., int], args: argparse.Namespace, factory: Callable[..., Any]) -> int:
    try:
        return fn(args, factory)
    except StoreError as exc:
        print(f"store error: {type(exc).__name__}", file=sys.stderr)
        return 2
