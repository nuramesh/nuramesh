"""Merge Gate change hash: the `diff-2` normaliser, with the frozen `diff-1` kept (spec
spec section 4 "Change hash", Fable fix F-2, #944).

The **change hash** identifies a pull request's own change independently of where its lines sit.
The caller (engine: the collector, the signer and the offline verifier, never this module) renders
`git diff --binary --full-index --no-ext-diff --no-renames <merge-base> <head>` under the F-2 pin
(`DIFF_PIN_CONFIG`, no textconv, no external diff, attributes from the base tip only) and passes
the raw bytes here. This module keeps, per file, only

    [path, status, old_mode, new_mode, lines]

with `status` one of `A`, `D`, `M` (renames are off, so a rename is a delete and an add), modes
as git prints them (`None` on the side where the file does not exist), and `lines` the removed
and added lines of the whole file as one flat list `[["-"|"+", text], ...]`: hunks are
concatenated in file order, context lines, hunk headers, offsets and `index` lines are dropped.
A `\\ No newline at end of file` marker becomes `["\\", "nonl"]` right after its line. A line is
taken as bytes without its terminating newline: valid UTF-8 is kept as text; under `diff-2` any
other line is the tagged array `["x", <lower-case hex of the bytes>]` (under the frozen `diff-1`
it was the string `"hex:"` plus the hex, which a text line spelled that way also produced: #944).
A path is encoded the same way. A trailing CR is part of the line; no whitespace is
normalised. A binary file carries its **new blob id** (a 40 or 64 hex string) in place of the
lines list. The canonical JSON (`core.canonical`, LCF-1) of the list of files is hashed with
SHA-256: `change_hash`. `diff_hash` is the SHA-256 of the raw bytes (exact for one merge base
only; informational).

Invariant (stated narrowly in the spec): a clean merge of main into the PR, or a squash onto a
moved main, keeps the hash as long as the removed and added lines are the same; any added or
removed line that differs changes it. It is a stable identifier, not a proof of an identical
tree: carry-forward also needs the clean-merge tree check (`vm_carry`).

Pure stdlib: this module never runs git and never reads the network.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from nuramesh.core.canonical import JsonValue, canonical_json

DIFF1_PIN = "diff-1"
"""The first pin: non-UTF-8 lines and paths as `"hex:"` plus hex (frozen, never edited; #944)."""

DIFF2_PIN = "diff-2"
"""`diff-1` with non-UTF-8 lines and paths as the tagged array `["x", hex]` (#944)."""

DIFF_PIN = DIFF2_PIN
"""The pin every writer uses and records in a bundle's `pins.diff`; a change to any pinned value
is a new id."""

KNOWN_DIFF_PINS = frozenset({DIFF1_PIN, DIFF2_PIN})
"""The pins this module can compute (`diff-1` only to recompute frozen vectors and old objects)."""

DIFF_COMMAND = ("diff", "--binary", "--full-index", "--no-ext-diff", "--no-renames")
"""The git sub-command and flags of the render (followed by `<merge-base> <head>`)."""

DIFF_PIN_CONFIG = (
    "diff.algorithm=myers",
    "diff.renames=false",
    "diff.noprefix=false",
    "core.quotepath=off",
    "diff.indentHeuristic=false",
    "diff.suppressBlankEmpty=false",
)
"""`-c` settings of the render (F-2, plus `diff.suppressBlankEmpty=false` so an empty context
line is always printed as one space, as this parser requires); the engine also runs with
`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, disables textconv and external diff
drivers, and reads attributes from the base tip only."""

Status = Literal["A", "D", "M"]

_HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_INDEX = re.compile(rb"^index ([0-9a-f]+)\.\.([0-9a-f]+)(?: ([0-7]{6}))?$")
_MODE = re.compile(rb"^(old|new|deleted file|new file) mode ([0-7]{6})$")
_FULL_ID = {40, 64}
_RENAME_HEADERS = (b"similarity index ", b"rename from ", b"rename to ", b"copy from ", b"copy to ")
_ESCAPES = {
    b"a": b"\a",
    b"b": b"\b",
    b"f": b"\f",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"v": b"\v",
    b"\\": b"\\",
    b'"': b'"',
}


class DiffFormatError(ValueError):
    """The bytes are not a diff this normaliser accepts. `code` names the problem and `line` the
    1-based line; the message never carries diff content."""

    def __init__(self, code: str, line: int) -> None:
        self.code = code
        self.line = line
        super().__init__(f"diff: {code} at line {line}")


Value = str | bytes
"""A line or a path: text when its bytes are valid UTF-8, otherwise the raw bytes."""


def _encode(value: Value, pin: str) -> JsonValue:
    """The JSON form of a line or a path under `pin` (text is always the text itself)."""
    if isinstance(value, str):
        return value
    if pin == DIFF1_PIN:
        return "hex:" + value.hex()
    return ["x", value.hex()]


@dataclass(frozen=True)
class FileChange:
    """One file of the normalised patch."""

    path: str
    """The path as text; a non-UTF-8 path is `hex:` plus its hex here (display form)."""
    status: Status
    old_mode: str | None
    new_mode: str | None
    lines: tuple[tuple[str, Value], ...] | None
    """Removed and added lines (and `nonl` markers), or None for a binary file; a line that is not
    valid UTF-8 is kept as its bytes."""
    new_blob: str | None = None
    """The new blob id of a binary file (None for a text file)."""
    raw_path: bytes | None = None
    """The path's bytes when they are not valid UTF-8 (None for a text path)."""

    def document(self, pin: str = DIFF_PIN) -> list[JsonValue]:
        """The hashed entry `[path, status, old_mode, new_mode, body]` under `pin`."""
        _check_pin(pin)
        path: JsonValue = self.path if self.raw_path is None else _encode(self.raw_path, pin)
        body: JsonValue = (
            self.new_blob
            if self.lines is None
            else [[tag, _encode(value, pin)] for tag, value in self.lines]
        )
        return [path, self.status, self.old_mode, self.new_mode, body]


def _check_pin(pin: str) -> None:
    if pin not in KNOWN_DIFF_PINS:
        raise ValueError(f"unknown diff pin {pin!r}")


def line_value(raw: bytes) -> Value:
    """A line's or a path's value: the UTF-8 text when valid, otherwise the bytes themselves."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw


def line_text(raw: bytes) -> str:
    """A line's text: the UTF-8 decoding when valid, otherwise `hex:` plus the bytes in hex."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "hex:" + raw.hex()
    try:
        text.encode("utf-8")  # a lone surrogate cannot come from bytes, kept for symmetry
    except UnicodeEncodeError:  # pragma: no cover
        return "hex:" + raw.hex()
    return text


def _unquote(raw: bytes, line: int) -> bytes:
    """A C-style quoted path as git prints it (`"..."` with backslash escapes and octal bytes)."""
    if len(raw) < 2 or raw[-1:] != b'"':
        raise DiffFormatError("bad_path_quote", line)
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i : i + 1]
        if c != b"\\":
            out += c
            i += 1
            continue
        nxt = body[i + 1 : i + 2]
        if nxt in _ESCAPES:
            out += _ESCAPES[nxt]
            i += 2
            continue
        octal = body[i + 1 : i + 4]
        if len(octal) == 3 and all(48 <= b <= 55 for b in octal):
            out.append(int(octal, 8))
            i += 4
            continue
        raise DiffFormatError("bad_path_quote", line)
    return bytes(out)


