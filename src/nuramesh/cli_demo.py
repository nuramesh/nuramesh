"""`nuramesh demo`: the first end-to-end demo (`docs/demo.md`) in one command (#991).

Copies the packaged `dev-team` template into a new folder and runs it with `try --fake
--approve-as human:demo`: the fake model only (no key, no cost, no network call), a scripted
approval recorded as such in the ledger. The tests of the demo project run in a container; with none
the command stops and says so, unless `--allow-unsandboxed` is given (printed and recorded)."""

import argparse
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from typing import Any

from nuramesh.cli_doctor import container_runtime

OK, FAILED, USAGE = 0, 1, 2
APPROVER = "human:demo"


def run_team(args: argparse.Namespace) -> int:
    """Run the folder (tests replace this)."""
    from nuramesh.cli import cmd_run

    return cmd_run(args)


def cmd_demo(args: argparse.Namespace) -> int:
    """Create the demo team folder and run it on the fake model."""
    from nuramesh.cli import _require_database, cmd_init, parser
    from nuramesh.env_bool import EnvBoolError
    from nuramesh.secret_env import SecretEnvError, database_url

    try:
        require_database = _require_database()
        url_set = database_url() is not None  # the plain variable or its _FILE twin
    except (EnvBoolError, SecretEnvError) as exc:  # a bad setting refuses, never lets it write
        print(f"demo refused: {exc}", file=sys.stderr)
        return USAGE
    for var, set_ in (
        ("NURAMESH_DATABASE_URL", url_set),
        ("NURAMESH_REQUIRE_DATABASE", require_database),
    ):
        if set_:  # the run would write to that database, and the ledger is append-only
            print(
                f"demo refused: {var} (or NURAMESH_DATABASE_URL_FILE) is set, and the demo "
                "would write its org, run and ledger entries into that database "
                "(append-only, cannot be undone).\n"
                f"Unset {var} and run `nuramesh demo` again.",
                file=sys.stderr,
            )
            return USAGE
    runtime = container_runtime()
    if runtime is None and not args.allow_unsandboxed:
        print(
            "no container runtime (Podman or Docker) answered, and the demo's tests run in one.\n"
            "Install Podman (`brew install podman`), or run `nuramesh demo --allow-unsandboxed` "
            "for a local look (the run records that the sandbox was off).",
            file=sys.stderr,
        )
        return FAILED
    if args.allow_unsandboxed:
        if runtime is None:
            print(
                "running UNSANDBOXED: no container runtime answered, so the demo's tests run on "
                "this machine without isolation (recorded in the ledger)",
                file=sys.stderr,
            )
        else:
            print(
                f"--allow-unsandboxed has no effect: {runtime} answered, so the demo's tests run "
                "in a container",
                file=sys.stderr,
            )
    target = (
        Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="nuramesh-demo-")) / "team"
    )
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        code = cmd_init(argparse.Namespace(template="dev-team", dir=str(target)))
    if code != OK:
        return code  # cmd_init said why on stderr (never overwrites a folder)
    print(f"demo team folder: {target} (fake model only: no key, no cost)", file=sys.stderr)
    argv = ["try", str(target), "--fake", "--approve-as", APPROVER]
    if args.allow_unsandboxed:
        argv.append("--allow-unsandboxed")
    run_args = parser().parse_args(argv)
    run_args.json = getattr(args, "output", None) == "json"
    return run_team(run_args)


def add_parsers(sub: Any) -> None:
    """Register `demo`."""
    demo = sub.add_parser("demo", help="run the demo dev team on the fake model (no cost)")
    demo.add_argument(
        "--dir", metavar="DIR", help="where to create the team folder (default: temp)"
    )
    demo.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="LOCAL ONLY: when no container runtime exists, run the demo's tests on this machine "
        "(recorded, not isolated); no effect when a runtime is present",
    )
    demo.set_defaults(func=cmd_demo)
