"""`nuramesh demo`: the first end-to-end demo in one command (#991).

Copies the packaged `dev-team` template into a new folder and runs it with `try --fake
--approve-as human:demo`: the fake model only (no key, no cost, no network call), a scripted
approval recorded as such in the ledger. The tests of the demo project run in a container; with none
the command stops and says so, unless `--allow-unsandboxed` is given (printed and recorded)."""

import argparse
import contextlib
import io
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any

from nuramesh.cli_doctor import container_runtime

OK, FAILED, USAGE = 0, 1, 2
APPROVER = "human:demo"
DEFAULT_PORT = 8765


def run_team(args: argparse.Namespace) -> int:
    """Run the folder (tests replace this)."""
    from nuramesh.cli import cmd_run

    return cmd_run(args)


def serve_team(args: argparse.Namespace) -> int:
    """Serve the folder (tests replace this)."""
    from nuramesh.cli import cmd_serve

    return cmd_serve(args)


def issue_setup_token(team: str) -> bool:
    """Show a first-owner setup token on this terminal (stderr, never stdout); True when shown."""
    from nuramesh.cli_security import cmd_auth_new_setup_token

    code = cmd_auth_new_setup_token(argparse.Namespace(team=team, store=None))
    if code != OK:
        print(
            "no setup token was shown, so nothing is served; create one in a terminal with "
            f"`nuramesh auth new-setup-token --team {team}`",
            file=sys.stderr,
        )
    return code == OK


def _stop_on_signals() -> list[tuple[int, Any]]:
    """Make SIGTERM and SIGHUP (terminal closed, `kill`) stop the demo like Ctrl-C, so the temp
    folder (team store, ledger, open setup token) is removed. Returns the handlers to restore."""

    def stop(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)

    saved: list[tuple[int, Any]] = []
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved.append((sig, signal.signal(sig, stop)))
        except ValueError:  # not the main thread: keep the default
            pass
    return saved


def cmd_demo(args: argparse.Namespace) -> int:
    """Create the demo team folder and run it on the fake model."""
    from nuramesh.cli import _require_database
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

    stack = bool(getattr(args, "stack", False))
    if not stack and getattr(args, "port", None) is not None:
        print("--port applies only with --stack", file=sys.stderr)
        return USAGE
    if args.port is None:
        args.port = DEFAULT_PORT
    if stack and getattr(args, "output", None) == "json":
        print("--stack serves until you stop it; it has no --output json", file=sys.stderr)
        return USAGE
    if stack and not 1 <= args.port <= 65535:
        print(f"--port must be from 1 to 65535, not {args.port}", file=sys.stderr)
        return USAGE
    if stack:
        from nuramesh.cli import port_is_free

        if not port_is_free("127.0.0.1", args.port):  # before any folder or token is made
            print(
                f"--port {args.port} is not free on 127.0.0.1; pass another with --port",
                file=sys.stderr,
            )
            return USAGE
    if stack and not sys.stderr.isatty():
        print(
            "--stack needs a terminal: the first-owner setup token is shown only on one, and a "
            "stack with no owner cannot be signed in to. Run it in a terminal (nothing was "
            "created or served).",
            file=sys.stderr,
        )
        return USAGE
    runtime = container_runtime()
    if runtime is None and not args.allow_unsandboxed:
        print(
            "no container runtime (Podman or Docker) answered, and the demo's tests run in one.\n"
            "Next step: run `nuramesh doctor` to see what is missing, install Podman "
            "(`brew install podman`), or, for a first look with the fake model only, run "
            "`nuramesh demo --allow-unsandboxed` (the run records that the sandbox was off).",
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
    scratch = None if args.dir else Path(tempfile.mkdtemp(prefix="nuramesh-demo-"))
    target = Path(args.dir) if scratch is None else scratch / "team"
    served: list[bool] = []  # set once the server started: only then did a stack run to stop
    restore = _stop_on_signals() if stack else []
    try:
        return _demo(args, target, stack, served)
    finally:
        for sig, old in restore:
            signal.signal(sig, old)
        if stack and scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
            if served:
                print(
                    f"demo stack stopped; its temporary folder {scratch} was removed",
                    file=sys.stderr,
                )
        elif stack and served:
            print(f"demo stack stopped; the folder {target} was kept", file=sys.stderr)


def _demo(args: argparse.Namespace, target: Path, stack: bool, served: list[bool]) -> int:
    """Create the folder, run the demo and, with `--stack`, serve it until stopped."""
    from nuramesh.cli import cmd_init, parser

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
    code = run_team(run_args)
    if not stack or code != OK:
        return code
    port = args.port
    url = f"http://127.0.0.1:{port}"
    from nuramesh.cli import _hosted_env, setup_url_base

    try:
        hosted = _hosted_env()
    except Exception:  # noqa: BLE001 (an unreadable setting counts as hosted: no link promised)
        hosted = True
    # one token only: when `serve` will print its one-time setup link (it retires every earlier
    # token), the demo issues none of its own; otherwise the demo shows one on this terminal
    serve_prints_link = (
        setup_url_base(
            auth=True,
            host="127.0.0.1",
            port=port,
            trusted_proxy=False,
            allowed_hosts=[],
            hosted=hosted,
        )
        is not None
    )
    if serve_prints_link:
        how = (
            "open the one-time setup link that serve prints below (it works once, for 15 "
            "minutes); the first person to redeem it becomes the owner"
        )
    elif not issue_setup_token(str(target)):
        return USAGE
    else:
        how = (
            f"open the setup page ({url}/setup, also the 'Create the owner account' link on the "
            "sign-in page) and paste the setup token shown above; the first person to redeem it "
            "becomes the owner"
        )
    print(
        f"demo stack: serving {target} at {url} on this machine only (loopback, sign-in "
        f"required, fake model: nothing leaves this machine). To sign in, {how}. "
        "Press Ctrl-C to stop.",
        file=sys.stderr,
    )
    serve_args = parser().parse_args(
        ["serve", str(target), "--auth", "--host", "127.0.0.1", "--port", str(port)]
    )
    served.append(True)
    return serve_team(serve_args)


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
    demo.add_argument(
        "--stack",
        action="store_true",
        help="after the run, serve the folder as a throwaway local stack (loopback, sign-in on, "
        "fake model) until Ctrl-C; a temp folder is removed when it stops",
    )
    demo.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"port for --stack only (default {DEFAULT_PORT}, loopback only)",
    )
    demo.set_defaults(func=cmd_demo)
