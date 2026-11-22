"""Questions the CLI asks on a terminal (R1.43 PR A2, `spec/cli-api-naming` section 6).

Rules (each is a test in `tests/test_cli_guards.py`):
- P1: a question is shown only when stdin AND stderr are terminals and `CI` is not set; the
  question and any pick list go to **stderr**, so stdout carries only the result.
- P2: with `--yes`, without a terminal, or with `CI` set nothing is asked: a missing argument is
  exit 2 naming the flag.
- P7: a pick list shows ids, states and ages only, never a title, a reason or content.
- P8: Ctrl-C at a question writes nothing and exits 130 (`main` turns the interrupt into 130).

Imports only the standard library.
"""

import argparse
import os
import sys
from collections.abc import Sequence

USAGE = 2


def on_terminal() -> bool:
    """True when a person can be asked: stdin and stderr are terminals and `CI` is not set."""
    return bool(sys.stdin.isatty() and sys.stderr.isatty() and not os.environ.get("CI"))


def can_ask(args: argparse.Namespace) -> bool:
    """Whether a missing argument or a confirmation may be asked for this command."""
    return not getattr(args, "yes", False) and on_terminal()


def ask(question: str) -> str | None:
    """One line typed at the terminal, or None at end of input. The question goes to stderr."""
    print(question, end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if line == "":
        print(file=sys.stderr)
        return None
    return line.strip()


def missing(what: str) -> int:
    """Say that `what` is missing and cannot be asked for; the exit code (2)."""
    print(f"missing {what}: pass it, or run on a terminal", file=sys.stderr)
    return USAGE


def pick(title: str, items: Sequence[tuple[str, str]]) -> str | None:
    """A numbered pick of `(id, description)` items on stderr; the chosen id, or None for an
    empty or invalid answer. Descriptions hold states and ages only (P7)."""
    print(title, file=sys.stderr)
    for number, (item_id, text) in enumerate(items, 1):
        print(f"  {number}. {item_id}  {text}", file=sys.stderr)
    answer = ask(f"number (1-{len(items)}): ")
    if answer is None or not answer.isdigit() or not 1 <= int(answer) <= len(items):
        return None
    return items[int(answer) - 1][0]


def confirm_org(args: argparse.Namespace, org: str, what: str) -> int | None:
    """The OD-14 confirmation for an irreversible command that runs without a terminal today
    (`content shred`, `auth factor-key destroy`). `--confirm ORG` must match when given. On a
    terminal the organization id is typed; `--yes` skips the question; no terminal and no
    `--yes` is exit 2 and nothing changes. None to go on, else the exit code."""
    given = getattr(args, "confirm", None)
    if given is not None and given != org:
        print(f"--confirm does not match the organization; {what} did nothing", file=sys.stderr)
        return USAGE
    if getattr(args, "yes", False):
        return None
    if not on_terminal():
        print(
            f"{what} is irreversible: confirm on a terminal by typing the organization id, "
            "or pass --yes; nothing was changed",
            file=sys.stderr,
        )
        return USAGE
    answer = ask("Type the organization id to confirm: ")
    if answer != org:
        print("the id does not match; nothing was changed", file=sys.stderr)
        return USAGE
    return None
