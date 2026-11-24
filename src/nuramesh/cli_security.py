"""The security-sensitive CLI commands, split out of `cli.py` so ordinary CLI changes stay tier 2
(#1224, #1132): setup token, recovery code, erase-owner, close-org, key init/rotate/destroy, MFA
status/reset, API tokens, agent credentials, session purge, content expiry, the organization halt
(kill switch) and the prompt-cache switch. A pure move: same names, output and exit codes. The
helpers that stay in `cli.py` (`_open_server_store`, `_store_problem`, `_org_context`, ...) are
looked up on the `nuramesh.cli` module at call time, so the existing patch points keep working."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from nuramesh.config import ConfigError
from nuramesh.core.config import ConfigValidationError
from nuramesh.core.people import is_human_id
from nuramesh.engine.content import ContentError
from nuramesh.stores.base import StoreError
from nuramesh.team import TeamError, load_team_config

OK, NOT_DONE, USAGE, REFUSED = 0, 1, 2, 3


class _CliProxy:
    """Late lookup of helpers that stay in `cli.py` (it imports this module: no top import)."""

    def __getattr__(self, name: str) -> Any:
        import nuramesh.cli as cli

        return getattr(cli, name)


cli = _CliProxy()


def cmd_expire_content(args: argparse.Namespace) -> int:
    """Delete every stored content record whose retention has passed (never for an organization on
    legal hold). A run does this itself while it works; this is for a store with no run active
    (cron or by hand). One `content.expired` ledger entry per organization, with counts only. Also
    sweeps approvals past their 13 months (ND195 a): `approvals.swept`, counts only."""
    from nuramesh.engine.approvals_sweep import sweep_approvals
    from nuramesh.engine.content import configured_content_service

    try:  # the worker's store: PostgreSQL when NURAMESH_DATABASE_URL is set (RET-WIRE R4a)
        store = cli._open_server_store(Path(args.team).resolve(), args.store)
    except (ImportError, StoreError) as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return int(cli._open_failed_code(exc))
    sweep_failure: str | None = None
    try:  # approvals first: they need no content store (R11); a failure is reported, not a trace
        swept = sweep_approvals(store)
        print(f"swept {swept.deleted} approval(s)")
    except Exception as exc:  # the content expiry still runs; the failure is reported after it
        sweep_failure = f"the approvals sweep failed: {type(exc).__name__}"
    try:
        content = configured_content_service(store)
    except ContentError as exc:
        print(f"the content store isn't available: {exc}", file=sys.stderr)
        if sweep_failure is not None:
            print(sweep_failure, file=sys.stderr)
        return USAGE
    from nuramesh.engine.autonomy import recapture_configs

    print(f"expired {content.expire()} content record(s)")
    print(f"re-captured {recapture_configs(store, content)} configuration(s) before expiry")
    if sweep_failure is not None:
        print(sweep_failure, file=sys.stderr)
        return NOT_DONE
    return OK


def cmd_auth_purge_sessions(args: argparse.Namespace) -> int:
    """Retention for a store no API handles logins for (R1.29 `retention` service): sessions
    ended 90 days ago, stale counters, security-log rows past their retention (ND60). Prints a
    count only."""
    from nuramesh.server.sessions import AuthService

    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        store = cli._open_server_store(root, args.store)
    except (TeamError, ConfigError, ConfigValidationError, ImportError, StoreError) as exc:
        print(
            cli._store_problem(exc) if isinstance(exc, ImportError | StoreError) else str(exc),
            file=sys.stderr,
        )
        return int(cli._open_failed_code(exc))
    print(f"purged {AuthService(store, config.org.id).purge()} record(s)")
    return OK


def cmd_org_halt(args: argparse.Namespace) -> int:
    """Halt the organization: no new work or model call starts, open runs are asked to stop."""
    from nuramesh.engine.killswitch import halt_org

    got = cli._org_context(args, restricting=True)
    if isinstance(got, int):
        return got
    config, store = got
    outcome = halt_org(store, config.org.id, actor=args.human, via="cli")
    state = outcome.state
    if outcome.already_halted:
        print(
            f"organization {config.org.id} is already halted by {state.by} "
            f"(ledger entry {state.seq}); no change"
        )
    else:
        print(
            f"organization {config.org.id} halted by {args.human} (ledger entry {state.seq}); "
            f"stop requested for {len(outcome.stopped_runs)} run(s)"
        )
    return OK


def cmd_org_prompt_cache(args: argparse.Namespace) -> int:
    """Switch provider-side prompt caching on or off for the organization (R1.34, ND320). Like the
    API route it is refused in hosted mode; the CLI opens the team's own store, so it acts with the
    owner's rights (as `autonomy` decisions do) and needs `--human human:<id>`."""
    from nuramesh.engine.killswitch import OrgErasedError
    from nuramesh.engine.prompt_cache import (
        PromptCacheHosted,
        refuse_if_hosted,
        set_prompt_cache_in,
    )

    if not is_human_id(args.human or ""):
        print(cli.HUMAN_CLAIM, file=sys.stderr)
        return USAGE
    try:
        refuse_if_hosted()
    except PromptCacheHosted as exc:
        print(str(exc), file=sys.stderr)
        return REFUSED
    got = cli._org_context(args, restricting=False)
    if isinstance(got, int):
        return got
    config, store = got
    want = args.state == "on"
    from nuramesh.engine.content import configured_content_service

    try:
        # refuse_if_hosted ran above, so this does not raise for hosted problems; if it ever
        # does, the command fails closed (the erased check is never skipped).
        service = configured_content_service(store, config.content)
        with store.transaction() as uow:
            if (
                uow.key_tombstone(config.org.id) is not None
                or service.retired(config.org.id) is not None
            ):
                raise OrgErasedError("the organization was erased")
            change = set_prompt_cache_in(uow, config.org.id, want, actor=args.human, via="cli")
    except OrgErasedError as exc:
        print(str(exc), file=sys.stderr)
        return REFUSED
    word = "on" if change.enabled else "off"
    if change.changed:
        print(
            f"prompt caching is now {word} for organization {config.org.id} "
            f"(ledger entry {change.seq}); the provider keeps cached text for about 5 minutes"
        )
    else:
        print(f"prompt caching is already {word} for organization {config.org.id}; no change")
    return OK


