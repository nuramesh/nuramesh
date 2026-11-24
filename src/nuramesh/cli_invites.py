"""The invite commands (R1.37):

    nuramesh auth invites create EMAIL --role approver|viewer [--print-link]
    nuramesh auth invites list [--status S]
    nuramesh auth invites revoke INVITE_ID
    nuramesh auth invites resend INVITE_ID

They run in a shell on the server (store access, no session). Actor `service:cli`; with
`create --print-link` (design decision; `resend` has no such option) the actor is
`operator:<os user>`, the shell must say `NURAMESH_OPERATOR=1`, and the link is printed once,
after the commit, as the last stdout line.
Exit 0 done, 1 refused by a rule, 2 usage or email off."""

import argparse
import os
import sys
from typing import Any

from nuramesh.cli_email import make_sender
from nuramesh.env_bool import EnvBoolError, env_bool
from nuramesh.server.invites import (
    InviteSettings,
    Issued,
    invite_settings_from_env,
    public_url_problem,
)
from nuramesh.server.sessions import AuthProblem

OK, NOT_DONE, USAGE = 0, 1, 2
STATUSES = ("pending", "accepted", "revoked", "expired")


def write_line(line: str) -> None:
    """One stdout line, flushed so a closed stdout fails here (and the invite is revoked)."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _problem_exit(problem: AuthProblem) -> int:
    print(f"refused ({problem.code}): {problem.detail}", file=sys.stderr)
    return USAGE if problem.code == "email_not_configured" else NOT_DONE


def _service(args: argparse.Namespace, settings: InviteSettings, *, with_mail: bool) -> Any:
    from nuramesh.cli_security import _operator_service
    from nuramesh.server.invites import InviteService
    from nuramesh.server.mail import MailConfigError, MailService, load_mail_config

    if not getattr(args, "team", None):
        args.team = os.environ.get("NURAMESH_TEAM_DIR", "")
    if not args.team:
        return "name the team folder: --team or NURAMESH_TEAM_DIR"
    auth = _operator_service(args)
    if isinstance(auth, str):
        return auth
    mail = None
    if with_mail:  # R1.39: the real SMTP sender from the environment; email off -> 503 text
        try:
            config = load_mail_config(os.environ)
        except MailConfigError as exc:
            return f"error: {exc}"
        mail = MailService(config, make_sender(config, auth.now), auth.store, auth.org_id, auth.now)
    return InviteService(auth, mail, settings)


def _prepare(args: argparse.Namespace) -> tuple[Any, str] | int:
    """The service and the actor, or an exit code after saying why. Print mode checks the
    operator switch and the public URL before the store is opened."""
    print_link = bool(getattr(args, "print_link", False))
    try:
        settings = invite_settings_from_env(os.environ)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    actor = "service:cli"
    if print_link:
        try:
            operator = env_bool("NURAMESH_OPERATOR")
        except EnvBoolError as exc:
            print(f"refused: {exc}")
            return NOT_DONE
        if not operator:
            print("refused: operator only")
            return NOT_DONE
        url = os.environ.get("NURAMESH_PUBLIC_URL", "").strip()
        if not url or public_url_problem(url) is not None:
            print("refused: NURAMESH_PUBLIC_URL")
            return USAGE
        from nuramesh.server.recovery import operator_actor

        actor = operator_actor()
    service = _service(args, settings, with_mail=not print_link)
    if isinstance(service, str):
        print(service, file=sys.stderr)
        return USAGE
    return service, actor


def _show(issued: Issued, service: Any, actor: str) -> int:
    """Print the id and expiry; in print mode then the one link line, revoking if it fails."""
    invite = issued.invite
    print(f"invite: {invite.id}")
    print(f"expires: {invite.expires_at:%Y-%m-%dT%H:%M:%SZ}")
    if issued.link is not None:
        try:
            write_line(f"link: {issued.link}")
        except OSError:
            service.revoke_after_failed_print(actor, invite.id)
            print("could not print the link; the invitation was revoked", file=sys.stderr)
            return NOT_DONE
    return OK


def cmd_invite_create(args: argparse.Namespace) -> int:
    prepared = _prepare(args)
    if isinstance(prepared, int):
        return prepared
    service, actor = prepared
    try:
        issued = service.create(actor, args.email, args.role, print_link=args.print_link)
    except AuthProblem as problem:
        return _problem_exit(problem)
    return _show(issued, service, actor)


def cmd_invite_resend(args: argparse.Namespace) -> int:
    prepared = _prepare(args)
    if isinstance(prepared, int):
        return prepared
    service, actor = prepared
    try:
        issued = service.resend(actor, args.invite_id)
    except AuthProblem as problem:
        return _problem_exit(problem)
    return _show(issued, service, actor)


def cmd_invite_revoke(args: argparse.Namespace) -> int:
    prepared = _prepare(args)
    if isinstance(prepared, int):
        return prepared
    service, actor = prepared
    try:
        invite, changed = service.revoke(actor, args.invite_id)
    except AuthProblem as problem:
        return _problem_exit(problem)
    print(f"{'revoked' if changed else 'already revoked'}: {invite.id}")
    return OK


def cmd_invite_list(args: argparse.Namespace) -> int:
    prepared = _prepare(args)
    if isinstance(prepared, int):
        return prepared
    service, _ = prepared
    for row in service.list_invites():
        if args.status is not None and row["status"] != args.status:
            continue
        print(
            f"{row['id']}  {row['status']:<8}  {row['role']:<8}  "
            f"{row['email'] or '-'}  expires {row['expires_at']}"
        )
    return OK


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--team", help="team folder (or NURAMESH_TEAM_DIR)")
    p.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")


def _create_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("email")
    p.add_argument("--role", required=True, help="approver or viewer")
    p.add_argument(
        "--print-link",
        action="store_true",
        help="operator only (NURAMESH_OPERATOR=1): no mail; print the link once",
    )
    _common(p)
    p.set_defaults(func=cmd_invite_create)


def register(auth_sub: Any) -> None:
    """Add `auth invites ...` to the `auth` sub-commands (no alias)."""
    inv = auth_sub.add_parser("invites", help="invite people by email (owner shell)")
    inv_sub = inv.add_subparsers(dest="invite_command", required=True)
    _create_args(inv_sub.add_parser("create", help="create and send an invitation"))
    ls = inv_sub.add_parser("list", help="list invitations (never a link)")
    ls.add_argument("--status", choices=STATUSES)
    _common(ls)
    ls.set_defaults(func=cmd_invite_list)
    rv = inv_sub.add_parser("revoke", help="revoke a pending invitation")
    rv.add_argument("invite_id")
    _common(rv)
    rv.set_defaults(func=cmd_invite_revoke)
    rs = inv_sub.add_parser("resend", help="new link and expiry for a pending invitation")
    rs.add_argument("invite_id")
    _common(rs)
    rs.set_defaults(func=cmd_invite_resend)
