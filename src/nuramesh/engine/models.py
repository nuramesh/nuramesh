"""The model client: one metered, budgeted, recorded path for every model call (PLAN I.3, T18).

`call_model` does, in this order:

1. **authorize**: the caller's policy check (`authorize(context, request)`, raises `PolicyDenied`;
   `engine.policy.ModelPolicy` is the standard one: default deny, approvals). After pricing, the
   optional `authorize_spend(context, worst_case)` checks spend rules. Nothing is spent before both
   pass. (A refusal by `authorize_spend` leaves a `price.override_used` entry if an override priced
   the call: each attempt is recorded.)
2. **price**: resolve the model's price (A3). No price and no override: the call is **blocked**
   (`model.blocked` recorded, `UnknownPriceError` raised). A call priced by an owner override
   records `price.override_used` (who set it, when, the rates).
3. **reserve**: hold the worst case (`worst_case_cost` with the prompt-size upper bound and
   `max_output_tokens`) in every applicable budget (`reserve_for_call`). A hard stop is recorded
   by the budget service and raised before the model is called.
4. **call** the provider, timing it. If it fails (any exception, including cancellation), the
   reservation is released and `model.called` is recorded with `outcome: error` (the exception's
   class name only; messages can quote content, so the provider's exception is not chained).
   Nothing is charged. An `Exception` becomes `ModelCallError`; a `BaseException` (cancellation,
   Ctrl-C) is re-raised after the cleanup. If the cleanup itself fails, `ModelCallError` says the
   reservation may still be open and lists what failed.
5. **settle**: charge the cost computed from the provider's reported tokens. If a used token kind
   has no price the cost is recorded as unknown and the reservation is charged (T12). A cost above
   the reservation is an overrun, recorded by the budget service.
6. **record** `model.called`: tokens, cost, basis, latency, model, finish reason and the content
   **hashes** of prompt and response. Never the text (A7).

The settlement is committed before `model.called`, because money is what must never be wrong: if the
process dies in between, the budgets are right and `budget.settled` already carries the model and
tokens. (The provider call itself can't be part of a database transaction.)

Known windows, honestly: if the process dies after the reservation and before the answer is
settled, the reservation stays open and holds budget until the reconciler
(`engine.reconcile`, R1.14a) closes it; that over-blocks, it never loses or double-charges
money. Cancellation is handled during the provider call; a `BaseException` raised while
settling is not intercepted (the same reconciler covers it). A reconciler must key off
`budget.settled` and the reservation's status, not off `model.called` (an `outcome:
settle_failed` entry is best-effort and says nothing about money). If settling or recording fails
after the provider answered, `ModelCallIncomplete` carries the response (the caller already paid
for it) and the stage that failed; a best-effort `model.called` with `outcome: settle_failed` is
written when the store allows it.

Providers that report tokens the request can't explain (zero tokens for a real prompt, more input
than the prompt bound, more output than `max_output_tokens`) are charged what they report and
flagged in `model.called` under `anomalies`.
"""

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from nuramesh.core.budgets import CallContext, ReservationStatus, org_key
from nuramesh.core.content import LedgerHashKind, MarkedLedgerHasher
from nuramesh.core.ledger import CostBasis, EventType
from nuramesh.core.models import (
    CacheSupport,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    cache_support_of,
)
from nuramesh.core.money import Money
from nuramesh.core.org import OrgGraph
from nuramesh.core.output_caps import OutputCap, configured_size
from nuramesh.core.prices import (
    PriceBasis,
    PriceBook,
    PriceOverride,
    ResolvedPrice,
    TokenUsage,
    UnknownPriceError,
    cost_of,
    resolve_price,
    worst_case_cost,
)
from nuramesh.engine.budgets import (
    apply_adjustment,
    output_cap_payload,
    release_reservation,
    reserve_for_call,
    reserve_for_call_capped,
    settle_reservation,
)
from nuramesh.engine.call_signal import CallCancelled, CallControl
from nuramesh.engine.content import ContentError
from nuramesh.engine.credentials import CredentialUnavailable
from nuramesh.engine.governance import GateRef, Governance, Governed, ProviderSource
from nuramesh.engine.org_halt import HaltWatch, OrgHalted, RolePaused
from nuramesh.engine.prompt_cache import decide_cache
from nuramesh.stores.base import EventDraft, Store, run_stream

_LOG = logging.getLogger(__name__)


class PolicyDenied(Exception):
    """The caller's policy check refused the call; nothing was spent."""


