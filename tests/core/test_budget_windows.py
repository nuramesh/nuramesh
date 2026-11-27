"""Time-window budgets and the org scope: the pure rules (T56)."""

import random
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nuramesh.core.budgets import (
    Budget,
    BudgetBalance,
    BudgetScope,
    BudgetWindow,
    CallContext,
    ClosedWindow,
    Reservation,
    ReservedWindow,
    applicable_budgets,
    as_of,
    release,
    reserve,
    roll,
    settle,
    shortfalls,
    unblock,
    window_end,
    window_start,
)
from nuramesh.core.money import Money
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind

M = Money.of
DAY, MONTH, NONE = BudgetWindow.DAY, BudgetWindow.MONTH, BudgetWindow.NONE


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, micro: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=UTC)


ORG = OrgGraph(
    id="acme",
    name="Acme",
    roles=(Role(id="cos", name="CoS", kind=RoleKind.CHIEF_OF_STAFF), Role(id="w", name="W")),
    agents=(
        Agent(id="boss", name="Boss", role="cos"),
        Agent(id="dev", name="Dev", role="w", reports_to="boss"),
    ),
)


def org_daily(limit: str = "30") -> Budget:
    return Budget(
        id="org-daily",
        org_id="acme",
        scope=BudgetScope.ORG,
        scope_id="acme",
        window=DAY,
        limit=M(limit),
    )


# -- window arithmetic ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("window", "at", "start", "end"),
    [
        (DAY, utc(2026, 10, 23, 13, 5, 7, 123456), utc(2026, 10, 23), utc(2026, 10, 24)),
        (
            DAY,
            utc(2026, 10, 23, 0, 0, 0),
            utc(2026, 10, 23),
            utc(2026, 10, 24),
        ),  # midnight: new day
        (DAY, utc(2026, 10, 23, 23, 59, 59, 999999), utc(2026, 10, 23), utc(2026, 10, 24)),
        (DAY, utc(2026, 12, 31, 23, 0), utc(2026, 12, 31), utc(2027, 1, 1)),
        (DAY, utc(2028, 2, 28, 12), utc(2028, 2, 28), utc(2028, 2, 29)),  # leap year
        (DAY, utc(2028, 2, 29, 12), utc(2028, 2, 29), utc(2028, 3, 1)),
        (DAY, utc(2027, 2, 28, 12), utc(2027, 2, 28), utc(2027, 3, 1)),  # not a leap year
        (MONTH, utc(2026, 10, 23, 13), utc(2026, 10, 1), utc(2026, 11, 1)),
        (MONTH, utc(2026, 10, 1, 0, 0, 0), utc(2026, 10, 1), utc(2026, 11, 1)),
        (MONTH, utc(2026, 10, 31, 23, 59, 59, 999999), utc(2026, 10, 1), utc(2026, 11, 1)),
        (MONTH, utc(2026, 12, 15), utc(2026, 12, 1), utc(2027, 1, 1)),  # New Year
        (MONTH, utc(2028, 2, 29, 23), utc(2028, 2, 1), utc(2028, 3, 1)),  # leap February
        (MONTH, utc(2027, 2, 28, 23), utc(2027, 2, 1), utc(2027, 3, 1)),
        (MONTH, utc(2026, 1, 31, 23, 59, 59), utc(2026, 1, 1), utc(2026, 2, 1)),
        (MONTH, utc(2026, 3, 31), utc(2026, 3, 1), utc(2026, 4, 1)),
        (MONTH, utc(2026, 4, 30), utc(2026, 4, 1), utc(2026, 5, 1)),
    ],
)
def test_window_start_and_end(
    window: BudgetWindow, at: datetime, start: datetime, end: datetime
) -> None:
    assert window_start(window, at) == start
    assert window_end(window, start) == end


def test_offsets_are_converted_to_utc() -> None:
    minus5 = timezone(timedelta(hours=-5))
    plus2 = timezone(timedelta(hours=2))
    assert window_start(DAY, datetime(2026, 10, 23, 18, 59, 59, tzinfo=minus5)) == utc(2026, 10, 23)
    assert window_start(DAY, datetime(2026, 10, 23, 19, 0, 0, tzinfo=minus5)) == utc(2026, 10, 24)
    assert window_start(DAY, datetime(2026, 10, 24, 1, 59, 59, tzinfo=plus2)) == utc(2026, 10, 23)
    assert window_start(MONTH, datetime(2026, 10, 31, 20, 0, tzinfo=minus5)) == utc(2026, 11, 1)
    assert window_start(DAY, datetime(2026, 10, 23, tzinfo=minus5)).tzinfo is UTC  # type: ignore[union-attr]