def cmd_auth_new_setup_token(args: argparse.Namespace) -> int:
    """Retire any open setup token and print a new one on this terminal (stderr only)."""
    from nuramesh.server.sessions import AuthService

    if not sys.stderr.isatty():
        print(
            "run this in a terminal: the token is shown only on a terminal. In a container: "
            "docker exec -it <container> nuramesh auth new-setup-token --team <team>",
            file=sys.stderr,
        )
        return USAGE
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        store = cli._open_server_store(root, args.store)
    except (TeamError, ConfigError, ConfigValidationError, ImportError, StoreError) as exc:
        print(
            cli._store_problem(exc) if isinstance(exc, ImportError | StoreError) else str(exc),
            file=sys.stderr,
        )
        return USAGE
    outcome = AuthService(store, config.org.id).issue_setup_token(rotate=True)
    if outcome.status == "owner_exists":
        print("an owner already exists; no setup token is needed", file=sys.stderr)
        return USAGE
    if outcome.status == "closed":
        print("the organization was closed; no owner can be set up", file=sys.stderr)
        return USAGE
    assert outcome.issued is not None and outcome.expires_at is not None
    print(
        f"setup token (shown once, it cannot be shown again):\n  {outcome.issued.token}\n"
        f"It expires at {outcome.expires_at:%Y-%m-%d %H:%M} UTC and works once.",
        file=sys.stderr,
    )
    return OK


def _operator_service(args: argparse.Namespace) -> Any:
    """The `AuthService` over the server's own store for an operator command (T128), or an error
    text. Needs the team folder and the database, i.e. a shell on the server: there is no HTTP
    route for these commands."""
    from nuramesh.server.sessions import AuthService

    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        store = cli._open_server_store(root, args.store)
    except (TeamError, ConfigError, ConfigValidationError, ImportError, StoreError) as exc:
        return cli._store_problem(exc) if isinstance(exc, ImportError | StoreError) else str(exc)
    content = None
    try:  # erasing a person also scrubs their configuration free text (ND218)
        from nuramesh.engine.content import configured_content_service

        content = configured_content_service(store)
    except ImportError:  # no content extra: nothing was ever stored
        pass
    except ContentError as exc:  # e.g. a bad NURAMESH_HOSTED: one line, no traceback (ND220)
        return str(exc)
    return AuthService(store, config.org.id, content=content)