class ModelCallError(RuntimeError):
    """The provider failed. The reservation was released and nothing was charged.

    If the cleanup failed too, `cleanup_errors` names what (the reservation may still be open).

    On the durable runner a call that failed after its request was sent is not released: it is
    settled at the reserved amount as unknown (`charged` is then True; real-models design R20.8).
    """

    def __init__(
        self,
        message: str,
        cleanup_errors: tuple[str, ...] = (),
        *,
        code: str | None = None,
        charged: bool = False,
    ) -> None:
        self.cleanup_errors = cleanup_errors
        self.code = code  # the fixed refusal code of a refused stored key; never text or a value
        self.charged = charged  # the reservation was charged as unknown (a sent call that failed)
        super().__init__(message)


class ModelCallIncomplete(RuntimeError):
    """The provider answered but settling or recording failed. `response` is what was paid for.

    `stage` is `settle` (the reservation is still open and nothing was charged), `record` (the
    budgets are right; only the `model.called` entry is missing), `late` (the reconciler closed
    the reservation first; the late result is recorded and any uncharged cost was added to the
    budgets' spent, `budget.adjusted`, ND23) or `late_unrecorded` (the same, but writing the late
    result failed even after retries: the cost is in **neither** the ledger nor the balances, so
    the caller must treat it as an incident for a human, never as success).
    """

    def __init__(
        self,
        stage: str,
        response: ModelResponse,
        cause: Exception,
        *,
        effective_max_tokens: int | None = None,
        cap_hit: bool = False,
    ) -> None:
        self.stage = stage
        self.response = response
        self.effective_max_tokens = effective_max_tokens
        """The output size the provider was given (R1.42)."""
        self.cap_hit = cap_hit
        """The provider stopped at an output cap that was applied (R1.42)."""
        super().__init__(f"the model answered but the {stage} step failed: {type(cause).__name__}")


@dataclass(frozen=True)
class ModelResult:
    """A completed call: the response, what it cost (None if unknown) and how that is known."""

    response: ModelResponse
    cost: Money | None
    cost_basis: CostBasis
    reservation_id: str
    latency_ms: int
    effective_max_tokens: int = 0
    """The output size the provider was given (0 only for a result built by hand)."""
    cap_hit: bool = False
    """The provider stopped at an output cap that was applied (R1.42): the answer may be cut."""


def _usage_payload(usage: TokenUsage | None) -> dict[str, int] | None:
    """The tokens for a ledger payload; None (`usage: null`) when the service gave none we can
    trust (OpenAI-compatible design 7.4): the cost is then unknown, never zero."""
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


def _record(
    store: Store,
    type_: EventType,
    context: CallContext,
    actor: str,
    payload: dict[str, Any],
    *,
    cost: Money | None = None,
    cost_basis: CostBasis | None = None,
    then: dict[str, Any] | None = None,
) -> None:
    """Append one entry; `then` is a `model.output_capped` (`hit`) payload appended right after,
    in the same transaction, so `model.called.cap_hit` and the hit entry cannot disagree."""
    with store.transaction() as uow:
        uow.append(
            EventDraft(
                stream=run_stream(context.run_id),
                type=type_,
                actor=actor,
                run_id=context.run_id,
                task_id=context.task_id,
                payload=payload,
                cost=cost,
                cost_basis=cost_basis,
            )
        )
        if then is not None:
            uow.append(_hit_draft(context, actor, then))


def _hit_draft(context: CallContext, actor: str, payload: dict[str, Any]) -> EventDraft:
    return EventDraft(
        stream=run_stream(context.run_id),
        type=EventType.MODEL_OUTPUT_CAPPED,
        actor=actor,
        run_id=context.run_id,
        task_id=context.task_id,
        payload=payload,
    )


def _hit_payload(
    cap: OutputCap,
    request: ModelRequest,
    price: ResolvedPrice,
    reservation_id: str,
    agent: str,
    role: str,
    response: ModelResponse,
    asked: int,
) -> dict[str, Any]:
    """The `model.output_capped` (`hit`) payload: the provider stopped at the effective cap."""
    assert cap.binding is not None
    return output_cap_payload(
        "hit",
        reservation_id=reservation_id,
        agent=agent,
        role=role,
        request=request,
        price=price,
        requested=asked,
        effective=cap.effective,
        role_cap=cap.role_cap,
        task_cap=cap.task_cap,
        budget_cap=cap.budget_cap,
        binding=cap.binding,
        limiting_budget_id=cap.limiting_budget_id,
        available=cap.available,
        extra={
            "output_tokens": None if response.usage is None else response.usage.output_tokens,
            "finish_reason": response.finish_reason,
        },
    )


