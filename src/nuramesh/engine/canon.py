"""Read canon from a Git repository at a pinned commit, and record it (PLAN F.1; T26, R3.00).

`load_canon` runs `git cat-file` as a subprocess (an argument list, never a shell) against a local
repository and reads **objects only**, by full object id: the pinned commit, every tree reachable
from its root tree and the blobs it returns. It parses commit and tree objects itself and
**recomputes the hash of every object** (SHA-1, `type SP size NUL body`) against the id it asked
for, so the files it returns are exactly the files the pinned commit id names (R3.00). Sizes are
checked on each object's header before its body is read. What is verified: the pinned commit and its
full tree. What is not: ancestor commits (parents are parsed past, never followed), who wrote the
commit (no signature check), and colliding SHA-1 pairs written straight into the store. SHA-256
repositories are refused (`object_format_unsupported`); shallow clones are fine. Safety rules:

- the commit must be a full 40-character id that exists and **is a commit** (not a tag name, a
  branch, an abbreviation or a tag object's id);
- **replace refs are ignored** (`--no-replace-objects`): otherwise anyone who can write a ref could
  make a pinned commit show different content;
- the repository must be a repository root with a real `.git` directory (or a bare repository),
  addressed with an explicit `GIT_DIR`, so git never walks up into a parent repository or follows a
  `.git` file elsewhere;
- git runs with no user or system config, no hooks, no pager, no prompts, **no network** (all
  protocols off and lazy fetching of missing objects disabled, so a hostile repository config such
  as a partial-clone remote with an `sshCommand` can't run anything) and an overall deadline;
- only regular files (mode 100644/100755) are read, symlinks and submodules are refused, as are
  non-UTF-8 files and names, and names `CanonDocument` doesn't allow; duplicate and `.`/`..` names
  are refused; trees are bounded in bytes, entries and depth.

What the caller must still do: `repository` is a path on this machine. A server that accepts it
from users must confine it (an allow-listed root, ownership checks). A pinned commit that a
force-push and garbage collection removed can't be read (it fails closed): tag the commits you pin.

`record_canon_loaded` writes `canon.loaded` to the run's stream: the commit, the canon hash, each
document's name and content hash, never the text, and the `verification` summary.
"""

import contextlib
import hashlib
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nuramesh.core.canon import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENTS,
    MAX_NAME_BYTES,
    MAX_TOTAL_BYTES,
    Canon,
    CanonSource,
    CanonVerification,
)
from nuramesh.core.ledger import EventType
from nuramesh.core.policy import ActionKind, glob_match
from nuramesh.core.verifier import CanonDocument
from nuramesh.engine.starter import refuse_foreign_run
from nuramesh.stores.base import EventDraft, Store, run_stream

TIMEOUT_SECONDS = 30
TOTAL_SECONDS = 60
MAX_TREE_BYTES = 4_000_000  # all distinct tree bodies together; a tree bomb is cut off here
MAX_TREE_ENTRIES = 100_000  # entries visited (a shared subtree counts each time it is reached)
MAX_COMMIT_BYTES = 1_000_000
MAX_DEPTH = 64
HEADER_LINE_BYTES = 256
LOCKSTEP_MAX_BYTES = 16_000_000  # a backstop on all bytes read from one cat-file process
_REGULAR = {"100644", "100755"}
_MODES = {"100644", "100755", "120000", "160000", "40000"}
_RAW_ID = 20  # SHA-1 only (SHA-256 repositories are refused)


class CanonError(ValueError):
    """The canon couldn't be read; the message says why (never includes file contents).

    `code` is a short fixed string (`canon_error` for the older messages); the codes of the chain
    verification start their message with it."""

    def __init__(self, message: str, code: str = "canon_error") -> None:
        super().__init__(message)
        self.code = code


