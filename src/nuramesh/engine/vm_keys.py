"""Merge Gate signer keys in the engine (spec `spec/verified-merge` SV-4, SV-6, SV-14, GT-17,
GT-20 a, LA-2, LY-1; Fable fixes F-17 strict verification, F-12 throwaway shadow keys).

Custody option A: the signer's private key is a 64-hex Ed25519 seed in a file with mode **0400**
exactly, owned by the signer's own OS user (the process's effective user), outside every
repository, a regular file reached without following a link, at most 1024 bytes, not readable by
the agent user. `FileSignerBackend` re-reads and re-checks the file on **every** call (as
`engine/key_file.py` does for provider keys): nothing is cached, so a mode change takes effect
at the next signature. The private half never leaves the function that signs with it; `repr` is
a fixed string; every error is a fixed text with a reason from `SIGNER_KEY_FILE_REASONS` and
never a path, a seed or a public key.

`verify_strict` is the gate's `Verify` callable: the `core.vm_ed25519` pre-checks (canonical
encodings, small-order A and R, S < L) first, then the library's verification. `verify_strict`
answers only True or False (GT-10 reports `verdict_bad_signature`; which check failed is not
part of the wire).

`EphemeralSignerBackend` holds a key generated at run time (tests; a throwaway shadow key, F-12).
The LA-2 ledger adapter (`StoreLedger`) lives in `engine/org_ledger_adapter.py`: this file imports
no store and no provider code, so the trust-root import closure stays small (TC-176 a).
`generate_signer_key_file` writes a new seed with `O_EXCL` and mode 0400 and returns only the key
id and the public key.
"""

from __future__ import annotations

import errno
import logging
import os
import pwd
import re
import stat
from collections.abc import Callable

from nuramesh.core.vm_ed25519 import signature_problem
from nuramesh.core.vm_formats import public_key_id

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError:  # pragma: no cover - the anchors extra is missing
    Ed25519PrivateKey = None  # type: ignore[assignment, misc]

SIGNER_KEY_MODE = 0o400
MAX_SIGNER_KEY_FILE_BYTES = 1024
SIGNER_KEY_FILE_REASONS = (
    "not an absolute path",
    "inside a repository",
    "not a regular file",
    "cannot be read",
    "not owned by the signer user",
    "permissions must be 0400",
    "readable by the agent user",
    "larger than 1024 bytes",
    "not a 32-byte hex seed",
    "reviewer id is not a plain name",
)
_PREFIX = "merge-gate signer: the key file is not usable ("
SIGNER_KEY_SUFFIX = ".seed"
_REVIEWER_FILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

# --- key-file helpers (vendored from engine/key_file.py, stdlib only: TC-176 a) ---------------

AGENT_USER_ENV = "NURAMESH_AGENT_USER"
DEFAULT_AGENT_USER = "nuramesh-agent"
_ACCOUNT_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def agent_user_name() -> str:
    """The coding agent's account name: `NURAMESH_AGENT_USER` when it is a plain account name
    ([a-z_][a-z0-9_-]{0,31}), else the neutral default. A set but invalid value (empty, a path,
    an option-like name) falls back to the default and never reaches a lookup."""
    value = os.environ.get(AGENT_USER_ENV)
    if value is not None and _ACCOUNT_NAME.fullmatch(value) is not None:
        return value
    if value:
        # the value itself is never logged (it is untrusted text)
        logging.getLogger(__name__).warning(
            "%s is not a plain account name; using the default %s",
            AGENT_USER_ENV,
            DEFAULT_AGENT_USER,
        )
    return DEFAULT_AGENT_USER


IsAgentUser = Callable[[int], bool]


def is_agent_user(uid: int) -> bool:
    """True when the file belongs to the coding agent's account."""
    agent_uid: int | None = None
    try:
        agent_uid = pwd.getpwnam(agent_user_name()).pw_uid
    except (KeyError, OSError):
        pass
    return agent_uid == uid