def _role_of(org: OrgGraph, context: CallContext) -> str:
    """The calling agent's role now, written into every `model.called` entry (costs design)."""
    return org.agent(context.agent_id).role


def call_model(
    store: Store,
    provider: ModelProvider | ProviderSource,
    request: ModelRequest,
    *,
    context: CallContext,
    org: OrgGraph,
    prices: PriceBook,
    reservation_id: str,
    actor: str,
    authorize: Callable[[CallContext, ModelRequest], None],
    authorize_spend: Callable[[CallContext, Money], None] | None = None,
    overrides: tuple[PriceOverride, ...] = (),
    now: Callable[[], datetime] | None = None,
    clock: Callable[[], float] = time.monotonic,
    governance: Governance | None = None,
    gate: GateRef | None = None,
    watch: HaltWatch | None = None,
    hasher: MarkedLedgerHasher,
    control: CallControl | None = None,
) -> ModelResult:
    """Make one model call through authorization, pricing, budgets and the ledger (see module).

    `control` is given by the durable runner only (real-models design R20.8, R27; the CLI path
    passes none and keeps the rules above). With it: the provider call can be cut by a stop
    (`CallCancelled`: released when the request was not sent, else settled at the reserved amount
    as unknown with `model.called` `outcome: cancelled`); and a call that fails after its request
    was sent is settled at the reserved amount as unknown instead of released (a provider may
    bill a request it received). A failure before the request was sent, a refused credential and
    every non-`Exception` (an interrupt, a shutdown) release as before.

    A halted organization (E19) starts no call: `model.blocked` (`org_halted`, with the caller's
    `role`) is recorded, nothing is reserved and `OrgHalted` is raised. The same for a paused role
    (`role_paused`, `RolePaused`). `watch` answers both questions from one read of the org stream
    (the run engine passes its incremental watch); by default the stream is read. If it can't be
    read, the call is refused too, as `org_halted` (fail closed). Order: halt, role pause, then
    (L5, ND92) the model resolver, then policy, price and budget.

    With `governance` the call is checked by the model resolver (`engine.governance`); `provider`
    may then be a `ProviderSource` that builds the provider from the resolved endpoint. `gate` marks
    a verifier gate's own call: it is compared with the models recorded for its task, and its
    `model.called` entries carry the gate id.

    `hasher` (required, bound to the organization) makes the keyed prompt and response hashes
    (PH-KEY): the prompt hash after halt, pause, governance, policy and price checks and before
    the reservation (a key refusal reserves and spends nothing), the response hash once, right
    after the provider answered and before settling (if it can't be made the money path goes
    ahead with `response_hash: null` and a `hash_status`). Every `model.called` row carries the
    marker (`hash_scheme`, `hash_key_id`, `hash_org`)."""
    now = now or store.now  # the store's clock (ND243)
    role = _role_of(org, context)  # an agent outside the org is a caller bug: refused up front
    _refuse_if_halted_or_paused(store, org.id, context, actor, now(), watch, role)
    governed = (
        governance.check_call(
            context,
            request,
            role=role,
            actor=actor,
            gate=gate,
            plain_provider=not isinstance(provider, ProviderSource),
        )
        if governance is not None
        else None
    )
    extra: dict[str, Any] = {}
    if governed is not None:
        extra.update(governed.called_extra())
    if gate is not None:
        extra["gate"] = gate.gate
    authorize(context, request)
    at = now()
    price = _resolve(store, request, context, actor, prices, overrides, at, role, governed)
    role_cap = org.role_of(context.agent_id).max_tokens
    task_cap = context.task_max_tokens
    capped = (role_cap is not None or task_cap is not None) and not context.cap_exempt
    asked = request.max_output_tokens
    # authorize_spend sees the configured-capped worst case (never below the reserved amount); an
    # exempt call (calibration, eval) sees the full amount, as today (cap design section 4).
    sized = configured_size(asked, role_cap, task_cap) if capped else asked
    worst = worst_case_cost(request.prompt_token_bound, sized, price)
    if authorize_spend is not None:
        authorize_spend(context, worst)
    decision = decide_cache(
        store,
        org.id,
        request,
        is_gate=gate is not None,
        support=_cache_support(provider, request, governed) if request.cache_prefix else "none",
        price=price,
    )
    prompt_hash = hasher(LedgerHashKind.MODEL_PROMPT, request.prompt_bytes)
    prefix_hash: str | None = None
    if decision is not None and decision.applied:
        # the same key as the prompt hash (`use` never creates one and refuses a replaced key), and
        # before anything is reserved: a refusal here reserves and spends nothing (PH-KEY K6)
        assert request.prefix_bytes is not None
        prefix_hash = hasher.use(LedgerHashKind.MODEL_PREFIX, request.prefix_bytes)
    marker = hasher.marker()
    if decision is not None:
        extra["cache"] = {"status": decision.status, "support": decision.support}
        if not decision.applied and request.cache_prefix:
            request = request.model_copy(update={"cache_prefix": False})  # the provider is told no
    reserve_details = {
        "provider": request.provider,
        **_endpoint(request),
        "model": request.model,
        "price": price.ledger_payload(),
    }
    cap: OutputCap | None = None
    if role_cap is None and task_cap is None:  # no cap configured: exactly the uncapped call
        reserve_for_call(
            store,
            reservation_id=reservation_id,
            context=context,
            org=org,
            amount=worst,
            actor=actor,
            details=reserve_details,
        )
    else:
        cap = reserve_for_call_capped(
            store,
            reservation_id=reservation_id,
            context=context,
            org=org,
            request=request,
            price=price,
            role_cap=role_cap,
            task_cap=task_cap,
            cap_exempt=context.cap_exempt,
            actor=actor,
            details=reserve_details,
        ).cap
    applied = cap if cap is not None and cap.lowered else None
    effective = applied.effective if applied is not None else asked
    if applied is not None:
        assert applied.binding is not None
        extra["max_output_tokens"] = effective
        extra["output_cap_binding"] = applied.binding.value
        request = request.model_copy(update={"max_output_tokens": effective})
    started = clock()
    late = _LateAnswers()
    cut: bool | None = None
    charged = False

    def finish(response: ModelResponse) -> ModelResult:
        """Steps after the answer: settle, then record (also the late path of an abandoned call:
        its settlement finds the reservation closed and records `settled_late`)."""
        response = _trusted_usage(response)
        own = dict(extra)  # the call's thread and the step's thread never share a dict
        latency = _ms(clock() - started)
        cap_hit = applied is not None and response.finish_reason == "length"
        if cap_hit:
            own["cap_hit"] = True
        hit_event = (
            _hit_payload(
                applied, request, price, reservation_id, context.agent_id, role, response, asked
            )
            if cap_hit and applied is not None
            else None
        )
        hashes = _response_hashes(hasher, response, prompt_hash, marker)
        if prefix_hash is not None and decision is not None:
            # on `ok` and `settled_late` rows only: the full object, with the keyed hash of
            # the prefix
            hashes["cache"] = {
                **own["cache"],
                "prefix_hash": prefix_hash,
                "prefix_messages": (request.cache_breakpoint or 0) + 1,
            }
        cost: Money | None = None  # these stay unset if pricing the answer itself fails
        basis = CostBasis.UNKNOWN
        _note_attempts(own, active)
        try:
            cost, basis = _cost(response.usage, price)
            anomalies = _anomalies(request, response.usage)
            settle_reservation(
                store,
                reservation_id,
                actual=cost,
                cost_basis=basis,
                actor=actor,
                details={
                    "provider": request.provider,
                    **_endpoint(request),
                    "model": request.model,
                    "usage": _usage_payload(response.usage),
                    "price_basis": price.basis.value,
                },
            )
        except Exception as exc:
            late = _record_late(
                store,
                context,
                actor,
                reservation_id,
                request,
                response,
                latency,
                cost,
                basis,
                role,
                own,
                hashes,
                hit_event,
            )
            if not late:
                _best_effort(
                    store,
                    context,
                    actor,
                    {
                        "reservation": reservation_id,
                        "agent": context.agent_id,
                        "role": role,
                        "provider": request.provider,
                        **_endpoint(request),
                        "model": request.model,
                        "outcome": "settle_failed",
                        "usage": _usage_payload(response.usage),
                        "latency_ms": latency,
                        **hashes,
                        **own,
                    },
                    hit_event,
                )
            raise ModelCallIncomplete(
                late or "settle", response, exc, effective_max_tokens=effective, cap_hit=cap_hit
            ) from None
        payload: dict[str, Any] = {
            "reservation": reservation_id,
            "agent": context.agent_id,
            "role": role,
            "provider": request.provider,
            **_endpoint(request),
            "model": request.model,
            "outcome": "ok",
            "usage": _usage_payload(response.usage),
            "finish_reason": response.finish_reason,
            "latency_ms": latency,
            **hashes,
            "price": price.ledger_payload(),
            **own,
        }
        if "cache" in hashes:
            payload["cache"] = hashes["cache"]
        if anomalies:
            payload["anomalies"] = anomalies
        try:
            _record(
                store,
                EventType.MODEL_CALLED,
                context,
                actor,
                payload,
                cost=cost,
                cost_basis=basis,
                then=hit_event,
            )
        except Exception as exc:
            raise ModelCallIncomplete(
                "record", response, exc, effective_max_tokens=effective, cap_hit=cap_hit
            ) from None
        return ModelResult(
            response=response,
            cost=cost,
            cost_basis=basis,
            reservation_id=reservation_id,
            latency_ms=latency,
            effective_max_tokens=effective,
            cap_hit=cap_hit,
        )

    late.finish = finish
    try:
        failure: str | None = None
        errors: tuple[str, ...] = ()
        response: ModelResponse | None = None
        active: ModelProvider | None = None
        try:
            if governed is not None and governance is not None:
                governance.record_use(context, governed)  # the use, immediately before the call
                active = (
                    governance.provider_for(governed, provider)
                    if isinstance(provider, ProviderSource)
                    else provider
                )
            else:
                active = provider  # type: ignore[assignment]
            assert active is not None
            if control is None:
                response = active.complete(request)
            else:
                calling, sending = active, request
                response = control.run(lambda: calling.complete(sending), late=late.append)
            if not isinstance(response, ModelResponse):
                raise TypeError("the provider returned something that isn't a ModelResponse")
        except CallCancelled as stop:  # before the general handler: a sent call is never released
            cut = stop.sent
        except BaseException as exc:
            _note_attempts(extra, active)
            refused = exc.code if isinstance(exc, CredentialUnavailable) else None
            failure = type(exc).__name__ if refused is None else f"{type(exc).__name__}: {refused}"
            charged = (
                control is not None
                and control.signal.sent
                and refused is None
                and isinstance(exc, Exception)
            )
            noted = {**extra, "prompt_hash": prompt_hash, **marker}
            took = _ms(clock() - started)
            if charged:  # R20.8 (owner, OQ10 A): sent, so settled at the reservation as unknown
                errors = _charge_unknown(
                    store,
                    request,
                    context,
                    actor,
                    reservation_id,
                    failure,
                    took,
                    role,
                    noted,
                    price,
                )
            else:
                errors = _abort(
                    store, request, context, actor, reservation_id, failure, took, role, noted,
                    record_call=refused is None,
                )  # fmt: skip
            if not isinstance(exc, Exception):
                raise
        if cut is not None:
            # R27: (b) a sent call is settled at its reservation, synchronously, before the step
            # returns; (a) a call that was not sent is released and is no call at all
            _note_attempts(extra, active)
            noted = {**extra, "prompt_hash": prompt_hash, **marker}
            took = _ms(clock() - started)
            if cut:
                errors = _charge_unknown(
                    store, request, context, actor, reservation_id, None, took, role, noted, price
                )
            else:
                errors = _abort(
                    store, request, context, actor, reservation_id, "stopped before it was sent",
                    took, role, noted, record_call=False,
                )  # fmt: skip
            if errors:
                _LOG.warning("a stopped call left its reservation open: %s", ", ".join(errors))
            raise CallCancelled(cut)
        if failure is not None or response is None:
            # Raised outside the `except` block so the provider's exception (whose message can quote
            # content) is not reachable through `__context__` either.
            raise ModelCallError(
                f"{request.model_ref} failed: {failure}"
                + (" (charged at the reservation: the request was sent)" if charged else "")
                + (" (cleanup failed; the reservation may still be open)" if errors else ""),
                errors,
                code=refused,
                charged=charged,
            )
        return finish(response)
    finally:
        late.settled.set()  # on every path out: a late answer settles after the cut


