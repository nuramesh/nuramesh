"""`nuramesh otel check | export | rotate-pseudonym-key` (E3 spec sections 2, 6.3, 10).

Read-only against the ledger. Output never holds the endpoint, a header, a key or a payload: only
closed codes, counts, stream ids and the (not secret) key id."""

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nuramesh.cli_rules import add_team
from nuramesh.telemetry.people import PseudonymKeyError, load_key, rotate_key
from nuramesh.telemetry.runtime import BAD_KEY, MISSING_EXTRA, build_tailer
from nuramesh.telemetry.settings import (
    OtelSettings,
    OtelSettingsError,
    load_settings,
    pseudonym_key_path,
)
from nuramesh.telemetry.tailer import SinkError

OK, FAILED, USAGE, UNAVAILABLE = 0, 1, 2, 5
MAX_CYCLES = 1_000_000


def _settings() -> OtelSettings | int:
    try:
        settings = load_settings(os.environ)
    except OtelSettingsError as exc:
        print(f"otel export disabled: {exc}", file=sys.stderr)
        return USAGE
    if settings is None:
        print("otel export is off: NURAMESH_OTEL_ENDPOINT is not set")
        return OK
    return settings


def _failure_code(settings: OtelSettings) -> int:
    """1 when the collector answered and the answer fails (a 5xx, a 401), 5 when nothing answered
    (connection refused, no route, timeout: a retry may work). The SDK exporter reports both as
    one failure, so a short TCP connect to the endpoint tells them apart (BC3, OD-7b). Nothing is
    sent and nothing about the endpoint is printed."""
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(settings.endpoint)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname or "", port), timeout=3):
            return FAILED
    except (OSError, ValueError):
        return UNAVAILABLE


def _sink(settings: OtelSettings) -> Any:
    from nuramesh.telemetry.runtime import make_sink

    try:
        return make_sink(settings)
    except ImportError:
        print(f"otel export disabled: {MISSING_EXTRA} (install nuramesh[otel])", file=sys.stderr)
        return None


def cmd_check(args: argparse.Namespace) -> int:
    """Validate the settings and send one empty test request per signal."""
    settings = _settings()
    if isinstance(settings, int):
        return settings
    if settings.person_ids == "pseudonym":
        # a missing key is refused here, while `export`/`serve` create it (spec 6.3)
        try:
            load_key(settings.pseudonym_key_file, create=False)  # a check never writes a key
        except (PseudonymKeyError, OSError):
            print(f"otel export disabled: {BAD_KEY}", file=sys.stderr)
            return USAGE
    sink = _sink(settings)
    if sink is None:
        return USAGE
    try:
        sink.probe(sorted(settings.signals))
    except SinkError as exc:
        print("otel check failed: " + ",".join(exc.signals), file=sys.stderr)
        return _failure_code(settings)  # 1: an answer that fails; 5: no answer (BC3, OD-7b)
    finally:
        sink.close()
    print("otel check ok: " + ",".join(sorted(settings.signals)))
    return OK


def cmd_export(args: argparse.Namespace, store_factory: Callable[[Path, str | None], Any]) -> int:
    """One foreground pass over the ledger (`--once`), optionally from the start."""
    settings = _settings()
    if isinstance(settings, int):
        return settings
    if not args.once:
        print("use --once (the server runs the continuous exporter)", file=sys.stderr)
        return USAGE
    sink = _sink(settings)
    if sink is None:
        return USAGE
    store = store_factory(Path(args.team).resolve(), args.store)
    streams = frozenset(args.stream) if args.stream else None
    try:
        try:
            tailer = build_tailer(store, settings, sink, only_streams=streams)
        except (PseudonymKeyError, OSError):
            print(f"otel export disabled: {BAD_KEY}", file=sys.stderr)
            return USAGE
        if args.from_start:
            tailer.replay(streams)
        events = spans = 0
        for _ in range(MAX_CYCLES):
            result = tailer.run_cycle()
            if not result.ok:
                print(f"otel export failed: {result.error}", file=sys.stderr)
                return _failure_code(settings)
            events += result.events
            spans += result.spans
            if not result.served:
                break
        tailer.stop()
    finally:
        sink.close()
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
    print(f"otel export done: {events} events, {spans} spans")
    return OK


def cmd_rotate(args: argparse.Namespace) -> int:
    """Replace the pseudonym key (the old one is not kept); prints the new key id."""
    try:
        key_id = rotate_key(pseudonym_key_path(os.environ))
    except (PseudonymKeyError, OSError):
        print("could not write the pseudonym key file", file=sys.stderr)
        return FAILED
    print(f"pseudonym key rotated: key id {key_id}")
    return OK


def add_parsers(sub: Any, store_factory: Callable[[Path, str | None], Any]) -> None:
    """Register `otel check|export|rotate-pseudonym-key`."""
    otel = sub.add_parser("otel", help="OpenTelemetry export (off unless NURAMESH_OTEL_ENDPOINT)")
    osub = otel.add_subparsers(dest="otel_command", required=True)
    check = osub.add_parser("check", help="validate the settings and send one empty test request")
    check.set_defaults(func=cmd_check)
    export = osub.add_parser("export", help="one foreground export pass")
    export.add_argument("--once", action="store_true", help="export what is new, then stop")
    export.add_argument("--from-start", action="store_true", help="ignore the saved cursors")
    export.add_argument("--stream", action="append", metavar="ID", help="only this stream")
    add_team(export)
    export.add_argument("--store")
    export.set_defaults(func=lambda a: cmd_export(a, store_factory))
    rotate = osub.add_parser("rotate-pseudonym-key", help="replace the pseudonym key")
    rotate.set_defaults(func=cmd_rotate)