def test_a_naive_time_is_refused_and_a_lifetime_budget_has_no_window() -> None:
    with pytest.raises(ValueError, match="timezone"):
        window_start(DAY, datetime(2026, 10, 23))
    assert window_start(NONE, utc(2026, 10, 23)) is None
    with pytest.raises(ValueError):
        window_end(NONE, utc(2026, 10, 23))


def test_every_window_of_four_years_is_contiguous() -> None:
    at = utc(2027, 1, 1)
    for window in (DAY, MONTH):
        start = window_start(window, at)
        assert start is not None
        for _ in range(1500 if window is DAY else 48):
            end = window_end(window, start)
            assert end > start and window_start(window, end) == end
            assert window_start(window, end - timedelta(microseconds=1)) == start
            start = end


# -- models --------------------------------------------------------------------------------------


def test_a_budget_is_a_lifetime_budget_unless_a_window_is_given() -> None:
    plain = Budget(id="b", scope=BudgetScope.RUN, scope_id="r1", limit=M("1"))
    assert plain.window is NONE
    assert (
        Budget.model_validate(
            {"id": "b", "scope": "org", "scope_id": "acme", "limit": {"amount": "1"}}
        ).window
        is NONE
    )
    assert (
        Budget.model_validate(
            {
                "id": "b",
                "scope": "org",
                "scope_id": "acme",
                "limit": {"amount": "1"},
                "window": "month",
            }
        ).window
        is MONTH
    )
    with pytest.raises(ValidationError):
        Budget.model_validate(
            {
                "id": "b",
                "scope": "org",
                "scope_id": "acme",
                "limit": {"amount": "1"},
                "window": "week",
            }
        )


def test_money_in_a_budget_is_never_a_float() -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate(
            {
                "id": "b",
                "scope": "org",
                "scope_id": "acme",
                "window": "day",
                "limit": {"amount": 30.0},
            }
        )


def test_a_balance_stored_before_windows_existed_still_loads_unchanged() -> None:
    old = (
        '{"budget":{"id":"run","scope":"run","scope_id":"r1","limit":{"amount":"1","currency":"USD"}},'
        '"spent":{"amount":"0.2","currency":"USD"},"reserved":{"amount":"0","currency":"USD"},"blocked":false}'
    )
    balance = BudgetBalance.model_validate_json(old)
    assert balance.budget.window is NONE and balance.window_start is None and balance.closed == ()
    assert balance.available == M("0.8")
    old_reservation = (
        '{"id":"x","context":{"run_id":"r1","agent_id":"dev"},"budget_ids":["run"],'
        '"amount":{"amount":"0.1","currency":"USD"},"status":"open","charged":null}'
    )
    assert Reservation.model_validate_json(old_reservation).windows == ()


def test_balance_round_trips_through_json() -> None:
    balance = BudgetBalance.fresh(org_daily(), utc(2026, 10, 23, 5))
    rolled = roll(reserve([balance], M("5"))[0], utc(2026, 10, 24, 1))
    assert rolled is not None
    again = BudgetBalance.model_validate_json(rolled.balance.model_dump_json())
    assert again == rolled.balance and again.closed[0].reserved == M("5")


def test_a_windowed_balance_needs_the_exact_window_start() -> None:
    budget = org_daily()
    zero = Money.zero()
    with pytest.raises(ValidationError):
        BudgetBalance(budget=budget, spent=zero, reserved=zero)
    with pytest.raises(ValidationError):
        BudgetBalance(budget=budget, spent=zero, reserved=zero, window_start=utc(2026, 10, 23, 1))
    with pytest.raises(ValueError, match="needs the current time"):
        BudgetBalance.fresh(budget)
    with pytest.raises(ValidationError):  # a lifetime budget has no window
        BudgetBalance(
            budget=budget.model_copy(update={"window": NONE}),
            spent=zero,
            reserved=zero,
            window_start=utc(2026, 10, 23),
        )
    with pytest.raises(ValidationError):  # a closed window must be older
        BudgetBalance(
            budget=budget,
            spent=zero,
            reserved=zero,
            window_start=utc(2026, 10, 23),
            closed=(ClosedWindow(start=utc(2026, 10, 23), spent=zero, reserved=zero),),
        )
    assert BudgetBalance.fresh(budget, utc(2026, 10, 23, 17)).window_start == utc(2026, 10, 23)