class _LateAnswers:
    """Where an abandoned call's answer goes (R27): through `finish`, whose settlement finds the
    reservation already closed and writes the `settled_late` row (and `budget.adjusted` when the
    cost is above what was charged). Never a result, never a gate input."""

    def __init__(self) -> None:
        self.finish: Callable[[ModelResponse], object] | None = None
        self.settled = threading.Event()
        """Set once the step's own settlement (the cut, or the abandon on an interrupt) is done."""

    def append(self, response: ModelResponse) -> None:
        if self.finish is None or not isinstance(response, ModelResponse):
            return
        # R27(b): the cut settlement comes first, so this one finds the reservation closed. The
        # wait has no bound: a late answer that settled first would write an `ok` row, which R27
        # forbids (review m3 of #1068). The step sets `settled` on every path out of the call, and
        # this is the abandoned call's own daemon thread, so waiting holds nothing else.
        self.settled.wait()
        try:
            self.finish(response)
        except ModelCallIncomplete:
            pass  # `late`: recorded; anything else was recorded as far as the store allowed


def _record_late(
    store: Store,
    context: CallContext,
    actor: str,
    reservation_id: str,
    request: ModelRequest,
    response: ModelResponse,
    latency: int,
    cost: Money | None,
    basis: CostBasis,
    role: str,
    extra: Mapping[str, Any],
    hashes: Mapping[str, Any],
    hit_event: dict[str, Any] | None = None,
    attempts: int = 3,
) -> str | None:
    """If settling failed because the reservation was already closed (the reconciler got there
    first), record the late result and any cost that was not charged, in ONE transaction
    (retried up to `attempts` times). Returns `late` once written, `late_unrecorded` if every try
    failed, and None if the reservation is still open or can't be read (the ordinary
    settle-failure path). Once the reservation is seen closed the caller must never treat it as
    open again.
    """
    try:
        stored = store.get_reservation(reservation_id)
    except Exception:
        return None
    if stored is None or stored.reservation.status is ReservationStatus.OPEN:
        return None
    reservation = stored.reservation
    charged = reservation.charged.amount if reservation.charged is not None else Decimal(0)
    released = reservation.status is ReservationStatus.RELEASED
    # What the budgets are still missing (ND23): the real cost beyond what was charged; an unknown
    # cost after a release is charged at the reservation, an upper bound (T12).
    if cost is None:
        missing = reservation.amount if released else None
        late_basis = CostBasis.UNKNOWN
    else:
        missing = cost - Money.of(charged, cost.currency) if cost.amount > charged else None
        late_basis = basis
    for _ in range(max(1, attempts)):
        try:
            with store.transaction() as uow:
                uow.append(
                    EventDraft(
                        stream=run_stream(context.run_id),
                        type=EventType.MODEL_CALLED,
                        actor=actor,
                        run_id=context.run_id,
                        task_id=context.task_id,
                        payload={
                            "reservation": reservation_id,
                            "agent": context.agent_id,
                            "role": role,
                            "provider": request.provider,
                            **_endpoint(request),
                            "model": request.model,
                            **extra,
                            "outcome": "settled_late",
                            "reservation_status": reservation.status.value,
                            "charged": charged,
                            "usage": _usage_payload(response.usage),
                            "latency_ms": latency,
                            **hashes,
                        },
                        cost=cost,
                        cost_basis=basis,
                    )
                )
                if hit_event is not None:
                    uow.append(_hit_draft(context, actor, hit_event))
                if missing is not None and missing.amount > 0:
                    apply_adjustment(
                        uow,
                        context=context,
                        org_id=org_key(reservation.org_id),
                        budget_ids=reservation.budget_ids,
                        amount=missing,
                        reason="overrun_after_reconciliation",
                        actor=actor,
                        reservation_id=reservation_id,
                        cost_basis=late_basis,
                        details={"charged": str(charged)},
                        budget_owners={o.budget_id: o.org_id for o in reservation.budget_orgs},
                    )
        except Exception:  # nothing partial: both entries or neither; try again
            continue
        return "late"
    return "late_unrecorded"


