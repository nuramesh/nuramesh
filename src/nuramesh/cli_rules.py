"""The CLI rules every command shares (R1.43, rules T and O).

Rule T: a command finds its team folder from `--team DIR`, else `NURAMESH_TEAM_DIR`, else the
nearest folder upwards holding `nuramesh.yaml`, `.yml` or `.json`; none found is exit 2 naming the
three ways. The irreversible operator commands refuse a folder found any way but `--team`.
Rule O: `--org X` that differs from the team folder's organization is exit 2, never a switch.
`--dry-run`: the commands that write say what they would do and write nothing; the commands that
cannot be dry-run refuse with exit 2.

Imports only the standard library (the config loader is imported inside the one function that
needs it), so every `cli_*.py` module can use `add_team`.
"""

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

TEAM_ENV = "NURAMESH_TEAM_DIR"
USAGE = 2
CONFIG_NAMES = ("nuramesh.yaml", "nuramesh.yml", "nuramesh.json")  # team.CONFIG_NAMES

NO_TEAM_TEXT = (
    "no team folder: pass --team DIR, set NURAMESH_TEAM_DIR, or run from a folder (or below one) "
    "that holds nuramesh.yaml"
)

# Commands that name the team folder some other way, or have none (path words, as typed now).
SKIP_TEAM = frozenset({"try", "worker", "validate", "init", "completion", "demo", "doctor"})
# Irreversible operator commands: only an explicit --team names the folder (never a discovery).
EXPLICIT_TEAM_ONLY = frozenset(
    {
        "auth erase-owner",
        "auth close-org",
        "auth purge-sessions",
        "auth factor-key destroy",
        "auth mfa reset",
        "content shred",
    }
)
# Operator commands print which organization and folder they act on, first, on stderr.
OPERATOR = frozenset({"db migrate", *EXPLICIT_TEAM_ONLY})
# Terminal-only operator commands: no flag skips their prompts, so they take no `--yes` and no
# `--confirm` (T128, OD-14): only a person at a terminal answers.
NO_YES = frozenset({"auth erase-owner", "auth close-org"})
# Commands whose `--org` names the organization acted on (a subject), not the team's own.
SUBJECT_ORG = frozenset({"content shred", "keys status", "keys reconcile", "keys adopt-legacy"})

# `--dry-run`: Y = prints what it would do and writes nothing; R = refused (exit 2). Every other
# command only reads, so the flag is accepted and the command runs as usual.
DRY_RUN_YES = frozenset(
    {
        "init",
        "stop",
        "org halt",
        "org prompt-cache",
        "roles pause",
        "breakers trip",
        "db migrate",
        "content expire",
        "content shred",
        "keys reconcile",
        "keys adopt-legacy",
        "autonomy grants create",
        "autonomy grants revoke",
        "autonomy ceilings set",
        "autonomy settings set",
        "auth purge-sessions",
        "auth erase-owner",
        "auth close-org",
        "auth factor-key init",
        "auth factor-key rotate",
        "auth factor-key destroy",
        "auth mfa reset",
        "auth tokens revoke",
        "auth tokens rename",
        "auth tokens delete",
        "auth tokens scrub-erased",
        "auth invites revoke",
        "agents credentials revoke",
        "agents credentials revoke-all",
        "anchor export",
        "models disable",
        "models disconnect",
        "models connect",
        "models key set",
        "models key rotate",
        "models key revoke",
        "models pause",
        "models retire",
        "models price",
    }
)
DRY_RUN_REFUSED = frozenset(
    {
        "try",
        "serve",
        "worker",
        "minter anchor-record",
        "minter anchor-install",
        "otel export",
        "otel rotate-pseudonym-key",
        "email check",
        "demo",
        "auth new-setup-token",
        "auth recovery-code issue",
        "auth password-reset",
        "auth tokens create",
        "auth invites create",
        "auth invites resend",
        "anchor keygen",
        "anchor rotate",
        "eval run",
        "calibrate run",
        "models test",
    }
)


