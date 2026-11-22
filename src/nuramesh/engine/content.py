"""The content store service: capture, reveal, expire, shred and hold (ND25, T46).

`core.content` defines the data model; this module adds what needs a library and the file system:

- **Keys.** An organization's 256-bit master key lives in a `KeyRing`, never in the content store
  or the ledger. `FileKeyRing` keeps one owner-only file per organization; `MemoryKeyRing` is for
  tests. **Destroying a key is crypto-shredding**: every record of that organization, in the
  database, its replicas and its backups, becomes unreadable at once.
- **Encryption.** AES-256-GCM (the `content` extra: `cryptography`), keyed by a key derived from the
  master key, with the organization, kind and keyed hash as associated data, so a record can't be
  moved to another organization or id.
- **Capture is part of the caller's transaction.** `capture` writes the encrypted record through the
  caller's unit of work, so the content and the ledger entry that names its hash commit together or
  not at all (no ledger hash without content, no content without its entry).
- **Everything that changes or reads content is in the ledger without the content:** `content.read`
  (who read which hash), `content.expired` (how many, no content) and `decision.recorded` for legal
  holds and shredding. Erasing content never touches the chain.
"""

import base64
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from nuramesh.core.auth import SecurityLogEntry, coalesced_row_id
from nuramesh.core.autonomy import (
    KIND_CONFIG,
    KIND_CONFIG_SWAP,
    current_config_content,
    decision_kind,
    swapped_config_content,
)
from nuramesh.core.canonical import canonical_json, canonical_json_jcs
from nuramesh.core.content import (
    DEFAULT_REASON_DAYS,
    DEFAULT_RETENTION_DAYS,
    HASH_SCHEME,
    HOSTED_MAX_DAYS,
    KEY_BYTES,
    MAX_TEXT_BYTES,
    ContentKind,
    ContentRecord,
    ContentSettings,
    KeyTombstone,
    LedgerHashKind,
    encryption_key,
    hkdf,
    key_id,
    keyed_hash,
    ledger_hash,
    valid_tombstone_actor,
)
from nuramesh.core.gates import GateResult
from nuramesh.core.idempotency import human_subjects
from nuramesh.core.ledger import EventType, format_time
from nuramesh.core.prices import PriceOverride
from nuramesh.core.tasks import Task
from nuramesh.engine.run_request import read_plan_text
from nuramesh.env_bool import EnvBoolError, env_bool
from nuramesh.env_bool import startup_problem as env_startup_problem
from nuramesh.hosted import HOSTED_ENV_PROBLEM as _SHARED_HOSTED_PROBLEM
from nuramesh.hosted import HostedConfigError
from nuramesh.hosted import hosted_from_env as _shared_hosted_from_env
from nuramesh.paths import home_path
from nuramesh.secret_env import startup_problem as secret_startup_problem
from nuramesh.stores import EventDraft, Store, UnitOfWork
from nuramesh.stores.base import NestedTransactionError

DEFAULT_CONTENT_DAYS = DEFAULT_RETENTION_DAYS
"""ND25: how long gate evidence and task outputs are kept by default."""

REASON_DAYS = DEFAULT_REASON_DAYS
MAX_REASON_DAYS = 36_500  # the same bound as `ContentSettings.reason_days`
"""How long a person's reason is kept by default (13 months: approvals' retention, ND28 D). A reason
is a governance record the owner required, so it outlives ordinary content; it is still erasable.
On a hosted deployment it is capped at `HOSTED_MAX_DAYS` (ND195 b); the organization's
`content.reason_days` sets it (`ContentService.capture_reason`)."""
CONFIG_DAYS = DEFAULT_REASON_DAYS
"""E13 policy configuration content (kind `config`): 396 days self-hosted. It holds pseudonymous
approver ids and operator free text, so on a hosted deployment it is capped at `HOSTED_MAX_DAYS`
like a reason (ND213), content stored before a switch to hosted is clamped by
`ContentService.clamp_hosted_config` (ND218); the current configuration is re-captured before it
expires (ND194)."""

WORKING_DAYS = HOSTED_MAX_DAYS
"""How long a durable run's task output is kept **while its task is alive** (a working expiry,
ND55): the hosted maximum (a fixed bound, also for self-hosted). When the task is final,
`finish_task_output` sets the real expiry (final + the organization's retention). A task that
waits for a human longer than this is warned
about and then stopped (R1.20 PR 3); self-hosted deployments use the same bound."""


_REWRAP_PREFIX = ".rewrap-"
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_LOG = logging.getLogger("nuramesh.content")
_STORE_ID = re.compile(r"[0-9a-f]{32}")
_MAX_TOMBSTONE_BYTES = 1024
_MAX_KEY_FILE_BYTES = 1024
_HASH = re.compile(r"[0-9a-f]{64}")
READ_REASONS = frozenset({"audit", "api", "ui", "export", "support", "legal"})
"""Why a person read content: a fixed category, never free text (the ledger can't be erased)."""
SYSTEM_READ_REASONS = frozenset({"recapture", "erasure_scrub"})
"""Why a system job (never a person) reads content; recorded as `content.read` like a person's."""


def _blank_reasons_of(text: str, person: str) -> str | None:
    """The stored configuration `text` with the rule `reason` of every policy that lists `person`
    under `approvers` blanked, or None when nothing has to change (or it is not a configuration
    document). Re-serialised compactly with sorted keys, like `canonical_json`."""
    try:
        document = json.loads(text)
        policies = document["policies"]["policies"]
    except (ValueError, KeyError, TypeError):
        return None
    changed = False
    for policy in policies:
        if not isinstance(policy, dict) or person not in (policy.get("approvers") or ()):
            continue
        for rule in policy.get("rules") or ():
            if isinstance(rule, dict) and rule.get("reason"):
                rule["reason"] = ""
                changed = True
    return canonical_json(document).decode("utf-8") if changed else None


def _org(org_id: str) -> str:
    """The organization id, checked before it is used in a file name or a query."""
    if not _SLUG.fullmatch(org_id):
        raise ContentError("an organization id is a lower-case slug")
    return org_id


class ContentError(Exception):
    """Something the caller can fix: a missing key, an over-long text, a bad key file. `code` is
    set for the refusals callers tell apart (R1.37): see the subclasses below."""

    code: str | None = None

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


KEY_DESTROYED = "org_key_destroyed"
KEY_MISSING = "org_key_missing"
KEY_MISMATCH = "org_key_mismatch"
TOMBSTONE_UNAVAILABLE = "key_tombstone_unavailable"
GATE_CODES = frozenset({KEY_DESTROYED, KEY_MISSING, KEY_MISMATCH, TOMBSTONE_UNAVAILABLE})
"""The three refusals of the key gate. `request_stop` and the Temporal wrapper treat them alike."""

DESTROYED_MESSAGE = (
    "this organization's content key was destroyed; content can't be kept for it. "
    "Nothing was changed."
)
MISSING_MESSAGE = (
    "this organization has no content key but still has content or hashes made with one. "
    "Nothing was changed."
)
MISMATCH_MESSAGE = (
    "this organization's key is not the one its stored content or ledger hashes were made with. "
    "Nothing was changed."
)


class ContentExtraMissing(ContentError):
    """The `content` extra (`cryptography`) is not installed, so no content service can be built.
    The ONLY content error the halt refusal treats specially (it reads the ring tombstone file
    directly); every other ContentError, and every other exception, counts as erased."""


class KeyDestroyedError(ContentError):
    """The organization's key was destroyed and is never recreated (R1.37, K1)."""

    code = KEY_DESTROYED

    def __init__(self, org_id: str) -> None:
        super().__init__(DESTROYED_MESSAGE)
        self.org_id = org_id


class OrphanedContentError(ContentError):
    """No key, but content rows or keyed ledger rows exist (a lost key, not a new organization), or
    a key that is not the one those rows were made with (R1.37 orphan guard, PH-KEY R5b)."""

    code = KEY_MISSING

    def __init__(self, org_id: str, *, mismatch: bool = False) -> None:
        super().__init__(MISMATCH_MESSAGE if mismatch else MISSING_MESSAGE)
        self.org_id = org_id
        if mismatch:
            self.code = KEY_MISMATCH


class OrgIdRetiredError(ContentError):
    """The organization id was erased and is retired for the life of the store (R1.37 section 9):
    a run can't start under it. Not a key-gate refusal: nothing was attempted."""

    code = "org_id_retired"

    def __init__(self, org_id: str, destroyed_at: datetime, *, approximate: bool = False) -> None:
        if approximate:  # the date is the migration clock, never shown as the real one (T258)
            when = "on a date that can't be read (original date unreadable)"
        else:
            when = f"on {destroyed_at.astimezone(UTC).strftime('%Y-%m-%d')}"
        super().__init__(f"organization '{org_id}' was erased {when}; use a new organization id")
        self.org_id = org_id
        self.destroyed_at = destroyed_at
        self.at_approximate = approximate


def tombstone_unavailable(detail: str) -> ContentError:
    """A tombstone that exists but can't be read: refused, never "no tombstone"."""
    return ContentError(detail, code=TOMBSTONE_UNAVAILABLE)


def gate_error(code: str, org_id: str) -> ContentError:
    """The refusal a gate code stands for (wind-down mode raises it before any model call)."""
    if code == KEY_DESTROYED:
        return KeyDestroyedError(org_id)
    if code == KEY_MISSING:
        return OrphanedContentError(org_id)
    if code == KEY_MISMATCH:
        return OrphanedContentError(org_id, mismatch=True)
    return tombstone_unavailable("the organization's key tombstone can't be read")


def is_gate_refusal(exc: BaseException) -> bool:
    """True for the three key-gate refusals (destroyed, missing key, unreadable tombstone)."""
    return isinstance(exc, ContentError) and exc.code in GATE_CODES


class KeyRing(Protocol):
    """Where master keys and tombstones live. Never inside the content store."""

    def master(self, org_id: str, *, create: bool = False) -> bytes | None:
        """The organization's key, or None if it has none (never made, or tombstoned). With a
        tombstone (either copy) `create=True` raises `KeyDestroyedError` and the key is never
        opened (K2)."""

    def tombstone(self, org_id: str) -> KeyTombstone | None:
        """The ring's own tombstone copy, or None; an unreadable one raises."""

    def tombstone_present(self, org_id: str) -> bool:
        """Whether a ring tombstone entry exists (readable or not); never opens it."""

    def put_tombstone(self, record: KeyTombstone) -> bool:
        """Write the ring copy once. True if this call made it; False if one exists."""

    def key_present(self, org_id: str) -> bool:
        """Whether a key file exists in this ring's folder; never opens it."""

    def key_for_shred(self, org_id: str) -> bytes | None:
        """The key whatever the tombstones say. Only `ContentService.shred` calls this."""

    def tombstoned_orgs(self) -> list[str]:
        """Organizations that have a ring tombstone entry."""

    def destroy(self, org_id: str, tombstone: KeyTombstone | None = None) -> bool:
        """Crypto-shred: write the ring tombstone FIRST (if given), then destroy the key. True if
        there was a key."""


def _record_json(record: KeyTombstone) -> bytes:
    body: dict[str, object] = {
        "version": record.version,
        "org_id": record.org_id,
        "destroyed_at": format_time(record.destroyed_at),
        "actor": record.actor,
        "reason": record.reason,
        "key_id": record.key_id,
    }
    if record.at_approximate:  # the date is the migration clock (T258): say so in every export
        body["at_approximate"] = True
        body["date_note"] = "original date unreadable"
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


class MemoryKeyRing:
    """Keys and tombstones in memory, for tests and short-lived tools. `store_tombstone` is the
    same hook `FileKeyRing` takes (optional here)."""

    def __init__(
        self, *, store_tombstone: Callable[[str], KeyTombstone | None] | None = None
    ) -> None:
        self._keys: dict[str, bytes] = {}
        self._tombstones: dict[str, KeyTombstone] = {}
        self._store_tombstone = store_tombstone
        self._create_lock = threading.Lock()  # check-and-create is one step (two tasks, one key)

    def _destroyed(self, org_id: str) -> bool:
        if self._store_tombstone is not None and self._store_tombstone(org_id) is not None:
            return True
        return org_id in self._tombstones

    def master(self, org_id: str, *, create: bool = False) -> bytes | None:
        _org(org_id)
        if self._destroyed(org_id):
            if create:
                raise KeyDestroyedError(org_id)
            return None
        if org_id not in self._keys and create:
            with self._create_lock:
                if self._destroyed(org_id):
                    raise KeyDestroyedError(org_id)
                if org_id not in self._keys:
                    self._keys[org_id] = secrets.token_bytes(KEY_BYTES)
        return self._keys.get(org_id)

    def tombstone(self, org_id: str) -> KeyTombstone | None:
        return self._tombstones.get(_org(org_id))

    def tombstone_present(self, org_id: str) -> bool:
        return _org(org_id) in self._tombstones

    def put_tombstone(self, record: KeyTombstone) -> bool:
        if record.org_id in self._tombstones:
            return False
        self._tombstones[record.org_id] = record.model_copy(update={"backfilled": False})
        return True

    def key_present(self, org_id: str) -> bool:
        return _org(org_id) in self._keys

    def key_for_shred(self, org_id: str) -> bytes | None:
        return self._keys.get(_org(org_id))

    def tombstoned_orgs(self) -> list[str]:
        return sorted(self._tombstones)

    def destroy(self, org_id: str, tombstone: KeyTombstone | None = None) -> bool:
        if tombstone is not None:
            self.put_tombstone(tombstone)
        return self._keys.pop(org_id, None) is not None


