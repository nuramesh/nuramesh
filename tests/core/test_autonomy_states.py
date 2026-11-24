"""Grant states, demotion and void derivation: spec/autonomy section 9.6 (D cases) and the void
cases G35 to G51 (6.2, 6.3, 6.5). Expected values come from the spec's tables."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from autonomy_world import (
    ORG,
    RUN,
    SUITES,
    T,
    World,
    config_record,
    d,
    event,
)

from nuramesh.core import autonomy as au
from nuramesh.core.autonomy import Entry, GrantState, Settings, grant_states
from nuramesh.core.ledger import LedgerEvent


def states(
    w: World, as_of: datetime = T, settings: Settings | None = None
) -> dict[str, GrantState]:
    return grant_states(
        w.entries, as_of, required_suites=SUITES, settings=settings or au.DEFAULT_SETTINGS
    )


def state_of(w: World, grant: str = "g", as_of: datetime = T) -> str:
    return states(w, as_of)[grant].state


def base(*, grant_at: datetime | None = None, **kw: Any) -> World:
    """The usual setup: the configuration, then the role-level grant `g` on `r1`."""
    w = World()
    w.config(d(-40))
    w.ceiling(d(-40, -1), "allow")
    w.grant(at=grant_at, **kw)
    return w


def eval_failed(
    w: World, at: datetime, subject: tuple[str, str] = ("agent", "a1"), suite: str = "smoke"
) -> None:
    w.add(
        ORG,
        "eval.scored",
        at,
        {
            "subject_kind": subject[0],
            "subject_id": subject[1],
            "suite_id": suite,
            "passed": False,
        },
    )


# -- D: demotion ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "suite", "expected"),
    [
        (("agent", "a1"), "smoke", "demoted"),  # D1
        (("role", "dev"), "smoke", "demoted"),  # D2
        (("agent", "a2"), "smoke", "demoted"),  # D3
        (("agent", "a1"), "other", "active"),  # D6
        (("role", "rev"), "smoke", "active"),  # another role's eval
    ],
)
def test_eval_triggers(subject: tuple[str, str], suite: str, expected: str) -> None:
    w = base()
    eval_failed(w, d(-5), subject, suite)
    assert state_of(w) == expected


def test_d4_trigger_committed_before_the_grant_never_counts() -> None:
    w = World()
    w.config(d(-40))
    w.ceiling(d(-40, -1), "allow")
    eval_failed(w, d(-11), ("agent", "a1"))
    w.grant()
    assert state_of(w) == "active"


def test_d5_trigger_at_exactly_granted_at_but_committed_after_demotes() -> None:
    w = base()
    eval_failed(w, d(-10))
    assert state_of(w) == "demoted"


def test_d7_a_later_pass_never_reactivates() -> None:
    w = base()
    eval_failed(w, d(-5))
    w.add(
        ORG,
        "eval.scored",
        d(-4),
        {"subject_kind": "agent", "subject_id": "a1", "suite_id": "smoke", "passed": True},
    )
    assert state_of(w) == "demoted"


@pytest.mark.parametrize(
    ("agent", "expected"),
    [("a1", "demoted"), ("w1", "active"), (None, "demoted")],  # D8, D9, D10
)
def test_budget_breach_attribution(agent: str | None, expected: str) -> None:
    w = base()
    w.add(RUN, "budget.breached", d(-3), {} if agent is None else {"agent": agent})
    assert state_of(w) == expected


def test_d10_a_breach_with_no_agent_demotes_every_grant_in_the_organization() -> None:
    w = base()
    w.grant("g2", subject=("agent", "w1"), role="rev", key=1)
    w.add(RUN, "budget.breached", d(-3), {"agent": None})
    assert {s.state for s in states(w).values()} == {"demoted"}


@pytest.mark.parametrize(("rule", "expected"), [(1, "demoted"), (2, "active")])  # D11, D12
def test_approval_denied_joins_the_rule_key_and_hash(rule: int, expected: str) -> None:
    w = base()
    w.add(
        RUN,
        "policy.decided",
        d(-3),
        {
            "agent": "a1",
            "approval": "ap1",
            "rule": rule,
            "rule_hash": f"h{rule}",
            "decision": "ask",
        },
    )
    w.add(RUN, "approval.decided", d(-2), {"approval": "ap1", "agent": "a1", "approved": False})
    assert state_of(w) == expected


def streak(pattern: str, agent: str = "a1", *, before: bool = False) -> World:
    """Verdicts 1..n of the agent, `F` failed and `.` passed; before the grant if asked."""
    w = World()
    w.config(d(-40))
    w.ceiling(d(-40, -1), "allow")
    if before:
        w.assign(d(-30), agent)
        for i, ch in enumerate(pattern):
            w.verdict(d(-29, i), ch == ".")
        w.grant()
        return w
    w.grant()
    w.assign(d(-9), agent)
    for i, ch in enumerate(pattern):
        w.verdict(d(-8, i), ch == ".")
    return w


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("F........F", "active"),  # D13: 10 verdicts, 2 failed
        ("FFF", "demoted"),  # D14: the first 3
        ("F........FF", "active"),  # D15: positions 1, 10, 11
        ("F...F....F", "demoted"),  # D16: 1, 5, 10
        ("F...F.....F", "active"),  # D17: 1, 5, 11
    ],
)
def test_failure_streak(pattern: str, expected: str) -> None:
    assert state_of(streak(pattern)) == expected


def test_d18_failures_committed_before_the_grant_do_not_count() -> None:
    assert state_of(streak("FFF", before=True)) == "active"


def test_d19_human_and_error_results_are_not_verdicts() -> None:
    w = streak("")
    for i in range(3):
        w.verdict(d(-7, i), False, kind="human")
        w.verdict(d(-6, i), False, outcome="error")
    assert state_of(w) == "active"


def test_d20_and_d21_role_level_counts_all_agents_agent_level_only_its_own() -> None:
    def build(grant_subject: tuple[str, str]) -> World:
        w = World()
        w.config(d(-40))
        w.ceiling(d(-40, -1), "allow")
        w.grant(subject=grant_subject)
        for i, (agent, ok) in enumerate(
            [("a1", False), ("a2", True), ("a2", False), ("a2", False), ("a1", True)]
        ):
            w.assign(d(-9, i), agent, task=f"t{i}")
            w.add(
                RUN,
                "gate.completed",
                d(-8, i),
                {"gate": "g", "kind": "test_command", "outcome": "passed" if ok else "failed"},
                task=f"t{i}",
            )
        return w

    assert state_of(build(("role", "dev"))) == "demoted"  # D20: 1 + 2 failures in the role
    assert state_of(build(("agent", "a1"))) == "active"  # D21: a1 alone has one


def test_d22_agent_level_grant_ignores_another_agents_eval() -> None:
    w = base(subject=("agent", "a1"))
    eval_failed(w, d(-5), ("agent", "a2"))
    assert state_of(w) == "active"


def test_d23_a_breach_of_a1_demotes_both_the_agent_and_the_role_grant() -> None:
    w = base()
    w.grant("g1", subject=("agent", "a1"))
    w.add(RUN, "budget.breached", d(-3), {"agent": "a1"})
    assert {k: s.state for k, s in states(w).items()} == {"g": "demoted", "g1": "demoted"}


def test_d24_stops_blocks_and_denies_are_not_triggers() -> None:
    w = base()
    w.add(RUN, "run.stop_requested", d(-3), {})
    w.add(RUN, "model.blocked", d(-3), {"agent": "a1"})
    w.add(RUN, "policy.decided", d(-3), {"agent": "a1", "decision": "deny", "rule": 5})
    assert state_of(w) == "active"


def test_d25_time_does_not_heal() -> None:
    w = base()
    eval_failed(w, d(-9))
    assert state_of(w, as_of=d(10)) == "demoted"


def test_d26_d27_either_source_suffices() -> None:
    w = base()
    eval_failed(w, d(-5))
    assert state_of(w) == "demoted"  # derived, no autonomy_demotion written
    w2 = base()  # an autonomy_demotion entry and no findable trigger
    w2.decision(
        d(-4),
        {
            "kind": "autonomy_demotion",
            "grant_id": "g",
            "trigger": "eval_failed",
            "trigger_entry": "x",
        },
    )
    assert state_of(w2) == "demoted"


def test_d28_a_trigger_after_as_of_does_not_exist() -> None:
    w = base()
    eval_failed(w, d(-5))
    assert state_of(w, as_of=d(-6)) == "active"


def test_d29_expired_names_the_state_before_demotion() -> None:
    w = base(days=9)  # granted T-10d, expires T-1d
    eval_failed(w, d(-5))
    assert state_of(w) == "expired"


def test_d30_a_trigger_committed_before_a_later_grant_does_not_touch_it() -> None:
    w = base()
    eval_failed(w, d(-5))
    w.grant("g2", at=d(-1))
    assert (state_of(w, "g"), state_of(w, "g2")) == ("demoted", "active")


def test_d32_demotion_comes_before_void() -> None:
    w = base()
    eval_failed(w, d(-5))
    w.config(d(-4), roles={"dev": ["h0", "hX", "h2"], "rev": ["h0", "h1"]})
    assert state_of(w) == "demoted"


def test_d34_d35_commit_order_not_the_clock() -> None:
    late = base()
    late.add(RUN, "budget.breached", d(-12), {"agent": "a1"})  # back-dated, committed after
    assert state_of(late) == "demoted"
    early = World()
    early.config(d(-40))
    early.ceiling(d(-40, -1), "allow")
    early.add(RUN, "budget.breached", d(-12), {"agent": "a1"})  # committed before the grant
    early.grant()
    assert state_of(early) == "active"


def test_state_order_not_yet_expired_revoked() -> None:
    w = base(grant_at=d(1))
    assert state_of(w) == "not_yet"
    w = base(days=10)
    assert (state_of(w, as_of=d(0, -1)), state_of(w, as_of=d(0))) == ("active", "expired")
    w.decision(d(-2), {"kind": "autonomy_revoke", "grant_id": "g"})
    assert (state_of(w, as_of=d(-3)), state_of(w, as_of=d(-2))) == ("active", "revoked")


def test_ceiling_state_and_default_none() -> None:
    w = World()
    w.config(d(-40))
    w.grant()
    assert state_of(w) == "ceiling"  # no ceiling entry: the default is none
    w.ceiling(d(-9), "allow")
    assert state_of(w) == "active"
    w.ceiling(d(-5), "none")
    assert state_of(w) == "ceiling"


# -- void ---------------------------------------------------------------------------------------


def allowed(w: World) -> World:
    w.ceiling(d(-40, -1), "allow")
    return w


def void_base() -> World:
    """Configuration, ceiling allow, then the agent-level grant `g1` for a1 on `r1`."""
    w = World()
    w.config(d(-40))
    allowed(w)
    w.grant("g1", subject=("agent", "a1"))
    return w


def test_g41_g42_a_plain_edit_voids_for_good_and_reverting_never_revives() -> None:
    w = void_base()
    w.config(d(-3), roles={"dev": ["h0", "h1b", "h2"], "rev": ["h0", "h1"]})
    s = states(w)["g1"]
    assert (s.state, s.void_reason) == ("void", "rule_changed")
    assert state_of(w, "g1", d(-4)) == "active"  # as-of before the edit
    w.config(d(-1))  # the old rule hashes again
    assert state_of(w, "g1", T) == "void"
    assert state_of(w, "g1", d(10)) == "void"


def test_g36_g53_a_promotion_is_a_permanent_void_with_the_entry_id() -> None:
    w = void_base()
    promo = w.decision(d(-3), {"kind": "promotion", "voided_grants": ["g1"]})
    w.decision(d(-1), {"kind": "promotion", "voided_grants": []})  # a rollback
    s = states(w)["g1"]
    assert (s.state, s.void_reason, s.promotion_entry) == (
        "void",
        "promotion",
        f"{ORG}#{promo.event.seq}",
    )
    assert state_of(w, "g1", d(10)) == "void"
    assert state_of(w, "g1", d(-4)) == "active"  # g37: before the promotion's at


def test_g38_a_promotion_naming_another_grant_changes_nothing() -> None:
    w = void_base()
    w.decision(d(-3), {"kind": "promotion", "voided_grants": ["g9"]})
    assert state_of(w, "g1") == "active"


def test_g49_promotion_is_checked_before_a_configuration_reason() -> None:
    w = void_base()
    w.decision(d(-3), {"kind": "promotion", "voided_grants": ["g1"]})
    w.config(d(-3), roles={"dev": ["h0", "h1b", "h2"], "rev": ["h0", "h1"]})
    assert states(w)["g1"].void_reason == "promotion"


def test_g44_g45_role_widened_but_removing_an_agent_voids_nothing() -> None:
    w = World()
    w.config(d(-40))
    allowed(w)
    w.grant("g")
    w.grant("g1", subject=("agent", "a1"), key=1)
    plus = {"a1": ("dev", "m1"), "a2": ("dev", "m1"), "w1": ("rev", "m1"), "a3": ("dev", "m1")}
    w.config(d(-2), agents=plus)
    got = {k: (s.state, s.void_reason) for k, s in states(w).items()}
    assert got == {"g": ("void", "role_widened"), "g1": ("active", None)}
    assert state_of(w, "g", d(-3)) == "active"
    fewer = World()
    fewer.config(d(-40))
    allowed(fewer)
    fewer.grant("g")
    fewer.config(d(-2), agents={"a1": ("dev", "m1"), "w1": ("rev", "m1")})
    assert state_of(fewer, "g") == "active"


def test_g46_agent_moved_and_moving_back_does_not_revive() -> None:
    w = void_base()
    w.config(d(-3), agents={"a1": ("rev", "m1"), "a2": ("dev", "m1"), "w1": ("rev", "m1")})
    w.config(d(-1))
    assert (states(w)["g1"].state, states(w)["g1"].void_reason) == ("void", "agent_moved")


def test_g47_model_change_voids_only_the_grants_that_cover_that_agent() -> None:
    w = World()
    w.config(d(-40))
    allowed(w)
    w.grant("g1", subject=("agent", "a1"))
    w.grant("g2", subject=("agent", "a2"))
    w.grant("g")
    w.config(d(-2), agents={"a1": ("dev", "m2"), "a2": ("dev", "m1"), "w1": ("rev", "m1")})
    got = {k: (s.state, s.void_reason) for k, s in states(w).items()}
    assert got == {
        "g1": ("void", "model_changed"),
        "g2": ("active", None),
        "g": ("void", "model_changed"),
    }


def test_g48_a_new_contract_voids() -> None:
    w = void_base()
    w.config(d(-2), contract=4)
    s = states(w)["g1"]
    assert (s.state, s.void_reason) == ("void", "contract_changed")


def test_o19_a_grant_written_under_another_contract_is_void_against_its_base() -> None:
    w = World()
    w.config(d(-40))
    allowed(w)
    w.grant("g1", contract=2)
    assert states(w)["g1"].void_reason == "contract_changed"


def test_g50_a_void_while_in_ceiling_state_still_counts() -> None:
    w = World()
    w.config(d(-40))
    w.ceiling(d(-40, -1), "allow")
    w.grant("g1")
    w.ceiling(d(-5), "none")
    assert state_of(w, "g1", d(-4)) == "ceiling"
    w.config(d(-3), roles={"dev": ["h0", "h1b", "h2"], "rev": ["h0", "h1"]})
    w.config(d(-2))
    w.ceiling(d(-1), "allow")
    s = states(w)["g1"]
    assert (s.state, s.void_reason) == ("void", "rule_changed")


def test_g51_revoked_comes_before_void() -> None:
    w = void_base()
    w.decision(d(-5), {"kind": "autonomy_revoke", "grant_id": "g1"})
    w.config(d(-3), roles={"dev": ["h0", "h1b", "h2"], "rev": ["h0", "h1"]})
    assert state_of(w, "g1") == "revoked"


def test_an_autonomy_void_entry_alone_is_a_second_source() -> None:
    w = void_base()
    w.decision(d(-2), {"kind": "autonomy_void", "grant_id": "g1", "reason": "model_changed"})
    assert states(w)["g1"].void_reason == "model_changed"
    assert state_of(w, "g1", d(-3)) == "active"


# -- the load path's writes (6.5) -------------------------------------------------------------


def factory(pos: int, at: datetime, payload: Any) -> LedgerEvent:
    return event(ORG, 10_000 + pos, "decision.recorded", at, payload)


def voids(w: World, record: dict[str, Any], now: datetime = T) -> list[dict[str, Any]]:
    return au.voids_for_new_config(
        w.entries, record, now, required_suites=SUITES, event_factory=factory
    )


def test_g41_the_load_path_writes_one_void_with_the_hash_seen() -> None:
    w = void_base()
    record = config_record(roles={"dev": ["h0", "h1b", "h2"], "rev": ["h0", "h1"]})
    assert voids(w, record) == [
        {
            "kind": "autonomy_void",
            "grant_id": "g1",
            "reason": "rule_changed",
            "rule_hash_seen": "h1b",
            "policy_contract_seen": 3,
        }
    ]


def test_g42_a_grant_already_void_gets_no_second_entry() -> None:
    w = void_base()
    w.config(d(-3), roles={"dev": ["h0", "h1b", "h2"], "rev": ["h0", "h1"]})
    assert voids(w, config_record()) == []


def test_g47_g48_the_load_path_picks_the_first_reason_per_grant() -> None:
    w = World()
    w.config(d(-40))
    allowed(w)
    w.grant("g1", subject=("agent", "a1"))
    w.grant("g2", subject=("agent", "a2"))
    w.grant("g")
    record = config_record(agents={"a1": ("dev", "m2"), "a2": ("dev", "m1"), "w1": ("rev", "m1")})
    got = {v["grant_id"]: v["reason"] for v in voids(w, record)}
    assert got == {"g1": "model_changed", "g": "model_changed"}
    other = voids(w, config_record(contract=4))
    assert {v["reason"] for v in other} == {"contract_changed"} and len(other) == 3
    assert other[0]["rule_hash_seen"] == "h1" and other[0]["policy_contract_seen"] == 4


def test_g50_g51_ceiling_state_is_included_revoked_demoted_are_not() -> None:
    w = World()
    w.config(d(-40))  # no ceiling entry: g1 is in state `ceiling`
    w.grant("g1")
    w.grant("g2")
    w.decision(d(-5), {"kind": "autonomy_revoke", "grant_id": "g2"})
    changed = config_record(roles={"dev": ["h0", "hZ", "h2"], "rev": ["h0", "h1"]})
    assert [v["grant_id"] for v in voids(w, changed)] == ["g1"]


# -- settings and the rule identity --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "problem"),
    [
        ("MIN_APPROVED", 10, "looser_than_default"),  # G27
        ("MIN_APPROVED", 40, None),
        ("MIN_VERDICTS", 19, "looser_than_default"),
        ("MIN_PASS_RATE", Decimal("0.9"), "looser_than_default"),
        ("MIN_PASS_RATE", Decimal("0.99"), None),
        ("MIN_PASS_RATE", Decimal("1.01"), "invalid"),
        ("COOLDOWN_DAYS", 13, "looser_than_default"),
        ("MAX_EXPIRY_DAYS", 91, "looser_than_default"),
        ("MAX_EXPIRY_DAYS", 30, None),
        ("STREAK_LENGTH", 5, "looser_than_default"),  # G34
        ("STREAK_LENGTH", 20, None),
        ("STREAK_FAILS", 4, "looser_than_default"),
        ("STREAK_FAILS", 2, None),
        ("STREAK_FAILS", 0, "invalid"),
        ("WINDOW_DAYS", 60, "not_settable"),
        ("NOPE", 1, "invalid"),
    ],
)
def test_settings_can_only_be_made_stricter(
    name: str, value: int | Decimal, problem: str | None
) -> None:
    found = au.setting_problem(name, value, au.DEFAULT_SETTINGS)
    assert (found[0] if found else None) == problem


def test_streak_length_may_not_drop_below_streak_fails() -> None:
    current = Settings(streak_fails=3, streak_length=10)
    assert au.setting_problem("STREAK_FAILS", 2, current) is None
    assert au.setting_problem("STREAK_LENGTH", 10, Settings(streak_fails=3)) is None


def test_a_stricter_streak_setting_changes_the_demotion() -> None:
    w = streak("F.F.")  # two failures in four: fine by default
    assert state_of(w) == "active"
    assert states(w, settings=Settings(streak_fails=2))["g"].state == "demoted"


def test_rule_hash_changes_with_any_edit_and_the_config_record_lists_hashes_in_key_order() -> None:
    from nuramesh.core.org import OrgGraph
    from nuramesh.core.policy import ActionKind, Decision, Policy, PolicyRule, PolicySet

    a = PolicyRule(action=ActionKind.RUN_COMMAND, target="pytest *", decision=Decision.ASK)
    b = a.model_copy(update={"target": "pytest tests"})
    c = a.model_copy(update={"decision": Decision.ALLOW})
    assert len({au.rule_hash(a), au.rule_hash(b), au.rule_hash(c)}) == 3
    assert au.rule_hash(a) == au.rule_hash(a.model_copy(update={"reason": "other words"}))
    org = OrgGraph.build(
        {
            "id": "o1",
            "name": "Org One",
            "roles": [
                {"id": "cos", "name": "CoS", "kind": "chief_of_staff"},
                {"id": "dev", "name": "Dev", "default_model": "fake/m1"},
            ],
            "agents": [
                {"id": "boss", "name": "Boss", "role": "cos"},
                {"id": "a1", "name": "A1", "role": "dev", "reports_to": "boss"},
            ],
        }
    )
    record = au.config_payload(org, PolicySet(policies=(Policy(role="dev", rules=(a, b)),)))
    assert record["roles"] == {"cos": None, "dev": [au.rule_hash(a), au.rule_hash(b)]}
    assert record["agents"]["a1"] == {"role": "dev", "model": "fake/m1"}
    assert record["policy_contract"] == 3
    assert len(record["config_hash"]) == 64


def test_expiry_is_exclusive_at_the_instant() -> None:
    assert au.expires_at(T, 30) == T + timedelta(days=30)


def test_entries_are_unchanged_by_reading() -> None:
    w = base()
    before = list(w.entries)
    states(w)
    assert w.entries == before and all(isinstance(e, Entry) for e in w.entries)


@pytest.mark.parametrize("text", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity", "inf"])
def test_non_finite_decimal_settings_are_invalid_not_a_crash(text: str) -> None:
    """MIN_PASS_RATE is the Decimal setting; NaN and infinities answer `invalid` (fuzz finding)."""
    problem = au.setting_problem("MIN_PASS_RATE", Decimal(text), au.DEFAULT_SETTINGS)
    assert problem is not None and problem[0] == "invalid"


@pytest.mark.parametrize("text", ["-1E+30", "-1E+999", "1E+999", "1E-999", "-1E-999", "-5"])
def test_finite_extreme_pass_rates_are_invalid_not_a_crash(text: str) -> None:
    """Finite values outside 0..1 or too fine for 4 places answer `invalid`, never raise."""
    problem = au.setting_problem("MIN_PASS_RATE", Decimal(text), au.DEFAULT_SETTINGS)
    assert problem is not None and problem[0] == "invalid"


def test_finite_pass_rate_boundaries_are_unchanged() -> None:
    def answer(text: str) -> str | None:
        problem = au.setting_problem("MIN_PASS_RATE", Decimal(text), au.DEFAULT_SETTINGS)
        return None if problem is None else problem[0]

    assert answer("1") is None
    assert answer("0.9500") is None
    assert answer("0.5") == "looser_than_default"
    assert answer("0") == "looser_than_default"
    assert answer("1.0001") == "invalid"
