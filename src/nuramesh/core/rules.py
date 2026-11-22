"""Rule checks: deterministic tests on a task's output, used by rule gates (PLAN D.2).

Each check returns met / not met with a detail saying what was looked at and what was found, so
it can be evidence (D.4). Text checks look at the output if it is text, or at its canonical JSON
otherwise. Path checks need structured output; a path is dotted keys and `[index]` steps, for
example `files[0].name`.

Patterns come from the organization's own configuration (trusted), are limited in length, and run
on outputs limited in size. Details never quote the output (it may be personal data, S2); they say
what was looked for and where or how it differed. `matches` uses Python `re` semantics.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from nuramesh.core.canonical import canonical_json, canonical_json_jcs
from nuramesh.core.schema import json_type

MAX_PATTERN = 1_000
MAX_TEXT = 5_000_000

_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*|\[\d+\])*$")
_STEP = re.compile(r"\.?([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)\]")

JsonPath = Annotated[str, StringConstraints(pattern=_PATH.pattern, max_length=500)]


class _Check(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Contains(_Check):
    """The text contains `value`."""

    type: Literal["contains"]
    value: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    case_sensitive: bool = True


class NotContains(_Check):
    """The text doesn't contain `value`."""

    type: Literal["not_contains"]
    value: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    case_sensitive: bool = True


class Matches(_Check):
    """The text matches the regular expression `pattern` somewhere."""

    type: Literal["matches"]
    pattern: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATTERN)]

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return value


class LengthBetween(_Check):
    """The text has between `min` and `max` characters (inclusive)."""

    type: Literal["length_between"]
    min: int = Field(default=0, ge=0)
    max: int | None = Field(default=None, ge=0)


class PathExists(_Check):
    """Structured output has a value at `path`."""

    type: Literal["path_exists"]
    path: JsonPath


class PathEquals(_Check):
    """Structured output has exactly `value` at `path`."""

    type: Literal["path_equals"]
    path: JsonPath
    value: Any

    @field_validator("value")
    @classmethod
    def _canonical(cls, value: Any) -> Any:
        try:
            canonical_json(value)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        return value


RuleCheck = Annotated[
    Contains | NotContains | Matches | LengthBetween | PathExists | PathEquals,
    Field(discriminator="type"),
]


class Rule(_Check):
    """One check and the criterion it is evidence for."""

    criterion: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]
    check: RuleCheck


class RuleGateConfig(_Check):
    """A rule gate's config: at least one rule."""

    rules: tuple[Rule, ...] = Field(min_length=1)


def output_text(output: Any) -> str:
    """The text text-checks look at: the output itself, or its canonical JSON."""
    text = output if isinstance(output, str) else canonical_json_jcs(output).decode("utf-8")
    if len(text) > MAX_TEXT:
        raise ValueError(f"output is over {MAX_TEXT} characters")
    return text


MISSING = object()  # what `resolve_path` returns for "nothing there"


def resolve_path(output: Any, path: str) -> Any:
    """The value at `path`, or a sentinel meaning "not there"."""
    current = output
    for key, index in _STEP.findall(path):
        if key:
            if not isinstance(current, Mapping) or key not in current:
                return MISSING
            current = current[key]
        else:
            position = int(index)
            if (
                isinstance(current, str)
                or not isinstance(current, Sequence)
                or position >= len(current)
            ):
                return MISSING
            current = current[position]
    return current


def _same(a: Any, b: Any) -> bool:
    try:
        return canonical_json_jcs(a) == canonical_json_jcs(b)
    except (TypeError, ValueError):
        return False


def _short(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evaluate(check: RuleCheck, output: Any) -> tuple[bool, str]:
    """(met, detail) for one check on one output."""
    if isinstance(check, Contains | NotContains):
        text = output_text(output)
        haystack, needle = (
            (text, check.value)
            if check.case_sensitive
            else (
                text.casefold(),
                check.value.casefold(),
            )
        )
        found = needle in haystack
        met = found if isinstance(check, Contains) else not found
        where = "found" if found else "not found"
        return met, f"'{_short(check.value)}' {where} in the output ({len(text)} characters)"
    if isinstance(check, Matches):
        text = output_text(output)
        match = re.search(check.pattern, text)
        if match:
            return True, f"/{_short(check.pattern)}/ matched at position {match.start()}"
        return False, f"/{_short(check.pattern)}/ didn't match the output"
    if isinstance(check, LengthBetween):
        length = len(output_text(output))
        within = length >= check.min and (check.max is None or length <= check.max)
        upper = "∞" if check.max is None else str(check.max)
        return within, f"{length} characters (allowed {check.min}–{upper})"
    value = resolve_path(output, check.path)
    if isinstance(check, PathExists):
        if value is MISSING:
            return False, f"nothing at {check.path}"
        return True, f"{check.path} is present"
    if value is MISSING:
        return False, f"nothing at {check.path}"
    if _same(value, check.value):
        return True, f"{check.path} equals the expected value"
    return False, f"{check.path} is a {json_type(value)}, not the expected value"
