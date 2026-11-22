"""The stable-prefix check (R1.34 Part C, spec/token-savings 5.1): pure, no model call, stdlib only.

A message renders as `<role>: <content>`; a call renders as its messages joined by a blank line,
as UTF-8. The leading system run is the longest run of `system` messages at the start; its last
index is the breakpoint. `check_prefix(a, b)` says how much of two calls is common and whether a
block-level provider cache set at `a`'s breakpoint would still hit on `b`. Reports only counts and
a flag: no text.
"""

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict

from nuramesh.core.models import Message

SEPARATOR = b"\n\n"


class PrefixCheck(BaseModel):
    """What two calls share. `breakpoint_a`, `stable_prefix_bytes_a` and `stable_prefix_intact`
    are None when `a` has no leading system message."""

    model_config = ConfigDict(frozen=True)

    bytes_a: int
    bytes_b: int
    common_prefix_bytes: int
    reusable_share: Decimal
    breakpoint_a: int | None
    stable_prefix_bytes_a: int | None
    stable_prefix_intact: bool | None


def render_message(message: Message) -> bytes:
    """One message as `<role>: <content>` in UTF-8."""
    return f"{message.role}: {message.content}".encode()


def render_call(messages: Sequence[Message]) -> bytes:
    """The whole call: the rendered messages joined by a blank line."""
    return SEPARATOR.join(render_message(m) for m in messages)


def breakpoint_of(messages: Sequence[Message]) -> int | None:
    """The index of the last message of the leading system run, or None."""
    last: int | None = None
    for i, m in enumerate(messages):
        if m.role != "system":
            break
        last = i
    return last


def _round6(numerator: int, denominator: int) -> Decimal:
    """`numerator / denominator` rounded once to 6 places, half away from zero."""
    if denominator == 0:
        return Decimal(0)
    value = Decimal(numerator) / Decimal(denominator)
    return value.copy_abs().quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP).copy_sign(value)


def check_prefix(a: Sequence[Message], b: Sequence[Message]) -> PrefixCheck:
    """Compare two calls (section 5.1). Never raises on content."""
    ra, rb = render_call(a), render_call(b)
    common = 0
    for x, y in zip(ra, rb, strict=False):
        if x != y:
            break
        common += 1
    point = breakpoint_of(a)
    stable: int | None = None
    intact: bool | None = None
    if point is not None:
        stable = len(render_call(a[: point + 1]))
        at_boundary = len(rb) == stable or rb[stable : stable + 2] == SEPARATOR
        intact = common >= stable and at_boundary and len(rb) >= stable
    return PrefixCheck(
        bytes_a=len(ra),
        bytes_b=len(rb),
        common_prefix_bytes=common,
        reusable_share=_round6(common, len(rb)),
        breakpoint_a=point,
        stable_prefix_bytes_a=stable,
        stable_prefix_intact=intact,
    )