def _cache_support(provider: Any, request: ModelRequest, governed: Governed | None) -> CacheSupport:
    """What the provider declares about caching, asked BEFORE reserving and without building a
    provider (savings design 5.3). A source or router without the method is `none`."""
    ask = getattr(provider, "cache_support_for", None)
    if ask is None:
        return "none" if isinstance(provider, ProviderSource) else cache_support_of(provider)
    if isinstance(provider, ProviderSource):
        return ask(governed.resolution) if governed is not None else "none"
    return ask(request)  # type: ignore[no-any-return]


def _response_hashes(
    hasher: MarkedLedgerHasher, response: ModelResponse, prompt_hash: str, marker: dict[str, str]
) -> dict[str, Any]:
    """The hash fields of an answered call, computed ONCE and reused by every record site. The
    response hash never blocks settlement: a key error gives `hash_status: key_unavailable`, any
    other failure `hash_failed`, and `response_hash` is null (never a plain value)."""
    fields: dict[str, Any] = {"prompt_hash": prompt_hash, **marker}
    try:
        fields["response_hash"] = hasher.use(LedgerHashKind.MODEL_RESPONSE, response.response_bytes)
    except ContentError:
        fields.update(response_hash=None, hash_status="key_unavailable")
    except Exception:
        fields.update(response_hash=None, hash_status="hash_failed")
    return fields