def _operator_gate(args: argparse.Namespace, what: str) -> Any:
    """The checks every operator command that erases people passes before anything is asked
    (T128): an interactive terminal on stdin AND stdout (no script, no pipe, no flag or variable
    that stands in for it), the server's store, and a recovery code file only this OS user can
    read. Returns the `AuthService`, or the exit code after printing why."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            f"{what} must be run from an interactive terminal (stdin and stdout), by a person; "
            "it is refused in scripts, pipes and redirected output. In a container: "
            "docker exec -it <container> nuramesh ...",
            file=sys.stderr,
        )
        return USAGE
    service = _operator_service(args)
    if isinstance(service, str):
        print(service, file=sys.stderr)
        return USAGE
    from nuramesh.server.recovery import RecoveryCodeError, inspect_recovery_file

    try:
        inspect_recovery_file(Path(args.team).resolve())
    except RecoveryCodeError as exc:
        print(f"refused: {exc}; nothing was changed", file=sys.stderr)
        return REFUSED
    return service


def _ask_recovery_code(args: argparse.Namespace, service: Any) -> int | None:
    """Ask for the recovery code at a hidden prompt and check it (it is spent later, by
    `_spend_recovery_code`). None if it is good, else the exit code."""
    import getpass

    from nuramesh.server.recovery import RecoveryCodeError, verify_recovery_code

    code = getpass.getpass("operator recovery code (typing is hidden): ")
    try:
        verify_recovery_code(Path(args.team).resolve(), code, service.store.now())
    except RecoveryCodeError as exc:
        print(f"refused: {exc}; nothing was changed", file=sys.stderr)
        return REFUSED
    args.recovery_code_typed = code
    return None


def _spend_recovery_code(
    args: argparse.Namespace, service: Any, command: str, actor: str
) -> int | None:
    """Just before the action's own transaction: spend the code (single use) and ledger that.
    If the action is then refused the code stays spent, so a code never serves two actions.
    None to go on, else the exit code."""
    from nuramesh.server.recovery import RecoveryCodeError, claim_recovery_code

    code = args.recovery_code_typed
    args.recovery_code_typed = None
    try:
        claim_recovery_code(Path(args.team).resolve(), code, service.store.now())
    except RecoveryCodeError as exc:
        print(f"refused: {exc}; nothing was changed", file=sys.stderr)
        return REFUSED
    service.record_recovery_code_used(command, actor)
    return None


def cmd_auth_recovery_code_issue(args: argparse.Namespace) -> int:
    """`nuramesh auth recovery-code issue`: make the operator recovery code and show it once, on
    this terminal's standard error. Run by the OS user that runs the server. The issue is ledgered
    (operator, team, expiry; never the code) before the code is shown; if that fails no code is
    left."""
    from nuramesh.server.recovery import issue_recovery_code, operator_actor, recovery_code_path

    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        print(
            "run this in a terminal: the recovery code is shown only on a terminal, once",
            file=sys.stderr,
        )
        return USAGE
    service = _operator_service(args)
    if isinstance(service, str):
        print(service, file=sys.stderr)
        return USAGE
    root = Path(args.team).resolve()
    issued = issue_recovery_code(root, service.store.now())
    try:
        service.record_recovery_code_issued(operator_actor(), issued.expires_at)
    except Exception as exc:  # the code must not exist if its issue was not recorded
        recovery_code_path(root).unlink(missing_ok=True)
        print(
            f"could not record the issue, so no code was made: {type(exc).__name__}",
            file=sys.stderr,
        )
        return USAGE
    print(
        f"operator recovery code (shown once, it cannot be shown again):\n  {issued.code}\n"
        f"It expires at {issued.expires_at:%Y-%m-%d %H:%M} UTC and works for one command "
        "(auth erase-owner or auth close-org). Any older code no longer works.",
        file=sys.stderr,
    )
    return OK


def cmd_auth_erase_owner(args: argparse.Namespace) -> int:
    """`nuramesh auth erase-owner`: erase a user (the sole owner) after handing ownership to a
    named active user or a new one, in one transaction (T128). Interactive terminal only; asks
    for the recovery code and the user id again; nothing personal is printed."""
    import getpass

    from nuramesh.server.recovery import operator_actor
    from nuramesh.server.sessions import AuthProblem, NewOwner

    if (args.promote is None) == (args.new_owner_email is None):
        print("give exactly one of --promote or --new-owner-email", file=sys.stderr)
        return USAGE
    service = _operator_gate(args, "erase-owner")
    if isinstance(service, int):
        return service
    problem = _ask_recovery_code(args, service)
    if problem is not None:
        return problem
    if input("type the user id again to proceed: ").strip() != args.user:
        print("the id does not match; nothing was changed", file=sys.stderr)
        return USAGE
    new_owner = None
    if args.new_owner_email is not None:
        first = getpass.getpass("new owner's password: ")
        if getpass.getpass("repeat the password: ") != first:
            print("the passwords differ; nothing was changed", file=sys.stderr)
            return USAGE
        new_owner = NewOwner(args.new_owner_email, args.new_owner_name, first)
    actor = operator_actor()
    problem = _spend_recovery_code(args, service, "erase-owner", actor)
    if problem is not None:
        return problem
    try:
        promote = args.promote
        if promote is not None and "@" in promote:
            found = service.store.get_user_by_email(service.org_id, promote)
            promote = None if found is None else found.id
            if promote is None:
                print("no active user has that email; nothing was changed", file=sys.stderr)
                return REFUSED
        result = service.operator_erase_owner(
            args.user, promote=promote, new_owner=new_owner, actor=actor
        )
    except AuthProblem as exc:
        print(f"refused ({exc.code}): {exc.detail}; nothing was changed", file=sys.stderr)
        return REFUSED
    if not result.erased:
        print("no such active user; nothing was changed", file=sys.stderr)
        return NOT_DONE
    print(f"erased {args.user}; the owner now is {result.new_owner_id}")
    if getattr(service.store, "wal_truncate_pending", False):
        print(
            "erased, WAL truncate pending (a reader holds the log; it is retried)", file=sys.stderr
        )
    return OK


def cmd_auth_close_org(args: argparse.Namespace) -> int:
    """`nuramesh auth close-org`: close the organization (T128): end every session and API token,
    erase every user, ledger `org.closed`. Interactive terminal only; asks for the recovery code
    and the organization id."""
    from nuramesh.server.recovery import operator_actor

    service = _operator_gate(args, "close-org")
    if isinstance(service, int):
        return service
    problem = _ask_recovery_code(args, service)
    if problem is not None:
        return problem
    if input("type the organization id to proceed: ").strip() != service.org_id:
        print("the id does not match; nothing was changed", file=sys.stderr)
        return USAGE
    actor = operator_actor()
    problem = _spend_recovery_code(args, service, "close-org", actor)
    if problem is not None:
        return problem
    count = service.close_organization(actor=actor)
    if count == 0:
        print("nothing to close: the organization has no active users", file=sys.stderr)
        return NOT_DONE
    print(f"organization closed; {count} users erased")
    if getattr(service.store, "wal_truncate_pending", False):
        print(
            "erased, WAL truncate pending (a reader holds the log; it is retried)", file=sys.stderr
        )
    return OK


def _operator_only() -> bool:
    """`NURAMESH_OPERATOR=1` marks the operator's shell; else the command is refused."""
    from nuramesh.env_bool import EnvBoolError, env_bool

    try:
        operator = env_bool("NURAMESH_OPERATOR")
    except EnvBoolError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return False
    if not operator:
        print("refused: operator only", file=sys.stderr)
        return False
    return True