def test_a_reservation_names_each_window_at_most_once() -> None:
    ctx = CallContext(run_id="r1", agent_id="dev")
    window = ReservedWindow(budget_id="org-daily", start=utc(2026, 10, 23))
    with pytest.raises(ValidationError):
        Reservation(
            id="x", context=ctx, budget_ids=("org-daily",), amount=M("1"), windows=(window, window)
        )
    with pytest.raises(ValidationError):
        Reservation(id="x", context=ctx, budget_ids=("other",), amount=M("1"), windows=(window,))
    assert Reservation(
        id="x", context=ctx, budget_ids=("org-daily",), amount=M("1"), windows=(window,)
    ).window_map == {"org-daily": utc(2026, 10, 23)}


# -- the org scope -------------------------------------------------------------------------------


def test_an_org_budget_applies_to_every_call_of_that_org_only() -> None:
    budgets = (
        org_daily(),
        Budget(
            id="other-org", org_id="acme", scope=BudgetScope.ORG, scope_id="elsewhere", limit=M("5")
        ),
        Budget(id="run", org_id="acme", scope=BudgetScope.RUN, scope_id="r1", limit=M("5")),
        # another organization's org budget naming ours applies to nothing of ours (ND90)
        Budget(
            id="foreign-org", org_id="globex", scope=BudgetScope.ORG, scope_id="acme", limit=M("5")
        ),
    )
    for ctx in (
        CallContext(run_id="r1", task_id="t1", agent_id="dev"),
        CallContext(run_id="r2", task_id=None, agent_id="boss"),
    ):
        assert "org-daily" in [b.id for b in applicable_budgets(budgets, ctx, ORG)]
        assert "other-org" not in [b.id for b in applicable_budgets(budgets, ctx, ORG)]
    ids = [b.id for b in applicable_budgets(budgets, CallContext(run_id="r2", agent_id="dev"), ORG)]
    assert ids == ["org-daily"]


# -- rollover ------------------------------------------------------------------------------------


def test_roll_starts_a_fresh_window_and_clears_a_block() -> None:
    balance = BudgetBalance.fresh(org_daily(), utc(2026, 10, 23, 8))
    balance = balance.model_copy(update={"spent": M("35"), "blocked": True})
    rolled = roll(balance, utc(2026, 10, 24))
    assert rolled is not None
    assert (rolled.closed_start, rolled.closed_end) == (utc(2026, 10, 23), utc(2026, 10, 24))
    assert (rolled.spent, rolled.reserved, rolled.cleared_block) == (M("35"), M("0"), True)
    new = rolled.balance
    assert (new.spent, new.reserved, new.blocked, new.window_start) == (
        M("0"),
        M("0"),
        False,
        utc(2026, 10, 24),
    )
    assert new.closed == () and new.budget == balance.budget


def test_roll_does_nothing_inside_the_window_and_never_goes_back() -> None:
    balance = BudgetBalance.fresh(org_daily(), utc(2026, 10, 23, 8))
    assert roll(balance, utc(2026, 10, 23, 23, 59, 59, 999999)) is None
    assert roll(balance, utc(2026, 10, 23)) is None
    assert roll(balance, utc(2026, 10, 22, 23)) is None  # the clock went backwards
    assert as_of(balance, utc(2026, 10, 22)) == balance
    lifetime = BudgetBalance.fresh(
        Budget(id="b", scope=BudgetScope.RUN, scope_id="r1", limit=M("1"))
    )
    assert roll(lifetime, utc(2099, 1, 1)) is None