def _quoted_end(rest: bytes) -> int:
    """Index just past the closing quote of a C-quoted token at the start of `rest`, or -1."""
    i = 1
    while i < len(rest):
        c = rest[i : i + 1]
        if c == b"\\":
            i += 2
            continue
        if c == b'"':
            return i + 1
        i += 1
    return -1


def _header_paths(rest: bytes, line: int) -> tuple[bytes, bytes]:
    """The two names of a `diff --git a/P b/Q` header (P equals Q unless the render had renames
    on, which `_parse_file` reports as `rename_present`)."""
    if rest.startswith(b'"'):
        end = _quoted_end(rest)
        if end < 0 or rest[end : end + 1] != b" ":
            raise DiffFormatError("bad_header", line)
        a_path = _unquote(rest[:end], line)
        right = rest[end + 1 :]
        b_path = _unquote(right, line) if right.startswith(b'"') else right
    else:
        if len(rest) < 7 or (len(rest) - 1) % 2:
            raise DiffFormatError("bad_header", line)
        a_len = (len(rest) - 1) // 2
        a_path, b_path = rest[:a_len], rest[a_len + 1 :]
        if rest[a_len : a_len + 1] != b" ":
            raise DiffFormatError("bad_header", line)
    if not a_path.startswith(b"a/") or not b_path.startswith(b"b/"):
        raise DiffFormatError("bad_header", line)
    return a_path[2:], b_path[2:]


class _Lines:
    """The input split into lines with their 1-based numbers; `peek` never consumes."""

    def __init__(self, raw: bytes) -> None:
        self.items = raw.split(b"\n")
        if self.items and self.items[-1] == b"":
            self.items.pop()
        self.pos = 0

    def peek(self) -> bytes | None:
        return self.items[self.pos] if self.pos < len(self.items) else None

    def take(self) -> bytes:
        item = self.items[self.pos]
        self.pos += 1
        return item

    @property
    def number(self) -> int:
        return self.pos + 1