def cmd_auth_key_init(args: argparse.Namespace) -> int:
    """`nuramesh auth factor-key init --file PATH`: make the auth key file that seals TOTP secrets.
    Operator only. Prints the key's id and the variable to set, never the
    key; the ledger event and the file stand or fall together."""
    from nuramesh.server.mfa_keys import AuthKeyFileError, init_key_file
    from nuramesh.server.mfa_operator import MfaOperator
    from nuramesh.server.recovery import operator_actor

    if not _operator_only():
        return NOT_DONE
    service = _operator_service(args)
    if isinstance(service, str):
        print(service, file=sys.stderr)
        return USAGE
    path = Path(args.file)
    try:
        new_id = init_key_file(path)
    except AuthKeyFileError as exc:
        print(f"{exc}; nothing was written", file=sys.stderr)
        return NOT_DONE
    try:
        MfaOperator(service).key_created(new_id, operator_actor())
    except Exception as exc:  # no key file may outlive an unrecorded creation
        path.unlink(missing_ok=True)
        print(
            f"could not record the key, so no key file was kept: {type(exc).__name__}",
            file=sys.stderr,
        )
        return NOT_DONE
    print(f"auth key created key_id={new_id} file={path}")
    print(f"NURAMESH_AUTH_KEY_FILE={path}")
    print(
        f"The file is owned by uid {path.stat().st_uid}. Start-up refuses a file the server's "
        "user does not own: run this as the server's OS user, or chown the file to it."
    )
    return OK


