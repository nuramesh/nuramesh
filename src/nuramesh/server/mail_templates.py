"""The fixed mail texts (R1.39; `spec/identity/email.md` section 1). English plain text.

The only variable parts are a link, a role name, an expiry text, the organization name (from
`NURAMESH_ORG_NAME`, never user input), the UTC time of the event and, for a notice, a fixed word.
**No display name and no text a person typed goes into a header, the subject or the body.**
"""

from datetime import UTC, datetime
from typing import Final, Literal

from nuramesh.core.identity import MailKind, OutgoingMail

NoticeWord = Literal["turned on", "turned off", "reset"]
NOTICE_WORDS: Final = ("turned on", "turned off", "reset")
_FOOTER = "If you did not expect this message, you can ignore it."


def _utc(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def invite_mail(to: str, *, link: str, role: str, expires: str, org_name: str) -> OutgoingMail:
    """The invitation to join the organization with `role`."""
    text = (
        f"You have been invited to join {org_name} as {role}.\n\n"
        f"Open this link to accept (it expires {expires}):\n{link}\n\n{_FOOTER}\n"
    )
    return OutgoingMail(to, MailKind.INVITE, f"Your invitation to {org_name}", text)


def reset_mail(to: str, *, link: str, expires: str, org_name: str) -> OutgoingMail:
    """The password reset link."""
    text = (
        f"A password reset was requested for your {org_name} account.\n\n"
        f"Open this link to choose a new password (it expires {expires}):\n{link}\n\n{_FOOTER}\n"
    )
    return OutgoingMail(to, MailKind.RESET, f"Reset your {org_name} password", text)


def verify_mail(to: str, *, link: str, expires: str, org_name: str) -> OutgoingMail:
    """The link that proves an address."""
    text = (
        f"Confirm this address for your {org_name} account.\n\n"
        f"Open this link (it expires {expires}):\n{link}\n\n{_FOOTER}\n"
    )
    return OutgoingMail(to, MailKind.VERIFY, f"Confirm your email address for {org_name}", text)


def notice_mail(to: str, *, what: str, at: datetime, org_name: str) -> OutgoingMail:
    """A notice that something changed on the account. `what` is a fixed sentence the caller
    chooses from its own code (for an MFA notice use `mfa_notice`)."""
    text = f"{what}\n\nWhen: {_utc(at)}\nOrganization: {org_name}\n\n{_FOOTER}\n"
    return OutgoingMail(to, MailKind.NOTICE, f"A change on your {org_name} account", text)


def wait_warning_mail(
    to: str, *, days_left: int, at: datetime, org_name: str, link: str | None
) -> OutgoingMail:
    """A task waits for the reader's decision and will expire (R1.20 PR D, spec 7). Fixed words:
    no task title, worker output, reason or model text; `days_left` is a lead-time number."""
    what = f"A task in {org_name} waits for your decision and will expire in {days_left} days." + (
        f"\n\nOpen the approvals in the app: {link}" if link else ""
    )
    return notice_mail(to, what=what, at=at, org_name=org_name)


def mfa_notice(to: str, *, word: NoticeWord, at: datetime, org_name: str) -> OutgoingMail:
    """Two-step sign-in was `turned on`, `turned off` or `reset`."""
    if word not in NOTICE_WORDS:
        raise ValueError("word must be one of the fixed words")
    return notice_mail(
        to,
        what=(
            f"Two-step sign-in was {word} on your account. "
            "If this was not you, change your password and tell your owner."
        ),
        at=at,
        org_name=org_name,
    )


def check_mail(to: str, *, org_name: str) -> OutgoingMail:
    """The message `nuramesh email check` sends."""
    text = f"This is a test message from {org_name}. Email sending works.\n"
    return OutgoingMail(to, MailKind.TEST, f"Test message from {org_name}", text)
