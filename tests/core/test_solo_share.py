"""The solo share in the scorecard and the proposal (spec/rbac/solo-approval.md SA-18 items 1 and
2; cases B40, B41, B42 and B61 of section 8). A decision "is solo" when its `approval.decided`
entry has `approval_mode: solo`; an entry with no mode (written before the change) is never solo
and is counted in the totals. The expected values are written from the spec."""

from typing import Any

import pytest
from autonomy_world import SUITES, T, World, d

from nuramesh.core.proposals import evaluate_cell, solo_lines
from nuramesh.core.scorecard import make_period, scorecard, solo_share

from .test_proposals import ROLES, denial, evidence, propose
from .test_proposals import world as proposal_world


def approvals_of(w: World) -> dict[str, Any]:
    period, minimum = make_period(None, None, T, 20)
    card = scorecard(
        w.entries,
        kind="agent",
        subject="a1",
        period=period,
        min_results=minimum,
        evals=[],
        required_suites=SUITES,
    )
    assert card["scorecard_version"] == 1  # additive keys: the version stays
    return dict(card["approvals"])


def decided(w: World, n: int, approved: bool, mode: str | None, agent: str = "a1") -> None:
    start = sum(1 for e in w.entries if e.event.type == "approval.decided")
    for i in range(start, start + n):
        w.add("run:r1", "approval.requested", d(-6, i), {"agent": agent, "approval": f"p{i}"})
        payload: dict[str, Any] = {"agent": agent, "approval": f"p{i}", "approved": approved}
        if mode is not None:
            payload["approval_mode"] = mode
            if mode == "solo" and approved:
                payload["solo_proof"] = "none"
        w.add("run:r1", "approval.decided", d(-5, i), payload)


def card_world() -> World:
    w = World()
    w.config(d(-60))
    return w


def test_b40_solo_decisions_are_subsets_of_approved_and_denied() -> None:
    w = card_world()
    decided(w, 3, True, "solo")
    decided(w, 1, True, "peer")
    decided(w, 1, False, "solo")
    decided(w, 1, True, None)  # from before this change: counted in the totals, never solo
    assert approvals_of(w) == {
        "requested": 6,
        "approved": 5,
        "denied": 1,
        "pending": 0,
        "approved_solo": 3,
        "denied_solo": 1,
        "solo_share": "0.6000",
    }


def test_b41_no_approved_decision_has_no_share() -> None:
    w = card_world()
    assert approvals_of(w)["solo_share"] is None
    decided(w, 2, False, "solo")
    got = approvals_of(w)
    assert (got["approved"], got["denied_solo"], got["solo_share"]) == (0, 2, None)


@pytest.mark.parametrize(
    ("solo", "approved", "share"),
    [
        (1, 3, "0.3333"),
        (2, 3, "0.6667"),
        (1, 8, "0.1250"),
        (1, 32, "0.0312"),  # 0.03125: half to even
        (3, 32, "0.0938"),  # 0.09375: half to even
        (0, 4, "0.0000"),
        (3, 5, "0.6000"),
        (30, 30, "1.0000"),
    ],
)
def test_b61_the_share_is_rounded_once_half_to_even(solo: int, approved: int, share: str) -> None:
    assert solo_share(solo, approved) == share
    w = card_world()
    decided(w, solo, True, "solo")
    decided(w, approved - solo, True, "no_originator")
    got = approvals_of(w)
    assert (got["approved_solo"], got["approved"], got["solo_share"]) == (solo, approved, share)


def test_other_modes_and_other_agents_are_not_solo_of_this_agent() -> None:
    w = card_world()
    decided(w, 2, True, "peer")
    decided(w, 1, True, "no_originator")
    decided(w, 4, True, "solo", agent="a2")
    got = approvals_of(w)
    assert (got["approved"], got["approved_solo"], got["solo_share"]) == (3, 0, "0.0000")


def test_a_solo_decision_outside_the_period_is_not_counted() -> None:
    w = card_world()
    w.add("run:r1", "approval.requested", d(-40), {"agent": "a1", "approval": "old"})
    w.add(
        "run:r1",
        "approval.decided",
        d(-31),
        {"agent": "a1", "approval": "old", "approved": True, "approval_mode": "solo"},
    )
    got = approvals_of(w)
    assert (got["approved"], got["approved_solo"], got["solo_share"]) == (0, 0, None)


