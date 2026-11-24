"""`nuramesh auth password-reset --user human:u_...` (R1.41).
Runs in a shell on the server (no session); actor `service:cli`. The same checks and
mail as the owner route, and it prints `reset email queued` (the ledger records email.sent or
email.failed) or the refusal, never a link.
Exit 0 done, 1 refused by a rule, 2 usage or email off."""

import argparse
import os
import sys
from typing import Any

from nuramesh.cli_email import make_sender
from nuramesh.server.password_reset import CLI_ACTOR, reset_settings_from_env
from nuramesh.server.sessions import AuthProblem

OK, NOT_DONE, USAGE = 0, 1, 2


def cmd_password_reset(args: argparse.Namespace) -> int:
    from nuramesh.cli_security import _operator_service
    from nuramesh.server.mail import MailConfigError, MailService, load_mail_config
    from nuramesh.server.password_reset import ResetService

    try:
        settings = reset_settings_from_env(os.environ)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    if not getattr(args, "team", None):
        args.team = os.environ.get("NURAMESH_TEAM_DIR", "")
    if not args.team:
        print("name the team folder: --team or NURAMESH_TEAM_DIR", file=sys.stderr)
        return USAGE
    auth: Any = _operator_service(args)
    if isinstance(auth, str):
        print(auth, file=sys.stderr)
        return USAGE
    try:
        config = load_mail_config(os.environ)
    except MailConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return USAGE
    mail = MailService(config, make_sender(config, auth.now), auth.store, auth.org_id, auth.now)
    try:
        try:
            ResetService(auth, mail, settings).owner_issue(CLI_ACTOR, args.user, via="cli")
        except AuthProblem as problem:
            print(f"refused ({problem.code}): {problem.detail}", file=sys.stderr)
            return USAGE if problem.code == "email_not_configured" else NOT_DONE
    finally:
        mail.close()  # the mail goes out in the background: wait for it before the process ends
    print("reset email queued (the ledger records email.sent or email.failed)")
    return OK


def register(auth_sub: Any) -> None:
    """Add `auth password-reset` to the `auth` sub-commands."""
    p = auth_sub.add_parser("password-reset", help="start a password reset for a person (shell)")
    p.add_argument("--user", required=True, help="the person's id, human:u_...")
    p.add_argument("--team", help="team folder (or NURAMESH_TEAM_DIR)")
    p.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    p.set_defaults(func=cmd_password_reset)
