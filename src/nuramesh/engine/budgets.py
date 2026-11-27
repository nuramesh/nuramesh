"""Budget service: reservations, settlements and hard stops, each in one transaction (A1).

- `define_budget` stores a fresh balance and records `budget.defined` in the org's stream (agent
  and team budgets span runs).
- `reserve_for_call` works out the applicable budgets itself, inside the transaction, from the org
  graph and every stored budget: callers can't choose or skip budgets. If all have room, it holds
  the amount and records `budget.reserved`. If any doesn't, **or none applies (default deny)**, it
  records `budget.exceeded` and commits that first, then raises `BudgetExceededError` (or
  `NoBudgetError`): a hard stop is never silent and never lost to a rollback.
- `settle` charges the actual cost and releases the rest (`budget.settled`). If the cost is
  unknown (decision T12), the reservation itself is charged, an upper bound, and the ledger
  records the cost as unknown. An overrun is recorded with `budget.exceeded`, and a budget that
  ends up over its limit is **blocked** (`budget.breached`, ND23).
- `release` returns a reservation that was never charged (`budget.released`).
- `adjust_spend` adds a late actual cost to *spent* (`budget.adjusted`, ND23/T24). A budget it
  leaves over its limit is breached: marked blocked, `budget.breached` recorded in the same
  transaction, and a human is required: `reserve_for_call` refuses a blocked budget (recorded hard
  stop) until `unblock_budget` records a human decision (`budget.unblocked`, with a reason, and a
  higher limit if the budget is still over it). Never silent: every step is a ledger event and the
  balance always agrees with the ledger.

**Windows (T56).** A budget with a `window` (`day` or `month`, UTC) counts spending inside the
current window only. `reserve_for_call` and `apply_adjustment` first **roll** every applicable
budget whose window has ended: the old window is closed and a fresh balance starts, recorded as
`budget.window_rolled` (closed window's final spent, the new window, whether a block cleared or
was held) in the same transaction, in the stream of the call that triggered it (even when that
call is then refused). A reservation belongs to the window it was made in: `settle_reservation` and
`release_reservation` charge or return it there, even after a roll (the closed window's final
numbers then show in the `budget.settled` payload). The time comes from the store's injected
clock (`uow.now()`).

Reservation events go to the run's stream; each names its budgets, so an audit of a budget that
spans runs reads every run stream (plus the org stream for its definition). Caller-supplied
`details` are nested under a `details` key and can't overwrite the recorded fields.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter

from nuramesh.core.budgets import (
    BreachMode,
    Budget,
    BudgetBalance,
    BudgetExceededError,
    BudgetScope,
    BudgetWindow,
    CallContext,
    Reservation,
    ReservationStatus,
    ReservedWindow,
    Shortfall,
    adjust,
    applicable_budgets,
    org_key,
    release,
    reserve,
    roll,
    settle,
    shortfalls,
    unblock,
    window_end,
)
from nuramesh.core.ledger import Actor, CostBasis, EventType, format_time
from nuramesh.core.models import ModelRequest
from nuramesh.core.money import Money
from nuramesh.core.org import OrgGraph
from nuramesh.core.output_caps import (
    CapBinding,
    NoRoom,
    OutputCap,
    configured_size,
    decide_cap,
)
from nuramesh.core.people import is_human_id
from nuramesh.core.prices import ResolvedPrice, worst_case_cost
from nuramesh.engine.content import ContentService
from nuramesh.stores.base import (
    ConcurrencyError,
    EventDraft,
    NotFoundError,
    Store,
    StoredBalance,
    UnitOfWork,
    run_stream,
)

_ACTOR = TypeAdapter(Actor)


class NoBudgetError(BudgetExceededError):
    """No budget applies to the call: refused (default deny), like any other hard stop."""

    def __init__(self, context: CallContext) -> None:
        self.shortfalls = ()
        Exception.__init__(
            self,
            f"no budget applies to agent '{context.agent_id}' in run '{context.run_id}'; "
            "define a run, task, agent or team budget first",
        )


def _check_actor(actor: str) -> None:
    _ACTOR.validate_python(actor)


def _details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    return {"details": dict(details)} if details else {}


def _shortfall_payload(items: Sequence[Shortfall]) -> list[dict[str, Any]]:
    return [
        {
            "budget": s.budget_id,
            "scope": s.scope.value,
            "scope_id": s.scope_id,
            "limit": s.limit.amount,
            "available": s.available.amount,
            "requested": s.requested.amount,
            "blocked": s.blocked,
            "spent": None if s.spent is None else s.spent.amount,
            "window": _window_text(s.window),
            "on_breach": s.on_breach.value,
            "held": s.held,
            **({} if s.window_start is None else {"window_start": format_time(s.window_start)}),
            **({} if s.resets_at is None else {"resets_at": format_time(s.resets_at)}),
        }
        for s in items
    ]


def _window_text(window: BudgetWindow) -> str | None:
    """`day`, `month`, or None for a lifetime budget (alert payloads use null, not 'none')."""
    return None if window is BudgetWindow.NONE else window.value


BreachItem = tuple[Budget, Money, datetime | None, bool]
"""A breached budget: the budget, the spent of the window that overspent, that window's start
(None for a lifetime budget) and whether that window had already ended (a closed window)."""


def _breach_item(
    budget: Budget, spent: Money, window_start: datetime | None, *, closed: bool
) -> dict[str, Any]:
    """What an alert on a breach needs: which budget, its scope and window, limit, spent (of the
    window that overspent), what happens next (`on_breach`, `held`) and, for a budget that resets
    and was overspent in its current window, when (`resets_at`)."""
    mode = budget.breach_mode
    item: dict[str, Any] = {
        "budget": budget.id,
        "scope": budget.scope.value,
        "scope_id": budget.scope_id,
        "window": _window_text(budget.window),
        "limit": budget.limit.amount,
        "spent": spent.amount,
        "over_by": (spent - budget.limit).amount,
        "on_breach": mode.value,
        "held": mode is BreachMode.HOLD,  # a hold budget is blocked after any breach
    }
    if window_start is not None:
        item["window_start"] = format_time(window_start)
        if mode is BreachMode.RESET and not closed:
            item["resets_at"] = format_time(window_end(budget.window, window_start))
    if closed:
        item["window_closed"] = True
    return item


def _breach_event(
    context: CallContext,
    actor: str,
    reason: str,
    items: Sequence[BreachItem],
    reservation_id: str | None,
) -> EventDraft:
    """`budget.breached`: these budgets went over their limits (ND23). A budget that holds, or
    whose current window overspent, is now blocked; a `reset` budget overspent in a window that had
    already ended blocks nothing (but is still flagged for a human). Each item carries the window
    that overspent and what an alert needs."""
    return EventDraft(
        stream=run_stream(context.run_id),
        type=EventType.BUDGET_BREACHED,
        actor=actor,
        run_id=context.run_id,
        task_id=context.task_id,
        payload={
            "reason": reason,
            "reservation": reservation_id,
            "agent": context.agent_id,
            "human_required": True,
            "budgets": [
                _breach_item(budget, spent, start, closed=closed)
                for budget, spent, start, closed in items
            ],
        },
    )


def _load(
    uow: UnitOfWork,
    org_id: str | None,
    budget_ids: Sequence[str],
    owners: Mapping[str, str | None] | None = None,
) -> list[StoredBalance]:
    """The balances of these budget ids of one organization (never another's, ND90); `owners`
    names the organization of a budget that is not `org_id`'s (a migrated closed reservation)."""
    found = []
    for budget_id in budget_ids:
        owner = org_id if owners is None or budget_id not in owners else owners[budget_id]
        stored = uow.get_balance(org_key(owner), budget_id)
        if stored is None:
            raise NotFoundError(f"budget '{budget_id}' isn't defined")
        found.append(stored)
    return found


def _save(uow: UnitOfWork, stored: Sequence[StoredBalance], new: Sequence[BudgetBalance]) -> None:
    for old, balance in zip(stored, new, strict=True):
        uow.put_balance(balance, expected_version=old.version)


def _roll_due(
    uow: UnitOfWork,
    stored: Sequence[StoredBalance],
    *,
    stream: str,
    run_id: str | None,
    task_id: str | None,
    agent: str | None,
    actor: str,
    reservation_id: str | None,
) -> list[StoredBalance]:
    """Roll every windowed budget whose window has ended; one `budget.window_rolled` each.

    Saved and recorded in the caller's transaction, in the given order. The new balance starts
    empty and unblocked; reservations still open in the closed window stay with it
    (`BudgetBalance.closed`). Nothing is edited or deleted: the closed window's final numbers are
    in the event. The window is decided from one clock reading (`uow.now()`) and the event
    records it, so the payload (not the event's `at`, which never goes back in its stream) is
    the authority on which window a call fell in.
    """
    now = uow.now()
    out: list[StoredBalance] = []
    for item in stored:
        rolled = roll(item.balance, now)
        if rolled is None:
            out.append(item)
            continue
        version = uow.put_balance(rolled.balance, expected_version=item.version)
        budget = item.balance.budget
        assert rolled.balance.window_start is not None
        uow.append(
            EventDraft(
                stream=stream,
                type=EventType.BUDGET_WINDOW_ROLLED,
                actor=actor,
                run_id=run_id,
                task_id=task_id,
                payload={
                    "budget": budget.id,
                    "window": budget.window.value,
                    "limit": budget.limit.amount,
                    "closed_window": {
                        "start": format_time(rolled.closed_start),
                        "end": format_time(rolled.closed_end),
                        "spent": rolled.spent.amount,
                        "reserved_open": rolled.reserved.amount,
                        "reservations_open": rolled.open,
                    },
                    "new_window": {
                        "start": format_time(rolled.balance.window_start),
                        "end": format_time(window_end(budget.window, rolled.balance.window_start)),
                    },
                    "cleared_block": rolled.cleared_block,
                    "held": rolled.held_block,
                    "on_breach": budget.breach_mode.value,
                    "reservation": reservation_id,
                    "agent": agent,
                },
            )
        )
        out.append(StoredBalance(balance=rolled.balance, version=version))
    return out


def define_budget(store: Store, budget: Budget, *, actor: str, org_id: str) -> BudgetBalance:
    """Store a new budget with nothing spent, and record it in the org's ledger stream.

    **Limits are fixed once defined:** defining the same budget id again raises
    `ConcurrencyError` and writes nothing, so there is no silent way to raise a limit. Changing a
    limit is human-only and ledgered: `unblock_budget` with a higher `new_limit` (ND23, T28).

    **Organizations (ND90):** a budget's key is (`org_id`, budget id), so another organization's
    budget with the same id is a different budget and defining it is accepted; nobody can squat
    an id. The budget is stamped with `org_id` (a budget that names another organization is
    refused), and it applies only to calls under that organization. A run-scoped budget on a run
    that exists and was started by another organization is refused (`ValueError`, nothing
    written).
    """
    _check_actor(actor)
    if budget.org_id is not None and budget.org_id != org_id:
        raise ValueError("a budget can only be defined for the organization it names")
    budget = budget.model_copy(update={"org_id": org_id})
    with store.transaction() as uow:
        if budget.scope is BudgetScope.RUN:
            first = uow.events_after(run_stream(budget.scope_id), 0, 1)
            if first and first[0].type == EventType.RUN_STARTED.value:
                owner = first[0].payload.get("org")
                if isinstance(owner, str) and owner and owner != org_id:
                    raise ValueError("that run belongs to another organization")
        balance = BudgetBalance.fresh(budget, uow.now())
        uow.put_balance(balance, expected_version=None)
        uow.append(
            EventDraft(
                stream=f"org:{org_id}",
                type=EventType.BUDGET_DEFINED,
                actor=actor,
                run_id=budget.scope_id if budget.scope is BudgetScope.RUN else None,
                payload={
                    "budget": budget.id,
                    "scope": budget.scope.value,
                    "scope_id": budget.scope_id,
                    "limit": budget.limit.amount,
                    "currency": budget.limit.currency.value,
                    **(
                        {}
                        if budget.window is BudgetWindow.NONE or balance.window_start is None
                        else {
                            "window": budget.window.value,
                            "on_breach": budget.breach_mode.value,
                            "window_start": format_time(balance.window_start),
                        }
                    ),
                },
            )
        )
    return balance


def reserve_for_call(
    store: Store,
    *,
    reservation_id: str,
    context: CallContext,
    org: OrgGraph,
    amount: Money,
    actor: str,
    details: Mapping[str, Any] | None = None,
) -> Reservation:
    """Hold `amount` in every applicable budget, or record the hard stop and raise.

    `details` (for example the resolved price) are recorded under `details` in the payload.
    """
    return _reserve(
        store,
        reservation_id=reservation_id,
        context=context,
        org=org,
        amount=amount,
        actor=actor,
        details=details,
        planner=None,
    )[0]


@dataclass(frozen=True)
class CappedReservation:
    """A reservation made by `reserve_for_call_capped` and the cap decision behind it (None: no
    cap was configured, so the call is exactly the uncapped one)."""

    reservation: Reservation
    cap: OutputCap | None


@dataclass(frozen=True)
class _Plan:
    amount: Money
    cap: OutputCap | NoRoom | None
    event: dict[str, Any] | None  # a `model.output_capped` payload, written only on success
    refusal: dict[str, Any] | None  # `details.output_cap` of a hard stop


def output_cap_payload(
    stage: str,
    *,
    reservation_id: str,
    agent: str,
    role: str,
    request: ModelRequest,
    price: ResolvedPrice,
    requested: int,
    effective: int,
    role_cap: int | None,
    task_cap: int | None,
    budget_cap: int | None,
    binding: CapBinding,
    limiting_budget_id: str | None,
    available: Money | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A `model.output_capped` payload (section 6)."""
    return {
        "stage": stage,
        "reservation": reservation_id,
        "agent": agent,
        "role": role,
        "provider": request.provider,
        **({} if request.endpoint_id is None else {"endpoint_id": request.endpoint_id}),
        "model": request.model,
        "requested_max_tokens": requested,
        "effective_max_tokens": effective,
        "role_cap": role_cap,
        "task_cap": task_cap,
        "budget_cap": budget_cap,
        "binding": binding.value,
        "limiting_budget_id": limiting_budget_id,
        "available": None if available is None else available.amount,
        "prompt_token_bound": request.prompt_token_bound,
        "price": price.ledger_payload(),
        **(extra or {}),
    }


def reserve_for_call_capped(
    store: Store,
    *,
    reservation_id: str,
    context: CallContext,
    org: OrgGraph,
    request: ModelRequest,
    price: ResolvedPrice,
    role_cap: int | None,
    task_cap: int | None,
    cap_exempt: bool,
    actor: str,
    details: Mapping[str, Any] | None = None,
) -> CappedReservation:
    """`reserve_for_call` for a call that may have an output cap (R1.42, section 4).

    The decision is made inside the reservation transaction, from the balances read there (after
    the window roll). With no cap configured it is exactly `reserve_for_call` of the worst case of
    the request. With one, the reservation is the worst case of the effective size; a lowered call
    records `model.output_capped` (`applied`) just before `budget.reserved`. A `cap_exempt` call
    (calibration, eval) reserves the full request and records `not_applied` when a cap would have
    lowered it. A hard stop that a cap shaped carries `details.output_cap`.
    """
    asked = request.max_output_tokens
    prompt = request.prompt_token_bound
    agent_role = org.agent(context.agent_id).role

    def event(stage: str, cap: OutputCap) -> dict[str, Any]:
        assert cap.binding is not None
        return output_cap_payload(
            stage,
            reservation_id=reservation_id,
            agent=context.agent_id,
            role=agent_role,
            request=request,
            price=price,
            requested=cap.requested,
            effective=cap.effective,
            role_cap=cap.role_cap,
            task_cap=cap.task_cap,
            budget_cap=cap.budget_cap,
            binding=cap.binding,
            limiting_budget_id=cap.limiting_budget_id,
            available=cap.available,
        )

    def planner(balances: Sequence[BudgetBalance]) -> _Plan:
        if role_cap is None and task_cap is None:
            return _Plan(worst_case_cost(prompt, asked, price), None, None, None)
        if cap_exempt:
            cfg = configured_size(asked, role_cap, task_cap)
            would = decide_cap(asked, role_cap, task_cap, (), prompt, price)
            assert isinstance(would, OutputCap) and would.effective == cfg
            return _Plan(
                worst_case_cost(prompt, asked, price),
                None,
                event("not_applied", would) if would.lowered else None,
                None,
            )
        decision = decide_cap(asked, role_cap, task_cap, balances, prompt, price)
        if isinstance(decision, NoRoom):
            refusal = (
                {
                    "requested": asked,
                    "effective": decision.configured,
                    "binding": decision.binding.value if decision.binding else None,
                }
                if decision.lowered
                else None
            )
            return _Plan(
                worst_case_cost(prompt, decision.configured, price), decision, None, refusal
            )
        lowered = decision.lowered
        return _Plan(
            worst_case_cost(prompt, decision.effective, price),
            decision,
            event("applied", decision) if lowered else None,
            {
                "requested": asked,
                "effective": decision.effective,
                "binding": decision.binding.value if decision.binding else None,
            }
            if lowered
            else None,
        )

    reservation, plan = _reserve(
        store,
        reservation_id=reservation_id,
        context=context,
        org=org,
        amount=None,
        actor=actor,
        details=details,
        planner=planner,
    )
    cap = plan.cap if isinstance(plan.cap, OutputCap) else None
    return CappedReservation(reservation=reservation, cap=cap)


def _reserve(
    store: Store,
    *,
    reservation_id: str,
    context: CallContext,
    org: OrgGraph,
    amount: Money | None,
    actor: str,
    details: Mapping[str, Any] | None,
    planner: Callable[[Sequence[BudgetBalance]], _Plan] | None,
) -> tuple[Reservation, _Plan]:
    _check_actor(actor)
    org.agent(context.agent_id)  # KeyError for an agent outside the org
    refused: list[Shortfall] = []
    no_budget = False
    reservation: Reservation | None = None
    with store.transaction() as uow:
        stored_all = {s.balance.budget.id: s for s in uow.list_balances(org.id)}
        applicable = applicable_budgets(
            [s.balance.budget for s in stored_all.values()], context, org
        )
        ids = tuple(b.id for b in applicable)
        stored = _roll_due(
            uow,
            [stored_all[i] for i in ids],
            stream=run_stream(context.run_id),
            run_id=context.run_id,
            task_id=context.task_id,
            agent=context.agent_id,
            actor=actor,
            reservation_id=reservation_id,
        )
        held = tuple(
            ReservedWindow(budget_id=s.balance.budget.id, start=s.balance.window_start)
            for s in stored
            if s.balance.window_start is not None
        )
        common: dict[str, Any] = {
            "reservation": reservation_id,
            "agent": context.agent_id,
            "budgets": list(ids),
            **({"windows": {w.budget_id: format_time(w.start) for w in held}} if held else {}),
            **_details(details),
        }
        if planner is not None:
            plan = planner([s.balance for s in stored])
        else:
            assert amount is not None
            plan = _Plan(amount, None, None, None)
        amount = plan.amount
        if not ids:
            no_budget = True
        else:
            refused = shortfalls([s.balance for s in stored], amount)
        if no_budget or refused:
            if plan.refusal is not None:
                common = {
                    **common,
                    "details": {**common.get("details", {}), "output_cap": plan.refusal},
                }
            uow.append(
                EventDraft(
                    stream=run_stream(context.run_id),
                    type=EventType.BUDGET_EXCEEDED,
                    actor=actor,
                    run_id=context.run_id,
                    task_id=context.task_id,
                    payload={
                        **common,
                        "reason": "no_budget" if no_budget else "hard_stop",
                        "requested": amount.amount,
                        "shortfalls": _shortfall_payload(refused),
                    },
                )
            )
        else:
            if uow.get_reservation(reservation_id) is not None:
                raise ConcurrencyError(f"reservation '{reservation_id}' already exists")
            reservation = Reservation(
                id=reservation_id,
                org_id=org.id,
                context=context,
                budget_ids=ids,
                amount=amount,
                windows=held,
            )
            _save(uow, stored, reserve([s.balance for s in stored], amount))
            uow.put_reservation(reservation, expected_version=None)
            if plan.event is not None:  # just before budget.reserved, same transaction
                uow.append(
                    EventDraft(
                        stream=run_stream(context.run_id),
                        type=EventType.MODEL_OUTPUT_CAPPED,
                        actor=actor,
                        run_id=context.run_id,
                        task_id=context.task_id,
                        payload=plan.event,
                    )
                )
            uow.append(
                EventDraft(
                    stream=run_stream(context.run_id),
                    type=EventType.BUDGET_RESERVED,
                    actor=actor,
                    run_id=context.run_id,
                    task_id=context.task_id,
                    payload=common,
                    cost=amount,
                    cost_basis=CostBasis.RESERVED,
                )
            )
    # Raised after the commit, so the hard stop stays in the ledger.
    if no_budget:
        raise NoBudgetError(context)
    if refused:
        raise BudgetExceededError(refused)
    assert reservation is not None
    return reservation, plan


def start_budget_problem(
    store: Store, org: OrgGraph, run_id: str, assignees: Mapping[str, str]
) -> str | None:
    """The start check of ND347 4.6, read-only (nothing is reserved, nothing is written): for each
    planned task (`assignees` maps task id to assignee) at least one budget applies, none is
    blocked and some headroom is left. Returns the fixed code `run_budget_missing`,
    `run_budget_blocked` or `run_budget_exhausted`, else None. A windowed budget is read as if its
    window had rolled, so a `reset` budget spent in a past window does not refuse a start."""
    now = store.now()
    with store.transaction() as uow:
        balances = {s.balance.budget.id: s.balance for s in uow.list_balances(org.id)}
    for task_id, agent in assignees.items():
        context = CallContext(run_id=run_id, task_id=task_id, agent_id=agent)
        applicable = applicable_budgets([b.budget for b in balances.values()], context, org)
        if not applicable:
            return "run_budget_missing"
        for budget in applicable:
            balance = balances[budget.id]
            rolled = roll(balance, now)
            if rolled is not None:
                balance = rolled.balance
            if balance.blocked:
                return "run_budget_blocked"
            if balance.available.amount <= 0:
                return "run_budget_exhausted"
    return None


def settle_reservation(
    store: Store,
    reservation_id: str,
    *,
    actual: Money | None,
    cost_basis: CostBasis,
    actor: str,
    details: Mapping[str, Any] | None = None,
) -> Reservation:
    """Charge a reservation. `actual=None` means the cost is unknown (charged at the reservation).

    `cost_basis` is `actual` or `provisional` for a known amount, `unknown` when `actual` is None.
    """
    if (actual is None) != (cost_basis is CostBasis.UNKNOWN):
        raise ValueError("an unknown cost has no amount; a known one has basis actual/provisional")
    if cost_basis is CostBasis.RESERVED:
        raise ValueError("'reserved' isn't a settlement basis")
    _check_actor(actor)
    with store.transaction() as uow:
        current = uow.get_reservation(reservation_id)
        if current is None:
            raise NotFoundError(f"reservation '{reservation_id}' doesn't exist")
        reservation = current.reservation
        if reservation.status is not ReservationStatus.OPEN:
            raise ConcurrencyError(
                f"reservation '{reservation_id}' is already {reservation.status}"
            )
        charge = reservation.amount if actual is None else actual
        stored = _load(uow, reservation.org_id, reservation.budget_ids)
        results = settle(
            [s.balance for s in stored], reservation.amount, charge, reservation.window_map
        )
        _save(uow, stored, [r.balance for r in results])
        settled = reservation.model_copy(
            update={"status": ReservationStatus.SETTLED, "charged": charge}
        )
        uow.put_reservation(settled, expected_version=current.version)
        context = reservation.context
        payload: dict[str, Any] = {
            "reservation": reservation_id,
            "agent": context.agent_id,
            "reserved": reservation.amount.amount,
            "budget_charge": charge.amount,
            "budgets": [
                {
                    "budget": r.balance.budget.id,
                    "released": r.released.amount,
                    "overrun": r.overrun.amount,
                    "spent": r.spent.amount,
                    "available": r.available.amount,
                    **(
                        {}
                        if r.window_start is None
                        else {"window_start": format_time(r.window_start)}
                    ),
                    **({"window_closed": True} if r.closed_window else {}),
                }
                for r in results
            ],
            **_details(details),
        }
        if actual is None:
            payload["cost_unknown"] = "charged the reservation (an upper bound) to the budgets"
        uow.append(
            EventDraft(
                stream=run_stream(context.run_id),
                type=EventType.BUDGET_SETTLED,
                actor=actor,
                run_id=context.run_id,
                task_id=context.task_id,
                payload=payload,
                cost=actual,
                cost_basis=cost_basis,
            )
        )
        breached: list[BreachItem] = [
            (r.balance.budget, r.spent, r.window_start, r.closed_window)
            for r in results
            if r.blocked_now or (r.closed_window and r.became_overspent)
        ]
        if breached:
            uow.append(
                _breach_event(context, actor, "settlement_overrun", breached, reservation_id)
            )
        over = [r for r in results if r.overrun.amount > 0 or r.became_overspent]
        if over:
            uow.append(
                EventDraft(
                    stream=run_stream(context.run_id),
                    type=EventType.BUDGET_EXCEEDED,
                    actor=actor,
                    run_id=context.run_id,
                    task_id=context.task_id,
                    payload={
                        "reservation": reservation_id,
                        "agent": context.agent_id,
                        "reason": "overrun",
                        "budgets": [
                            {
                                "budget": r.balance.budget.id,
                                "scope": r.balance.budget.scope.value,
                                "scope_id": r.balance.budget.scope_id,
                                "window": _window_text(r.balance.budget.window),
                                "on_breach": r.balance.budget.breach_mode.value,
                                **(
                                    {
                                        "resets_at": format_time(
                                            window_end(
                                                r.balance.budget.window, r.balance.window_start
                                            )
                                        )
                                    }
                                    if r.balance.window_start is not None
                                    and not r.closed_window
                                    and r.balance.budget.breach_mode is BreachMode.RESET
                                    else {}
                                ),
                                "overrun": r.overrun.amount,
                                "spent": r.spent.amount,
                                "limit": r.balance.budget.limit.amount,
                                "overspent": r.spent > r.balance.budget.limit,
                                **(
                                    {}
                                    if r.window_start is None
                                    else {"window_start": format_time(r.window_start)}
                                ),
                                **({"window_closed": True} if r.closed_window else {}),
                            }
                            for r in over
                        ],
                    },
                )
            )
    return settled


def release_reservation(
    store: Store,
    reservation_id: str,
    *,
    actor: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> Reservation:
    """Return a reservation that was never charged (for example the call failed first).

    `details` are nested under a `details` key of the payload, as for `settle_reservation`.
    """
    if not reason.strip():
        raise ValueError("a release needs a reason")
    _check_actor(actor)
    with store.transaction() as uow:
        current = uow.get_reservation(reservation_id)
        if current is None:
            raise NotFoundError(f"reservation '{reservation_id}' doesn't exist")
        reservation = current.reservation
        if reservation.status is not ReservationStatus.OPEN:
            raise ConcurrencyError(
                f"reservation '{reservation_id}' is already {reservation.status}"
            )
        stored = _load(uow, reservation.org_id, reservation.budget_ids)
        _save(
            uow,
            stored,
            release([s.balance for s in stored], reservation.amount, reservation.window_map),
        )
        released = reservation.model_copy(update={"status": ReservationStatus.RELEASED})
        uow.put_reservation(released, expected_version=current.version)
        uow.append(
            EventDraft(
                stream=run_stream(reservation.context.run_id),
                type=EventType.BUDGET_RELEASED,
                actor=actor,
                run_id=reservation.context.run_id,
                task_id=reservation.context.task_id,
                payload={
                    "reservation": reservation_id,
                    "agent": reservation.context.agent_id,
                    "budgets": list(reservation.budget_ids),
                    "released": reservation.amount.amount,
                    "reason": reason,
                    **_details(details),
                },
            )
        )
    return released


def apply_adjustment(
    uow: UnitOfWork,
    *,
    context: CallContext,
    org_id: str,
    budget_ids: Sequence[str],
    amount: Money,
    reason: str,
    actor: str,
    reservation_id: str | None = None,
    cost_basis: CostBasis = CostBasis.ACTUAL,
    details: Mapping[str, Any] | None = None,
    budget_owners: Mapping[str, str | None] | None = None,
) -> list[BudgetBalance]:
    """Add `amount` to the budgets' spent inside the caller's transaction (see `adjust_spend`).

    `org_id` is the organization whose budgets `budget_ids` name (ND90); `budget_owners` names
    the organization of any budget that belongs to another one (a migrated closed reservation).
    Returns the budgets this
    newly breached (now blocked); `budget.adjusted` (with each budget's `breached` state) and,
    for a new breach, `budget.breached` are appended in the same transaction as the balance
    change. It checks its own inputs (reason, budgets, amount, basis, actor).
    """
    if not reason.strip():
        raise ValueError("an adjustment needs a reason")
    if not budget_ids:
        raise ValueError("an adjustment needs at least one budget")
    if not amount.amount > 0:
        raise ValueError("an adjustment must be a positive amount")
    if cost_basis is CostBasis.RESERVED:
        raise ValueError("'reserved' isn't an adjustment basis")
    _check_actor(actor)
    stored = _roll_due(
        uow,
        _load(uow, org_id, budget_ids, budget_owners),
        stream=run_stream(context.run_id),
        run_id=context.run_id,
        task_id=context.task_id,
        agent=context.agent_id,
        actor=actor,
        reservation_id=reservation_id,
    )
    results = adjust([s.balance for s in stored], amount)
    _save(uow, stored, [r.balance for r in results])
    uow.append(
        EventDraft(
            stream=run_stream(context.run_id),
            type=EventType.BUDGET_ADJUSTED,
            actor=actor,
            run_id=context.run_id,
            task_id=context.task_id,
            payload={
                "reason": reason,
                "reservation": reservation_id,
                "agent": context.agent_id,
                "amount": amount.amount,
                "budgets": [
                    {
                        "budget": r.balance.budget.id,
                        "spent": r.balance.spent.amount,
                        "available": r.balance.available.amount,
                        "breached": r.breached,
                        **(
                            {}
                            if r.balance.window_start is None
                            else {"window_start": format_time(r.balance.window_start)}
                        ),
                    }
                    for r in results
                ],
                **_details(details),
            },
            cost=None if cost_basis is CostBasis.UNKNOWN else amount,
            cost_basis=cost_basis,
        )
    )
    breached = [r.balance for r in results if r.blocked_now]  # newly breached: "became"
    if breached:
        uow.append(
            _breach_event(
                context,
                actor,
                reason,
                [(b.budget, b.spent, b.window_start, False) for b in breached],
                reservation_id,
            )
        )
    return breached


def adjust_spend(
    store: Store,
    *,
    context: CallContext,
    org_id: str,
    budget_ids: Sequence[str],
    amount: Money,
    reason: str,
    actor: str,
    reservation_id: str | None = None,
    cost_basis: CostBasis = CostBasis.ACTUAL,
    details: Mapping[str, Any] | None = None,
) -> list[BudgetBalance]:
    """Add a late actual cost to the budgets' *spent* in one transaction (ND23).

    For a cost that arrived after the reconciler closed its reservation (T24) or any other spend
    the budgets didn't see. Returns the budgets that are now breached (over their limit, blocked);
    a breach needs a human (`unblock_budget`).
    """
    with store.transaction() as uow:
        return apply_adjustment(
            uow,
            context=context,
            org_id=org_id,
            budget_ids=budget_ids,
            amount=amount,
            reason=reason,
            actor=actor,
            reservation_id=reservation_id,
            cost_basis=cost_basis,
            details=details,
        )


def unblock_budget(
    store: Store,
    budget_id: str,
    *,
    actor: str,
    reason: str,
    org_id: str,
    content: ContentService,
    new_limit: Money | None = None,
) -> BudgetBalance:
    """A human lifts a block (`budget.unblocked`, in the org's stream), maybe raising the limit.

    The actor must be a human (`human:<id>`) and give a reason. A budget still over its limit needs
    a higher limit that covers what was spent (`ValueError` otherwise; nothing changes). The ledger
    records the keyed **hash** of the reason; the text goes to the content store in the same
    transaction (T47).

    **Windowed budgets (T56):** the lift acts on the budget's *current* window. If its window has
    ended, it is rolled first in the same transaction (`budget.window_rolled`). When that roll
    already cleared the block, there is nothing left to lift: the rolled balance is returned
    unchanged (no `budget.unblocked`, `new_limit` is not applied, no error), the same thing a
    reader sees. Otherwise the usual rules apply to the current window's numbers; a budget that is
    not blocked (including one whose overrun is in a closed window) is refused. A refusal rolls
    nothing back into the store (the roll happens at the next call).
    """
    if not is_human_id(actor):
        raise ValueError("only a human can lift a budget block")
    if not reason.strip():
        raise ValueError("lifting a block needs a reason")
    _check_actor(actor)
    with store.transaction() as uow:
        return unblock_budget_in(
            uow,
            budget_id,
            actor=actor,
            reason=reason,
            org_id=org_id,
            content=content,
            new_limit=new_limit,
        )


def unblock_budget_in(
    uow: UnitOfWork,
    budget_id: str,
    *,
    actor: str,
    reason: str,
    org_id: str,
    content: ContentService,
    new_limit: Money | None = None,
) -> BudgetBalance:
    """`unblock_budget` inside the caller's unit of work (R1.35): opens no transaction."""
    if not is_human_id(actor):
        raise ValueError("only a human can lift a budget block")
    if not reason.strip():
        raise ValueError("lifting a block needs a reason")
    _check_actor(actor)
    loaded = _load(uow, org_id, [budget_id])[0]
    [stored] = _roll_due(
        uow,
        [loaded],
        stream=f"org:{org_id}",
        run_id=None,
        task_id=None,
        agent=None,
        actor=actor,
        reservation_id=None,
    )
    if stored is not loaded and loaded.balance.blocked and not stored.balance.blocked:
        return stored.balance  # the new window already cleared the block
    lifted = unblock(stored.balance, new_limit)
    _save(uow, [stored], [lifted])
    reason_hash = content.capture_reason(uow, org_id, reason.strip())
    uow.append(
        EventDraft(
            stream=f"org:{org_id}",
            type=EventType.BUDGET_UNBLOCKED,
            actor=actor,
            payload={
                "budget": budget_id,
                "reason_hash": reason_hash,
                "spent": lifted.spent.amount,
                "old_limit": stored.balance.budget.limit.amount,
                "new_limit": lifted.budget.limit.amount,
                **(
                    {}
                    if lifted.window_start is None
                    else {"window_start": format_time(lifted.window_start)}
                ),
            },
        )
    )
    return lifted