# -- the proposal (SA-18 item 2) ---


def with_modes(w: World, mode_of: Any) -> World:
    """The same entries, each `approval.decided` carrying the mode `mode_of(index)` gives."""
    out = World()
    n = 0
    for item in w.entries:
        event = item.event
        payload = dict(event.payload)
        if event.type == "approval.decided":
            mode = mode_of(n)
            n += 1
            if mode is not None:
                payload["approval_mode"] = mode
        out.add(event.stream, event.type, event.at, payload, task=event.task_id, actor=event.actor)
    return out


def test_b42_thirty_solo_asks_and_the_evidence_is_what_it_was_without_the_change() -> None:
    plain = proposal_world()
    evidence(plain)
    before = propose(plain)  # entries with no mode: the values computed without this change
    after = propose(with_modes(plain, lambda i: "solo"))
    assert after.to_json()["solo"] == {"approved_asks_solo": 30, "solo_share": "1.0000"}
    assert after.evidence == before.evidence and after.evidence_hash == before.evidence_hash
    assert "solo" not in after.evidence  # beside the evidence, outside the hash
    assert before.to_json()["solo"] == {"approved_asks_solo": 0, "solo_share": "0.0000"}
    assert after.status == before.status == "proposable"  # the share blocks nothing


def test_the_proposal_counts_solo_over_the_same_approved_asks() -> None:
    plain = proposal_world()
    evidence(plain)
    mixed = with_modes(plain, lambda i: "solo" if i < 10 else ("peer" if i < 20 else None))
    # a solo approve of another cell's ask is not part of this cell's evidence
    mixed.add(
        "run:r1",
        "approval.decided",
        d(-15, 500),
        {"approval": "elsewhere", "agent": "a1", "approved": True, "approval_mode": "solo"},
    )
    got = propose(mixed).to_json()
    assert got["evidence"]["approved_asks"] == 30
    assert got["solo"] == {"approved_asks_solo": 10, "solo_share": "0.3333"}


def test_a_solo_denied_ask_in_the_cell_is_not_counted_in_the_approved_solo_share() -> None:
    plain = proposal_world()
    evidence(plain)
    denial(plain, d(-10))  # the 31st decision of the cell: a denied ask
    mixed = with_modes(plain, lambda i: "solo" if i < 10 or i == 30 else None)
    got = propose(mixed).to_json()
    assert (got["evidence"]["approved_asks"], got["evidence"]["denied_asks"]) == (30, 1)
    assert got["solo"] == {"approved_asks_solo": 10, "solo_share": "0.3333"}  # not 11


def test_a_cell_with_no_evidence_has_a_solo_object_with_no_share() -> None:
    w = World()
    w.config(d(-90), roles=ROLES)
    item = evaluate_cell(
        w.entries,
        kind="agent",
        subject="a1",
        rule_key=1,
        as_of=T,
        required_suites=SUITES,
        eval_blocked=lambda scope: False,
    ).to_json()
    assert item["solo"] == {"approved_asks_solo": 0, "solo_share": None}


@pytest.mark.parametrize(
    ("approved", "solo", "share", "lines"),
    [
        (
            30,
            30,
            "1.0000",
            [
                "approved asks: 30 (solo 30, 100 %)",
                "no second person reviewed the solo ones",
            ],
        ),
        (
            3,
            1,
            "0.3333",
            ["approved asks: 3 (solo 1, 33.33 %)", "no second person reviewed the solo ones"],
        ),
        (
            8,
            1,
            "0.1250",
            ["approved asks: 8 (solo 1, 12.5 %)", "no second person reviewed the solo ones"],
        ),
        (4, 0, "0.0000", ["approved asks: 4 (solo 0, 0 %)"]),
        (0, 0, None, ["approved asks: 0 (solo 0)"]),
    ],
)
def test_the_printed_line_of_a_proposal(
    approved: int, solo: int, share: str | None, lines: list[str]
) -> None:
    assert solo_lines(approved, solo, share) == lines
