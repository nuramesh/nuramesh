"""Budgets: hard stops on spending (PLAN E.1–E.2; "Budgets are hard stops").

A budget caps spending for one **scope**: a run, a task, an agent, or a team (an agent and every
agent below it in the org graph). A call must fit inside **every** budget that applies to it.

Each budget's balance has `spent` (settled costs) and `reserved` (open reservations);
`available = limit - spent - reserved`.

1. **Reserve** the worst-case cost before the call. If any applicable budget's available amount
   is smaller than the reservation, the call is refused: a hard stop.
2. **Settle** with the actual cost after the call: `reserved` drops by the reservation, `spent`
   rises by the actual cost. If the actual cost is higher than the reservation (it shouldn't be),
   the truth is still recorded and the overrun is reported; a budget whose spending passed its
   limit refuses every later reservation.
3. **Release** a reservation that was never charged (the call failed before spending).

**Windows (T56).** A budget may carry a UTC calendar `window` (`day` or `month`). Its `spent` and
`reserved` then count only inside the current window; the first reservation or adjustment that
falls in a later window **rolls** it: the old window is closed (its final numbers go to the ledger,
nothing is edited), a fresh balance starts and a block clears by itself (`on_breach: reset`) or
stays until a human lifts it (`hold`). A reservation belongs to the window it was made in and is
settled or released against that window, even after a roll.
Without a window (the default) a budget is a lifetime cap, exactly as before.

**Organizations (R3.02a, ND90).** A budget belongs to the organization that defined it (`org_id`)
and is identified by the pair (organization, id). It applies only to calls made under that
organization: agent, team, task and run ids are read inside it, never across organizations.
`org_id` is `None` only for a budget from before this rule that no ledger entry could attribute;
nothing applies it.

Everything here is pure and exact; the engine stores balances and writes the ledger in the same
transaction. Time is always passed in (`now`); nothing here reads a clock.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from nuramesh.core.money import Money
from nuramesh.core.org import OrgGraph, Slug
from nuramesh.core.prices import MAX_TOKENS


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BudgetScope(StrEnum):
    """What a budget caps."""

    RUN = "run"
    TASK = "task"
    AGENT = "agent"
    TEAM = "team"
    ORG = "org"
    """The whole organization: every run, task and agent. `scope_id` is the org's id."""


class BudgetWindow(StrEnum):
    """How long a budget's balance lives (T56). UTC calendar windows."""

    NONE = "none"
    """A lifetime cap (the default; how every budget worked before windows)."""
    DAY = "day"
    MONTH = "month"


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("a window needs a timezone-aware time")
    return at.astimezone(UTC)


def window_start(window: BudgetWindow, at: datetime) -> datetime | None:
    """The first instant (UTC) of the window that contains `at`; None for a lifetime budget.

    A window includes its start and excludes its end: midnight belongs to the new day.
    """
    moment = _utc(at)
    if window is BudgetWindow.NONE:
        return None
    if window is BudgetWindow.DAY:
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def window_end(window: BudgetWindow, start: datetime) -> datetime:
    """The first instant of the next window after the one that starts at `start` (exclusive end)."""
    if window is BudgetWindow.NONE:
        raise ValueError("a lifetime budget has no window end")
    if window is BudgetWindow.DAY:
        return start + timedelta(days=1)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start.replace(year=year, month=month)


class BreachMode(StrEnum):
    """What a breach (spending over the limit) does to a windowed budget at the next window."""

    RESET = "reset"
    """The block clears by itself when the next window starts (a human may lift it earlier)."""
    HOLD = "hold"
    """The block stays until a human lifts it, however many windows pass."""


NO_ORG = ""
"""The store key of a budget no organization could be attributed to (see `org_key`)."""


def org_key(org_id: str | None) -> str:
    """The organization part of a budget's store key; `NO_ORG` for an unattributed budget."""
    return NO_ORG if org_id is None else org_id