def in_repository(path: str) -> bool:
    """True when `path` lies inside a Git working tree (a `.git` entry in a parent folder)."""
    folder = os.path.dirname(os.path.realpath(path))
    while True:
        if os.path.lexists(os.path.join(folder, ".git")):
            return True
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def _lstat_reason(path: str) -> str:
    """After EACCES: the reason, from one `lstat` for the error text only."""
    found = None
    try:
        found = os.lstat(path)
    except OSError:
        pass
    if found is None:
        return "cannot be read"
    if not stat.S_ISREG(found.st_mode):
        return "not a regular file"
    if found.st_uid != os.geteuid():
        return "not owned by the signer user"
    if stat.S_IMODE(found.st_mode) != SIGNER_KEY_MODE:
        return "permissions must be 0400"
    return "cannot be read"


def open_key(path: str) -> tuple[int, str | None]:
    """Open without following a link or blocking; returns `(fd, None)` or `(-1, reason)`."""
    code = 0
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        code = error.errno or -1
    if fd >= 0:
        return fd, None
    if code == errno.ELOOP:
        return -1, "not a regular file"
    if code == errno.EACCES:
        return -1, _lstat_reason(path)
    return -1, "cannot be read"


class SignerKeyError(Exception):
    """A fixed text about the signer key file; never a path or key material."""


def _require_crypto() -> None:
    if Ed25519PrivateKey is None:  # pragma: no cover
        raise SignerKeyError("merge-gate signer: install nuramesh[anchors] for Ed25519")


def _raw_public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


# --- strict verification (F-17) -------------------------------------------------------------------


def verify_strict(public_key_hex: str, message: bytes, sig_hex: str) -> bool:
    """Strict Ed25519: lower-case hex inputs, the `core.vm_ed25519` pre-checks, then the library.
    False for anything else; never raises for bad input."""
    _require_crypto()
    if public_key_hex != public_key_hex.lower() or sig_hex != sig_hex.lower():
        return False
    try:
        public = bytes.fromhex(public_key_hex)
        signature = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    if signature_problem(public, signature) is not None:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


# --- backends -------------------------------------------------------------------------------------


class EphemeralSignerBackend:
    """A key that lives only in this process: generated at run time or from a seed the caller
    hands over (which this object does not keep by name). For tests and throwaway shadow keys."""

    def __init__(self, private: Ed25519PrivateKey) -> None:
        public = _raw_public(private).hex()

        def _sign(message: bytes) -> bytes:
            return private.sign(message)

        self._sign_with = _sign
        self._public = public
        self._key_id = public_key_id(public)

    @classmethod
    def generate(cls) -> EphemeralSignerBackend:
        _require_crypto()
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> EphemeralSignerBackend:
        _require_crypto()
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    def __repr__(self) -> str:
        return "EphemeralSignerBackend(key hidden)"

    __str__ = __repr__

    @property
    def key_id(self) -> str:
        return self._key_id

    def public_key_hex(self) -> str:
        return self._public

    def sign(self, domain: bytes, message: bytes) -> bytes:
        return self._sign_with(domain + message)


def signer_key_path(key_dir: str, reviewer_id: str) -> str:
    """F-7, one key per reviewer id: the seed file of `reviewer_id` inside `key_dir`
    (`<key_dir>/<reviewer_id>.seed`). The id must be a plain lower-case name (letters, digits,
    `_`, `-`; no dot, no separator), so an id can never name a path outside the folder or
    another reviewer's file; anything else is refused with a fixed text."""
    if not isinstance(key_dir, str) or not os.path.isabs(key_dir) or "\0" in key_dir:
        raise SignerKeyError(_PREFIX + "not an absolute path)")
    if not isinstance(reviewer_id, str) or _REVIEWER_FILE_NAME.fullmatch(reviewer_id) is None:
        raise SignerKeyError(_PREFIX + "reviewer id is not a plain name)")
    return os.path.join(key_dir, reviewer_id + SIGNER_KEY_SUFFIX)