def add_team(parser: argparse.ArgumentParser) -> None:
    """Give a command the `--team DIR` option (rule T)."""
    parser.add_argument(
        "--team",
        metavar="DIR",
        default=None,
        help="the team folder (default: NURAMESH_TEAM_DIR, else the nearest folder above "
        "this one holding nuramesh.yaml)",
    )


def discover_team(
    explicit: str | None, *, cwd: Path, env: Mapping[str, str]
) -> tuple[Path, str] | None:
    """`(folder, how)` with how in `flag`, `env`, `walk`; `None` when nothing is found."""
    if explicit:
        return Path(explicit), "flag"
    named = env.get(TEAM_ENV)
    if named:
        return Path(named), "env"
    here = cwd.resolve()
    for folder in (here, *here.parents):
        if any((folder / name).is_file() for name in CONFIG_NAMES):
            return folder, "walk"
    return None


def _team_org(folder: Path) -> str | None:
    """The folder's organization id, or None when its configuration cannot be read."""
    from nuramesh.team import TeamError, load_team_config

    try:
        return str(load_team_config(folder).org.id)
    except (TeamError, OSError, ValueError, KeyError):
        return None
    except Exception:  # noqa: BLE001  (a configuration error type: the command reports it itself)
        return None


def _say(text: str) -> None:
    print(text, file=sys.stderr)


# What the real command checks only after it opens a store: a dry run cannot know, and says so.
NEEDS_STORE = {
    "stop": "whether the run exists in the store",
    "db migrate": "which migrations are pending",
    "content expire": "how many rows would expire",
}
HUMAN_REQUIRED = frozenset({"stop"})
# `--output json` (freeze B, #895): honoured by these commands (the mapped route's keys) ...
JSON_OUTPUT = frozenset(
    {
        "status",
        "stop",
        "reject",
        "report",
        "org status",
        "breakers list",
        "auth tokens list",
        "models catalog",
        "models key status",
        "doctor",
        "demo",
    }
)
# ... and on these it is the same as their older `--json` flag (their own JSON, unchanged); every
# other command refuses `--output json` with exit 2 instead of printing a table.
JSON_BY_FLAG = frozenset(
    {
        "try",
        "scorecards show",
        "autonomy propose",
        "autonomy grants list",
        "autonomy grants create",
        "autonomy grants revoke",
        "autonomy ceilings set",
        "autonomy settings set",
        "savings tokens",
        "anchor verify",
        "models connections",
        "models presets",
        "models check",
    }
)
# Commands whose real call is refused without a terminal on stdin AND stdout (`_operator_gate`).
TERMINAL_ONLY = frozenset({"auth erase-owner", "auth close-org"})
USER_ID_TEXT = "--user needs an opaque id like human:u_7k2x9q (no email or name)"


def _dry_run_problem(args: argparse.Namespace, path: str) -> str | None:
    """The checks the real command makes before it opens a store (same message, same exit 2)."""
    from nuramesh.core.people import is_human_id
    from nuramesh.secret_env import database_url

    if path == "db migrate" and not database_url():
        return "migrate needs NURAMESH_DATABASE_URL (it never uses SQLite)"
    if path in TERMINAL_ONLY and not (sys.stdin.isatty() and sys.stdout.isatty()):
        return (
            f"{path.split()[-1]} must be run from an interactive terminal (stdin and stdout), by "
            "a person; it is refused in scripts, pipes and redirected output. In a container: "
            "docker exec -it <container> nuramesh ..."
        )
    if path == "auth mfa reset" and not is_human_id(str(getattr(args, "user", None) or "")):
        return USER_ID_TEXT
    human = getattr(args, "human", None)
    if (human or path in HUMAN_REQUIRED) and not is_human_id(human or ""):
        return "--human needs an opaque id like human:u_7k2x9q (no email or name)"
    return None