def _git_dir(repo: Path) -> Path:
    inner = repo / ".git"
    if inner.is_dir() and not inner.is_symlink():
        git_dir = inner
    elif (repo / "HEAD").is_file() and (repo / "objects").is_dir():  # a bare repository
        git_dir = repo
    else:
        raise CanonError("the canon repository path isn't a git repository root")
    if (git_dir / "commondir").exists() or (git_dir / "objects").is_symlink():
        raise CanonError("the canon repository redirects to other storage (commondir, symlink)")
    return git_dir


def _kill(proc: "subprocess.Popen[bytes]") -> None:
    """Kill git and anything it started (its own process group)."""
    pid = getattr(proc, "pid", None)
    try:
        if not isinstance(pid, int):
            raise ProcessLookupError
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _spawn(git_dir: Path, args: tuple[str, ...], *, stdin: int) -> "subprocess.Popen[bytes]":
    """Start git with the hardened environment (no config, hooks, network or prompts)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_DIR": str(git_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }
    command = [
        "git",
        "--no-replace-objects",
        "-c",
        "protocol.allow=never",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
    ]
    try:
        return subprocess.Popen(  # noqa: S603 (an argument list, no shell)
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=stdin,
            env=env,
            start_new_session=True,
            bufsize=0,
        )
    except FileNotFoundError:
        raise CanonError("git isn't installed") from None
    except OSError:
        raise CanonError("git couldn't be started") from None


def _git(git_dir: Path, deadline: float, *args: str, max_bytes: int = MAX_TOTAL_BYTES * 2) -> bytes:
    """Run git and return stdout; output over `max_bytes` kills git (no unbounded buffering)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CanonError(f"reading the canon took longer than {TOTAL_SECONDS}s")
    proc = _spawn(git_dir, args, stdin=subprocess.DEVNULL)
    timed_out = threading.Event()

    def _expire() -> None:
        timed_out.set()
        _kill(proc)

    timer = threading.Timer(min(TIMEOUT_SECONDS, remaining), _expire)
    timer.start()
    chunks: list[bytes] = []
    size = 0
    assert proc.stdout is not None
    try:
        while chunk := proc.stdout.read(65536):
            size += len(chunk)
            if size > max_bytes:
                _kill(proc)
                raise CanonError(f"git {args[0]} produced more than {max_bytes} bytes")
            chunks.append(chunk)
        code = proc.wait()
    finally:
        timer.cancel()
        _kill(proc)
        proc.stdout.close()
        proc.wait()
    if timed_out.is_set():
        raise CanonError(f"git took longer than {TIMEOUT_SECONDS}s")
    if code != 0:
        raise CanonError(f"git {args[0]} failed (is the commit present in the repository?)")
    return b"".join(chunks)


def _fail(code: str, text: str) -> CanonError:
    """A coded error: the message starts with the code, never carries object bodies or paths."""
    return CanonError(f"{code}: {text}", code)


