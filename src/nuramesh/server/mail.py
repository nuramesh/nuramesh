"""Email sending, fail closed (R1.39; `spec/identity/email.md`).

`load_mail_config` reads the SMTP settings (a bad one stops the server, naming the
variable and never the value). `SmtpMailSender` is the real sender (stdlib `smtplib`; certificates
verified; `starttls` that is not offered is an error, never a downgrade); `RecordingMailSender` is
for tests. `MailService` is what routes use: whether mail is on, a bounded background queue with
slot reservation, the failure counter, and the `email.sent` / `email.failed` ledger facts.

Nothing here logs or ledgers a recipient, a subject, a body or a link, and no error text carries
the SMTP server's reply.
"""

import logging
import os
import queue
import secrets
import smtplib
import ssl
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Final

from nuramesh.core.auth import email_text, identifier_hash
from nuramesh.core.identity import (
    DEFAULT_ORG_NAME,
    DEFAULT_PORTS,
    MailErrorCode,
    MailKind,
    MailSecurity,
    MailSender,
    MailSendError,
    MailSettings,
    OutgoingMail,
    address_problem,
    is_loopback_host,
    org_name_problem,
    parse_public_url,
)
from nuramesh.core.ledger import EventType
from nuramesh.env_bool import EnvBoolError, env_bool
from nuramesh.server.secevents import SecurityRecorder
from nuramesh.stores import Store

LOG = logging.getLogger("nuramesh.server")
HOST: Final = "NURAMESH_SMTP_HOST"
FROM: Final = "NURAMESH_SMTP_FROM"
PUBLIC_URL: Final = "NURAMESH_PUBLIC_URL"
PASSWORD_FILE_MAX: Final = 1024
DEFAULT_QUEUE_MAX: Final = 100
DEFAULT_VERIFY_QUEUE_MAX: Final = 100


class MailConfigError(ValueError):
    """A mail setting is unusable. `variable` is the name; the text never holds a value."""

    def __init__(self, variable: str, reason: str) -> None:
        super().__init__(f"{variable}: {reason}")
        self.variable = variable


class MailBusy(Exception):
    """The background queue has no room for the slots asked for (answer 503 `busy`)."""


@dataclass(frozen=True)
class MailConfig:
    """What the environment says. `settings` is None when email is off."""

    settings: MailSettings | None
    missing: tuple[str, ...]
    org_name: str = DEFAULT_ORG_NAME
    queue_max: int = DEFAULT_QUEUE_MAX
    verify_queue_max: int = DEFAULT_VERIFY_QUEUE_MAX

    @property
    def enabled(self) -> bool:
        return self.settings is not None