def add_global_options(
    leaf: argparse.ArgumentParser, *, has_team: bool, allow_yes: bool = True
) -> None:
    """The flags every command accepts: `--dry-run`, and `--org` where there is a team folder."""
    existing = set(leaf._option_string_actions)
    if "--dry-run" not in existing:
        leaf.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="check and say what would change; write nothing (not for every command)",
        )
    if allow_yes and "--yes" not in existing:
        leaf.add_argument(
            "--yes",
            action="store_true",
            default=False,
            help="never ask: a missing argument is exit 2, a confirmation is taken as yes",
        )
    if "--output" not in existing:
        leaf.add_argument(
            "--output",
            choices=("table", "json"),
            default=None,
            help="table (default) or json (the mapped route's keys)",
        )
    if has_team and "--org" not in existing:
        leaf.add_argument(
            "--org",
            metavar="ORG",
            default=None,
            help="must equal the team folder's organization (exit 2 otherwise)",
        )


def apply_rules(args: argparse.Namespace, path: str, *, cwd: Path | None = None) -> int | None:
    """Rules T and O and `--dry-run` for the command `path` (the words as typed now). Returns an
    exit code when the command must not run (or has been dry-run), else None. Sets `args.team`."""
    if getattr(args, "output", None) == "json":
        if path in JSON_BY_FLAG:
            args.json = True
        elif path not in JSON_OUTPUT:
            _say(
                f"--output json is not supported by `{path}` (it prints text only); "
                "`nuramesh <command> --help` and the command line guide list the commands that "
                "support it"
            )
            return USAGE
    dry = bool(getattr(args, "dry_run", False))
    if dry and path in DRY_RUN_REFUSED:
        _say(f"--dry-run is not supported by {path}")
        return USAGE
    folder: Path | None = None
    if hasattr(args, "team") and path not in SKIP_TEAM and not path.startswith("minter"):
        found = discover_team(getattr(args, "team", None), cwd=cwd or Path.cwd(), env=os.environ)
        if found is None:
            _say(NO_TEAM_TEXT)
            return USAGE
        folder, how = found
        if path in EXPLICIT_TEAM_ONLY and how != "flag":
            _say(
                f"{path} is irreversible: name the team folder with --team DIR (not found by "
                f"{TEAM_ENV} or by looking upwards)"
            )
            return USAGE
        args.team = str(folder)
    elif getattr(args, "team", None):
        folder = Path(str(args.team))
    wanted_org = getattr(args, "org", None)
    team_org: str | None = None
    if folder is not None and (path in OPERATOR or (wanted_org and path not in SUBJECT_ORG)):
        team_org = _team_org(folder)
    if wanted_org and path not in SUBJECT_ORG and team_org is not None and wanted_org != team_org:
        _say(f"this team folder is organization {team_org}, not {wanted_org}")
        return USAGE
    if path in OPERATOR and folder is not None:
        subject = wanted_org if path in SUBJECT_ORG else team_org
        _say(f"organization {subject or '(unknown)'} (team folder {folder})")
    if dry and path in DRY_RUN_YES:
        problem = _dry_run_problem(args, path)
        if problem is not None:
            _say(problem)
            return USAGE
        where = f" in team folder {folder}" if folder is not None else ""
        print(
            f"dry run: nuramesh {path}{where}: arguments and folder are valid; "
            "nothing was run or written"
        )
        if path in NEEDS_STORE:
            print(f"note: {NEEDS_STORE[path]}; not checked (A1 dry run never opens a store)")
        return 0
    return None


def leaf_parsers(root: argparse.ArgumentParser) -> list[tuple[tuple[str, ...], Any]]:
    """Every leaf command of the parser tree as `(words, parser)`."""
    out: list[tuple[tuple[str, ...], Any]] = []

    def walk(p: argparse.ArgumentParser, words: tuple[str, ...]) -> None:
        subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            out.append((words, p))
            return
        for sub in subs:
            for name, child in sub.choices.items():
                walk(child, (*words, name))

    walk(root, ())
    return out
