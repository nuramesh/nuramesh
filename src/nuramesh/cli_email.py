"""`nuramesh email status | check --to ADDRESS` (R1.39; `spec/identity/email.md` section 6).

Output never holds the SMTP server's reply, a setting value, or a link: only `on`/`off`, variable
names, `sent` and a closed failure code."""

import argparse
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from nuramesh.cli_rules import add_team
from nuramesh.core.identity import MailSender, address_problem
from nuramesh.server.mail import (
    MailConfig,
    MailConfigError,
    MailService,
    SmtpMailSender,
    load_mail_config,
)
from nuramesh.server.mail_templates import check_mail
from nuramesh.team import load_team_config

OK, OFF, USAGE = 0, 1, 2


def make_sender(config: MailConfig, clock: Callable[[], datetime]) -> MailSender | None:
    """The real sender for `config` (tests replace this)."""
    return None if config.settings is None else SmtpMailSender(config.settings, clock)


def mail_service(store: Any, org: str) -> MailService | None:
    """A `MailService` for CLI notices over `store`, or None when email is off or its settings
    are invalid (the caller prints `skipped`; a notice never changes an exit code)."""
    try:
        config = load_mail_config(os.environ)
    except MailConfigError:
        return None
    if not config.enabled:
        return None
    return MailService(config, make_sender(config, store.now), store, org, store.now)


def _config() -> MailConfig | None:
    try:
        return load_mail_config(os.environ)
    except MailConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def cmd_status(args: argparse.Namespace) -> int:
    """`email: on`, or `email: off` and the variable names that are missing."""
    config = _config()
    if config is None:
        return USAGE
    if config.enabled:
        print("email: on")
        return OK
    print("email: off")
    for name in config.missing:
        print(f"missing: {name}")
    return OFF


def cmd_check(args: argparse.Namespace, store_factory: Callable[[Path, str | None], Any]) -> int:
    """Send the fixed test message; print `sent` or `failed: <code>`."""
    from nuramesh.core.identity import MailSendError

    config = _config()
    if config is None:
        return USAGE
    if not config.enabled:
        print("email is off: run `nuramesh email status`", file=sys.stderr)
        return USAGE
    if address_problem(args.to):
        print("error: --to must be a plain ASCII address", file=sys.stderr)
        return USAGE
    root = Path(args.team).resolve()
    org = load_team_config(root).org.id
    store = store_factory(root, args.store)
    sender = make_sender(config, store.now)
    service = MailService(config, sender, store, org, store.now)
    mail = check_mail(args.to, org_name=config.org_name)
    code = None
    try:
        service.deliver(mail, "service:cli", ledger=False)
    except MailSendError as exc:
        code = exc.code
    if not service.record(mail, "service:cli", code):
        # no event was written: never claim a result the ledger does not hold
        print("error: the result could not be recorded in the ledger", file=sys.stderr)
        return USAGE
    if code is not None:
        print(f"failed: {code.value}")
        return OFF
    print("sent")
    return OK


def add_parsers(sub: Any, store_factory: Callable[[Path, str | None], Any]) -> None:
    """Register `email status` and `email check`."""
    email = sub.add_parser("email", help="outgoing email (SMTP): status and a test message")
    esub = email.add_subparsers(dest="email_command", required=True)
    status = esub.add_parser("status", help="on or off, and the settings that are missing")
    status.set_defaults(func=cmd_status)
    check = esub.add_parser("check", help="send a fixed test message to ADDRESS")
    check.add_argument("--to", required=True, metavar="ADDRESS")
    add_team(check)
    check.add_argument("--store")
    check.set_defaults(func=lambda a: cmd_check(a, store_factory))
