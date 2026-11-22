"""Verifier calibration: seeded known-good and known-bad outputs measure a verifier (PLAN D.7).

A **calibration suite** holds labelled samples (`good` or `bad`) for the criteria a verifier gate
checks. A **calibration run** shows every sample to one verifier agent through the same prompt
builder the real gate uses (`core.verifier.build_messages`), scores the answers and records ONE
ledger event (`calibration.scored`).

Each sample ends in one of seven classes: a bad sample the verifier failed on at least one of its
named defect criteria is **caught**; failed for another reason it is a **wrong_reason** (not
caught); passed it is **missed**; an unusable answer or a stop is an **error**. A good sample is
**accepted**, **false_reject**ed or an **error**. Rates are exact `Decimal`s: the catch rate, the
accept rate and the agreement round DOWN to 4 places and the false-accept rate rounds UP, so a
shown rate is never kinder than the truth. A run **passes** only when the integer comparison with
the unrounded thresholds holds, no `critical` sample is anything but caught and no sample is an
error.

A result is bound to the verifier, the exact suite content (`suite_hash`), the verifier prompt text
(`prompt_hash`) and the model that answered. A result counts for `valid_days`; the latest counting
result decides, so a failure is cleared only by a newer pass. Calibration never edits a verifier:
results are recorded, shown and read by the opt-in gate check (`calibration_suites` on a verifier
gate), nothing else. Nothing here touches I/O; `engine.calibration` runs it.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)

from nuramesh.core import verifier as _verifier
from nuramesh.core.canonical import canonical_json, content_hash
from nuramesh.core.gate_config import VerifierGateConfig
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.ledger import Hash, LedgerEvent
from nuramesh.core.org import OrgGraph, Slug
from nuramesh.core.verifier import CanonDocument, VerifierInput

CALIBRATION_SUITE_INVALID = "calibration_suite_invalid"
CALIBRATION_REAL_MODEL_GATE = "calibration_real_model_gate"
NOT_CALIBRATED_PREFIX = "verifier_not_calibrated: "
MIN_PER_LABEL = 5
MAX_SAMPLES = 200
MAX_OUTPUT_BYTES = 200_000
DEFAULT_VALID_DAYS = 30
DEFAULT_MAX_OUTPUT_TOKENS = 1_500
CALIBRATION_TASK_ID = "calibration"
RECENT_RESULTS = 5
_FOUR = Decimal("0.0001")

REASON_NONE = "not calibrated for this suite version, prompt and model"
REASON_EXPIRED = "expired"
REASON_FAILED = "the latest result failed"
REASON_NOT_REPRODUCED = "the latest result does not reproduce"
REASON_UNAVAILABLE = "suite not available"


class _Frozen(BaseModel):
    # A rejected sample output or key may be sensitive: never echo the input in str() and repr().
    # Known limit (#1144 F3 asks for no more): errors() and json() still carry the input; the
    # policy models scrub those with `_hide_input` (nuramesh.core.policy).
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class Label(StrEnum):
    """What a sample is: an output that meets its criteria, or one that does not."""

    GOOD = "good"
    BAD = "bad"


class SampleResult(StrEnum):
    """How one sample ended (spec rule 16)."""

    CAUGHT = "caught"
    MISSED = "missed"
    WRONG_REASON = "wrong_reason"
    BAD_ERROR = "bad_error"
    ACCEPTED = "accepted"
    FALSE_REJECT = "false_reject"
    GOOD_ERROR = "good_error"


class Observation(StrEnum):
    """What the verifier's answer to a sample was."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


_BAD_RESULTS = frozenset(
    {
        SampleResult.CAUGHT,
        SampleResult.MISSED,
        SampleResult.WRONG_REASON,
        SampleResult.BAD_ERROR,
    }
)
_ERRORS = frozenset({SampleResult.BAD_ERROR, SampleResult.GOOD_ERROR})


