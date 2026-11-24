"""Merge Gate tier list: the file `.nuramesh/tiers.json`, format `vm-tiers-1`
(tiers spec, TL-1 to TL-32).

The one loader, matcher and tier function (TL-24): the collector, the gate's recompute and
`merge doctor` all call these; there is no second copy.

* **Read from the protected branch, by commit sha** (TL-3, TL-4): `read_tier_list` asks the host
  for the tree entry at `.nuramesh/tiers.json` in one 40-hex commit. Only a regular file (mode
  `100644` or `100755`) is a list; anything else (absent, a directory, a symlink, a gitlink) is
  **missing**. A symlink is never followed. The PR's head is never an argument here.
* **Hash of the raw bytes** (TL-5): computed before any other check, whatever the bytes are; the
  empty-string hash for a missing list.
* **Loading never raises** (TL-9): the result is the three validated lists or exactly one cause
  code. The checks run in the spec's order and the first that fails decides. No message carries a
  byte, a key or a pattern of the file.
* **Patterns** (TL-10, TL-11): `literal`, `*` inside one segment (never crosses `/`) and `**` as a
  whole segment; root-anchored, whole path, case-sensitive, by code point. `?`, `[`, `]`, `{`,
  `}`, `\\` and a leading `!` are refused (TL-12). Not `fnmatch`, not a regular expression.
* **Fail closed** (TL-15): an invalid list makes every PR tier 3 with the cause code; none of its
  patterns is used.
* **Code rules the list cannot lower** (TL-16, TL-26, TL-27, TL-31): a trust root, an unreadable
  path, a submodule pointer and a passed path limit are tier 3; a symlink is never tier 1.
* **Trust roots** (TL-26, TL-28, TL-29): the constant `vm_trust_roots.OWNER_ONLY_PATHS` plus the
  policy's `extra_trust_roots`, which only add. Matched with the same matcher plus the case-fold
  alias rule, against the path as git printed it, before the unreadable-path rule. An invalid
  `extra_trust_roots` makes every PR tier 3 with `trust_roots_invalid`.
* **One entry per path, from NUL-delimited git output** (TL-13, TL-30): `parse_raw_paths` reads
  the raw form of the pinned `diff-1` run (`RAW_PATHS_COMMAND`), never a quoted form; a type
  change is one `T` record and the same path in two records is refused (TL-32).
  `merge_patch_paths` is the same rule for a reader of the patch form (a delete and an add of
  one path are one entry).
* **A failure while computing is not a tier** (TL-32): the parsers raise `RawPathsError` (a
  code); a caller that catches it, or any other exception of the matcher or the tier function,
  builds no bundle or answers `gate_error` `tier_compute_error`. Nothing here invents a tier.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from nuramesh.core.vm_trust_roots import OWNER_ONLY_PATHS

TIERS_PATH = ".nuramesh/tiers.json"
TIER_LIST_VERSIONS = frozenset({"vm-tiers-1"})
"""TL-7: the accept set. A later format is a new string here, never an edit of `vm-tiers-1`."""

MAX_TIER_LIST_BYTES = 262144
MAX_TIER_LIST_DEPTH = 8
MAX_TIER_PATTERNS_PER_LIST = 1000
MAX_TIER_PATTERN_BYTES = 256
MAX_TIER_PATTERN_WILDCARDS = 8
MAX_CHANGED_PATHS = 3000
"""TL-8, TL-31: entries in the changed-path set of one PR (exactly 3000 is allowed)."""
MAX_PATH_BYTES = 4096
"""TL-8, TL-31: one changed path, in bytes as git printed it (exactly 4096 is allowed)."""

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
"""TL-5: `tier_list_sha256` of a missing list (the SHA-256 of the empty byte string)."""

CAUSE_CODES: tuple[str, ...] = (
    "tier_list_missing",
    "tier_list_empty",
    "tier_list_too_large",
    "tier_list_not_utf8",
    "tier_list_not_json",
    "tier_list_duplicate_key",
    "tier_list_not_object",
    "tier_list_missing_key",
    "tier_list_wrong_type",
    "tier_list_unsupported_version",
    "tier_list_unknown_key",
    "tier_list_too_many_patterns",
    "tier_list_bad_pattern",
    "tier_list_duplicate_pattern",
    "tier_list_error",
)
"""TL-9: every cause an invalid list can have. An invalid list has exactly one."""

REGULAR_MODES = frozenset({"100644", "100755"})
SYMLINK_MODE = "120000"
SUBMODULE_MODE = "160000"

_LISTS = ("tier3", "tier1", "tier1_except")
_KEYS = frozenset({"version", *_LISTS})
_REFUSED = frozenset("\\?[]{}")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


# --- the loaded list ------------------------------------------------------------------------------


@dataclass(frozen=True)
class TierList:
    """What the loader returns: the hash of the raw bytes and either the three validated lists
    (`cause` None) or one cause code (the lists are then empty: no pattern of an invalid list is
    ever used, TL-15)."""

    sha256: str
    cause: str | None
    tier3: tuple[str, ...] = ()
    tier1: tuple[str, ...] = ()
    tier1_except: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.cause is None


@dataclass(frozen=True)
class TierListAt:
    """A tier list together with the commit it was read at (TL-3, TL-19): the collector and the
    gate refuse one read anywhere but the protected branch's tip."""

    commit_sha: str
    tier_list: TierList