class FileKeyRing:
    """One key file and one tombstone file per organization in the store's own folder
    `<directory>/<store_id>/`, a folder only the owner can use (mode 0700 and 0600). Two stores
    (or deployments) sharing `directory` share nothing (R1.37 K7).

    The key file holds the key as base64 text. Every use refuses a symbolic link, a file or
    directory anyone else can use and a key of the wrong size; creation is atomic and exclusive
    (the key is written to a private temporary file and linked into place, so two processes never
    make two keys and nobody ever reads a half-written one). **The store is asked first**
    (`store_tombstone`): an organization with a tombstone in either place never gets a key and a
    key that is present beside a tombstone is never opened.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        store_id: str,
        store_tombstone: Callable[[str], KeyTombstone | None],
        keks: Sequence[bytes] = (),
    ) -> None:
        if not _STORE_ID.fullmatch(store_id):
            raise ContentError("a store id is 32 lower-case hex digits")
        for kek in keks:
            _check_kek(kek)
        self._keks = tuple(keks)  # newest first; empty = key files are plain (the R1 default)
        self.root = Path(directory)
        self.store_id = store_id
        self.directory = self.root / store_id
        self._store_tombstone = store_tombstone

    def _path(self, org_id: str) -> Path:
        return self.directory / f"{_org(org_id)}.key"

    def _tombstone_path(self, org_id: str) -> Path:
        return self.directory / f"{_org(org_id)}.tombstone"

    @staticmethod
    def _check_directory(directory: Path) -> None:
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ContentError(f"{directory} must be a real directory, not a link")
        if info.st_mode & 0o077:
            raise ContentError(f"{directory} is accessible by others; chmod 700")

    def _ensure_folder(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_directory(self.root)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self._check_directory(self.directory)

    def _folder_ready(self) -> bool:
        """Whether the store's folder exists (and, if so, is safe)."""
        if not os.path.lexists(self.directory):
            return False
        self._check_directory(self.root)
        self._check_directory(self.directory)
        return True

    # -- tombstones --------------------------------------------------------------------------

    def tombstone_present(self, org_id: str) -> bool:
        return os.path.lexists(self._tombstone_path(org_id))

    def tombstone(self, org_id: str) -> KeyTombstone | None:
        path = self._tombstone_path(org_id)
        if not os.path.lexists(path):
            return None
        unreadable = f"the key tombstone {path.name} can't be read"
        try:
            self._check_directory(self.directory)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise tombstone_unavailable(f"{unreadable}: not a regular file")
            if info.st_mode & 0o077:
                raise tombstone_unavailable(f"{unreadable}: readable by others; chmod 600")
            if info.st_size > _MAX_TOMBSTONE_BYTES:
                raise tombstone_unavailable(f"{unreadable}: too large")
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, dict) or data.get("org_id") != org_id:
                raise tombstone_unavailable(f"{unreadable}: names another organization")
            return KeyTombstone(
                org_id=org_id,
                destroyed_at=datetime.fromisoformat(data["destroyed_at"]),
                actor=data["actor"],
                reason=data["reason"],
                key_id=data.get("key_id"),
                version=data["version"],
            )
        except ContentError as exc:
            if exc.code == TOMBSTONE_UNAVAILABLE:
                raise
            raise tombstone_unavailable(f"{unreadable}: {exc}") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise tombstone_unavailable(unreadable) from exc

    def put_tombstone(self, record: KeyTombstone) -> bool:
        path = self._tombstone_path(record.org_id)
        if os.path.lexists(path):
            return False
        self._ensure_folder()
        descriptor, temporary = tempfile.mkstemp(
            dir=self.directory, prefix=".new-", suffix=".tombstone"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_record_json(record))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, path)  # atomic and exclusive: never overwrites
            except FileExistsError:
                return False
            return True
        finally:
            os.unlink(temporary)

    def tombstoned_orgs(self) -> list[str]:
        if not self._folder_ready():
            return []
        return sorted(p.stem for p in self.directory.glob("*.tombstone") if _SLUG.fullmatch(p.stem))

    def _destroyed(self, org_id: str) -> bool:
        """Either copy exists: the store row first, then the ring file (even a damaged one, which
        raises `key_tombstone_unavailable` instead of answering)."""
        if self._store_tombstone(org_id) is not None:
            return True
        return self.tombstone(org_id) is not None

    # -- keys --------------------------------------------------------------------------------

    def key_present(self, org_id: str) -> bool:
        return os.path.lexists(self._path(org_id))

    def master(self, org_id: str, *, create: bool = False) -> bytes | None:
        path = self._path(org_id)
        if self._destroyed(org_id):
            if create:
                raise KeyDestroyedError(org_id)
            return None  # the key file, if one came back, is never opened (K2)
        if create:
            self._ensure_folder()
        if not self._folder_ready():
            return None
        if create and not os.path.lexists(path):
            self._create(path, org_id)
        return self._read(path)

    def key_for_shred(self, org_id: str) -> bytes | None:
        path = self._path(org_id)
        if not self._folder_ready():
            return None
        return self._read(path)

    def _read(self, path: Path) -> bytes | None:
        if not os.path.lexists(path):
            return None
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ContentError(f"{path} must be a regular file, not a link")
        if info.st_mode & 0o077:
            mode = info.st_mode & 0o777
            raise ContentError(f"{path} is readable by others (mode {mode:o}); chmod 600")
        # open without following a link, then check the OPEN file (R1.30 review-2 m5): a file
        # swapped for a link, or loosened, after the lstat above is caught on the descriptor
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ContentError(f"{path} must be a regular file, not a link") from exc
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ContentError(f"{path} must be a regular file, not a link")
            if os.name == "posix" and opened.st_uid != os.geteuid():
                raise ContentError(f"{path} is not owned by the current user")
            if opened.st_mode & 0o077:
                mode = opened.st_mode & 0o777
                raise ContentError(f"{path} is readable by others (mode {mode:o}); chmod 600")
            raw = handle.read(_MAX_KEY_FILE_BYTES + 1)
        if raw.startswith(_WRAP_PREFIX):
            return self._unwrap(path, raw)
        try:
            key = base64.b64decode(raw.decode("ascii").strip(), validate=True)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContentError(f"{path} is not a valid key file") from exc
        if len(key) != KEY_BYTES:
            raise ContentError(f"{path} holds a key of the wrong size")
        return key

    def _create(self, path: Path, org_id: str) -> None:
        fresh = secrets.token_bytes(KEY_BYTES)
        if self._keks:
            encoded = _wrap_master(fresh, self._keks[0], self.store_id, org_id)
        else:
            encoded = base64.b64encode(fresh) + b"\n"
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".new-", suffix=".key")
        linked = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, path)  # atomic and exclusive: fails if another process won
                linked = True
            except FileExistsError:
                pass  # use theirs
        finally:
            os.unlink(temporary)
        if linked and os.path.lexists(self._tombstone_path(org_id)):
            # a tombstone appeared while the key was being made (a shred, or a restore): the key
            # just made is destroyed before anyone can seal with it
            self._wipe(path)
            raise KeyDestroyedError(org_id)

    def _wipe(self, path: Path) -> bool:
        """Overwrite with zero bytes and unlink, never writing through a link."""
        if not os.path.lexists(path):
            return False
        self._check_directory(self.directory)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ContentError(f"{path} must be a regular file, not a link")
        # overwrite before unlinking (best effort: journaling disks may keep old blocks); opened
        # without following a link, and checked again on the open file, so a file swapped for a
        # link after the check above can't redirect the write
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except OSError as exc:
            raise ContentError(f"{path} must be a regular file, not a link") from exc
        with os.fdopen(descriptor, "r+b") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ContentError(f"{path} must be a regular file, not a link")
            handle.write(b"\x00" * info.st_size)
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
        return True

    def destroy(self, org_id: str, tombstone: KeyTombstone | None = None) -> bool:
        path = self._path(org_id)
        if tombstone is not None:
            self.put_tombstone(tombstone)  # first: a crash after this still refuses every use
        if not self._folder_ready():
            return False
        self.sweep_rewrap_orphans(org_id)
        return self._wipe(path)

    # -- key wrapping and re-wrap (R1.30 H-18, Q8) ---------------------------------------------

    def _unwrap(self, path: Path, raw: bytes) -> bytes:
        """Open a wrapped key file with the configured key-encryption keys. Refuses (never
        guesses) when none of them is the one the file names."""
        try:
            wrapped_id, blob = _parse_wrapped(raw)
        except ValueError as exc:
            raise ContentError(f"{path} is not a valid key file") from exc
        for kek in self._keks:
            if _kek_id(kek) == wrapped_id:
                key = _open_master(blob, kek, self.store_id, path.stem)
                if key is None:
                    raise ContentError(f"{path} is not a valid key file")
                return key
        raise ContentError(
            f"{path} is wrapped under key-encryption key {wrapped_id}, which this ring does not "
            "hold"
        )

    def wrapped_under(self, org_id: str) -> str | None:
        """The id of the key-encryption key the org's key file is wrapped under; None if the file
        is plain or missing. Reads no key."""
        path = self._path(org_id)
        if not self._folder_ready() or not os.path.lexists(path):
            return None
        raw = self._read_head(path)
        if not raw.startswith(_WRAP_PREFIX):
            return None
        try:
            return _parse_wrapped(raw)[0]
        except ValueError as exc:
            raise ContentError(f"{path} is not a valid key file") from exc

    @staticmethod
    def _read_head(path: Path) -> bytes:
        """At most one key file's worth of bytes, never through a link (`O_NOFOLLOW`)."""
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ContentError(f"{path} must be a regular file, not a link") from exc
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ContentError(f"{path} must be a regular file, not a link")
            return handle.read(_MAX_KEY_FILE_BYTES + 1)

    def holds_kek(self, kek: bytes) -> bool:
        """Whether this ring was given `kek` (compared by id; no key is shown)."""
        return any(_kek_id(k) == _kek_id(kek) for k in self._keks)

    def kek_count(self) -> int:
        return len(self._keks)

    def sweep_rewrap_orphans(self, org_id: str) -> int:
        """Wipe and remove the org's leftover re-wrap temp files (a crash between the temp write
        and the replace leaves one: the org's live master key under a key-encryption key). Run
        under the store's write transaction, and by every shred. Returns how many were removed."""
        if not self._folder_ready():
            return 0
        removed = 0
        stuck = 0
        for found in self.directory.glob(f"{_REWRAP_PREFIX}{_org(org_id)}.*"):
            try:
                self._wipe(found)
                removed += 1
                continue
            except (ContentError, OSError):
                pass
            try:
                found.unlink()
                removed += 1
            except FileNotFoundError:
                removed += 1
            except OSError:
                stuck += 1
        if stuck:
            raise ContentError("a re-wrap temp file could not be removed: key material may remain")
        return removed

    def key_orgs(self) -> list[str]:
        """Organizations that have a key file in this ring's folder (tombstoned ones included)."""
        if not self._folder_ready():
            return []
        found = []
        for p in self.directory.glob("*.key"):
            if _SLUG.fullmatch(p.stem) and p.is_file() and not p.is_symlink():
                found.append(p.stem)
        return sorted(found)

    def rewrap(self, org_id: str, new_kek: bytes) -> bool:
        """Re-wrap the org's key file under `new_kek`: read it with the ring's keys, write the new
        file privately, read it back, then replace the old one atomically. The master key does not
        change (content ids and ledger hashes depend on it); only the file's protection does.
        False if the file is already wrapped under `new_kek` (a resumed run). A tombstoned
        organization is refused. The old file is never partly written: a crash leaves the old
        file or the new one, both readable by a ring holding both keys."""
        _check_kek(new_kek)
        if self._destroyed(org_id):
            raise KeyDestroyedError(org_id)
        path = self._path(org_id)
        if not self._folder_ready():
            raise ContentError("no key file")
        if not self.holds_kek(new_kek):
            raise ContentError("the ring does not hold the new key-encryption key; nothing changed")
        master = self._read(path)  # also proves the ring can open the current file
        if master is None:
            raise ContentError("no key file")
        if self.wrapped_under(org_id) == _kek_id(new_kek):
            # decided by opening the file (the header is not authenticated): `_read` above did it
            return False
        encoded = _wrap_master(master, new_kek, self.store_id, org_id)
        # a private name no other writer uses, so a sweep never touches a key being created
        descriptor, temporary = tempfile.mkstemp(
            dir=self.directory, prefix=f"{_REWRAP_PREFIX}{org_id}.", suffix=".key"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            # read-back check on the file as it is on disk, not the bytes held in memory
            try:
                _, blob = _parse_wrapped(self._read_head(Path(temporary)))
            except ValueError as exc:
                raise ContentError("re-wrap check failed; the old key file is unchanged") from exc
            if _open_master(blob, new_kek, self.store_id, org_id) != master:
                raise ContentError("re-wrap check failed; the old key file is unchanged")
            os.replace(temporary, path)
            temporary = ""
        finally:
            if temporary:
                with suppress(OSError):
                    self._wipe(Path(temporary))
                with suppress(OSError):
                    os.unlink(temporary)
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True

    # -- legacy flat keys (before the per-store folder) ---------------------------------------

    def legacy_orgs(self) -> list[str]:
        """Organizations with a flat `<directory>/<org>.key` from before the per-store folder."""
        if not self.root.is_dir():
            return []
        found = []
        for p in self.root.glob("*.key"):
            if _SLUG.fullmatch(p.stem) and p.is_file() and not p.is_symlink():
                found.append(p.stem)
        return sorted(found)

    def adopt_legacy(self, org_id: str) -> bool:
        """Move the flat key into this store's folder (a hard link, then unlink of the flat name).
        False if there is no flat key (or another store took it).

        One adoption runs at a time per key folder: an `flock` on `<directory>/.adopt.lock` is
        held for the whole move and goes away when the process dies. Under it the flat name is
        removed only when it and the destination are the same file and have exactly two names;
        anything else is refused and nothing is shared (R1.30 final review M1)."""
        flat = self.root / f"{_org(org_id)}.key"
        if not os.path.lexists(flat):
            return False
        self._ensure_folder()
        with self._adoption_lock():
            return self._adopt_locked(flat, org_id)

    @contextmanager
    def _adoption_lock(self) -> Iterator[None]:
        lock_path = self.root / ".adopt.lock"
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:  # a symlink (ELOOP), no permission, a read-only folder
            raise ContentError(
                f"cannot open the adoption lock {lock_path} ({exc.strerror}); it must be a "
                "plain file owned by you; nothing was moved"
            ) from exc
        try:
            if os.name == "posix":
                import fcntl

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                except OSError as exc:  # for example ENOLCK on a filesystem without locks
                    raise ContentError(
                        f"cannot lock {lock_path} ({exc.strerror}); this filesystem does not "
                        "support file locks; move the key into the store folder by hand"
                    ) from exc
            yield
        finally:
            os.close(descriptor)  # closing releases the lock

    def _adopt_locked(self, flat: Path, org_id: str) -> bool:
        try:
            info = os.lstat(flat)
        except FileNotFoundError:  # the owner finished while we waited: nothing to adopt
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ContentError(f"{flat} must be a regular file, not a link")
        if os.name == "posix" and info.st_uid != os.geteuid():
            raise ContentError(f"{flat} is not owned by the current user")
        if info.st_mode & 0o077:
            raise ContentError(f"{flat} is readable by others; chmod 600")
        destination = self._path(org_id)
        # Rule: the first adoption that linked the flat key owns it. A crash between link and
        # unlink leaves both names on one inode; the owner's rerun finishes the unlink. Any
        # other store that finds the flat key linked elsewhere refuses and never shares it.
        if os.path.lexists(destination):
            if _same_file(flat, destination):
                if info.st_nlink != 2:
                    raise ContentError(
                        f"{flat} is shared with another store or a backup (it has "
                        f"{info.st_nlink} names); not moved. Remove the extra links by hand"
                    )
                try:
                    os.unlink(flat)
                except FileNotFoundError:
                    pass
                return True
            raise ContentError(f"{destination} already holds a key; the legacy key was not moved")
        if info.st_nlink > 1:
            raise ContentError(
                f"{flat} has other hard links (a backup, or a store whose adoption never "
                "finished); not moved. Remove the extra links or move the key manually"
            )
        # a hard link fails if the destination exists (a rename would silently replace a key a
        # run made in the meantime, R1.30 review-2 m2); links are never followed (a flat name
        # swapped for a symlink is linked as the symlink and caught by the check below)
        try:
            os.link(flat, destination, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except FileExistsError:
            raise ContentError(
                f"{destination} already holds a key; the legacy key was not moved"
            ) from None
        except OSError as exc:  # EXDEV, EPERM or ENOTSUP: no hard links here
            raise ContentError(
                f"cannot adopt {flat}: the filesystem refused a hard link ({exc.strerror}); "
                "move the key into the store folder by hand"
            ) from exc
        try:
            now = os.lstat(flat)
            linked = os.lstat(destination)
        except FileNotFoundError:  # the flat name vanished: give ours back
            destination.unlink(missing_ok=True)
            return False
        same = (
            (info.st_dev, info.st_ino)
            == (linked.st_dev, linked.st_ino)
            == (
                now.st_dev,
                now.st_ino,
            )
        )
        if not same or not stat.S_ISREG(linked.st_mode) or now.st_nlink != 2:
            destination.unlink(missing_ok=True)
            raise ContentError(
                f"{flat} changed or is being linked by another store while it was adopted; "
                "not moved"
            )
        try:
            os.unlink(flat)
        except FileNotFoundError:
            destination.unlink(missing_ok=True)
            return False
        return True


def _same_file(a: Path, b: Path) -> bool:
    """True if both names are the same file (device and inode), links not followed."""
    try:
        first, second = os.lstat(a), os.lstat(b)
    except FileNotFoundError:  # a name vanished: not the same file, nothing to compare
        return False
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


class _KeyReader(Protocol):
    """What the presence check reads: a unit of work or a store (PH-KEY R5b)."""

    def org_key_material_in_use(self, org_id: str) -> bool: ...

    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]: ...

    def org_content_key_ids(self, org_id: str) -> frozenset[str]: ...

    def key_tombstone(self, org_id: str) -> KeyTombstone | None: ...


def key_presence_problem(
    reader: _KeyReader,
    org_id: str,
    master: bytes | None,
    tombstoned: bool,
    ring: KeyRing | None = None,
) -> ContentError | None:
    """The problem half of `key_presence_check` (see there)."""
    return key_presence_check(reader, org_id, master, tombstoned, ring)[0]


def key_presence_check(
    reader: _KeyReader,
    org_id: str,
    master: bytes | None,
    tombstoned: bool,
    ring: KeyRing | None = None,
) -> tuple[ContentError | None, bytes | None]:
    """THE key-presence rule (PH-KEY R5b), the only place it lives. `tombstoned` is checked by
    the caller first (a destroyed key is its own error), so it is never a problem here.

    (a) no key, key material in use (content rows or keyed ledger rows of the organization) ->
    `org_key_missing`; (b) a key whose public id differs from any id recorded on the
    organization's content or keyed ledger rows -> `org_key_mismatch`; else None.

    With `ring`, "no key but rows in use" is re-read once before the key is called lost: another
    task may have made the key and recorded a row between the caller's read and ours. If the
    key was shredded in that window the answer is `KeyDestroyedError`, not "missing".

    Returns `(problem, key)`: `key` is the key that was verified (the one passed in, or the one
    the re-read found). A caller must use exactly that key and never call `master(create=True)`
    after any read found one (a key lost in between would be replaced by a new one over its
    rows). `key` is None only when there is no key and no key material in use."""
    if tombstoned:
        return None, master
    if master is None:
        if not reader.org_key_material_in_use(org_id):
            return None, None
        if ring is not None:
            master = ring.master(org_id)
            if master is None and (
                reader.key_tombstone(org_id) is not None or ring.tombstone(org_id) is not None
            ):
                return KeyDestroyedError(org_id), None
        if master is None:
            return OrphanedContentError(org_id), None
    recorded = reader.org_keyed_ledger_key_ids(org_id) | reader.org_content_key_ids(org_id)
    mine = key_id(master)
    if any(found != mine for found in recorded):
        return OrphanedContentError(org_id, mismatch=True), master
    return None, master


_WRAP_PREFIX = b"nmk1:"


def _check_kek(kek: bytes) -> None:
    if len(kek) != KEY_BYTES:
        raise ContentError(f"a key-encryption key is exactly {KEY_BYTES} bytes")


def _kek_id(kek: bytes) -> str:
    """A public fingerprint of a key-encryption key (reveals nothing usable)."""
    return hkdf(kek, "nuramesh key wrap id v1", 8).hex()


def _wrap_master(master: bytes, kek: bytes, store_id: str, org_id: str) -> bytes:
    """`nmk1:<kek id>:<base64 nonce+ciphertext>`; AES-256-GCM, bound to the store and organization
    so a wrapped key can't be moved to another."""
    nonce = secrets.token_bytes(12)
    aad = f"{store_id}|{org_id}|{_kek_id(kek)}".encode()
    sealed = _aesgcm(hkdf(kek, "nuramesh key wrap v1")).encrypt(nonce, master, aad)  # type: ignore[attr-defined]
    return _WRAP_PREFIX + _kek_id(kek).encode() + b":" + base64.b64encode(nonce + sealed) + b"\n"


def _parse_wrapped(raw: bytes) -> tuple[str, bytes]:
    parts = raw.strip().split(b":")
    if (
        len(parts) != 3
        or parts[0] + b":" != _WRAP_PREFIX
        or not re.fullmatch(rb"[0-9a-f]{16}", parts[1])
    ):
        raise ValueError("not a wrapped key")
    return parts[1].decode("ascii"), base64.b64decode(parts[2], validate=True)


def _open_master(blob: bytes, kek: bytes, store_id: str, org_id: str) -> bytes | None:
    if len(blob) != 12 + KEY_BYTES + 16:
        return None
    aad = f"{store_id}|{org_id}|{_kek_id(kek)}".encode()
    try:
        key: bytes = _aesgcm(
            hkdf(kek, "nuramesh key wrap v1")
        ).decrypt(  # type: ignore[attr-defined]
            blob[:12], blob[12:], aad
        )
    except Exception:
        return None
    return key if len(key) == KEY_BYTES else None


def _aesgcm(key: bytes) -> object:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - exercised when the extra is missing
        raise ContentExtraMissing(
            "content encryption needs the extra: pip install 'nuramesh[content]'"
        ) from exc
    return AESGCM(key)


def _associated(org_id: str, kind: ContentKind, content_id: str) -> bytes:
    return f"{org_id}|{kind.value}|{content_id}".encode()


class _AlreadyTombstoned(Exception):
    """Internal: the tombstone insert lost a race; rolls the shred's entry back."""


class _NothingToAdopt(Exception):
    """Internal: another store took the flat key; rolls the adoption's entry back."""


@dataclass(frozen=True)
class ContentInfo:
    """What `ContentService.describe` shows: no text, no ciphertext."""

    kind: ContentKind
    expires_at: datetime | None


CLAMP_FAILURE_STREAM = "org:system-content-clamp"
ERASE_FAILURE_STREAM = "org:system-content-erase"
SCRUB_ACTOR = "system:erasure-scrub"


@dataclass(frozen=True)
class RewrapReport:
    """What `ContentService.rewrap_keys` did, by organization id."""

    rewrapped: list[str]
    already: list[str]
    skipped: list[str]
    kek_id: str


class ContentService:
    """Capture, reveal, expire, shred and hold content for any `Store`.

    `hosted=True` enforces ND25's hosted maximum (90 days, never "keep forever") for ordinary
    content; a person's reason follows its own retention (`REASON_DAYS`).
    """

    def __init__(
        self,
        store: Store,
        keys: KeyRing,
        *,
        clock: Callable[[], datetime] | None = None,
        hosted: bool = False,
        reason_days: int = REASON_DAYS,
    ) -> None:
        if reason_days < REASON_DAYS:
            raise ContentError(
                f"reason_days can only raise the 13-month default of {REASON_DAYS} days"
            )
        if reason_days > MAX_REASON_DAYS:
            raise ContentError(f"reason_days must be at most {MAX_REASON_DAYS}")
        self.store, self.keys, self.hosted = store, keys, hosted
        self._clock = clock or store.now  # the store's clock (ND243)
        self._swept = False
        self.reason_days = reason_days
        self.capture_failing = False  # set by a capture that failed for a reason other than a
        # destroyed key, cleared by the next key use that works (the health warning, 8.5)
        self._alerts: list[tuple[str, str]] = []
        self.ledger_hashing = LedgerHashing(store, keys, service=self)
        self.key_erasure: Callable[[str, str], bool] | None = None
        """R1.14c: `(org_id, actor) -> keys_pending`, run after a shred commits (spec 6 step 7)."""

    # -- the key gate (R1.37, section 7) ------------------------------------------------------

    def _master_for_capture(self, org_id: str, uow: UnitOfWork) -> bytes:
        """The one place a key may be made: the slug, the store tombstone, the ring tombstone, the
        existing key, the orphan guard, then `master(create=True)`."""
        try:
            _org(org_id)
            if uow.key_tombstone(org_id) is not None:
                raise KeyDestroyedError(org_id)
            if self.keys.tombstone(org_id) is not None:
                raise KeyDestroyedError(org_id)
            master = self.keys.master(org_id)
            problem, master = key_presence_check(uow, org_id, master, False, self.keys)
            if problem is not None:
                raise problem
            if master is None:  # no key and no key material in use: the only place to make one
                master = self.keys.master(org_id, create=True)
                if master is not None:  # a key lost since the read must not be replaced unseen
                    problem, master = key_presence_check(uow, org_id, master, False, None)
                    if problem is not None:
                        raise problem
            if master is None:  # pragma: no cover - create=True always returns a key
                raise ContentError(f"no key for organization '{org_id}'")
        except ContentError as exc:
            self._note_failure(org_id, exc)
            raise
        self.capture_failing = False
        return master

    def retired(self, org_id: str) -> KeyTombstone | None:
        """The tombstone that retires this organization id (the store's, else the ring's), or None.
        An unreadable ring tombstone raises."""
        _org(org_id)
        found = self.store.key_tombstone(org_id)
        return found if found is not None else self.keys.tombstone(org_id)

    def require_not_retired(self, org_id: str) -> None:
        """Raise `OrgIdRetiredError` if the id was erased (a run must start under a new id)."""
        found = self.retired(org_id)
        if found is not None:
            raise OrgIdRetiredError(org_id, found.destroyed_at, approximate=found.at_approximate)

    def require_key_present(self, org_id: str) -> None:
        """Read-only start check (deny-first): raise `KeyDestroyedError` if the organization's key
        was destroyed (a tombstone in either copy), `OrphanedContentError` if it has no key but
        still has content rows. Never makes a key, a folder or a ring file; an unreadable
        tombstone raises its own refusal."""
        _org(org_id)
        self.ledger_hashing.reset_cache()  # the query wins at every run start (R5)
        if self.retired(org_id) is not None:
            raise KeyDestroyedError(org_id)
        problem = key_presence_problem(
            self.store, org_id, self.keys.master(org_id), False, self.keys
        )
        if problem is not None:
            raise problem

    def _master_for_use(self, org_id: str) -> bytes | None:
        """The key for reading or hashing, never creating one. A tombstoned organization has none
        (None, as for any destroyed key); an unreadable tombstone raises, never None."""
        try:
            _org(org_id)
            if self.store.key_tombstone(org_id) is not None:
                return None
            if self.keys.tombstone(org_id) is not None:
                return None
            master = self.keys.master(org_id)
        except ContentError as exc:
            self._note_failure(org_id, exc)
            raise
        if master is not None:
            self.capture_failing = False
        return master

    # -- operational alerts and the refusal record (8, 8.5) ------------------------------------

    def _note_failure(self, org_id: str, exc: ContentError) -> None:
        """A capture that fails for a reason other than a destroyed key needs a person: set the
        health flag and queue the `content.capture_failed` row (written once no transaction is
        open: `flush_alerts`)."""
        if exc.code in (KEY_MISSING, KEY_MISMATCH, TOMBSTONE_UNAVAILABLE) and _SLUG.fullmatch(
            org_id
        ):
            self.capture_failing = True
            self._alerts.append((org_id, exc.code))

    def warnings(self) -> list[str]:
        """Fixed strings for `GET /v1/health`: empty when there is none."""
        return ["content_capture_failing"] if self.capture_failing else []

    def flush_alerts(self) -> None:
        """Write the queued `content.capture_failed` rows (best effort; kept for later if a
        transaction is still open)."""
        pending, self._alerts = self._alerts, []
        for index, (org_id, reason) in enumerate(pending):
            try:
                self.record_security_count(org_id, "content.capture_failed", reason)
            except NestedTransactionError:
                self._alerts = pending[index:] + self._alerts
                return

    def record_security_count(self, org_id: str, kind: str, reason: str) -> int | None:
        """Add one to the coalesced security-log row of (organization, UTC day, reason), in its own
        small transaction. Best effort: a store failure is logged and the caller's answer is
        unchanged. Never records a route, a reason text or a person."""
        try:
            now = self._clock()
            entry = SecurityLogEntry.new(
                org_id, now, kind, actor="system", details={"reason": reason, "count": 1}
            ).model_copy(update={"id": coalesced_row_id(kind, org_id, now, reason)})
            return self.store.upsert_security_log_count(entry)
        except NestedTransactionError:
            raise
        except Exception:
            _LOG.warning("could not record %s for %s", kind, org_id, exc_info=True)
            return None

    def reason_retention_days(self) -> int:
        """How long a person's reason is kept here: the organization's `content.reason_days`
        (default `REASON_DAYS`), never more than `HOSTED_MAX_DAYS` on a hosted deployment."""
        days = self.reason_days if self.reason_days >= REASON_DAYS else REASON_DAYS  # R11b
        return min(days, HOSTED_MAX_DAYS) if self.hosted else days

    def capture_reason(
        self, uow: UnitOfWork, org_id: str, text: str, *, run_id: str | None = None
    ) -> str:
        """Keep a person's reason (kind `reason`) for `reason_retention_days`; every path that
        stores one calls this, so the hosted cap cannot be skipped (ND195 b). A decision of a
        durable run passes its `run_id` so the row is tagged with the people it names."""
        content_id = self.capture(
            uow,
            org_id,
            ContentKind.REASON,
            text,
            retention_days=self.reason_retention_days(),
            run_id=run_id,
        )
        if self.hosted:  # a reason saved before (longer kept) is capped too (ND202)
            uow.shorten_content_expiry(
                ContentKind.REASON, HOSTED_MAX_DAYS, org_id=org_id, content_id=content_id
            )
        return content_id

    def config_retention_days(self) -> int:
        """How long the E13 configuration content is kept here: `CONFIG_DAYS`, never more than
        `HOSTED_MAX_DAYS` on a hosted deployment (ND213)."""
        return min(CONFIG_DAYS, HOSTED_MAX_DAYS) if self.hosted else CONFIG_DAYS

    def capture_config(self, uow: UnitOfWork, org_id: str, text: str) -> str:
        """Keep E13 configuration content (kind `config`) for `config_retention_days`. Unlike a
        reason it is not re-clamped from `created_at` here: the re-capture sweep renews the same
        record on purpose (ND194). The hosted clamp (`clamp_hosted_config`, ND218) caps the current
        version from the sweep time, never from `created_at`, so it does not undo a re-capture."""
        return self.capture(
            uow, org_id, ContentKind.CONFIG, text, retention_days=self.config_retention_days()
        )

    def clamp_hosted_reasons(self) -> int:
        """On a hosted deployment, cap every existing reason at `HOSTED_MAX_DAYS` from when it
        was saved (ND202): the sweep that runs when a deployment switches to hosted. It only
        shortens, is safe to repeat (nothing left to change writes nothing), and records one
        `decision.recorded` `reasons_clamped` entry per organization (system actor, counts only).
        Returns how many reasons were shortened; 0 when not hosted."""
        if not self.hosted:
            return 0
        with self.store.transaction() as uow:
            changed = uow.shorten_content_expiry(ContentKind.REASON, HOSTED_MAX_DAYS)
            for org_id, count in sorted(changed.items()):
                uow.append(
                    EventDraft(
                        stream=f"org:{org_id}",
                        type=EventType.DECISION_RECORDED,
                        actor="system",
                        payload={
                            "kind": "reasons_clamped",
                            "count": count,
                            "max_days": HOSTED_MAX_DAYS,
                        },
                    )
                )
        return sum(changed.values())

    def clamp_hosted_config(self) -> int:
        """On a hosted deployment, cap the configuration content already stored (ND218): an OLD
        version (not the content of the organization's latest `config_loaded` entry) at
        `HOSTED_MAX_DAYS` from when it was saved; the CURRENT version at `HOSTED_MAX_DAYS` from
        now, because a re-capture renews the same record in place (it has no new save time) and
        the sweep must not undo one. Only shortens, so a re-run writes nothing; an organization
        on legal hold is left alone. One `decision.recorded` `config_clamped` entry per
        organization (system actor, counts only). Returns how many were shortened; 0 when not
        hosted."""
        if not self.hosted:
            return 0
        now = self._clock().astimezone(UTC)
        records = self.store.expiring_content(
            ContentKind.CONFIG, datetime(1970, 1, 1, tzinfo=UTC), datetime(9000, 1, 1, tzinfo=UTC)
        )
        total = 0
        with self.store.transaction() as uow:
            current: dict[str, str | None] = {}
            changed: dict[str, int] = {}
            for record in records:
                org_id = record.org_id
                if org_id not in current:
                    current[org_id] = self._current_config_id(uow, org_id)
                if uow.legal_hold(org_id):
                    continue
                done = uow.shorten_content_expiry(
                    ContentKind.CONFIG,
                    HOSTED_MAX_DAYS,
                    org_id=org_id,
                    content_id=record.id,
                    since=now if record.id == current[org_id] else None,
                )
                for org, count in done.items():
                    changed[org] = changed.get(org, 0) + count
            for org_id, count in sorted(changed.items()):
                uow.append(
                    EventDraft(
                        stream=f"org:{org_id}",
                        type=EventType.DECISION_RECORDED,
                        actor="system",
                        payload={
                            "kind": "config_clamped",
                            "count": count,
                            "max_days": HOSTED_MAX_DAYS,
                        },
                    )
                )
                total += count
        return total

    def scrub_person_config(self, uow: UnitOfWork, org_id: str, user_id: str) -> int:
        """Subject erasure of the stored configuration content (ND218): blank the operator free
        text linked to the erased person, in every stored version (the organization's
        `config_loaded` entries name them). The link: a policy that lists the person's id
        (`human:u_...`, the user id) under `approvers` has the `reason` of each of its rules
        blanked, since the operator wrote it for the people who answer that policy. The approver
        id stays (pseudonymous; the person's identifier key is gone), and so do the content id,
        its times and its kind; the ledger's `config_hash` does not cover `reason`, so it is
        unchanged. Run inside the erasure's transaction; appends one `config_scrubbed` entry (system
        actor, count only).
        Nothing happens for a shredded organization (its content is already unreadable).
        Returns how many stored versions were rewritten."""
        _org(org_id)
        master = self._master_for_use(org_id)
        if master is None or uow.key_tombstone(org_id) is not None:
            return 0
        person = user_id
        stream = f"org:{org_id}"
        events = [
            e
            for _pos, e in uow.autonomy_events(
                org_id, {KIND_CONFIG, KIND_CONFIG_SWAP}, streams="org"
            )
            if e.stream == stream
        ]
        swaps = swapped_config_content(events)
        ids: list[str] = []
        for event in events:
            kept = event.payload.get("config_content")
            if decision_kind(event) == KIND_CONFIG and isinstance(kept, str):
                kept = current_config_content(swaps, kept)  # a version scrubbed before
                if kept not in ids:
                    ids.append(kept)
        done = 0
        for content_id in ids:
            record = uow.get_content(org_id, content_id)
            if record is None or record.kind is not ContentKind.CONFIG:
                continue
            try:
                text = self.read_in(uow, org_id, content_id, actor=SCRUB_ACTOR, why="erasure_scrub")
            except ContentError:
                _LOG.warning("could not read a stored configuration to scrub it", exc_info=True)
                continue
            if text is None:
                continue
            scrubbed = _blank_reasons_of(text, person)
            if scrubbed is None:
                continue
            # ND220: the cleaned text is NEW content (its own keyed id); the old record is deleted
            # (its ciphertext is gone, unreadable for good) and the swap is ledgered
            new_id = keyed_hash(master, org_id, ContentKind.CONFIG, scrubbed)
            existing = uow.get_content(org_id, new_id)
            # the ledgered expiry is the STORED record's (ND234 minor): when another version
            # already scrubbed to the same text it is that record's, not this version's
            expires = existing.expires_at if existing is not None else record.expires_at
            if existing is None:
                nonce = secrets.token_bytes(12)
                cipher = _aesgcm(encryption_key(master))
                sealed = cipher.encrypt(  # type: ignore[attr-defined]
                    nonce, scrubbed.encode("utf-8"), _associated(org_id, record.kind, new_id)
                )
                uow.put_content(
                    record.model_copy(
                        update={
                            "id": new_id,
                            "nonce": nonce,
                            "ciphertext": sealed,
                            "size": len(scrubbed.encode()),
                        }
                    )
                )
            uow.delete_content(org_id, content_id)
            uow.append(
                EventDraft(
                    stream=stream,
                    type=EventType.DECISION_RECORDED,
                    actor="system",
                    payload={
                        "kind": KIND_CONFIG_SWAP,
                        "old_content": content_id,
                        "new_content": new_id,
                        "reason": "subject erasure",
                        "expires_at": format_time(expires) if expires is not None else None,
                    },
                )
            )
            done += 1
        if done:
            uow.append(
                EventDraft(
                    stream=stream,
                    type=EventType.DECISION_RECORDED,
                    actor="system",
                    payload={"kind": "config_scrubbed", "count": done},
                )
            )
        return done

    @staticmethod
    def _current_config_id(uow: UnitOfWork, org_id: str) -> str | None:
        """The content id named by the organization's latest `config_loaded` entry, or None."""
        stream = f"org:{org_id}"
        found: str | None = None
        events = [
            e
            for _pos, e in uow.autonomy_events(
                org_id, {KIND_CONFIG, KIND_CONFIG_SWAP}, streams="org"
            )
            if e.stream == stream
        ]
        for event in events:
            if decision_kind(event) == KIND_CONFIG:
                kept = event.payload.get("config_content")
                found = kept if isinstance(kept, str) else None
        if found is None:
            return None
        return current_config_content(swapped_config_content(events), found)

    def capture(
        self,
        uow: UnitOfWork,
        org_id: str,
        kind: ContentKind,
        text: str,
        *,
        retention_days: int | None,
        run_id: str | None = None,
        subjects: Collection[str] | None = None,
    ) -> str:
        """Encrypt `text` and write it through the caller's unit of work. Returns its keyed hash:
        put that in the ledger entry written in the same transaction.

        `retention_days` is how long it is kept (at least 1), or `None` to keep it until the
        organization removes it. Capturing the same reason again extends the expiry, never shortens;
        a still-live task output or gate evidence keeps its first expiry (ND55).

        `run_id` marks a row of a durable run: in the same transaction it is tagged with the
        people it names (R1.32 S1), so erasing one of them deletes it. `subjects` replaces the
        computed set (a caller that already knows it)."""
        content_id = self._capture_row(uow, org_id, kind, text, retention_days=retention_days)
        if run_id is not None or subjects is not None:
            found = (
                frozenset(subjects)
                if subjects is not None
                else self.subjects_for(uow, org_id, run_id or "", text)
            )
            uow.add_content_subjects(org_id, content_id, run_id, found)
        return content_id

    def subjects_for(
        self, uow: UnitOfWork, org_id: str, run_id: str, *texts: str
    ) -> frozenset[str]:
        """The people a row of this run names (S1): the ids in the stored text of every task of the
        run known now (title, instructions, definition of done), the ids in the row's own text and
        every `human:` actor on the run's stream so far. Agents, `system` and API keys never are.
        A person already erased is dropped by the store when the tags are written (S6)."""
        every = list(texts)
        for stored in uow.tasks_for_run(run_id):
            task = stored.task
            if task.text_ref is not None:
                # an API plan: its text is in the content store (R1.20 PR B, ND347 4.3, R45);
                # a row already gone took its subjects with it
                every += self._stored_task_text(uow, org_id, task)
                continue
            every += [task.title, task.instructions, task.definition_of_done.model_dump_json()]
        found = set(human_subjects(*every))
        stream, after = f"run:{run_id}", 0
        while True:
            batch = uow.events_after(stream, after, 500)
            if not batch:
                break
            for event in batch:
                found |= human_subjects(event.actor)
            after = batch[-1].seq
        return frozenset(found)

    def _stored_task_text(self, uow: UnitOfWork, org_id: str, task: Task) -> list[str]:
        """The plan text of an API task that is still readable (inside the caller's transaction)."""
        ref = task.text_ref
        if ref is None:
            return []
        found: list[str] = []
        for content_id in ref.ids:
            record = uow.get_content(org_id, content_id)
            if record is None or not self._is_live(org_id, record):
                continue
            stored = self._open(org_id, record)
            if stored is None:
                continue
            if record.kind is ContentKind.TASK_TEMPLATE:
                found.append(stored)
                continue
            text = read_plan_text(stored, task.run_id)
            if text is not None:
                found.append(text)
        return found

    def read_plan_text(self, org_id: str, content_id: str, run_id: str) -> str | None:
        """The text of one plan-text row of this run (the envelope unwrapped, no ledger entry:
        the engine's own read, as for task outputs). None if it is gone: erased, expired, its key
        lost, or not this run's row."""
        stored = self.read_unledgered(org_id, content_id)
        return None if stored is None else read_plan_text(stored, run_id)

    def read_template(self, org_id: str, content_id: str) -> tuple[str, str] | None:
        """`(title, definition of done)` of an org task template, or None if it is gone or not a
        template. The stored text is JSON `{"title", "definition_of_done"}`."""
        _org(org_id)
        record = self.store.get_content(org_id, content_id)
        if record is None or record.kind is not ContentKind.TASK_TEMPLATE:
            return None
        stored = self.read_unledgered(org_id, content_id)
        if stored is None:
            return None
        try:
            data = json.loads(stored)
            return str(data["title"]), str(data["definition_of_done"])
        except (ValueError, KeyError, TypeError):
            return None

    def extend_plan_text(
        self, uow: UnitOfWork, org_id: str, content_ids: Iterable[str], *, at: datetime
    ) -> int:
        """RET-PT: lengthen the expiry of plan-text rows to `max(current, at)`; never shortens.
        On a runner's claim and each time a task enters a wait (`at` = then + 90 days). The hosted
        cap `HOSTED_MAX_DAYS` bounds a per-setting retention at capture, not this extension.
        Returns the rows changed."""
        _org(org_id)
        changed = 0
        for content_id in sorted(set(content_ids)):
            record = uow.get_content(org_id, content_id)
            if record is None or record.kind is not ContentKind.PLAN_TEXT:
                continue
            if record.expires_at is None or record.expires_at >= at:
                continue
            if not self._is_live(org_id, record):
                continue  # already past its expiry: an extension never revives a row
            changed += int(uow.set_run_content_expiry(org_id, content_id, at))
        return changed

    def set_plan_text_expiry(
        self, uow: UnitOfWork, org_id: str, content_ids: Iterable[str], *, at: datetime
    ) -> int:
        """RET-PT (b): a refused or aborted start request's plan text expires exactly at `at`
        (7 days after the refusal), shorter or longer than before. Returns the rows changed."""
        _org(org_id)
        changed = 0
        for content_id in sorted(set(content_ids)):
            record = uow.get_content(org_id, content_id)
            if record is None or record.kind is not ContentKind.PLAN_TEXT:
                continue
            changed += int(uow.set_run_content_expiry(org_id, content_id, at))
        return changed

    def tag_run_rows(self, uow: UnitOfWork, org_id: str, run_id: str, subject: str) -> int:
        """S2: a person who decides on a round joins every live row of the run that the run's own
        entries name (`evidence_hash`, `feedback_hash`, `output_hash`), in the caller's
        transaction. Returns the tags added."""
        stream, after, added = f"run:{run_id}", 0, 0
        seen: set[str] = set()
        while True:
            batch = uow.events_after(stream, after, 500)
            if not batch:
                break
            for event in batch:
                for name in ("evidence_hash", "feedback_hash", "output_hash"):
                    value = event.payload.get(name)
                    if isinstance(value, str) and value not in seen:
                        seen.add(value)
                        added += uow.add_content_subjects(org_id, value, run_id, [subject])
            after = batch[-1].seq
        return added

    def _capture_row(
        self,
        uow: UnitOfWork,
        org_id: str,
        kind: ContentKind,
        text: str,
        *,
        retention_days: int | None,
    ) -> str:
        _org(org_id)
        if retention_days is not None and retention_days < 1:
            raise ContentError("retention_days must be at least 1 (0 means: don't capture)")
        if self.hosted:
            if retention_days is None or retention_days > HOSTED_MAX_DAYS:
                raise ContentError(f"hosted content is kept at most {HOSTED_MAX_DAYS} days")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ContentError(f"content is over {MAX_TEXT_BYTES} bytes")
        master = self._master_for_capture(org_id, uow)
        content_id = keyed_hash(master, org_id, kind, text)
        nonce = secrets.token_bytes(12)
        cipher = _aesgcm(encryption_key(master))
        plain = text.encode("utf-8")
        ciphertext = cipher.encrypt(nonce, plain, _associated(org_id, kind, content_id))  # type: ignore[attr-defined]
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ContentError("the clock needs a time zone")  # never assume host local time
        now = now.astimezone(UTC)  # expiry is UTC arithmetic, never wall-clock (ND188)
        existing = self.store.get_content(org_id, content_id)
        if existing is not None:
            if self._is_live(org_id, existing):
                if kind not in (ContentKind.REASON, ContentKind.CONFIG):
                    # a durable step replayed (same text, same attempt): the working expiry is not
                    # renewed (ND55); the first record keeps its clock
                    return content_id
            elif uow.delete_if_expired(org_id, content_id, now):
                # past its expiry and not held: the old record is gone (not revived by the new
                # capture's later expiry), and the ledger says so like the expiry job does
                uow.append(
                    EventDraft(
                        stream=f"org:{org_id}",
                        type=EventType.CONTENT_EXPIRED,
                        actor="system",
                        payload={
                            "count": 1,
                            "kinds": [existing.kind.value],
                            "oldest_expiry": format_time(existing.expires_at or now),
                        },
                    )
                )
        uow.put_content(
            ContentRecord(
                org_id=org_id,
                id=content_id,
                kind=kind,
                key_id=key_id(master),
                nonce=nonce,
                ciphertext=ciphertext,
                size=len(plain),
                created_at=now,
                expires_at=None if retention_days is None else now + timedelta(days=retention_days),
            )
        )
        return content_id

    def capture_task_output(
        self, uow: UnitOfWork, org_id: str, output: Any, *, run_id: str, task_id: str, attempt: int
    ) -> str:
        """Keep a task's output for as long as its task is alive (`WORKING_DAYS`). The stored text
        is canonical JSON of `{"run_id", "task_id", "attempt", "output"}` (the `MAX_TEXT_BYTES` cap
        counts the envelope), so two tasks (or two attempts) with identical outputs never share a
        record: finishing one can't shorten the other's clock. A reader unwraps `output`.
        Written through the caller's unit of work, in the same transaction as the ledger entry that
        names the returned keyed hash. A value that isn't JSON, or is over `MAX_TEXT_BYTES`, is
        refused with `ContentError` (never truncated)."""
        try:
            wrapped = {"run_id": run_id, "task_id": task_id, "attempt": attempt, "output": output}
            text = canonical_json_jcs(wrapped).decode("utf-8")  # decimals: RFC 8785 (ND78)
        except (TypeError, ValueError, RecursionError) as exc:  # too deep is "not JSON" too
            raise ContentError("a task output must be JSON to be kept") from exc
        return self.capture(
            uow, org_id, ContentKind.TASK_OUTPUT, text, retention_days=WORKING_DAYS, run_id=run_id
        )

    def capture_gate_feedback(
        self,
        uow: UnitOfWork,
        org_id: str,
        feedback: Sequence[str],
        *,
        run_id: str,
        task_id: str,
        attempt: int,
    ) -> str:
        """Keep the gate feedback the next attempt of a task starts with (kind `gate_evidence`,
        ND57), as canonical JSON of `{"run_id", "task_id", "attempt", "feedback"}`: a crash between
        "another attempt" and the worker's first call no longer loses it. Same working expiry and
        final retention as the task's outputs (`finish_task_output`). Returns the keyed hash."""
        wrapped: dict[str, Any] = {
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "feedback": list(feedback),
        }
        text = canonical_json(wrapped).decode("utf-8")
        return self.capture(
            uow,
            org_id,
            ContentKind.GATE_EVIDENCE,
            text,
            retention_days=WORKING_DAYS,
            run_id=run_id,
        )

    @staticmethod
    def gate_evidence_text(
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        submission: int,
        gate: str,
        result: GateResult,
    ) -> str:
        """The stored text of a gate result (R1.32 G2): canonical JSON of the round, the gate and
        the result, so no record is shared by two runs, tasks, rounds or gates."""
        wrapped: dict[str, Any] = {
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "submission": submission,
            "gate": gate,
            "result": result.model_dump(mode="json"),
        }
        return canonical_json(wrapped).decode("utf-8")

    def capture_gate_result(
        self,
        uow: UnitOfWork,
        org_id: str,
        result: GateResult,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        submission: int,
        gate: str,
    ) -> str:
        """Keep a passed result of a required non-human gate while a person decides (R1.32, ND79,
        kind `gate_evidence`), so the round completes without running the verifiers again. Same
        working expiry and final retention as the task output. Over `MAX_TEXT_BYTES` of canonical
        text: `ContentError`, never truncated. Returns the keyed content id (the ledger's
        `evidence_hash`)."""
        text = self.gate_evidence_text(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            submission=submission,
            gate=gate,
            result=result,
        )
        return self.capture(
            uow,
            org_id,
            ContentKind.GATE_EVIDENCE,
            text,
            retention_days=WORKING_DAYS,
            run_id=run_id,
        )

    def capture_human_decision(
        self,
        uow: UnitOfWork,
        org_id: str,
        decision: Mapping[str, Any],
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        submission: int,
        gate_id: str,
    ) -> str:
        """Keep a person's decision on a human gate of a durable run: who decided and, per
        criterion, the verdict and the written reason. A person's reason is kept as every other
        one is (T47): kind `reason`, `REASON_DAYS` (13 months, ND28 D), erasable; no new rule. The
        task may wait days for it, so the text lives here (never in the ledger or in Temporal) and
        the gate is run again with it when the run goes on. Returns the keyed hash."""
        text = self._decision_text(decision, run_id, task_id, attempt, submission, gate_id)
        return self.capture_reason(uow, org_id, text, run_id=run_id)

    @staticmethod
    def _decision_text(
        decision: Mapping[str, Any],
        run_id: str,
        task_id: str,
        attempt: int,
        submission: int,
        gate_id: str,
    ) -> str:
        wrapped: dict[str, Any] = {
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "submission": submission,
            "gate": gate_id,
            **decision,
        }
        return canonical_json(wrapped).decode("utf-8")

    def human_decision_hash(
        self,
        org_id: str,
        decision: Mapping[str, Any],
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        submission: int,
        gate_id: str,
    ) -> str | None:
        """The keyed hash `capture_human_decision` would return for this decision, without storing
        anything (a replayed request is recognised by it). None if the organization has no key."""
        text = self._decision_text(decision, run_id, task_id, attempt, submission, gate_id)
        return self.hash_of(org_id, ContentKind.REASON, text)

    def finish_task_output(
        self, uow: UnitOfWork, org_id: str, content_id: str, *, retention_days: int
    ) -> bool:
        """The task is final: its output now expires `retention_days` from now (0: right away, so
        the next `expire` deletes it and records `content.expired`). This may **shorten** the
        working expiry, and only for a task output (`set_run_content_expiry`). True if it was
        set; a legal hold still stops the deletion itself (`expire`).

        **A record already past its expiry is left alone** (ND55: the working expiry is not
        renewed): an earlier attempt's output that outlived its 90 days while the task waited, and
        that no expiry pass had deleted yet, must not get a new lease from the task finishing. The
        next `expire` pass deletes it. (Read through the service clock, so all stores behave the
        same; a record is only ever past its expiry once, so the unlocked read is safe.)"""
        _org(org_id)
        if retention_days < 0:
            raise ContentError("retention_days can't be negative")
        if self.hosted and retention_days > HOSTED_MAX_DAYS:
            raise ContentError(f"hosted content is kept at most {HOSTED_MAX_DAYS} days")
        now = self._clock()
        current = self.store.get_content(org_id, content_id)
        if current is not None and current.expires_at is not None and current.expires_at <= now:
            return False
        return uow.set_run_content_expiry(org_id, content_id, now + timedelta(days=retention_days))

    def shorten_unfinished_output(
        self, org_id: str, content_id: str, *, retention_days: int
    ) -> bool:
        """RET-WIRE R10: a task that ended the run without being final (`needs_human`, `blocked`):
        its record now expires `retention_days` from now, in a transaction of its own. Never
        lengthens (`min` with the working expiry, so it is idempotent), leaves a record already
        past its expiry alone (the expiry pass deletes it) and does not touch a legal hold: the
        hold stops the deletion, not the shortening. True if the expiry changed."""
        _org(org_id)
        if retention_days < 0:
            raise ContentError("retention_days can't be negative")
        if self.hosted and retention_days > HOSTED_MAX_DAYS:
            raise ContentError(f"hosted content is kept at most {HOSTED_MAX_DAYS} days")
        now = self._clock()
        current = self.store.get_content(org_id, content_id)
        if current is None or current.expires_at is None or current.expires_at <= now:
            return False
        target = now + timedelta(days=retention_days)
        if target >= current.expires_at:
            return False
        with self.store.transaction() as uow:
            return uow.set_run_content_expiry(org_id, content_id, target)

    def key_overrides(
        self, org_id: str, overrides: "tuple[PriceOverride, ...]"
    ) -> "tuple[PriceOverride, ...]":
        """The overrides with their reasons keyed (the ledger records only that hash, T47). The
        reason text stays in the owner's config file; makes the organization's key if needed."""
        if not overrides:
            return overrides
        with self.store.transaction() as uow:
            master = self._master_for_capture(_org(org_id), uow)
        return tuple(
            o.with_reason_hash(keyed_hash(master, org_id, ContentKind.REASON, o.reason))
            for o in overrides
        )

    def ledger_hash_of(self, org_id: str, kind: LedgerHashKind, data: bytes) -> str | None:
        """The keyed ledger hash of `data` (to confirm "was this the prompt of row N?"), without
        storing anything; library only. None when the key was destroyed (nothing can be
        confirmed, by design). A loaded key that is not the one the organization's rows were
        made with raises `OrphanedContentError` (PH-KEY R5, caller 5)."""
        master = self._master_for_use(org_id)
        if master is None:
            return None
        problem = key_presence_problem(self.store, org_id, master, False, self.keys)
        if problem is not None:
            raise problem
        return ledger_hash(master, org_id, kind, data)

    def has_kind(self, uow: UnitOfWork, org_id: str, content_id: str, kind: ContentKind) -> bool:
        """Whether the organization holds a record of this kind under this id (no decryption)."""
        found = uow.get_content(org_id, content_id)
        return found is not None and found.kind is kind

    def hash_in(self, uow: UnitOfWork, org_id: str, kind: ContentKind, text: str) -> str:
        """The keyed hash of `text` in the caller's transaction, creating the organization's key
        if it has none (as a capture would). Nothing is stored: used to make a plan hash and to
        know a content id before deciding whether to write it."""
        return keyed_hash(self._master_for_capture(org_id, uow), org_id, kind, text)

    def hash_of(self, org_id: str, kind: ContentKind, text: str) -> str | None:
        """The keyed hash of `text` without storing anything (to check a hash from the ledger)."""
        master = self._master_for_use(org_id)
        return None if master is None else keyed_hash(master, org_id, kind, text)

    def _is_live(self, org_id: str, record: ContentRecord) -> bool:
        """Not expired. Expired means `now >= expires_at` (the test `expire` deletes by), whether
        or not the expiry job has run; a legal hold stops expiry, so held content stays live."""
        return (
            record.expires_at is None
            or record.expires_at > self._clock()
            or self.store.legal_hold(org_id)
        )

    def unreadable_at(self, uow: UnitOfWork, org_id: str, content_id: str, at: datetime) -> bool:
        """True if the content cannot be read at `at`: absent, its key destroyed or replaced
        (shredded), or expired (`at >= expires_at`, day 396 itself counts; a legal hold stops
        expiry). Reads through `uow`, so a record written in the same transaction is seen.
        Used by the autonomy overlay (ND188); an error is raised, never False."""
        record = uow.get_content(org_id, content_id)
        if record is None:
            return True
        if uow.key_tombstone(org_id) is not None or self.keys.tombstone(org_id) is not None:
            return True
        master = self.keys.master(org_id)
        if master is None or key_id(master) != record.key_id:
            return True
        if record.expires_at is None or uow.legal_hold(org_id):
            return False
        return record.expires_at <= at

    def describe(self, org_id: str, content_id: str) -> ContentInfo | None:
        """Metadata only (kind and expiry, never text or bytes, no ledger entry), or None if the
        record is absent or expired (same rule as `read_unledgered`)."""
        _org(org_id)
        record = self.store.get_content(org_id, content_id)
        if record is None or not self._is_live(org_id, record):
            return None
        return ContentInfo(kind=record.kind, expires_at=record.expires_at)

    def read_unledgered(self, org_id: str, content_id: str) -> str | None:
        """Decrypt a record **without a ledger entry** (internal use: people read through `reveal`).
        None if it was never stored, has expired (`now >= expires_at`, even before the
        expiry job deletes it; not while a legal hold is on), its key was destroyed, or it was
        sealed under an older key (a key destroyed and made again);
        `ContentError` only if bytes were altered."""
        _org(org_id)
        record = self.store.get_content(org_id, content_id)
        if record is None:
            return None
        if not self._is_live(org_id, record):
            return None
        return self._open(org_id, record)

    def read_in(
        self, uow: UnitOfWork, org_id: str, content_id: str, *, actor: str, why: str
    ) -> str | None:
        """A system job reads content inside its own transaction (a re-capture sweep that decides
        and writes in one unit). Recorded like a person's read: one `content.read` entry (actor,
        hash and a reason from `SYSTEM_READ_REASONS`, never the content) in the same transaction,
        so the read and its entry stand or roll back together. None, and no entry, if the record
        is absent, expired, held-out or sealed under a destroyed key."""
        if why not in SYSTEM_READ_REASONS:
            raise ContentError(f"why must be one of {sorted(SYSTEM_READ_REASONS)}")
        if not actor.startswith("system:"):
            raise ContentError("only a system actor reads through read_in; people use reveal")
        _org(org_id)
        record = uow.get_content(org_id, content_id)
        if record is None or not self._is_live(org_id, record):
            return None
        text = self._open(org_id, record)
        if text is None:
            return None
        uow.append(
            EventDraft(
                stream=f"org:{org_id}",
                type=EventType.CONTENT_READ,
                actor=actor,
                payload={"content": content_id, "why": why},
            )
        )
        return text

    def _open(self, org_id: str, record: ContentRecord) -> str | None:
        master = self._master_for_use(org_id)
        if master is None or key_id(master) != record.key_id:
            return None  # crypto-shredded: the ciphertext is unreadable forever
        cipher = _aesgcm(encryption_key(master))
        try:
            plain = cipher.decrypt(  # type: ignore[attr-defined]
                record.nonce, record.ciphertext, _associated(org_id, record.kind, record.id)
            )
        except Exception as exc:  # cryptography raises InvalidTag, whose type we don't import
            raise ContentError("the stored content failed its integrity check") from exc
        return bytes(plain).decode("utf-8")

    def reveal(self, org_id: str, content_id: str, *, actor: str, why: str) -> str | None:
        """A person reads content: recorded in the organization's ledger (who and which hash,
        never the content; `why` is one of `READ_REASONS`, never free text), then decrypted.
        Whether the actor may do this is the caller's policy check (default deny, R1.22)."""
        if why not in READ_REASONS:
            raise ContentError(f"why must be one of {sorted(READ_REASONS)}")
        if not _HASH.fullmatch(content_id):
            raise ContentError("a content id is a 64-digit hash")
        text = self.read_unledgered(org_id, content_id)
        if text is None:
            return None
        with self.store.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=f"org:{org_id}",
                    type=EventType.CONTENT_READ,
                    actor=actor,
                    payload={"content": content_id, "why": why},
                )
            )
        return text

    def expire(self, *, batch: int = 500) -> int:
        """Delete every record whose time has passed (never for an organization on legal hold),
        recording one `content.expired` entry per organization and batch: counts, no content.
        Each delete re-checks expiry and hold inside its transaction, so a hold set or the same text
        captured again after the list was read still protects the record."""
        total = 0
        self._clamp_without_stopping_expiry()
        self._erase_deferred()
        while True:
            now = self._clock()
            due = self.store.expired_content(now, limit=batch)
            if not due:
                return total
            by_org: dict[str, list[ContentRecord]] = {}
            for record in due:
                by_org.setdefault(record.org_id, []).append(record)
            deleted_any = False
            with self.store.transaction() as uow:
                for org_id, records in sorted(by_org.items()):
                    deleted = [r for r in records if uow.delete_if_expired(org_id, r.id, now)]
                    if deleted:
                        deleted_any = True
                        uow.append(
                            EventDraft(
                                stream=f"org:{org_id}",
                                type=EventType.CONTENT_EXPIRED,
                                actor="system",
                                payload={
                                    "count": len(deleted),
                                    "kinds": sorted({r.kind.value for r in deleted}),
                                    "oldest_expiry": format_time(
                                        min(r.expires_at for r in deleted if r.expires_at)
                                    ),
                                },
                            )
                        )
                        total += len(deleted)
            if len(due) < batch or not deleted_any:
                return total  # a full batch of records that were all protected would loop forever

    def _erase_deferred(self) -> int:
        """Carry out the erasures a legal hold put off (R1.32 S4): rows with a subject row marked
        `erase_pending` go once their organization is no longer on hold. One `content.erased`
        entry per organization, `deferred`, actor `system`, each in its own transaction. A failure
        for one organization is logged at error level and recorded as a system ledger entry (the
        error class only, no text), then the others carry on; it never stops `expire()`.
        Returns the rows deleted."""
        try:
            with self.store.transaction() as uow:
                orgs = list(uow.pending_erasure_orgs())
        except Exception as exc:  # noqa: BLE001 - expiry must run whatever the store did
            self._erase_failed("", exc)
            return 0
        total = 0
        for org_id in orgs:
            try:
                total += self._erase_deferred_org(org_id)
            except Exception as exc:  # noqa: BLE001 - one organization never stops the rest
                self._erase_failed(org_id, exc)
        return total

    def _erase_deferred_org(self, org_id: str) -> int:
        with self.store.transaction() as uow:
            if uow.legal_hold(org_id):
                return 0
            count, kinds = uow.erase_pending_content(org_id)
            if count:
                uow.append(
                    EventDraft(
                        stream=f"org:{org_id}",
                        type=EventType.CONTENT_ERASED,
                        actor="system",
                        payload={
                            "scope": "subject",
                            "count": count,
                            "kinds": kinds,
                            "skipped": 0,
                            "deferred": True,
                            "actor": "system",
                        },
                    )
                )
            return count

    def _erase_failed(self, org_id: str, exc: Exception) -> None:
        _LOG.error(
            "deferred content erasure failed (%s); the rows stay marked and the next pass retries",
            type(exc).__name__,
        )
        try:
            with self.store.transaction() as uow:
                uow.append(
                    EventDraft(
                        stream=ERASE_FAILURE_STREAM,
                        type=EventType.DECISION_RECORDED,
                        actor="system",
                        payload={
                            "kind": "content_erase_deferred_failed",
                            "error": type(exc).__name__,
                            "org": org_id,
                        },
                    )
                )
        except Exception:  # noqa: BLE001, S110 - the record is best effort, the log line stands
            pass

    def _clamp_without_stopping_expiry(self) -> None:
        """The hosted clamp inside `expire()`: a failure there never stops the deletes. It is
        recorded as a system ledger entry (the error class and the setting, no text, no message);
        if even that cannot be written, expiry still carries on. Once a process has swept
        successfully it does not rescan (every later hosted save is capped as it is made)."""
        if not self.hosted or self._swept:
            return
        ok = True
        for sweep in (self.clamp_hosted_reasons, self.clamp_hosted_config):
            try:  # each sweep on its own: one failing never skips the other
                sweep()
            except Exception as exc:  # noqa: BLE001 - expiry must run whatever the store did
                ok = False
                self._record_clamp_failure(exc)
        self._swept = ok

    def _record_clamp_failure(self, exc: Exception) -> None:
        try:
            with self.store.transaction() as uow:
                uow.append(
                    EventDraft(
                        stream=CLAMP_FAILURE_STREAM,
                        type=EventType.DECISION_RECORDED,
                        actor="system",
                        payload={
                            "kind": "content_clamp_failed",
                            "error": type(exc).__name__,
                            "max_days": HOSTED_MAX_DAYS,
                        },
                    )
                )
        except Exception:  # noqa: BLE001, S110 - the record is best effort
            pass

    def _whole_seconds(self) -> datetime:
        return self._clock().astimezone(UTC).replace(microsecond=0)

    def shred(self, org_id: str, *, actor: str, reason: str = "shred") -> bool:
        """Destroy the organization's key and leave a **permanent tombstone** (R1.37): all its
        content becomes unreadable, everywhere, and the key is never made again. The ciphertext
        rows stay until they expire (they are noise now). Refused while a legal hold is on.

        The hold check, the ledger entry, the store tombstone and the key destruction share one
        transaction (the ring's own tombstone file is written first, so a crash in between still
        refuses every use). Returns True if a key was destroyed. A second call with no key left
        returns False and changes nothing; a key that came back (a restored folder) is destroyed
        again and the repair is recorded."""
        return self.shred_reporting(org_id, actor=actor, reason=reason)[0]

    def shred_reporting(
        self, org_id: str, *, actor: str, reason: str = "shred"
    ) -> tuple[bool, bool]:
        """`shred`, and whether stored model keys are still pending (`keys_pending`). The key
        erasure hook (R1.14c) runs AFTER the shred transaction has committed, whether the shred
        returned True or False (a second shred repairs), and never when it raises. A failing hook
        is not raised: the sweeper finishes the job."""
        _org(org_id)
        if reason not in ("shred", "erasure"):
            raise ValueError("reason is 'shred' or 'erasure'")
        if not valid_tombstone_actor(actor):
            raise ValueError("actor must be an opaque id (human:u_..., service:tok_..., cli)")
        try:
            with self.store.transaction() as uow:
                destroyed = self._shred_in(uow, org_id, actor, reason)
        except _AlreadyTombstoned:
            destroyed = False  # a lost race on the primary key: the entry was rolled back
        pending = False
        if self.key_erasure is not None:
            try:
                pending = self.key_erasure(org_id, actor)
            except Exception:  # noqa: BLE001 - the caller keeps the shred's own result
                _LOG.warning("key_erasure_hook_failed")  # fixed code: no value, no text
                pending = True
        return destroyed, pending

    def _shred_in(self, uow: UnitOfWork, org_id: str, actor: str, reason: str) -> bool:
        # checked inside the transaction (the write lock is held), and the key is destroyed
        # before it commits, so a hold set at the same moment waits and then sees the shred
        # refused or done, never half of each
        if uow.legal_hold(org_id):
            raise ContentError("a legal hold is on: content can't be shredded until it is lifted")
        existing = uow.key_tombstone(org_id)
        if existing is not None:
            uow.ext_erase(org_id)  # idempotent: nothing left to destroy is no change
            if not self.keys.key_present(org_id):
                if isinstance(self.keys, FileKeyRing):
                    self.keys.sweep_rewrap_orphans(org_id)  # a crash left key material behind
                return False  # already done: idempotent, no second entry
            return self._destroy_resurrected(uow, org_id, existing, actor)
        # a plugin's erasable records (secrets that must not outlive the key) go with it, in the
        # same transaction; the ledger is untouched
        uow.ext_erase(org_id)
        found = self.keys.tombstone(org_id)  # a resumed shred or an adoption; unreadable raises
        key = self.keys.key_for_shred(org_id)
        record = KeyTombstone(
            org_id=org_id,
            destroyed_at=found.destroyed_at if found else self._whole_seconds(),
            actor=found.actor if found else actor,
            reason=found.reason if found and found.at_approximate else reason,
            key_id=None if key is None else key_id(key),
        )
        uow.append(
            EventDraft(
                stream=f"org:{org_id}",
                type=EventType.DECISION_RECORDED,
                actor=actor,
                payload={"kind": "content_shred", "had_key": key is not None},
            )
        )
        if not uow.put_key_tombstone(record):
            raise _AlreadyTombstoned
        uow.purge_idempotency_org(org_id)  # stored responses are unreadable now (R1.35 6.3, Y10)
        return self.keys.destroy(org_id, tombstone=record)  # if this fails the entry rolls back

    def rewrap_keys(self, new_kek: bytes, *, actor: str) -> "RewrapReport":
        """Rotate the key ring's protection (H-18): re-wrap every organization's key file under
        `new_kek` and write one `key_rewrapped` ledger entry per organization (kek ids only, never
        a key). Resumable: an organization already under `new_kek` is skipped, except that one
        whose file was re-wrapped but whose entry was lost to a crash gets the entry now
        (`resumed`). Tombstoned organizations are skipped. The ring must hold the old and the new
        key while this runs; afterwards a ring holding only the new key reads everything and one
        holding only the old key refuses."""
        ring = self.keys
        if not isinstance(ring, FileKeyRing):
            raise ContentError("re-wrap needs a file key ring")
        if not valid_tombstone_actor(actor):
            raise ValueError("actor must be an opaque id")
        _check_kek(new_kek)
        if not ring.holds_kek(new_kek):
            raise ContentError(
                "the key ring does not hold the new key-encryption key: start it with "
                "keks=[new, old]; nothing was changed"
            )
        to_id = _kek_id(new_kek)
        only_new = ring.kek_count() == 1  # no older key: nothing can have been re-wrapped from one
        done: list[str] = []
        already: list[str] = []
        skipped: list[str] = []
        for org_id in ring.key_orgs():
            with self.store.transaction() as uow:
                ring.sweep_rewrap_orphans(org_id)  # a crashed run's temp holds a live key
                if uow.key_tombstone(org_id) is not None or ring.tombstone_present(org_id):
                    skipped.append(org_id)
                    continue
                from_id = ring.wrapped_under(org_id)
                changed = ring.rewrap(org_id, new_kek)
                # read inside the write lock: two concurrent runs never write two entries
                if not changed and (only_new or self._rewrap_logged(uow, org_id, to_id)):
                    already.append(org_id)
                    continue
                uow.append(
                    EventDraft(
                        stream=f"org:{org_id}",
                        type=EventType.DECISION_RECORDED,
                        actor=actor,
                        payload={
                            "kind": "key_rewrapped",
                            # null: the file was plain, or (resumed) the earlier key is unknown
                            "from_kek_id": from_id if changed else None,
                            "to_kek_id": to_id,
                            "resumed": not changed,
                        },
                    )
                )
                done.append(org_id)
        return RewrapReport(rewrapped=done, already=already, skipped=skipped, kek_id=to_id)

    @staticmethod
    def _rewrap_logged(uow: UnitOfWork, org_id: str, to_id: str) -> bool:
        after = 0
        while True:
            page = uow.events_after(f"org:{org_id}", after, 500)
            if not page:
                return False
            for e in page:
                payload = dict(e.payload)
                if (
                    e.type == EventType.DECISION_RECORDED
                    and payload.get("kind") == "key_rewrapped"
                    and payload.get("to_kek_id") == to_id
                ):
                    return True
            after = page[-1].seq

    def _destroy_resurrected(
        self, uow: UnitOfWork, org_id: str, record: KeyTombstone, actor: str
    ) -> bool:
        """A key came back beside a tombstone (states 4 and 6): destroy it and record the repair."""
        self.keys.destroy(org_id, tombstone=record)
        uow.append(
            EventDraft(
                stream=f"org:{org_id}",
                type=EventType.DECISION_RECORDED,
                actor=actor,
                payload={"kind": "key_tombstone_repaired", "how": "destroyed_resurrected"},
            )
        )
        return True

    def destroy_resurrected(self, org_id: str, *, actor: str) -> bool:
        """`keys reconcile --destroy-resurrected`: destroy a key that is present beside a store
        tombstone. False if there is no key file. `ContentError` if the organization has no store
        tombstone."""
        _org(org_id)
        if not valid_tombstone_actor(actor):
            raise ValueError("actor must be an opaque id")
        with self.store.transaction() as uow:
            record = uow.key_tombstone(org_id)
            if record is None:
                raise ContentError("no store tombstone")
            if not self.keys.key_present(org_id):
                if isinstance(self.keys, FileKeyRing):
                    self.keys.sweep_rewrap_orphans(org_id)  # a restored backup's leftover temp
                return False
            return self._destroy_resurrected(uow, org_id, record, actor)

    def adopt_tombstone(self, org_id: str, *, actor: str) -> bool:
        """`keys reconcile --adopt`: write the store row from the ring file (state 7) with reason
        `reconciled` and one `key_tombstone_repaired` ledger entry. False if the store already has
        the row (nothing changed); `ContentError` if there is no ring tombstone either."""
        _org(org_id)
        if not valid_tombstone_actor(actor):
            raise ValueError("actor must be an opaque id")
        with self.store.transaction() as uow:
            if uow.key_tombstone(org_id) is not None:
                return False
            found = self.keys.tombstone(org_id)
            if found is None:
                raise ContentError("no tombstone to adopt")
            record = KeyTombstone(
                org_id=org_id,
                destroyed_at=found.destroyed_at,
                actor=found.actor,
                reason=found.reason if found.at_approximate else "reconciled",
                key_id=None,
            )
            if not uow.put_key_tombstone(record):
                return False
            uow.append(
                EventDraft(
                    stream=f"org:{org_id}",
                    type=EventType.DECISION_RECORDED,
                    actor=actor,
                    payload={"kind": "key_tombstone_repaired", "how": "adopted"},
                )
            )
            return True

    # -- states, start-up repair, legacy keys (6, 6.1, 9.1) --------------------------------------

    def tombstone_state(self, org_id: str) -> int:
        """The state number of section 6 (1 to 8) from the store row (S), the ring file (R) and
        the key file (K). Opens nothing."""
        _org(org_id)
        s = self.store.key_tombstone(org_id) is not None
        r = self.keys.tombstone_present(org_id)
        k = self.keys.key_present(org_id)
        return {
            (False, False, False): 1,
            (False, False, True): 2,
            (True, True, False): 3,
            (True, True, True): 4,
            (True, False, False): 5,
            (True, False, True): 6,
            (False, True, False): 7,
            (False, True, True): 8,
        }[(s, r, k)]

    def note_resurrected(self, org_id: str) -> None:
        """A key is present beside a tombstone (states 4, 6, 8): one coalesced security-log row
        `content.key_resurrected`, and a log warning."""
        _LOG.warning("key_resurrected: a key file is present for destroyed organization %s", org_id)
        self.record_security_count(org_id, "content.key_resurrected", "tombstone")

    def reconcile_tombstones(self, org_id: str | None = None) -> dict[str, int]:
        """The start-up step (once, at `serve`, `worker` and `run`, with the active organization's
        id): copy its store tombstone to the ring file (states 5 and 6), warn about a key beside a
        tombstone (4, 6, 8) and about a ring tombstone the store does not know (7, 8). Writes no
        store row, no ledger entry, and never destroys a key. Returns `{org: state before}` for
        the organization it looked at, and only if it has a tombstone anywhere. Without `org_id`
        it looks at every organization (for tests and tools, never start-up)."""
        if org_id is not None:
            _org(org_id)

        def wanted(candidate: str) -> bool:
            return org_id is None or candidate == org_id

        seen: dict[str, int] = {}
        for record in self.store.key_tombstones():
            if not wanted(record.org_id):
                continue
            state = self.tombstone_state(record.org_id)
            seen[record.org_id] = state
            if state in (5, 6):
                self.keys.put_tombstone(record)
            if state in (4, 6):
                self.note_resurrected(record.org_id)
            if state in (3, 5) and isinstance(self.keys, FileKeyRing):
                self.keys.sweep_rewrap_orphans(record.org_id)  # leftover of a crashed re-wrap
        known = set(seen)
        for ring_org in self.keys.tombstoned_orgs():
            if ring_org in known or not wanted(ring_org):
                continue
            state = self.tombstone_state(ring_org)
            seen[ring_org] = state
            _LOG.warning("key_tombstone_unreconciled: %s has a ring tombstone only", ring_org)
            if state == 8:
                self.note_resurrected(ring_org)
        self._warn_legacy_keys(org_id)
        return seen

    def _warn_legacy_keys(self, only: str | None = None) -> None:
        legacy = getattr(self.keys, "legacy_orgs", None)
        if legacy is None:
            return
        for org_id in legacy():
            if (only is not None and org_id != only) or self.keys.key_present(org_id):
                continue
            with self.store.transaction() as uow:
                has_content = uow.org_has_content(org_id)
            if has_content:
                _LOG.warning(
                    "legacy_key_unadopted: %s has a key from before per-store folders; "
                    "run `nuramesh keys adopt-legacy %s` once",
                    org_id,
                    org_id,
                )

    def adopt_legacy(self, org_id: str, *, actor: str) -> str:
        """`keys adopt-legacy`: move the flat key into this store's folder. Returns `adopted`,
        `none` (no flat key), `tombstoned` (a tombstone in either copy) or `exists` (a key is
        already in the folder); in every refusal nothing is moved and no entry is written."""
        _org(org_id)
        if not valid_tombstone_actor(actor):
            raise ValueError("actor must be an opaque id")
        adopt = getattr(self.keys, "adopt_legacy", None)
        legacy = getattr(self.keys, "legacy_orgs", None)
        if adopt is None or legacy is None:
            raise ContentError("this key ring has no legacy keys")
        try:
            with self.store.transaction() as uow:
                if org_id not in legacy():
                    return "none"
                if uow.key_tombstone(org_id) is not None or self.keys.tombstone_present(org_id):
                    return "tombstoned"
                if self.keys.key_present(org_id):
                    return "exists"
                uow.append(
                    EventDraft(
                        stream=f"org:{org_id}",
                        type=EventType.DECISION_RECORDED,
                        actor=actor,
                        payload={"kind": "key_adopted_from_legacy"},
                    )
                )
                if not adopt(org_id):  # another store took it: the entry rolls back
                    raise _NothingToAdopt
        except _NothingToAdopt:
            return "none"
        return "adopted"

    def set_legal_hold(self, org_id: str, on: bool, *, actor: str, reason_hash: str) -> None:
        """Turn the organization's legal hold on or off (nothing expires while it is on). An
        explicit, owner-set action recorded in the ledger; never automatic (ND25). `reason_hash` is
        the keyed hash of the written reason (captured by the caller as `ContentKind.REASON`)."""
        _org(org_id)
        if not _HASH.fullmatch(reason_hash):
            raise ContentError("the reason is recorded as a 64-digit keyed hash, never as text")
        with self.store.transaction() as uow:
            uow.set_legal_hold(org_id, on)
            uow.append(
                EventDraft(
                    stream=f"org:{org_id}",
                    type=EventType.DECISION_RECORDED,
                    actor=actor,
                    payload={"kind": "legal_hold", "on": on, "reason_hash": reason_hash},
                )
            )


