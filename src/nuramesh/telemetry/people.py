"""Actors in an export: kind always, id for agents and services, a keyed pseudonym for people
only on opt-in (ND117, spec section 6.3). The raw id of a person is never produced here."""

import base64
import hashlib
import hmac
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

KEY_BYTES = 32
_KINDS = ("system", "agent", "service", "human", "operator")
_PERSON_KINDS = ("human", "operator")
_TOKEN_PREFIX = "tok_"
# Domain separation for the pseudonym HMAC; a public constant, not a secret. Changing it changes
# every pseudonym.
_PERSON_DOMAIN = "nuramesh/otel/person/"


class PseudonymKeyError(Exception):
    """The pseudonym key file is unusable (the exporter then stays off, code `bad_setting`).
    The message never holds the key."""


@dataclass(frozen=True)
class ActorView:
    """What an export may say about one ledger actor."""

    kind: str
    id: str | None = None
    pseudonym: str | None = None
    pseudonym_key_id: str | None = None


def actor_kind(actor: str) -> str:
    """The actor's prefix when it is one of the known kinds, else `other`."""
    prefix = actor.split(":", 1)[0]
    return prefix if prefix in _KINDS else "other"


def key_id(key: bytes) -> str:
    """First 8 hex characters of SHA-256 of the domain tag and the key."""
    return hashlib.sha256(b"nuramesh/otel/pseudonym-key-id/" + key).hexdigest()[:8]


def pseudonym(key: bytes, actor: str) -> str:
    """First 16 hex characters of the keyed HMAC-SHA256 of the person domain label plus `actor`."""
    message = (_PERSON_DOMAIN + actor).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16]


def describe_actor(actor: str, key: bytes | None) -> ActorView:
    """The export view of `actor`. People get kind only, or kind and a pseudonym when `key` is
    given. A token actor (`service:tok_...`) is a service with no id. The id of an agent or
    service is returned unguarded: the caller passes it through the value guard."""
    kind = actor_kind(actor)
    if kind in _PERSON_KINDS:
        if key is None:
            return ActorView(kind)
        return ActorView(kind, None, pseudonym(key, actor), key_id(key))
    if kind in ("agent", "service"):
        ident = actor.split(":", 1)[1] if ":" in actor else ""
        if not ident or (kind == "service" and ident.startswith(_TOKEN_PREFIX)):
            return ActorView(kind)
        return ActorView(kind, ident)
    return ActorView(kind)


def _check_file(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PseudonymKeyError("the pseudonym key file must be a regular file, not a link")
    if info.st_mode & 0o077:
        raise PseudonymKeyError("the pseudonym key file is readable by others; chmod 600")


def load_key(path: Path, *, create: bool = True) -> bytes:
    """Read the key, creating it on first use (atomic, exclusive, mode 0600). Refused: a link, a
    file group or others can read, anything that does not decode to 32 bytes."""
    if not os.path.lexists(path):
        if not create:
            raise PseudonymKeyError("there is no pseudonym key file")
        _write(path, exclusive=True)
    _check_file(path)
    try:
        key = base64.b64decode(path.read_text("ascii").strip(), validate=True)
    except (ValueError, UnicodeDecodeError):
        raise PseudonymKeyError("the pseudonym key file is not a valid key") from None
    if len(key) != KEY_BYTES:
        raise PseudonymKeyError("the pseudonym key file holds a key of the wrong size")
    return key


def rotate_key(path: Path) -> str:
    """Write a new key over the old one atomically (the old key is not kept); returns the new key
    id, which is not secret."""
    key = _write(path, exclusive=False)
    return key_id(key)


def _write(path: Path, *, exclusive: bool) -> bytes:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".new-", suffix=".key")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(base64.b64encode(key) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if exclusive:
            try:
                os.link(temporary, path)  # fails if another process won: use theirs
            except FileExistsError:
                pass
        else:
            os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return key
