"""`did you mean ...?` for a mistyped command or sub-command (#1259).

Pure text: it looks at the real parser's command paths and the old names `cli_aliases` rewrites,
and never runs anything. A name the open parser does not have is never suggested.
"""

import argparse
import difflib
import re
import sys
from typing import NoReturn

from nuramesh.cli_aliases import RENAMES, _split_globals
from nuramesh.cli_rules import leaf_parsers

MAX_ECHO = 80  # a mistyped token is echoed at most this long
_INVALID = re.compile(r"invalid choice: '((?:[^'\\]|\\.)*)'")


def _typed_words(typed: list[str]) -> list[str]:
    parts = _split_globals(typed)
    rest = parts[1] if parts else typed
    while rest and rest[0] == "--json":  # the old global flag is not in GLOBAL_ARITY
        rest = rest[1:]
    words: list[str] = []
    for tok in rest:
        if tok.startswith("-"):
            break
        words.append(tok)
    return words


def suggest(parser: argparse.ArgumentParser, message: str, typed: list[str]) -> str | None:
    """The text to print for an unknown command word, or None for any other usage error."""
    from nuramesh.cli_model_keys import is_key_command

    if is_key_command(typed):
        return None  # a `models key` usage error never echoes a typed value
    match = _INVALID.search(message)
    if match is None:
        return None
    # only the top-level command action: a bad value of another positional keeps argparse's text
    prefix_text = message.split("invalid choice", 1)[0]
    if not any(
        isinstance(a, argparse._SubParsersAction)
        and prefix_text.strip() == f"argument {argparse._get_action_name(a)}:"
        for a in parser._actions
    ):
        return None
    bad = match.group(1)
    words = _typed_words(typed)
    if bad not in words:
        return None
    depth = words.index(bad)
    prefix = tuple(words[:depth])
    shown = " ".join(words[: depth + 1])
    options: dict[str, str] = {}  # what a user may have typed -> what to suggest
    for path, _leaf in leaf_parsers(parser):
        full = " ".join((*prefix, *path))
        head = " ".join(full.split()[: depth + 1])
        options[head] = head
        options[full] = full
    if not prefix:
        for r in RENAMES:
            options.setdefault(" ".join(r.old), " ".join(r.new))
    close = difflib.get_close_matches(shown, list(options), n=1, cutoff=0.6)
    echoed = shown if len(shown) <= MAX_ECHO else shown[:MAX_ECHO] + "..."
    lines = [f"nuramesh: unknown command '{echoed}'"]
    if close:
        lines.append(f"did you mean '{options[close[0]]}'?")
    lines.append("run 'nuramesh --help' for the list")
    return "\n".join(lines)


def fail(text: str) -> NoReturn:
    """Print `text` on stderr and exit 2 (usage), like `ArgumentParser.error`."""
    print(text, file=sys.stderr)
    raise SystemExit(2)
