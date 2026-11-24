"""The two-step split of a Merge Gate review (spec M-6, SV-1 to SV-3,
SV-11; threat T-07; cases S-01 to S-04, S-07 to S-12).

Two principals, one direction of data:

1. The **reviewer step** (`Reviewer`) is keyless. It is handed a `ReviewInput`, which can hold
   nothing but plain data (the SV-2 input folder: task, definition of done, diff, canon
   excerpts, bundle summary, plus PR and sha to echo on line 1), and returns a `ReviewOutput`:
   the proposal text and what the model call used. It never sees the signer, a backend, a
   store, the ledger or the request's requester, and the PR body is not a field.
2. The **signer step** (`run_review` driving `core.vm_signer.Signer`) owns everything else: it
   is given only (repo, PR, head) and who asks; its own side (`SignerSide`) fetches the diff and
   establishes the facts it will sign (SV-3, F-2); it checks the budget before the model call
   and ledgers the cost after it (SV-2); it reads the reviewer's text **as data**
   (`parse_proposal`); it re-reads the live head after the review; only then does it sign.

Order (each stop is a 7.2 refusal, nothing signed): org (`org_mismatch`, before anything is
fetched) -> the signer's own fetch (`diff_too_large`, `diff_binary`, `diff_header_mismatch`,
`head_moved`) -> `Signer.admit` (requester, stored decision `decision_exists`, limits; writes
`merge.review_requested`) -> budget (`budget_exceeded`, no model call) -> the reviewer step
(any failure, a timeout, an output of the wrong shape, another model than the signer
configured, a text that is too large or not UTF-8: `model_failed`) -> `merge.review_cost` ->
live head and resets re-read -> `Signer.issue` (`malformed_proposal`, `head_moved`, or the
signed verdict).

A failure of the signer's own side is not a refusal: it is `SignerFailure("side_unavailable")`,
loud, with nothing signed. The OS-level half of the split (separate users, no key file readable
by the reviewer, network fence) is the runner's and `merge doctor`'s; this module is the
interface both halves meet at, so the key cannot reach the reviewer through code either.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from nuramesh.core.vm_signer import (
    Issued,
    Refusal,
    ReviewFacts,
    ReviewRequest,
    Signer,
    SignerFailure,
)
from nuramesh.core.vm_verdicts import ResetRecord

PREPARE_REFUSALS = frozenset(
    {"diff_too_large", "diff_binary", "diff_header_mismatch", "head_moved"}
)
"""What the signer's own fetch may refuse with (SV-3)."""
MAX_PROPOSAL_BYTES = 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class ReviewInput:
    """Everything the reviewer step gets (SV-2). Exact plain types only, checked at
    construction, so no object with behaviour or a reference to the signer can ride along."""

    pr: int
    head_sha: str
    task: str
    definition_of_done: str
    diff: bytes
    canon_excerpts: tuple[str, ...]
    bundle_summary: str

    def __post_init__(self) -> None:
        if type(self.pr) is not int or self.pr < 1:
            raise TypeError("review input: pr is a positive integer")
        if type(self.head_sha) is not str or _SHA.fullmatch(self.head_sha) is None:
            raise TypeError("review input: head_sha is 40 lower-case hex characters")
        for name in ("task", "definition_of_done", "bundle_summary"):
            if type(getattr(self, name)) is not str:
                raise TypeError(f"review input: {name} is text")
        if type(self.diff) is not bytes:
            raise TypeError("review input: diff is bytes")
        if type(self.canon_excerpts) is not tuple or any(
            type(item) is not str for item in self.canon_excerpts
        ):
            raise TypeError("review input: canon_excerpts is a tuple of text")


@dataclass(frozen=True)
class Usage:
    """What one model call used. `cost` is a Decimal, or None when the price is unknown (never
    guessed, never a float)."""

    model_id: str
    tokens_in: int
    tokens_out: int
    price_version: str | None
    cost: Decimal | None

    def __post_init__(self) -> None:
        if type(self.model_id) is not str or not self.model_id:
            raise TypeError("usage: model_id is text")
        for name in ("tokens_in", "tokens_out"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"usage: {name} is a non-negative integer")
        if self.price_version is not None and type(self.price_version) is not str:
            raise TypeError("usage: price_version is text or None")
        if self.cost is not None and not isinstance(self.cost, Decimal):
            raise TypeError("usage: cost is a Decimal or None")


@dataclass(frozen=True)
class ReviewOutput:
    """What the reviewer step hands back: the proposal file's text and the call's usage."""

    text: str
    usage: Usage


