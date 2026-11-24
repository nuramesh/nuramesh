"""`nuramesh savings tokens` (R1.34 Part A).

Read only: it opens the team's store like `nuramesh report` and prints the same answer as
`GET /v1/savings/tokens`. It writes no ledger entry, makes no network call and has no `apply`
verb: the open core only shows the figures (prompt caching is switched on or off with
`nuramesh org prompt-cache`, ND320)."""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nuramesh.stores.base import StoreError

OK, USAGE = 0, 2
SUBCOMMANDS = ("tokens",)
StoreFactory = Callable[[Path, str | None], Any]


def _tokens(args: argparse.Namespace, factory: StoreFactory) -> int:
    from nuramesh.config import ConfigError
    from nuramesh.core.config import ConfigValidationError
    from nuramesh.server.cost_reader import CostReadError
    from nuramesh.server.token_report import token_report
    from nuramesh.team import TeamError, load_team_config

    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    try:
        store = factory(root, args.store)
    except (ImportError, StoreError) as exc:
        print(f"could not open the store: {exc}", file=sys.stderr)
        return USAGE
    try:
        report = token_report(
            store,
            config,
            group_by=args.group_by,
            from_=args.start,
            to=args.end,
            as_of=args.as_of,
        )
    except CostReadError as exc:
        print(f"cannot answer ({exc.status} {exc.code}): {exc.detail}", file=sys.stderr)
        return USAGE
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    print(json.dumps(report, indent=2) if args.json else _text(report))
    return OK


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _text(report: dict[str, Any]) -> str:
    lines = [
        f"Tokens and cache, {report['from']} to {report['to']} (as of {report['as_of']}), "
        f"by {report['group_by']}; money in {report['currency']}"
    ]
    if not report["rows"]:
        lines.append("no model calls in this period")
    for row in report["rows"]:
        unpriced = row["unknown_calls"]
        lines.append(
            f"{row['group']}: {_n(row['calls'], 'call')}"
            + (
                f", {_n(unpriced, 'call')} not priced, excluded from the money figures"
                if unpriced
                else ""
            )
        )
        lines.append(
            f"  tokens: input {row['input_tokens']}, output {row['output_tokens']}, "
            f"cached input {row['cached_input_tokens']}, cache write {row['cache_write_tokens']}"
            + (
                f"; {_n(row['calls_without_usage'], 'call')} without usage"
                if row["calls_without_usage"]
                else ""
            )
        )
        rate = row["cache_hit_rate"]
        lines.append(f"  cache hit rate: {'n/a' if rate is None else rate}")
        lines.append(
            f"  cache saving {row['cache_saving']}, write premium {row['cache_write_premium']}, "
            f"net {row['net_cache_saving']}"
            + (
                f" (excludes {_n(row['unknown_saving_calls'], 'call')} whose saving is unknown)"
                if row["unknown_saving_calls"]
                else ""
            )
        )
        share = row["cache_saving_share"]
        lines.append(
            f"  spent {row['known_total']}; saving as a share of the no-cache cost: "
            f"{'n/a' if share is None else share}"
        )
    return "\n".join(lines)


def add_parsers(sub: Any, store_factory: StoreFactory) -> None:
    """Register `savings tokens`."""
    sv = sub.add_parser("savings", help="token and cache figures read from the ledger")
    sv_sub = sv.add_subparsers(dest="savings_command", required=True)
    tk = sv_sub.add_parser("tokens", help="tokens by kind, cache hit rate and what the cache saved")
    tk.add_argument("--team", default=".", help="the team folder (default: this folder)")
    tk.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    tk.add_argument("--from", dest="start", help="start of the period, UTC (default: this month)")
    tk.add_argument("--to", dest="end", help="end of the period, UTC, exclusive")
    tk.add_argument("--as-of", help="ignore anything recorded after this instant (default: now)")
    tk.add_argument("--group-by", choices=("none", "provider", "model", "role"), default="none")
    tk.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    tk.set_defaults(func=lambda a: _tokens(a, store_factory))
