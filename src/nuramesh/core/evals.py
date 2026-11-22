"""Evals per role: suites, scoring, thresholds, validity windows and eligibility (PLAN D.6; T27).

An **eval suite** is a set of cases for one role. A case has a prompt, one or more **rules** (the
same deterministic checks rule gates use, `core.rules`) that a response must meet, a `weight` and
an optional `critical` flag. A case passes only if **every** rule is met. The **score** is the
earned weight over the total weight; the suite **passes** when the score is at least the suite's
`pass_threshold` **and** no critical case failed. The comparison is exact (`Decimal`, no rounding
up across the threshold); the shown score is rounded **down** to 4 places.

A suite result is bound to the **exact suite content** (`suite_hash`, so editing any case
invalidates old results), to the **subject** (an agent or a role) and to the **model** that
answered (a different model needs its own eval). Results are ledger events (`eval.scored`) holding
case ids, verdicts, rule details and response **hashes**, never the prompts' answers (A7).

**Eligibility** (`check_eligibility`): a role may require suites (`EvalRequirement`). An agent can
take new work only if, for every required suite, looking at the results for **this suite content
and this model** (agent-level and role-level results):

1. a result that is older than the suite's `valid_days` is ignored (it expired);
2. of the remaining results, the **latest** at each level (agent, role) counts; if any of those
   failed, the agent is **not eligible**; only a newer passing result at that level clears it;
3. otherwise at least one passing result must remain, else the agent is **not eligible**
   ("not evaluated" or "expired").

A role with no requirements needs no evals. No code path treats missing results as a pass.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from nuramesh.core.canonical import canonical_json, content_hash
from nuramesh.core.content import LedgerHasher, LedgerHashKind
from nuramesh.core.ledger import Hash, LedgerEvent
from nuramesh.core.org import Slug
from nuramesh.core.rules import Rule, evaluate

MAX_CASES = 200
MAX_PROMPT = 20_000
MAX_RULES_TOTAL = 350  # all rules of a suite; with MAX_DETAIL_BYTES keeps `eval.scored` < 256 KB
MAX_DETAIL_BYTES = 350  # one rule's detail text, measured as canonical JSON
DEFAULT_VALID_DAYS = 30

Threshold = Annotated[Decimal, Field(gt=Decimal(0), le=Decimal(1), decimal_places=4)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvalCase(_Frozen):
    """One question for the agent and the rules a good answer meets."""

    id: Slug
    prompt: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PROMPT)]
    rules: tuple[Rule, ...] = Field(min_length=1, max_length=50)
    weight: int = Field(default=1, ge=1, le=100)
    critical: bool = False


class EvalSuite(_Frozen):
    """The cases for one role, with the bar to clear and how long a pass stays valid."""

    id: Slug
    version: int = Field(default=1, ge=1)
    role: Slug
    cases: tuple[EvalCase, ...] = Field(min_length=1, max_length=MAX_CASES)
    pass_threshold: Threshold = Decimal("0.8000")
    valid_days: int = Field(default=DEFAULT_VALID_DAYS, ge=1, le=365)

    @field_validator("pass_threshold")
    @classmethod
    def _normal(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.0001"))  # "0.8" and "0.80" are the same suite

    @model_validator(mode="after")
    def _unique_cases(self) -> Self:
        ids = [c.id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("case ids must be unique within a suite")
        if sum(len(c.rules) for c in self.cases) > MAX_RULES_TOTAL:
            raise ValueError(f"a suite may have at most {MAX_RULES_TOTAL} rules in all")
        return self

    @property
    def suite_hash(self) -> str:
        """SHA-256 over the whole suite: any edit to any case changes it."""
        return content_hash(self.model_dump(mode="json"))


class EvalRequirement(_Frozen):
    """The suites a role's agents must have passed to take new work."""

    role: Slug
    suites: tuple[Slug, ...] = Field(min_length=1, max_length=50)


