"""Unpack a workspace archive (R57) and delete a copy, through directory handles only.

`tarfile` only reads the headers (PAX and GNU long names included); it never creates anything
and its `data` filter is not used. Every member is checked by `ArchiveChecker` first and then
created relative to a directory handle of the copy: each parent is opened one path part at a time
with `O_DIRECTORY | O_NOFOLLOW`, the member itself with `O_NOFOLLOW | O_EXCL`. So nothing is ever
written through a link and nothing replaces an earlier member, whatever the rule check decided.
"""

import hashlib
import os
import stat
import tarfile
from collections.abc import Callable

from nuramesh.core.sandbox_service import ArchiveChecker, ArchiveEntry, ArchiveRefused

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_KINDS = {
    tarfile.REGTYPE: "file",
    tarfile.AREGTYPE: "file",
    tarfile.DIRTYPE: "dir",
    tarfile.SYMTYPE: "symlink",
    tarfile.LNKTYPE: "hardlink",
    tarfile.CHRTYPE: "chardev",
    tarfile.BLKTYPE: "blockdev",
    tarfile.FIFOTYPE: "fifo",
}


class _Counted:
    """Reads exactly `total` bytes from `read`, hashing them and noting the last non-zero byte."""

    def __init__(self, read: Callable[[int], bytes], total: int) -> None:
        self._read, self.left = read, total
        self.sha = hashlib.sha256()
        self.position = 0
        self.last_nonzero = -1

    def read(self, n: int = -1) -> bytes:
        want = self.left if n is None or n < 0 else min(n, self.left)
        if want <= 0:
            return b""
        data = self._read(min(want, 1 << 20))
        if not data:
            self.left = 0
            raise ArchiveRefused("the archive is shorter than declared")
        self.left -= len(data)
        self.sha.update(data)
        kept = len(data.rstrip(b"\0"))
        if kept:
            self.last_nonzero = self.position + kept - 1
        self.position += len(data)
        return data

    def drain(self) -> None:
        while self.left > 0:
            self.read(1 << 20)


def _kind(member: tarfile.TarInfo) -> str:
    if member.sparse is not None or member.type == tarfile.GNUTYPE_SPARSE:
        return "sparse"
    return _KINDS.get(member.type, "unknown")


def _open_parent(root_fd: int, parts: list[str]) -> int:
    """A handle of the member's parent directory; missing parents are created the same way."""
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            following = os.open(part, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    except OSError:
        os.close(current)
        raise
    return current


def _create(
    root_fd: int, entry: ArchiveEntry, member: tarfile.TarInfo, tar: tarfile.TarFile
) -> None:
    parts = entry.name.split("/")
    parent = _open_parent(root_fd, parts[:-1])
    name = parts[-1]
    try:
        if entry.kind == "dir":
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:  # made earlier as the parent of another member
                if not stat.S_ISDIR(os.lstat(name, dir_fd=parent).st_mode):
                    raise
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
            try:
                os.fchmod(fd, entry.mode | 0o700)
            finally:
                os.close(fd)
        elif entry.kind == "symlink":
            os.symlink(member.linkname, name, dir_fd=parent)
        else:
            source = tar.extractfile(member)
            if source is None:
                raise ArchiveRefused("a file member without data")
            fd = os.open(name, _FILE_FLAGS, 0o600, dir_fd=parent)
            try:
                left = entry.size
                while left > 0:
                    data = source.read(min(left, 1 << 20))
                    if not data:
                        raise ArchiveRefused("a file member is shorter than its size")
                    os.write(fd, data)
                    left -= len(data)
                os.fchmod(fd, entry.mode)
            finally:
                os.close(fd)
    finally:
        os.close(parent)


def unpack_archive(read: Callable[[int], bytes], total: int, dest_fd: int, limit_bytes: int) -> str:
    """Unpack `total` bytes of tar from `read` into the directory `dest_fd`; the SHA-256 of them.

    Raises `ArchiveRefused` on any broken rule of R57, on anything that is not a tar, and on
    bytes that are not zero after the last member. All `total` bytes are read (and hashed) even
    when the archive is refused, unless `read` itself fails. Errors of `read` pass through.
    """
    stream = _Counted(read, total)
    checker = ArchiveChecker(limit_bytes)
    refused: ArchiveRefused | None = None
    try:
        if total:
            with tarfile.open(fileobj=stream, mode="r|", errorlevel=2) as tar:  # type: ignore[call-overload]
                for member in tar:
                    entry = checker.add(
                        ArchiveEntry(member.name, _kind(member), member.size, member.mode)
                    )
                    _create(dest_fd, entry, member, tar)
                end = tar.offset
            stream.drain()
            if stream.last_nonzero >= end:
                raise ArchiveRefused("bytes after the end of the archive")
    except ArchiveRefused as error:
        refused = error
    except (tarfile.TarError, OSError, ValueError, EOFError, UnicodeError) as error:
        refused = ArchiveRefused(f"not a usable archive: {type(error).__name__}")
    if refused is not None:
        try:
            stream.drain()
        except ArchiveRefused:
            pass
        raise refused
    return stream.sha.hexdigest()


def _identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _enter(parent_fd: int, name: str) -> int:
    """Make the directory `name` usable by its owner (a command's `chmod 000`) and open it.

    The mode change never follows a link: a name that became a symbolic link since it was looked
    at is an `OSError`, and the link's target keeps its mode.
    """
    try:
        os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
    except (ValueError, NotImplementedError) as error:  # how Python reports "this is a link"
        raise OSError(f"not a directory any more: {type(error).__name__}") from None
    return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)


def remove_tree(parent_fd: int, name: str) -> None:
    """Delete `name` under the directory `parent_fd` without following any link.

    Called only when no command runs (the supervisor skips it while `bwrap` was not reaped). Even
    so, the `chmod` that a command's own `chmod 000` makes necessary does not follow a link: a
    directory swapped for a link between the `lstat` and the `chmod` ends the delete with an
    `OSError` and its target is not touched.

    No recursion and one open directory at a time: a command can make a tree thousands of
    directories deep, and a copy that cannot be deleted stops the service until restart. The way
    back up is `..`, accepted only when it is the directory that was left (device and inode).
    """
    if not stat.S_ISDIR(os.lstat(name, dir_fd=parent_fd).st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    # One frame per directory on the path: its name, its parent's identity, sub-directories left.
    frames: list[tuple[str, tuple[int, int], list[str]]] = []
    here, here_name, above = _enter(parent_fd, name), name, _identity(parent_fd)
    try:
        pending: list[str] | None = None
        while True:
            if pending is None:  # first visit: delete what is not a directory
                pending = []
                for child in os.listdir(here):
                    if stat.S_ISDIR(os.lstat(child, dir_fd=here).st_mode):
                        pending.append(child)
                    else:
                        os.unlink(child, dir_fd=here)
            if pending:
                child = pending.pop()
                below = _enter(here, child)
                frames.append((here_name, above, pending))
                above, here_name, pending = _identity(here), child, None
                os.close(here)
                here = below
                continue
            if not frames:
                break
            up = os.open("..", _DIR_FLAGS, dir_fd=here)
            os.close(here)
            here = up
            if _identity(here) != above:
                raise OSError("the parent of a directory changed while its copy was deleted")
            os.rmdir(here_name, dir_fd=here)
            here_name, above, pending = frames.pop()
    finally:
        os.close(here)
    os.rmdir(name, dir_fd=parent_fd)