def test_roll_skips_windows_nobody_used_and_keeps_open_reservations_with_the_old_window() -> None:
    balance = BudgetBalance.fresh(org_daily(), utc(2026, 10, 23, 8))
    balance = reserve([balance], M("7"))[0]
    rolled = roll(balance, utc(2026, 10, 27, 12))
    assert rolled is not None
    assert rolled.balance.window_start == utc(2026, 10, 27)
    assert rolled.reserved == M("7")
    assert rolled.balance.closed == (
        ClosedWindow(start=utc(2026, 10, 23), spent=M("0"), reserved=M("7")),
    )
    # as_of shows the same without storing anything
    assert as_of(balance, utc(2026, 10, 27, 12)) == rolled.balance
    assert balance.window_start == utc(2026, 10, 23)


def test_the_rolled_amounts_are_exact_decimals() -> None:
    balance = BudgetBalance.fresh(org_daily("30.10"), utc(2026, 10, 23))
    for _ in range(3):
        balance = reserve([balance], M("0.1"))[0]
    for held in (M("0.1"),) * 3:
        balance = settle([balance], held, M("0.0000001"))[0].balance
    assert balance.spent == M("0.0000003")  # 3 x 1e-7, no float drift
    rolled = roll(balance, utc(2026, 10, 24))
    assert rolled is not None and rolled.spent.amount == Decimal("0.0000003")
    assert isinstance(rolled.spent.amount, Decimal)


def test_shortfall_of_a_windowed_budget_says_when_it_resets() -> None:
    balance = BudgetBalance.fresh(org_daily("10"), utc(2026, 10, 23))
    [short] = shortfalls([balance], M("11"))
    assert (short.window_start, short.resets_at) == (utc(2026, 10, 23), utc(2026, 10, 24))
    lifetime = BudgetBalance.fresh(
        Budget(id="b", scope=BudgetScope.RUN, scope_id="r1", limit=M("1"))
    )
    [short] = shortfalls([lifetime], M("2"))
    assert short.window_start is None and short.resets_at is None


# -- settling and releasing across a rollover ----------------------------------------------------


def _held_across_a_roll() -> tuple[BudgetBalance, dict[str, datetime]]:
    old = reserve([BudgetBalance.fresh(org_daily(), utc(2026, 10, 23))], M("10"))[0]
    windows = {"org-daily": utc(2026, 10, 23)}
    rolled = roll(old, utc(2026, 10, 24, 1))
    assert rolled is not None
    return reserve([rolled.balance], M("20"))[0], windows


def test_settle_charges_the_window_the_reservation_was_made_in() -> None:
    balance, windows = _held_across_a_roll()
    [result] = settle([balance], M("10"), M("9"), windows)
    assert result.closed_window and result.window_start == utc(2026, 10, 23)
    assert (result.spent, result.available) == (M("9"), M("21"))
    assert result.released == M("1") and result.overrun == M("0")
    now = result.balance
    assert (now.spent, now.reserved, now.window_start) == (M("0"), M("20"), utc(2026, 10, 24))
    assert now.closed == ()  # nothing reserved is left in the closed window


def test_a_closed_window_is_kept_until_its_last_reservation_ends() -> None:
    old = BudgetBalance.fresh(org_daily(), utc(2026, 10, 23))
    old = reserve(reserve([old], M("4")), M("6"))[0]
    rolled = roll(old, utc(2026, 10, 24))
    assert rolled is not None
    windows = {"org-daily": utc(2026, 10, 23)}
    [first] = settle([rolled.balance], M("4"), M("4"), windows)
    assert first.balance.closed == (
        ClosedWindow(start=utc(2026, 10, 23), spent=M("4"), reserved=M("6")),
    )
    [second] = release([first.balance], M("6"), windows)
    assert second.closed == ()


def test_release_returns_money_to_the_closed_window_not_the_current_one() -> None:
    balance, windows = _held_across_a_roll()
    [after] = release([balance], M("10"), windows)
    assert (after.spent, after.reserved) == (M("0"), M("20")) and after.closed == ()


def test_an_overrun_in_a_closed_window_is_flagged_but_does_not_block_the_new_window() -> None:
    old = BudgetBalance.fresh(org_daily("30"), utc(2026, 10, 23)).model_copy(
        update={"spent": M("25")}
    )
    old = reserve([old], M("5"))[0]
    rolled = roll(old, utc(2026, 10, 24))
    assert rolled is not None
    [result] = settle([rolled.balance], M("5"), M("8"), {"org-daily": utc(2026, 10, 23)})
    assert result.closed_window and result.became_overspent and result.overrun == M("3")
    assert result.spent == M("33") and result.available == M("-3")
    assert result.blocked_now is False and result.balance.blocked is False


