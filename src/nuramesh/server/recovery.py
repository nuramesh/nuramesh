"""The operator recovery code (T128): the second key to the commands that erase an owner or close
an organization (`nuramesh auth erase-owner`, `nuramesh auth close-org`).

It follows the first-owner setup token (T66): a 256-bit random secret, shown once on a terminal,
only its SHA-256 stored, single use, short life. Unlike the setup token it lives in a **file in the
team's state folder**, not in the database, so only someone with the server's OS user (the file is
mode 0600 and owned by that user; a looser mode, another owner or a symbolic link is refused) can
make or use one. Issued with `nuramesh auth recovery-code issue`, typed at a hidden prompt, never in
argv or the environment.

Single use: `claim_recovery_code` renames the file away (atomic, so of two racing commands only one
wins) just before the action's own transaction. If that action is then refused, the code stays
spent and a new one must be issued (a code must never serve two actions).
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from nuramesh.paths import state_path

RECOVERY_LIFETIME = timedelta(hours=1)
MAX_RECOVERY_CODE_LENGTH = 256
FILE_NAME = "operator-recovery-code.json"
_ACTOR_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class RecoveryCodeError(Exception):
    """Why a recovery code cannot be used. The text is safe to print (never the code)."""


@dataclass(frozen=True)
class IssuedRecoveryCode:
    """The secret to show once and when it stops working."""

    code: str = field(repr=False)
    expires_at: datetime


def recovery_code_path(root: Path) -> Path:
    """Where a team folder keeps its open recovery code."""
    return state_path(root, FILE_NAME)


def hash_code(code: str) -> str:
    """SHA-256 hex of the code (256 random bits, so a slow hash would add nothing)."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def os_user_name() -> str:
    """The OS user of this process, from its uid (not from `$USER`, which anyone can set)."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):  # pragma: no cover - not POSIX, or a uid without an entry
        import getpass

        return getpass.getuser()


def operator_actor() -> str:
    """The ledger actor of an operator command: `operator:<os user>`, cleaned to the actor
    character set (other characters become `_`, at most 64 long, `unknown` if nothing is left)."""
    name = _ACTOR_CHARS.sub("_", os_user_name())[:64]
    return f"operator:{name or 'unknown'}"


def issue_recovery_code(root: Path, now: datetime) -> IssuedRecoveryCode:
    """Make a new code, replacing any open one. The file is created private (0600) and swapped in
    whole, so it is never readable by others, not even for a moment. `now` is the store's time
    (`store.now()`), the same clock the ledger's issue event uses."""
    path = recovery_code_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid == os.getuid():  # the store may have made it first, wider
        os.chmod(path.parent, 0o700)
    code = secrets.token_urlsafe(32)
    expires = now + RECOVERY_LIFETIME
    body = json.dumps(
        {
            "hash": hash_code(code),
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "issued_by": operator_actor(),
        }
    )
    tmp = path.with_name(f".{FILE_NAME}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return IssuedRecoveryCode(code, expires)


def inspect_recovery_file(root: Path) -> None:
    """Refuse unless a recovery code file exists and only this OS user can read it. Run before
    asking for the code, so the operator learns about a bad file without typing."""
    path = recovery_code_path(root)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise RecoveryCodeError(
            # old-name-ok: STALE #611 old positional form shown to the operator
            "no recovery code is open: run `nuramesh auth recovery-code issue <team>` on this "
            "server first"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise RecoveryCodeError("the recovery code file is not a regular file; refusing it")
    if info.st_uid != os.getuid():
        raise RecoveryCodeError("the recovery code file belongs to another user; refusing it")
    if info.st_mode & 0o077:
        raise RecoveryCodeError(
            f"the recovery code file is readable by others (mode {info.st_mode & 0o777:04o}); "
            f"make it 0600 or issue a new code"
        )


def _read(root: Path) -> tuple[str, datetime]:
    inspect_recovery_file(root)
    try:
        fd = os.open(recovery_code_path(root), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as handle:
            data = json.load(handle)
        digest, expires = data["hash"], datetime.fromisoformat(data["expires_at"])
    except FileNotFoundError:
        raise RecoveryCodeError("no recovery code is open") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise RecoveryCodeError("the recovery code file is damaged; issue a new code") from None
    if not isinstance(digest, str) or expires.tzinfo is None:
        raise RecoveryCodeError("the recovery code file is damaged; issue a new code")
    return digest, expires


def verify_recovery_code(root: Path, code: str, now: datetime) -> None:
    """Raise `RecoveryCodeError` unless `code` is the open, unexpired code. Spends nothing."""
    digest, expires = _read(root)
    generic = RecoveryCodeError("the recovery code can't be used (wrong or expired)")
    if len(code) > MAX_RECOVERY_CODE_LENGTH:
        raise generic
    try:
        given = hash_code(code)
    except UnicodeEncodeError:
        raise generic from None
    if not hmac.compare_digest(given, digest) or now >= expires:
        raise generic


def claim_recovery_code(root: Path, code: str, now: datetime) -> None:
    """Verify the code and spend it: the file is renamed away and deleted. Only one of two
    commands racing on the same code gets here; the other is refused."""
    verify_recovery_code(root, code, now)
    path = recovery_code_path(root)
    taken = path.with_name(f".{FILE_NAME}.{secrets.token_hex(8)}.used")
    try:
        os.rename(path, taken)
    except FileNotFoundError:
        raise RecoveryCodeError("the recovery code was just used by another command") from None
    taken.unlink(missing_ok=True)
