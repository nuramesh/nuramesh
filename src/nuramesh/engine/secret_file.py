"""The managed file secret store for self-hosted stored model keys (spec r114c 4.2, ND362).

One file per key version under a directory named by `NURAMESH_SECRET_DIR`, with the ND214 file
rules of `engine.key_file`: absolute, outside every repository, a regular file (no link), owned by
the server user and not by `nuramesh-agent`, mode 0600 (the directory 0700), at most 1024 bytes, read
on every call and never cached. A version is created atomically and only once (a temporary file
linked into place); `disable` creates the sibling marker `<name>.disabled` and `get` refuses a
marked version before it opens the value; `destroy` overwrites the value, unlinks it, then
unlinks the marker. The app does not encrypt the file: the OS permission is the control.

Refused when `NURAMESH_HOSTED=1`. Every failure is a `SecretStoreError` with a fixed code and a
fixed reason, raised outside any `except` block (no cause chain), never with a path or a value.
"""

import os
import re
import secrets as _secrets
import stat
import time as _time
from datetime import UTC, datetime
from pathlib import Path

from nuramesh.core.models import ProviderError
from nuramesh.core.secret_store import KEY_ID_PATTERN, SecretMeta, SecretRef, SecretStoreError
from nuramesh.core.secrets_text import Redacted, reveal_secret
from nuramesh.engine.key_file import (
    FILE_REASONS,
    KEY_SHAPE,
    MAX_KEY_FILE_BYTES,
    IsAgentUser,
    KeyTexts,
    in_repository,
    is_agent_user,
    read_checked_file,
)
from nuramesh.hosted import hosted_from_env

SECRET_DIR_ENV = "NURAMESH_SECRET_DIR"
REASON_HOSTED = "the file secret store is not available on a hosted deployment"
REASON_DIRECTORY = "the secret directory is not usable"
_TEXTS = KeyTexts(
    prefix="",
    no_key="the secret file is missing",
    not_usable="the secret is not usable",
    fixed_text="",
)
_NAME = re.compile(r"nm-([0-9a-f]{16})-([0-9a-f]{20})-v([0-9]+)")
_MARKER = ".disabled"
_TEMP = ".tmp-"
_TEMP_NAME = re.compile(r"\.tmp-(nm-([0-9a-f]{16})-[0-9a-f]{20}-v[0-9]+)-[0-9a-f]{16}")
TEMP_MAX_AGE_SECONDS = 600
"""A put finishes in milliseconds; a temp file older than this was left by a crash."""