def test_settling_into_a_window_the_balance_does_not_hold_is_refused() -> None:
    balance = BudgetBalance.fresh(org_daily(), utc(2026, 10, 24))
    with pytest.raises(ValueError, match="doesn't hold"):
        settle([balance], M("1"), M("1"), {"org-daily": utc(2026, 10, 23)})
    with pytest.raises(ValueError):
        settle(
            [balance], M("1"), M("1"), {"org-daily": utc(2026, 10, 25)}
        )  # a window in the future
    with pytest.raises(ValueError, match="doesn't hold"):
        release([balance], M("1"), {"org-daily": utc(2026, 10, 23)})


def test_the_current_window_path_is_the_old_behaviour() -> None:
    balance = reserve([BudgetBalance.fresh(org_daily(), utc(2026, 10, 23))], M("10"))[0]
    [result] = settle([balance], M("10"), M("4"), {"org-daily": utc(2026, 10, 23)})
    assert not result.closed_window
    assert (result.balance.spent, result.balance.reserved) == (M("4"), M("0"))
    assert (result.spent, result.available) == (M("4"), M("26"))
    # without windows, the same
    assert settle([balance], M("10"), M("4"))[0].balance == result.balance


def test_unblock_keeps_the_window() -> None:
    balance = BudgetBalance.fresh(org_daily("30"), utc(2026, 10, 23)).model_copy(
        update={"spent": M("35"), "blocked": True}
    )
    lifted = unblock(balance, M("40"))
    assert (
        not lifted.blocked
        and lifted.window_start == utc(2026, 10, 23)
        and lifted.budget.limit == M("40")
    )


# -- property-style: random histories keep every window inside its limit --------------------------


@pytest.mark.parametrize("seed", range(40))
def test_no_window_ever_holds_more_than_its_limit(seed: int) -> None:
    rng = random.Random(seed)
    window = rng.choice([DAY, MONTH])
    limit = Decimal(rng.randint(5, 40))
    budget = Budget(
        id="b", scope=BudgetScope.ORG, scope_id="acme", window=window, limit=M(str(limit))
    )
    now = utc(2026, 1, 1) + timedelta(hours=rng.randint(0, 2000))
    balance = BudgetBalance.fresh(budget, now)
    open_: dict[int, tuple[Decimal, dict[str, datetime]]] = {}
    spent_in: dict[datetime, Decimal] = {}
    n = 0
    for _ in range(120):
        now += timedelta(minutes=rng.choice([1, 30, 600, 3000, 20000]))
        rolled = roll(balance, now)
        if rolled is not None:
            balance = rolled.balance
        action = rng.choice(["reserve", "reserve", "settle", "release"])
        if action == "reserve":
            amount = Decimal(rng.randint(0, 800)) / 100
            if not shortfalls([balance], M(str(amount))):
                balance = reserve([balance], M(str(amount)))[0]
                n += 1
                assert balance.window_start is not None
                open_[n] = (amount, {"b": balance.window_start})
        elif open_:
            key = rng.choice(sorted(open_))
            amount, windows = open_.pop(key)
            if action == "settle":
                actual = (amount * Decimal(rng.randint(0, 100)) / 100).quantize(Decimal("0.0001"))
                result = settle([balance], M(str(amount)), M(str(actual)), windows)[0]
                balance = result.balance
                spent_in[windows["b"]] = result.spent.amount
                assert not result.became_overspent
            else:
                balance = release([balance], M(str(amount)), windows)[0]
        # every window, current or closed, stays within the limit
        assert balance.spent + balance.reserved <= balance.budget.limit
        for closed in balance.closed:
            assert closed.spent + closed.reserved <= balance.budget.limit
            assert closed.open > 0
        assert not balance.blocked


