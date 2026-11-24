"""Canonical JSON and hashing: the same content always produces the same bytes and hash.

Used for org version hashes and the ledger's hash chain, so Python and TypeScript must produce
identical bytes:

- object keys are sorted by UTF-16 code units (as JavaScript and RFC 8785 do), not code points;
- integers must fit in ±(2**53 - 1) so JavaScript represents them exactly;
- floats are rejected here (their text forms differ); `canonical_json_jcs` (task outputs only,
  never the ledger or any hash of org data, ND78) writes them by the RFC 8785 number rule;
  decimals (money, prices) are written as JSON
  strings in plain notation (`decimal_text`: `0.0000001`, never `1E-7`; trailing zeros kept, so
  `1.0` and `1` hash differently on purpose; negative zero written as zero);
- strings are escaped exactly as JavaScript's `JSON.stringify` does and hashed as given (no
  Unicode normalisation); lone surrogates are rejected.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

type JsonValue = None | bool | int | str | Decimal | Sequence[JsonValue] | Mapping[str, JsonValue]


MAX_SAFE_INTEGER = 2**53 - 1


def _utf16_order(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def decimal_text(value: Decimal) -> str:
    """Plain-notation text of a finite decimal: `1E-7` → `0.0000001`, `-0.00` → `0.00`."""
    if not value.is_finite():
        raise ValueError("non-finite decimal")
    text = format(value, "f")
    return text[1:] if value.is_zero() and text.startswith("-") else text


def _check_string(value: str, path: "Path") -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"string at {path_text(path)} contains a lone surrogate") from exc
    return value


class _Number:
    """A float already written as RFC 8785 text (only `canonical_json_jcs` makes these)."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def jcs_number_text(value: float) -> str:
    """The RFC 8785 (ECMAScript `Number::toString`) text of a finite float: shortest round-trip
    digits, plain notation for 1e-6 <= |x| < 1e21, otherwise `d.ddde+XX`; minus zero is `0`."""
    if not math.isfinite(value):
        raise ValueError(f"number is not finite ({'NaN' if math.isnan(value) else 'Infinity'})")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    parts = Decimal(repr(abs(value))).normalize().as_tuple()  # repr: shortest round-trip digits
    digits = "".join(map(str, parts.digits))
    assert isinstance(parts.exponent, int)
    k, n = len(digits), len(digits) + parts.exponent  # the value is 0.<digits> * 10**n
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return f"{sign}{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:
        return f"{sign}0.{'0' * -n}{digits}"
    e = n - 1
    mantissa = digits if k == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"


type Path = tuple[str | int, ...]


def path_text(path: Path) -> str:
    """`$` for the root, then `.key` for an object member and `[index]` for an array element."""
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)


class UnsafeNumberError(ValueError):
    """A number canonical JSON cannot write: NaN, an infinity, or an integer beyond
    ±(2**53 - 1). `path` holds the location as keys and indexes; `str(error)` names the path with
    its keys (for a developer), `public_message()` hides them (for a ledger or a person).

    Never persist or show `str(error)`, `args` or `path`: a path step can be a data key, and a key
    such as `"a[0].b"` reads like a path of its own, so the text is ambiguous as well as
    data-bearing. Anything stored or shown uses `public_message()`."""

    def __init__(self, path: Path, kind: Literal["nan", "infinity", "integer"]) -> None:
        self.path = path
        self.kind = kind  # "nan", "infinity" or "integer"
        word = {"nan": "NaN", "infinity": "Infinity"}.get(kind)
        if word is None:
            text = f"integer at {path_text(path)} is outside the JavaScript safe range"
        else:
            text = f"number at {path_text(path)} is not finite ({word})"
        super().__init__(text)

    def public_path(self, fixed: int = 0) -> str:
        """The masked path: object keys are written `[key]` (they come from the data), array
        indexes as numbers; the first `fixed` steps are names a schema fixes (shown as `.name`)."""
        steps = []
        for i, p in enumerate(self.path):
            if isinstance(p, int):
                steps.append(f"[{p}]")
            else:
                steps.append(f".{p}" if i < fixed else "[key]")
        return "$" + "".join(steps)

    def public_message(self, fixed: int = 0) -> str:
        """The error without data: the masked path (`public_path`), never the value."""
        where = self.public_path(fixed)
        if self.kind == "integer":
            return f"integer at {where} is outside the safe range +-(2^53 - 1)"
        return f"number at {where} is not finite"