class Budget(_Frozen):
    """A spending cap for one scope. `scope_id` is the run, task or agent id (team: its lead;
    org: the org's id). With a `window` the cap applies per UTC day or month."""

    id: Slug
    org_id: SkipJsonSchema[Slug | None] = None
    # The organization that defined the budget; its key is (`org_id`, `id`) (ND90). Set by
    # `define_budget`, never written in a configuration file, so it is not in the config schema.
    scope: BudgetScope
    scope_id: Slug
    limit: Money
    window: BudgetWindow = BudgetWindow.NONE
    on_breach: BreachMode | None = None
    """Only meaningful with a window. Default: `reset` for a day or month window; a lifetime
    budget always holds (there is no next window), so `reset` on it is refused."""

    @model_validator(mode="after")
    def _non_negative(self) -> Self:
        if self.limit.is_negative:
            raise ValueError("a budget limit can't be negative")
        if self.window is BudgetWindow.NONE and self.on_breach is BreachMode.RESET:
            raise ValueError(
                "on_breach: reset needs a window (day or month); a budget without one always holds"
            )
        return self

    @property
    def breach_mode(self) -> BreachMode:
        """The mode in force: a lifetime budget holds; a windowed one resets unless told to hold."""
        if self.window is BudgetWindow.NONE:
            return BreachMode.HOLD
        return self.on_breach or BreachMode.RESET


class ClosedWindow(_Frozen):
    """A finished window that still holds open reservations (T56).

    Kept only until its last reservation is settled or released; the window's final numbers are in
    the ledger (`budget.window_rolled`, then each `budget.settled`).
    """

    start: datetime
    spent: Money
    reserved: Money
    open: int = 1
    """How many open reservations (of any amount, zero included) still name this window."""