def cmd_auth_key_rotate(args: argparse.Namespace) -> int:
    """`nuramesh auth factor-key rotate`: re-seal every factor under the previous key with the
    current one. Operator only; exit 1 when a factor is unreadable."""
    from nuramesh.server.mfa_keys import AuthKeyFileError, key_ring_from_env
    from nuramesh.server.mfa_operator import KeyCommandError, MfaOperator
    from nuramesh.server.recovery import operator_actor

    if not _operator_only():
        return NOT_DONE
    try:
        ring = key_ring_from_env(os.environ, strict=False)
    except AuthKeyFileError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return NOT_DONE
    if ring is None:
        print("refused: NURAMESH_AUTH_KEY_FILE is not set", file=sys.stderr)
        return NOT_DONE
    service = _operator_service(args)
    if isinstance(service, str):
        print(service, file=sys.stderr)
        return USAGE
    try:
        done = MfaOperator(service).rotate(ring, operator_actor())
    except KeyCommandError as exc:
        print(f"refused: {exc}; nothing was changed", file=sys.stderr)
        return NOT_DONE
    print(f"rotated {done.rotated}, skipped {done.skipped}, unreadable {done.unreadable}")
    if done.unreadable:
        print(
            "some factors are sealed under a key that is not loaded: add its file as "
            "NURAMESH_AUTH_KEY_FILE_PREVIOUS and run rotate again",
            file=sys.stderr,
        )
        return NOT_DONE
    return OK


def cmd_auth_key_destroy(args: argparse.Namespace) -> int:
    """`nuramesh auth factor-key destroy`: delete every factor, code and challenge, switch the
    policy off, then zero and unlink the key files. Interactive terminal only;
    needs an operator recovery code."""
    from nuramesh.cli_prompts import confirm_org
    from nuramesh.server.mfa_keys import AuthKeyFileError, key_ring_from_env
    from nuramesh.server.mfa_operator import KeyCommandError, MfaOperator
    from nuramesh.server.recovery import operator_actor

    if not _operator_only():
        return NOT_DONE
    try:
        ring = key_ring_from_env(os.environ, strict=False)
    except AuthKeyFileError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return NOT_DONE
    service = _operator_gate(args, "auth factor-key destroy")
    if isinstance(service, int):
        return NOT_DONE if service == REFUSED else service
    try:  # before any code is asked for or spent: no key file, no ledger entry, no change
        MfaOperator(service).check_destroyable(ring)
    except KeyCommandError as exc:
        print(f"refused: {exc}; nothing was changed", file=sys.stderr)
        return NOT_DONE
    asked = confirm_org(args, service.org_id, "auth factor-key destroy")  # OD-14, K24
    if asked is not None:
        return asked
    problem = _ask_recovery_code(args, service)
    if problem is not None:
        return NOT_DONE if problem == REFUSED else problem
    actor = operator_actor()
    problem = _spend_recovery_code(args, service, "auth-key-destroy", actor)
    if problem is not None:
        return NOT_DONE if problem == REFUSED else problem
    try:
        done = MfaOperator(service).destroy(ring, actor)
    except KeyCommandError as exc:
        print(f"refused: {exc}; nothing was changed", file=sys.stderr)
        return NOT_DONE
    print(
        f"auth factor-key destroyed key_id={done.key_id}; {done.factors_removed} factors removed"
        + ("; the organization policy is now off" if done.policy_switched_off else "")
    )
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    for user_id in done.users:
        counts[_mfa_notice(service, user_id, actor)] += 1
    print(f"notices: sent {counts['sent']}, failed {counts['failed']}, skipped {counts['skipped']}")
    print(
        "Unset NURAMESH_AUTH_KEY_FILE and NURAMESH_AUTH_KEY_FILE_PREVIOUS: the files are gone "
        "and start-up stops while they name them. People enrol again after `auth factor-key init`."
    )
    return OK


