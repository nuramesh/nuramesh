"""The solo share of the organization status, the pure part (spec/rbac/solo-approval.md SA-18
item 4): which organization-stream entries removed an active decide-holder, and the counts over
the approval rows. The expected values are written from the spec; all data is made up."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.policy import Action, ActionKind, Approval, ApprovalMode, ApprovalStatus
from nuramesh.core.solo import removal_times, solo_status

T = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
DAY = 86400


def at(seconds: float) -> datetime:
    return T + timedelta(seconds=seconds)


def entries(*rows: tuple[str, float, dict[str, Any]]) -> list[LedgerEvent]:
    return [
        LedgerEvent(
            stream="org:acme",
            seq=i,
            type=kind,
            actor="human:u_owner1",
            at=at(when),
            payload=payload,
            prev_hash="0" * 64,
            hash="1" * 64,
        )
        for i, (kind, when, payload) in enumerate(rows, start=1)
    ]


def created(user: str, role: str, when: float = -1000) -> tuple[str, float, dict[str, Any]]:
    return ("user.created", when, {"user": user, "role": role, "via": "invite"})


def changed(user: str, old: str, new: str, when: float) -> tuple[str, float, dict[str, Any]]:
    return ("user.role_changed", when, {"user": user, "from": old, "to": new})


def one(kind: str, user: str, when: float) -> tuple[str, float, dict[str, Any]]:
    return (f"user.{kind}", when, {"user": user})


# -- removal entries ---


@pytest.mark.parametrize("role", ["approver", "admin", "owner"])
@pytest.mark.parametrize("kind", ["disabled", "erased"])
def test_disabling_or_erasing_an_active_decide_holder_is_a_removal(role: str, kind: str) -> None:
    assert removal_times(entries(created("human:u_a", role), one(kind, "human:u_a", 5))) == [at(5)]


@pytest.mark.parametrize("role", ["viewer", "builder"])
@pytest.mark.parametrize("kind", ["disabled", "erased"])
def test_removing_a_person_without_decide_is_not_a_removal(role: str, kind: str) -> None:
    assert removal_times(entries(created("human:u_a", role), one(kind, "human:u_a", 5))) == []


def test_a_role_change_counts_only_from_decide_to_no_decide() -> None:
    log = entries(
        created("human:u_a", "approver"),
        created("human:u_b", "viewer"),
        created("human:u_c", "approver"),
        changed("human:u_a", "approver", "viewer", 10),  # a removal
        changed("human:u_b", "viewer", "approver", 20),  # gains `decide`
        changed("human:u_c", "approver", "admin", 30),  # keeps `decide`
        changed("human:u_b", "approver", "builder", 40),  # a removal
    )
    assert removal_times(log) == [at(10), at(40)]


def test_b73_the_role_as_of_the_entry_is_rebuilt_from_the_earlier_entries() -> None:
    log = entries(
        created("human:u_apr", "approver"),
        created("human:u_bld", "builder"),
        changed("human:u_bld", "builder", "viewer", 5),
        one("disabled", "human:u_apr", 10),
        one("disabled", "human:u_bld", 20),  # never held `decide`: not a removal entry
    )
    assert removal_times(log) == [at(10)]
    # a person who was demoted first is not removed again when disabled later
    later = entries(
        created("human:u_a", "approver"),
        changed("human:u_a", "approver", "viewer", 10),
        one("disabled", "human:u_a", 20),
    )
    assert removal_times(later) == [at(10)]
    # and one promoted since their first role is
    promoted = entries(
        created("human:u_a", "viewer"),
        changed("human:u_a", "viewer", "approver", 10),
        one("erased", "human:u_a", 20),
    )
    assert removal_times(promoted) == [at(20)]


def test_only_an_active_person_is_removed() -> None:
    """A removal entry removed an ACTIVE decide-holder: erasing or demoting a person who is
    already disabled removes nobody; a person enabled again can be removed again."""
    log = entries(
        created("human:u_a", "approver"),
        one("disabled", "human:u_a", 10),
        changed("human:u_a", "approver", "viewer", 15),
        one("erased", "human:u_a", 20),
        created("human:u_b", "admin"),
        one("disabled", "human:u_b", 30),
        one("enabled", "human:u_b", 40),
        one("disabled", "human:u_b", 50),
    )
    assert removal_times(log) == [at(10), at(30), at(50)]


def test_a_person_with_no_recorded_role_and_an_unreadable_entry_are_not_removals() -> None:
    log = entries(
        one("disabled", "human:u_unknown", 5),  # no earlier entry says what they held
        ("user.disabled", 6, {}),
        ("user.role_changed", 7, {"user": "human:u_x", "from": 3, "to": None}),
        created("human:u_odd", "no-such-role"),
        one("disabled", "human:u_odd", 8),
        ("run.started", 9, {"user": "human:u_a"}),
    )
    assert removal_times(log) == []


# -- the counts ---


def row(n: int, decided: float | None, mode: str | None = "solo", status: str = "approved") -> Any:
    state = ApprovalStatus(status)
    fields: dict[str, Any] = {}
    if decided is not None:
        fields = {
            "decided_by": "human:u_owner1",
            "decided_at": at(decided),
            "decision_reason_hash": "0" * 64,
            "approval_mode": None if mode is None else ApprovalMode(mode),
        }
    if state is ApprovalStatus.CONSUMED:
        fields["consumed_at"] = at(decided or 0)
    return Approval(
        id=f"apr-{n}",
        run_id="r1",
        agent_id="omar",
        action=Action(kind=ActionKind.WRITE_FILE, target="notes.md"),
        requested_at=at(-5000 * DAY),
        status=state,
        **fields,
    )


def status(rows: list[Any], now: float, removals: list[float] | None = None) -> dict[str, Any]:
    return solo_status(
        rows, [at(r) for r in removals or []], at(now), solo_now=True, limit_per_hour=30
    )


def test_the_object_has_exactly_the_seven_keys() -> None:
    assert status([], 0) == {
        "solo_now": True,
        "limit_per_hour": 30,
        "window_days": 30,
        "approved": 0,
        "approved_solo": 0,
        "solo_share": None,
        "approved_solo_after_removal": 0,
    }


def test_b62_the_window_is_thirty_days_and_excludes_the_far_edge() -> None:
    n = 40 * DAY
    rows = [row(1, n - 2592000), row(2, n - 2591999), row(3, n)]
    got = status(rows, n)
    assert (got["approved"], got["approved_solo"]) == (2, 2)
    assert got["approved_solo_after_removal"] == 0 and got["solo_share"] == "1.0000"


def test_only_approved_rows_count_and_a_consumed_one_is_approved() -> None:
    rows = [
        row(1, 10, "solo", "consumed"),
        row(2, 20, "peer"),
        row(3, 30, None),  # decided before modes: in the total, never solo
        row(4, 40, "no_originator"),
        row(5, 50, "solo", "denied"),
        row(6, None, status="pending"),
        row(7, 5000, "solo"),  # decided after `now` (a clock set back): not in the window
    ]
    got = status(rows, 100)
    assert (got["approved"], got["approved_solo"], got["solo_share"]) == (4, 1, "0.2500")


def test_b63_seven_days_after_a_removal_includes_the_instant_and_excludes_the_edge() -> None:
    rows = [row(1, 0), row(2, 604799), row(3, 604800)]
    got = status(rows, 8 * DAY, removals=[0])
    assert (got["approved_solo"], got["approved_solo_after_removal"]) == (3, 2)


def test_b64_an_approve_is_counted_once_however_many_removals_precede_it() -> None:
    got = status([row(1, 2 * DAY)], 3 * DAY, removals=[0, DAY])
    assert got["approved_solo_after_removal"] == 1


def test_b43_one_of_two_solo_approves_is_within_seven_days_of_the_removal() -> None:
    got = status([row(1, 6 * DAY), row(2, 8 * DAY)], 9 * DAY, removals=[0])
    assert got == {
        "solo_now": True,
        "limit_per_hour": 30,
        "window_days": 30,
        "approved": 2,
        "approved_solo": 2,
        "solo_share": "1.0000",
        "approved_solo_after_removal": 1,
    }


def test_a_removal_after_the_approve_and_a_peer_approve_are_not_counted() -> None:
    rows = [row(1, 10), row(2, 30, "peer")]
    got = status(rows, 100, removals=[20])  # the removal came after the solo approve
    assert got["approved_solo_after_removal"] == 0
    old = status([row(1, 31 * DAY)], 32 * DAY, removals=[0])  # an old removal, of any age
    assert old["approved_solo_after_removal"] == 0