class BudgetBalance(_Frozen):
    """A budget with what has been spent and what is reserved (in the current window, if any)."""

    budget: Budget
    spent: Money
    reserved: Money
    blocked: bool = False
    """Set when spending passed the limit (ND23): nothing more can be reserved until a human
    lifts the block (`unblock`), with a new limit if the budget is still over it. A windowed
    budget's block also clears by itself when the next window starts (T56)."""
    window_start: datetime | None = None
    """Start of the current window (UTC); set exactly when the budget has a window."""
    closed: tuple[ClosedWindow, ...] = ()
    """Finished windows with reservations still open, oldest first."""
    open_count: int = 0
    """Open reservations in the current window (windowed budgets only; zero amounts count)."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for value in (self.spent, self.reserved):
            if value.currency != self.budget.limit.currency:
                raise ValueError("balance and limit must use the same currency")
        if self.reserved.is_negative or self.spent.is_negative:
            raise ValueError("spent and reserved can't be negative")
        window = self.budget.window
        if window is BudgetWindow.NONE:
            if self.window_start is not None or self.closed or self.open_count:
                raise ValueError("a lifetime budget has no window")
        else:
            if self.open_count < 0:
                raise ValueError("open reservations can't be negative")
            if self.window_start is None or window_start(window, self.window_start) != (
                self.window_start
            ):
                raise ValueError("a windowed budget needs the exact start of its window (UTC)")
            starts = [c.start for c in self.closed]
            if starts != sorted(set(starts)) or any(s >= self.window_start for s in starts):
                raise ValueError("closed windows must be older than the current one")
            for c in self.closed:
                if window_start(window, c.start) != c.start:
                    raise ValueError("a closed window needs the exact start of its window")
                if c.spent.currency != self.budget.limit.currency or c.reserved.currency != (
                    self.budget.limit.currency
                ):
                    raise ValueError("balance and limit must use the same currency")
                if c.open < 1:
                    raise ValueError("a closed window is kept only while a reservation names it")
                if c.spent.is_negative or c.reserved.is_negative:
                    raise ValueError("spent and reserved can't be negative")
        return self

    @classmethod
    def fresh(cls, budget: Budget, now: datetime | None = None) -> Self:
        """A budget nothing has been spent from. A windowed budget needs `now` (its window)."""
        zero = Money.zero(budget.limit.currency)
        start = window_start(budget.window, now) if now is not None else None
        if budget.window is not BudgetWindow.NONE and start is None:
            raise ValueError("a windowed budget needs the current time to start its window")
        return cls(budget=budget, spent=zero, reserved=zero, window_start=start)

    @property
    def available(self) -> Money:
        """What can still be reserved (negative after an overrun)."""
        return self.budget.limit - self.spent - self.reserved

    @property
    def overspent(self) -> bool:
        """Spending passed the limit (only possible through an overrun at settlement)."""
        return self.spent > self.budget.limit


class CallContext(_Frozen):
    """Who is spending: used to find the budgets that apply."""

    run_id: Slug
    task_id: Slug | None = None
    agent_id: Slug
    task_max_tokens: int | None = Field(default=None, strict=True, ge=1, le=MAX_TOKENS)
    """R1.42: the task's output cap (the worker's; unset for verifier, calibration, eval calls)."""
    cap_exempt: bool = False
    """R1.42: calibration and eval calls are never capped (they run at the size they ask for)."""

    # No return annotation on purpose: one would replace the serialisation JSON schema (OpenAPI).
    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        data: dict[str, object] = handler(self)
        if data.get("task_max_tokens") is None:
            data.pop("task_max_tokens", None)
        if not data.get("cap_exempt"):
            data.pop("cap_exempt", None)
        return data


def applicable_budgets(
    budgets: Iterable[Budget], context: CallContext, org: OrgGraph
) -> tuple[Budget, ...]:
    """Every budget the call must fit in, sorted by id.

    Only budgets of the call's own organization (`org.id`) are considered, whatever their scope ids
    (ND90). A team budget applies when the agent is the team's lead or anywhere below it; an org
    budget applies to every call of that organization.
    """
    chain = set(org.chain_of_command(context.agent_id))
    found = []
    for budget in budgets:
        if budget.org_id != org.id:
            continue  # another organization's (or an unattributed) budget never applies
        if budget.scope is BudgetScope.RUN:
            applies = budget.scope_id == context.run_id
        elif budget.scope is BudgetScope.TASK:
            applies = budget.scope_id == context.task_id
        elif budget.scope is BudgetScope.AGENT:
            applies = budget.scope_id == context.agent_id
        elif budget.scope is BudgetScope.ORG:
            applies = budget.scope_id == org.id
        else:
            applies = budget.scope_id in chain
        if applies:
            found.append(budget)
    return tuple(sorted(found, key=lambda b: b.id))


class Shortfall(_Frozen):
    """A budget that can't take a reservation. `blocked`: it is blocked (a `reset` budget until its
    window ends or a human lifts it; a `hold` budget, `held`, only until a human lifts it)."""

    blocked: bool = False

    budget_id: Slug
    scope: BudgetScope
    scope_id: Slug
    limit: Money
    available: Money
    requested: Money
    window: BudgetWindow = BudgetWindow.NONE
    window_start: datetime | None = None
    spent: Money | None = None
    resets_at: datetime | None = None
    """For a windowed budget that is not held: when its current window ends (the money resets then).
    None for a held block (only a human lifts it) and for a lifetime budget."""
    on_breach: BreachMode = BreachMode.HOLD
    held: bool = False
    """Blocked and in `hold` mode: only a human lifts it, the window ending changes nothing."""


class BudgetExceededError(Exception):
    """A hard stop: at least one applicable budget can't take the reservation."""

    def __init__(self, shortfalls: Sequence[Shortfall]) -> None:
        self.shortfalls = tuple(shortfalls)
        names = ", ".join(f"{s.budget_id} (available {s.available})" for s in self.shortfalls)
        super().__init__(f"budget hard stop: {names}")


def shortfalls(balances: Iterable[BudgetBalance], amount: Money) -> list[Shortfall]:
    """Budgets whose available amount is smaller than `amount` (empty means it fits)."""
    if amount.is_negative:
        raise ValueError("a reservation can't be negative")
    found = []
    for balance in balances:
        if balance.blocked or balance.available < amount:
            held = balance.blocked and balance.budget.breach_mode is BreachMode.HOLD
            found.append(
                Shortfall(
                    blocked=balance.blocked,
                    budget_id=balance.budget.id,
                    scope=balance.budget.scope,
                    scope_id=balance.budget.scope_id,
                    limit=balance.budget.limit,
                    available=balance.available,
                    requested=amount,
                    window=balance.budget.window,
                    window_start=balance.window_start,
                    spent=balance.spent,
                    resets_at=None
                    if balance.window_start is None or held
                    else window_end(balance.budget.window, balance.window_start),
                    on_breach=balance.budget.breach_mode,
                    held=held,
                )
            )
    return found