def _anomalies(request: ModelRequest, usage: TokenUsage | None) -> list[str]:
    found: list[str] = []
    if usage is None:
        return found  # no usage: nothing to compare, and not a zero-usage anomaly (7.4)
    prompt_side = usage.input_tokens + usage.cached_input_tokens + usage.cache_write_tokens
    if prompt_side == 0 and usage.output_tokens == 0:
        found.append("zero_usage")
    if prompt_side > request.prompt_token_bound:
        found.append("input_over_prompt_bound")
    if usage.output_tokens > request.max_output_tokens:  # the effective size, once capped
        found.append("output_over_max")
    return found


def _best_effort(
    store: Store,
    context: CallContext,
    actor: str,
    payload: dict[str, Any],
    hit_event: dict[str, Any] | None = None,
) -> None:
    try:
        _record(store, EventType.MODEL_CALLED, context, actor, payload, then=hit_event)
    except Exception:
        pass  # the ledger is the thing that failed; the caller is told through the raised error


def _abort(
    store: Store,
    request: ModelRequest,
    context: CallContext,
    actor: str,
    reservation_id: str,
    error: str,
    latency: int,
    role: str,
    extra: Mapping[str, Any],
    *,
    record_call: bool = True,
) -> tuple[str, ...]:
    """Release the hold and record the failed call; returns what failed (empty if all went well).

    A refused stored key (`record_call` False, VK22) is no call: the release (whose reason carries
    the fixed code) is the only entry, there is no `model.called`."""
    failed: list[str] = []
    try:
        release_reservation(store, reservation_id, actor=actor, reason=f"the call failed ({error})")
    except Exception as exc:
        failed.append(f"release: {type(exc).__name__}")
    if not record_call:
        return tuple(failed)
    try:
        _record(
            store,
            EventType.MODEL_CALLED,
            context,
            actor,
            {
                "reservation": reservation_id,
                "agent": context.agent_id,
                "role": role,
                "provider": request.provider,
                **_endpoint(request),
                "model": request.model,
                **extra,
                "outcome": "error",
                "error": error,
                "latency_ms": latency,
                **({"reservation_open": True} if failed else {}),
            },
        )
    except Exception as exc:
        failed.append(f"record: {type(exc).__name__}")
    return tuple(failed)