class BoundHasher:
    """A ledger hasher bound to ONE organization (PH-KEY R5): the HMAC and `hash_org` need it.

    Runs the key gate on EVERY call and never keeps the master key (a cache would ignore a shred
    mid-run, and a closure would put the key in a traceback); it keeps only the public id of the
    key that made its last digest, which `marker()` states, so the digest and its marker can't
    disagree. `__call__` may create the key for a never-keyed organization; `use` never does."""

    def __init__(
        self, owner: "LedgerHashing", reader: _KeyReader, uow: UnitOfWork | None, org: str
    ):
        self._owner, self._reader, self._uow, self._org = owner, reader, uow, org
        self._key_id: str | None = None

    def _tombstoned(self) -> bool:
        source = self._uow if self._uow is not None else self._owner.store
        return source.key_tombstone(self._org) is not None

    def _gate(self, *, create: bool, expect_same: bool = False) -> bytes:
        """The key gate. With `expect_same`, a loaded key whose id is not the one of this hasher's
        last digest is refused BEFORE its id is cached as verified (R5b (b), K30: a cache that
        trusted the replacement would let the next call write under a second key id)."""
        owner, org = self._owner, self._org
        try:
            _org(org)
            if self._tombstoned() or owner.ring.tombstone(org) is not None:
                raise KeyDestroyedError(org)
            master = owner.ring.master(org)
            if master is None and not create:
                raise OrphanedContentError(org)
            loaded = key_id(master) if master is not None else None
            if expect_same and self._key_id is not None and loaded != self._key_id:
                raise OrphanedContentError(org, mismatch=True)
            if not (loaded is not None and owner.verified(org) == loaded):
                problem, master = key_presence_check(self._reader, org, master, False, owner.ring)
                if problem is not None:
                    raise problem
                if master is not None:
                    loaded = key_id(master)
                    if expect_same and self._key_id is not None and loaded != self._key_id:
                        raise OrphanedContentError(org, mismatch=True)
            if master is None and not create:
                raise OrphanedContentError(org)
            if master is None:
                master = owner.ring.master(org, create=True)
                if master is not None:  # check the created key before it is cached as verified
                    problem, master = key_presence_check(self._reader, org, master, False, None)
                    if problem is not None:
                        raise problem
            if master is None:  # pragma: no cover - create=True always returns a key
                raise ContentError(f"no key for organization '{org}'")
            owner.verify(org, key_id(master))
        except ContentError as exc:
            owner.record_failure(org, exc)
            raise
        owner.note_working()
        return master

    def __call__(self, kind: LedgerHashKind, data: bytes) -> str:
        master = self._gate(create=True)
        self._key_id = key_id(master)
        return ledger_hash(master, self._org, kind, data)

    def use(self, kind: LedgerHashKind, data: bytes) -> str:
        """Hash with the key already in use: never creates one, and refuses a key whose id is not
        the one this hasher's first digest used (R6: the response hash is never made under a
        replaced key)."""
        master = self._gate(create=False, expect_same=True)
        return ledger_hash(master, self._org, kind, data)

    def marker(self) -> dict[str, str]:
        """`hash_scheme`, `hash_key_id` (of the key that made the last digest) and `hash_org`."""
        if self._key_id is None:
            raise RuntimeError("marker() before any digest")
        return {"hash_scheme": HASH_SCHEME, "hash_key_id": self._key_id, "hash_org": self._org}


