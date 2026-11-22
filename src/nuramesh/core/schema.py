"""A JSON Schema subset, modelled with pydantic, for schema gates (PLAN D.2).

Decision ND20 (owner): no JSON Schema library; core stays on stdlib + pydantic (A8). The supported
keywords are the ones a gate needs to describe an agent's structured output. **Anything else is
refused when the gate is configured**, so a schema can never silently check less than it says.

Supported keywords (draft 2020-12 meaning):

- `type`: one name or a list of `null`, `boolean`, `integer`, `number`, `string`, `array`, `object`.
  Booleans are never integers; `number` accepts integers and decimals. The value checked is the
  output as written by RFC 8785 and read back (ND78): `2.0` is written `2` and is the integer `2`;
  a fractional value, and a whole float of 2**53 or more in size (`1e20`, written in full, or
  `1e21`, written `1e+21`), reads back as a float, which is a `number` but not an `integer`.
- `enum`, `const`: equal by canonical JSON (so `1` and `true` differ).
- Objects: `properties`, `required`, `additionalProperties` (`true`, `false` or a schema).
- Arrays: `items` (one schema for every item), `minItems`, `maxItems`, `uniqueItems`.
- Strings: `minLength`, `maxLength` (characters), `pattern` (regular expression, searched, not
  anchored: the JSON Schema meaning).
- Numbers: `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` (integers).
- Combinators: `allOf`, `anyOf`, `oneOf`, `not`.
- Annotations ignored when checking: `title`, `description`.

Not supported (refused): `$ref`, `$defs`, `format`, `patternProperties`, `prefixItems`,
`dependentRequired`, `if/then/else`, `multipleOf`, floating-point bounds, and any unknown keyword.

Problems never quote anything from the checked value (it may be personal data, S2): not its
text, not its numbers, not the names of properties the schema didn't declare (shown as `*`). They
name the path through *declared* properties and array positions, the keyword, and limits taken
from the schema itself.

`pattern` uses Python `re` semantics, not ECMA-262 (the JSON Schema default): `$` also matches
before a final newline and `\\d` matches any Unicode digit. Patterns come from the organization's
own configuration (trusted, at most 1,000 characters); checked values are untrusted, so the work
done on them is bounded (`MAX_STEPS`). Python's `re` has no timeout, so a badly written pattern
(nested quantifiers) can still be slow: review patterns like code.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from nuramesh.core.canonical import canonical_json, canonical_json_jcs

MAX_PATTERN = 1_000
MAX_DEPTH = 32
MAX_PROBLEMS = 20
MAX_NODES = 1_000
_BYTES_PER_STEP = 64  # serialising 64 bytes costs about as much as visiting one node
MAX_STEPS = 2_000_000  # schema-node visits per check: bounds work on large outputs

JsonType = Literal["null", "boolean", "integer", "number", "string", "array", "object"]
# Strict: `true` and `1.0` are not counts or bounds.
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Bound = Annotated[int, Field(strict=True, ge=-(2**53 - 1), le=2**53 - 1)]


class SchemaNode(BaseModel):
    """One schema object. Unknown keywords are an error, not ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=False)

    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=5_000)
    type: JsonType | tuple[JsonType, ...] | None = None
    enum: tuple[Any, ...] | None = Field(default=None, max_length=1_000)
    const: Any = None
    properties: Mapping[str, "SchemaNode"] | None = None
    required: tuple[str, ...] | None = None
    additional_properties: "StrictBool | SchemaNode | None" = Field(
        default=None, alias="additionalProperties"
    )
    items: "SchemaNode | None" = None
    min_items: Count | None = Field(default=None, alias="minItems")
    max_items: Count | None = Field(default=None, alias="maxItems")
    unique_items: StrictBool | None = Field(default=None, alias="uniqueItems")
    min_length: Count | None = Field(default=None, alias="minLength")
    max_length: Count | None = Field(default=None, alias="maxLength")
    pattern: Annotated[str, StringConstraints(max_length=MAX_PATTERN)] | None = None
    minimum: Bound | None = None
    maximum: Bound | None = None
    exclusive_minimum: Bound | None = Field(default=None, alias="exclusiveMinimum")
    exclusive_maximum: Bound | None = Field(default=None, alias="exclusiveMaximum")
    all_of: tuple["SchemaNode", ...] | None = Field(default=None, alias="allOf", min_length=1)
    any_of: tuple["SchemaNode", ...] | None = Field(default=None, alias="anyOf", min_length=1)
    one_of: tuple["SchemaNode", ...] | None = Field(default=None, alias="oneOf", min_length=1)
    not_: "SchemaNode | None" = Field(default=None, alias="not")

    @field_validator("type")
    @classmethod
    def _unique_types(
        cls, value: JsonType | tuple[JsonType, ...] | None
    ) -> JsonType | tuple[JsonType, ...] | None:
        if isinstance(value, tuple) and (not value or len(set(value)) != len(value)):
            raise ValueError("type must list each name once")
        return value

    @field_validator("enum", "const")
    @classmethod
    def _canonical(cls, value: Any) -> Any:
        try:
            canonical_json(value)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("required")
    @classmethod
    def _unique_required(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("required lists a property twice")
        return value

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for low, high, name in (
            (self.min_items, self.max_items, "minItems/maxItems"),
            (self.min_length, self.max_length, "minLength/maxLength"),
            (self.minimum, self.maximum, "minimum/maximum"),
        ):
            if low is not None and high is not None and low > high:
                raise ValueError(f"{name}: the minimum is above the maximum")
        return self

    @property
    def has_const(self) -> bool:
        """`const` may be `null`, so presence is tracked, not value."""
        return "const" in self.model_fields_set


SchemaNode.model_rebuild()


def parse_schema(raw: Mapping[str, Any]) -> SchemaNode:
    """Validate `raw` (a plain mapping) as a schema in the supported subset and check its depth."""
    node = SchemaNode.model_validate(raw)
    if depth(node) > MAX_DEPTH:
        raise ValueError(f"schema nests deeper than {MAX_DEPTH} levels")
    if count_nodes(node) > MAX_NODES:
        raise ValueError(f"schema has more than {MAX_NODES} nodes")
    return node


def _children(node: SchemaNode) -> list[SchemaNode]:
    kids: list[SchemaNode] = list((node.properties or {}).values())
    if isinstance(node.additional_properties, SchemaNode):
        kids.append(node.additional_properties)
    for single in (node.items, node.not_):
        if single is not None:
            kids.append(single)
    for many in (node.all_of, node.any_of, node.one_of):
        kids.extend(many or ())
    return kids


def count_nodes(node: SchemaNode) -> int:
    """How many schema objects `node` contains, itself included."""
    return 1 + sum(count_nodes(k) for k in _children(node))


def depth(node: SchemaNode) -> int:
    """How many schema levels deep `node` goes (a leaf is 1)."""
    return 1 + max((depth(k) for k in _children(node)), default=0)


def json_type(value: Any) -> str:
    """The JSON type name of a Python value (`bool` before `int`: a boolean is not an integer)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"  # fractional, or whole and 2**53 or more in size (ND78: 2.0 is 2)
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return "array"
    return type(value).__name__


def _is_type(value: Any, name: str) -> bool:
    actual = json_type(value)
    return actual == name or (name == "number" and actual == "integer")


class _Collector:
    """Gathers problems up to a cap, then only counts the rest; shares a work counter."""

    def __init__(self, steps: list[int] | None = None) -> None:
        self.problems: list[str] = []
        self.extra = 0
        self.steps = steps if steps is not None else [0]

    def add(self, path: str, message: str) -> None:
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(f"{path}: {message}")
        else:
            self.extra += 1


def _charge(out: _Collector, steps: int) -> None:
    out.steps[0] += steps
    if out.steps[0] > MAX_STEPS:
        raise ValueError("the schema check needs too much work for this output")


def _check(node: SchemaNode, value: Any, path: str, out: _Collector) -> None:
    _charge(out, 1)
    actual = json_type(value)
    if node.type is not None:
        names = (node.type,) if isinstance(node.type, str) else node.type
        if not any(_is_type(value, n) for n in names):
            out.add(path, f"expected {' or '.join(names)}, got {actual}")
            return  # keywords below assume the right type
    if node.enum is not None or node.has_const:
        text = canonical_json_jcs(value)  # once per node, however many values it is compared to
        _charge(out, len(text) // _BYTES_PER_STEP)
        if node.enum is not None and not any(canonical_json_jcs(e) == text for e in node.enum):
            out.add(path, f"not one of the {len(node.enum)} allowed values")
        if node.has_const and canonical_json_jcs(node.const) != text:
            out.add(path, "not the required constant value")
    if isinstance(value, str):
        _check_string(node, value, path, out)
    elif actual in ("integer", "number"):
        _check_number(node, value, path, out)
    elif actual == "array":
        _check_array(node, value, path, out)
    elif actual == "object":
        _check_object(node, value, path, out)
    _check_combinators(node, value, path, out)


def _check_string(node: SchemaNode, value: str, path: str, out: _Collector) -> None:
    if node.min_length is not None and len(value) < node.min_length:
        out.add(path, f"fewer than {node.min_length} characters")
    if node.max_length is not None and len(value) > node.max_length:
        out.add(path, f"more than {node.max_length} characters")
    if node.pattern is not None and re.search(node.pattern, value) is None:
        out.add(path, "doesn't match the required pattern")


def _check_number(node: SchemaNode, value: float, path: str, out: _Collector) -> None:
    if node.minimum is not None and value < node.minimum:
        out.add(path, f"below the minimum {node.minimum}")
    if node.maximum is not None and value > node.maximum:
        out.add(path, f"above the maximum {node.maximum}")
    if node.exclusive_minimum is not None and value <= node.exclusive_minimum:
        out.add(path, f"not above {node.exclusive_minimum}")
    if node.exclusive_maximum is not None and value >= node.exclusive_maximum:
        out.add(path, f"not below {node.exclusive_maximum}")


def _check_array(node: SchemaNode, value: Sequence[Any], path: str, out: _Collector) -> None:
    if node.min_items is not None and len(value) < node.min_items:
        out.add(path, f"fewer than {node.min_items} items")
    if node.max_items is not None and len(value) > node.max_items:
        out.add(path, f"more than {node.max_items} items")
    if node.unique_items:
        seen = [canonical_json_jcs(v) for v in value]
        _charge(out, sum(len(t) for t in seen) // _BYTES_PER_STEP)
        if len(set(seen)) != len(seen):
            out.add(path, "items are not unique")
    if node.items is not None:
        for index, item in enumerate(value):
            _check(node.items, item, f"{path}[{index}]", out)


def _check_object(node: SchemaNode, value: Mapping[str, Any], path: str, out: _Collector) -> None:
    for name in node.required or ():
        if name not in value:
            out.add(path, f"missing required property '{name}'")
    props = node.properties or {}
    for name, sub in props.items():
        if name in value:
            _check(sub, value[name], f"{path}.{name}", out)
    extra = sorted(k for k in value if k not in props)
    if node.additional_properties is False and extra:
        out.add(path, f"{len(extra)} unexpected {'property' if len(extra) == 1 else 'properties'}")
    elif isinstance(node.additional_properties, SchemaNode):
        for name in extra:
            _check(node.additional_properties, value[name], f"{path}.*", out)


def _valid(node: SchemaNode, value: Any, out: _Collector) -> bool:
    scratch = _Collector(out.steps)
    _check(node, value, "$", scratch)
    return not scratch.problems and scratch.extra == 0


def _check_combinators(node: SchemaNode, value: Any, path: str, out: _Collector) -> None:
    for sub in node.all_of or ():
        _check(sub, value, path, out)
    if node.any_of is not None and not any(_valid(s, value, out) for s in node.any_of):
        out.add(path, f"matches none of the {len(node.any_of)} alternatives (anyOf)")
    if node.one_of is not None:
        matches = sum(_valid(s, value, out) for s in node.one_of)
        if matches != 1:
            out.add(
                path, f"matches {matches} of the {len(node.one_of)} alternatives, not exactly one"
            )
    if node.not_ is not None and _valid(node.not_, value, out):
        out.add(path, "matches a schema it must not match (not)")


def validate(node: SchemaNode, value: Any) -> list[str]:
    """Problems with `value` against `node` (empty means valid), at most 20 plus a count.

    `value` must be canonical-JSON-compatible (string keys; a float is allowed, ND78). Each
    problem starts with the path (`$` is the root, then `.name` and `[index]`; `.*` stands for a
    property the schema didn't declare). Raises `ValueError` if the check would take more than
    `MAX_STEPS` steps.
    """
    out = _Collector()
    _check(node, value, "$", out)
    problems = list(out.problems)
    if out.extra:
        problems.append(f"…and {out.extra} more problems")
    return problems