class TierListReader(Protocol):
    """The host port (TL-3, TL-4). The implementation reads the git object store by commit sha:
    never a ref name, a working tree, the PR head, the forge's file view or a cache keyed by
    anything but the commit sha; no checkout filter, no `.gitattributes` conversion, no LFS, no
    line-ending conversion; a symlink is reported as its own entry and never followed."""

    def tier_list_entry(self, commit_sha: str) -> tuple[str, bytes] | None:
        """The tree entry at `.nuramesh/tiers.json` in that commit as `(mode, raw bytes)` with
        the six-digit octal mode git stores (`100644`, `100755`, `120000`, `160000`, `040000`;
        the bytes of anything but a blob may be empty), or None when the path does not exist."""


def read_tier_list(reader: TierListReader, commit_sha: str) -> TierListAt:
    """TL-3, TL-4: the list as it is in one commit. Anything but a regular file is **missing**.
    `commit_sha` must be a 40-hex commit id (a ref name is refused with `ValueError`); a reader
    that raises is the caller's failure (the collector builds no bundle, the gate answers
    `gate_error`): it is not a fact about the repository, so it is never a cause code."""
    if not isinstance(commit_sha, str) or not _COMMIT.fullmatch(commit_sha):
        raise ValueError("tier_list_commit_not_a_sha")
    entry = reader.tier_list_entry(commit_sha)
    raw: bytes | None = None
    if entry is not None:
        mode, data = entry
        if mode in REGULAR_MODES:
            raw = data
    return TierListAt(commit_sha, load_tier_list(raw))


# --- loading (TL-5 to TL-9) -----------------------------------------------------------------------


class _JsonNumber:
    """Any JSON number: a valid list has none, so its value is never needed (and never parsed:
    no float, no big-integer cost)."""


def _number(_text: str) -> _JsonNumber:
    return _JsonNumber()


def _constant(_name: str) -> Any:
    raise ValueError("constant")  # NaN, Infinity, -Infinity are not RFC 8259


