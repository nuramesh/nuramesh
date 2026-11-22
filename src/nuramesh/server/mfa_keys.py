"""The auth key and TOTP secret sealing (R1.38, spec/identity/mfa.md sections 2 and 2a).

The key lives only in a file (`NURAMESH_AUTH_KEY_FILE`), is read at each use and is never logged.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import hmac
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nuramesh.core.content import hkdf

SEAL_INFO = "nuramesh mfa secret v1"
KEY_ID_INFO = b"nuramesh auth key id"
MAX_KEY_FILE_BYTES = 1024
CURRENT_VAR = "NURAMESH_AUTH_KEY_FILE"
PREVIOUS_VAR = "NURAMESH_AUTH_KEY_FILE_PREVIOUS"


class MfaUnavailable(Exception):
    """No usable key, or a secret that does not open: TOTP cannot be verified (fail closed)."""


class AuthKeyFileError(Exception):
    """The key file is invalid. The message names the variable or path, never the contents."""


def key_id(key: bytes) -> str:
    """16 hex characters naming a key without exposing it."""
    return hmac.new(key, KEY_ID_INFO, hashlib.sha256).hexdigest()[:16]


def _aad(org: str, user: str, factor: str) -> bytes:
    out = b""
    for part in (org, user, factor):
        raw = part.encode()
        out += len(raw).to_bytes(8, "big") + raw
    return out


def _aead(key: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - optional extra
        raise MfaUnavailable("the server extra is not installed") from exc
    return AESGCM(hkdf(key, SEAL_INFO))


def seal_secret(key: bytes, secret: bytes, org: str, user: str, factor: str) -> bytes:
    """nonce(12) || ciphertext || tag(16), bound to the row by the AAD."""
    nonce = secrets.token_bytes(12)
    return bytes(nonce + _aead(key).encrypt(nonce, secret, _aad(org, user, factor)))


def open_secret(key: bytes, sealed: bytes, org: str, user: str, factor: str) -> bytes:
    """Open a sealed secret; any failure is `MfaUnavailable`."""
    if len(sealed) < 12 + 16:
        raise MfaUnavailable("sealed secret is malformed")
    try:
        return bytes(_aead(key).decrypt(sealed[:12], sealed[12:], _aad(org, user, factor)))
    except MfaUnavailable:
        raise
    except Exception as exc:
        raise MfaUnavailable("the secret does not open") from exc


def load_key_file(path: Path | str, *, var: str, check_owner: bool = True) -> bytes:
    """Read and validate a key file (regular, ours, 0400/0600, <= 1 KiB, base64 of 32 bytes)."""
    # One open, no following of a symlink, no blocking on a FIFO; every check is on the opened
    # descriptor, so the file checked is the file read (no swap between check and read).
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AuthKeyFileError(f"{var}: must name a regular file (not a symlink)") from exc
        raise AuthKeyFileError(f"{var}: the file cannot be read") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AuthKeyFileError(f"{var}: must name a regular file (not a symlink)")
        if stat.S_IMODE(st.st_mode) not in (0o400, 0o600):
            raise AuthKeyFileError(f"{var}: the file mode must be 0400 or 0600")
        if check_owner and st.st_uid != os.getuid():
            raise AuthKeyFileError(f"{var}: the file must be owned by the server's user")
        if st.st_size > MAX_KEY_FILE_BYTES:
            raise AuthKeyFileError(f"{var}: the file is larger than 1 KiB")
        try:
            raw = os.read(fd, MAX_KEY_FILE_BYTES + 1).strip()
            key = base64.b64decode(raw, validate=True)
        except (OSError, binascii.Error, ValueError) as exc:
            raise AuthKeyFileError(f"{var}: the file is not one base64 line") from exc
    finally:
        os.close(fd)
    if len(key) != 32 or b"\n" in raw:
        raise AuthKeyFileError(f"{var}: the key must decode to exactly 32 bytes")
    return key


def _in_git_work_tree(directory: Path) -> bool:
    return any((d / ".git").exists() for d in (directory, *directory.parents))


def init_key_file(path: Path | str) -> str:
    """Create a new key file exclusively (0600). Returns its key id. Never prints the key."""
    p = Path(path)
    if p.is_symlink() or p.exists():
        raise AuthKeyFileError("refused: the path exists or is a symlink")
    parent = p.resolve().parent
    try:
        pst = parent.stat()
    except OSError as exc:
        raise AuthKeyFileError("refused: the directory cannot be read") from exc
    if pst.st_mode & stat.S_IWOTH:
        raise AuthKeyFileError("refused: the directory is world-writable")
    if _in_git_work_tree(parent):
        raise AuthKeyFileError("refused: the path is inside a Git work tree")
    key = secrets.token_bytes(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64encode(key) + b"\n")
    return key_id(key)


@dataclass(frozen=True)
class KeyRing:
    """Current and previous auth key files; read at each use, never cached."""

    current: Path | None
    previous: Path | None = None
    check_owner: bool = True

    def _load(self, path: Path | None, var: str) -> bytes | None:
        if path is None:
            return None
        try:
            return load_key_file(path, var=var, check_owner=self.check_owner)
        except AuthKeyFileError:
            return None

    def available(self) -> bool:
        """True when a current key is usable."""
        return self._load(self.current, CURRENT_VAR) is not None

    def seal_key(self) -> bytes:
        """The current key (the only one that seals)."""
        key = self._load(self.current, CURRENT_VAR)
        if key is None:
            raise MfaUnavailable("no auth key")
        return key

    def previous_key(self) -> bytes | None:
        """The previous key (opens only), or None if unset or unreadable."""
        return self._load(self.previous, PREVIOUS_VAR)

    def key_for(self, wanted_id: str) -> bytes | None:
        """The current or previous key whose id matches, else None."""
        for path, var in ((self.current, CURRENT_VAR), (self.previous, PREVIOUS_VAR)):
            key = self._load(path, var)
            if key is not None and hmac.compare_digest(key_id(key), wanted_id):
                return key
        return None


def key_ring_from_env(env: Mapping[str, str], *, strict: bool = True) -> KeyRing | None:
    """The `KeyRing` named by `NURAMESH_AUTH_KEY_FILE` and `NURAMESH_AUTH_KEY_FILE_PREVIOUS`.

    None when the current variable is unset (a fresh install: MFA enrolment is off). With
    `strict` (server start-up) every variable that is set must name a valid file, else
    `AuthKeyFileError` naming the variable, never the contents. Operator commands pass
    `strict=False`: they run in the operator's shell, whose user need not own the files.
    """
    current = env.get(CURRENT_VAR, "").strip()
    previous = env.get(PREVIOUS_VAR, "").strip()
    if not current:
        if previous and strict:
            raise AuthKeyFileError(f"{PREVIOUS_VAR} is set but {CURRENT_VAR} is not")
        return None
    ring = KeyRing(Path(current), Path(previous) if previous else None, check_owner=strict)
    if strict:
        load_key_file(current, var=CURRENT_VAR)
        if previous:
            load_key_file(previous, var=PREVIOUS_VAR)
    return ring


def destroy_key_files(*paths: Path | None) -> int:
    """Overwrite each existing key file with zero bytes, then unlink it (best effort; the file
    system may keep copies). Returns the files removed."""
    removed = 0
    for path in paths:
        if path is None:
            continue
        try:
            size = path.lstat().st_size
            if path.is_file() and not path.is_symlink():
                with path.open("r+b") as handle:
                    handle.write(b"\0" * size)
                    handle.flush()
                    os.fsync(handle.fileno())
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