def _charge_unknown(
    store: Store,
    request: ModelRequest,
    context: CallContext,
    actor: str,
    reservation_id: str,
    error: str | None,
    latency: int,
    role: str,
    extra: Mapping[str, Any],
    price: ResolvedPrice,
) -> tuple[str, ...]:
    """Settle a call whose request was sent and that gave no complete answer at the reserved
    amount, cost unknown, then record it (real-models design R20.8 and R27 b): `outcome: error`
    with the exception's class name for a failure, `outcome: cancelled` (`error` None) for a call
    a stop cut. Settled before the row is written, like every call. Returns what failed."""
    failed: list[str] = []
    outcome = "cancelled" if error is None else "error"
    try:
        settle_reservation(
            store,
            reservation_id,
            actual=None,
            cost_basis=CostBasis.UNKNOWN,
            actor=actor,
            details={
                "provider": request.provider,
                **_endpoint(request),
                "model": request.model,
                "usage": None,
                "price_basis": price.basis.value,
                "sent": True,
                "outcome": outcome,
            },
        )
    except Exception as exc:
        failed.append(f"settle: {type(exc).__name__}")
    try:
        _record(
            store,
            EventType.MODEL_CALLED,
            context,
            actor,
            {
                "reservation": reservation_id,
                "agent": context.agent_id,
                "role": role,
                "provider": request.provider,
                **_endpoint(request),
                "model": request.model,
                **extra,
                "outcome": outcome,
                **({} if error is None else {"error": error}),
                "sent": True,
                "usage": None,
                "latency_ms": latency,
                "price": price.ledger_payload(),
                **({"reservation_open": True} if failed else {}),
            },
            cost=None,
            cost_basis=None if failed else CostBasis.UNKNOWN,
        )
    except Exception as exc:
        failed.append(f"record: {type(exc).__name__}")
    return tuple(failed)