def _read_seed_file(path: str, agent: IsAgentUser) -> bytes:
    """The checked bytes of the seed file (every rule of custody option A), or `SignerKeyError`
    with its reason. The one place that opens the key file."""
    if not path or "\0" in path:
        raise SignerKeyError(_PREFIX + "cannot be read)")
    if not os.path.isabs(path):
        raise SignerKeyError(_PREFIX + "not an absolute path)")
    if in_repository(path):
        raise SignerKeyError(_PREFIX + "inside a repository)")
    fd, reason = open_key(path)
    if reason is not None:
        raise SignerKeyError(_PREFIX + reason + ")")
    data = b""
    failed = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            reason = "not a regular file"
        elif info.st_uid != os.geteuid():
            reason = "not owned by the signer user"
        elif stat.S_IMODE(info.st_mode) != SIGNER_KEY_MODE:
            reason = "permissions must be 0400"
        elif agent(info.st_uid):
            reason = "readable by the agent user"
        elif info.st_size > MAX_SIGNER_KEY_FILE_BYTES:
            reason = "larger than 1024 bytes"
        else:
            limit = MAX_SIGNER_KEY_FILE_BYTES
            while len(data) <= limit:
                chunk = os.read(fd, limit + 1 - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) > limit:
                reason = "larger than 1024 bytes"
    except OSError:
        failed = True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if failed:
        raise SignerKeyError(_PREFIX + "cannot be read)")
    if reason is not None:
        raise SignerKeyError(_PREFIX + reason + ")")
    text = data.rstrip(b"\r\n")
    if len(text) != 64 or any(c not in b"0123456789abcdef" for c in text):
        raise SignerKeyError(_PREFIX + "not a 32-byte hex seed)")
    return bytes.fromhex(text.decode("ascii"))


class FileSignerBackend:
    """Custody option A (GT-20 a): the seed file is read and checked on every call."""

    def __init__(self, path: str, *, agent: IsAgentUser = is_agent_user) -> None:
        _require_crypto()
        self._path = path
        self._agent = agent

    @classmethod
    def for_reviewer(
        cls, key_dir: str, reviewer_id: str, *, agent: IsAgentUser = is_agent_user
    ) -> FileSignerBackend:
        """The backend over the one key file of `reviewer_id` in `key_dir` (F-7). Whether that
        key may sign for the reviewer is still the trust list's word (`holder`), checked by the
        signer at start and at every signature."""
        return cls(signer_key_path(key_dir, reviewer_id), agent=agent)

    def __repr__(self) -> str:
        return "FileSignerBackend(path hidden)"

    __str__ = __repr__

    def _with_key[T](self, use: Callable[[Ed25519PrivateKey], T]) -> T:
        seed = _read_seed_file(self._path, self._agent)
        try:
            return use(Ed25519PrivateKey.from_private_bytes(seed))
        except ValueError:
            raise SignerKeyError(_PREFIX + "not a 32-byte hex seed)") from None

    @property
    def key_id(self) -> str:
        return public_key_id(self.public_key_hex())

    def public_key_hex(self) -> str:
        return self._with_key(lambda private: _raw_public(private).hex())

    def sign(self, domain: bytes, message: bytes) -> bytes:
        return self._with_key(lambda private: private.sign(domain + message))


def generate_signer_key_file(path: str) -> tuple[str, str]:
    """Write a new seed to `path` (created with `O_EXCL`, mode 0400; refuses to overwrite) and
    return `(key_id, public_key_hex)`: the only values the caller should ever print."""
    _require_crypto()
    if not os.path.isabs(path):
        raise SignerKeyError(_PREFIX + "not an absolute path)")
    if in_repository(path):
        raise SignerKeyError(_PREFIX + "inside a repository)")
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    except FileExistsError:
        raise SignerKeyError(
            "merge-gate signer: the key file exists; refusing to overwrite a key"
        ) from None
    except OSError:
        raise SignerKeyError(_PREFIX + "cannot be read)") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(seed.hex().encode("ascii") + b"\n")
        os.chmod(path, 0o400)  # the umask cannot widen 0400, but be explicit about the mode
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise SignerKeyError(_PREFIX + "cannot be read)") from None
    public = _raw_public(private).hex()
    return public_key_id(public), public
