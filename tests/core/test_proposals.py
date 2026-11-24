"""Proposals (E13): the cases P1 to P45 of `spec/autonomy/README.md` section 9.4."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from autonomy_world import POLICY, SUITES, T, World, d

from nuramesh.core.autonomy import DEFAULT_SETTINGS, Settings
from nuramesh.core.proposals import Proposal, evaluate_cell, proposals_for, settings_at
from nuramesh.core.scorecard import SubjectScope

ROLES = {"dev": POLICY, "rev": ["h0", "h1"]}


def world(*, ceiling: str | None = "allow", kinds: tuple[str, ...] = ("run_command",)) -> World:
    w = World()
    w.config(d(-90), roles=ROLES)
    if ceiling is not None:
        for kind in kinds:
            w.ceiling(d(-89), ceiling, kind=kind)
    return w


def evidence(
    w: World,
    *,
    agent: str = "a1",
    approvals: int = 30,
    verdicts: int = 20,
    passed: int = 19,
    key: int = 1,
    rule_hash: str = "h1",
    contract: int = 3,
    with_hash: bool = True,
    prefix: str = "p",
    linked: bool = True,
    verdict_at: datetime | None = None,
) -> None:
    """Spec 9.4 'good evidence': approvals decided in the window, each from a `policy.decided`
    on the cell, and verdicts on the linked tasks."""
    for i in range(approvals):
        task = f"{prefix}{i}" if linked else None
        payload: dict[str, Any] = {
            "agent": agent,
            "rule": key,
            "policy_contract": contract,
            "decision": "ask",
            "approval": f"{prefix}ap{i}",
        }
        if with_hash:
            payload["rule_hash"] = rule_hash
        w.add("run:r1", "policy.decided", d(-20, i), payload, task=task)
        w.add(
            "run:r1",
            "approval.decided",
            d(-15, i),
            {"approval": f"{prefix}ap{i}", "agent": agent, "approved": True},
            task=task,
        )
    for i in range(verdicts):
        task = f"{prefix}{i}"
        w.add(
            "run:r1",
            "task.transitioned",
            d(-14, i),
            {"to": "verifying", "assignee": agent},
            task=task,
        )
        w.add(
            "run:r1",
            "gate.completed",
            verdict_at or d(-12, i),
            {"kind": "test_command", "outcome": "passed" if i < passed else "failed"},
            task=task,
        )


def propose(
    w: World,
    *,
    kind: str = "agent",
    subject: str = "a1",
    key: int = 1,
    as_of: datetime = T,
    blocked: bool = False,
    settings: Settings | None = None,
) -> Proposal:
    return evaluate_cell(
        w.entries,
        kind=kind,
        subject=subject,
        rule_key=key,
        as_of=as_of,
        required_suites=SUITES,
        eval_blocked=lambda scope: blocked,
        settings=settings,
    )


def outcome(p: Proposal) -> tuple[str, list[str]]:
    return p.status, list(p.reasons)


def test_p1_good_evidence_is_proposable() -> None:
    w = world()
    evidence(w)
    p = propose(w)
    assert outcome(p) == ("proposable", [])
    assert (p.evidence["approved_asks"], p.evidence["denied_asks"]) == (30, 0)
    assert (p.evidence["verdicts"], p.evidence["passed"]) == (20, 19)


@pytest.mark.parametrize(
    ("variation", "expected"),
    [
        ({"approvals": 29, "verdicts": 20}, ("not_enough_evidence", ["approved_below_min"])),  # P2
        (
            {"verdicts": 19, "passed": 18},
            ("not_enough_evidence", ["verdicts_below_min"]),
        ),  # P3
        ({"passed": 18}, ("blocked", ["pass_rate_below_min"])),  # P4 (0.90)
        ({"verdicts": 100, "approvals": 100, "passed": 94}, ("blocked", ["pass_rate_below_min"])),
        ({"verdicts": 100, "approvals": 100, "passed": 95}, ("proposable", [])),  # P5
        ({"linked": False}, ("not_enough_evidence", ["verdicts_below_min"])),  # P29
        (
            {"rule_hash": "h0"},
            ("not_enough_evidence", ["approved_below_min", "verdicts_below_min"]),
        ),  # P34
        (
            {"with_hash": False},
            ("not_enough_evidence", ["approved_below_min", "verdicts_below_min"]),
        ),  # P35
        (
            {"contract": 2},
            ("not_enough_evidence", ["approved_below_min", "verdicts_below_min"]),
        ),  # P36
    ],
)
def test_evidence_variations(variation: dict[str, Any], expected: tuple[str, list[str]]) -> None:
    w = world()
    evidence(w, **variation)
    assert outcome(propose(w)) == expected


def test_p31_a_verdict_outside_the_window_does_not_count() -> None:
    w = world()
    evidence(w, verdicts=19)
    evidence(w, approvals=0, verdicts=0)
    w.add("run:r1", "task.transitioned", d(-40), {"to": "x", "assignee": "a1"}, task="p19")
    w.add(
        "run:r1",
        "gate.completed",
        d(-31),
        {"kind": "test_command", "outcome": "passed"},
        task="p19",
    )
    assert outcome(propose(w)) == ("not_enough_evidence", ["verdicts_below_min"])


def denial(w: World, at: datetime, key: int = 1, rule_hash: str = "h1", agent: str = "a1") -> None:
    w.add(
        "run:r1",
        "policy.decided",
        at - timedelta(seconds=5),
        {
            "agent": agent,
            "rule": key,
            "rule_hash": rule_hash,
            "policy_contract": 3,
            "decision": "ask",
            "approval": "den",
        },
        task="dt",
    )
    w.add(
        "run:r1",
        "approval.decided",
        at,
        {"approval": "den", "agent": agent, "approved": False},
        task="dt",
    )


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (d(-29), ("blocked", ["denied_in_window"])),  # P6
        (d(-31), ("proposable", [])),  # P7
        (d(-30), ("blocked", ["denied_in_window"])),  # P8: the window is inclusive
    ],
)
def test_denials_in_the_window(when: datetime, expected: tuple[str, list[str]]) -> None:
    w = world()
    evidence(w)
    denial(w, when)
    assert outcome(propose(w)) == expected


def test_p9_a_denial_of_another_cell_does_not_count() -> None:
    w = world()
    evidence(w)
    denial(w, d(-1), key=2, rule_hash="h2")
    assert outcome(propose(w)) == ("proposable", [])


def test_p10_p11_pending_and_late_approvals_count_for_nothing() -> None:
    w = world()
    evidence(w, approvals=29)
    for i in range(5):  # asked, never answered
        w.add(
            "run:r1",
            "policy.decided",
            d(-3, i),
            {
                "agent": "a1",
                "rule": 1,
                "rule_hash": "h1",
                "policy_contract": 3,
                "decision": "ask",
                "approval": f"pend{i}",
            },
            task=f"pend{i}",
        )
    assert outcome(propose(w)) == ("not_enough_evidence", ["approved_below_min"])


def test_p12_p13_p14_eval_state() -> None:
    w = world()
    evidence(w)
    assert outcome(propose(w, blocked=True)) == ("blocked", ["eval_blocked"])
    w.add(
        "org:o1",
        "eval.scored",
        d(-10),
        {"subject_kind": "agent", "subject_id": "a1", "suite_id": "smoke", "passed": False},
    )
    assert outcome(propose(w)) == ("blocked", ["eval_failed_in_cooldown"])
    w = world()
    evidence(w)
    w.add(
        "org:o1",
        "eval.scored",
        d(-15),
        {"subject_kind": "agent", "subject_id": "a1", "suite_id": "smoke", "passed": False},
    )
    assert outcome(propose(w)) == ("proposable", [])


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (d(-12), ("blocked", ["breach_in_cooldown"])),
        (d(-14), ("blocked", ["breach_in_cooldown"])),  # P16: inclusive
        (d(-14, -1), ("proposable", [])),
    ],
)
def test_breach_cooldown(when: datetime, expected: tuple[str, list[str]]) -> None:
    w = world()
    evidence(w)
    w.add("org:o1", "budget.breached", when, {"agent": "a1"})
    assert outcome(propose(w)) == expected


def test_a_breach_naming_no_agent_counts_against_everyone() -> None:
    w = world()
    evidence(w)
    w.add("org:o1", "budget.breached", d(-3), {})
    assert outcome(propose(w)) == ("blocked", ["breach_in_cooldown"])


@pytest.mark.parametrize(
    ("breach_at", "expected"),
    [(d(-13), ("blocked", ["demotion_in_cooldown"])), (d(-15), ("proposable", []))],
)
def test_p17_p18_a_recent_demotion_of_a_grant_on_the_cell(
    breach_at: datetime, expected: tuple[str, list[str]]
) -> None:
    w = world()
    w.grant("g", at=d(-60), days=90)  # role-level, on the cell
    evidence(w)
    w.add("org:o1", "budget.breached", breach_at, {"agent": "a2"})
    assert outcome(propose(w)) == expected


def test_p19_ceiling_none() -> None:
    w = world(ceiling=None)
    evidence(w)
    assert outcome(propose(w)) == ("ceiling", ["ceiling_none"])


@pytest.mark.parametrize("key", [3, 4, 7, 11])
def test_p20_to_p22_p43_never_grantable_kinds_and_shapes(key: int) -> None:
    w = world()
    evidence(w, key=key, rule_hash=f"h{key}")
    assert outcome(propose(w, key=key)) == ("never_grantable", ["never_grantable"])


@pytest.mark.parametrize("key", [5, 99])
def test_p23_deny_rules_and_unknown_keys_are_no_such_rule(key: int) -> None:
    w = world()
    assert outcome(propose(w, key=key)) == ("no_such_rule", ["no_such_rule"])


def test_p24_an_active_grant_makes_the_cell_already_granted() -> None:
    w = world()
    evidence(w)
    w.grant("g1", subject=("agent", "a1"), at=d(-2))
    assert outcome(propose(w)) == ("already_granted", ["already_granted"])
    w2 = world()
    evidence(w2)
    w2.grant("g1", subject=("role", "dev"), at=d(-2))
    assert outcome(propose(w2)) == ("already_granted", ["already_granted"])


def test_p25_p26_every_reason_is_listed_in_the_tables_order() -> None:
    w = world(ceiling=None)
    evidence(w, approvals=10, verdicts=10, passed=10)
    for i in range(10):  # 20 verdicts on the 10 linked tasks
        w.add(
            "run:r1",
            "gate.completed",
            d(-11, i),
            {"kind": "test_command", "outcome": "passed"},
            task=f"p{i}",
        )
    denial(w, d(-3))
    assert outcome(propose(w)) == (
        "ceiling",
        ["ceiling_none", "approved_below_min", "denied_in_window"],
    )
    w = world()
    evidence(w, approvals=29)
    denial(w, d(-3))
    assert outcome(propose(w)) == (
        "not_enough_evidence",
        ["approved_below_min", "denied_in_window"],
    )


def test_p27_p28_a_role_pools_its_agents_evidence() -> None:
    w = world()
    evidence(w, agent="a1", approvals=18, verdicts=12, passed=12, prefix="x")
    evidence(w, agent="a2", approvals=12, verdicts=8, passed=7, prefix="y")
    role = propose(w, kind="role", subject="dev")
    assert outcome(role) == ("proposable", [])
    assert outcome(propose(w)) == (
        "not_enough_evidence",
        ["approved_below_min", "verdicts_below_min"],
    )
    denial(w, d(-3), agent="a2")
    assert outcome(propose(w, kind="role", subject="dev")) == ("blocked", ["denied_in_window"])


def test_p30_human_and_error_verdicts_are_ignored() -> None:
    w = world()
    evidence(w)
    for i in range(10):
        w.add(
            "run:r1", "gate.completed", d(-11, i), {"kind": "human", "outcome": "failed"}, task="p0"
        )
    for i in range(5):
        w.add(
            "run:r1",
            "gate.completed",
            d(-10, i),
            {"kind": "test_command", "outcome": "error"},
            task="p0",
        )
    p = propose(w)
    assert outcome(p) == ("proposable", []) and p.evidence["verdicts"] == 20


def test_p32_p33_verdicts_must_be_on_linked_tasks_and_count_once() -> None:
    w = world()
    evidence(w, approvals=30, verdicts=0)
    for i in range(20):
        w.add(
            "run:r1",
            "task.transitioned",
            d(-14, i),
            {"to": "v", "assignee": "a1"},
            task=f"other{i}",
        )
        w.add(
            "run:r1",
            "gate.completed",
            d(-12, i),
            {"kind": "test_command", "outcome": "passed"},
            task=f"other{i}",
        )
    assert outcome(propose(w)) == ("not_enough_evidence", ["verdicts_below_min"])
    w = world()
    for i in range(30):  # every approval links to one task
        w.add(
            "run:r1",
            "policy.decided",
            d(-20, i),
            {
                "agent": "a1",
                "rule": 1,
                "rule_hash": "h1",
                "policy_contract": 3,
                "decision": "ask",
                "approval": f"a{i}",
            },
            task="one",
        )
        w.add(
            "run:r1",
            "approval.decided",
            d(-15, i),
            {"approval": f"a{i}", "agent": "a1", "approved": True},
            task="one",
        )
    w.add("run:r1", "task.transitioned", d(-14), {"to": "v", "assignee": "a1"}, task="one")
    for i in range(20):
        w.add(
            "run:r1",
            "gate.completed",
            d(-12, i),
            {"kind": "test_command", "outcome": "passed" if i < 19 else "failed"},
            task="one",
        )
    assert outcome(propose(w)) == ("proposable", [])


def test_p37_the_evidence_hash_follows_the_evidence() -> None:
    w = world()
    evidence(w)
    first, second = propose(w), propose(w)
    assert first.evidence_hash == second.evidence_hash
    w.add(
        "run:r1", "gate.completed", d(-9), {"kind": "test_command", "outcome": "passed"}, task="p1"
    )
    assert propose(w).evidence_hash != first.evidence_hash


def test_p38_later_entries_do_not_exist() -> None:
    w = world()
    evidence(w)
    denial(w, d(-1))
    assert outcome(propose(w, as_of=d(-3))) == ("proposable", [])


def test_p39_reading_appends_nothing() -> None:
    w = world()
    evidence(w)
    before = len(w.entries)
    propose(w)
    assert len(w.entries) == before


def test_p41_p42_ceilings_are_per_kind() -> None:
    w = world(kinds=("run_command",))
    evidence(w, key=6, rule_hash="h6")
    assert outcome(propose(w, key=6)) == ("ceiling", ["ceiling_none"])
    w = world(kinds=("run_command", "write_file"))
    evidence(w, key=2, rule_hash="h2")
    assert outcome(propose(w, key=2)) == ("proposable", [])


def test_p44_p45_overlay_entries_are_never_evidence() -> None:
    def overlay(w: World) -> None:
        for i in range(100):
            w.add(
                "run:r1",
                "policy.decided",
                d(-5, i),
                {
                    "agent": "a1",
                    "rule": 1,
                    "rule_hash": "h1",
                    "policy_contract": 3,
                    "decision": "allow",
                    "autonomy_grant": ["g1"],
                },
                task=f"o{i}",
            )

    w = world()
    evidence(w)
    overlay(w)
    p = propose(w)
    assert outcome(p) == ("proposable", []) and p.evidence["approved_asks"] == 30
    w = world()
    overlay(w)
    assert outcome(propose(w)) == (
        "not_enough_evidence",
        ["approved_below_min", "verdicts_below_min"],
    )


def test_g27_g28_a_stricter_setting_is_read_from_the_ledger() -> None:
    w = world()
    evidence(w)
    w.decision(
        d(-50), {"kind": "autonomy_setting_set", "name": "MIN_APPROVED", "old": 30, "new": 40}
    )
    assert settings_at(w.entries, T).min_approved == 40
    assert outcome(propose(w)) == ("not_enough_evidence", ["approved_below_min"])


@pytest.mark.parametrize("stored", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_stored_non_finite_pass_rate_is_ignored_like_an_unreadable_one(stored: str) -> None:
    w = world()
    w.decision(
        d(-50),
        {"kind": "autonomy_setting_set", "name": "MIN_PASS_RATE", "old": "0.9", "new": stored},
    )
    assert settings_at(w.entries, T).min_pass_rate == DEFAULT_SETTINGS.min_pass_rate


def test_the_listing_has_one_item_per_grantable_ask_rule() -> None:
    w = world()
    items = proposals_for(
        w.entries,
        kind="agent",
        subject="a1",
        as_of=T,
        required_suites=SUITES,
        eval_blocked=lambda scope: False,
    )
    assert [i.rule_key for i in items] == [1, 2, 6, 8, 10, 11]
    assert isinstance(SubjectScope, type)