def _note_attempts(extra: dict[str, Any], provider: object) -> None:
    """Record the attempts a provider made for the call (`openai-compatible` retries a failure
    that certainly processed nothing): `{"n", "outcome"}` each, never a header, body or URL."""
    attempts = getattr(provider, "attempts", None)
    if isinstance(attempts, list) and attempts:
        extra["attempts"] = [dict(a) for a in attempts if isinstance(a, dict)]


def _endpoint(request: ModelRequest) -> dict[str, str]:
    """`endpoint_id` for a payload, only when the request has one (additive)."""
    return {} if request.endpoint_id is None else {"endpoint_id": request.endpoint_id}


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def _refuse_if_halted_or_paused(
    store: Store,
    org_id: str,
    context: CallContext,
    actor: str,
    at: datetime,
    watch: HaltWatch | None,
    role: str,
) -> None:
    refusal: Exception | None = None
    reason = "org_halted"
    try:
        halt, roles = (watch or HaltWatch(store, org_id)).snapshot()
        if halt.halted:
            refusal = OrgHalted(reason)
        elif (state := roles.get(role)) is not None and state.paused:
            reason, refusal = "role_paused", RolePaused(role)
    except Exception:  # unreadable: fail closed, the same entry as for a halt
        refusal = OrgHalted(reason)
    if refusal is None:
        return
    try:
        _record(
            store,
            EventType.MODEL_BLOCKED,
            context,
            actor,
            {"agent": context.agent_id, "role": role, "reason": reason, "at": at.isoformat()},
        )
    except Exception:  # best effort: the store may be what is down; the refusal stands
        _LOG.warning("could not record model.blocked (%s)", reason, exc_info=True)
    raise refusal


def record_unevaluated(
    store: Store, context: CallContext, actor: str, at: datetime, role: str
) -> None:
    """Record, best effort, that a call was refused because the breaker evaluation failed."""
    try:
        _record(
            store,
            EventType.MODEL_BLOCKED,
            context,
            actor,
            {
                "agent": context.agent_id,
                "role": role,
                "reason": "breaker_evaluation_failed",
                "at": at.isoformat(),
            },
        )
    except Exception:  # best effort
        _LOG.warning("could not record model.blocked (breaker_evaluation_failed)", exc_info=True)


def _resolve(
    store: Store,
    request: ModelRequest,
    context: CallContext,
    actor: str,
    prices: PriceBook,
    overrides: tuple[PriceOverride, ...],
    at: datetime,
    role: str,
    governed: Governed | None = None,
) -> ResolvedPrice:
    try:
        price = resolve_price(
            prices,
            request.provider,
            request.model,
            at,
            overrides=overrides,
            endpoint_id=request.endpoint_id,
        )
    except UnknownPriceError as exc:
        _record(
            store,
            EventType.MODEL_BLOCKED,
            context,
            actor,
            {
                "agent": context.agent_id,
                "provider": request.provider,
                **_endpoint(request),
                "model": request.model,
                "role": role,
                "reason": "unknown_price",
                "at": at.isoformat(),
                **({"stage": "call", "data_class": governed.data_class} if governed else {}),
            },
        )
        raise exc
    if price.basis is PriceBasis.PROVISIONAL:
        _record(
            store,
            EventType.PRICE_OVERRIDE_USED,
            context,
            actor,
            {"agent": context.agent_id, **price.ledger_payload()},
        )
    return price


def _trusted_usage(response: ModelResponse) -> ModelResponse:
    """The one rule for every provider (#1459): a served answer whose usage is missing or not a
    valid `TokenUsage` (negative, non-integer, over the bound: an adapter built it without
    checking) gets `usage=None`, so it is charged the whole reservation with an unknown cost
    (`budget.settled` `cost_unknown`), never zero and never released."""
    usage = response.usage
    if usage is None:
        return response
    try:
        TokenUsage.model_validate(
            {name: getattr(usage, name, None) for name in TokenUsage.model_fields},
            strict=True,
        )
    except Exception:
        return response.model_copy(update={"usage": None})
    return response


def _cost(usage: TokenUsage | None, price: ResolvedPrice) -> tuple[Money | None, CostBasis]:
    if usage is None:
        return None, CostBasis.UNKNOWN  # the reservation is charged as the upper bound (T12)
    try:
        return cost_of(usage, price), price.cost_basis
    except UnknownPriceError:
        return None, CostBasis.UNKNOWN
