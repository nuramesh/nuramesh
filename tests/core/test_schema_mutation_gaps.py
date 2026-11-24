"""Pins for the schema subset's limits and constants (farm survivors, Part of #1341).

Each test sits exactly on a limit (accepted at the limit, refused one past it) so a changed
constant or comparison is noticed. Fake data only.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.schema import (
    MAX_DEPTH,
    MAX_NODES,
    MAX_PROBLEMS,
    SchemaNode,
    count_nodes,
    depth,
    parse_schema,
    validate,
)


def test_limit_constants_have_their_documented_values() -> None:
    assert (MAX_DEPTH, MAX_NODES, MAX_PROBLEMS) == (32, 1_000, 20)


def test_a_leaf_is_one_node_and_one_level() -> None:
    leaf = parse_schema({"type": "string"})
    assert count_nodes(leaf) == 1
    assert depth(leaf) == 1
    two = parse_schema({"allOf": [{"type": "string"}, {"type": "null"}]})
    assert count_nodes(two) == 3
    assert depth(two) == 2


def test_depth_32_is_accepted_and_33_is_refused() -> None:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(MAX_DEPTH - 1):
        node = {"not": node}
    assert depth(parse_schema(node)) == 32
    with pytest.raises(ValueError, match="deeper than 32 levels"):
        parse_schema({"not": node})


def test_exactly_1000_nodes_are_accepted_and_1001_refused() -> None:
    parse_schema({"allOf": [{"type": "string"} for _ in range(999)]})
    with pytest.raises(ValueError, match="more than 1000 nodes"):
        parse_schema({"allOf": [{"type": "string"} for _ in range(1_000)]})


def test_exactly_20_problems_are_listed_without_a_tail_and_21_add_a_count() -> None:
    node = parse_schema({"items": {"type": "string"}})
    twenty = validate(node, [1] * 20)
    assert len(twenty) == 20
    assert not any("more problems" in p for p in twenty)
    twenty_one = validate(node, [1] * 21)
    assert len(twenty_one) == 21
    assert twenty_one[:20] == twenty
    assert twenty_one[-1] == "…and 1 more problems"
    assert validate(node, [1] * 25)[-1] == "…and 5 more problems"


@pytest.mark.parametrize("keyword", ["minItems", "maxItems", "minLength", "maxLength"])
def test_counts_accept_zero_and_one_million_but_nothing_outside(keyword: str) -> None:
    parse_schema({keyword: 0})
    parse_schema({keyword: 1_000_000})
    for bad in (-1, 1_000_001, True, 1.0):
        with pytest.raises(ValidationError):
            parse_schema({keyword: bad})


@pytest.mark.parametrize("keyword", ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"])
def test_bounds_accept_the_safe_integer_range_only(keyword: str) -> None:
    top = 2**53 - 1
    for good in (top, -top, 0):
        parse_schema({keyword: good})
    for bad in (top + 1, -top - 1, True, 1.5):
        with pytest.raises(ValidationError):
            parse_schema({keyword: bad})


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf"])
def test_a_combinator_needs_at_least_one_schema(keyword: str) -> None:
    with pytest.raises(ValidationError):
        parse_schema({keyword: []})
    parse_schema({keyword: [{"type": "string"}]})


def test_a_schema_node_is_frozen_and_takes_aliases_only() -> None:
    node = parse_schema({"minItems": 1})
    with pytest.raises(ValidationError):
        node.title = "changed"
    with pytest.raises(ValidationError):
        SchemaNode.model_validate({"min_items": 1})
    assert node.min_items == 1


def _unique_items_check(last_text_length: int) -> list[str]:
    """One million distinct strings: 999,999 whose canonical text is 64 bytes, then one other."""
    node = parse_schema({"uniqueItems": True, "items": {}})
    value = [str(i).zfill(62) for i in range(999_999)]
    value.append("" if last_text_length == 2 else "x" * (last_text_length - 2))
    return validate(node, value)


def test_work_limit_is_two_million_steps_at_64_bytes_per_step() -> None:
    # Cost = 1 (root) + 1 per item + (sum of canonical text lengths // 64) for uniqueItems.
    # Last text 2 bytes: sum = 63_999_938, bytes cost 999_999, total exactly 2_000_000: allowed.
    assert _unique_items_check(2) == []
    # Last text 66 bytes: sum = 64_000_002, bytes cost 1_000_000, total 2_000_001: refused.
    with pytest.raises(ValueError, match="too much work"):
        _unique_items_check(66)
