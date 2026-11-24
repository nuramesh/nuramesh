"""Pins for core/budgets.py behaviour that mutation testing found unguarded (tests only)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.budgets import (
    NO_ORG,
    BreachMode,
    Budget,
    BudgetBalance,
    BudgetOrg,
    BudgetScope,
    BudgetWindow,
    CallContext,
    ClosedWindow,
    Reservation,
    adjust,
    as_of,
    by_id,
    org_key,
    release,
    reserve,
    roll,
    settle,
    unblock,
    window_start,
)
from nuramesh.core.money import Money

M = Money.of
DAY = BudgetWindow.DAY
D23 = datetime(2026, 10, 23, tzinfo=UTC)
D24 = datetime(2026, 10, 24, tzinfo=UTC)
D25 = datetime(2026, 10, 25, tzinfo=UTC)


def daily(limit: str = "30", mode: BreachMode | None = None) -> Budget:
    extra: dict[str, Any] = {} if mode is None else {"on_breach": mode}
    return Budget(
        id="b1",
        org_id="acme",
        scope=BudgetScope.ORG,
        scope_id="acme",
        window=DAY,
        limit=M(limit),
        **extra,
    )


def lifetime(limit: str = "30") -> Budget:
    return Budget(id="l1", org_id="acme", scope=BudgetScope.ORG, scope_id="acme", limit=M(limit))


def _across_roll(
    budget: Budget, spent: str = "0", reserved: str = "5", blocked: bool = False
) -> tuple[BudgetBalance, dict[str, datetime]]:
    """A balance whose one reservation was made on day 23 and is settled on day 24."""
    old = BudgetBalance.fresh(budget, D23).model_copy(
        update={"spent": M(spent), "reserved": M(reserved), "open_count": 1, "blocked": blocked}
    )  # built directly: a hard stop would refuse this reservation, but the state can arise
    rolled = roll(old, D24)
    assert rolled is not None
    return rolled.balance, {budget.id: D23}


# -- organization keys ---------------


def test_org_key_maps_none_to_the_empty_key_and_keeps_a_real_org() -> None:
    assert NO_ORG == ""
    assert org_key(None) == NO_ORG
    assert org_key("acme") == "acme"
    assert org_key("") == ""


def _reservation(**kw: Any) -> Reservation:
    base: dict[str, Any] = {
        "id": "r1",
        "org_id": "acme",
        "context": CallContext(run_id="run", task_id="t", agent_id="a"),
        "budget_ids": ("b1", "b2"),
        "amount": M("1"),
    }
    return Reservation(**(base | kw))


def test_reservation_org_of_prefers_the_per_budget_owner_then_the_reservation_org() -> None:
    res = _reservation(budget_orgs=(BudgetOrg(budget_id="b2", org_id="other"),))
    assert res.org_of("b2") == "other"
    assert res.org_of("b1") == "acme"  # not listed: the reservation's own org
    unattributed = _reservation(budget_orgs=(BudgetOrg(budget_id="b2", org_id=None),))
    assert unattributed.org_of("b2") is None  # an explicit None wins over the org
    assert _reservation(org_id=None).org_of("b1") is None


# -- balance validation ---------------


def test_a_balance_refuses_other_currency_and_negative_amounts() -> None:
    budget = lifetime()
    zero = Money.zero()
    with pytest.raises(ValidationError, match="can't be negative"):
        BudgetBalance(budget=budget, spent=M("-1"), reserved=zero)
    with pytest.raises(ValidationError, match="can't be negative"):
        BudgetBalance(budget=budget, spent=zero, reserved=M("-1"))


def test_a_lifetime_balance_refuses_any_window_field() -> None:
    zero = Money.zero()
    closed = ClosedWindow(start=D23, spent=zero, reserved=zero)
    for extra in ({"window_start": D24}, {"closed": (closed,)}, {"open_count": 1}):
        with pytest.raises(ValidationError, match="lifetime budget has no window"):
            BudgetBalance(budget=lifetime(), spent=zero, reserved=zero, **extra)


def test_a_windowed_balance_refuses_a_negative_open_count() -> None:
    zero = Money.zero()
    with pytest.raises(ValidationError, match="open reservations can't be negative"):
        BudgetBalance(budget=daily(), spent=zero, reserved=zero, window_start=D24, open_count=-1)


def _with_closed(*closed: ClosedWindow) -> BudgetBalance:
    zero = Money.zero()
    return BudgetBalance(
        budget=daily(), spent=zero, reserved=zero, window_start=D25, closed=tuple(closed)
    )


def test_closed_windows_must_be_strictly_ordered_unique_and_older() -> None:
    zero = Money.zero()
    c23 = ClosedWindow(start=D23, spent=zero, reserved=zero)
    c24 = ClosedWindow(start=D24, spent=zero, reserved=zero)
    assert _with_closed(c23, c24).closed == (c23, c24)
    with pytest.raises(ValidationError, match="older than the current"):
        _with_closed(c24, c23)  # not oldest first
    with pytest.raises(ValidationError, match="older than the current"):
        _with_closed(c23, c23)  # duplicate
    with pytest.raises(ValidationError, match="older than the current"):
        _with_closed(ClosedWindow(start=D25, spent=zero, reserved=zero))  # not older


def test_every_closed_window_is_checked_not_just_the_first() -> None:
    zero = Money.zero()
    good = ClosedWindow(start=D23, spent=zero, reserved=zero)
    odd = datetime(2026, 10, 24, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="exact start of its window"):
        _with_closed(good, ClosedWindow(start=odd, spent=zero, reserved=zero))
    with pytest.raises(ValidationError, match="only while a reservation"):
        _with_closed(good, ClosedWindow(start=D24, spent=zero, reserved=zero, open=0))
    with pytest.raises(ValidationError, match="can't be negative"):
        _with_closed(good, ClosedWindow(start=D24, spent=M("-1"), reserved=zero))
    with pytest.raises(ValidationError, match="can't be negative"):
        _with_closed(good, ClosedWindow(start=D24, spent=zero, reserved=M("-1")))


# -- reserve, settle, release in the current window ---------------


def test_reserve_counts_open_reservations_only_for_windowed_budgets() -> None:
    win = BudgetBalance.fresh(daily(), D23)
    [once] = reserve([win], M("2"))
    assert once.open_count == 1
    assert reserve([once], M("0"))[0].open_count == 2  # a zero amount still counts
    assert reserve([BudgetBalance.fresh(lifetime())], M("2"))[0].open_count == 0
    [settled] = settle([once], M("2"), M("2"))
    assert settled.balance.open_count == 0
    twice = reserve(reserve([win], M("1")), M("1"))[0]
    [back] = release([twice], M("1"))
    assert (back.open_count, back.reserved) == (1, M("1"))
    [floor] = release([reserve([win], M("1"))[0].model_copy(update={"open_count": 0})], M("1"))
    assert floor.open_count == 0  # never below zero


def test_settling_exactly_at_the_limit_does_not_block_but_one_over_does() -> None:
    held = reserve([BudgetBalance.fresh(lifetime("10"), None)], M("10"))[0]
    [at_limit] = settle([held], M("10"), M("10"))
    assert not at_limit.balance.blocked and not at_limit.became_overspent
    assert not at_limit.blocked_now
    [over] = settle([held], M("10"), M("10.01"))
    assert over.balance.blocked and over.became_overspent and over.blocked_now
    assert over.overrun == M("0.01") and over.released == M("0")
    assert (over.spent, over.available) == (M("10.01"), M("-0.01"))


def test_blocked_now_is_false_when_the_budget_was_already_blocked() -> None:
    held = reserve([BudgetBalance.fresh(lifetime("10"), None)], M("5"))[0]
    already = held.model_copy(update={"blocked": True})
    [result] = settle([already], M("5"), M("20"))
    assert result.balance.blocked and not result.blocked_now
    [fresh] = settle([held], M("5"), M("20"))
    assert fresh.blocked_now


def test_settle_below_reservation_releases_and_never_reports_overrun() -> None:
    held = reserve([BudgetBalance.fresh(lifetime("10"), None)], M("5"))[0]
    [less] = settle([held], M("5"), M("3"))
    assert (less.released, less.overrun) == (M("2"), M("0"))
    [more] = settle([held], M("5"), M("8"))
    assert (more.released, more.overrun) == (M("0"), M("3"))
    [same] = settle([held], M("5"), M("5"))
    assert (same.released, same.overrun) == (M("0"), M("0"))
    with pytest.raises(ValueError, match="can't be negative"):
        settle([held], M("5"), M("-1"))
    with pytest.raises(ValueError, match="doesn't hold"):
        settle([held], M("6"), M("1"))
    assert settle([held], M("5"), M("0"))[0].balance.reserved == M("0")


def test_release_refuses_more_than_is_reserved_and_zero_is_allowed() -> None:
    held = reserve([BudgetBalance.fresh(lifetime("10"), None)], M("5"))[0]
    with pytest.raises(ValueError, match="doesn't hold"):
        release([held], M("5.01"))
    assert release([held], M("5"))[0].reserved == M("0")


# -- closed-window settle and release --------------------------------------------------------------


def test_a_closed_window_settle_reports_that_windows_numbers() -> None:
    # 20 spent + 5 held on day 23, plus another 3 still held there
    budget = daily("30")
    old = BudgetBalance.fresh(budget, D23).model_copy(update={"spent": M("20")})
    old = reserve(reserve([old], M("5")), M("3"))[0]
    rolled = roll(old, D24)
    assert rolled is not None
    win = {"b1": D23}
    [result] = settle([rolled.balance], M("5"), M("4"), win)
    assert result.closed_window and result.window_start == D23
    assert result.spent == M("24")
    assert result.available == M("3")  # 30 - 24 spent - 3 still reserved there
    assert result.released == M("1") and result.overrun == M("0")
    assert result.balance.closed == (ClosedWindow(start=D23, spent=M("24"), reserved=M("3")),)
    assert result.balance.spent == M("0") and result.balance.window_start == D24
    assert not result.became_overspent and not result.blocked_now


def test_a_closed_window_exactly_at_the_limit_is_not_overspent() -> None:
    balance, win = _across_roll(daily("10"), spent="5", reserved="5")
    [result] = settle([balance], M("5"), M("5"), win)
    assert not result.became_overspent and result.overrun == M("0")
    assert result.spent == M("10") and result.available == M("0")


def test_a_hold_budget_blocks_on_a_closed_window_breach_once() -> None:
    hold = daily("10", BreachMode.HOLD)
    balance, win = _across_roll(hold, spent="8", reserved="5")
    [result] = settle([balance], M("5"), M("5"), win)
    assert result.became_overspent and result.blocked_now and result.balance.blocked
    # already blocked: still blocked, but not newly so
    blocked = balance.model_copy(update={"blocked": True})
    [again] = settle([blocked], M("5"), M("5"), win)
    assert again.balance.blocked and again.became_overspent and not again.blocked_now
    # already over before this settlement: no new breach, no new block
    over_before, win2 = _across_roll(hold, spent="12", reserved="1")
    [late] = settle([over_before], M("1"), M("1"), win2)
    assert not late.became_overspent and not late.blocked_now and not late.balance.blocked


def test_a_reset_budget_never_blocks_from_a_closed_window_breach() -> None:
    balance, win = _across_roll(daily("10", BreachMode.RESET), spent="8", reserved="5")
    [result] = settle([balance], M("5"), M("5"), win)
    assert result.became_overspent and not result.blocked_now and not result.balance.blocked


def test_release_in_a_closed_window_gives_back_to_that_window_only() -> None:
    budget = daily("30")
    old = BudgetBalance.fresh(budget, D23)
    old = reserve(reserve([old], M("5")), M("3"))[0]
    rolled = roll(old, D24)
    assert rolled is not None
    [after] = release([rolled.balance], M("5"), {"b1": D23})
    assert after.closed == (ClosedWindow(start=D23, spent=M("0"), reserved=M("3")),)
    assert (after.spent, after.reserved, after.window_start) == (M("0"), M("0"), D24)
    with pytest.raises(ValueError, match="doesn't hold"):
        release([rolled.balance], M("8.01"), {"b1": D23})  # more than that window holds
    with pytest.raises(ValueError, match="doesn't hold"):
        settle([rolled.balance], M("8.01"), M("1"), {"b1": D23})


def test_a_window_start_after_the_current_one_is_refused_but_the_current_one_is_not() -> None:
    balance = reserve([BudgetBalance.fresh(daily(), D24)], M("1"))[0]
    with pytest.raises(ValueError, match="no window starting at"):
        settle([balance], M("1"), M("1"), {"b1": D25})
    with pytest.raises(ValueError, match="no window starting at"):
        release([balance], M("1"), {"b1": D25})
    assert settle([balance], M("1"), M("1"), {"b1": D24})[0].window_start == D24
    assert settle([balance], M("1"), M("1"), {})[0].window_start == D24  # no entry: current
    # a windows map naming another budget does not touch this one
    assert not settle([balance], M("1"), M("1"), {"other": D23})[0].closed_window


# -- adjust and unblock ---------------


def test_adjust_blocks_only_when_it_pushes_spending_over_the_limit() -> None:
    base = BudgetBalance.fresh(lifetime("10"), None).model_copy(update={"spent": M("6")})
    [exact] = adjust([base], M("4"))
    assert not exact.breached and not exact.blocked_now and not exact.balance.blocked
    assert exact.balance.spent == M("10") and exact.amount == M("4")
    [over] = adjust([base], M("4.01"))
    assert over.breached and over.blocked_now and over.balance.blocked
    [again] = adjust([base.model_copy(update={"blocked": True})], M("4.01"))
    assert again.breached and not again.blocked_now and again.balance.blocked
    with pytest.raises(ValueError, match="can't be negative"):
        adjust([base], M("-0.01"))
    assert adjust([base], M("0"))[0].balance.spent == M("6")


def test_unblock_needs_a_block_a_higher_limit_in_the_same_currency() -> None:
    free = BudgetBalance.fresh(lifetime("10"), None)
    with pytest.raises(ValueError, match="isn't blocked"):
        unblock(free)
    blocked = free.model_copy(update={"blocked": True})
    assert unblock(blocked).blocked is False
    with pytest.raises(ValueError, match="higher"):
        unblock(blocked, M("10"))
    assert unblock(blocked, M("10.01")).budget.limit == M("10.01")
    over = free.model_copy(update={"spent": M("12"), "blocked": True})
    with pytest.raises(ValueError, match="still over its limit"):
        unblock(over)
    assert unblock(over, M("12")).blocked is False  # covers what was spent
    legacy = free.model_copy(update={"spent": M("12")})  # over without the flag
    assert unblock(legacy, M("12")).blocked is False


# -- roll, as_of, by_id ---------------


def test_roll_records_the_closed_window_only_while_a_reservation_names_it() -> None:
    quiet = BudgetBalance.fresh(daily(), D23).model_copy(update={"spent": M("4")})
    r = roll(quiet, D24)
    assert r is not None and r.balance.closed == () and r.open == 0
    assert (r.closed_start, r.closed_end) == (D23, D24)
    assert (r.spent, r.reserved) == (M("4"), M("0"))
    busy = reserve([quiet], M("2"))[0]
    r = roll(busy, D25)
    assert r is not None and r.open == 1 and r.reserved == M("2")
    assert r.balance.closed == (ClosedWindow(start=D23, spent=M("4"), reserved=M("2"), open=1),)
    assert (r.closed_start, r.closed_end) == (D23, D24)  # the skipped day is not closed over
    assert r.balance.window_start == D25 and r.balance.open_count == 0
    zero_hold = reserve([quiet], M("0"))[0]
    z = roll(zero_hold, D24)
    assert z is not None and len(z.balance.closed) == 1 and z.balance.closed[0].open == 1


def test_roll_keeps_older_closed_windows_in_order() -> None:
    first = reserve([BudgetBalance.fresh(daily(), D23)], M("1"))[0]
    r1 = roll(first, D24)
    assert r1 is not None
    second = reserve([r1.balance], M("1"))[0]
    r2 = roll(second, D25)
    assert r2 is not None
    assert [c.start for c in r2.balance.closed] == [D23, D24]


def test_roll_is_a_noop_at_the_boundary_before_it_and_for_lifetime_budgets() -> None:
    balance = BudgetBalance.fresh(daily(), D24)
    assert roll(balance, D24) is None  # the start instant belongs to the current window
    assert roll(balance, datetime(2026, 10, 24, 23, 59, 59, 999999, tzinfo=UTC)) is None
    assert roll(balance, D23) is None  # a clock that went backwards
    assert roll(balance, D25) is not None
    assert roll(BudgetBalance.fresh(lifetime(), None), D25) is None


def test_roll_clears_or_holds_a_block_and_reports_which() -> None:
    reset = BudgetBalance.fresh(daily(mode=BreachMode.RESET), D23).model_copy(
        update={"blocked": True}
    )
    r = roll(reset, D24)
    assert r is not None and r.cleared_block and not r.held_block and not r.balance.blocked
    unblocked = roll(BudgetBalance.fresh(daily(mode=BreachMode.RESET), D23), D24)
    assert unblocked is not None and not unblocked.cleared_block and not unblocked.held_block
    hold = BudgetBalance.fresh(daily(mode=BreachMode.HOLD), D23)
    quiet = roll(hold, D24)
    assert quiet is not None and not quiet.held_block and not quiet.balance.blocked
    held = roll(hold.model_copy(update={"blocked": True}), D24)
    assert held is not None and held.held_block and not held.cleared_block


def test_as_of_rolls_only_when_the_window_ended_and_by_id_keys_by_budget() -> None:
    balance = BudgetBalance.fresh(daily(), D23).model_copy(update={"spent": M("4")})
    assert as_of(balance, D23) is balance
    rolled = as_of(balance, D24)
    assert rolled.window_start == D24 and rolled.spent == M("0")
    other = BudgetBalance.fresh(lifetime(), None)
    keyed = by_id([balance, other])
    assert dict(keyed) == {"b1": balance, "l1": other}
    assert by_id([]) == {}


# --- round 3 pins (farm survivors of the second mutation pass) ---


def test_models_are_frozen() -> None:
    with pytest.raises(ValidationError):
        daily().limit = M("1")


def test_timezone_with_no_offset_is_refused() -> None:
    from datetime import tzinfo

    class _NoOffset(tzinfo):
        def utcoffset(self, dt: datetime | None) -> None:
            return None

        def dst(self, dt: datetime | None) -> None:
            return None

        def tzname(self, dt: datetime | None) -> str:
            return "none"

    with pytest.raises(ValueError, match="timezone-aware"):
        window_start(DAY, datetime(2026, 10, 23, tzinfo=_NoOffset()))


def test_task_max_tokens_is_strict_and_at_least_one() -> None:
    ok = CallContext(run_id="r1", agent_id="a1", task_max_tokens=1)
    assert ok.task_max_tokens == 1
    with pytest.raises(ValidationError):
        CallContext(run_id="r1", agent_id="a1", task_max_tokens="5")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        CallContext(run_id="r1", agent_id="a1", task_max_tokens=0)


def test_cap_exempt_defaults_false_and_dump_omits_unset_fields() -> None:
    plain = CallContext(run_id="r1", agent_id="a1")
    assert plain.cap_exempt is False
    dumped = plain.model_dump()
    assert dumped is not None
    assert "task_max_tokens" not in dumped
    assert "cap_exempt" not in dumped
    full = CallContext(run_id="r1", agent_id="a1", task_max_tokens=7, cap_exempt=True).model_dump()
    assert full["task_max_tokens"] == 7
    assert full["cap_exempt"] is True


def test_outcome_flags_default_false() -> None:
    from nuramesh.core.budgets import Rollover, Settlement, Shortfall

    assert Shortfall.model_fields["blocked"].default is False
    assert Shortfall.model_fields["held"].default is False
    assert Settlement.model_fields["blocked_now"].default is False
    assert Rollover.model_fields["held_block"].default is False
