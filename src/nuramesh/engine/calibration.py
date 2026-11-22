"""Verifier calibration runs and standings (R2.08, spec/r208/README.md).

A run asks ONE verifier agent to judge every sample of a suite through the same prompt builder the
real gate uses, one metered `call_model` call per sample, scores the answers and records ONE
`calibration.scored` event in the organization stream. Nothing here edits a verifier, a gate or a
suite (rule 4): a result is only recorded, shown, or read by the opt-in gate check.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from nuramesh.core.budgets import BudgetExceededError, CallContext
from nuramesh.core.calibration import (
    CalibrationConfig,
    CalibrationRecord,
    CalibrationSuite,
    Label,
    Observation,
    SampleResult,
    Standing,
    classify,
    is_fake_ref,
    standing,
    verifier_input,
)
from nuramesh.core.content import MarkedLedgerHasher
from nuramesh.core.gates import Verdict
from nuramesh.core.ledger import EventType
from nuramesh.core.models import ModelProvider, ModelRequest, parse_model_ref
from nuramesh.core.money import Money
from nuramesh.core.org import OrgGraph, RoleKind
from nuramesh.core.prices import PriceBook, PriceOverride, UnknownPriceError
from nuramesh.core.verifier import build_messages, parse_reply
from nuramesh.engine.content import ContentError
from nuramesh.engine.governance import Governance, ModelBlocked
from nuramesh.engine.models import (
    ModelCallError,
    ModelCallIncomplete,
    PolicyDenied,
    call_model,
)
from nuramesh.engine.org_halt import OrgHalted, RolePaused
from nuramesh.stores.base import EventDraft, Store, run_stream

CALIBRATION_ACTOR = "service:cli"


class CalibrationError(ValueError):
    """A calibration run could not start or could not be trusted (exit 2, nothing recorded)."""


class CalibrationLedgerError(ValueError):
    """A stored `calibration.scored` event is unreadable (the gate treats it as an error)."""


class Stop:
    """Why a run stopped before judging every sample."""

    BUDGET = "budget"  # BudgetExceededError, NoBudgetError, PolicyDenied: exit 1
    HALT = "halt"  # OrgHalted or RolePaused raised in call_model: exit 4
    REFUSED = "refused"  # the organization's key was lost or unreadable mid-run: exit 3


@dataclass(frozen=True)
class CalibrationRun:
    """The recorded result of a run, and why it stopped early (None if it did not)."""

    record: CalibrationRecord
    stop: str | None
    stop_detail: str | None


def org_stream(org_id: str) -> str:
    """The organization's ledger stream."""
    return f"org:{org_id}"


def load_calibration_records(store: Store, org_id: str) -> list[CalibrationRecord]:
    """Every stored result of an organization, oldest first. An unreadable event raises
    `CalibrationLedgerError`: skipping it could hide a newer failure."""
    records: list[CalibrationRecord] = []
    for event in store.read_stream(org_stream(org_id)):
        if event.type != EventType.CALIBRATION_SCORED.value:
            continue
        if event.actor != CALIBRATION_ACTOR:  # rule 24: only the service writes this kind
            raise CalibrationLedgerError(
                f"calibration.scored event {event.seq} of {org_stream(org_id)} has actor "
                f"{event.actor!r}, not {CALIBRATION_ACTOR!r}"
            )
        try:
            records.append(CalibrationRecord.from_event(event))
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            raise CalibrationLedgerError(
                f"calibration.scored event {event.seq} of {org_stream(org_id)} is unreadable "
                f"({type(exc).__name__})"
            ) from None
    return records


def standings(
    store: Store,
    config: CalibrationConfig | None,
    suite_ids: Sequence[str],
    *,
    verifier: str,
    model: str | None,
    org_id: str,
    now: datetime,
) -> list[Standing]:
    """The standing of a verifier for each listed suite (a missing suite is not available)."""
    records = load_calibration_records(store, org_id)
    out: list[Standing] = []
    for suite_id in suite_ids:
        suite = config.suite(suite_id) if config is not None else None
        if suite is None:
            out.append(
                Standing(
                    suite=suite_id,
                    calibrated=False,
                    reason="suite not available",
                    latest=None,
                    trend=None,
                    recent_results=(),
                )
            )
            continue
        out.append(standing(suite, records, verifier=verifier, model=model, now=now))
    return out


def verifier_problem(org: OrgGraph, verifier: str, effective: str | None) -> str | None:
    """Why this agent cannot be calibrated (usage errors, rule 10 and 11), or None."""
    try:
        org.agent(verifier)
    except KeyError:
        return f"unknown agent '{verifier}'"
    if org.role_of(verifier).kind is not RoleKind.VERIFIER:
        return f"'{verifier}' doesn't have a verifier role"
    configured = org.model_for(verifier)
    if configured is None:
        return f"agent '{verifier}' has no model"
    for ref in (configured, effective):
        if ref is None:
            return f"agent '{verifier}' has no model"
        try:
            parse_model_ref(ref)
        except ValueError as exc:
            return f"verifier '{verifier}': {exc}"
        if not is_fake_ref(ref):
            return (
                "calibrate run is fake-only: no real provider is supported for calibration "
                f"in this version (model '{ref}')"
            )
    return None