def _mfa_notice(service: Any, user_id: str, actor: str) -> str:
    """Send the "reset" notice after a committed operator action: `sent`, `failed` or `skipped`.
    Never raises and never changes the exit code."""
    from nuramesh.cli_email import mail_service
    from nuramesh.server.mfa_notice import send_now

    try:
        mail = mail_service(service.store, service.org_id)
        return send_now(mail, service.store, service.org_id, user_id, "reset", actor)
    except Exception:
        return "failed"


def cmd_auth_mfa_status(args: argparse.Namespace) -> int:
    """`nuramesh auth mfa status`: the policy and one line per person (id, role, enrolled)."""
    from nuramesh.server.mfa_operator import MfaOperator

    service = _operator_service(args)
    if isinstance(service, str):
        print(service, file=sys.stderr)
        return USAGE
    policy, people = MfaOperator(service).status()
    print(f"policy: mfa {'required' if policy else 'not required'} for owners")
    for person in people:
        print(f"{person.user_id} {person.role} {'enrolled' if person.enrolled else 'not-enrolled'}")
    return OK


def cmd_auth_mfa_reset(args: argparse.Namespace) -> int:
    """`nuramesh auth mfa reset --user human:u_...`: remove one person's MFA and end their
    sessions. Interactive terminal only; needs an operator recovery code,
    which is spent (and ledgered) before the reset, also when the reset then fails."""
    from nuramesh.cli_rules import USER_ID_TEXT
    from nuramesh.core.people import is_human_id
    from nuramesh.server.mfa_operator import MfaOperator
    from nuramesh.server.recovery import operator_actor
    from nuramesh.server.sessions import AuthProblem

    if not is_human_id(str(args.user)):  # the same check, and text, as `--dry-run`
        print(USER_ID_TEXT, file=sys.stderr)
        return USAGE
    service = _operator_gate(args, "mfa-reset")
    if isinstance(service, int):
        return NOT_DONE if service == REFUSED else service
    problem = _ask_recovery_code(args, service)
    if problem is not None:
        return NOT_DONE if problem == REFUSED else problem
    actor = operator_actor()
    problem = _spend_recovery_code(args, service, "mfa-reset", actor)
    if problem is not None:
        return NOT_DONE if problem == REFUSED else problem
    try:
        changed, revoked = MfaOperator(service).reset_user(args.user, actor)
    except AuthProblem as exc:
        print(f"refused: {exc.code} ({exc.detail}); nothing was changed", file=sys.stderr)
        return NOT_DONE
    if not changed:
        print(f"{args.user} has no MFA; nothing was changed")
        return OK
    print(f"MFA removed for {args.user}; {revoked} sessions ended")
    print(f"notice: {_mfa_notice(service, args.user, actor)}")
    return OK