class FileSecretStore:
    """A `SecretStore` on the local file system. Pass `agent` only in tests."""

    def __init__(
        self,
        directory: str,
        *,
        hosted: bool | None = None,
        agent: IsAgentUser = is_agent_user,
    ) -> None:
        refused = hosted_from_env() if hosted is None else hosted
        if refused:
            raise SecretStoreError("invalid", REASON_HOSTED)
        self._dir, self._agent = directory, agent
        self._check_directory(create=True)

    # -- directory and paths --------------------------------------------------------------

    def _bad_directory(self) -> bool:
        path = self._dir
        if "\0" in path or not os.path.isabs(path) or in_repository(path):
            return True
        try:
            info = os.lstat(path)
        except OSError:
            return True
        return (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or self._agent(info.st_uid)
        )

    def _check_directory(self, *, create: bool = False) -> None:
        if (
            create
            and "\0" not in self._dir
            and os.path.isabs(self._dir)
            and not os.path.lexists(self._dir)
        ):
            try:
                os.mkdir(self._dir, 0o700)
            except OSError:
                pass  # checked below; the fixed reason is the same
        if self._bad_directory():
            raise SecretStoreError("unavailable", REASON_DIRECTORY)

    def _path(self, ref: SecretRef, suffix: str = "") -> str:
        return os.path.join(self._dir, ref.name + suffix)

    # -- the port -------------------------------------------------------------------------

    def put(
        self, ref: SecretRef, value: Redacted, *, expires_at: datetime | None = None
    ) -> SecretMeta:
        self._check_directory()
        text = reveal_secret(value)
        data = text.encode("utf-8", "replace")
        if len(data) > MAX_KEY_FILE_BYTES or not KEY_SHAPE.fullmatch(text):
            raise SecretStoreError("invalid")
        final = self._path(ref)
        temp = os.path.join(self._dir, f"{_TEMP}{ref.name}-{_secrets.token_hex(8)}")
        outcome = ""
        try:
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    _write_all(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.link(temp, final)  # fails if the version exists: create-only and atomic
                outcome = "ok"
            except FileExistsError:
                outcome = "conflict"
            except OSError:
                outcome = "unavailable"
        finally:
            # also on KeyboardInterrupt and other BaseException: no temp keeps the key
            _remove_temp(temp)
        if outcome == "conflict":
            raise SecretStoreError("conflict")
        if outcome != "ok":
            raise SecretStoreError("unavailable")
        return self._meta(ref, expires_at)

    def get(self, ref: SecretRef) -> Redacted:
        self._check_directory()
        path = self._path(ref)
        if os.path.lexists(path + _MARKER):
            raise SecretStoreError("disabled")
        if not os.path.lexists(path):
            raise SecretStoreError("not_found")
        data = b""
        reason = ""
        try:
            data = read_checked_file(path, _TEXTS, agent=self._agent)
        except ProviderError as exc:
            reason = str(exc)
        if reason:
            raise SecretStoreError("denied", _reason_of(reason))
        try:
            return Redacted(data.decode("ascii"))
        except UnicodeDecodeError:
            pass
        raise SecretStoreError("invalid")

    def disable(self, ref: SecretRef) -> SecretMeta:
        self._check_directory()
        path = self._path(ref)
        if not os.path.lexists(path):
            raise SecretStoreError("not_found")
        failed = False
        try:
            os.close(os.open(path + _MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass
        except OSError:
            failed = True
        if failed:
            raise SecretStoreError("unavailable")
        return self._meta(ref, None)

    def destroy(self, ref: SecretRef) -> None:
        self._check_directory()
        path = self._path(ref)
        failed = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = -1
        except OSError:
            fd, failed = -1, True
        if fd >= 0:
            try:
                size = os.fstat(fd).st_size
                os.write(fd, b"\0" * size)
                os.fsync(fd)
            except OSError:
                failed = True
            os.close(fd)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                failed = True
        try:
            os.unlink(path + _MARKER)
        except FileNotFoundError:
            pass
        except OSError:
            failed = True
        if failed:
            raise SecretStoreError("unavailable")

    def versions(self, org_id: str, key_id: str) -> list[SecretMeta]:
        found = [m for m in self._scan(org_id) if m.ref.key_id == key_id]
        return sorted(found, key=lambda m: m.ref.version)

    def list_versions(self, org_id: str) -> list[SecretMeta]:
        return sorted(self._scan(org_id), key=lambda m: (m.ref.key_id, m.ref.version))

    def stale_temps(self, org_id: str, *, now: float | None = None) -> list[str]:
        """File names of this organization's temporary put files older than
        `TEMP_MAX_AGE_SECONDS` (left by a crash between create and cleanup). They can hold a key
        that never got a version; the sweeper reports them and calls `sweep_temps`."""
        self._check_directory()
        tag = _org_tag(org_id)
        clock = _time.time() if now is None else now
        names: list[str] | None = None
        try:
            names = os.listdir(self._dir)
        except OSError:
            pass
        if names is None:
            raise SecretStoreError("unavailable")
        old: list[str] = []
        for name in sorted(names):
            match = _TEMP_NAME.fullmatch(name)
            if match is None or match.group(2) != tag:
                continue
            try:
                age = clock - os.lstat(os.path.join(self._dir, name)).st_mtime
            except OSError:
                continue
            if age > TEMP_MAX_AGE_SECONDS:
                old.append(name)
        return old

    def sweep_temps(self, org_id: str, *, now: float | None = None) -> int:
        """Overwrite and remove this organization's stale temporary files; the count removed."""
        removed = 0
        for name in self.stale_temps(org_id, now=now):
            if _remove_temp(os.path.join(self._dir, name)):
                removed += 1
        return removed

    def health(self) -> bool:
        return not self._bad_directory() and os.access(self._dir, os.W_OK | os.X_OK)

    # -- helpers --------------------------------------------------------------------------

    def _scan(self, org_id: str) -> list[SecretMeta]:
        self._check_directory()
        tag = _org_tag(org_id)
        found: list[SecretMeta] = []
        names: list[str] | None = None
        try:
            names = os.listdir(self._dir)
        except OSError:
            pass
        if names is None:
            raise SecretStoreError("unavailable")
        for name in names:
            match = _NAME.fullmatch(name)
            if match is None or match.group(1) != tag:
                continue
            ref = SecretRef(org_id=org_id, key_id="mk_" + match.group(2), version=int(match[3]))
            if re.fullmatch(KEY_ID_PATTERN, ref.key_id) and os.path.isfile(self._path(ref)):
                found.append(self._meta(ref, None))
        return found

    def _meta(self, ref: SecretRef, expires_at: datetime | None) -> SecretMeta:
        """Metadata from the file itself: `created_at` is the mtime of the exclusive create (the
        store's clock is the file system's)."""
        path = self._path(ref)
        created: datetime | None = None
        try:
            created = datetime.fromtimestamp(os.lstat(path).st_mtime, UTC)
        except OSError:
            pass
        if created is None:
            raise SecretStoreError("not_found")
        disabled = os.path.lexists(path + _MARKER)
        return SecretMeta(
            ref=ref,
            created_at=created,
            state="disabled" if disabled else "enabled",
            expires_at=expires_at,
        )


def _org_tag(org_id: str) -> str:
    return SecretRef(org_id=org_id, key_id="mk_" + "0" * 20, version=1).name.split("-")[1]


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte (a short `os.write` is legal)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError
        view = view[written:]


def _remove_temp(path: str) -> bool:
    """Zero (when it is the only name of the inode) and unlink a temporary file; True if it was
    removed. Never raises an `OSError`."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    except OSError:
        fd = -1
    if fd >= 0:
        try:
            info = os.fstat(fd)
            if info.st_nlink == 1:  # not yet linked to a version: overwrite the key
                _write_all(fd, b"\0" * info.st_size)
                os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def _reason_of(message: str) -> str | None:
    for reason in FILE_REASONS:
        if message.endswith(f"({reason})"):
            return reason
    return None


def file_store_from_env(*, agent: IsAgentUser = is_agent_user) -> FileSecretStore | None:
    """The store named by `NURAMESH_SECRET_DIR`, or None when it is unset."""
    directory = os.environ.get(SECRET_DIR_ENV, "")
    if not directory:
        return None
    return FileSecretStore(str(Path(directory)), agent=agent)
