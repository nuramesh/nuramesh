"""`nuramesh integrations list` and `hash` (R2.05): pure computation. Neither command opens a
connection, starts a process or contacts a service."""

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from nuramesh.core.integrations import definition_hash_of
from nuramesh.engine.integrations import load_packs

OK, USAGE = 0, 2


def _list(args: argparse.Namespace) -> int:
    for name, pack in load_packs().items():
        print(f"{name}  version {pack.pack_version}  data_kind {pack.data_kind}")
        for tool in pack.tools:
            print(f"  {tool.tool} {tool.kind}")
    return OK


def _hash(args: argparse.Namespace) -> int:
    try:
        text = sys.stdin.read() if args.file == "-" else _read(args.file)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("the input must be one JSON object (a tool object)")
        digest = definition_hash_of(data)
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {type(exc).__name__}: cannot hash this input", file=sys.stderr)
        return USAGE
    print(digest)
    return OK


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def add_parsers(sub: Any, _unused: Callable[..., Any] | None = None) -> None:
    """Register `integrations list` and `integrations hash`."""
    ig = sub.add_parser("integrations", help="integration packs: list them, hash a tool object")
    ig_sub = ig.add_subparsers(dest="integrations_command", required=True)
    lst = ig_sub.add_parser("list", help="packs, versions, data kinds and tools (no connection)")
    lst.set_defaults(func=_list)
    hsh = ig_sub.add_parser("hash", help="the definition hash of one tool object in a JSON file")
    hsh.add_argument("file", help="a JSON file holding one tool object, or - for stdin")
    hsh.set_defaults(func=_hash)
