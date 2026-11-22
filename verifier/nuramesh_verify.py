#!/usr/bin/env python3
"""nuramesh-verify: check a NuraMesh ledger export against its signed anchors, offline.

Standard library only. This file imports nothing from NuraMesh and shares no code with it: the
hash chain, the canonical JSON rules and the Ed25519 signature check are written out again here
from the specification (`spec/verify/README.md`, `spec/ledger/README.md`), so a bug in the main
verifier cannot hide in this one. Read it top to bottom; the sections follow the specification.

    python3 nuramesh_verify.py LEDGER.jsonl [--anchors DIR --pubkey KEY.pem ...]
                               [--expected-head STREAM=HASH ...] [--require-anchors]
                               [--git [--git-ref REF | --git-commit ID]] [--json]

`--git` is optional and the only part that runs another program (the `git` binary, read-only
commands). Without it nothing but the standard library is used. The output always says which
mode ran: chain, chain+anchors or chain+anchors+git.

Exit code 0: no problem found. 1: problems found. 2: the check could not run (never 0).
What this proves and what it does not: docs/auditor-guide.md.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

VERSION = "1"
MAX_SAFE = 2**53 - 1
GENESIS = "0" * 64
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_ANCHOR_LINE = 4096
NO_STREAM = "-"  # problems that belong to no stream (a line that cannot be read)


# ---------------------------------------------------------------------------------------------
# 1. Reading JSON strictly
# ---------------------------------------------------------------------------------------------


class _Float(float):
    """Any JSON number written with a fraction or exponent, and NaN/Infinity. Never hashable."""


class _DuplicateKey(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def parse_line(text: str) -> Any:
    """`json.loads`, but refusing duplicate keys; floats come back as `_Float` to be found later."""
    return json.loads(
        text,
        object_pairs_hook=_object,
        parse_float=lambda t: _Float(float(t)),
        # Python refuses integers of more than 4300 digits; one that long is an unsafe number
        parse_int=lambda t: int(t) if len(t) <= 400 else _Float(float("inf")),
        parse_constant=lambda t: _Float(float(t.replace("Infinity", "inf").replace("NaN", "nan"))),
    )


# ---------------------------------------------------------------------------------------------
# 2. Canonical JSON (what gets hashed)
# ---------------------------------------------------------------------------------------------


class UnsafeNumber(Exception):
    """A number that cannot be hashed. `where` hides data keys; it never holds a value."""

    def __init__(self, where: str, kind: str) -> None:
        super().__init__(where, kind)
        self.where = where
        self.kind = kind


class BadString(Exception):
    """A string with a lone surrogate (it has no UTF-8 form)."""


_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _string(text: str) -> str:
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise BadString
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_units(key: str) -> list[int]:
    units: list[int] = []
    for ch in key:
        code = ord(ch)
        if code >= 0x10000:
            code -= 0x10000
            units += [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        else:
            units.append(code)
    return units


def canonical(value: Any) -> str:
    """Canonical JSON text of a parsed value (`None`, bool, int, str, list, dict only)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=_utf16_units)
        return "{" + ",".join(f"{_string(k)}:{canonical(value[k])}" for k in keys) + "}"
    raise TypeError(type(value).__name__)


