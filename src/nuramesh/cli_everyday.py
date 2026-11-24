"""The everyday commands R1.43 A2 adds: `status`, `reject` and the bare `nuramesh`.

All three open the store read-only where they only read: a SQLite file that does not exist or
needs a migration is never created or migrated (the bare command says what to run instead). They
print ids, states and ages only: never plan text, titles, reasons, `asked_because`, ledger payloads
or secrets. `reject` restricts, so the CLI accepts a typed `--human` claim for it, like `stop`;
there is no `approve` here (OD-11): approving widens, and stays API and app only.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nuramesh import cli_prompts
from nuramesh.cli_rules import add_team
from nuramesh.core.people import is_human_id
from nuramesh.paths import state_path
from nuramesh.secret_env import database_url
from nuramesh.stores import SqliteStore
from nuramesh.stores.base import StoreError

if TYPE_CHECKING:
    from nuramesh.stores import Store

OK, NOT_DONE, USAGE, REFUSED, STOPPED, UNAVAILABLE = 0, 1, 2, 3, 4, 5
UNFINISHED = ("running", "stopping", "needs_human")
MAX_LINES = 20
COMMANDS = (
    ("status", "this list, or one run (nuramesh status RUN)"),
    ("stop", "ask a run to stop (nuramesh stop RUN --human H)"),
    ("reject", "reject what waits for a person (nuramesh reject TASK --human H --reason T)"),
    ("report", "a run's report (nuramesh report RUN)"),
    ("try", "run a team folder on this host (nuramesh try TEAM --fake)"),
)


def wants_json(args: argparse.Namespace) -> bool:
    """`--output json` (the mapped route's keys, BC4)."""
    return getattr(args, "output", None) == "json"


def _age(then: str | None, now: datetime) -> str:
    if not then:
        return "-"
    try:
        seconds = int((now - datetime.fromisoformat(then)).total_seconds())
    except ValueError:
        return "-"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{max(seconds, 0)}s"


def _sqlite_state(path: Path) -> str:
    """`ok`, `missing` or `migrate` for a SQLite store file, read through a read-only connection
    (nothing is created, written or migrated)."""
    from nuramesh.stores.sqlite import MIGRATIONS

    if not path.is_file():
        return "missing"
    try:
        db = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return "migrate"
    try:
        found = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        version = (
            0
            if found is None
            else (db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0)
        )
    finally:
        db.close()
    return "ok" if version == len(MIGRATIONS) else "migrate"


def open_read_only(root: Path, override: str | None) -> "Store | str | None":
    """The team's store for a reader: a store, `"migrate"` when it needs a migration (nothing was
    changed), or None when no ledger exists yet (nothing was created)."""
    from nuramesh import cli
    from nuramesh.stores.base import SchemaOutOfDate

    url = database_url()
    if url:
        from nuramesh.stores.postgres import PostgresStore

        if not PostgresStore.has_schema(url):
            return None
        try:
            return cli._track(PostgresStore(url, migrate=False))
        except SchemaOutOfDate:
            return "migrate"
    if cli._require_database():
        raise StoreError(cli._DATABASE_REQUIRED)
    path = Path(override) if override else state_path(root, "runs.db")
    state = _sqlite_state(path)
    if state == "missing":
        return None
    if state == "migrate":
        return "migrate"
    return cli._track(SqliteStore(str(path), read_only=True))


def _own_org(store: "Store", run_id: str, org_id: str) -> bool:
    """Is this run the organization's own? Fail closed: a run with no recorded organization
    belongs to nobody (the API's scope hides it from every team too)."""
    first = list(store.read_stream(f"run:{run_id}", after_seq=0, limit=1))
    if first and first[0].type == "run.started":
        run_org = first[0].payload.get("org")
        return isinstance(run_org, str) and run_org == org_id
    return False


def _visible_to(store: "Store", org_id: str) -> Any:
    """`views.list_runs(visible=...)` for the CLI: only the organization's own runs. Like the
    API's scope it has a lookup limit: after `MAX_NEW_LOOKUPS` first-entry reads it answers None
    (not decided) and the listing stops, marked truncated."""
    from nuramesh.server.scope import MAX_NEW_LOOKUPS

    left = MAX_NEW_LOOKUPS

    def visible(stream: str) -> bool | None:
        nonlocal left
        if not stream.startswith("run:"):
            return False
        if left <= 0:
            return None
        left -= 1
        return _own_org(store, stream[4:], org_id)

    return visible


def _unfinished_rows(store: "Store", org_id: str, limit: int = 50) -> list[dict[str, Any]]:
    from nuramesh.server import views

    rows, _truncated, _scanned = views.list_runs(
        store, limit=limit, status=None, visible=_visible_to(store, org_id)
    )
    return [r for r in rows if r["status"] in UNFINISHED and r["org"] == org_id]


def waiting_items(store: "Store", config: Any, run_ids: list[str]) -> list[dict[str, Any]]:
    """What waits for a person in these runs: pending approvals and human gates of tasks being
    verified. Ids, kind, role and the time only (no `asked_because`, no title)."""
    from nuramesh.core.gates import GateKind
    from nuramesh.core.policy import ApprovalStatus
    from nuramesh.core.tasks import TaskStatus

    def role_of(agent: str | None) -> str | None:
        try:
            return str(config.org.role_of(agent).id) if agent else None
        except (KeyError, ValueError):
            return None

    items: list[dict[str, Any]] = []
    for stored in store.approvals(status=ApprovalStatus.PENDING):
        a = stored.approval
        if a.run_id in run_ids:
            items.append(
                {
                    "id": a.id,
                    "task_id": a.task_id,
                    "run_id": a.run_id,
                    "kind": "approval",
                    "role": role_of(a.agent_id),
                    "since": a.requested_at.isoformat(),
                }
            )
    for run_id in run_ids:
        for st in store.tasks_for_run(run_id):
            task = st.task
            if task.status is TaskStatus.VERIFYING and any(
                g.kind is GateKind.HUMAN for g in task.definition_of_done.gates
            ):
                items.append(
                    {
                        "id": task.id,
                        "task_id": task.id,
                        "run_id": run_id,
                        "kind": "gate",
                        "role": role_of(task.assignee),
                        "since": None,
                    }
                )
    return items


def _commands_block() -> list[str]:
    return ["commands:", *(f"  {name:<8} {text}" for name, text in COMMANDS)]


def _state_lines(
    store: "Store", config: Any, rows: list[dict[str, Any]], waiting: list[dict[str, Any]]
) -> list[str]:
    from nuramesh.core.breakers import halt_state, role_states
    from nuramesh.engine.org_halt import read_org_events

    events = read_org_events(store, config.org.id)
    halt = halt_state(events)
    paused = sorted(r for r, s in role_states(events).items() if s.paused)
    head = f"organization {config.org.id}: " + ("HALTED" if halt.halted else "running")
    if paused:
        head += f", paused roles: {', '.join(paused)}"
    now = store.now()
    lines = [head, f"unfinished runs: {len(rows)}"]
    for r in rows:
        tasks = r["tasks"]
        done = tasks["by_status"].get("done", 0)
        lines.append(
            f"  {r['run_id']}  {r['status']}  {_age(r['started_at'], now)}  "
            f"tasks {done}/{tasks['total']}"
        )
    lines.append(f"waiting for a person: {len(waiting)}")
    for w in waiting:
        lines.append(f"  {w['id']}  {w['kind']}  {w['role'] or '-'}  since {_age(w['since'], now)}")
    return lines


def _fit(lines: list[str]) -> list[str]:
    """At most `MAX_LINES` lines with the commands block kept (the long lists are cut)."""
    block = _commands_block()
    room = MAX_LINES - len(block)
    if len(lines) <= room:
        return [*lines, *block]
    return [
        *lines[: room - 1],
        f"  ... {len(lines) - room + 1} more lines (use nuramesh status)",
        *block,
    ]


def bare(args: argparse.Namespace) -> int:
    """`nuramesh` with no command: the organization's state, what runs and what waits, and the
    everyday commands. Exit 0. Opens the store read-only; without a team folder, only the
    commands."""
    from nuramesh import cli
    from nuramesh.cli_rules import discover_team
    from nuramesh.config import ConfigError
    from nuramesh.core.config import ConfigValidationError
    from nuramesh.stores.base import StoreBusyError, StoreError
    from nuramesh.team import TeamError, load_team_config

    found = discover_team(getattr(args, "team", None), cwd=Path.cwd(), env=os.environ)
    if found is None:
        if wants_json(args):
            print(json.dumps({"org": None, "runs": [], "waiting": []}, indent=2))
        else:
            print("\n".join(_commands_block()))
        return OK
    root = found[0]
    try:
        config = load_team_config(root)
        wanted = getattr(args, "org", None)
        if wanted and wanted != config.org.id:  # rule O, as for `status --org`
            print(
                f"this team folder is organization {config.org.id}, not {wanted}", file=sys.stderr
            )
            return USAGE
        opened = open_read_only(root, getattr(args, "store", None))
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except StoreBusyError as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return UNAVAILABLE
    except (ImportError, StoreError) as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return USAGE
    if opened is None or isinstance(opened, str):
        if wants_json(args):
            print(json.dumps({"org": config.org.id, "runs": [], "waiting": []}, indent=2))
            return OK
        lines = [f"organization {config.org.id}: no runs yet"]
        if opened == "migrate":
            lines = [
                f"organization {config.org.id}: the database needs a migration",
                "run: nuramesh db migrate",
            ]
        print("\n".join([*lines, *_commands_block()]))
        return OK
    rows = _unfinished_rows(opened, config.org.id)
    waiting = waiting_items(opened, config, [r["run_id"] for r in rows])
    if wants_json(args):
        from nuramesh.engine.org_halt import read_org_events
        from nuramesh.server import org_views

        events = read_org_events(opened, config.org.id)
        halt = org_views.halt_from_events(events, [r.id for r in config.org.roles], False)
        print(json.dumps({"org": halt, "runs": rows, "waiting": waiting}, indent=2, default=str))
        return OK
    print("\n".join(_fit(_state_lines(opened, config, rows, waiting))))
    return OK


def _pick_run(args: argparse.Namespace, store: "Store", config: Any, *, recent: bool) -> str | int:
    """The run to act on: the typed RUN, else (on a terminal) a numbered pick, else exit 2."""
    from nuramesh.server import views

    if args.run_id:
        return str(args.run_id)
    if not cli_prompts.can_ask(args):
        return cli_prompts.missing("RUN")
    rows, _t, _s = views.list_runs(
        store, limit=20, status=None, visible=_visible_to(store, config.org.id)
    )
    rows = [r for r in rows if r["org"] == config.org.id]
    if not recent:
        rows = [r for r in rows if r["status"] in UNFINISHED]
    if not rows:
        print("no runs to pick from", file=sys.stderr)
        return USAGE
    now = store.now()
    items = [(r["run_id"], f"{r['status']}  {_age(r['started_at'], now)}") for r in rows]
    chosen = cli_prompts.pick("pick a run:", items)
    if chosen is None:
        print("no run picked; nothing was changed", file=sys.stderr)
        return USAGE
    return chosen


def pick_run_for(args: argparse.Namespace, *, recent: bool) -> int | None:
    """Fill `args.run_id` for `stop` and `report` when it is missing (OD-18). None to go on."""
    from nuramesh import cli
    from nuramesh.config import ConfigError
    from nuramesh.core.config import ConfigValidationError
    from nuramesh.stores.base import StoreError
    from nuramesh.team import TeamError, load_team_config

    if args.run_id:
        return None
    if not cli_prompts.can_ask(args):
        return cli_prompts.missing("RUN")
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        opened = open_read_only(root, getattr(args, "store", None))
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except (ImportError, StoreError) as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return USAGE
    if opened is None or isinstance(opened, str):
        print("no runs to pick from", file=sys.stderr)
        return USAGE
    chosen = _pick_run(args, opened, config, recent=recent)
    if isinstance(chosen, int):
        return chosen
    args.run_id = chosen
    return None


def spend_lines(events: list[Any]) -> list[str]:
    """What the run's model calls cost, in the words of spec default-prices 6.1: nothing for a run
    with no call, `spend: $X` when every cost is known, and `spend: at least $X` with one line per
    call of unknown cost (the reservation charged, never zero)."""
    from nuramesh.core.price_words import (
        COST_UNKNOWN_SHORT,
        at_least_words,
        cost_unknown_call_words,
    )
    from nuramesh.engine.run import run_spend_line

    found = run_spend_line(events)
    if not found.calls:
        return []
    noun = "call" if found.calls == 1 else "calls"
    if not found.unknown:
        return [f"spend: ${found.known_total} ({found.calls} {noun})"]
    out = [
        f"spend: {at_least_words(found.at_least or '0')} "
        f"({found.calls} {noun}, {len(found.unknown)} {COST_UNKNOWN_SHORT})"
    ]
    out += [f"{u.model}: {cost_unknown_call_words(u.charged)}" for u in found.unknown]
    return out


def cmd_status(args: argparse.Namespace) -> int:
    """`nuramesh status [RUN]`: the unfinished runs and what waits for a person (no RUN), or one
    run (`GET /v1/runs` and `/v1/runs/{run_id}`)."""
    from nuramesh import cli
    from nuramesh.config import ConfigError
    from nuramesh.core.config import ConfigValidationError
    from nuramesh.server import views
    from nuramesh.stores.base import StoreBusyError, StoreError
    from nuramesh.team import TeamError, load_team_config

    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        opened = open_read_only(root, args.store)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except StoreBusyError as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return UNAVAILABLE
    except (ImportError, StoreError) as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return USAGE
    if isinstance(opened, str):
        print("the database needs a migration: run `nuramesh db migrate`", file=sys.stderr)
        return NOT_DONE
    if opened is None:
        if args.run_id:
            print(f"no run '{args.run_id}' in this store", file=sys.stderr)
            return USAGE
        opened = cli._track(SqliteStore(":memory:"))
    store = opened
    if args.pick and not args.run_id:
        chosen = _pick_run(args, store, config, recent=False)
        if isinstance(chosen, int):
            return chosen
        args.run_id = chosen
    if args.run_id:
        events = views.read_all(store, f"run:{args.run_id}")
        if not events:
            print(f"no run '{args.run_id}' in this store", file=sys.stderr)
            return USAGE
        started = next((e for e in events if e.type == "run.started"), None)
        run_org = started.payload.get("org") if started else None
        if run_org != config.org.id:
            print(f"no run '{args.run_id}' in this store", file=sys.stderr)
            return USAGE
        detail = views.run_detail(store, args.run_id, events, config)
        if wants_json(args):
            print(json.dumps(detail, indent=2, default=str))
            return OK
        summary = views.run_summary(store, args.run_id, events)
        tasks = summary["tasks"]
        print(
            f"run {args.run_id}: {summary['status']}, "
            f"started {_age(summary['started_at'], store.now())} "
            f"ago, tasks {tasks['by_status'].get('done', 0)}/{tasks['total']} done"
        )
        for line in spend_lines(events):
            print(f"  {line}")
        for w in waiting_items(store, config, [args.run_id]):
            print(f"  waiting: {w['id']}  {w['kind']}  {w['role'] or '-'}")
        return OK
    all_rows, truncated, scanned = views.list_runs(
        store, limit=50, status=None, visible=_visible_to(store, config.org.id)
    )
    rows = [r for r in all_rows if r["org"] == config.org.id]
    if wants_json(args):
        waiting = waiting_items(
            store, config, [r["run_id"] for r in rows if r["status"] in UNFINISHED]
        )
        print(
            json.dumps(
                {"runs": rows, "truncated": truncated, "scanned": scanned, "approvals": waiting},
                indent=2,
                default=str,
            )
        )
        return OK
    active = [r for r in rows if r["status"] in UNFINISHED]
    waiting = waiting_items(store, config, [r["run_id"] for r in active])
    print("\n".join(_state_lines(store, config, active, waiting)))
    return OK


# -- reject -------------------------------------------------------------------------------------


def _candidates(store: "Store", config: Any, ident: str) -> list[dict[str, Any]]:
    """What `ident` (an approval id or a task id) can mean: the items waiting for a person."""
    from nuramesh.core.policy import ApprovalStatus

    found = store.get_approval(ident)
    if found is not None:
        a = found.approval
        return [{"kind": "approval", "id": a.id, "run_id": a.run_id, "status": a.status.value}]
    out: list[dict[str, Any]] = []
    stored = store.get_task(ident)
    if stored is None:
        return out
    task = stored.task
    for s in store.approvals(run_id=task.run_id, status=ApprovalStatus.PENDING):
        if s.approval.task_id == task.id:
            out.append(
                {
                    "kind": "approval",
                    "id": s.approval.id,
                    "run_id": task.run_id,
                    "status": "pending",
                }
            )
    out.extend(
        {"kind": "gate", "id": g["id"], "run_id": task.run_id, "task_id": task.id}
        for g in _human_gates(task)
    )
    return out


def _human_gates(task: Any) -> list[dict[str, Any]]:
    from nuramesh.core.gates import GateKind
    from nuramesh.core.tasks import TaskStatus

    if task.status is not TaskStatus.VERIFYING:
        return []
    return [
        {"id": g.id, "checks": list(g.checks)}
        for g in task.definition_of_done.gates
        if g.kind is GateKind.HUMAN
    ]


def cmd_reject(args: argparse.Namespace) -> int:
    """`nuramesh reject TASK --human H --reason T [--criterion ID]...`: reject the one item that
    waits for a person on TASK (a pending approval, or a human gate of the current attempt), or
    the approval named by id. Rejecting restricts, so the typed `--human` is accepted (like
    `stop`). The ledger entries are the ones the API writes for a denial."""
    from nuramesh import cli
    from nuramesh.config import ConfigError
    from nuramesh.core.config import ConfigValidationError
    from nuramesh.engine.content import ContentError, configured_content_service
    from nuramesh.stores.base import StoreBusyError, StoreError
    from nuramesh.team import TeamError, load_team_config

    if not is_human_id(args.human or ""):
        if args.human is None and cli_prompts.can_ask(args):
            typed = cli_prompts.ask("your id (human:<id>): ")
            args.human = typed or None
        if not is_human_id(args.human or ""):
            if args.human is None:
                return cli_prompts.missing("--human")
            print(cli.HUMAN_CLAIM, file=sys.stderr)
            return USAGE
    if not args.reason:
        if not cli_prompts.can_ask(args):
            return cli_prompts.missing("--reason")
        args.reason = cli_prompts.ask("reason (required): ")
        if not args.reason:
            print("a rejection needs a reason; nothing was changed", file=sys.stderr)
            return USAGE
    if not args.task and not cli_prompts.can_ask(args):
        return cli_prompts.missing("TASK")
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        if getattr(args, "dry_run", False):
            # a dry run writes nothing: no file is created, no schema migrated, no key made
            opened = open_read_only(root, args.store)
            if opened is None or isinstance(opened, str):
                print(
                    "there is no ledger store here to check against"
                    if opened is None
                    else "the store needs a migration; a dry run does not migrate it",
                    "; nothing was changed",
                    file=sys.stderr,
                    sep="",
                )
                return USAGE
            store = opened
        else:
            store = cli._open_server_store(root, args.store)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except StoreBusyError as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return UNAVAILABLE
    except (ImportError, StoreError) as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return USAGE
    if not args.task:
        rows = _unfinished_rows(store, config.org.id)
        waiting = waiting_items(store, config, [r["run_id"] for r in rows])
        chosen = (
            cli_prompts.pick(
                "pick what to reject:",
                [(w["id"], f"{w['kind']}  {w['role'] or '-'}") for w in waiting],
            )
            if waiting
            else None
        )
        if chosen is None:
            print("nothing picked or nothing waiting; nothing was changed", file=sys.stderr)
            return USAGE
        args.task = chosen
    items = _candidates(store, config, args.task)
    items = [i for i in items if _own_org(store, i["run_id"], config.org.id)]
    pending = [i for i in items if i["kind"] == "gate" or i.get("status") == "pending"]
    if not pending:
        print(f"task {args.task} has nothing waiting for a person", file=sys.stderr)
        return USAGE
    if len(pending) > 1:
        if not cli_prompts.can_ask(args):
            print(
                f"{args.task} has {len(pending)} items waiting; name one with its id:",
                file=sys.stderr,
            )
            for i in pending:
                print(f"  {i['id']}  {i['kind']}", file=sys.stderr)
            return USAGE
        chosen = cli_prompts.pick(
            "several items are waiting:", [(i["id"], i["kind"]) for i in pending]
        )
        pending = [i for i in pending if i["id"] == chosen]
        if not pending:
            print("nothing picked; nothing was changed", file=sys.stderr)
            return USAGE
    item = pending[0]
    try:
        content = configured_content_service(store, config.content)
    except ContentError as exc:
        print(
            f"a reason is kept in the content store, which isn't available: {exc}", file=sys.stderr
        )
        return USAGE
    return _reject_item(
        args, store, config, content, item, dry=bool(getattr(args, "dry_run", False))
    )


class _DryRunDone(Exception):
    """Raised inside a transaction after every check passed, so it is rolled back (`--dry-run`)."""


def _reject_item(
    args: argparse.Namespace,
    store: "Store",
    config: Any,
    content: Any,
    item: dict[str, Any],
    *,
    dry: bool = False,
) -> int:
    from nuramesh.core.policy import ApprovalError
    from nuramesh.engine.content import ContentError
    from nuramesh.engine.gate_runners import GateError
    from nuramesh.engine.human_decisions import DecisionRefused, record_decision
    from nuramesh.engine.originator import SelfApproval
    from nuramesh.engine.policy import answer_approval, answer_approval_in
    from nuramesh.stores import NotFoundError

    try:
        if item["kind"] == "approval":
            if dry:  # the same checks as the real call, inside a transaction that is rolled back
                try:
                    with store.transaction() as uow:
                        answer_approval_in(
                            uow,
                            item["id"],
                            approver=args.human,
                            approve=False,
                            reason=args.reason,
                            policies=config.effective_policies,
                            org=config.org,
                            content=content,
                            hashing=content.ledger_hashing,
                            dry_run=True,  # every refusal, but no reason is captured, no key made
                        )
                        raise _DryRunDone
                except _DryRunDone:
                    pass
                print(f"dry run: would reject approval {item['id']}; nothing was written")
                return OK
            answered = answer_approval(
                store,
                item["id"],
                approver=args.human,
                approve=False,
                reason=args.reason,
                policies=config.effective_policies,
                org=config.org,
                content=content,
                hashing=content.ledger_hashing,
            )
            result: dict[str, Any] = {
                "id": answered.id,
                "run_id": answered.run_id,
                "task_id": answered.task_id,
                "status": answered.status.value,
                "decided_by": answered.decided_by,
            }
            text = f"rejected approval {answered.id} (run {answered.run_id})"
        else:
            task = store.get_task(item["task_id"])
            assert task is not None
            gate = next(g for g in _human_gates(task.task) if g["id"] == item["id"])
            named = list(args.criterion or [])
            unknown = [c for c in named if c not in gate["checks"]]
            if unknown:
                print(f"gate {gate['id']} has no criterion {unknown[0]!r}", file=sys.stderr)
                return USAGE
            # a decision covers every criterion; the CLI cannot approve, so every one is
            # rejected (the named ones are the ones the person means)
            findings = {c: (False, args.reason) for c in gate["checks"]}
            made = record_decision(
                store,
                config.org,
                content,
                item["run_id"],
                item["task_id"],
                gate["id"],
                decided_by=args.human,
                findings=findings,
                dry_run=dry,
            )
            if dry:
                print(
                    f"dry run: would reject gate {made.gate} of task {made.task_id}; "
                    "nothing was written"
                )
                return OK
            result = {
                "run_id": item["run_id"],
                "task_id": made.task_id,
                "gate": made.gate,
                "attempt": made.attempt,
                "submission": made.submission,
                "outcome": made.outcome,
                "decision_hash": made.decision_hash,
                "already_recorded": made.already_recorded,
            }
            text = f"rejected gate {made.gate} of task {made.task_id}: {made.outcome}"
    except (ApprovalError, SelfApproval, DecisionRefused, GateError) as exc:
        print(f"refused: {exc}; nothing was changed", file=sys.stderr)
        return REFUSED
    except NotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except ContentError as exc:
        print(f"the reason couldn't be stored: {exc}; nothing was changed", file=sys.stderr)
        return USAGE
    print(json.dumps(result, indent=2, default=str) if wants_json(args) else text)
    return OK


def add_parsers(sub: Any) -> None:
    """Register `status` and `reject`."""
    st = sub.add_parser("status", help="what runs and what waits for a person (or one run)")
    add_team(st)
    st.add_argument("run_id", nargs="?", metavar="RUN", help="one run (default: the unfinished)")
    st.add_argument("--pick", action="store_true", help="pick the run from a list (terminal)")
    st.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    st.set_defaults(func=cmd_status)
    rj = sub.add_parser(
        "reject",
        help="reject what waits for a person on a task (approving is API and app only)",
    )
    add_team(rj)
    rj.add_argument("task", nargs="?", metavar="TASK", help="a task id (or an approval id)")
    rj.add_argument(
        "--human", metavar="HUMAN", help="who is rejecting: human:<id> (your own claim)"
    )
    rj.add_argument("--reason", help="why (kept in the content store; the ledger keeps a hash)")
    rj.add_argument(
        "--criterion",
        action="append",
        metavar="ID",
        help="a criterion of the human gate you mean (every criterion is rejected)",
    )
    rj.add_argument("--store")
    rj.set_defaults(func=cmd_reject)
