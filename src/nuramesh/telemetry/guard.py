"""The value guard: the last line of defence against content in an export (spec section 6.1).

Every string that is not from a closed set must look like an identifier: no free text, no email,
no file path, no person, nothing key-shaped (secret prefixes, unbroken 32+ runs). A value that
fails is dropped by the caller and counted (`dropped_attributes`). Pure functions, stdlib only.
"""

import re
from collections.abc import Collection
from decimal import Decimal, InvalidOperation

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/@+-]{1,128}$")
_EMAIL = re.compile(r"^[^@]+@[^@]+\.[A-Za-z]{2,}$")
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_KEY_PREFIX = re.compile(r"(?<![A-Za-z0-9])(?:sk-|ghp_|gho_|github_pat_|AKIA|xox[bpas]-)")
_KEY_RUN = re.compile(r"[A-Za-z0-9+/]{32,}")  # covers unbroken hex and base64 runs
_PERSON_PREFIXES = ("human:", "operator:")
OTHER = "other"
_MAX_DECIMAL_CHARS = 128  # same bound as an identifier; keeps `format(..., "f")` small


def is_safe_string(value: object) -> bool:
    """Whether `value` is an identifier-shaped string that is no email, path or person."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        return False
    if _EMAIL.fullmatch(value) or value.startswith(_PERSON_PREFIXES):
        return False
    if _KEY_PREFIX.search(value) or _KEY_RUN.search(value):
        return False  # key-shaped: refused in every field, even an id (the value is never echoed)
    if "/" in value:
        if value.startswith(("/", ".")) or "//" in value or ".." in value:
            return False
        if value.count("/") != 1 or _EXTENSION.search(value):
            return False
    return True


def safe_decimal(value: object) -> str | None:
    """Plain decimal text (`format(Decimal(value), "f")`) or None when it is not a plain number.
    Never a float: a float or a boolean is refused."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    if isinstance(value, str) and len(value) > _MAX_DECIMAL_CHARS:
        return None
    try:
        number = Decimal(value)
        if not number.is_finite():
            return None
        # `format(Decimal("1e999999999999"), "f")` writes a trillion digits: bound the plain-text
        # length BEFORE formatting (the exponent is attacker-controlled ledger content).
        digits = len(number.as_tuple().digits)
        exponent = int(number.as_tuple().exponent)
        if max(digits + exponent, digits, -exponent) > _MAX_DECIMAL_CHARS:
            return None
        text = format(number, "f")
    except (InvalidOperation, ValueError):
        return None
    return text if _DECIMAL.fullmatch(text) else None


def closed(value: object, allowed: Collection[str]) -> str | None:
    """`value` when it is in the closed set, `other` when it is a string outside it, None when it
    is not a string (a type problem the caller counts)."""
    if not isinstance(value, str):
        return None
    return value if value in allowed else OTHER


def whole_number(value: object) -> int | None:
    """An integer that is not a boolean, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None