def _token_service(args: argparse.Namespace) -> Any:
    """The team's `TokenService` over the same store `serve` uses, or an error text."""
    import time

    from nuramesh.server.tokens import TokenService

    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        store = cli._open_server_store(root, args.store)
    except (TeamError, ConfigError, ConfigValidationError, ImportError, StoreError) as exc:
        return cli._store_problem(exc) if isinstance(exc, ImportError | StoreError) else str(exc)
    return TokenService(store, config.org.id, store.now, time.monotonic)


CLI_TOKEN_DAYS = 7
"""Default lifetime of a token minted by the CLI (design decision 2026-10-25)."""


def cmd_auth_tokens(args: argparse.Namespace) -> int:
    """`nuramesh auth tokens create|list|revoke|rename|delete|scrub-erased`: API tokens for
    programs, recorded as `service:cli` in the ledger. A new token is shown once, on a terminal
    only (stderr). `rename`, `delete` and `scrub-erased` are #634 (R10, R21)."""
    from datetime import timedelta

    from nuramesh.core.auth import MAX_API_TOKEN_LIFETIME, MAX_CLI_TOKEN_LIFETIME, TokenScope
    from nuramesh.server.sessions import AuthProblem

    if args.tokens_command == "create" and not sys.stderr.isatty():
        print("run this in a terminal: the token is shown only on a terminal", file=sys.stderr)
        return USAGE
    max_days = MAX_CLI_TOKEN_LIFETIME.days
    if args.tokens_command == "create" and args.days is None:
        args.days = CLI_TOKEN_DAYS  # short by default: a minted token is a standing credential
    if args.tokens_command == "create" and not 1 <= args.days <= max_days:
        print(
            f"--days must be a whole number from 1 to {max_days} (the longest a token made by "
            f"the CLI may live; a person signed in can make one for up to "
            f"{MAX_API_TOKEN_LIFETIME.days})",
            file=sys.stderr,
        )
        return USAGE
    service = _token_service(args)
    if isinstance(service, str):
        if args.tokens_command in _TOKEN_EDITS and getattr(args, "team", None):
            service = f"no team {args.team!r}" if "nuramesh.yaml" in service else service
        print(service, file=sys.stderr)
        return USAGE
    if args.tokens_command in _TOKEN_EDITS:
        return _cmd_token_edit(args, service)
    try:
        if args.tokens_command == "create":
            issued = service.create(
                args.name,
                {TokenScope(s) for s in args.scope},
                created_by="service:cli",
                lifetime=timedelta(days=args.days),
            )
            record = issued.record
            print(
                f"API token {record.name} ({record.id}), acting as {record.actor}, scopes "
                f"{', '.join(sorted(s.value for s in record.scopes))}.\n"
                f"Shown once; it cannot be shown again:\n  {issued.token}\n"
                f"It expires at {record.expires_at:%Y-%m-%d %H:%M} UTC. Send it as "
                "`Authorization: Bearer <token>`.",
                file=sys.stderr,
            )
            return OK
        if args.tokens_command == "list" and args.output == "json":
            from dataclasses import asdict

            print(
                json.dumps({"tokens": [asdict(v) for v in service.list()]}, indent=2, default=str)
            )
            return OK
        if args.tokens_command == "list":
            for view in service.list():
                state = "active" if view.active else ("revoked" if view.revoked_at else "expired")
                print(
                    f"{view.id}  {view.name:<20} {','.join(view.scopes):<9} {state:<8} "
                    f"expires {view.expires_at}"
                )
            return OK
        listed = service.list()
        by_id = [v for v in listed if v.id == args.token]
        by_name = [v for v in listed if v.name == args.token]
        if by_id and by_name and by_id[0].id != by_name[0].id:
            print(
                f"{args.token!r} is ambiguous (it is one token's id and another's name); "
                "nothing was revoked",
                file=sys.stderr,
            )
            return USAGE
        found = by_id or by_name
        if not found:
            print(f"no token {args.token!r}", file=sys.stderr)
            return USAGE
        already = found[0].revoked_at is not None
        view = service.revoke(found[0].id, by="service:cli")  # a no-op writes nothing
        verb = "already revoked" if already else "revoked"
        print(f"{verb} {view.name} ({view.id})")
        return OK
    except AuthProblem as exc:
        return _token_problem(exc)