def reserve(balances: Sequence[BudgetBalance], amount: Money) -> tuple[BudgetBalance, ...]:
    """Balances after reserving `amount` from every one, or `BudgetExceededError`."""
    missing = shortfalls(balances, amount)
    if missing:
        raise BudgetExceededError(missing)
    return tuple(
        b.model_copy(update={"reserved": b.reserved + amount, "open_count": _held(b, 1)})
        for b in balances
    )


class Settlement(_Frozen):
    """The effect of settling one reservation on one budget.

    `spent` and `available` are those of the window the reservation belongs to (the current one,
    or a closed one: `closed_window`); `balance` is the budget after the settlement.
    """

    balance: BudgetBalance
    released: Money
    overrun: Money
    became_overspent: bool
    blocked_now: bool = False
    spent: Money
    available: Money
    window_start: datetime | None = None
    closed_window: bool = False


def _window_of(balance: BudgetBalance, windows: Mapping[str, datetime] | None) -> datetime | None:
    """The window a reservation holds in this budget, if it isn't the current one."""
    held = None if windows is None else windows.get(balance.budget.id)
    if held is None or held == balance.window_start:
        return None
    if balance.window_start is None or held > balance.window_start:
        raise ValueError(f"budget '{balance.budget.id}' has no window starting at {held}")
    return held


def _held(balance: BudgetBalance, change: int) -> int:
    """The open-reservation count after `change` (only windowed budgets count them)."""
    if balance.budget.window is BudgetWindow.NONE:
        return 0
    return max(0, balance.open_count + change)


def _closed(balance: BudgetBalance, start: datetime, reserved: Money) -> ClosedWindow:
    """The closed window a reservation was made in (it must still name it)."""
    for window in balance.closed:
        if window.start == start:
            if window.reserved < reserved:
                raise ValueError(f"budget '{balance.budget.id}' doesn't hold this reservation")
            return window
    raise ValueError(f"budget '{balance.budget.id}' doesn't hold this reservation")


def _with_closed(balance: BudgetBalance, updated: ClosedWindow) -> BudgetBalance:
    """The balance with one closed window replaced; a window no reservation names is dropped."""
    kept = tuple(
        updated if c.start == updated.start else c
        for c in balance.closed
        if c.start != updated.start or updated.open > 0
    )
    return balance.model_copy(update={"closed": kept})


def settle(
    balances: Sequence[BudgetBalance],
    reserved: Money,
    actual: Money,
    windows: Mapping[str, datetime] | None = None,
) -> tuple[Settlement, ...]:
    """Move a reservation into spending at its actual cost.

    `released` is what was reserved but not spent; `overrun` is what was spent beyond the
    reservation (normally zero). `windows` maps budget ids to the window start the reservation was
    made in; a reservation whose window has since been rolled is settled against that closed
    window (never against the current one).
    """
    if actual.is_negative:
        raise ValueError("an actual cost can't be negative")
    zero = Money.zero(actual.currency)
    out = []
    for balance in balances:
        old = _window_of(balance, windows)
        released = reserved - actual if actual < reserved else zero
        overrun = actual - reserved if actual > reserved else zero
        if old is not None:
            window = _closed(balance, old, reserved)
            was_over = window.spent > balance.budget.limit
            updated = window.model_copy(
                update={
                    "reserved": window.reserved - reserved,
                    "spent": window.spent + actual,
                    "open": window.open - 1,
                }
            )
            over = updated.spent > balance.budget.limit
            moved = _with_closed(balance, updated)
            holds = over and not was_over and balance.budget.breach_mode is BreachMode.HOLD
            if holds:  # a `hold` budget stays blocked after any breach, in any window
                moved = moved.model_copy(update={"blocked": True})
            out.append(
                Settlement(
                    balance=moved,
                    blocked_now=holds and not balance.blocked,
                    released=released,
                    overrun=overrun,
                    became_overspent=over and not was_over,
                    spent=updated.spent,
                    available=balance.budget.limit - updated.spent - updated.reserved,
                    window_start=old,
                    closed_window=True,
                )
            )
            continue
        if balance.reserved < reserved:
            raise ValueError(f"budget '{balance.budget.id}' doesn't hold this reservation")
        was_over = balance.overspent
        new = balance.model_copy(
            update={
                "reserved": balance.reserved - reserved,
                "spent": balance.spent + actual,
                "open_count": _held(balance, -1),
            }
        )
        if new.overspent:  # ND23: over the limit means blocked until a human decides
            new = new.model_copy(update={"blocked": True})
        out.append(
            Settlement(
                balance=new,
                released=released,
                overrun=overrun,
                became_overspent=new.overspent and not was_over,
                blocked_now=new.blocked and not balance.blocked,
                spent=new.spent,
                available=new.available,
                window_start=balance.window_start,
            )
        )
    return tuple(out)