def _parse_file(lines: _Lines) -> FileChange:
    header = lines.take()
    header_line = lines.number - 1
    a_name, b_name = _header_paths(header[len(b"diff --git ") :], header_line)
    path = line_text(a_name)
    raw_path = None if isinstance(line_value(a_name), str) else a_name
    status: Status = "M"
    old_mode: str | None = None
    new_mode: str | None = None
    new_blob: str | None = None
    saw_index = False
    binary = False
    # extended headers
    while (item := lines.peek()) is not None and not item.startswith(b"@@"):
        if item.startswith(b"diff --git "):
            break
        if any(item.startswith(h) for h in _RENAME_HEADERS):
            raise DiffFormatError("rename_present", lines.number)
        if (mode := _MODE.match(item)) is not None:
            kind, value = mode.group(1), mode.group(2).decode()
            if kind == b"old":
                old_mode = value
            elif kind == b"new":
                new_mode = value
            elif kind == b"deleted file":
                status, old_mode = "D", value
            else:
                status, new_mode = "A", value
            lines.take()
            continue
        if (index := _INDEX.match(item)) is not None:
            old_id, new_id = index.group(1), index.group(2)
            if len(old_id) not in _FULL_ID or len(new_id) not in _FULL_ID:
                raise DiffFormatError("index_not_full", lines.number)
            if index.group(3) is not None:
                old_mode = new_mode = index.group(3).decode()
            new_blob = new_id.decode()
            saw_index = True
            lines.take()
            continue
        if item.startswith((b"--- ", b"+++ ")):
            lines.take()
            continue
        if item.startswith(b"Binary files ") or item == b"GIT binary patch":
            binary = True
            lines.take()
            while (rest := lines.peek()) is not None and not rest.startswith(b"diff --git "):
                lines.take()
            break
        raise DiffFormatError("unexpected_line", lines.number)
    if a_name != b_name:
        raise DiffFormatError("bad_header", header_line)
    if binary:
        if not saw_index or new_blob is None:
            raise DiffFormatError("binary_without_index", header_line)
        return FileChange(path, status, old_mode, new_mode, None, new_blob, raw_path)
    body: list[tuple[str, Value]] = []
    while (item := lines.peek()) is not None and item.startswith(b"@@"):
        hunk = _HUNK.match(item)
        if hunk is None:
            raise DiffFormatError("unexpected_line", lines.number)
        lines.take()
        old_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
        new_left = int(hunk.group(4)) if hunk.group(4) is not None else 1
        last = b""  # tag of the previous hunk line: a marker counts only after `-` or `+`
        while old_left > 0 or new_left > 0:
            item = lines.peek()
            if item is None:
                raise DiffFormatError("hunk_truncated", lines.number)
            tag, text = item[:1], item[1:]
            if tag == b" ":
                old_left -= 1
                new_left -= 1
            elif tag == b"-":
                old_left -= 1
                body.append(("-", line_value(text)))
            elif tag == b"+":
                new_left -= 1
                body.append(("+", line_value(text)))
            elif tag == b"\\":
                if last in (b"-", b"+"):
                    body.append(("\\", "nonl"))
            else:
                raise DiffFormatError("unexpected_line", lines.number)
            if old_left < 0 or new_left < 0:
                raise DiffFormatError("hunk_overrun", lines.number)
            if tag != b"\\":
                last = tag
            lines.take()
        if (item := lines.peek()) is not None and item.startswith(b"\\"):
            if last in (b"-", b"+"):
                body.append(("\\", "nonl"))
            lines.take()
    return FileChange(path, status, old_mode, new_mode, tuple(body), None, raw_path)


def _files(raw: bytes) -> Iterator[FileChange]:
    lines = _Lines(raw)
    while (item := lines.peek()) is not None:
        if not item.startswith(b"diff --git "):
            raise DiffFormatError("unexpected_line", lines.number)
        yield _parse_file(lines)


def parse_diff(raw: bytes) -> list[FileChange]:
    """The files of a rendered diff, in file order; raises `DiffFormatError` on anything that is
    not a `--no-renames --full-index` render (renames, abbreviated ids, truncated hunks)."""
    return list(_files(raw))


def change_document(files: list[FileChange], pin: str = DIFF_PIN) -> list[JsonValue]:
    """The JSON value that is hashed under `pin`: one `[path, status, old_mode, new_mode, lines]`
    per file, in file order."""
    _check_pin(pin)
    return [file.document(pin) for file in files]


def diff1_document(files: list[FileChange]) -> list[JsonValue]:
    """The frozen `diff-1` document (non-UTF-8 lines and paths as `"hex:"` plus hex)."""
    return change_document(files, DIFF1_PIN)


def diff2_document(files: list[FileChange]) -> list[JsonValue]:
    """The `diff-2` document (non-UTF-8 lines and paths as `["x", hex]`)."""
    return change_document(files, DIFF2_PIN)


def change_hash(raw: bytes, pin: str = DIFF_PIN) -> str:
    """SHA-256 (hex) of the canonical JSON of the normalised patch under `pin` (`diff-2` unless
    a frozen `diff-1` value is recomputed); an unknown pin is a `ValueError`."""
    _check_pin(pin)
    return hashlib.sha256(canonical_json(change_document(parse_diff(raw), pin))).hexdigest()


def diff_hash(raw: bytes) -> str:
    """SHA-256 (hex) of the raw rendered diff (informational, exact for one merge base)."""
    return hashlib.sha256(raw).hexdigest()
