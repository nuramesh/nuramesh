"""Hidden aliases for the commands R1.43 renamed (`spec/cli-api-naming`, rule A).

An old name or form is rewritten to the new argv BEFORE parsing, so it runs the new command with
identical behaviour; it prints one line on stderr and nothing on stdout. Aliases are not argparse
parsers: they are not in `--help` and not in completion, and `leaf_commands` sees only new names.
One table below holds every old name; remove a row when its release is over.

The rewriter counts positionals with the option arity of the command's own parser (an option that
takes a value consumes it), and accepts the global flags (`--team`, `--org`, `--dry-run`) before
the command. Exit codes and `--json` output are not touched: the command that runs is the same.
"""

import argparse
from dataclasses import dataclass

from nuramesh.cli_rules import leaf_parsers

# One date for the CLI and the API (`server/deprecations.SUNSET`, a test keeps them equal).
SUNSET = "the old form is removed after 2027-03-01"
GLOBAL_ARITY = {"--team": 1, "--org": 1, "--dry-run": 0, "--yes": 0, "--output": 1, "--store": 1}


@dataclass(frozen=True)
class Rename:
    """An old command words prefix and the new one. `unless_next`: when the word after the old
    prefix is one of these, the argv is already the new form (a group that was a leaf)."""

    old: tuple[str, ...]
    new: tuple[str, ...]
    unless_next: frozenset[str] = frozenset()
    legacy_exit: int = 2  # what the old command exited with where the new one exits 5 (OD-13)


RENAMES: tuple[Rename, ...] = (
    Rename(("run",), ("try",)),
    Rename(("migrate",), ("db", "migrate")),
    Rename(("expire-content",), ("content", "expire")),
    Rename(("scorecard",), ("scorecards",)),
    Rename(
        ("autonomy", "grants"),
        ("autonomy", "grants", "list"),
        frozenset({"list", "create", "revoke"}),
    ),
    Rename(("autonomy", "grant"), ("autonomy", "grants", "create")),
    Rename(("autonomy", "revoke"), ("autonomy", "grants", "revoke")),
    Rename(("autonomy", "ceiling"), ("autonomy", "ceilings")),
    Rename(("autonomy", "settings"), ("autonomy", "settings", "set"), frozenset({"set"})),
    Rename(("auth", "key"), ("auth", "factor-key")),
    Rename(("auth", "mfa-reset"), ("auth", "mfa", "reset")),
    Rename(("verify-ledger",), ("ledger", "verify")),
    Rename(("export-ledger",), ("ledger", "export")),
)

# Commands (new words) whose first positional used to be the team folder (rule T).
TEAM_WAS_POSITIONAL = frozenset(
    {
        "org status",
        "org halt",
        "org prompt-cache",
        "roles pause",
        "breakers list",
        "breakers trip",
        "db migrate",
        "content expire",
        "auth new-setup-token",
        "auth purge-sessions",
        "auth erase-owner",
        "auth close-org",
        "auth recovery-code issue",
        "auth factor-key init",
        "auth factor-key rotate",
        "auth factor-key destroy",
        "auth mfa status",
        "auth mfa reset",
        "auth tokens create",
        "auth tokens list",
        "auth tokens revoke",
        "agents credentials list",
        "agents credentials revoke",
        "agents credentials revoke-all",
        "anchor export",
        "eval run",
        "eval status",
        "calibrate run",
        "calibrate status",
        "models catalog",
        "models assignments",
        "models connections",
        "models disable",
        "models disconnect",
        "models test",
        "models connect",
        "models pause",
        "models retire",
        "models price",
        "models local-model",
        "models check",
        "ledger verify",
        "ledger export",
    }
)
# Of those, the commands whose TEAM was the LAST positional (`org prompt-cache on|off TEAM`).
TEAM_WAS_LAST = frozenset({"org prompt-cache"})
# Commands whose first positional used to be the organization acted on (rule O).
ORG_WAS_POSITIONAL = frozenset(
    {"content shred", "keys status", "keys reconcile", "keys adopt-legacy"}
)
# `stop TEAM --run-id R` and `report TEAM --run-id R` became `stop R` and `report R`.
RUN_ID_FORMS = frozenset({"stop", "report"})


def legacy_exit(argv: list[str]) -> int:
    """The exit code an old name gives where the new name exits 5 (unavailable): every old
    command reported a store it could not use as 2 (usage), so that is the default; a rename
    row can say otherwise in its `legacy_exit`. `argv` is the rewritten argv."""
    words = _leading_words(argv)
    for r in RENAMES:
        if tuple(words[: len(r.new)]) == r.new and r.new != r.old:
            return r.legacy_exit
    return 2


def _arity(action: argparse.Action | None) -> int:
    if action is None or action.nargs == 0:
        return 0
    if isinstance(action.nargs, int):
        return action.nargs
    return 1


def _positionals(
    leaf: argparse.ArgumentParser, tokens: list[str], extra: dict[str, int]
) -> list[int]:
    """The indexes of the positional arguments in `tokens`, by the leaf's own option arity."""
    out: list[int] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            out.extend(range(i + 1, len(tokens)))
            break
        if tok.startswith("-") and len(tok) > 1:
            if "=" in tok:
                i += 1
            else:
                i += 1 + extra.get(tok, _arity(leaf._option_string_actions.get(tok)))
            continue
        out.append(i)
        i += 1
    return out