def find_unsafe_number(value: Any, where: str) -> None:
    """Raise `UnsafeNumber` for the first float, NaN, infinity or integer beyond +-(2^53 - 1).
    Object keys are visited in canonical order; the location shows data keys as `[key]`."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, _Float):
        kind = (
            "nan"
            if value != value
            else "infinity"
            if value in (float("inf"), -float("inf"))
            else "float"
        )
        raise UnsafeNumber(where, kind)
    if isinstance(value, int):
        if not -MAX_SAFE <= value <= MAX_SAFE:
            raise UnsafeNumber(where, "integer")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            find_unsafe_number(item, f"{where}[{index}]")
    elif isinstance(value, dict):
        for key in sorted(value, key=_utf16_units):
            find_unsafe_number(value[key], f"{where}[key]")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# 3. Ed25519 verification (RFC 8032 section 5.1.7), verify-only
# ---------------------------------------------------------------------------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, -1, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)  # a square root of -1
type _Point = tuple[int, int, int, int]  # extended coordinates (X, Y, Z, T)


def _add(a: _Point, b: _Point) -> _Point:
    a1 = (a[1] - a[0]) * (b[1] - b[0]) % _P
    b1 = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c1 = 2 * a[3] * b[3] * _D % _P
    d1 = 2 * a[2] * b[2] % _P
    e, f, g, h = b1 - a1, d1 - c1, d1 + c1, b1 + a1
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar: int, point: _Point) -> _Point:
    result: _Point = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(a: _Point, b: _Point) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _decode_point(data: bytes) -> _Point | None:
    """A curve point from 32 bytes; None if the encoding is not canonical or not on the curve."""
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, -1, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _I % _P
    if (x * x - x2) % _P != 0:
        return None
    if x == 0 and sign == 1:
        return None
    if x & 1 != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


_BY = 4 * pow(5, -1, _P) % _P
_BASE: _Point = _decode_point(_BY.to_bytes(32, "little")) or (0, 1, 1, 0)


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a valid signature (cofactorless check `[S]B == R + [h]A`, S < L)."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a_point = _decode_point(public_key)
    r_point = _decode_point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a_point is None or r_point is None or s >= _L:
        return False
    h = (
        int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little")
        % _L
    )
    return _equal(_multiply(s, _BASE), _add(r_point, _multiply(h, a_point)))


_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


class KeyError_(ValueError):
    """A public key file that cannot be used."""


def load_public_key(pem: str) -> bytes:
    """The 32 key bytes of an Ed25519 `PUBLIC KEY` PEM block."""
    lines = [line.strip() for line in pem.strip().splitlines()]
    if (
        len(lines) < 3
        or lines[0] != "-----BEGIN PUBLIC KEY-----"
        or lines[-1] != "-----END PUBLIC KEY-----"
    ):
        raise KeyError_("not a PEM PUBLIC KEY block")
    try:
        der = base64.b64decode("".join(lines[1:-1]), validate=True)
    except ValueError:
        raise KeyError_("the key is not valid base64") from None
    if len(der) != 44 or not der.startswith(_SPKI_PREFIX):
        raise KeyError_("not an Ed25519 public key")
    return der[12:]


def key_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------
# 4. Results
# ---------------------------------------------------------------------------------------------


@dataclass
class Problem:
    stream: str
    code: str
    seq: int | None
    detail: str

    def triple(self) -> tuple[str, str, int | None]:
        return (self.stream, self.code, self.seq)


@dataclass
class StreamResult:
    stream: str
    events: int = 0  # events read
    chain_checked: int = 0  # events verified before the first problem
    head: str | None = None
    anchors: int = 0  # anchors read
    verified_anchors: int = 0
    unanchored: int | None = None
    problems: list[Problem] = field(default_factory=list)


@dataclass
class Report:
    streams: list[StreamResult]
    problems: list[Problem]
    mode: str = "chain"  # what ran: chain, chain+anchors, chain+anchors+git

    @property
    def ok(self) -> bool:
        return not self.problems


# ---------------------------------------------------------------------------------------------
# 5. The ledger: events and the hash chain
# ---------------------------------------------------------------------------------------------

_STREAM = re.compile(r"^(run|org):[a-z0-9][a-z0-9_-]{0,62}$", re.ASCII)
_TYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", re.ASCII)
# Same grammar as the ledger's `Actor` type (core/ledger.py); spec/verifier/actors.md and
# spec/verifier/actors.json keep the two in step. Use only with fullmatch: `$` also matches before a
# trailing newline, so `.match` would let "agent:a\n" through.
_ACTOR = re.compile(
    r"^(system(:[a-z][a-z0-9-]{0,63})?|(agent|service):[A-Za-z0-9._@-]{1,128}"
    r"|operator:[A-Za-z0-9._-]{1,64}|human:[A-Za-z0-9_-]{1,64})$",
    re.ASCII,
)
_AT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$", re.ASCII)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$", re.ASCII)
_AMOUNT = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", re.ASCII)
_BASIS = re.compile(r"^[a-z_]{1,32}$", re.ASCII)
_HASH = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_REQUIRED = ("stream", "seq", "type", "actor", "at", "payload", "prev_hash", "hash")
_OPTIONAL = ("run_id", "task_id", "cost", "cost_basis")
_HASHED = (
    "actor", "at", "cost", "cost_basis", "payload", "prev_hash", "run_id", "seq", "stream",
    "task_id", "type",
)  # fmt: skip


def _real_time(text: str) -> bool:
    if text[17:19] > "59":  # strptime accepts :60 and :61; a leap second is not a valid time here
        return False
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


def _is_text(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def event_shape_error(event: dict[str, Any]) -> str | None:
    """Why `event` breaks the field rules, or None. Never quotes a value."""
    for key in event:
        if key not in _REQUIRED and key not in _OPTIONAL:
            return "has a field that is not part of the format"
    for key in _REQUIRED:
        if key not in event:
            return f"is missing the field '{key}'"
    seq = event["seq"]
    checks: list[tuple[str, bool]] = [
        ("stream", _is_text(event["stream"], _STREAM)),
        ("seq", isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1),
        ("type", _is_text(event["type"], _TYPE)),
        ("actor", _is_text(event["actor"], _ACTOR)),
        ("at", _is_text(event["at"], _AT) and _real_time(event["at"])),
        ("run_id", event.get("run_id") is None or _is_text(event["run_id"], _SLUG)),
        ("task_id", event.get("task_id") is None or _is_text(event["task_id"], _SLUG)),
        ("payload", isinstance(event["payload"], dict)),
        ("cost", _cost_ok(event.get("cost"))),
        ("cost_basis", event.get("cost_basis") is None or _is_text(event["cost_basis"], _BASIS)),
        ("prev_hash", _is_text(event["prev_hash"], _HASH)),
        ("hash", _is_text(event["hash"], _HASH)),
    ]
    for name, good in checks:
        if not good:
            return f"has a '{name}' that breaks the format"
    return None


def _cost_ok(cost: Any) -> bool:
    if cost is None:
        return True
    return (
        isinstance(cost, dict)
        and set(cost) == {"amount", "currency"}
        and _is_text(cost["amount"], _AMOUNT)
        and isinstance(cost["currency"], str)
    )


def event_hash(event: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the 11 hashed fields (a missing optional field is null)."""
    content = {key: event.get(key) for key in _HASHED}
    return sha256_hex(canonical(content))


