"""Rule checks (PLAN D.2): each returns met / not met with a detail that can be evidence."""

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from nuramesh.core.rules import (
    MAX_TEXT,
    MISSING,
    RuleCheck,
    RuleGateConfig,
    evaluate,
    output_text,
    resolve_path,
)

adapter: TypeAdapter[RuleCheck] = TypeAdapter(RuleCheck)


def run(check: dict[str, Any], output: Any) -> tuple[bool, str]:
    return evaluate(adapter.validate_python(check), output)


def test_contains_and_not_contains() -> None:
    assert run({"type": "contains", "value": "ok"}, "all ok here")[0]
    assert not run({"type": "contains", "value": "OK"}, "all ok here")[0]
    assert run({"type": "contains", "value": "OK", "case_sensitive": False}, "all ok here")[0]
    assert run({"type": "not_contains", "value": "TODO"}, "done")[0]
    assert not run({"type": "not_contains", "value": "TODO"}, "TODO: later")[0]
    assert not run({"type": "not_contains", "value": "todo", "case_sensitive": False}, "TODO")[0]


def test_structured_output_is_searched_as_canonical_json() -> None:
    assert run({"type": "contains", "value": '"status":"ok"'}, {"status": "ok"})[0]


def test_matches() -> None:
    met, detail = run({"type": "matches", "pattern": r"v\d+\.\d+"}, "release v1.2 out")
    assert met and "position 8" in detail and "v1.2" not in detail  # the output isn't quoted
    met, detail = run({"type": "matches", "pattern": r"^\d+$"}, "abc")
    assert not met and "didn't match" in detail


def test_length_between() -> None:
    assert run({"type": "length_between", "min": 2, "max": 4}, "abc")[0]
    assert run({"type": "length_between", "min": 2, "max": 4}, "ab")[0]
    assert not run({"type": "length_between", "min": 2, "max": 4}, "a")[0]
    assert not run({"type": "length_between", "min": 2, "max": 4}, "abcde")[0]
    assert run({"type": "length_between", "min": 2}, "x" * 1000)[0]


def test_paths() -> None:
    output = {"files": [{"name": "a.py"}, {"name": "b.py"}], "ok": True, "n": 0, "none": None}
    assert run({"type": "path_exists", "path": "files[1].name"}, output)[0]
    assert run({"type": "path_exists", "path": "none"}, output)[0]  # present, though null
    assert not run({"type": "path_exists", "path": "files[2]"}, output)[0]
    assert not run({"type": "path_exists", "path": "nope.deeper"}, output)[0]
    assert run({"type": "path_equals", "path": "files[0].name", "value": "a.py"}, output)[0]
    assert run({"type": "path_equals", "path": "ok", "value": True}, output)[0]
    assert not run({"type": "path_equals", "path": "n", "value": False}, output)[0]  # 0 != false
    assert not run({"type": "path_equals", "path": "files[0].name", "value": "b.py"}, output)[0]
    assert not run({"type": "path_equals", "path": "missing", "value": 1}, output)[0]


def test_resolve_path_edges() -> None:
    assert resolve_path({"a": "text"}, "a[0]") is MISSING  # a string is not an array
    assert resolve_path({"a": [1]}, "a.b") is MISSING
    assert resolve_path([1, 2], "x") is MISSING
    assert resolve_path({"a": {"b": [5]}}, "a.b[0]") == 5


def test_details_never_quote_the_output() -> None:
    _, detail = run(
        {"type": "path_equals", "path": "ssn", "value": "000-00-0000"}, {"ssn": "123-45-6789"}
    )
    assert "123-45-6789" not in detail and "string" in detail
    _, detail = run({"type": "matches", "pattern": r"\\d{3}-\\d{2}"}, "id 123-45-6789")
    assert "123" not in detail


@pytest.mark.parametrize(
    "check",
    [
        {"type": "contains", "value": ""},
        {"type": "matches", "pattern": "("},
        {"type": "matches", "pattern": ""},
        {"type": "path_exists", "path": "a..b"},
        {"type": "path_exists", "path": "[0]"},
        {"type": "path_exists", "path": "a[-1]"},
        {"type": "path_equals", "path": "a", "value": 1.5},
        {"type": "length_between", "min": -1},
        {"type": "contains", "value": "x", "extra": 1},
        {"type": "unknown"},
    ],
)
def test_bad_checks_are_refused(check: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python(check)


def test_gate_config_needs_a_rule_and_names_a_criterion() -> None:
    with pytest.raises(ValidationError):
        RuleGateConfig.model_validate({"rules": []})
    with pytest.raises(ValidationError):
        RuleGateConfig.model_validate(
            {"rules": [{"criterion": "Bad Id", "check": {"type": "contains", "value": "x"}}]}
        )


def test_oversized_or_non_canonical_output_is_refused() -> None:
    with pytest.raises(ValueError, match="over"):
        output_text("x" * (MAX_TEXT + 1))
    with pytest.raises(ValueError, match=r"\$\.price"):
        output_text({"price": float("nan")})
    assert output_text({"price": 1.5}) == '{"price":1.5}'  # a decimal is fine (ND78)


def test_values_that_cannot_be_canonicalised_are_never_equal() -> None:
    from nuramesh.core.rules import _same

    assert _same({"a": 1}, {"a": 1})
    assert not _same(object(), object())