class Adjustment(_Frozen):
    """The effect of adding a late actual cost to one budget's spent (ND23)."""

    balance: BudgetBalance
    amount: Money
    breached: bool
    blocked_now: bool


def adjust(balances: Sequence[BudgetBalance], amount: Money) -> tuple[Adjustment, ...]:
    """Add `amount` (a late actual cost, T24) to every budget's *spent*.

    A budget this leaves over its limit is **breached**: it is marked blocked, so nothing more can
    be reserved until a human lifts the block. Reservations are not touched.
    """
    if amount.is_negative:
        raise ValueError("an adjustment can't be negative")
    out = []
    for balance in balances:
        new = balance.model_copy(update={"spent": balance.spent + amount})
        breached = new.overspent
        if breached:
            new = new.model_copy(update={"blocked": True})
        out.append(
            Adjustment(
                balance=new,
                amount=amount,
                breached=breached,
                blocked_now=new.blocked and not balance.blocked,
            )
        )
    return tuple(out)


def unblock(balance: BudgetBalance, new_limit: Money | None = None) -> BudgetBalance:
    """Lift a block (a human decision, ledgered by the caller).

    A budget still over its limit can only be unblocked with a **higher** limit that covers what
    was spent; a budget within its limit can be unblocked as it is, or with a higher limit.
    """
    if not (balance.blocked or balance.overspent):  # overspent: from before blocking existed
        raise ValueError(f"budget '{balance.budget.id}' isn't blocked")
    budget = balance.budget
    if new_limit is not None:
        if new_limit.currency != budget.limit.currency:
            raise ValueError("a new limit must use the budget's currency")
        if new_limit <= budget.limit:
            raise ValueError("a new limit must be higher than the current one")
        budget = budget.model_copy(update={"limit": new_limit})
    lifted = balance.model_copy(update={"budget": budget, "blocked": False})
    if lifted.overspent:
        raise ValueError(
            f"budget '{budget.id}' is still over its limit; unblocking needs a higher limit "
            f"(spent {lifted.spent})"
        )
    return lifted


def release(
    balances: Sequence[BudgetBalance],
    reserved: Money,
    windows: Mapping[str, datetime] | None = None,
) -> tuple[BudgetBalance, ...]:
    """Give back a reservation that was never charged (in the window it was made in)."""
    out = []
    for balance in balances:
        old = _window_of(balance, windows)
        if old is not None:
            window = _closed(balance, old, reserved)
            out.append(
                _with_closed(
                    balance,
                    window.model_copy(
                        update={"reserved": window.reserved - reserved, "open": window.open - 1}
                    ),
                )
            )
            continue
        if balance.reserved < reserved:
            raise ValueError(f"budget '{balance.budget.id}' doesn't hold this reservation")
        out.append(
            balance.model_copy(
                update={"reserved": balance.reserved - reserved, "open_count": _held(balance, -1)}
            )
        )
    return tuple(out)


class Rollover(_Frozen):
    """A windowed budget moving to a new window (T56): what the closed window ended with."""

    balance: BudgetBalance
    """The budget in its new window: nothing spent or reserved, not blocked."""
    closed_start: datetime
    closed_end: datetime
    spent: Money
    """The closed window's final spent (reservations still open there may add to it)."""
    reserved: Money
    """Money still reserved in the closed window."""
    open: int
    """Open reservations (zero amounts too) still naming the closed window."""
    cleared_block: bool
    held_block: bool = False
    """The budget was blocked and `hold` keeps it blocked in the new window."""


