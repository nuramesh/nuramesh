"""Run an independent verifier gate (PLAN D.3, D.4, D.5; decisions A4, T19).

`run_verifier_gate` checks a task's output with a verifier agent:

1. The gate must be a verifier gate on a task that is `verifying`, and the verifier must satisfy
   `core.gate_config.verifier_gate_problems` (an agent with a verifier role and a `provider/model`,
   not the worker, and a different model if the gate requires it). Otherwise `GateError`: that is a
   configuration or caller mistake, not a verification outcome.
2. The verifier is shown a `VerifierInput` (task, the criteria this gate checks, the gate's notes,
   the output and the canon) and nothing else. The output must be canonical JSON.
3. The call goes through `call_model` with the **verifier's own agent id and the task's id**, so
   its cost counts against the task's, the run's and the verifier's budgets (D.5) and appears in
   the ledger (tokens, cost, prompt hash; never the text).
4. The reply is read strictly (`core.verifier.parse_reply`). The result is `passed` only if every
   criterion is `met` with real evidence, `failed` if the verifier found something not met, and
   `error` for everything that means the verifier couldn't judge: a reply that can't be used, a
   budget stop, an unknown price, a policy refusal, a paused verifier role (E19), a provider
   failure. An `error` can be re-run without costing the worker an attempt.

The caller records the result (`record_gate_result`) like any other. **Duty of the run engine
(R1.15):** record every verifier result, including `error` and `failed` ones, so that re-running a
verifier until it says yes isn't possible unseen; a recorded failure is final for its submission
(`verify_submission`). If settling fails (`ModelCallIncomplete`, stage `settle`) the paid-for reply
is not used and the reservation stays open until the reconciler (R1.14a) releases it; the gate
returns `error`.
"""

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from nuramesh.core.budgets import BudgetExceededError, CallContext
from nuramesh.core.calibration import (
    CalibrationConfig,
    gate_standing_reasons,
    not_calibrated_message,
)
from nuramesh.core.canonical import thaw
from nuramesh.core.content import MarkedLedgerHasher
from nuramesh.core.gate_config import VerifierGateConfig, verifier_gate_problems
from nuramesh.core.gates import GateKind, GateResult, GateSpec, Verdict
from nuramesh.core.models import ModelProvider, ModelRequest, parse_model_ref
from nuramesh.core.money import Money
from nuramesh.core.org import OrgGraph
from nuramesh.core.prices import PriceBook, PriceOverride, UnknownPriceError
from nuramesh.core.tasks import Task, TaskStatus
from nuramesh.core.verifier import CanonDocument, VerifierInput, build_messages, parse_reply
from nuramesh.engine.calibration import CalibrationLedgerError, load_calibration_records
from nuramesh.engine.call_signal import CallControl
from nuramesh.engine.gate_runners import GateError, error_result, judged_result
from nuramesh.engine.governance import GateRef, Governance, ModelBlocked
from nuramesh.engine.models import (
    ModelCallError,
    ModelCallIncomplete,
    PolicyDenied,
    call_model,
)
from nuramesh.engine.org_halt import HaltWatch, RolePaused
from nuramesh.stores.base import NotFoundError, Store

OUTPUT_CAPPED_MESSAGE = (
    "output_capped: the verifier's answer was cut by an output cap, so it could not be judged"
)

_UNAVAILABLE = (
    BudgetExceededError,
    UnknownPriceError,
    PolicyDenied,
    ModelCallError,
    ModelCallIncomplete,
    RolePaused,
)


def _load(store: Store, task_id: str, gate_id: str) -> tuple[Task, GateSpec]:
    with store.transaction() as uow:
        stored = uow.get_task(task_id)
    if stored is None:
        raise NotFoundError(f"task '{task_id}' doesn't exist")
    try:
        return stored.task, stored.task.definition_of_done.gate(gate_id)
    except KeyError:
        raise GateError(f"the task has no gate '{gate_id}'") from None