class ReviewerFailed(Exception):
    """The reviewer step failed or timed out; `usage` when tokens were still spent."""

    def __init__(self, usage: Usage | None = None) -> None:
        self.usage = usage
        super().__init__("reviewer step failed")


Reviewer = Callable[[ReviewInput], ReviewOutput]
"""The keyless step. Any exception it raises is `model_failed` (SV-11)."""


@dataclass(frozen=True)
class Prepared:
    """What the signer's own side established for a request: the facts it will sign and the
    input folder for the reviewer."""

    facts: ReviewFacts
    review_input: ReviewInput


class SignerSide(Protocol):
    """The signer's own reads (engine): never the reviewer's, never the gate's request."""

    def prepare(self, request: ReviewRequest) -> Prepared | str:
        """Fetch and check the diff for the requested head (SV-3) and gather the facts; or one
        of `PREPARE_REFUSALS`."""
        ...

    def budget_left(self, request: ReviewRequest, facts: ReviewFacts) -> bool:
        """SV-2: whether the org's review budget allows one more model call."""
        ...

    def live_head(self, request: ReviewRequest) -> str:
        """The PR's head as the forge reports it now."""
        ...

    def resets(self) -> Sequence[ResetRecord]:
        """The ledgered `merge.review_reset` records of the org, built with
        `vm_limits.resets_from_ledger`: only admin-signed resets count (a trust boundary)."""
        ...


def _side[T](read: Callable[[], T]) -> T:
    try:
        return read()
    except SignerFailure:
        raise
    except Exception as exc:
        raise SignerFailure("side_unavailable") from exc


def _checked_output(output: object, facts: ReviewFacts) -> tuple[str, str, Usage | None] | None:
    """`(text, sha256 of its UTF-8 bytes, usage)` for an output the signer may parse; None for
    anything else. A usable `usage` is returned even when the rest is not (see `run_review`)."""
    try:
        if type(output) is not ReviewOutput:
            return None
        text, usage = output.text, output.usage
        if type(usage) is not Usage or type(text) is not str:
            return None
        if usage.model_id != facts.model_id:
            return None
        encoded = text.encode("utf-8")
    except (AttributeError, UnicodeError):
        return None
    if len(encoded) > MAX_PROPOSAL_BYTES:
        return None
    return text, hashlib.sha256(encoded).hexdigest(), usage


def _usage_of(value: object) -> Usage | None:
    usage = getattr(value, "usage", None)
    return usage if type(usage) is Usage else None


def run_review(
    signer: Signer, request: ReviewRequest, *, side: SignerSide, reviewer: Reviewer
) -> Issued | Refusal:
    """One review through both steps (module docstring). Returns the issued or stored decision,
    or the refusal; raises `SignerFailure` when the signer cannot continue safely."""
    refused = signer.screen(request)
    if refused is not None:
        return refused
    prepared = _side(lambda: side.prepare(request))
    if isinstance(prepared, str):
        if prepared not in PREPARE_REFUSALS:
            raise SignerFailure("side_unavailable")
        detail = "before_review" if prepared == "head_moved" else None
        return signer.refuse(request, prepared, detail)
    if type(prepared) is not Prepared:
        raise SignerFailure("side_unavailable")
    facts, review_input = prepared.facts, prepared.review_input
    if (
        type(review_input) is not ReviewInput
        or review_input.pr != request.pr
        or review_input.head_sha != request.head_sha
    ):
        raise SignerFailure("side_unavailable")
    admitted = signer.admit(request, facts, _side(side.resets))
    if admitted is not None:
        return admitted
    if _side(lambda: side.budget_left(request, facts)) is not True:
        return signer.refuse(request, "budget_exceeded")
    try:
        output: object = reviewer(review_input)
    except Exception as exc:
        failed_usage = _usage_of(exc)
        if failed_usage is not None:
            _record_cost(signer, request, failed_usage)
        return signer.refuse(request, "model_failed")
    checked = _checked_output(output, facts)
    spent = _usage_of(output)
    if spent is not None:
        _record_cost(signer, request, spent)
    if checked is None:
        return signer.refuse(request, "model_failed")
    text, text_sha256, _usage = checked
    live = _side(lambda: side.live_head(request))
    return signer.issue(
        request,
        facts,
        proposal_text=text,
        verdict_text_sha256=text_sha256,
        live_head=live,
        resets=_side(side.resets),
    )


def _record_cost(signer: Signer, request: ReviewRequest, usage: Usage) -> None:
    signer.record_cost(
        request,
        model_id=usage.model_id,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        price_version=usage.price_version,
        cost=usage.cost,
    )
