"""`nuramesh scorecards show` and `nuramesh autonomy ...` (E13 + E14, `spec/autonomy/README.md`).

The commands open the team's store like `nuramesh stop` does and call the same engine functions
as the HTTP routes. Reading needs no `--human`. A decision (`grant`, `revoke`, `ceiling set`,
`settings`) needs `--human human:<id>`, and the engine refuses anything but a person. Someone
who can open the team's store and key folder is the operator of that organization, so the CLI
acts with the owner's rights (the same trust as `content shred`); the roles of the API do not
apply here.
"""

import argparse
import json
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from nuramesh.cli_rules import add_team
from nuramesh.core.autonomy import DEFAULT_EXPIRY_DAYS
from nuramesh.core.people import is_human_id
from nuramesh.core.proposals import solo_lines
from nuramesh.core.scorecard import DEFAULT_MIN_RESULTS, ScorecardError
from nuramesh.engine import autonomy
from nuramesh.engine.autonomy import AutonomyError
from nuramesh.engine.content import ContentError, configured_content_service
from nuramesh.stores.base import StoreError

OK, USAGE, REFUSED = 0, 2, 3
HUMAN_CLAIM = "--human needs an opaque id like human:u_7k2x9q (no email or name)"
StoreFactory = Callable[[Path, str | None], Any]


def _when(text: str | None, default: datetime) -> datetime:
    if text is None:
        return default
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _context(args: argparse.Namespace, factory: StoreFactory, *, decide: bool) -> Any:
    """`(config, store)` or an exit code."""
    from nuramesh.config import ConfigError
    from nuramesh.core.config import ConfigValidationError
    from nuramesh.team import TeamError, load_team_config

    if decide and not is_human_id(getattr(args, "human", None) or ""):
        print(HUMAN_CLAIM, file=sys.stderr)
        return USAGE
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    try:
        return config, factory(root, args.store)
    except (ImportError, StoreError) as exc:
        print(f"could not open the store: {exc}", file=sys.stderr)
        return USAGE


def _run(args: argparse.Namespace, factory: StoreFactory, body: Callable[[Any, Any], Any]) -> int:
    decide = bool(getattr(args, "decides", False))
    got = _context(args, factory, decide=decide)
    if isinstance(got, int):
        return got
    config, store = got
    try:
        result = body(config, store)
    except (AutonomyError, ScorecardError) as exc:
        print(f"refused ({exc.status} {exc.code}): {exc.detail}", file=sys.stderr)
        return REFUSED
    except (ContentError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    if args.json or not isinstance(result, str):
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result)
    return OK


def _scorecard(args: argparse.Namespace, factory: StoreFactory) -> int:
    def body(config: Any, store: Any) -> Any:
        now = store.now()
        when = _when(args.as_of, now)
        kind, subject = ("agent", args.agent) if args.agent else ("role", args.role)
        return autonomy.read_scorecard(
            store,
            config.org.id,
            kind=kind,
            subject=subject,
            as_of=when,
            start=None if args.start is None else _when(args.start, when),
            end=None if args.end is None else _when(args.end, when),
            min_results=args.min_results,
            evals=config.evals,
        )

    args.json = True
    return _run(args, factory, body)


def solo_report(items: list[dict[str, Any]]) -> list[str]:
    """The solo share of each proposal item, as lines for a person (SA-18 item 2 of
    `spec/rbac/solo-approval.md`), each named by its subject and cell."""
    lines: list[str] = []
    for item in items:
        subject, cell, kind = item["subject"], item["cell"], item.get("kind")
        name = f"{subject['kind']} {subject['id']}, rule {cell['rule_key']}"
        if kind is not None:
            name += f" ({kind})"
        if "approved_asks" not in item["evidence"]:
            continue  # never_grantable / no_such_rule: nothing was counted, so nothing is shown
        solo = item["solo"]
        approved = int(item["evidence"]["approved_asks"])
        for line in solo_lines(approved, int(solo["approved_asks_solo"]), solo["solo_share"]):
            lines.append(f"{name}: {line}")
    return lines


def _propose(args: argparse.Namespace, factory: StoreFactory) -> int:
    def body(config: Any, store: Any) -> Any:
        when = _when(args.as_of, store.now())
        role = args.role
        if role is None:
            agent = next((a for a in config.org.agents if a.id == args.agent), None)
            if agent is None:
                raise ValueError(f"agent '{args.agent}' is not in the organization")
            role = agent.role
        items: list[dict[str, Any]] = []
        position = 0
        for kind, subject in [("role", role), *([("agent", args.agent)] if args.agent else [])]:
            found, position = autonomy.read_proposals(
                store, config.org.id, kind=kind, subject=subject, as_of=when, evals=config.evals
            )
            items += [p.to_json() for p in found]
        # SA-18 item 2: the solo share for the person reading; stdout stays one JSON document
        for line in solo_report(items):
            print(line, file=sys.stderr)
        return {"items": items, "ledger_position": position, "as_of": when.isoformat()}

    args.json = True
    return _run(args, factory, body)


def _grants(args: argparse.Namespace, factory: StoreFactory) -> int:
    def body(config: Any, store: Any) -> Any:
        when = _when(args.as_of, store.now())
        items = autonomy.list_grants(
            store, config.org.id, as_of=when, evals=config.evals, active_only=not args.all
        )
        return {"items": items, "as_of": when.isoformat()}

    args.json = True
    return _run(args, factory, body)


