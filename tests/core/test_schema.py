"""The JSON Schema subset (ND20): what it accepts, what it refuses, and how it judges values."""

from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.canonical import canonical_value
from nuramesh.core.schema import MAX_DEPTH, MAX_PROBLEMS, json_type, parse_schema, validate


def check(schema: dict[str, Any], value: Any) -> list[str]:
    return validate(parse_schema(schema), canonical_value(value))


def test_types_and_the_boolean_is_not_an_integer_trap() -> None:
    assert check({"type": "integer"}, 3) == []
    assert check({"type": "integer"}, True) == ["$: expected integer, got boolean"]
    assert check({"type": "number"}, 3) == []
    assert check({"type": "number"}, True) == ["$: expected number, got boolean"]
    assert check({"type": ["string", "null"]}, None) == []
    assert check({"type": ["string", "null"]}, 1) == ["$: expected string or null, got integer"]
    assert check({"type": "object"}, []) == ["$: expected object, got array"]
    assert check({"type": "array"}, {}) == ["$: expected array, got object"]
    assert json_type(None) == "null"
    assert json_type(b"x") == "bytes"


def test_empty_enum_and_empty_pattern_are_valid_json_schema() -> None:
    assert check({"enum": []}, 1) == ["$: not one of the 0 allowed values"]
    assert check({"pattern": ""}, "abc") == []


def test_enum_and_const_compare_by_canonical_json() -> None:
    assert check({"enum": [1, "a", None]}, None) == []
    assert check({"enum": [1, "a"]}, True) == ["$: not one of the 2 allowed values"]
    assert check({"enum": [1]}, "1") != []
    assert check({"const": {"a": [1, 2]}}, {"a": [1, 2]}) == []
    assert check({"const": None}, None) == []
    assert check({"const": None}, 0) == ["$: not the required constant value"]
    assert check({"const": 0}, False) != []


def test_strings() -> None:
    schema = {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^a"}
    assert check(schema, "abc") == []
    assert check(schema, "a") == ["$: fewer than 2 characters"]
    assert check(schema, "abcde") == ["$: more than 4 characters"]
    assert check(schema, "bcd") == ["$: doesn't match the required pattern"]
    assert check({"pattern": "b"}, "abc") == []  # searched, not anchored
    assert check({"maxLength": 1}, "é") == []  # characters, not bytes


def test_numbers() -> None:
    schema = {"minimum": 1, "maximum": 3, "exclusiveMinimum": 0, "exclusiveMaximum": 4}
    assert check(schema, 2) == []
    assert check({"minimum": 1}, 0) == ["$: below the minimum 1"]
    assert check({"maximum": 1}, 2) == ["$: above the maximum 1"]
    assert check({"exclusiveMinimum": 1}, 1) == ["$: not above 1"]
    assert check({"exclusiveMaximum": 1}, 1) == ["$: not below 1"]
    assert check({"minimum": 1}, "x") == []  # keywords for other types don't apply


def test_arrays() -> None:
    schema = {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 2}
    assert check(schema, [1, 2]) == []
    assert check(schema, []) == ["$: fewer than 1 items"]
    assert check(schema, [1, 2, 3]) == ["$: more than 2 items"]
    assert check(schema, [1, "a"]) == ["$[1]: expected integer, got string"]
    assert check({"uniqueItems": True}, [1, 2, 1]) == ["$: items are not unique"]
    assert check({"uniqueItems": True}, [1, True]) == []  # 1 and true are different
    assert check({"uniqueItems": True}, [{"a": 1}, {"a": 1}]) != []


def test_objects() -> None:
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }
    assert check(schema, {"id": 1, "tags": ["a"]}) == []
    assert check(schema, {}) == ["$: missing required property 'id'"]
    assert check(schema, {"id": 1, "x": 1}) == ["$: 1 unexpected property"]
    assert check(schema, {"id": 1, "tags": ["a", 2]}) == ["$.tags[1]: expected string, got integer"]
    typed = {"additionalProperties": {"type": "integer"}, "properties": {"a": {"type": "string"}}}
    assert check(typed, {"a": "x", "b": 2}) == []
    assert check(typed, {"a": "x", "b": "y"}) == ["$.*: expected integer, got string"]


def test_combinators() -> None:
    assert check({"allOf": [{"type": "integer"}, {"minimum": 2}]}, 1) == ["$: below the minimum 2"]
    assert check({"anyOf": [{"type": "integer"}, {"type": "string"}]}, 1) == []
    assert check({"anyOf": [{"type": "integer"}, {"type": "string"}]}, None) == [
        "$: matches none of the 2 alternatives (anyOf)"
    ]
    one = {"oneOf": [{"minimum": 1}, {"maximum": 5}]}
    assert check(one, 3) == ["$: matches 2 of the 2 alternatives, not exactly one"]
    assert check(one, 9) == []
    assert check({"not": {"type": "string"}}, "x") == [
        "$: matches a schema it must not match (not)"
    ]
    assert check({"not": {"type": "string"}}, 1) == []


def test_problems_are_capped_and_never_quote_values() -> None:
    problems = check({"items": {"type": "string"}}, list(range(MAX_PROBLEMS + 5)))
    assert len(problems) == MAX_PROBLEMS + 1
    assert problems[-1] == "…and 5 more problems"
    secret = "jane.doe@example.com"
    for schema in (
        {"maxLength": 3},
        {"enum": ["x"]},
        {"pattern": "^zzz"},
        {"const": "x"},
        {"type": "integer"},
    ):
        assert secret not in " ".join(check(schema, secret))


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/defs/x"},
        {"format": "email"},
        {"multipleOf": 2},
        {"patternProperties": {}},
        {"prefixItems": []},
        {"if": {}},
        {"type": "float"},
        {"type": []},
        {"type": ["string", "string"]},
        {"required": ["a", "a"]},
        {"minimum": 1.5},
        {"minimum": 5, "maximum": 1},
        {"minItems": 3, "maxItems": 1},
        {"minLength": -1},
        {"pattern": "("},
        {"pattern": "x" * 1001},
        {"allOf": []},
        {"properties": {"a": {"bogus": 1}}},
        {"const": 1.5},
        {"minItems": True},
        {"minimum": 1.0},
        {"uniqueItems": 1},
    ],
)
def test_unsupported_or_invalid_schemas_are_refused(schema: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_schema(schema)


def test_depth_is_limited() -> None:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(MAX_DEPTH - 1):
        node = {"items": node}
    parse_schema(node)
    with pytest.raises(ValueError, match="deeper"):
        parse_schema({"items": node})


def test_annotations_are_accepted_and_ignored() -> None:
    assert check({"title": "t", "description": "d", "type": "string"}, "x") == []


def test_work_on_one_output_is_bounded() -> None:
    from nuramesh.core import schema as module

    wide = {"allOf": [{"items": {"type": "string"}} for _ in range(50)]}
    node = parse_schema(wide)
    assert validate(node, ["x"] * 100) == []
    old = module.MAX_STEPS
    module.MAX_STEPS = 500
    try:
        with pytest.raises(ValueError, match="too much work"):
            validate(node, ["x"] * 100)
    finally:
        module.MAX_STEPS = old


def test_schema_size_is_limited() -> None:
    many = {"allOf": [{"type": "string"} for _ in range(1_000)]}
    with pytest.raises(ValueError, match="nodes"):
        parse_schema(many)
