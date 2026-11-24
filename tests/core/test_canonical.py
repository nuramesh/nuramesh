from decimal import Decimal

import pytest

from nuramesh.core.canonical import canonical_json, content_hash


def test_keys_sorted_no_whitespace_utf8() -> None:
    assert (
        canonical_json({"b": 1, "a": ["é", None, True]}) == '{"a":["é",null,true],"b":1}'.encode()
    )


def test_decimal_is_a_plain_string_keeping_trailing_zeros() -> None:
    assert canonical_json({"x": Decimal("1.50")}) == b'{"x":"1.50"}'


def test_same_content_same_hash() -> None:
    assert content_hash({"a": 1, "b": [1, 2]}) == content_hash({"b": [1, 2], "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


@pytest.mark.parametrize(
    "bad", [1.5, {"x": 0.1}, [float("nan")], {1: "int key"}, b"bytes", object()]
)
def test_rejects_non_canonical_values(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json(bad)  # type: ignore[arg-type]


def test_rejects_non_finite_decimal() -> None:
    with pytest.raises(ValueError):
        canonical_json(Decimal("Infinity"))


def test_keys_are_sorted_by_utf16_code_units_like_javascript() -> None:
    # U+1F600 is a surrogate pair (D83D DE00) in UTF-16, so it sorts before U+FF21 in JavaScript,
    # although its code point is larger.
    assert canonical_json({"\uff21": 2, "\U0001f600": 1}) == '{"\U0001f600":1,"\uff21":2}'.encode()


def test_integers_must_be_javascript_safe() -> None:
    assert canonical_json(2**53 - 1) == b"9007199254740991"
    with pytest.raises(ValueError):
        canonical_json(2**53)
    with pytest.raises(ValueError):
        canonical_json({"n": -(2**53)})


def test_bool_is_not_treated_as_int() -> None:
    assert canonical_json([True, 1]) == b"[true,1]"


def test_equal_decimals_with_different_text_hash_differently() -> None:
    # Intended: the stored text is what gets hashed.
    assert content_hash(Decimal("1.0")) != content_hash(Decimal("1"))


@pytest.mark.parametrize(
    ("value", "text"),
    [(Decimal("1E-7"), '"0.0000001"'), (Decimal("1E+3"), '"1000"'), (Decimal("-0"), '"0"')],
)
def test_decimals_use_plain_notation(value: Decimal, text: str) -> None:
    assert canonical_json(value) == text.encode()


def test_escaping_matches_javascript() -> None:
    # JSON.stringify("\b\f\n\r\t\u0001\u007f\u2028\"\\é") gives exactly this:
    out = canonical_json('\b\f\n\r\t\u0001\u007f\u2028"\\é').decode()
    assert out == '"\\b\\f\\n\\r\\t\\u0001\u007f\u2028\\"\\\\é"'


def test_lone_surrogates_are_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_json({"k": "\udc00"})
    with pytest.raises(ValueError):
        canonical_json({"\ud800": 1})