def _grant(args: argparse.Namespace, factory: StoreFactory) -> int:
    def body(config: Any, store: Any) -> Any:
        kind, subject = ("agent", args.agent) if args.agent else ("role", args.role)
        return autonomy.grant(
            store,
            config.org.id,
            actor=args.human,
            actor_role="owner",
            kind=kind,
            subject=subject,
            rule_key=args.rule,
            as_of=_when(args.as_of, store.now()),
            ledger_position=args.ledger_position,
            evidence_hash=args.evidence_hash,
            expires_in_days=args.days,
            reason=args.reason,
            evals=config.evals,
            content=configured_content_service(store, config.content),
            grant_id=uuid.uuid4().hex,
        )

    args.decides = True
    return _run(args, factory, body)


def _revoke(args: argparse.Namespace, factory: StoreFactory) -> int:
    def body(config: Any, store: Any) -> Any:
        return autonomy.revoke(
            store,
            config.org.id,
            args.grant_id,
            actor=args.human,
            actor_role="owner",
            reason=args.reason,
            evals=config.evals,
            content=configured_content_service(store, config.content),
        )

    args.decides = True
    return _run(args, factory, body)


def _ceiling(args: argparse.Namespace, factory: StoreFactory) -> int:
    def body(config: Any, store: Any) -> Any:
        if args.role not in [r.id for r in config.org.roles]:
            raise ValueError(f"role '{args.role}' is not in the organization")
        return autonomy.set_ceiling(
            store,
            config.org.id,
            args.role,
            args.kind,
            args.value,
            actor=args.human,
            actor_role="owner",
            reason=args.reason,
            content=configured_content_service(store, config.content),
        )

    args.decides = True
    return _run(args, factory, body)


def _setting(args: argparse.Namespace, factory: StoreFactory) -> int:
    def body(config: Any, store: Any) -> Any:
        try:
            value: int | Decimal = int(args.value)
        except ValueError:
            try:
                value = Decimal(args.value)
            except InvalidOperation:
                raise ValueError("value is a whole number or a decimal") from None
        return autonomy.set_setting(
            store,
            config.org.id,
            args.name,
            value,
            actor=args.human,
            actor_role="owner",
            reason=args.reason,
            content=configured_content_service(store, config.content),
        )

    args.decides = True
    return _run(args, factory, body)


def add_parsers(sub: Any, store_factory: StoreFactory) -> None:
    """Register `scorecard show` and `autonomy propose|grants|grant|revoke|ceiling|settings`."""

    def common(p: argparse.ArgumentParser, *, human: bool) -> None:
        add_team(p)
        p.add_argument("--store")
        p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
        if human:
            p.add_argument("--human", required=True, metavar="HUMAN", help="who: human:<id>")
            p.add_argument("--reason", required=True)

    def bind(p: argparse.ArgumentParser, fn: Callable[[argparse.Namespace, StoreFactory], int]):  # type: ignore[no-untyped-def]
        p.set_defaults(func=lambda a: fn(a, store_factory))

    def subject(p: argparse.ArgumentParser, *, required: bool) -> None:
        group = p.add_mutually_exclusive_group(required=required)
        group.add_argument("--role")
        group.add_argument("--agent")

    sc = sub.add_parser(
        "scorecards", help="agent records: the verified results of an agent or role"
    )
    sc_sub = sc.add_subparsers(dest="scorecard_command", required=True)
    show = sc_sub.add_parser("show", help="one scorecard, read from the ledger")
    common(show, human=False)
    subject(show, required=True)
    show.add_argument("--from", dest="start")
    show.add_argument("--to", dest="end")
    show.add_argument("--as-of")
    show.add_argument("--min-results", type=int, default=DEFAULT_MIN_RESULTS)
    bind(show, _scorecard)

    au = sub.add_parser("autonomy", help="earned autonomy: proposals, grants, ceilings, settings")
    au_sub = au.add_subparsers(dest="autonomy_command", required=True)
    pr = au_sub.add_parser("propose", help="what could be granted now, with the evidence")
    common(pr, human=False)
    subject(pr, required=True)
    pr.add_argument("--as-of")
    bind(pr, _propose)
    gg = au_sub.add_parser("grants", help="grants: list, create, revoke")
    gg_sub = gg.add_subparsers(dest="grants_command", required=True)
    gl = gg_sub.add_parser("list", help="grants and their state")
    common(gl, human=False)
    gl.add_argument("--all", action="store_true", help="include grants that are not active")
    gl.add_argument("--as-of")
    bind(gl, _grants)
    gr = gg_sub.add_parser("create", help="grant an ask rule to a role or agent (a person decides)")
    common(gr, human=True)
    subject(gr, required=True)
    gr.add_argument("--rule", type=int, required=True, help="the rule key of the proposal")
    gr.add_argument("--as-of", required=True)
    gr.add_argument("--ledger-position", type=int, required=True)
    gr.add_argument("--evidence-hash", required=True)
    gr.add_argument("--days", type=int, default=DEFAULT_EXPIRY_DAYS)
    bind(gr, _grant)
    rv = gg_sub.add_parser("revoke", help="end a grant at once")
    common(rv, human=True)
    rv.add_argument("grant_id")
    bind(rv, _revoke)
    ce = au_sub.add_parser("ceilings", help="the most a role may be granted, per action kind")
    ce_sub = ce.add_subparsers(dest="ceiling_command", required=True)
    cs = ce_sub.add_parser("set", help="set a ceiling to none or allow")
    common(cs, human=True)
    cs.add_argument("--role", required=True)
    cs.add_argument("--kind", required=True)
    cs.add_argument("--value", required=True, choices=["none", "allow"])
    bind(cs, _ceiling)
    sg = au_sub.add_parser("settings", help="thresholds: make one stricter (never looser)")
    sg_sub = sg.add_subparsers(dest="settings_command", required=True)
    st = sg_sub.add_parser("set", help="make a threshold stricter (never looser)")
    common(st, human=True)
    st.add_argument("--name", required=True)
    st.add_argument("--value", required=True)
    bind(st, _setting)