def _number_problem(error: UnsafeNumber) -> str:
    where = error.where
    return (
        f"number at {where} is not finite"
        if error.kind in ("nan", "infinity")
        else f"number at {where} is not allowed (a float)"
        if error.kind == "float"
        else f"integer at {where} is outside the safe range +-(2^53 - 1)"
    )


def check_event_form(event: dict[str, Any]) -> tuple[str, str] | None:
    """(code, detail) if the event cannot be hashed or breaks a field rule, else None."""
    try:
        return _check_event_form(event)
    except RecursionError:
        return "malformed_event", "the event is nested too deeply to check"


def _check_event_form(event: dict[str, Any]) -> tuple[str, str] | None:
    try:
        for key in sorted(event, key=_utf16_units):
            find_unsafe_number(event[key], "$." + key if key in _HASHED else "$[key]")
    except UnsafeNumber as error:
        return "unsafe_number", _number_problem(error)
    why = event_shape_error(event)
    if why is not None:
        return "malformed_event", f"the event {why}"
    try:
        size = len(canonical(event["payload"]).encode("utf-8"))
    except BadString:
        return "malformed_event", "the event holds a string with a lone surrogate"
    if size > MAX_PAYLOAD_BYTES:
        return "malformed_event", f"the payload is larger than {MAX_PAYLOAD_BYTES} bytes"
    try:
        canonical({key: event.get(key) for key in _HASHED})
    except BadString:
        return "malformed_event", "the event holds a string with a lone surrogate"
    return None


def shown_seq(event: dict[str, Any]) -> int | None:
    """The seq shown with a problem of this event (specification, chain check): its own `seq`
    when that is an integer (never a boolean) within +-(2^53 - 1), else none."""
    seq = event.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and abs(seq) <= MAX_SAFE:
        return seq
    return None


def check_chain(stream: str, events: list[dict[str, Any]]) -> tuple[StreamResult, int | None]:
    """Walk one stream. Returns its result (problems from the chain only) and, if the chain was
    intact, nothing more is needed; the first problem ends the walk (specification, chain check)."""
    result = StreamResult(stream, events=len(events))
    previous: dict[str, Any] | None = None
    for event in events:
        formed = check_event_form(event)
        seq = shown_seq(event)
        if formed is not None:
            result.problems.append(Problem(stream, formed[0], seq, formed[1]))
            return result, None
        expected_seq = 1 if previous is None else previous["seq"] + 1
        if event["seq"] != expected_seq:
            result.problems.append(
                Problem(stream, "wrong_seq", event["seq"], f"expected {expected_seq}")
            )
            return result, None
        if event["prev_hash"] != (GENESIS if previous is None else previous["hash"]):
            result.problems.append(
                Problem(stream, "broken_link", event["seq"], "prev_hash does not match")
            )
            return result, None
        if event_hash(event) != event["hash"]:
            result.problems.append(
                Problem(stream, "bad_hash", event["seq"], "content does not match its hash")
            )
            return result, None
        if previous is not None and event["at"] < previous["at"]:
            result.problems.append(
                Problem(
                    stream, "time_went_backwards", event["seq"], "earlier than the event before"
                )
            )
            return result, None
        previous = event
        result.chain_checked += 1
    result.head = previous["hash"] if previous is not None else None
    return result, (previous["seq"] if previous is not None else None)


# ---------------------------------------------------------------------------------------------
# 6. Anchors
# ---------------------------------------------------------------------------------------------

_KEY_ID = re.compile(r"^[0-9a-f]{16}$", re.ASCII)
_SIG = re.compile(r"^[0-9a-f]{128}$", re.ASCII)
_ANCHOR_FILE = re.compile(r"^(run|org)__[a-z0-9][a-z0-9_-]{0,62}\.jsonl$", re.ASCII)
_ANCHOR_KEYS = {"v", "stream", "seq", "head", "at", "key_id", "sig"}


@dataclass
class Anchor:
    stream: str
    seq: int
    head: str
    at: str
    key_id: str
    sig: str

    def message(self) -> bytes:
        text = canonical(
            {
                "v": 1,
                "stream": self.stream,
                "seq": self.seq,
                "head": self.head,
                "at": self.at,
                "key_id": self.key_id,
            }
        )
        return text.encode("utf-8")


class AnchorFileProblem(ValueError):
    pass


def stream_of_anchor_file(name: str) -> str:
    return name[: -len(".jsonl")].replace("__", ":", 1)