def _normalise(value: Any, path: Path, floats: bool = False) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _check_string(value, path)
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise UnsafeNumberError(path, "integer")
        return value
    if isinstance(value, float) and floats:
        if not math.isfinite(value):
            raise UnsafeNumberError(path, "nan" if math.isnan(value) else "infinity")
        return _Number(jcs_number_text(value))
    if isinstance(value, float):
        raise TypeError(f"float at {path_text(path)} is not allowed in canonical JSON; use Decimal")
    if isinstance(value, Decimal):
        if not value.is_finite():
            if floats:  # the task-output path names the field like any other unsafe number
                raise UnsafeNumberError(path, "nan" if value.is_nan() else "infinity")
            raise ValueError(f"non-finite Decimal at {path_text(path)}")
        return decimal_text(value)
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise TypeError(f"non-string key {key!r} at {path_text(path)}")
            _check_string(key, path)
        return {
            key: _normalise(value[key], (*path, key), floats)
            for key in sorted(value, key=_utf16_order)
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_normalise(item, (*path, index), floats) for index, item in enumerate(value)]
    raise TypeError(f"{type(value).__name__} at {path_text(path)} is not JSON-compatible")


def canonical_json(value: JsonValue) -> bytes:
    """Canonical JSON: keys in UTF-16 order, no whitespace, UTF-8, Decimals as strings."""
    # Keys are already in UTF-16 order; json.dumps keeps dict insertion order.
    return json.dumps(_normalise(value, ()), separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _dump(value: Any) -> str:
    if isinstance(value, _Number):
        return value.text
    if isinstance(value, dict):
        return "{" + ",".join(f"{_dump(k)}:{_dump(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_dump(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _scan_unsafe(value: Any, path: Path) -> None:
    """Raise `UnsafeNumberError` for the first unsafe number, walking keys in sorted order and
    ignoring every other kind of problem, so an unsafe number wins over a type error and the
    result does not depend on the order of an object's keys."""
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise UnsafeNumberError(path, "integer")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise UnsafeNumberError(path, "nan" if math.isnan(value) else "infinity")
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise UnsafeNumberError(path, "nan" if value.is_nan() else "infinity")
    elif isinstance(value, Mapping):
        for key in sorted((k for k in value if isinstance(k, str)), key=_utf16_order):
            _scan_unsafe(value[key], (*path, key))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(value):
            _scan_unsafe(item, (*path, index))


def canonical_json_jcs(value: Any) -> bytes:
    """Canonical JSON for a task output (ND78): as `canonical_json`, but a float is allowed and
    written by the RFC 8785 number rule. NaN, infinity and an integer beyond ±(2**53 - 1) raise
    `UnsafeNumberError` (a `ValueError`), each naming the JSON path (`$.a[1].b`). Never used for
    the ledger."""
    _scan_unsafe(value, ())
    return _dump(_normalise(value, (), True)).encode("utf-8")


_MAX_INT_DIGITS = 309  # 1e309 is beyond the largest double


def _read_float(text: str) -> float:
    """`float(text)`, refusing a literal that overflows a double or underflows to zero. The
    message names neither the literal nor any key."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number literal overflows a double (overflow)")
    if value == 0 and any(c in "123456789" for c in text.split("e")[0].split("E")[0]):
        raise ValueError("number literal underflows a double (underflow)")
    return value


def _read_int(text: str) -> int | float:
    if len(text.lstrip("-")) > _MAX_INT_DIGITS:  # always beyond a double; no int() of a huge text
        raise ValueError("number literal overflows a double (overflow)")
    value = int(text)
    return value if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER else _read_float(text)


def _refuse_constant(text: str) -> Any:
    raise ValueError(f"{text} is not JSON")


def read_jcs(text: str | bytes) -> Any:
    """Read back text written by `canonical_json_jcs`, so writing it again gives the same text.

    A whole float is written without a point (`1e20` is `100000000000000000000`). Plain
    `json.loads` would turn such a number from 2**53 up to 1e21 into an `int` beyond the safe
    range, which `canonical_json_jcs` then refuses; here an integer beyond ±(2**53 - 1) is read as
    the float it was written from. `NaN` and `Infinity` (never written) are refused, and so is a
    literal that overflows a double (`1e400`) or underflows a nonzero mantissa to zero
    (`1e-400`)."""
    return json.loads(
        text, parse_int=_read_int, parse_float=_read_float, parse_constant=_refuse_constant
    )


def sha256_hex(data: bytes) -> str:
    """Lower-case hex SHA-256 of `data`."""
    return hashlib.sha256(data).hexdigest()


def content_hash(value: JsonValue) -> str:
    """SHA-256 of the canonical JSON form of `value`."""
    return sha256_hex(canonical_json(value))


def freeze(value: Any) -> Any:
    """A read-only deep copy: mappings become `MappingProxyType`, lists and tuples become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """The plain JSON-shaped form of a frozen value (dicts and lists)."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def canonical_value(value: JsonValue) -> Any:
    """`value` as it reads back from storage: canonical JSON parsed again, then frozen.

    Decimals become their plain-notation strings, tuples become lists, keys are sorted. Storing
    this form means a save-and-load round trip can never change a hash.
    """
    return freeze(json.loads(canonical_json(value)))