def run_verifier_gate(
    store: Store,
    provider: ModelProvider,
    task_id: str,
    gate_id: str,
    output: Any,
    *,
    org: OrgGraph,
    prices: PriceBook,
    reservation_id: str,
    actor: str,
    authorize: Callable[[CallContext, ModelRequest], None],
    authorize_spend: Callable[[CallContext, Money], None] | None = None,
    canon: Sequence[CanonDocument] = (),
    overrides: tuple[PriceOverride, ...] = (),
    now: Callable[[], datetime] | None = None,
    clock: Callable[[], float] = time.monotonic,
    governance: Governance | None = None,
    watch: HaltWatch | None = None,
    hasher: MarkedLedgerHasher,
    calibration: CalibrationConfig | None = None,
    shown: Callable[[Task], Task] | None = None,
    control: CallControl | None = None,
) -> GateResult:
    """Verify `output` for a task with the gate's verifier agent (module docstring).

    The task and gate are read from the store, never taken from the caller, so the worker's
    identity, the instructions and the round can't be misstated. Each run needs a fresh
    `reservation_id` (a reused one is refused by the budget service).
    """
    now = now or store.now  # the store's clock (ND243)
    task, spec = _load(store, task_id, gate_id)
    if shown is not None:  # an API task: the verifier sees the real text, read from the store
        task = shown(task)
    if spec.kind is not GateKind.VERIFIER:
        raise GateError(f"gate '{spec.id}' is a {spec.kind} gate, not a verifier gate")
    if task.status is not TaskStatus.VERIFYING:
        raise GateError(f"task '{task.id}' is '{task.status}', not verifying")
    try:
        config = VerifierGateConfig.model_validate(thaw(spec.config))
    except ValidationError:
        raise GateError(f"gate '{spec.id}' has an invalid configuration") from None
    problems = verifier_gate_problems(spec, org, assignee=task.assignee)
    if problems:
        raise GateError("; ".join(problems))
    checked_by = f"agent:{config.verifier}"

    def failed(message: str) -> GateResult:
        return error_result(
            spec,
            task_id=task.id,
            attempt=task.attempt,
            submission=task.submission,
            checked_by=checked_by,
            message=message,
        )

    wanted = {c for c in spec.checks}
    criteria = tuple(c for c in task.definition_of_done.criteria if c.id in wanted)
    model_ref = (
        governance.effective_ref(config.verifier)
        if governance is not None
        else org.model_for(config.verifier)
    )
    assert model_ref is not None  # verifier_gate_problems checked
    if config.calibration_suites:  # opt-in (R2.08): checked BEFORE the prompt, never skipped
        try:
            records = load_calibration_records(store, org.id)
        except CalibrationLedgerError as exc:
            # rule 21: `<suite id>: <reason>` for every listed suite
            return failed(
                not_calibrated_message([f"{sid}: {exc}" for sid in config.calibration_suites])
            )
        reasons = gate_standing_reasons(
            calibration,
            config.calibration_suites,
            notes=config.notes,
            max_output_tokens=config.max_output_tokens,
            criteria=criteria,
            records=records,
            verifier=config.verifier,
            model=model_ref,
            now=now(),
            role_max_tokens=org.role_of(config.verifier).max_tokens,
        )
        if reasons:  # zero model calls, zero reservations; the task goes to a human
            return failed(not_calibrated_message(reasons))
    try:
        data = VerifierInput(
            task_id=task.id,
            task_title=task.title,
            task_instructions=task.instructions,
            criteria=criteria,
            gate_notes=config.notes,
            output=output,
            canon=tuple(canon),
        )
        messages = build_messages(data)
    except (TypeError, ValueError):
        return failed("the output could not be shown to the verifier")
    provider_id, endpoint_id, model = parse_model_ref(model_ref)
    request = ModelRequest(
        provider=provider_id,
        endpoint_id=endpoint_id,
        model=model,
        messages=messages,
        max_output_tokens=config.max_output_tokens,
    )
    try:
        result = call_model(
            store,
            provider,
            request,
            context=CallContext(run_id=task.run_id, task_id=task.id, agent_id=config.verifier),
            org=org,
            prices=prices,
            reservation_id=reservation_id,
            actor=actor,
            authorize=authorize,
            authorize_spend=authorize_spend,
            overrides=overrides,
            now=now,
            clock=clock,
            governance=governance,
            gate=GateRef(spec.id, task.assignee or ""),
            watch=watch,
            hasher=hasher,
            control=control,
        )
    except ModelCallIncomplete as exc:
        if exc.stage in ("settle", "late_unrecorded"):  # spend not settled or not on record
            return failed(f"the verifier could not run ({type(exc).__name__}, {exc.stage})")
        # "record" or "late": the money is accounted for (late: added to spent, ND23); use the reply
        response = exc.response
        if exc.cap_hit:
            return failed(OUTPUT_CAPPED_MESSAGE)
    except ModelBlocked as exc:  # governance refused the verifier's model (spec section 7.1)
        return failed(f"model blocked [{exc.reason}]: {exc.detail}")
    except _UNAVAILABLE as exc:
        return failed(f"the verifier could not run ({type(exc).__name__})")
    else:
        response = result.response
        if result.cap_hit:  # an applied output cap cut the answer: never a pass (R1.42, Q5)
            return failed(OUTPUT_CAPPED_MESSAGE)
    if control is not None and (response.finish_reason == "filtered" or response.text == ""):
        # spec/real-models R28 (the runner): a tool-call or empty answer is never a pass
        return failed("the verifier gave no usable answer (model_no_usable_answer)")
    judgement = parse_reply(response.text, spec.checks)
    if not judgement.usable:
        return failed("the verifier's reply could not be used: " + "; ".join(judgement.problems))
    return judged_result(
        spec,
        task_id=task.id,
        attempt=task.attempt,
        submission=task.submission,
        checked_by=checked_by,
        findings={c: (v is Verdict.MET, why) for c, (v, why) in judgement.findings.items()},
    )