def parse_anchor_file(data: bytes, stream: str) -> list[Anchor]:
    """Read an anchor file strictly (specification, anchor files). Raises `AnchorFileProblem`."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise AnchorFileProblem("not valid UTF-8") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    anchors: list[Anchor] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            raise AnchorFileProblem(f"line {number}: blank lines are not allowed")
        if line.endswith("\r"):
            raise AnchorFileProblem(f"line {number}: ends with a carriage return")
        if len(line) > MAX_ANCHOR_LINE:
            raise AnchorFileProblem(f"line {number}: too long")
        try:
            obj = parse_line(line)
        except ValueError:
            raise AnchorFileProblem(f"line {number}: not valid JSON") from None
        anchors.append(_anchor_from(obj, stream, number))
    return anchors


def _anchor_from(obj: Any, stream: str, number: int) -> Anchor:
    def bad(why: str) -> NoReturn:
        raise AnchorFileProblem(f"line {number}: {why}")

    if not isinstance(obj, dict) or set(obj) != _ANCHOR_KEYS:
        bad("not an anchor (wrong set of fields)")
    v, seq = obj["v"], obj["seq"]
    if not (isinstance(v, int) and not isinstance(v, bool) and v == 1):
        bad("unknown version")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and 1 <= seq <= MAX_SAFE):
        bad("bad seq")
    for name, pattern in (
        ("head", _HASH), ("at", _AT), ("key_id", _KEY_ID), ("sig", _SIG), ("stream", _STREAM),
    ):  # fmt: skip
        if not _is_text(obj[name], pattern):
            bad(f"bad {name}")
    if not _real_time(obj["at"]):
        bad("bad at")
    if obj["stream"] != stream:
        bad("belongs to another stream")
    return Anchor(obj["stream"], seq, obj["head"], obj["at"], obj["key_id"], obj["sig"])


def check_anchors(
    stream: str,
    anchors: list[Anchor],
    keys: dict[str, bytes],
    last_seq: int,
    by_seq: dict[int, str],
    compare: bool = True,
) -> tuple[list[Problem], list[Anchor]]:
    """Signature and consistency problems of one stream's anchors, and the verified anchors."""
    problems: list[Problem] = []
    verified: list[Anchor] = []
    for anchor in anchors:
        public = keys.get(anchor.key_id)
        if public is None:
            problems.append(
                Problem(stream, "wrong_key", anchor.seq, "signed by a key that was not given")
            )
        elif not ed25519_verify(public, anchor.message(), bytes.fromhex(anchor.sig)):
            problems.append(Problem(stream, "bad_signature", anchor.seq, "not validly signed"))
        else:
            verified.append(anchor)
    previous: Anchor | None = None
    for anchor in verified if compare else []:
        if anchor.seq > last_seq:
            problems.append(
                Problem(
                    stream,
                    "stream_shorter",
                    anchor.seq,
                    f"anchored at seq {anchor.seq} but the export ends at {last_seq}",
                )
            )
        elif by_seq.get(anchor.seq) != anchor.head:
            problems.append(
                Problem(
                    stream, "head_mismatch", anchor.seq, "the event there is not the anchored one"
                )
            )
        if previous is not None and (anchor.seq < previous.seq or anchor.at < previous.at):
            problems.append(Problem(stream, "anchors_out_of_order", anchor.seq, "goes backwards"))
        previous = anchor
    return problems, verified


# ---------------------------------------------------------------------------------------------
# 7. Putting it together
# ---------------------------------------------------------------------------------------------


def read_ledger(text: str) -> tuple[dict[str, list[dict[str, Any]]], list[Problem]]:
    """Group the export's lines by stream. Unreadable lines become problems of stream `-`."""
    streams: dict[str, list[dict[str, Any]]] = {}
    problems: list[Problem] = []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, start=1):
        if line.endswith("\r") or not line.strip():
            why = "ends with a carriage return" if line.endswith("\r") else "is blank"
            problems.append(Problem(NO_STREAM, "malformed_line", None, f"line {number} {why}"))
            continue
        try:
            obj = parse_line(line)
        except (ValueError, RecursionError):
            problems.append(
                Problem(NO_STREAM, "malformed_line", None, f"line {number} is not valid JSON")
            )
            continue
        if not isinstance(obj, dict):
            problems.append(
                Problem(NO_STREAM, "malformed_line", None, f"line {number} is not an object")
            )
            continue
        stream = obj.get("stream")
        key = stream if isinstance(stream, str) and _STREAM.fullmatch(stream) else NO_STREAM
        streams.setdefault(key, []).append(obj)
    return streams, problems


def verify(
    ledger_text: str,
    *,
    anchor_files: dict[str, bytes] | None = None,
    public_keys: list[bytes] | None = None,
    expected_heads: dict[str, str] | None = None,
    require_anchors: bool = False,
    git_problems: list[Problem] | None = None,
) -> Report:
    """The whole check (specification). `anchor_files` maps file names to their bytes.
    `git_problems` is the result of the optional Git check (`check_git_history`), or None when
    it was not run."""
    keys = {key_id(k): k for k in public_keys or []}
    grouped, line_problems = read_ledger(ledger_text)
    unreadable = grouped.pop(NO_STREAM, [])
    results: dict[str, StreamResult] = {}
    for stream in sorted(grouped):
        result, _ = check_chain(stream, grouped[stream])
        results[stream] = result
    top: list[Problem] = list(line_problems)
    if unreadable:  # objects without a usable stream: report them, one problem each
        for obj in unreadable:
            formed = check_event_form(obj)
            code, detail = formed or ("malformed_event", "the event has no usable stream")
            top.append(Problem(NO_STREAM, code, shown_seq(obj), detail))
    if not grouped:  # no event of any valid stream: also reported when lines were malformed
        top.append(Problem(NO_STREAM, "empty_export", None, "the export holds no events"))
    for stream, head in sorted((expected_heads or {}).items()):
        result = results.setdefault(stream, StreamResult(stream))
        if stream not in grouped:
            result.problems.append(
                Problem(stream, "stream_missing", None, "no events for this stream")
            )
        elif not result.problems and result.head != head:
            last = grouped[stream][-1]["seq"]
            result.problems.append(
                Problem(
                    stream,
                    "expected_head_mismatch",
                    last,
                    "the last event is not the expected head",
                )
            )
    if anchor_files is not None:
        _check_all_anchors(results, grouped, anchor_files, keys, require_anchors)
    for problem in git_problems or []:  # after the stream's anchor problems
        if problem.stream == NO_STREAM:
            top.append(problem)
        else:
            results.setdefault(problem.stream, StreamResult(problem.stream)).problems.append(
                problem
            )
    all_problems = list(top)
    for stream in sorted(results):
        all_problems += results[stream].problems
    all_problems.sort(key=lambda p: p.stream != NO_STREAM)  # stable: `-` first
    mode = "chain" if anchor_files is None else "chain+anchors"
    if git_problems is not None:
        mode += "+git"
    return Report([results[s] for s in sorted(results)], all_problems, mode)