def roll(balance: BudgetBalance, now: datetime) -> Rollover | None:
    """Start a new window if `now` is past the end of the current one; None otherwise.

    A clock that goes backwards never rolls back: `now` before the current window's start leaves
    the balance as it is. Windows with no activity in between are simply skipped (they spent
    nothing: any activity would have rolled the budget).
    """
    window = balance.budget.window
    if window is BudgetWindow.NONE or balance.window_start is None:
        return None
    start = window_start(window, now)
    assert start is not None
    if start <= balance.window_start:
        return None
    zero = Money.zero(balance.budget.limit.currency)
    held = balance.blocked and balance.budget.breach_mode is BreachMode.HOLD
    still_open = (
        (
            ClosedWindow(
                start=balance.window_start,
                spent=balance.spent,
                reserved=balance.reserved,
                open=balance.open_count,
            ),
        )
        if balance.open_count > 0
        else ()
    )
    return Rollover(
        balance=balance.model_copy(
            update={
                "spent": zero,
                "reserved": zero,
                "blocked": held,
                "open_count": 0,
                "window_start": start,
                "closed": (*balance.closed, *still_open),
            }
        ),
        closed_start=balance.window_start,
        closed_end=window_end(window, balance.window_start),
        spent=balance.spent,
        reserved=balance.reserved,
        open=balance.open_count,
        cleared_block=balance.blocked and not held,
        held_block=held,
    )


def as_of(balance: BudgetBalance, now: datetime) -> BudgetBalance:
    """The balance as a reader sees it at `now`: rolled if its window has ended (not stored)."""
    rolled = roll(balance, now)
    return balance if rolled is None else rolled.balance


def by_id(balances: Iterable[BudgetBalance]) -> Mapping[str, BudgetBalance]:
    """Balances keyed by budget id."""
    return {b.budget.id: b for b in balances}


class ReservationStatus(StrEnum):
    """A reservation is open until it is settled (charged) or released (not charged)."""

    OPEN = "open"
    SETTLED = "settled"
    RELEASED = "released"


class ReservedWindow(_Frozen):
    """The window of a windowed budget a reservation was made in (T56)."""

    budget_id: Slug
    start: datetime


class BudgetOrg(_Frozen):
    """The organization (None: unattributed) of one budget of a reservation."""

    budget_id: Slug
    org_id: Slug | None


class Reservation(_Frozen):
    """Money held for one call in every applicable budget."""

    id: Slug
    org_id: Slug | None = None
    """The organization of the call: `budget_ids` are budgets of this organization."""
    budget_orgs: tuple[BudgetOrg, ...] = ()
    """Budgets of `budget_ids` that belong to another organization than `org_id`. Only the
    migration to organization keys sets it, for a closed reservation of the old shared-id
    behaviour that held money in budgets of two owners (each keeps its own, ND90)."""
    context: CallContext
    budget_ids: tuple[Slug, ...]
    amount: Money
    windows: tuple[ReservedWindow, ...] = ()
    """For each windowed budget in `budget_ids`: the window the money is held in."""
    status: ReservationStatus = ReservationStatus.OPEN
    charged: Money | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.budget_ids)) != len(self.budget_ids):
            raise ValueError("a reservation lists a budget twice")
        if self.amount.is_negative:
            raise ValueError("a reservation can't be negative")
        held = [w.budget_id for w in self.windows]
        if len(set(held)) != len(held) or not set(held) <= set(self.budget_ids):
            raise ValueError("a reservation's windows must name each of its budgets at most once")
        if (self.status is ReservationStatus.SETTLED) != (self.charged is not None):
            raise ValueError("only a settled reservation has a charged amount")
        return self

    def org_of(self, budget_id: str) -> str | None:
        """The organization that owns `budget_id` of this reservation."""
        for other in self.budget_orgs:
            if other.budget_id == budget_id:
                return other.org_id
        return self.org_id

    @property
    def window_map(self) -> dict[str, datetime]:
        """Budget id to the window start the reservation holds (windowed budgets only)."""
        return {w.budget_id: w.start for w in self.windows}