class CalibrationSample(_Frozen):
    """One labelled output. `defect_criteria` are the criteria a `bad` sample really fails."""

    id: Slug
    output: Any
    label: Label
    defect_criteria: tuple[Slug, ...] = ()
    critical: StrictBool = False

    @field_validator("output")
    @classmethod
    def _canonical(cls, value: Any) -> Any:
        try:
            size = len(canonical_json(value))
        except (TypeError, ValueError, RecursionError):
            raise ValueError(
                "a sample output must be canonical JSON (no floats or bytes)"
            ) from None
        if size > MAX_OUTPUT_BYTES:
            raise ValueError(f"a sample output is over {MAX_OUTPUT_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _label_matches(self) -> Self:
        if self.label is Label.GOOD:
            if self.defect_criteria:
                raise ValueError(f"good sample '{self.id}' has defect criteria")
            if self.critical:
                raise ValueError(f"good sample '{self.id}' is critical")
        elif not self.defect_criteria:
            raise ValueError(f"bad sample '{self.id}' names no defect criterion")
        if len(set(self.defect_criteria)) != len(self.defect_criteria):
            raise ValueError(f"sample '{self.id}' lists a defect criterion twice")
        return self


class CalibrationTask(_Frozen):
    """The task text the verifier is shown with every sample."""

    title: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    instructions: Annotated[str, StringConstraints(max_length=100_000)] = ""


Rate = Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1), decimal_places=4)]
PositiveRate = Annotated[Decimal, Field(gt=Decimal(0), le=Decimal(1), decimal_places=4)]