def _check_all_anchors(
    results: dict[str, StreamResult],
    grouped: dict[str, list[dict[str, Any]]],
    anchor_files: dict[str, bytes],
    keys: dict[str, bytes],
    require_anchors: bool,
) -> None:
    files = {
        stream_of_anchor_file(name): data
        for name, data in anchor_files.items()
        if _ANCHOR_FILE.fullmatch(name)
    }
    for stream in sorted(set(grouped) | set(files)):
        result = results.setdefault(stream, StreamResult(stream))
        events = grouped.get(stream, [])
        if (
            stream in files
            and not events
            and not any(p.code == "stream_missing" for p in result.problems)
        ):  # reported once, even when --expected-head names the stream too
            result.problems.append(
                Problem(
                    stream, "stream_missing", None, "anchors exist but the export has no events"
                )
            )
        anchors: list[Anchor] = []
        if stream in files:
            try:
                anchors = parse_anchor_file(files[stream], stream)
            except AnchorFileProblem as error:
                result.problems.append(Problem(stream, "unreadable_anchor_file", None, str(error)))
        result.anchors = len(anchors)
        last_seq = max((e["seq"] for e in events if _seq_ok(e)), default=0)
        by_seq = {
            e["seq"]: e["hash"] for e in events if _seq_ok(e) and isinstance(e.get("hash"), str)
        }
        found, verified = check_anchors(
            stream, anchors, keys, last_seq, by_seq, compare=bool(events)
        )
        result.problems += found
        result.verified_anchors = len(verified)
        if events:
            result.unanchored = (
                max(0, last_seq - verified[-1].seq) if verified else len(events)
            )  # no verified anchor: the number of events
            if require_anchors and not verified:
                result.problems.append(
                    Problem(stream, "unanchored", None, "no verified anchor for this stream")
                )


def _seq_ok(event: dict[str, Any]) -> bool:
    return isinstance(event.get("seq"), int) and not isinstance(event.get("seq"), bool)


# ---------------------------------------------------------------------------------------------
# 7b. Optional: is the anchors repository's history append-only? (`--git`, ND107)
# ---------------------------------------------------------------------------------------------
# This is the only part of the file that starts another program. It runs the `git` binary with
# read-only plumbing commands, never through a shell, with the repository's hooks and fsmonitor
# off and no user or system configuration. Without `--git` none of this code runs.

GIT_TIMEOUT_SECONDS = 60
_COMMIT_ID = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$", re.ASCII)
_COMMIT_ID_40 = re.compile(r"^[0-9a-f]{40}$", re.ASCII)  # what --git-commit takes
_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$", re.ASCII)
LOCAL_ANCHOR_REF = "refs/heads/ledger-anchors"
REMOTE_ANCHOR_REF = "refs/remotes/origin/ledger-anchors"


class UsageError(Exception):
    """The check could not run."""


def find_git() -> str:
    """The `git` binary, or `UsageError`: asking for the Git check and not getting it is never a
    quiet skip."""
    found = shutil.which("git")
    if found is None:
        raise UsageError("--git needs the git program, and none was found on the PATH")
    return found


# `-c` settings win over the repository's own config file. Every key that can start a program in
# the commands used here (cat-file, rev-parse, rev-list, ls-tree, merge-base, for-each-ref, log) or
# in one that could be started by mistake is set to nothing: signature checking (gpg.*,
# log.showSignature), the file-system monitor, hooks, pagers, external diff, attribute files,
# ssh and credential helpers, alternate-ref helpers and the remote protocols.
_GIT_NEUTRAL_CONFIG = (
    "log.showSignature=false",
    "core.fsmonitor=false",
    "core.hooksPath=" + os.devnull,
    "core.pager=cat",
    "pager.log=false",
    "core.sshCommand=false",
    "core.askPass=",
    "core.attributesFile=" + os.devnull,
    "core.alternateRefsCommand=",
    "core.editor=false",
    "diff.external=",
    "gpg.program=false",
    "gpg.openpgp.program=false",
    "gpg.x509.program=false",
    "gpg.ssh.program=false",
    "credential.helper=",
    "protocol.allow=never",
    "protocol.file.allow=never",
    "protocol.ext.allow=never",
    "protocol.ssh.allow=never",
    "protocol.git.allow=never",
    "protocol.http.allow=never",
    "protocol.https.allow=never",
    "gc.auto=0",
    "maintenance.auto=false",
    "uploadpack.packObjectsHook=",
)