def _leading_words(tokens: list[str]) -> list[str]:
    words: list[str] = []
    for tok in tokens:
        if tok.startswith("-") or len(words) == 3:
            break
        words.append(tok)
    return words


def _split_globals(argv: list[str]) -> tuple[list[str], list[str]] | None:
    """`(global flags typed before the command, the rest)`; None when argv starts with anything
    else that is an option (`--help`, `--version`): nothing is rewritten then."""
    head: list[str] = []
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        name = argv[i].split("=", 1)[0]
        if name not in GLOBAL_ARITY:
            return None
        take = 1 if "=" in argv[i] else 1 + GLOBAL_ARITY[name]
        head.extend(argv[i : i + take])
        i += take
    return head, argv[i:]


def command_words(root: argparse.ArgumentParser, argv: list[str]) -> tuple[str, ...]:
    """The command words of `argv` as the (new) parser knows them."""
    parts = _split_globals(argv)
    rest = parts[1] if parts else argv
    leaves = {words for words, _p in leaf_parsers(root)}
    best: tuple[str, ...] = ()
    words = _leading_words(rest)
    for n in range(1, len(words) + 1):
        if tuple(words[:n]) in leaves:
            best = tuple(words[:n])
    return best


def rewrite(root: argparse.ArgumentParser, argv: list[str]) -> tuple[list[str], str | None]:
    """`(argv to parse, the one line to print on stderr or None)`."""
    parts = _split_globals(argv)
    if parts is None or not parts[1]:
        return argv, None
    head, rest = parts
    typed = _leading_words(rest)
    old_display = ""
    renamed = False
    new_len = 0
    for r in sorted(RENAMES, key=lambda r: -len(r.old)):
        n = len(r.old)
        if tuple(typed[:n]) != r.old:
            continue
        if r.unless_next and len(typed) > n and typed[n] in r.unless_next:
            continue
        old_display = " ".join(r.old)
        rest = [*r.new, *rest[n:]]
        renamed = True
        new_len = len(r.new)
        break
    leaves = dict(leaf_parsers(root))
    words = _leading_words(rest)
    path: tuple[str, ...] = ()
    for n in range(1, len(words) + 1):
        if tuple(words[:n]) in leaves:
            path = tuple(words[:n])
    if not path:
        return ([*rest, *head] if (renamed or head) else argv), _line(
            renamed, old_display, rest, "", ""
        )
    name = " ".join(path)
    leaf = leaves[path]
    tail = rest[len(path) :]
    extra_old = ""
    extra_new = ""
    if name in RUN_ID_FORMS and any(t == "--run-id" or t.startswith("--run-id=") for t in tail):
        tail, ok = _run_id_form(leaf, tail)
        if ok:
            extra_old = " TEAM --run-id RUN"
            extra_new = " RUN --team TEAM"
    elif name in TEAM_WAS_POSITIONAL:
        found = _positionals(leaf, tail, {})
        wanted = sum(1 for a in leaf._actions if not a.option_strings and a.dest != "help")
        wanted -= sum(1 for a in leaf._actions if isinstance(a, argparse._SubParsersAction))
        if len(found) == wanted + 1:
            team = tail.pop(found[-1] if name in TEAM_WAS_LAST else found[0])
            tail = [*tail, "--team", team]
            extra_old, extra_new = " TEAM", " --team DIR"
    elif name in ORG_WAS_POSITIONAL:
        found = _positionals(leaf, tail, {})
        if found:
            org = tail.pop(found[0])
            tail = [*tail, "--org", org]
            extra_old, extra_new = " ORG", " --org ORG"
    changed = renamed or bool(extra_old) or bool(head)
    new_argv = [*path, *tail, *head] if changed else argv
    if renamed and len(path) > new_len:
        # a renamed group word: show the whole old command, not only the old group word
        old_display = " ".join([old_display, *path[new_len:]])
    old_text = (old_display or name) + extra_old
    new_text = name + extra_new
    return new_argv, (
        f"`nuramesh {old_text}` was renamed to `nuramesh {new_text}`; {SUNSET}"
        if old_text != new_text
        else None
    )


def _line(renamed: bool, old_display: str, rest: list[str], a: str, b: str) -> str | None:
    if not renamed:
        return None
    new = " ".join(_leading_words(rest))
    return f"`nuramesh {old_display}` was renamed to `nuramesh {new}`; {SUNSET}"


def _run_id_form(leaf: argparse.ArgumentParser, tail: list[str]) -> tuple[list[str], bool]:
    """Old `stop TEAM --run-id R ...`: one positional (TEAM) and a value for `--run-id`."""
    run_id: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok == "--run-id" and i + 1 < len(tail):
            run_id = tail[i + 1]
            i += 2
        elif tok.startswith("--run-id="):
            run_id = tok.split("=", 1)[1]
            i += 1
        else:
            rest.append(tok)
            i += 1
    if run_id is None:
        return tail, False
    found = _positionals(leaf, rest, {})
    if len(found) != 1:
        return tail, False
    team = rest.pop(found[0])
    return [run_id, *rest, "--team", team], True