def _too_deep(text: str) -> bool:
    """Nesting of `[` and `{` counted outside strings, checked before the parser runs so the
    parser's own recursion is bounded by the limit."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_TIER_LIST_DEPTH:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def _parse_json(text: str) -> tuple[Any, bool]:
    """One RFC 8259 value followed only by JSON whitespace, and whether any object in it has a
    key twice (keys compared as decoded strings). Raises `ValueError` for text that is not that.
    A `\\uD800`-style escape of a lone surrogate is valid text and is accepted (TL-9 step 5)."""
    duplicate = False

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                duplicate = True
            out[key] = value
        return out

    decoder = json.JSONDecoder(
        object_pairs_hook=pairs,
        parse_constant=_constant,
        parse_int=_number,
        parse_float=_number,
        strict=True,
    )
    return decoder.decode(text), duplicate


def load_tier_list(raw: bytes | None) -> TierList:
    """TL-9: a pure function of the raw bytes (None: the list is missing). Never raises: any
    failure that is not one of the named checks is `tier_list_error`."""
    if raw is None:
        return TierList(EMPTY_SHA256, "tier_list_missing")
    try:
        digest = hashlib.sha256(raw).hexdigest()  # TL-5: before any other check
    except Exception:
        return TierList(EMPTY_SHA256, "tier_list_error")
    try:
        return _load(raw, digest)
    except Exception:  # TL-9 "any": fail closed with a code, never a message
        return TierList(digest, "tier_list_error")


def _load(raw: bytes, digest: str) -> TierList:
    def invalid(cause: str) -> TierList:
        return TierList(digest, cause)

    if len(raw) == 0:
        return invalid("tier_list_empty")
    if len(raw) > MAX_TIER_LIST_BYTES:
        return invalid("tier_list_too_large")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return invalid("tier_list_not_utf8")
    if text.startswith("﻿") or _too_deep(text):
        return invalid("tier_list_not_json")
    try:
        value, duplicate = _parse_json(text)
    except (ValueError, RecursionError):
        return invalid("tier_list_not_json")
    if duplicate:
        return invalid("tier_list_duplicate_key")
    if not isinstance(value, dict):
        return invalid("tier_list_not_object")
    if "version" not in value:
        return invalid("tier_list_missing_key")
    version = value["version"]
    if not isinstance(version, str):
        return invalid("tier_list_wrong_type")
    if version not in TIER_LIST_VERSIONS:
        return invalid("tier_list_unsupported_version")
    if any(key not in _KEYS for key in value):
        return invalid("tier_list_unknown_key")
    if any(key not in value for key in _LISTS):
        return invalid("tier_list_missing_key")
    lists = [value[key] for key in _LISTS]
    for entries in lists:
        if not isinstance(entries, list) or any(not isinstance(e, str) for e in entries):
            return invalid("tier_list_wrong_type")
    if any(len(entries) > MAX_TIER_PATTERNS_PER_LIST for entries in lists):
        return invalid("tier_list_too_many_patterns")
    if any(not valid_pattern(entry) for entries in lists for entry in entries):
        return invalid("tier_list_bad_pattern")
    if any(len(set(entries)) != len(entries) for entries in lists):
        return invalid("tier_list_duplicate_pattern")
    tier3, tier1, tier1_except = (tuple(entries) for entries in lists)
    return TierList(digest, None, tier3=tier3, tier1=tier1, tier1_except=tier1_except)


# --- patterns (TL-10, TL-11) ----------------------------------------------------------------------


def _is_surrogate(ch: str) -> bool:
    return 0xD800 <= ord(ch) <= 0xDFFF


def _is_control(ch: str) -> bool:
    """TL-10 test 2: the control characters a pattern may not hold (U+0000 to U+001F, U+007F)."""
    return ord(ch) < 0x20 or ord(ch) == 0x7F


def valid_pattern(pattern: str) -> bool:
    """TL-10: every test must pass."""
    if not isinstance(pattern, str) or any(_is_surrogate(ch) for ch in pattern):
        return False
    if not 1 <= len(pattern.encode("utf-8")) <= MAX_TIER_PATTERN_BYTES:
        return False
    if any(_is_control(ch) or ch in _REFUSED for ch in pattern):
        return False
    if pattern[0] in "/! " or pattern[-1] == " ":
        return False
    pieces = pattern.split("/")
    if pieces[-1] == "":  # one final `/`: a directory pattern
        pieces.pop()
    if not pieces:
        return False
    for piece in pieces:
        if piece in ("", ".", ".."):
            return False
        if "**" in piece and piece != "**":
            return False
    if pattern.endswith("**/"):
        return False
    wildcards = sum(1 if piece == "**" else piece.count("*") for piece in pieces)
    return wildcards <= MAX_TIER_PATTERN_WILDCARDS


def _segment_matches(pattern: str, segment: str) -> bool:
    """One pattern segment against one path segment: `*` is zero or more characters of that
    segment; every other character is itself."""
    if "*" not in pattern:
        return pattern == segment
    parts = pattern.split("*")
    head, tail = parts[0], parts[-1]
    if len(segment) < len(head) + len(tail):
        return False
    if not segment.startswith(head) or not segment.endswith(tail):
        return False
    position, end = len(head), len(segment) - len(tail)
    for part in parts[1:-1]:
        found = segment.find(part, position, end)
        if found < 0:
            return False
        position = found + len(part)
    return True


def pattern_matches(pattern: str, path: str) -> bool:
    """TL-11 for a pattern that passed `valid_pattern`: anchored at the repository root, the
    whole path, case-sensitive, no normalisation. A final `/` means `/**`; `**` as the last
    segment is one or more path segments, anywhere else zero or more; any other segment is
    exactly one path segment. The cost is bounded: pattern segments times path segments."""
    segments = pattern.split("/")
    if segments[-1] == "":
        segments[-1] = "**"
    last = len(segments) - 1

    def closure(states: set[int]) -> set[int]:
        # a `**` that is not the last segment may match no path segment at all
        out = set(states)
        for index in sorted(states):
            while index < last and segments[index] == "**" and index + 1 not in out:
                index += 1
                out.add(index)
        return out

    states = closure({0})
    for part in path.split("/"):
        following: set[int] = set()
        for index in states:
            if index > last:
                continue
            if segments[index] == "**":
                following.add(index)
                if index == last:
                    following.add(index + 1)
            elif _segment_matches(segments[index], part):
                following.add(index + 1)
        if not following:
            return False
        states = closure(following)
    return last + 1 in states


_UNREADABLE_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x0000, 0x001F),  # (c) C0 control characters
    (0x005C, 0x005C),  # (g) a backslash
    (0x007F, 0x009F),  # (c) DEL and (d) the C1 control characters
    (0x200B, 0x200F),  # (f) zero-width and direction marks
    (0x2028, 0x202E),  # (e) line and paragraph separator, (f) the direction embeddings
    (0x2060, 0x206F),  # (f) invisible and deprecated format characters
    (0xD800, 0xDFFF),  # (a) not valid UTF-8: `surrogateescape` leaves a surrogate
    (0xFEFF, 0xFEFF),  # (f) the byte-order mark
)
"""TL-16: fixed code points, never a Unicode category (the result must not change with the
runtime's Unicode version)."""


def _unreadable_point(ch: str) -> bool:
    point = ord(ch)
    return any(low <= point <= high for low, high in _UNREADABLE_RANGES)


def path_unreadable(path: str) -> bool:
    """TL-16: a path that was not valid UTF-8 (git's bytes decoded with `surrogateescape` leave a
    surrogate; the diff normaliser renders such a path as `hex:` plus hex), that begins with
    `hex:`, or that contains a C0 or C1 control character, DEL, a line or paragraph separator,
    one of the listed invisible or direction-changing format characters, or a backslash. It is
    never matched against the tier list's patterns; it **is** matched against the trust roots
    first (TL-26 rule 3)."""
    return path.startswith("hex:") or any(_unreadable_point(ch) for ch in path)


def path_bytes(path: str) -> int:
    """TL-31: the length of a changed path in bytes as git printed it (`path` is those bytes
    decoded as UTF-8 with `surrogateescape`)."""
    return len(path.encode("utf-8", "surrogateescape"))


# --- trust roots (TL-26, TL-29) -------------------------------------------------------------------


class _Absent:
    """The policy file has no `extra_trust_roots` key."""


ABSENT: Final = _Absent()
"""TL-29: an absent key is the empty array (`null` is not absent: it is an invalid value)."""


@dataclass(frozen=True)
class ExtraTrustRoots:
    """`policy.extra_trust_roots` after TL-29's checks. `valid` False: the value has a wrong
    shape, or an entry fails TL-10 or a limit; then no entry is used and every PR is tier 3 with
    `trust_roots_invalid` (fail closed)."""

    valid: bool
    patterns: tuple[str, ...] = ()


def load_extra_trust_roots(value: object) -> ExtraTrustRoots:
    """TL-29: the array of `.nuramesh/policy.json` as the policy loader read it at the base tip
    (`ABSENT`: no such key; the policy object writes an absent key as `[]`, GC-11 (c)). The raw
    JSON value is judged here and nowhere else: the policy loader carries it as written and never
    refuses the policy for it. An array of at most `MAX_TIER_PATTERNS_PER_LIST` strings, each a
    valid TL-10 pattern (so at most `MAX_TIER_PATTERN_BYTES` bytes and
    `MAX_TIER_PATTERN_WILDCARDS` wildcards); anything else is invalid. Duplicates are harmless
    and dropped. Never raises."""
    if value is ABSENT:
        return ExtraTrustRoots(True)
    if not isinstance(value, list) or len(value) > MAX_TIER_PATTERNS_PER_LIST:
        return ExtraTrustRoots(False)
    patterns: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not valid_pattern(entry):
            return ExtraTrustRoots(False)
        if entry not in patterns:
            patterns.append(entry)
    return ExtraTrustRoots(True, tuple(patterns))


def is_trust_root(path: str, extra: ExtraTrustRoots) -> bool:
    """TL-26: whether a changed path is a trust root: the minimum `OWNER_ONLY_PATHS`, always,
    plus the policy's valid extra entries (they only add). `path` is the path as git printed it
    (UTF-8 with `surrogateescape`, never the `hex:` rendering: rule 2). A line matches by TL-11,
    or by TL-11 on the case-folded path and line (rule 1, the alias rule: on a case-insensitive
    checkout `.Github/x` is the file `.github/x`). The tier list's patterns are never used here
    and never matched this way."""
    folded = path.casefold()
    for pattern in (*OWNER_ONLY_PATHS, *extra.patterns):
        if pattern_matches(pattern, path) or pattern_matches(pattern.casefold(), folded):
            return True
    return False


# --- the tier (TL-13 to TL-18, TL-26, TL-27) ------------------------------------------------------


@dataclass(frozen=True)
class ChangedPath:
    """One path of the PR's own change: exactly one entry per path (TL-30) with its old and its
    new tree-entry mode (TL-13, TL-27). An added path has no old mode, a deleted path no new
    mode; a type change (git prints a delete and an add of one path) is one entry with both.
    `path` is the path as git printed it, decoded as UTF-8 with `surrogateescape`."""

    path: str
    old_mode: str | None = None
    new_mode: str | None = None

    @property
    def modes(self) -> frozenset[str]:
        """The modes the path has before and after (empty: no mode is known)."""
        return frozenset(mode for mode in (self.old_mode, self.new_mode) if mode is not None)


RAW_PATHS_COMMAND: Final[tuple[str, ...]] = (
    "diff",
    "--raw",
    "-z",
    "-r",
    "--ignore-submodules=none",
    "--no-relative",
    "--no-textconv",
    "--no-ext-diff",
    "--no-renames",
)
"""TL-13: the git sub-command of the pinned run `diff-1` whose NUL-delimited output
`parse_raw_paths` reads (followed by `<merge-base> <head>` as 40-hex commit ids). `-z` output is
never quoted, so `core.quotePath` and every other quoting setting have no effect. The scratch
repository and the cleared environment of `diff-1` are the runner's part. A change to any value
is a new pin id (`diff-2`), never an edit."""

_RAW_ENTRY = re.compile(rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{4,64}) ([0-9a-f]{4,64}) ([ADMT])")
_NO_MODE = "000000"


class RawPathsError(ValueError):
    """The bytes are not the raw form of `diff-1`, or a path is there twice (TL-30, TL-32): no
    path set and so no tier. The message is a code and never carries a byte of the input."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def parse_raw_paths(raw: bytes) -> tuple[ChangedPath, ...]:
    """TL-13, TL-30: the changed-path set from NUL-delimited git output, one entry per path,
    sorted by path. Each record is `:<old mode> <new mode> <old id> <new id> <status>` NUL
    `<path>` NUL with the status `A`, `D`, `M` or `T`; only the two modes, the status and the
    path are used. A rename or a copy means the render had renames on: refused. Anything else
    that does not parse (a newline-delimited or quoted form, a mode that is not six octal
    digits, a status that contradicts its modes, a missing NUL, an empty path) raises
    `RawPathsError("raw_paths_invalid")`; the same path in two records raises
    `RawPathsError("raw_paths_duplicate")` (the raw form prints a type change as one `T`
    record, so a repeat is never honest output). Either is a failure of TL-32 for the caller."""
    if raw == b"":
        return ()
    if not raw.endswith(b"\0"):
        raise RawPathsError("raw_paths_invalid")
    tokens = raw[:-1].split(b"\0")
    if len(tokens) % 2:
        raise RawPathsError("raw_paths_invalid")
    seen: dict[str, ChangedPath] = {}
    for meta, name in zip(tokens[0::2], tokens[1::2], strict=True):
        entry = _RAW_ENTRY.fullmatch(meta)
        if entry is None or name == b"":
            raise RawPathsError("raw_paths_invalid")
        old_text, new_text, status = (entry.group(i).decode("ascii") for i in (1, 2, 5))
        old = None if old_text == _NO_MODE else old_text
        new = None if new_text == _NO_MODE else new_text
        if (old is None) != (status == "A") or (new is None) != (status == "D"):
            raise RawPathsError("raw_paths_invalid")
        path = name.decode("utf-8", "surrogateescape")
        if path in seen:
            raise RawPathsError("raw_paths_duplicate")
        seen[path] = ChangedPath(path, old, new)
    return tuple(seen[path] for path in sorted(seen))


def merge_patch_paths(entries: Sequence[ChangedPath]) -> tuple[ChangedPath, ...]:
    """TL-30 for a reader of the **patch** form of `diff-1`, which splits a type change into a
    delete of the path followed by an add of the same path: the two are merged into one entry
    whose old mode is the delete's and whose new mode is the add's (TC-123, TC-124). Any other
    repeat of a path raises `RawPathsError("raw_paths_duplicate")`. Sorted by path."""
    seen: dict[str, list[ChangedPath]] = {}
    for entry in entries:
        seen.setdefault(entry.path, []).append(entry)
    out: list[ChangedPath] = []
    for path in sorted(seen):
        found = seen[path]
        if len(found) == 1:
            out.append(found[0])
            continue
        deletes = [e for e in found if e.old_mode is not None and e.new_mode is None]
        adds = [e for e in found if e.old_mode is None and e.new_mode is not None]
        if len(found) != 2 or len(deletes) != 1 or len(adds) != 1:
            raise RawPathsError("raw_paths_duplicate")
        out.append(ChangedPath(path, deletes[0].old_mode, adds[0].new_mode))
    return tuple(out)


def tier_of(
    paths: Sequence[ChangedPath],
    tier_list: TierList,
    *,
    label: bool,
    sticky: bool,
    extra_trust_roots: ExtraTrustRoots,
) -> tuple[int, tuple[str, ...]]:
    """TL-14: the tier and its reason codes, sorted by code point (codes only, TL-17).

    Tier 3 if any code applies: the label, stickiness, a trust root (the code constant plus the
    policy's extra entries; the list cannot lower it, TL-26), an invalid `extra_trust_roots`
    (every PR, TL-29), an unreadable path (TL-16), a submodule pointer (TL-27 b), a passed path
    limit (TL-31), the cause code of an invalid list (TL-15), a `tier3` match. Else tier 1 only
    when there is a path and every path is a regular file that matches `tier1` and not
    `tier1_except` (a symlink, and a path whose mode is not known to be a regular file's, is
    never tier 1: TL-27 a and e, every doubt resolves upward). Else tier 2.

    Path limits (TL-31): with more than `MAX_CHANGED_PATHS` entries **no** path is matched
    against anything and no path-derived code is computed, only `tier3_path_limit` (the label,
    stickiness, the list's cause and `trust_roots_invalid` apply as usual); a single path longer
    than `MAX_PATH_BYTES` is likewise not judged at all and gives `tier3_path_limit`, the other
    paths being judged as usual.

    The trust roots are not an argument a caller can weaken: the minimum is the constant, and
    each path is matched against it **before** the unreadable-path rule (TL-26 rule 3), so an
    unreadable trust root carries both codes. `paths` holds one entry per path (TL-30:
    `parse_raw_paths` or `merge_patch_paths`). An exception from here is a failure of TL-32
    for the caller, never a tier."""
    codes: set[str] = set()
    if label:
        codes.add("tier3_label")
    if sticky:
        codes.add("tier3_sticky")
    if tier_list.cause is not None:
        codes.add(tier_list.cause)
    if not extra_trust_roots.valid:
        codes.add("trust_roots_invalid")
    if len(paths) > MAX_CHANGED_PATHS:
        codes.add("tier3_path_limit")
        return 3, tuple(sorted(codes))
    readable: list[ChangedPath] = []
    for entry in paths:
        if path_bytes(entry.path) > MAX_PATH_BYTES:
            codes.add("tier3_path_limit")
            continue
        if is_trust_root(entry.path, extra_trust_roots):
            codes.add("trust_root")
        if SUBMODULE_MODE in entry.modes:
            codes.add("tier3_submodule")
        if path_unreadable(entry.path):
            codes.add("tier3_path_unreadable")
        else:
            readable.append(entry)
    if tier_list.valid and any(_any(entry.path, tier_list.tier3) for entry in readable):
        codes.add("tier3_path")
    if codes:
        return 3, tuple(sorted(codes))
    if paths and all(
        entry.modes
        and entry.modes <= REGULAR_MODES
        and _any(entry.path, tier_list.tier1)
        and not _any(entry.path, tier_list.tier1_except)
        for entry in paths
    ):
        return 1, ("tier1_paths",)
    return 2, ("tier2_default",)


def _any(path: str, patterns: Sequence[str]) -> bool:
    return any(pattern_matches(pattern, path) for pattern in patterns)