def test_a_zero_reservation_keeps_its_closed_window_alive() -> None:
    balance = BudgetBalance.fresh(org_daily(), utc(2026, 10, 23))
    held = reserve(reserve([balance], M("0")), M("0"))[0]
    assert held.open_count == 2 and held.reserved == M("0")
    rolled = roll(held, utc(2026, 10, 24))
    assert rolled is not None and rolled.open == 2
    windows = {"org-daily": utc(2026, 10, 23)}
    [first] = settle([rolled.balance], M("0"), M("0"), windows)
    assert first.balance.closed[0].open == 1
    [second] = release([first.balance], M("0"), windows)
    assert second.closed == () and (second.spent, second.reserved) == (M("0"), M("0"))
    # a zero and a non-zero reservation share the closed window; the non-zero one ends first
    mixed = reserve(reserve([balance], M("5")), M("0"))[0]
    rolled = roll(mixed, utc(2026, 10, 24))
    assert rolled is not None
    [paid] = settle([rolled.balance], M("5"), M("5"), windows)
    assert paid.balance.closed == (
        ClosedWindow(start=utc(2026, 10, 23), spent=M("5"), reserved=M("0"), open=1),
    )
    [done] = settle([paid.balance], M("0"), M("0"), windows)
    assert done.balance.closed == () and done.spent == M("5") and done.available == M("25")


def test_a_lifetime_budget_keeps_no_reservation_count() -> None:
    life = BudgetBalance.fresh(Budget(id="b", scope=BudgetScope.RUN, scope_id="r1", limit=M("1")))
    assert reserve([life], M("0.5"))[0].open_count == 0


# -- on_breach: reset or hold ----------------------------------------------------------------------


def test_on_breach_defaults_reset_for_windows_and_hold_without_one() -> None:
    from nuramesh.core.budgets import BreachMode

    assert org_daily().breach_mode is BreachMode.RESET
    month = org_daily().model_copy(update={"window": MONTH})
    assert month.breach_mode is BreachMode.RESET
    held = org_daily().model_copy(update={"on_breach": BreachMode.HOLD})
    assert held.breach_mode is BreachMode.HOLD
    life = Budget(id="b", scope=BudgetScope.RUN, scope_id="r1", limit=M("1"))
    assert life.breach_mode is BreachMode.HOLD and life.on_breach is None
    assert life.model_copy(update={"on_breach": BreachMode.HOLD}).breach_mode is BreachMode.HOLD
    with pytest.raises(ValidationError, match="needs a window"):
        Budget.model_validate(
            {
                "id": "b",
                "scope": "run",
                "scope_id": "r1",
                "limit": {"amount": "1"},
                "on_breach": "reset",
            }
        )
    with pytest.raises(ValidationError):
        Budget.model_validate(
            {
                "id": "b",
                "scope": "run",
                "scope_id": "r1",
                "limit": {"amount": "1"},
                "on_breach": "keep",
            }
        )


def test_a_hold_budget_stays_blocked_through_a_roll() -> None:
    from nuramesh.core.budgets import BreachMode

    budget = org_daily().model_copy(update={"on_breach": BreachMode.HOLD})
    balance = BudgetBalance.fresh(budget, utc(2026, 10, 23)).model_copy(
        update={"spent": M("35"), "blocked": True}
    )
    rolled = roll(balance, utc(2026, 10, 24))
    assert rolled is not None
    assert (rolled.cleared_block, rolled.held_block) == (False, True)
    assert rolled.balance.blocked and rolled.balance.spent == M("0")
    [short] = shortfalls([rolled.balance], M("1"))
    assert short.held and short.blocked and short.on_breach is BreachMode.HOLD
    assert short.resets_at is None and short.spent == M("0")  # only a human lifts it
    assert short.window is DAY and short.window_start == utc(2026, 10, 24)
    lifted = unblock(rolled.balance)  # not over its limit any more: a human lifts it as it is
    assert not lifted.blocked and lifted.window_start == utc(2026, 10, 24)
    reset = roll(balance.model_copy(update={"budget": org_daily()}), utc(2026, 10, 24))
    assert reset is not None and (reset.cleared_block, reset.held_block) == (True, False)
    assert not reset.balance.blocked


def test_a_full_or_reset_budget_still_says_when_it_resets() -> None:
    full = BudgetBalance.fresh(org_daily("10"), utc(2026, 10, 23)).model_copy(
        update={"spent": M("10")}
    )
    [short] = shortfalls([full], M("1"))
    assert not short.held and short.resets_at == utc(2026, 10, 24) and short.spent == M("10")
    blocked = full.model_copy(update={"spent": M("12"), "blocked": True})
    [short] = shortfalls([blocked], M("1"))
    assert short.blocked and not short.held and short.resets_at == utc(2026, 10, 24)