def _whole(env: Mapping[str, str], name: str, low: int, high: int, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6 or not low <= int(raw) <= high:
        raise MailConfigError(name, f"a whole number from {low} to {high}")
    return int(raw)


def _inside_repository(path: Path) -> bool:
    return any((p / ".git").exists() for p in path.resolve().parents)


def read_secret_file(path: str) -> str:
    """The one line in a secret file. Raises `OSError` for anything unsafe: a symlink, not a
    regular file, not owned by this user, a mode other than 0400 or 0600, over 1 KiB, in a
    repository, not ASCII, or not one line. The message never holds the path's content."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)  # a symlink fails here (ELOOP)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
            or info.st_size > PASSWORD_FILE_MAX
            or _inside_repository(Path(path))
        ):
            raise OSError("unsafe secret file")
        data = os.read(fd, PASSWORD_FILE_MAX + 1)
    finally:
        os.close(fd)
    if len(data) > PASSWORD_FILE_MAX:
        raise OSError("unsafe secret file")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OSError("unsafe secret file") from None
    text = text.removesuffix("\n")
    if not text.isascii():  # smtplib encodes the login as ASCII and its error would quote it
        raise OSError("unsafe secret file")
    if not text or "\n" in text or "\r" in text or "\x00" in text:
        raise OSError("unsafe secret file")
    return text


def load_mail_config(env: Mapping[str, str]) -> MailConfig:
    """Read and check the mail settings. Raises `MailConfigError` (naming the variable). With
    `NURAMESH_SMTP_HOST` unset email is off and only the organization name is checked."""
    org_name = env.get("NURAMESH_ORG_NAME")
    if org_name is None:
        org_name = DEFAULT_ORG_NAME
    elif org_name_problem(org_name):
        raise MailConfigError("NURAMESH_ORG_NAME", "1 to 80 characters, no control characters")
    host = env.get(HOST)
    if host is None or host == "":
        missing = tuple(n for n in (HOST, FROM, PUBLIC_URL) if not env.get(n))
        return MailConfig(None, missing, org_name)
    if not host.isascii() or not 0 < len(host) <= 253 or any(c in host for c in " \t\r\n\x00/@"):
        raise MailConfigError(HOST, "not a host name")
    try:
        security = MailSecurity(env.get("NURAMESH_SMTP_SECURITY", "starttls"))
    except ValueError:
        raise MailConfigError("NURAMESH_SMTP_SECURITY", "starttls, tls or none") from None
    sender = env.get(FROM)
    if not sender:
        raise MailConfigError(FROM, "required when the host is set")
    sender = sender.strip()
    if address_problem(sender):
        raise MailConfigError(FROM, "not a plain address")
    url_raw = env.get(PUBLIC_URL)
    if not url_raw:
        raise MailConfigError(PUBLIC_URL, "required when the host is set")
    try:
        public_url = parse_public_url(url_raw)
    except ValueError as exc:
        raise MailConfigError(PUBLIC_URL, str(exc)) from None
    try:
        allow_insecure = env_bool("NURAMESH_SMTP_ALLOW_INSECURE", env)
    except EnvBoolError as exc:
        raise MailConfigError(
            "NURAMESH_SMTP_ALLOW_INSECURE", str(exc).split(" must ", 1)[1]
        ) from None
    port = _whole(env, "NURAMESH_SMTP_PORT", 1, 65535, DEFAULT_PORTS[security])
    user = env.get("NURAMESH_SMTP_USER") or None
    password_file = env.get("NURAMESH_SMTP_PASSWORD_FILE") or None
    if user is not None and password_file is None:
        raise MailConfigError(
            "NURAMESH_SMTP_PASSWORD_FILE", "required when NURAMESH_SMTP_USER is set"
        )
    if password_file is not None and user is None:
        raise MailConfigError(
            "NURAMESH_SMTP_PASSWORD_FILE", "needs NURAMESH_SMTP_USER (set both or neither)"
        )
    if security is MailSecurity.NONE and not is_loopback_host(host):
        if user is not None:
            raise MailConfigError(
                "NURAMESH_SMTP_SECURITY", "none would send the password in clear to this host"
            )
        if not allow_insecure:
            raise MailConfigError(
                "NURAMESH_SMTP_SECURITY", "none is allowed for a loopback host only"
            )
    if password_file is not None:
        try:
            read_secret_file(password_file)
        except OSError:
            raise MailConfigError(
                "NURAMESH_SMTP_PASSWORD_FILE", "not a safe password file (see the mail docs)"
            ) from None
    timeout = _whole(env, "NURAMESH_SMTP_TIMEOUT_SECONDS", 1, 60, 10)
    queue_max = _whole(env, "NURAMESH_MAIL_QUEUE_MAX", 2, 1000, DEFAULT_QUEUE_MAX)
    verify_queue_max = _whole(
        env, "NURAMESH_VERIFY_MAIL_QUEUE_MAX", 8, 1000, DEFAULT_VERIFY_QUEUE_MAX
    )
    settings = MailSettings(
        host=host,
        port=port,
        security=security,
        sender=sender,
        public_url=public_url,
        user=user,
        password_file=password_file,
        timeout=float(timeout),
    )
    return MailConfig(settings, (), org_name, queue_max, verify_queue_max)


class SmtpMailSender:
    """The real sender. One connection per message; the password is read at send time."""

    def __init__(self, settings: MailSettings, clock: Callable[[], datetime]) -> None:
        self.settings = settings
        self._clock = clock  # the store's clock: the only time source (the Date header)

    def _message(self, mail: OutgoingMail) -> EmailMessage:
        s = self.settings
        if (
            address_problem(mail.to)
            or address_problem(s.sender)
            or any(c in mail.subject for c in "\r\n\x00")
        ):
            raise MailSendError(MailErrorCode.INVALID_MESSAGE)
        try:
            msg = EmailMessage()
            msg["From"] = s.sender
            msg["To"] = mail.to
            msg["Subject"] = mail.subject
            msg["Date"] = format_datetime(self._clock())
            msg["Message-ID"] = f"<{secrets.token_hex(16)}@{s.sender.rpartition('@')[2]}>"
            msg["Auto-Submitted"] = "auto-generated"
            msg.set_content(mail.text, charset="utf-8")
        except (ValueError, LookupError):  # e.g. U+2028 in a header; never the text of the error
            raise MailSendError(MailErrorCode.INVALID_MESSAGE) from None
        return msg

    def send(self, mail: OutgoingMail) -> None:
        """Deliver `mail` or raise `MailSendError` (no retry, no downgrade to plain text). The
        error is raised afresh outside any handler, so it carries no cause and no context (the
        server's reply or an encode error that quotes a setting never hangs off it)."""
        try:
            self._attempt(mail)
        except MailSendError as exc:
            code = exc.code
        else:
            return
        raise MailSendError(code) from None

    def _attempt(self, mail: OutgoingMail) -> None:
        msg = self._message(mail)  # refused before any connection
        s = self.settings
        password = None
        if s.user is not None:
            try:
                password = read_secret_file(s.password_file or "")
            except OSError:
                raise MailSendError(MailErrorCode.AUTH) from None
        context = ssl.create_default_context()
        client: smtplib.SMTP | None = None
        try:
            try:
                if s.security is MailSecurity.TLS:
                    client = smtplib.SMTP_SSL(s.host, s.port, timeout=s.timeout, context=context)
                else:
                    client = smtplib.SMTP(s.host, s.port, timeout=s.timeout)
                client.ehlo()
                if s.security is MailSecurity.STARTTLS:
                    if not client.has_extn("starttls"):
                        raise MailSendError(MailErrorCode.TLS)
                    try:
                        client.starttls(context=context)
                    except smtplib.SMTPResponseException:  # e.g. 454 TLS not available now
                        raise MailSendError(MailErrorCode.TLS) from None
                    client.ehlo()
                if s.user is not None and password is not None:
                    try:
                        client.login(s.user, password)
                    except UnicodeError:  # its text would quote the password
                        raise MailSendError(MailErrorCode.AUTH) from None
                client.send_message(msg)
            finally:
                if client is not None:
                    try:
                        client.quit()
                    except (smtplib.SMTPException, OSError):
                        client.close()
        except MailSendError:
            raise
        except ssl.SSLError:  # before ValueError: SSLCertVerificationError is both
            raise MailSendError(MailErrorCode.TLS) from None
        except ValueError:  # UnicodeError is one; the text of the original may quote a setting
            raise MailSendError(MailErrorCode.INVALID_MESSAGE) from None
        except TimeoutError:
            raise MailSendError(MailErrorCode.TIMEOUT) from None
        except smtplib.SMTPAuthenticationError:
            raise MailSendError(MailErrorCode.AUTH) from None
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
            smtplib.SMTPNotSupportedError,
            smtplib.SMTPHeloError,
        ):
            raise MailSendError(MailErrorCode.REJECTED) from None
        except (smtplib.SMTPException, OSError) as exc:
            # smtplib turns a read timeout into "server disconnected": look at what caused it
            if isinstance(exc.__context__, TimeoutError):
                raise MailSendError(MailErrorCode.TIMEOUT) from None
            raise MailSendError(MailErrorCode.CONNECT) from None


class RecordingMailSender:
    """Tests only: keeps the messages in memory and fails with a given code when told to."""

    def __init__(self) -> None:
        self.sent: list[OutgoingMail] = []
        self.fail_with: MailErrorCode | None = None

    def send(self, mail: OutgoingMail) -> None:
        if address_problem(mail.to):
            raise MailSendError(MailErrorCode.INVALID_MESSAGE)
        if self.fail_with is not None:
            raise MailSendError(self.fail_with)
        self.sent.append(mail)


@dataclass
class _Job:
    mail: OutgoingMail
    actor: str
    ledger: bool


@dataclass
class MailService:
    """What routes use. `deliver` sends at once (the caller learns the outcome); `reserve` and
    `enqueue` send in the background (the caller learns nothing about the address)."""

    config: MailConfig
    sender: MailSender | None
    store: Store | None = None
    org_id: str | None = None
    clock: Callable[[], datetime] | None = None
    background_failed: int = 0
    _occupied: int = field(default=0, init=False, repr=False)
    _verify_occupied: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _queue: "queue.Queue[_Job | None]" = field(default_factory=queue.Queue, init=False, repr=False)
    _worker: threading.Thread | None = field(default=None, init=False, repr=False)
    _verify_queue: "queue.Queue[_Job | None]" = field(
        default_factory=queue.Queue, init=False, repr=False
    )
    _verify_worker: threading.Thread | None = field(default=None, init=False, repr=False)

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.sender is not None

    @property
    def missing(self) -> list[str]:
        return list(self.config.missing)

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return self._occupied

    @property
    def verify_queue_depth(self) -> int:
        with self._lock:
            return self._verify_occupied

    @property
    def org_name(self) -> str:
        return self.config.org_name

    @property
    def public_url(self) -> str | None:
        return None if self.config.settings is None else self.config.settings.public_url

    def status(self) -> dict[str, object]:
        """Names and counts only: never a value, the host, the sender or a recipient."""
        return {
            "enabled": self.enabled,
            "missing": self.missing,
            "queue_depth": self.queue_depth,
            "verify_queue_depth": self.verify_queue_depth,
            "background_failed": self.background_failed,
        }

    def deliver(
        self,
        mail: OutgoingMail,
        actor: str,
        *,
        ledger: bool = True,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        """Send now. Raises `MailSendError` (also `not_configured` when email is off). The fact
        `email.sent` or `email.failed` is written after the send, outside any other transaction.
        `extra` adds ids (never text) to the fact's payload, such as the wait warning's key."""
        if self.sender is None:
            raise MailSendError(MailErrorCode.NOT_CONFIGURED)
        try:
            self.sender.send(mail)
        except MailSendError as exc:
            if ledger:
                self.record(mail, actor, exc.code, extra)
            raise
        if ledger:
            self.record(mail, actor, None, extra)

    def reserve(self, slots: int, verify_slots: int = 0) -> "Reservation":
        """Take `slots` of the main queue and `verify_slots` of the verification queue before
        anything is written; all or nothing under one lock (main checked first). Raises
        `MailBusy` when either has too little room (the answer is the same for every address)."""
        with self._lock:
            if self.config.queue_max - self._occupied < slots:
                raise MailBusy
            if self.config.verify_queue_max - self._verify_occupied < verify_slots:
                raise MailBusy
            self._occupied += slots
            self._verify_occupied += verify_slots
        return Reservation(self, slots, verify_slots)

    def enqueue(
        self, reservation: "Reservation", mail: OutgoingMail, actor: str, *, ledger: bool
    ) -> None:
        """Use one reserved slot to send `mail` in the background: a `verify` mail on the
        verification queue (its own worker), every other kind on the main queue."""
        verify = mail.kind is MailKind.VERIFY
        reservation.take(verify=verify)
        self._start(verify)
        (self._verify_queue if verify else self._queue).put(_Job(mail, actor, ledger))

    def _start(self, verify: bool = False) -> None:
        with self._lock:
            if verify:
                if self._verify_worker is None:
                    self._verify_worker = threading.Thread(
                        target=self._run, args=(True,), name="mail-verify", daemon=True
                    )
                    self._verify_worker.start()
            elif self._worker is None:
                self._worker = threading.Thread(target=self._run, name="mail", daemon=True)
                self._worker.start()

    def _run(self, verify: bool = False) -> None:
        jobs = self._verify_queue if verify else self._queue
        while (job := jobs.get()) is not None:
            try:
                self._send_background(job)
            finally:
                with self._lock:
                    if verify:
                        self._verify_occupied -= 1
                    else:
                        self._occupied -= 1

    def _send_background(self, job: _Job) -> None:
        code: MailErrorCode | None = None
        try:
            if self.sender is None:
                raise MailSendError(MailErrorCode.NOT_CONFIGURED)
            self.sender.send(job.mail)
        except MailSendError as exc:
            code = exc.code
        except Exception as exc:
            code = MailErrorCode.CONNECT
            LOG.error("background mail raised %s", type(exc).__name__)  # the type only
        if code is not None:
            with self._lock:
                self.background_failed += 1
            LOG.error("background mail failed code=%s", code.value)
        if job.ledger:
            self.record(job.mail, job.actor, code)

    def close(self) -> None:
        """Stop the worker after the queued mails."""
        for worker, jobs in (
            (self._worker, self._queue),
            (self._verify_worker, self._verify_queue),
        ):
            if worker is not None:
                jobs.put(None)
                worker.join(timeout=5)

    def record(
        self,
        mail: OutgoingMail,
        actor: str,
        code: MailErrorCode | None,
        extra: Mapping[str, object] | None = None,
    ) -> bool:
        """`email.sent` (code None) or `email.failed`, with the recipient as a keyed hash only; the
        address goes to the security-log row. A failure to write it is logged, never raised: the
        mail has already been tried. Returns False when the fact could not be written."""
        if self.store is None or self.org_id is None or self.clock is None:
            return True
        try:
            address = email_text(mail.to)
            rec = SecurityRecorder(self.org_id, self.clock)
            with self.store.transaction() as uow:
                user = uow.get_user_by_email(self.org_id, address)
                subject = None if user is None or user.erased else user.id
                payload: dict[str, object] = {
                    "kind": mail.kind.value,
                    "recipient_hash": identifier_hash(rec.key(uow, subject), self.org_id, address),
                }
                if code is not None:
                    payload["code"] = code.value
                if extra:
                    payload.update(extra)
                rec.record(
                    uow,
                    EventType.EMAIL_SENT if code is None else EventType.EMAIL_FAILED,
                    actor,
                    payload,
                    subject=subject,
                    details={"email": address},
                )
        except Exception:
            LOG.error("could not record the email fact (the send stands)")
            return False
        return True


class Reservation:
    """Slots taken from the queues. Release what is not used (a decoy, a rollback)."""

    def __init__(self, service: MailService, slots: int, verify_slots: int = 0) -> None:
        self._service, self._slots, self._verify_slots = service, slots, verify_slots

    def take(self, *, verify: bool = False) -> None:
        if verify:
            if self._verify_slots < 1:
                raise RuntimeError("no reserved verification slot left")
            self._verify_slots -= 1
            return
        if self._slots < 1:
            raise RuntimeError("no reserved slot left")
        self._slots -= 1

    def release_verify(self) -> None:
        """Give back the unused verification slots now (a decoy start)."""
        with self._service._lock:
            self._service._verify_occupied -= self._verify_slots
        self._verify_slots = 0

    def release(self) -> None:
        """Give back every slot not used; safe to call more than once."""
        with self._service._lock:
            self._service._occupied -= self._slots
            self._service._verify_occupied -= self._verify_slots
        self._slots = self._verify_slots = 0

    def __enter__(self) -> "Reservation":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