class CalibrationThresholds(_Frozen):
    """The bar: at most 4 decimal places (more is refused, never rounded)."""

    min_catch_rate: PositiveRate = Decimal("0.9500")
    max_false_accept_rate: Rate = Decimal("0.0500")
    min_accept_rate: PositiveRate = Decimal("0.8000")

    @field_validator("min_catch_rate", "max_false_accept_rate", "min_accept_rate", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        if isinstance(value, float):  # rule 1: no floats; write the rate as a string
            raise ValueError('a threshold is a string such as "0.95", never a float')
        return value

    @field_validator("min_catch_rate", "max_false_accept_rate", "min_accept_rate")
    @classmethod
    def _normal(cls, value: Decimal) -> Decimal:
        return value.quantize(_FOUR)  # "0.95" and "0.9500" are the same suite


class CalibrationSuite(_Frozen):
    """Labelled samples for the criteria of a verifier gate, with the bar to clear."""

    id: Slug
    version: StrictInt = Field(default=1, ge=1)
    task: CalibrationTask
    criteria: tuple[Criterion, ...] = Field(min_length=1)
    notes: Annotated[str, StringConstraints(max_length=5_000)] = ""
    canon: tuple[CanonDocument, ...] = ()
    samples: tuple[CalibrationSample, ...]
    max_output_tokens: StrictInt = Field(default=DEFAULT_MAX_OUTPUT_TOKENS, ge=1, le=16_000)
    valid_days: StrictInt = Field(default=DEFAULT_VALID_DAYS, ge=1, le=365)
    thresholds: CalibrationThresholds = CalibrationThresholds()

    @model_validator(mode="wrap")
    @classmethod
    def _coded(cls, data: Any, handler: ValidatorFunctionWrapHandler) -> Self:
        """Every way a suite can be wrong carries the one code `calibration_suite_invalid`."""
        try:
            return handler(data)  # type: ignore[no-any-return]
        except ValidationError as exc:
            parts = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(suite)'}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            raise ValueError(f"{CALIBRATION_SUITE_INVALID}: {parts}") from None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        try:
            self._check()
        except ValueError as exc:
            raise ValueError(f"{CALIBRATION_SUITE_INVALID}: {exc}") from None
        return self

    def _check(self) -> None:
        sample_ids = [s.id for s in self.samples]
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("sample ids must be unique within a suite")
        criterion_ids = [c.id for c in self.criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("criterion ids must be unique")
        if len(self.samples) > MAX_SAMPLES:
            raise ValueError(f"a suite has at most {MAX_SAMPLES} samples")
        bad = sum(1 for s in self.samples if s.label is Label.BAD)
        good = len(self.samples) - bad
        if bad < MIN_PER_LABEL or good < MIN_PER_LABEL:
            raise ValueError(
                f"a suite needs at least {MIN_PER_LABEL} bad and {MIN_PER_LABEL} good samples"
            )
        known = set(criterion_ids)
        for sample in self.samples:
            unknown = [c for c in sample.defect_criteria if c not in known]
            if unknown:
                raise ValueError(f"sample '{sample.id}' names unknown criteria {unknown}")

    @property
    def suite_hash(self) -> str:
        """SHA-256 over the whole suite: any edit changes it."""
        return content_hash(self.model_dump(mode="json"))


class CalibrationSuiteError(ValueError):
    """A suite could not be loaded (`calibration_suite_invalid`)."""


def parse_suite(data: Mapping[str, Any]) -> CalibrationSuite:
    """Load one suite; any problem raises `CalibrationSuiteError` carrying the code."""
    try:
        return CalibrationSuite.model_validate(data)
    except ValidationError as exc:
        raise CalibrationSuiteError(str(exc.errors()[0]["msg"])) from None


class CalibrationConfig(_Frozen):
    """All suites of an organization (the `calibration:` section)."""

    suites: tuple[CalibrationSuite, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [s.id for s in self.suites]
        if len(set(ids)) != len(ids):
            raise ValueError("suite ids must be unique")
        return self

    def suite(self, suite_id: str) -> CalibrationSuite | None:
        """The suite with this id, if any."""
        return next((s for s in self.suites if s.id == suite_id), None)


# -- the prompt the verifier sees ---------------------------------------------------------------

_PROBE = VerifierInput(
    task_id=CALIBRATION_TASK_ID,
    task_title="Probe task",
    task_instructions="Judge the probe output.",
    criteria=(
        Criterion(id="probe-one", text="The output is a greeting."),
        Criterion(id="probe-two", text="The output is polite."),
    ),
    gate_notes="Probe notes.",
    output={"probe": "hello"},
    canon=(CanonDocument(name="probe.md", text="Probe canon."),),
)


def prompt_hash() -> str:
    """SHA-256 of the messages the real gate's prompt builder makes for a fixed probe input.

    Hashing the built messages covers the system text, the reminder AND the rest of the fixed text
    of `build_messages` (the ids line, the section and marker format), so a change to any of it
    makes every older result stale."""
    built = _verifier.build_messages(_PROBE)
    return content_hash([{"role": m.role, "content": m.content} for m in built])


def verifier_input(suite: CalibrationSuite, sample: CalibrationSample) -> VerifierInput:
    """What the verifier is shown for one sample: the task, criteria, notes, the output and canon.
    The label, defect criteria, critical flag, sample id and thresholds are never in it."""
    return VerifierInput(
        task_id=CALIBRATION_TASK_ID,
        task_title=suite.task.title,
        task_instructions=suite.task.instructions,
        criteria=suite.criteria,
        gate_notes=suite.notes,
        output=sample.output,
        canon=suite.canon,
    )


# -- scoring ------------------------------------------------------------------------------------


def classify(
    label: Label, observation: Observation, defect_criteria: Sequence[str], not_met: Sequence[str]
) -> SampleResult:
    """One sample's class from its label and what the verifier said (rule 16). `not_met` are the
    criterion ids the verifier answered `not_met` for."""
    if label is Label.BAD:
        if observation is Observation.ERROR:
            return SampleResult.BAD_ERROR
        if observation is Observation.PASSED:
            return SampleResult.MISSED
        if set(defect_criteria) & set(not_met):
            return SampleResult.CAUGHT
        return SampleResult.WRONG_REASON
    if observation is Observation.ERROR:
        return SampleResult.GOOD_ERROR
    if observation is Observation.PASSED:
        return SampleResult.ACCEPTED
    return SampleResult.FALSE_REJECT


class Counts(_Frozen):
    """How many samples ended in each class."""

    caught: StrictInt = Field(ge=0)
    missed: StrictInt = Field(ge=0)
    wrong_reason: StrictInt = Field(ge=0)
    bad_error: StrictInt = Field(ge=0)
    accepted: StrictInt = Field(ge=0)
    false_reject: StrictInt = Field(ge=0)
    good_error: StrictInt = Field(ge=0)

    @property
    def bad(self) -> int:
        """B: every bad sample."""
        return self.caught + self.missed + self.wrong_reason + self.bad_error

    @property
    def good(self) -> int:
        """G: every good sample."""
        return self.accepted + self.false_reject + self.good_error


def count(results: Sequence[SampleResult]) -> Counts:
    """Tally classes."""
    return Counts(**{r.value: sum(1 for x in results if x is r) for r in SampleResult})


class Rates(_Frozen):
    """The four shown rates (4 places; the false-accept rate rounds up, the others down)."""

    catch_rate: Decimal
    false_accept_rate: Decimal
    accept_rate: Decimal
    agreement: Decimal


def rates_of(counts: Counts) -> Rates:
    """Rates from counts. A suite has at least 5 samples of each label, so no denominator is 0."""
    bad, good = Decimal(counts.bad), Decimal(counts.good)
    return Rates(
        catch_rate=(Decimal(counts.caught) / bad).quantize(_FOUR, rounding=ROUND_DOWN),
        false_accept_rate=(Decimal(counts.missed) / bad).quantize(_FOUR, rounding=ROUND_UP),
        accept_rate=(Decimal(counts.accepted) / good).quantize(_FOUR, rounding=ROUND_DOWN),
        agreement=(Decimal(counts.caught + counts.accepted) / (bad + good)).quantize(
            _FOUR, rounding=ROUND_DOWN
        ),
    )


def run_passes(
    thresholds: CalibrationThresholds, counts: Counts, results: Sequence[tuple[bool, SampleResult]]
) -> bool:
    """The pass rule (rule 18), compared exactly with the unrounded thresholds. `results` are
    `(critical, class)` per sample."""
    bad, good = Decimal(counts.bad), Decimal(counts.good)
    if bad == 0 or good == 0:
        return False
    return (
        Decimal(counts.caught) >= thresholds.min_catch_rate * bad
        and Decimal(counts.missed) <= thresholds.max_false_accept_rate * bad
        and Decimal(counts.accepted) >= thresholds.min_accept_rate * good
        and not any(critical and r is not SampleResult.CAUGHT for critical, r in results)
        and not any(r in _ERRORS for _, r in results)
    )


class CalibrationScore(_Frozen):
    """The score of one run: counts, rates and whether it passed."""

    counts: Counts
    rates: Rates
    passed: bool


def score_run(suite: CalibrationSuite, results: Sequence[SampleResult]) -> CalibrationScore:
    """Score one class per suite sample, in suite order."""
    if len(results) != len(suite.samples):
        raise ValueError("a calibration run needs exactly one result per sample")
    for sample, result in zip(suite.samples, results, strict=True):
        if (result in _BAD_RESULTS) is not (sample.label is Label.BAD):
            raise ValueError(f"result '{result.value}' doesn't fit sample '{sample.id}'")
    counts = count(results)
    flags = [(s.critical, r) for s, r in zip(suite.samples, results, strict=True)]
    return CalibrationScore(
        counts=counts, rates=rates_of(counts), passed=run_passes(suite.thresholds, counts, flags)
    )


# -- the stored result --------------------------------------------------------------------------


class Outcome(_Frozen):
    """How one sample ended: ids and a class, never the output or the reply."""

    sample_id: Slug
    label: Label
    result: SampleResult


class CalibrationRecord(_Frozen):
    """A stored result (the `calibration.scored` event)."""

    verifier: Slug
    model: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    suite_id: Slug
    suite_version: StrictInt
    suite_hash: Hash
    prompt_hash: Hash
    run_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    counts: Counts
    catch_rate: Decimal
    false_accept_rate: Decimal
    accept_rate: Decimal
    agreement: Decimal
    passed: StrictBool
    cost: Decimal
    currency: Annotated[str, StringConstraints(max_length=8)] | None
    cost_complete: StrictBool
    outcomes: tuple[Outcome, ...]
    at: datetime
    seq: StrictInt

    @staticmethod
    def ledger_payload(
        *,
        verifier: str,
        model: str,
        suite: CalibrationSuite,
        run_id: str,
        results: Sequence[SampleResult],
        cost: Decimal,
        currency: str | None,
        cost_complete: bool,
    ) -> dict[str, Any]:
        """The `calibration.scored` payload: ids, counts and rates only (no outputs, replies,
        evidence or output hashes)."""
        score = score_run(suite, results)
        return {
            "verifier": verifier,
            "model": model,
            "suite_id": suite.id,
            "suite_version": suite.version,
            "suite_hash": suite.suite_hash,
            "prompt_hash": prompt_hash(),
            "run_id": run_id,
            **score.counts.model_dump(),
            "catch_rate": str(score.rates.catch_rate),
            "false_accept_rate": str(score.rates.false_accept_rate),
            "accept_rate": str(score.rates.accept_rate),
            "agreement": str(score.rates.agreement),
            "passed": score.passed,
            "cost": str(cost),
            "currency": currency,
            "cost_complete": cost_complete,
            "outcomes": [
                {"sample_id": s.id, "label": s.label.value, "result": r.value}
                for s, r in zip(suite.samples, results, strict=True)
            ],
        }

    @classmethod
    def from_event(cls, event: LedgerEvent) -> Self:
        """Read a stored event (raises `KeyError`, `ValueError` or `TypeError` if malformed)."""
        p = event.payload
        return cls(
            verifier=p["verifier"],
            model=p["model"],
            suite_id=p["suite_id"],
            suite_version=p["suite_version"],
            suite_hash=p["suite_hash"],
            prompt_hash=p["prompt_hash"],
            run_id=p["run_id"],
            counts=Counts(**{r.value: p[r.value] for r in SampleResult}),
            catch_rate=Decimal(p["catch_rate"]),
            false_accept_rate=Decimal(p["false_accept_rate"]),
            accept_rate=Decimal(p["accept_rate"]),
            agreement=Decimal(p["agreement"]),
            passed=p["passed"],
            cost=Decimal(p["cost"]),
            currency=p["currency"],
            cost_complete=p["cost_complete"],
            outcomes=tuple(Outcome.model_validate(o) for o in p["outcomes"]),
            at=event.at,
            seq=event.seq,
        )


def reproduces(record: CalibrationRecord, suite: CalibrationSuite) -> bool:
    """Whether the stored result can be re-derived from its outcomes against this suite: the
    outcome list must equal the suite's samples in order, and the counts, rates and `passed` must
    match what the outcomes give (the `critical` flags are the suite's, never the record's)."""
    expected = [(s.id, s.label) for s in suite.samples]
    if [(o.sample_id, o.label) for o in record.outcomes] != expected:
        return False
    try:
        score = score_run(suite, [o.result for o in record.outcomes])
    except ValueError:
        return False
    return (
        score.counts == record.counts
        and score.rates.catch_rate == record.catch_rate
        and score.rates.false_accept_rate == record.false_accept_rate
        and score.rates.accept_rate == record.accept_rate
        and score.rates.agreement == record.agreement
        and score.passed == record.passed
    )


# -- standing -----------------------------------------------------------------------------------


class ResultView(_Frozen):
    """A result as `status` and the route show it: ids, counts and rates only."""

    seq: int
    at: datetime
    model: str
    suite_version: int
    passed: bool
    reproduces: bool
    counts: Counts
    catch_rate: str
    false_accept_rate: str
    accept_rate: str
    agreement: str


def _view(record: CalibrationRecord, ok: bool) -> ResultView:
    return ResultView(
        seq=record.seq,
        at=record.at,
        model=record.model,
        suite_version=record.suite_version,
        passed=record.passed and ok,
        reproduces=ok,
        counts=record.counts,
        catch_rate=str(record.catch_rate),
        false_accept_rate=str(record.false_accept_rate),
        accept_rate=str(record.accept_rate),
        agreement=str(record.agreement),
    )


class Standing(_Frozen):
    """Whether a verifier is calibrated for one suite now, and why."""

    suite: Slug
    calibrated: bool
    reason: str | None
    latest: ResultView | None
    trend: Decimal | None
    recent_results: tuple[ResultView, ...]


def standing(
    suite: CalibrationSuite,
    records: Sequence[CalibrationRecord],
    *,
    verifier: str,
    model: str | None,
    now: datetime,
) -> Standing:
    """Apply rule 20. `records` are every stored result of the organization in ledger order.

    Only results for this verifier, this suite content, the current prompt hash and this model
    count; one older than `valid_days` (exactly at the boundary included) is expired; each is
    re-derived (`reproduces`) and one that does not counts as failed. The latest counting result
    decides; a failure is cleared only by a newer pass."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    window = timedelta(days=suite.valid_days)
    current_prompt = prompt_hash()
    mine = sorted(
        (r for r in records if r.verifier == verifier and r.suite_id == suite.id),
        key=lambda r: r.seq,
    )
    recent = tuple(_view(r, reproduces(r, suite)) for r in reversed(mine[-RECENT_RESULTS:]))
    same = [
        r
        for r in mine
        if r.suite_hash == suite.suite_hash and r.prompt_hash == current_prompt and r.model == model
    ]
    counting = [r for r in same if now < r.at + window]
    if not counting:
        why = REASON_EXPIRED if same else REASON_NONE
        return Standing(
            suite=suite.id, calibrated=False, reason=why, latest=None, trend=None,
            recent_results=recent,
        )  # fmt: skip
    ok = [reproduces(r, suite) for r in counting]
    latest = counting[-1]
    latest_ok = ok[-1]
    trend: Decimal | None = None
    for older, older_ok in zip(reversed(counting[:-1]), reversed(ok[:-1]), strict=True):
        if older_ok:
            trend = latest.catch_rate - older.catch_rate
            break
    if not latest_ok:
        reason: str | None = REASON_NOT_REPRODUCED
    elif not latest.passed:
        reason = REASON_FAILED
    else:
        reason = None
    return Standing(
        suite=suite.id,
        calibrated=reason is None,
        reason=reason,
        latest=_view(latest, latest_ok),
        trend=trend,
        recent_results=recent,
    )


def gate_mismatch(
    suite: CalibrationSuite,
    *,
    notes: str,
    max_output_tokens: int,
    criteria: Sequence[Criterion],
    role_max_tokens: int | None = None,
) -> str | None:
    """What the gate gives the verifier that the suite did not calibrate (M3): its notes, its
    output limit, or a criterion whose exact text is not in the suite. The gate's canon is not
    part of calibration (stated limit)."""
    if notes != suite.notes:
        return "gate notes differs from the suite"
    if max_output_tokens != suite.max_output_tokens:
        return "gate max_output_tokens differs from the suite"
    if role_max_tokens is not None and role_max_tokens < suite.max_output_tokens:
        return "verifier role max_tokens below the suite max_output_tokens"  # R1.42 (C49)
    texts = {c.text for c in suite.criteria}
    if any(c.text not in texts for c in criteria):
        return "gate criteria differs from the suite"
    return None


def not_calibrated_message(reasons: Sequence[str]) -> str:
    """The one gate error text for every failing suite, in listed order."""
    return NOT_CALIBRATED_PREFIX + "; ".join(reasons)


def gate_standing_reasons(
    config: CalibrationConfig | None,
    suite_ids: Sequence[str],
    *,
    notes: str,
    max_output_tokens: int,
    criteria: Sequence[Criterion],
    records: Sequence[CalibrationRecord],
    verifier: str,
    model: str | None,
    now: datetime,
    role_max_tokens: int | None = None,
) -> list[str]:
    """`<suite id>: <reason>` for every listed suite that does not hold, in order (empty when all
    hold). Fail closed: no configuration or a missing suite is `suite not available`."""
    reasons: list[str] = []
    for suite_id in suite_ids:
        suite = config.suite(suite_id) if config is not None else None
        if suite is None:
            reasons.append(f"{suite_id}: {REASON_UNAVAILABLE}")
            continue
        mismatch = gate_mismatch(
            suite,
            notes=notes,
            max_output_tokens=max_output_tokens,
            criteria=criteria,
            role_max_tokens=role_max_tokens,
        )
        if mismatch is not None:
            reasons.append(f"{suite_id}: {mismatch}")
            continue
        found = standing(suite, records, verifier=verifier, model=model, now=now)
        if not found.calibrated:
            reasons.append(f"{suite_id}: {found.reason}")
    return reasons


# -- configuration problems ---------------------------------------------------------------------


def is_fake_ref(ref: str | None) -> bool:
    """Whether a `provider/model` setting names the fake provider."""
    return ref is not None and ref.split("/", 1)[0] == "fake"


def gate_calibration_problems(
    gate: GateSpec,
    dod: DefinitionOfDone,
    org: OrgGraph,
    config: CalibrationConfig | None,
    *,
    effective_ref: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Problems with one gate's `calibration_suites` (rule 21): an unknown suite, a verifier that
    is not on the fake model (configured or effective: calibration cannot run for a real model, so
    the gate could never be satisfied), a gate whose notes, output limit or criteria differ from
    the suite's. Empty for a gate without the field or one that is not a verifier gate."""
    if gate.kind is not GateKind.VERIFIER:
        return []
    from nuramesh.core.canonical import thaw

    try:
        gate_config = VerifierGateConfig.model_validate(thaw(gate.config))
    except ValidationError:
        return []  # reported by gate_config_problems
    if not gate_config.calibration_suites:
        return []
    problems: list[str] = []
    refs = [org.model_for(gate_config.verifier)] if _has_agent(org, gate_config.verifier) else []
    if effective_ref is not None:
        refs.append(effective_ref(gate_config.verifier))
    if any(not is_fake_ref(r) for r in refs):
        problems.append(
            f"{CALIBRATION_REAL_MODEL_GATE}: verifier '{gate_config.verifier}' is not on a fake/* "
            "model, and calibration can only run for the fake model, so this gate could never "
            "be satisfied"
        )
    wanted = set(gate.checks)
    criteria = tuple(c for c in dod.criteria if c.id in wanted)
    role_cap = (
        org.role_of(gate_config.verifier).max_tokens
        if _has_agent(org, gate_config.verifier)
        else None
    )
    for suite_id in gate_config.calibration_suites:
        suite = config.suite(suite_id) if config is not None else None
        if suite is None:
            problems.append(f"calibration_suites names unknown suite '{suite_id}'")
            continue
        mismatch = gate_mismatch(
            suite,
            notes=gate_config.notes,
            max_output_tokens=gate_config.max_output_tokens,
            criteria=criteria,
            role_max_tokens=role_cap,
        )
        if mismatch is not None:
            problems.append(f"calibration suite '{suite_id}': {mismatch}")
    return problems


def _has_agent(org: OrgGraph, agent_id: str) -> bool:
    try:
        org.agent(agent_id)
    except KeyError:
        return False
    return True


def calibration_problems(
    dod: DefinitionOfDone,
    org: OrgGraph,
    config: CalibrationConfig | None,
    *,
    effective_ref: Callable[[str], str | None] | None = None,
) -> list[str]:
    """`gate_calibration_problems` for every gate, prefixed by the gate id."""
    return [
        f"gate '{g.id}': {p}"
        for g in dod.gates
        for p in gate_calibration_problems(g, dod, org, config, effective_ref=effective_ref)
    ]