def run_calibration(
    store: Store,
    provider: ModelProvider,
    suite: CalibrationSuite,
    *,
    org: OrgGraph,
    verifier: str,
    prices: PriceBook,
    run_id: str,
    authorize: Callable[[CallContext, ModelRequest], None],
    authorize_spend: Callable[[CallContext, Money], None] | None = None,
    overrides: tuple[PriceOverride, ...] = (),
    governance: Governance | None = None,
    hasher: MarkedLedgerHasher,
    after_call: Callable[[], None] | None = None,
    now: Callable[[], datetime] | None = None,
    actor: str = CALIBRATION_ACTOR,
) -> CalibrationRun:
    """Judge every sample of `suite` with `verifier` and record the result (rules 10 to 15, 24).

    Each sample is one stand-alone `call_model` call on the verifier's effective model. A stop
    (budget, policy, halt, pause, a lost key) ends the calls; every sample not yet judged is an
    `error` and the run is still scored and recorded. A `CalibrationError` records nothing."""
    try:
        org.agent(verifier)
    except KeyError:
        raise CalibrationError(f"unknown agent '{verifier}'") from None
    effective = governance.effective_ref(verifier) if governance else org.model_for(verifier)
    problem = verifier_problem(org, verifier, effective)
    if problem is not None:
        raise CalibrationError(problem)
    assert effective is not None
    provider_id, endpoint_id, model = parse_model_ref(effective)
    criterion_ids = [c.id for c in suite.criteria]
    results: list[SampleResult] = []
    stop: str | None = None
    detail: str | None = None
    for number, sample in enumerate(suite.samples, start=1):
        if stop is not None:
            results.append(_error_of(sample.label))
            continue
        request = ModelRequest(
            provider=provider_id,
            endpoint_id=endpoint_id,
            model=model,
            messages=build_messages(verifier_input(suite, sample)),
            max_output_tokens=suite.max_output_tokens,
        )
        observation, not_met = Observation.ERROR, []
        try:
            try:
                result = call_model(
                    store,
                    provider,
                    request,
                    context=CallContext(run_id=run_id, agent_id=verifier, cap_exempt=True),
                    org=org,
                    prices=prices,
                    reservation_id=f"{run_id}-{number}",
                    actor=f"agent:{verifier}",
                    authorize=authorize,
                    authorize_spend=authorize_spend,
                    overrides=overrides,
                    now=now,
                    governance=governance,
                    hasher=hasher,
                )
                text: str | None = result.response.text
            except ModelCallIncomplete as exc:
                text = None if exc.stage in ("settle", "late_unrecorded") else exc.response.text
            if text is not None:
                judgement = parse_reply(text, criterion_ids)
                if judgement.usable:
                    not_met = [
                        c for c, (v, _) in judgement.findings.items() if v is Verdict.NOT_MET
                    ]
                    observation = Observation.FAILED if not_met else Observation.PASSED
        except (BudgetExceededError, PolicyDenied) as exc:
            stop, detail = Stop.BUDGET, type(exc).__name__
        except (OrgHalted, RolePaused) as exc:
            stop, detail = Stop.HALT, type(exc).__name__
        except ContentError as exc:
            stop, detail = Stop.REFUSED, type(exc).__name__
        except (UnknownPriceError, ModelBlocked, ModelCallError):
            pass  # this sample is an error; the run goes on (rule 15)
        finally:
            if after_call is not None:
                after_call()
        results.append(classify(sample.label, observation, sample.defect_criteria, not_met))
    cost, currency, complete, models = _spend(store, run_id)
    if len(models) > 1:
        raise CalibrationError(
            f"the samples were answered by more than one model ({', '.join(sorted(models))}); "
            "the run was refused and nothing was recorded"
        )
    answered = next(iter(models)) if models else effective
    payload = CalibrationRecord.ledger_payload(
        verifier=verifier,
        model=answered,
        suite=suite,
        run_id=run_id,
        results=results,
        cost=cost,
        currency=currency,
        cost_complete=complete,
    )
    with store.transaction() as uow:
        event = uow.append(
            EventDraft(
                stream=org_stream(org.id),
                type=EventType.CALIBRATION_SCORED,
                actor=actor,
                payload=payload,
            )
        )
    return CalibrationRun(CalibrationRecord.from_event(event), stop, detail)


def _error_of(label: Label) -> SampleResult:
    return SampleResult.BAD_ERROR if label is Label.BAD else SampleResult.GOOD_ERROR


def _spend(store: Store, run_id: str) -> tuple[Decimal, str | None, bool, set[str]]:
    """The run's cost from its `model.called` events (rule 19), whether that is complete, and
    the model refs that answered."""
    total = Decimal(0)
    currency: str | None = None
    complete = True
    models: set[str] = set()
    for event in store.read_stream(run_stream(run_id)):
        payload: Any = event.payload
        if event.type == EventType.MODEL_BLOCKED.value:
            if payload.get("reason") == "unknown_price":
                complete = False
            continue
        if event.type != EventType.MODEL_CALLED.value:
            continue
        # Every outcome counts (ok, settled_late, ...): a paid call cannot hide a spend (rule 19).
        models.add(f"{payload['provider']}/{payload['model']}")
        if event.cost is None:
            complete = False
            continue
        code = str(event.cost.currency)
        if currency is not None and code != currency:
            complete = False
            continue
        currency = code
        total += event.cost.amount
    return total, currency, complete, models