class LedgerHashing:
    """Makes keyed ledger hashes for the engine (PH-KEY R5): stdlib HMAC over the key ring, no
    `cryptography` needed. `bind(uow, org_id)` is for a caller already inside a unit of work,
    `bind(None, org_id)` for one outside (each key lookup then reads through the store). It owns
    the alert queue of failed ledger hashes, or shares the content service's."""

    def __init__(
        self,
        store: Store,
        ring: KeyRing,
        *,
        service: "ContentService | None" = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store, self.ring, self._service = store, ring, service
        self._clock = clock or store.now  # the store's clock (ND243)
        self._alerts: list[tuple[str, str]] = []
        self.capture_failing = False
        self._verified: dict[str, str] = {}

    def bind(self, uow: UnitOfWork | None, org_id: str) -> BoundHasher:
        """A hasher for one organization (see the class)."""
        return BoundHasher(self, uow if uow is not None else self.store, uow, org_id)

    # -- the public key id last checked against the ledger (a cache that never beats the query)

    def verified(self, org_id: str) -> str | None:
        return self._verified.get(org_id)

    def verify(self, org_id: str, key_id_: str) -> None:
        self._verified[org_id] = key_id_

    def reset_cache(self) -> None:
        """Forget the verified ids (a run start and a request do this: the query wins)."""
        self._verified.clear()

    # -- the read-only start check (deny before any write, R5b)

    def key_problem(self, org_id: str) -> ContentError | None:
        """None if a run may start for the organization, else the refusal: a destroyed key, an
        unreadable tombstone, a lost key or a replaced key. Creates nothing."""
        self.reset_cache()
        try:
            _org(org_id)
            if self.store.key_tombstone(org_id) is not None or self.ring.tombstone(org_id):
                return KeyDestroyedError(org_id)
            return key_presence_problem(
                self.store, org_id, self.ring.master(org_id), False, self.ring
            )
        except ContentError as exc:
            return exc

    def require_key_present(self, org_id: str) -> None:
        """Raise what `key_problem` returns."""
        problem = self.key_problem(org_id)
        if problem is not None:
            raise problem

    # -- alerts (the same `content.capture_failed` row as a failed capture, 8.5)

    def record_failure(self, org_id: str, exc: ContentError) -> None:
        """Queue the alert for a refusal that needs a person (not a deliberate destruction)."""
        if self._service is not None:
            self._service._note_failure(org_id, exc)
        elif exc.code in (KEY_MISSING, KEY_MISMATCH, TOMBSTONE_UNAVAILABLE) and _SLUG.fullmatch(
            org_id
        ):
            self.capture_failing = True
            self._alerts.append((org_id, exc.code))

    def note_working(self) -> None:
        if self._service is not None:
            self._service.capture_failing = False
        else:
            self.capture_failing = False

    def flush_alerts(self) -> None:
        """Write the queued rows once no transaction is open (best effort)."""
        if self._service is not None:
            self._service.flush_alerts()
            return
        pending, self._alerts = self._alerts, []
        for index, (org_id, reason) in enumerate(pending):
            try:
                now = self._clock()
                entry = SecurityLogEntry.new(
                    org_id,
                    now,
                    "content.capture_failed",
                    actor="system",
                    details={"reason": reason, "count": 1},
                ).model_copy(
                    update={"id": coalesced_row_id("content.capture_failed", org_id, now, reason)}
                )
                self.store.upsert_security_log_count(entry)
            except NestedTransactionError:
                self._alerts = pending[index:] + self._alerts
                return
            except Exception:
                _LOG.warning(
                    "could not record content.capture_failed for %s", org_id, exc_info=True
                )


def default_key_dir() -> Path:
    """Where organization keys live: `NURAMESH_KEY_DIR`, else `~/.nuramesh/keys` (never inside a
    repository or the database)."""
    configured = os.environ.get("NURAMESH_KEY_DIR")
    return Path(configured) if configured else home_path("keys")


def default_ledger_hashing(store: Store) -> LedgerHashing:
    """Ledger hashing over the default file key ring; needs no `cryptography` (PH-KEY R5)."""
    ring = FileKeyRing(
        default_key_dir(), store_id=store.store_id(), store_tombstone=store.key_tombstone
    )
    return LedgerHashing(store, ring)


def default_content_service(
    store: Store, *, hosted: bool = False, reason_days: int = REASON_DAYS
) -> ContentService:
    """A content service over the default file key ring (needs the `content` extra). `hosted`
    turns on the 90-day cap of ND25 (RET-WIRE R6)."""
    _aesgcm(b"\x00" * KEY_BYTES)  # fail early, with the install hint, if the extra is missing
    ring = FileKeyRing(
        default_key_dir(), store_id=store.store_id(), store_tombstone=store.key_tombstone
    )
    return ContentService(store, ring, hosted=hosted, reason_days=reason_days)


HOSTED_ENV_PROBLEM = _SHARED_HOSTED_PROBLEM


def hosted_from_env() -> bool | None:
    """`NURAMESH_HOSTED` as a bool; `None` for a value the shared parser (`nuramesh.hosted`, ND220)
    refuses, which the caller reports as a usage error (fail closed). The meaning of each value
    comes from that parser only."""
    try:
        return _shared_hosted_from_env()
    except HostedConfigError:
        return None


def hosted_startup_problem() -> str | None:
    """Why this process must not start (ND202), or None. `NURAMESH_HOSTED` must be a valid value;
    and where `NURAMESH_REQUIRE_HOSTED=1` is set (our staging and production configuration), the
    deployment must say `NURAMESH_HOSTED=1` explicitly: a missing flag would silently mean
    self-hosted retention (396 days)."""
    hosted = hosted_from_env()
    if hosted is None:
        return HOSTED_ENV_PROBLEM
    try:
        need_hosted = env_bool("NURAMESH_REQUIRE_HOSTED")
    except EnvBoolError as exc:
        return str(exc)
    if need_hosted and not hosted:
        return (
            "NURAMESH_REQUIRE_HOSTED is on but NURAMESH_HOSTED is not (a hosted deployment says so)"
        )
    return env_startup_problem() or secret_startup_problem()


def configured_content_service(
    store: Store, settings: ContentSettings | None = None
) -> ContentService:
    """THE way every CLI and server path builds a content service (ND195 b): the hosted setting
    from `NURAMESH_HOSTED` and, when the organization's `content` settings are known, its
    `reason_days`. A bad flag value, or a setting a hosted deployment refuses, raises
    `ContentError`. A test (`test_hosted_reasons`) fails if any other code builds a service."""
    problem = hosted_startup_problem()
    if problem is not None:
        raise ContentError(problem)
    hosted = hosted_from_env()  # valid: the startup problem above covers a bad value
    if hosted and settings is not None:
        problems = settings.problems(hosted=True)
        if problems:
            raise ContentError(problems[0])
    kwargs: dict[str, Any] = {}
    if hosted:
        kwargs["hosted"] = True
    if settings is not None and settings.reason_days is not None:
        kwargs["reason_days"] = settings.reason_days
    return default_content_service(store, **kwargs)
