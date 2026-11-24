"""Notice mails for MFA changes (R1.38 area 6).

One fixed plain-text notice (`kind=notice`, no link, no code, no user text) for every MFA enrol,
disable and reset. Two shapes:

* `NoticeDesk.slot` for the HTTP routes: a queue slot is reserved BEFORE any write (all or
  nothing; none free is 503 `busy` with nothing changed), the notice is queued after the commit
  and never waits for SMTP. Email off, an erased or address-less person or a non-ASCII address:
  no slot and no notice.
* `send_now` for the operator CLI: sent after the commit, the outcome is `sent`, `failed` or
  `skipped`, and it never changes the exit code.

A mail failure is never raised into the security action and never shows an address or a code.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from nuramesh.core.auth import email_text
from nuramesh.core.identity import MailSendError, address_problem
from nuramesh.server.mail import MailBusy, MailService, Reservation
from nuramesh.server.mail_templates import NoticeWord, mfa_notice
from nuramesh.server.sessions import AuthProblem
from nuramesh.stores import Store

LOG = logging.getLogger("nuramesh.mfa_notice")


def _address(store: Store, org: str, user_id: str) -> str | None:
    """The person's deliverable address, or None (unknown, erased, none stored, non-ASCII)."""
    user = store.get_user(org, user_id)
    if user is None or user.erased or user.email is None:
        return None
    address = email_text(user.email)
    return None if address_problem(address) is not None else address


@dataclass
class NoticeSlot:
    """A reserved queue slot. `send` queues the notice (after the commit); never raises."""

    desk: "NoticeDesk"
    word: NoticeWord
    reservation: Reservation | None
    to: str | None
    sent: bool = False

    def send(self, actor: str) -> None:
        if self.sent or self.reservation is None or self.to is None:
            return
        self.sent = True
        mail = self.desk.mail
        try:
            message = mfa_notice(
                self.to, word=self.word, at=self.desk.clock(), org_name=mail.org_name
            )
            mail.enqueue(self.reservation, message, actor, ledger=True)
        except Exception:
            LOG.error("could not queue an MFA notice")  # the action stands; no address here


class NoticeDesk:
    """Reserves notice slots for the routes of one organization."""

    def __init__(
        self, mail: MailService, store: Store, org: str, clock: Callable[[], datetime]
    ) -> None:
        self.mail, self.store, self.org, self.clock = mail, store, org, clock

    @contextmanager
    def slot(self, user_id: str, word: NoticeWord) -> Iterator[NoticeSlot]:
        """Reserve before any write; leaving the block releases what `send` did not use."""
        if not self.mail.enabled:
            yield NoticeSlot(self, word, None, None)
            return
        try:
            reservation = self.mail.reserve(1)
        except MailBusy:
            raise AuthProblem(503, "busy", "try again shortly") from None
        try:
            to = _address(self.store, self.org, user_id)
            yield NoticeSlot(self, word, reservation, to)
        finally:
            reservation.release()


def send_now(
    mail: MailService | None, store: Store, org: str, user_id: str, word: NoticeWord, actor: str
) -> str:
    """Send one notice at once (the operator CLI). `sent`, `failed` or `skipped`; the fact
    `email.sent` or `email.failed` is written by the mail service; nothing is raised."""
    if mail is None or not mail.enabled:
        return "skipped"
    to = _address(store, org, user_id)
    if to is None:
        return "skipped"
    try:
        message = mfa_notice(to, word=word, at=store.now(), org_name=mail.org_name)
        mail.deliver(message, actor)
    except MailSendError:
        return "failed"
    except Exception:
        LOG.error("could not send an MFA notice")
        return "failed"
    return "sent"


class NoSlot:
    """What the routes use when notices are not wired (no mail service): sends nothing."""

    def send(self, actor: str) -> None:
        return None


@contextmanager
def slot_of(
    desk: NoticeDesk | None, user_id: str, word: NoticeWord
) -> Iterator[NoticeSlot | NoSlot]:
    """`desk.slot(...)`, or a slot that does nothing when there is no desk."""
    if desk is None:
        yield NoSlot()
        return
    with desk.slot(user_id, word) as slot:
        yield slot