def _git(
    binary: str, root: Path, *args: str, stdin: bytes | None = None, ok: tuple[int, ...] = (0,)
) -> tuple[int, bytes]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_DIR": str(root / ".git"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PAGER": "cat",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_ASKPASS": "",
        "GIT_SSH_COMMAND": "",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",  # a missing object is an error, never a fetch
        "LC_ALL": "C",
    }
    command = [binary, "--no-pager", "--no-replace-objects"]
    for setting in _GIT_NEUTRAL_CONFIG:
        command += ["-c", setting]
    command += list(args)
    try:
        done = subprocess.run(
            command,
            cwd=root,
            env=env,
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise UsageError(f"git {args[-1] if args else ''} took longer than 60 seconds") from None
    except OSError as error:
        raise UsageError(f"cannot run git: {error.strerror or type(error).__name__}") from None
    if done.returncode not in ok:
        raise UsageError(f"git failed in {root} (is it a repository, and is it complete?)")
    return done.returncode, done.stdout


def _commit_of(binary: str, root: Path, ref: str) -> str | None:
    """The commit of the exact ref name `ref` as stored (a tag whose name is the same text,
    under refs/tags/, is not it), or None."""
    listed = _git(binary, root, "for-each-ref", "--format=%(objectname) %(refname)", ref)[1]
    for line in listed.decode("utf-8", "replace").splitlines():
        oid, _, name = line.partition(" ")
        if name == ref and _COMMIT_ID.fullmatch(oid):
            spec = f"{oid}^{{commit}}"
            code, out = _git(binary, root, "rev-parse", "--verify", "-q", spec, ok=(0, 1))
            return out.decode("ascii", "replace").strip() if code == 0 else None
    return None


def _is_ancestor(binary: str, root: Path, older: str, newer: str) -> bool:
    return _git(binary, root, "merge-base", "--is-ancestor", older, newer, ok=(0, 1))[0] == 0


def _exact_commit(binary: str, root: Path, commit: str) -> str:
    """`commit` (already checked to be 40 lower-case hex digits) if that exact object is a
    commit in the repository. A tag object, a tree or a blob is refused, never peeled."""
    out = _git(binary, root, "cat-file", "--batch-check", stdin=f"{commit}\n".encode("ascii"))[1]
    parts = out.decode("ascii", "replace").split()
    if parts[1:2] == ["missing"]:
        raise UsageError(f"the commit {commit} does not exist in {root}")
    if len(parts) != 3 or parts[0] != commit or parts[1] != "commit":
        raise UsageError(f"{commit} is not a commit (a tag, tree or file id is not accepted)")
    return commit


def _tip(binary: str, root: Path, ref: str | None, commit: str | None = None) -> str | Problem:
    """The commit to check, or the `anchor_branch_diverged` problem (specification)."""
    if commit is not None:
        return _exact_commit(binary, root, commit)
    if ref is not None:
        if ref.startswith("-"):
            raise UsageError("--git-ref must be a branch or ref name")
        if ref == "HEAD" or _COMMIT_ID.fullmatch(ref):
            raise UsageError(
                "--git-ref must be a branch name, not HEAD, a tag or a commit id; to check a"
                " commit pass its 40 lower-case hex digit id with --git-commit <commit-id> (for a"
                " tag, resolve it first with git rev-parse <tag>^{commit})"
            )
        if not _REF_NAME.fullmatch(ref) or ".." in ref or ref.endswith((".", "/", ".lock")):
            raise UsageError("--git-ref must be a branch name (short or full), not a revision")
        # A full name (refs/...) as written; a short name is a local branch, else a
        # remote-tracking one. Never a tag, a commit id, HEAD or a revision expression.
        if ref.startswith("refs/"):
            if not ref.startswith(("refs/heads/", "refs/remotes/")):
                raise UsageError("--git-ref must name a branch (refs/heads/ or refs/remotes/)")
            full = [ref]
        else:
            full = [f"refs/heads/{ref}", f"refs/remotes/{ref}"]
        found = next((c for c in (_commit_of(binary, root, f) for f in full) if c), None)
        if found is None:
            raise UsageError(f"the ref {ref} does not exist in {root}")
        return found
    local = _commit_of(binary, root, LOCAL_ANCHOR_REF)
    remote = _commit_of(binary, root, REMOTE_ANCHOR_REF)
    if local is None and remote is None:
        raise UsageError(f"{root} has no ledger-anchors branch (use --git-ref to name one)")
    if local is None or remote is None:
        return local or remote or ""
    if local == remote or _is_ancestor(binary, root, remote, local):
        return local
    if _is_ancestor(binary, root, local, remote):
        return remote
    return Problem(
        NO_STREAM,
        "anchor_branch_diverged",
        None,
        "the local and remote-tracking ledger-anchors branches have diverged",
    )


def _blobs(
    binary: str, root: Path, commits: list[str], names: list[str]
) -> dict[tuple[str, str], bytes | None]:
    """The bytes of every name in every commit. None only where the commit's tree has no such
    entry; an entry whose object cannot be read is `UsageError`, never "no such file"."""
    wanted = set(names)
    entries: dict[tuple[str, str], str] = {}
    for commit in commits:
        listing = _git(binary, root, "ls-tree", "-z", commit)[1]
        for record in listing.split(b"\0"):
            meta, _, raw_name = record.partition(b"\t")
            name = raw_name.decode("utf-8", "replace")
            if name not in wanted:
                continue
            fields = meta.split()
            if (
                len(fields) != 3
                or fields[1] != b"blob"
                or not _COMMIT_ID.fullmatch(fields[2].decode("ascii", "replace"))
            ):
                raise UsageError("git returned something unexpected for an anchor file")
            entries[(commit, name)] = fields[2].decode("ascii")
    ids = sorted(set(entries.values()))
    contents: dict[str, bytes] = {}
    if ids:
        out = _git(
            binary, root, "cat-file", "--batch", stdin="".join(f"{i}\n" for i in ids).encode()
        )[1]
        position = 0
        for oid in ids:
            end = out.index(b"\n", position)
            header = out[position:end].split()
            position = end + 1
            if (
                len(header) != 3
                or header[0].decode("ascii", "replace") != oid
                or header[1] != b"blob"
            ):
                raise UsageError(f"an anchor file's content ({oid}) cannot be read from git")
            size = int(header[2])
            contents[oid] = out[position : position + size]
            position += size + 1
    return {
        (c, n): contents[entries[(c, n)]] if (c, n) in entries else None
        for c in commits
        for n in names
    }


def check_git_history(
    root: Path, files: dict[str, bytes], ref: str | None = None, commit: str | None = None
) -> list[Problem]:
    """Is the anchors repository's history append-only? (specification, Git mode). `files` are
    the anchor files read from `root`; `ref` a branch or `commit` an exact commit id to read
    instead of the default branch. Raises `UsageError` when the check cannot run."""
    binary = find_git()
    dot_git = root / ".git"
    if not dot_git.is_dir() or dot_git.is_symlink():
        raise UsageError(f"{root} is not the top of a git repository (no .git directory)")
    shallow = _git(binary, root, "rev-parse", "--is-shallow-repository")[1].strip() == b"true"
    if shallow or (dot_git / "shallow").exists():
        raise UsageError("the anchors repository is a shallow clone: its history is incomplete")
    tip = _tip(binary, root, ref, commit)
    if isinstance(tip, Problem):
        return [tip]
    commits = _git(binary, root, "rev-list", tip)[1].decode("ascii").split()
    logged = _git(
        binary, root, "-c", "log.showRoot=true", "log", "--full-history", "-m", "--no-renames",
        "--name-only", "--format=", tip,
    )[1]  # fmt: skip
    top = _git(binary, root, "ls-tree", "--name-only", tip)[1]
    seen = {
        line
        for text in (logged, top)
        for line in text.decode("utf-8", "replace").splitlines()
        if _ANCHOR_FILE.fullmatch(line)
    }
    names = sorted(seen | {n for n in files if _ANCHOR_FILE.fullmatch(n)})
    blobs = _blobs(binary, root, commits, names)
    problems: list[Problem] = []
    for name in names:
        stream = stream_of_anchor_file(name)
        committed = blobs[(tip, name)]
        for commit in commits:
            version = blobs[(commit, name)]
            if version is not None and (committed is None or not committed.startswith(version)):
                if committed is None:
                    problems.append(
                        Problem(stream, "anchor_file_removed", None, f"{name} was removed")
                    )
                else:
                    problems.append(
                        Problem(
                            stream,
                            "history_rewritten",
                            None,
                            f"an earlier version of {name} is not the start of the current one",
                        )
                    )
                break
        if files.get(name) != committed:
            problems.append(
                Problem(
                    stream,
                    "uncommitted_anchor_file",
                    None,
                    f"{name} in the directory is not what the branch holds",
                )
            )
    return problems


# ---------------------------------------------------------------------------------------------
# 8. Command line
# ---------------------------------------------------------------------------------------------


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise UsageError(f"cannot read {path}: {error.strerror or type(error).__name__}") from None


def run(argv: list[str], out: Callable[[str], object] = print) -> int:
    parser = argparse.ArgumentParser(
        prog="nuramesh_verify",
        description="Check a NuraMesh ledger export against signed anchors, offline.",
    )
    parser.add_argument("ledger", help="the ledger export (JSON Lines)")
    parser.add_argument("--anchors", metavar="DIR", help="directory of anchor files (*.jsonl)")
    parser.add_argument(
        "--pubkey",
        metavar="FILE",
        action="append",
        default=[],
        help="Ed25519 public key (PEM); repeatable",
    )
    parser.add_argument("--expected-head", metavar="STREAM=HASH", action="append", default=[])
    parser.add_argument(
        "--require-anchors",
        action="store_true",
        help="a stream without a verified anchor is a problem",
    )
    parser.add_argument(
        "--git",
        action="store_true",
        help="also check the anchors repository's history is append-only (needs the git program)",
    )
    parser.add_argument(
        "--git-ref", metavar="REF", help="with --git: the branch to read (default ledger-anchors)"
    )
    parser.add_argument(
        "--git-commit",
        metavar="COMMIT",
        help="with --git: read exactly this commit (40 lower-case hex digits) instead of a branch",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2
    try:
        report = _run(args)
    except UsageError as error:
        print(f"nuramesh_verify: could not check: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # a crash is "could not check", never a pass and never "tampered"
        print(
            f"nuramesh_verify: could not check: internal error ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2
    out(json.dumps(_as_json(report), indent=2) if args.json else _as_text(report))
    return 0 if report.ok else 1


def _run(args: argparse.Namespace) -> Report:
    if args.anchors is None and (args.pubkey or args.require_anchors):
        raise UsageError("--pubkey and --require-anchors need --anchors")
    if args.anchors is not None and not args.pubkey:
        raise UsageError("--anchors needs --pubkey (the public key that signed them)")
    if (args.git or args.git_ref is not None or args.git_commit is not None) and (
        args.anchors is None
    ):
        raise UsageError("--git needs --anchors (the anchors repository checkout)")
    if args.git_ref is not None and not args.git:
        raise UsageError("--git-ref needs --git")
    if args.git_commit is not None:
        if not args.git:
            raise UsageError("--git-commit needs --git")
        if args.git_ref is not None:
            raise UsageError("--git-commit and --git-ref cannot be used together")
        if not _COMMIT_ID_40.fullmatch(args.git_commit):
            raise UsageError(
                "--git-commit needs a commit id of exactly 40 lower-case hex digits"
                " (git rev-parse <name> prints one); use --git-ref for a branch name"
            )
    if args.git:
        find_git()  # asked for and missing is "could not check", before anything else is read
    expected: dict[str, str] = {}
    for item in args.expected_head:
        stream, _, digest = item.partition("=")
        if not _STREAM.fullmatch(stream) or not _HASH.fullmatch(digest):
            raise UsageError("--expected-head needs STREAM=<64 lower-case hex digits>")
        expected[stream] = digest
    try:
        text = _read_bytes(Path(args.ledger)).decode("utf-8")
    except UnicodeDecodeError:
        raise UsageError("the ledger export is not valid UTF-8") from None
    keys: list[bytes] = []
    for path in args.pubkey:
        try:
            keys.append(load_public_key(_read_bytes(Path(path)).decode("utf-8")))
        except (KeyError_, UnicodeDecodeError) as error:
            raise UsageError(f"{path}: {error}") from None
    files: dict[str, bytes] | None = None
    if args.anchors is not None:
        root = Path(args.anchors).resolve()  # the Git commands run inside it: no relative path
        if not root.is_dir():
            raise UsageError(f"{root} is not a directory")
        files = {
            entry.name: _read_bytes(entry)
            for entry in sorted(root.iterdir())
            if entry.is_file() and _ANCHOR_FILE.fullmatch(entry.name)
        }
    git_problems = (
        check_git_history(root, files or {}, args.git_ref, args.git_commit) if args.git else None
    )
    return verify(
        text,
        anchor_files=files,
        public_keys=keys,
        expected_heads=expected,
        require_anchors=args.require_anchors,
        git_problems=git_problems,
    )


def _plain(text: str) -> str:
    """Text for a terminal: control characters (a newline in an untrusted value) are escaped."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in text)


def _as_text(report: Report) -> str:
    lines: list[str] = [f"mode: {report.mode}"]
    for result in report.streams:
        head = f"head {result.head[:16]}..." if result.head else "no head"
        state = (
            "chain ok"
            if not [p for p in result.problems if p.code in _CHAIN_CODES]
            else "CHAIN BROKEN"
        )
        anchors = ""
        if result.unanchored is not None:
            anchors = (
                f"; {result.verified_anchors} of {result.anchors} anchors verified,"
                f" {result.unanchored} events newer than the last one"
            )
        lines.append(f"{result.stream}: {result.events} events, {head}, {state}{anchors}")
    for problem in report.problems:
        where = f" at seq {problem.seq}" if problem.seq is not None else ""
        lines.append(
            f"PROBLEM {_plain(problem.stream)} {problem.code}{where}: {_plain(problem.detail)}"
        )
    if report.ok:
        lines.append(
            f"RESULT: no problems found (mode: {report.mode}; "
            "see docs/auditor-guide.md for what that does not prove)"
        )
    else:
        lines.append(f"RESULT: {len(report.problems)} problem(s) found")
    return "\n".join(lines)


_CHAIN_CODES = {
    "wrong_seq", "broken_link", "bad_hash", "time_went_backwards", "malformed_event",
    "unsafe_number",
}  # fmt: skip


def _as_json(report: Report) -> dict[str, Any]:
    return {
        "tool": "nuramesh-verify",
        "version": VERSION,
        "ok": report.ok,
        "mode": report.mode,
        "streams": [
            {
                "stream": r.stream,
                "events": r.events,
                "chain_checked": r.chain_checked,
                "head": r.head,
                "anchors": r.anchors,
                "verified_anchors": r.verified_anchors,
                "unanchored": r.unanchored,
            }
            for r in report.streams
        ],
        "problems": [
            {"stream": p.stream, "code": p.code, "seq": p.seq, "detail": p.detail}
            for p in report.problems
        ],
    }


def main(argv: Iterable[str] | None = None) -> int:
    return run(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