_TOKEN_EDITS = ("rename", "delete", "scrub-erased")


def _token_problem(exc: Any) -> int:
    """A token problem as the CLI says it: a busy store or an organization with no usable key is
    exit 1 `store busy, try again`; everything else keeps exit 2 and its own text."""
    if exc.status == 503:
        print("store busy, try again", file=sys.stderr)
        return NOT_DONE
    print(exc.detail, file=sys.stderr)
    return USAGE


def _cmd_token_edit(args: argparse.Namespace, service: Any) -> int:
    """`rename`, `delete` and `scrub-erased` (#634 R10, R21): the lookup by id or name comes
    before the name rules, an error line holds only the user's own argument, and every change is
    `service:cli`."""
    from nuramesh.server.sessions import AuthProblem

    try:
        if args.tokens_command == "scrub-erased":
            count = service.scrub_erased(by="service:cli")
            print(f"scrubbed {count} token(s)")
            return OK
        listed = service.list()
        ids = {v.id for v in listed if v.id == args.token} | {
            v.id for v in listed if v.name == args.token
        }
        if len(ids) > 1:
            print(f"{args.token!r} matches more than one token, use the id", file=sys.stderr)
            return USAGE
        if not ids:
            print(f"no token {args.token!r}", file=sys.stderr)
            return USAGE
        (token_id,) = ids
        if args.tokens_command == "rename":
            before = next(v for v in listed if v.id == token_id)
            service.rename(token_id, args.new_name, by="service:cli")
            print(("unchanged " if before.name == args.new_name else "renamed ") + token_id)
            return OK
        service.delete(token_id, by="service:cli")
        print(f"deleted {token_id}")
        return OK
    except AuthProblem as exc:
        if exc.code == "token_name_taken":
            print("that name is taken", file=sys.stderr)
            return USAGE
        if exc.code == "token_active":
            print("revoke it first", file=sys.stderr)
            return USAGE
        return _token_problem(exc)


def cmd_agents_credentials(args: argparse.Namespace) -> int:
    """`nuramesh agents credentials list|revoke|revoke-all` (E2).

    **Look and restrict only (owner Q9):** the CLI has no authenticated person, so it never issues,
    rotates or lengthens an agent credential and never shows a secret; those go through the API
    and the app, by an owner in a session. Revoking is `service:cli`, reason `admin`, never
    suspended by a halt or a pause. The store is opened as `auth tokens` open it."""
    from nuramesh.core.auth import team_key
    from nuramesh.server.agent_credentials import AgentCredentialService, agent_directory

    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        store = cli._open_server_store(root, args.store)
    except (TeamError, ConfigError, ConfigValidationError, ImportError, StoreError) as exc:
        print(
            cli._store_problem(exc) if isinstance(exc, ImportError | StoreError) else str(exc),
            file=sys.stderr,
        )
        return USAGE
    directory = agent_directory(config.org)
    service = AgentCredentialService(
        store, config.org.id, team_key(root.name), directory, store.now
    )
    try:
        agent = getattr(args, "agent", None)
        if agent is not None and agent not in directory:
            print(f"no agent {agent!r} in this team", file=sys.stderr)
            return USAGE
        if args.credentials_command == "list":
            for view in service.list_views(agent):
                if view["team"] == service.team:
                    state = (
                        "active"
                        if view["active"]
                        else ("revoked" if view["revoked_at"] else "expired")
                    )
                    erased = " issuer-erased" if view["issuer_erased"] else ""
                    print(
                        f"{view['id']}  {view['agent']:<12} {','.join(view['scopes']):<9} "
                        f"{state:<8} expires {view['expires_at']}{erased}"
                    )
            return OK
        if args.credentials_command == "revoke":
            outcome = service.cli_revoke(args.credential)
            if outcome is None:
                print(f"no credential {args.credential!r}", file=sys.stderr)
                return USAGE
            print(("already revoked " if outcome == "already" else "revoked ") + args.credential)
            return OK
        print(f"revoked {service.cli_revoke_all(args.agent)}")
        return OK
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
