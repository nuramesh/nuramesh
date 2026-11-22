"""Per-role and per-task output caps, and the budget-aware shrink (R1.42, spec/r142 sections 2-3).

Pure: stdlib and pydantic only. `decide_cap` is called inside the reservation transaction with the
balances read there, so the decision and the reservation see the same money.

The cap formula (round down to whole tokens, always):

- no role cap and no task cap: `effective = requested` (nothing else happens);
- `cfg = min(requested, role cap, task cap)` over those present;
- no applicable budget: `effective = cfg`;
- `W(cfg) <= A` (A: the smallest available over the applicable budgets, a blocked one counting 0):
  `effective = cfg`, the budget does not bind;
- otherwise the budget binds: `N = A * 10**6 - P * I`, `effective = N // O` (O: output rate),
  or `NoRoom` when O is 0, N < 0 or the quotient is below 1.

Arithmetic is exact `Decimal` (precision 120, traps on Inexact and Rounded). No float.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal, Inexact, InvalidOperation, Rounded, localcontext
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from nuramesh.core.budgets import BudgetBalance
from nuramesh.core.money import CurrencyMismatchError, Money
from nuramesh.core.prices import ResolvedPrice, worst_case_cost


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CapBinding(StrEnum):
    """Which limit set the effective size."""

    TASK = "task"
    ROLE = "role"
    BUDGET = "budget"


class OutputCap(_Frozen):
    """The decision: what the provider gets."""

    requested: int
    effective: int
    role_cap: int | None
    task_cap: int | None
    budget_cap: int | None
    binding: CapBinding | None
    limiting_budget_id: str | None
    available: Money | None

    @property
    def lowered(self) -> bool:
        """The call was made smaller than the caller asked."""
        return self.effective < self.requested


class NoRoom(_Frozen):
    """The budget pays for fewer than 1 output token: the caller keeps the configured size
    (`configured`, with its `binding`) and is refused as today."""

    requested: int
    configured: int
    role_cap: int | None
    task_cap: int | None
    binding: CapBinding | None

    @property
    def lowered(self) -> bool:
        """A configured cap lowered the request below what was asked."""
        return self.configured < self.requested


@contextmanager
def _exact() -> Iterator[None]:
    with localcontext() as ctx:
        ctx.prec = 120
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        ctx.traps[InvalidOperation] = True
        yield


def configured_size(requested: int, role_cap: int | None, task_cap: int | None) -> int:
    """`min(requested, role cap, task cap)` over those present."""
    return min(v for v in (requested, role_cap, task_cap) if v is not None)


def _label(
    requested: int, effective: int, role_cap: int | None, task_cap: int | None
) -> CapBinding | None:
    if effective >= requested:
        return None
    return CapBinding.TASK if task_cap == effective else CapBinding.ROLE


def decide_cap(
    requested: int,
    role_cap: int | None,
    task_cap: int | None,
    budgets: Sequence[BudgetBalance],
    prompt_tokens: int,
    price: ResolvedPrice,
) -> OutputCap | NoRoom:
    """Decide the effective output size (module docstring). Never raises for an empty budget list.

    Raises `CurrencyMismatchError` when a budget is in another currency than the price."""
    for name, v in (("requested", requested), ("role_cap", role_cap), ("task_cap", task_cap)):
        if v is not None and (type(v) is not int or v < 1):
            raise ValueError(f"{name} must be an integer of 1 or more")
    cfg = configured_size(requested, role_cap, task_cap)
    label = _label(requested, cfg, role_cap, task_cap)
    if role_cap is None and task_cap is None:
        return OutputCap(
            requested=requested,
            effective=requested,
            role_cap=None,
            task_cap=None,
            budget_cap=None,
            binding=None,
            limiting_budget_id=None,
            available=None,
        )

    def fits(effective: int, binding: CapBinding | None) -> OutputCap:
        return OutputCap(
            requested=requested,
            effective=effective,
            role_cap=role_cap,
            task_cap=task_cap,
            budget_cap=None,
            binding=binding,
            limiting_budget_id=None,
            available=None,
        )

    if not budgets:
        return fits(cfg, label)
    zero = Money.zero(price.currency)
    limiting: BudgetBalance | None = None
    limiting_available = zero
    for balance in sorted(budgets, key=lambda b: b.budget.id):
        if balance.budget.limit.currency != price.currency:
            raise CurrencyMismatchError(f"{balance.budget.limit.currency} vs {price.currency}")
        available = zero if balance.blocked else balance.available
        if limiting is None or available < limiting_available:  # ties: the lowest id (sorted)
            limiting, limiting_available = balance, available
    assert limiting is not None
    if worst_case_cost(prompt_tokens, cfg, price) <= limiting_available:
        return fits(cfg, label)
    no_room = NoRoom(
        requested=requested, configured=cfg, role_cap=role_cap, task_cap=task_cap, binding=label
    )
    out_rate = price.output_per_million
    if out_rate == 0:
        return no_room
    in_rate = price.input_per_million
    if price.cache_write_per_million is not None:
        in_rate = max(in_rate, price.cache_write_per_million)
    if price.cached_input_per_million is not None:
        in_rate = max(in_rate, price.cached_input_per_million)
    with _exact():
        n = limiting_available.amount.scaleb(6) - Decimal(prompt_tokens) * in_rate
        if n < 0:
            return no_room
        q = int(n // out_rate)
    if q < 1:
        return no_room
    return OutputCap(
        requested=requested,
        effective=q,
        role_cap=role_cap,
        task_cap=task_cap,
        budget_cap=q,
        binding=CapBinding.BUDGET,
        limiting_budget_id=limiting.budget.id,
        available=limiting_available,
    )