class _Reader:
    """One `git cat-file` process driven in lockstep (write one id, read its whole response).

    Header lines are read byte by byte with a hard limit, so a read stops exactly at the LF and
    never consumes body bytes; `consumed` and `requests` count what crossed the pipe (the test
    seam). Any error leaves the process to be killed by `close`, never drained."""

    def __init__(self, proc: Any, *, objects: Path, timeout: float) -> None:
        self._proc = proc
        self._objects = objects
        self.consumed = 0
        self.requests = 0
        self._timed_out = threading.Event()
        self._closed = False
        self._lock = threading.Lock()
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.start()

    def _expire(self) -> None:
        with self._lock:  # never kill a process group that close() has already reaped
            if self._closed:
                return
            self._timed_out.set()
            _kill(self._proc)

    def close(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._closed = True
            _kill(self._proc)
        for pipe in (self._proc.stdin, self._proc.stdout):
            with contextlib.suppress(Exception):
                pipe.close()
        with contextlib.suppress(Exception):
            self._proc.wait()

    def _eof(self) -> CanonError:
        if self._timed_out.is_set():
            return CanonError(f"git took longer than {TIMEOUT_SECONDS}s")
        return CanonError("git cat-file failed (is the repository complete?)")

    def _count(self, n: int) -> None:
        self.consumed += n
        if self.consumed > LOCKSTEP_MAX_BYTES:
            raise _fail("object_corrupt", "git sent more data than the canon reader accepts")

    def _read(self, n: int) -> bytes:
        parts: list[bytes] = []
        left = n
        while left > 0:
            chunk = self._proc.stdout.read(min(left, 65536))
            if not chunk:
                raise self._eof()
            parts.append(chunk)
            left -= len(chunk)
            self._count(len(chunk))
        return b"".join(parts)

    def _line(self) -> bytes:
        out = bytearray()
        while True:
            byte = self._proc.stdout.read(1)
            if not byte:
                raise self._eof()
            self._count(1)
            if byte == b"\n":
                return bytes(out)
            out.append(byte[0])
            if len(out) >= HEADER_LINE_BYTES:
                raise _fail("object_corrupt", "a git header line is too long")

    def ask(self, oid: str, kind: str) -> int:
        """Request `oid`, check the header (shape, missing, echoed id, type); return its size."""
        try:
            self._proc.stdin.write(oid.encode() + b"\n")
            self._proc.stdin.flush()
        except (OSError, ValueError):
            raise self._eof() from None
        self.requests += 1
        line = self._line()
        parts = line.split(b" ")
        if len(parts) == 2 and parts[1] == b"missing":
            if parts[0].decode("ascii", "replace") != oid:
                raise _fail(
                    "object_corrupt", f"git answered for another object than the {kind} {oid}"
                )
            if (self._objects / oid[:2] / oid[2:]).is_file() or (
                self._objects / oid[:2] / oid[2:]
            ).is_symlink():
                raise _fail("object_corrupt", f"the {kind} {oid} is on disk but can't be decoded")
            raise _fail("object_missing", f"the {kind} {oid} is missing from the repository")
        if (
            len(parts) != 3
            or not parts[2].isdigit()
            or not parts[2].isascii()
            or len(parts[2]) > 12
            or not parts[1].isalpha()
            or not parts[1].isascii()
        ):
            raise _fail("object_corrupt", "a git header line is malformed")
        if parts[0].decode("ascii", "replace") != oid:
            raise _fail("object_corrupt", f"git answered for another object than the {kind} {oid}")
        found = parts[1].decode("ascii")
        if found != kind:
            if kind == "commit":
                raise CanonError(f"{oid} isn't a commit id", "not_a_commit")
            raise _fail("object_type_mismatch", f"expected a {kind}, {oid} is a {found}")
        return int(parts[2])

    def body(self, oid: str, kind: str, size: int) -> bytes:
        """Read exactly `size` bytes and the LF; the hash must equal the id asked for."""
        data = self._read(size + 1)
        if data[-1:] != b"\n":
            raise _fail("object_corrupt", f"the {kind} {oid} isn't followed by a line feed")
        data = data[:-1]
        digest = hashlib.sha1(  # noqa: S324 (git's object id, not security)
            b"%s %d\x00" % (kind.encode(), size) + data, usedforsecurity=False
        ).hexdigest()
        if digest != oid:
            raise _fail("object_corrupt", f"the {kind} {oid} doesn't match its object id")
        return data


@contextlib.contextmanager
def _cat_file(git_dir: Path, deadline: float, mode: str) -> Iterator[_Reader]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CanonError(f"reading the canon took longer than {TOTAL_SECONDS}s")
    proc = _spawn(git_dir, ("cat-file", mode), stdin=subprocess.PIPE)
    reader = _Reader(proc, objects=git_dir / "objects", timeout=min(TIMEOUT_SECONDS, remaining))
    try:
        yield reader
    finally:
        reader.close()


_COMMIT_TREE = re.compile(rb"tree ([0-9a-f]{40})\n")


def _parse_tree(oid: str, body: bytes) -> list[tuple[str, bytes, str]]:
    """Entries (mode, raw name, id) of a tree body; anything off-format is `tree_malformed`."""
    entries: list[tuple[str, bytes, str]] = []
    names: set[bytes] = set()
    pos = 0
    while pos < len(body):
        space = body.find(b" ", pos)
        nul = body.find(b"\x00", space + 1) if space >= 0 else -1
        if space < 0 or nul < 0:
            raise _fail("tree_malformed", f"the tree {oid} has a truncated entry")
        mode = body[pos:space].decode("ascii", "replace")
        name = body[space + 1 : nul]
        raw = body[nul + 1 : nul + 1 + _RAW_ID]
        if mode not in _MODES or not name or b"/" in name or len(raw) != _RAW_ID or name in names:
            raise _fail("tree_malformed", f"the tree {oid} has an entry that isn't allowed")
        names.add(name)
        entries.append((mode, name, raw.hex()))
        pos = nul + 1 + _RAW_ID
    return entries


class _Walk:
    """The verified walk from the pinned commit down every reachable tree (V1, V7 to V9)."""

    def __init__(self, reader: _Reader, source: CanonSource) -> None:
        self.reader = reader
        self.source = source
        self.trees: dict[str, list[tuple[str, bytes, str]]] = {}
        self.tree_bytes = 0
        self.visited = 0
        self.chosen: list[tuple[str, str]] = []
        self.seen: set[str] = set()

    def root(self) -> str:
        size = self.reader.ask(self.source.commit, "commit")
        if size > MAX_COMMIT_BYTES:
            raise _fail("chain_limit", f"a commit is over {MAX_COMMIT_BYTES} bytes")
        body = self.reader.body(self.source.commit, "commit", size)
        found = _COMMIT_TREE.match(body)
        if found is None:
            raise _fail("commit_malformed", f"the commit {self.source.commit} has no tree line")
        tree = found.group(1).decode("ascii")
        self.visit(tree, "", 0)
        return tree

    def _read_tree(self, oid: str) -> list[tuple[str, bytes, str]]:
        size = self.reader.ask(oid, "tree")
        if size > MAX_TREE_BYTES - self.tree_bytes:
            raise _fail("chain_limit", f"more than {MAX_TREE_BYTES} bytes of trees")
        entries = _parse_tree(oid, self.reader.body(oid, "tree", size))
        self.tree_bytes += size
        return entries

    def visit(self, oid: str, prefix: str, depth: int) -> None:
        entries = self.trees.get(oid)
        if entries is None:
            entries = self.trees[oid] = self._read_tree(oid)
        self.visited += len(entries)
        if self.visited > MAX_TREE_ENTRIES:
            raise _fail("chain_limit", f"more than {MAX_TREE_ENTRIES} entries")
        for mode, raw_name, entry in entries:
            name = raw_name.decode("utf-8", "surrogateescape")
            if mode == "40000":
                if depth + 1 > MAX_DEPTH:
                    raise _fail("chain_limit", f"deeper than {MAX_DEPTH} directories")
                self.visit(entry, f"{prefix}{name}/", depth + 1)
            else:
                self._select(mode, f"{prefix}{name}", entry)

    def _select(self, mode: str, path: str, oid: str) -> None:
        if not any(glob_match(p, path, ActionKind.WRITE_FILE) for p in self.source.paths):
            return
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:  # surrogateescape marks bytes that aren't valid UTF-8
            raise CanonError("a file name that matches isn't valid UTF-8 text") from None
        if mode not in _REGULAR:
            raise CanonError(f"'{path[:60]!r}' is a {_describe(mode)}, not a regular file")
        if any(part in ("", ".", "..") for part in path.split("/")):
            raise CanonError(f"'{path[:60]}' isn't an allowed canon file name")
        try:
            CanonDocument(name=path, text="")
        except ValidationError:
            raise CanonError(f"'{path[:60]}' isn't an allowed canon file name") from None
        if path in self.seen:
            raise CanonError(f"'{path[:60]}' appears twice in the tree")
        self.seen.add(path)
        self.chosen.append((path, oid))


def _check_format(git_dir: Path, deadline: float) -> None:
    out = _git(git_dir, deadline, "rev-parse", "--show-object-format").decode("utf-8", "replace")
    out = out[:-1] if out.endswith("\n") else out
    if out == "sha256":
        raise _fail(
            "object_format_unsupported", "SHA-256 repositories aren't supported for the canon yet"
        )
    if out != "sha1":
        raise _fail("git_too_old", "this git can't report the repository's object format")


def _is_shallow(git_dir: Path, deadline: float) -> bool:
    if (git_dir / "shallow").exists():
        return True
    try:
        return _git(git_dir, deadline, "rev-parse", "--is-shallow-repository").strip() == b"true"
    except CanonError:  # informational only (S1)
        return False


def load_canon(source: CanonSource) -> Canon:
    """Read the documents `source` names from its pinned commit, verifying every object from the
    commit down to each blob returned (module docstring)."""
    repo = Path(source.repository)
    if not repo.is_dir():
        raise CanonError("the canon repository path isn't a directory")
    git_dir = _git_dir(repo)
    deadline = time.monotonic() + TOTAL_SECONDS
    _check_format(git_dir, deadline)
    with _cat_file(git_dir, deadline, "--batch") as reader:
        walk = _Walk(reader, source)
        root = walk.root()
    chosen = walk.chosen
    if len(chosen) > MAX_DOCUMENTS:
        raise CanonError(f"{len(chosen)} documents match; the limit is {MAX_DOCUMENTS}")
    if sum(len(name.encode("utf-8")) for name, _ in chosen) > MAX_NAME_BYTES:
        raise CanonError(f"the document names are over {MAX_NAME_BYTES} bytes in all")
    sizes: dict[str, int] = {}
    total = 0
    with _cat_file(git_dir, deadline, "--batch-check") as checker:
        for name, oid in chosen:
            size = checker.ask(oid, "blob")
            if size > MAX_DOCUMENT_BYTES:
                raise CanonError(f"'{name}' is {size} bytes; the limit is {MAX_DOCUMENT_BYTES}")
            sizes[name] = size
            total += size
    if total > MAX_TOTAL_BYTES:
        raise CanonError(f"the canon is over {MAX_TOTAL_BYTES} bytes in all")
    documents: list[CanonDocument] = []
    with _cat_file(git_dir, deadline, "--batch") as blobs:
        for name, oid in sorted(chosen):
            size = blobs.ask(oid, "blob")
            if size != sizes[name]:
                raise _fail("object_corrupt", f"the blob {oid} changed size while it was read")
            blob = blobs.body(oid, "blob", size)
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                raise CanonError(f"'{name}' isn't UTF-8 text") from None
            if "\x00" in text:
                raise CanonError(f"'{name}' isn't plain text")
            documents.append(CanonDocument(name=name, text=text))
    return Canon(
        commit=source.commit,
        documents=tuple(documents),
        verification=CanonVerification(
            object_format="sha1",
            tree=root,
            trees=len(walk.trees),
            blobs=len(documents),
            shallow=_is_shallow(git_dir, deadline),
        ),
    )


def _describe(mode: str) -> str:
    return {"120000": "symlink", "160000": "submodule"}.get(mode, f"special file (mode {mode})")


def record_canon_loaded(
    store: Store, run_id: str, canon: Canon, *, org_id: str, actor: str = "system"
) -> str:
    """Record `canon.loaded` for a run of `org_id`; returns the canon hash. A run that belongs to
    another organization is refused (`ForeignRun`) before anything is written."""
    with store.transaction() as uow:
        refuse_foreign_run(uow, org_id, run_id)
        uow.append(
            EventDraft(
                stream=run_stream(run_id),
                type=EventType.CANON_LOADED,
                actor=actor,
                run_id=run_id,
                payload=canon.ledger_payload(),
            )
        )
    return canon.canon_hash