class EvalConfig(_Frozen):
    """All suites and which roles require which."""

    suites: tuple[EvalSuite, ...] = ()
    requirements: tuple[EvalRequirement, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        by_id = {s.id: s for s in self.suites}
        if len(by_id) != len(self.suites):
            raise ValueError("suite ids must be unique")
        roles: set[str] = set()
        for requirement in self.requirements:
            if requirement.role in roles:
                raise ValueError(f"two requirements for role '{requirement.role}'")
            roles.add(requirement.role)
            if len(set(requirement.suites)) != len(requirement.suites):
                raise ValueError(f"role '{requirement.role}' lists a suite twice")
            for suite_id in requirement.suites:
                suite = by_id.get(suite_id)
                if suite is None:
                    raise ValueError(
                        f"role '{requirement.role}' requires unknown suite '{suite_id}'"
                    )
                if suite.role != requirement.role:
                    raise ValueError(
                        f"suite '{suite_id}' is for role '{suite.role}', not '{requirement.role}'"
                    )
        return self

    def suite(self, suite_id: str) -> EvalSuite | None:
        """The suite with this id, if any."""
        return next((s for s in self.suites if s.id == suite_id), None)

    def required_for(self, role: str) -> tuple[EvalSuite, ...]:
        """The suites a role requires (empty when it requires none)."""
        for requirement in self.requirements:
            if requirement.role == role:
                return tuple(s for sid in requirement.suites if (s := self.suite(sid)) is not None)
        return ()


class SubjectKind(StrEnum):
    """What an eval result is about."""

    AGENT = "agent"
    ROLE = "role"


class CaseOutcome(_Frozen):
    """How one case went: pass or fail with the rule details (which never quote the answer)."""

    case_id: Slug
    passed: StrictBool
    weight: StrictInt
    critical: StrictBool
    details: tuple[str, ...]
    response_hash: Hash | None = None


class SuiteScore(_Frozen):
    """The result of scoring one set of responses against a suite."""

    suite_id: Slug
    suite_version: int
    suite_hash: Hash
    score: Decimal
    passed: bool
    outcomes: tuple[CaseOutcome, ...]


def _bounded(text: str) -> str:
    """`text` cut so its canonical JSON is at most MAX_DETAIL_BYTES (control and non-ASCII
    characters take up to 12 bytes each there), so a stored result always fits the ledger."""
    while len(canonical_json(text)) > MAX_DETAIL_BYTES:
        text = text[: max(1, int(len(text) * 0.8) - 1)]
    return text


def score_case(case: EvalCase, response: Any | None, hasher: LedgerHasher) -> CaseOutcome:
    """Score one response (`None` means no response). Every rule must be met. `hasher` makes the
    keyed `eval_response` hash (PH-KEY R3); it isn't called for a missing response."""
    if response is None:
        return CaseOutcome(
            case_id=case.id,
            passed=False,
            weight=case.weight,
            critical=case.critical,
            details=("no response",),
        )
    details: list[str] = []
    passed = True
    for rule in case.rules:
        try:
            met, detail = evaluate(rule.check, response)
        except (ValueError, TypeError, RecursionError) as exc:
            met, detail = False, f"{rule.criterion}: couldn't be checked ({type(exc).__name__})"
        passed = passed and met
        details.append(_bounded(f"{rule.criterion}: {'met' if met else 'not met'}: {detail}"))
    try:
        response_hash: str | None = hasher(LedgerHashKind.EVAL_RESPONSE, canonical_json(response))
    except (TypeError, ValueError, RecursionError):  # a float, bytes, or absurd nesting
        passed, response_hash = False, None
        details.append("response isn't canonical JSON (floats and bytes are refused)")
    return CaseOutcome(
        case_id=case.id,
        passed=passed,
        weight=case.weight,
        critical=case.critical,
        details=tuple(details),
        response_hash=response_hash,
    )


def score_suite(suite: EvalSuite, responses: Mapping[str, Any], hasher: LedgerHasher) -> SuiteScore:
    """Score every case; a case without a response fails. Unknown case ids are an error."""
    known = {c.id for c in suite.cases}
    extra = sorted(set(responses) - known)
    if extra:
        raise ValueError(f"responses for unknown cases: {', '.join(extra[:5])}")
    outcomes = tuple(score_case(case, responses.get(case.id), hasher) for case in suite.cases)
    total = sum(o.weight for o in outcomes)
    earned = sum(o.weight for o in outcomes if o.passed)
    score = (Decimal(earned) / Decimal(total)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return SuiteScore(
        suite_id=suite.id,
        suite_version=suite.version,
        suite_hash=suite.suite_hash,
        score=score,
        passed=suite_passes(suite, outcomes),
        outcomes=outcomes,
    )


def suite_passes(suite: EvalSuite, outcomes: Sequence[CaseOutcome]) -> bool:
    """The pass rule applied to outcomes: exact threshold and no critical failure."""
    total = sum(o.weight for o in outcomes)
    earned = sum(o.weight for o in outcomes if o.passed)
    clears = total > 0 and Decimal(earned) >= suite.pass_threshold * Decimal(total)
    return clears and not any(o.critical and not o.passed for o in outcomes)


class EvalRecord(_Frozen):
    """A stored eval result (the `eval.scored` event): who, which suite content, which model."""

    subject_kind: SubjectKind
    subject_id: Slug
    model: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None
    suite_id: Slug
    suite_version: int
    suite_hash: Hash
    score: Decimal
    passed: StrictBool
    outcomes: tuple[CaseOutcome, ...]
    at: datetime
    seq: int

    @staticmethod
    def ledger_payload(
        kind: SubjectKind, subject_id: str, model: str | None, score: SuiteScore
    ) -> dict[str, Any]:
        """The `eval.scored` payload for a result (no answers, only hashes and verdicts)."""
        return {
            "subject_kind": kind.value,
            "subject_id": subject_id,
            "model": model,
            "suite_id": score.suite_id,
            "suite_version": score.suite_version,
            "suite_hash": score.suite_hash,
            "score": str(score.score),
            "passed": score.passed,
            "outcomes": [o.model_dump(mode="json") for o in score.outcomes],
        }

    @classmethod
    def from_event(cls, event: LedgerEvent) -> Self:
        """Read a stored `eval.scored` event (raises `ValueError` if it is malformed)."""
        p = event.payload
        return cls(
            subject_kind=SubjectKind(p["subject_kind"]),
            subject_id=p["subject_id"],
            model=p["model"],
            suite_id=p["suite_id"],
            suite_version=p["suite_version"],
            suite_hash=p["suite_hash"],
            score=Decimal(p["score"]),
            passed=p["passed"],
            outcomes=tuple(CaseOutcome.model_validate(o) for o in p["outcomes"]),
            at=event.at,
            seq=event.seq,
        )


def verified(record: EvalRecord, suite: EvalSuite) -> EvalRecord:
    """The record as the suite says it should read: `passed` is recomputed from the outcomes.

    A stored result is only trusted as far as it can be re-derived: if the outcomes don't match the
    suite's cases (ids, weights, critical flags) or the stored `passed`/`score` disagree with them,
    the record counts as a **failure**, never a pass (a forged or damaged event can't grant work).
    """
    expected = [(c.id, c.weight, c.critical) for c in suite.cases]
    actual = [(o.case_id, o.weight, o.critical) for o in record.outcomes]
    if record.suite_hash != suite.suite_hash:
        return record
    if actual != expected:
        return record.model_copy(update={"passed": False})
    derived = suite_passes(suite, record.outcomes)
    earned = sum(o.weight for o in record.outcomes if o.passed)
    total = sum(o.weight for o in record.outcomes)
    shown = (Decimal(earned) / Decimal(total)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    if record.passed != derived or record.score != shown:
        return record.model_copy(update={"passed": False})
    return record


class Eligibility(_Frozen):
    """Whether an agent may take new work, and the reason for every blocked suite."""

    eligible: bool
    reasons: tuple[str, ...] = ()


def check_eligibility(
    config: EvalConfig | None,
    *,
    role: str,
    agent: str,
    model: str | None,
    records: Sequence[EvalRecord],
    now: datetime,
    suites: Sequence[str] | None = None,
) -> Eligibility:
    """Apply the module's eligibility rules. `records` are in ledger order (oldest first).

    `suites` (suite ids, from `core.presets.effective_suites`) replaces the role's own
    requirements and is resolved against `config`; `config` may then be `None`. A suite that isn't
    defined, or is for another role, can never be satisfied: fail closed, one reason per suite.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    reasons: list[str] = []
    if suites is None:
        wanted: list[tuple[str, EvalSuite | None]] = [
            (s.id, s) for s in (config.required_for(role) if config is not None else ())
        ]
    else:
        wanted = [(sid, config.suite(sid) if config is not None else None) for sid in suites]
    for suite_id, suite in wanted:
        if suite is None:
            reasons.append(f"suite '{suite_id}': not defined")
            continue
        if suite.role != role:
            reasons.append(f"suite '{suite_id}': is for role '{suite.role}', not '{role}'")
            continue
        window = timedelta(days=suite.valid_days)
        mine = [
            r
            for r in records
            if r.suite_id == suite.id
            and r.suite_hash == suite.suite_hash
            and r.model == model
            and (
                (r.subject_kind is SubjectKind.AGENT and r.subject_id == agent)
                or (r.subject_kind is SubjectKind.ROLE and r.subject_id == role)
            )
        ]
        fresh = [r for r in mine if now < r.at + window]
        latest: dict[SubjectKind, EvalRecord] = {}
        for record in sorted(fresh, key=lambda r: r.seq):
            latest[record.subject_kind] = record  # later records overwrite earlier ones
        failed = [r for r in latest.values() if not r.passed]
        if failed:
            who = ", ".join(sorted(r.subject_kind.value for r in failed))
            reasons.append(f"suite '{suite.id}': the latest result failed ({who} level)")
        elif not latest:
            why = "expired" if mine else "not evaluated for this suite version and model"
            reasons.append(f"suite '{suite.id}': {why}")
    return Eligibility(eligible=not reasons, reasons=tuple(reasons))
