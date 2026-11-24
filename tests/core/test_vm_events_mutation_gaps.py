"""Mutation pins for `core.vm_events` (#1336): returned values and refusals of the actor checks,
the human-id boundary and the exact table contents. Fake ids only."""

from __future__ import annotations

import pytest

from nuramesh.core.ledger import EventType
from nuramesh.core.vm_events import (
    COLLECTOR_ACTOR,
    GATE_ACTOR,
    HUMAN_ACTORS,
    MERGE_EVENT_ACTORS,
    MERGE_EVENT_TYPES,
    SIGNED_HUMAN_EVENTS,
    SIGNER_ACTOR,
    actor_allowed,
    check_merge_actor,
)

_HUMAN_TYPES = (
    "merge.review_reset",
    "merge.approval_recorded",
    "merge.approval_withdrawn",
    "merge.override",
    "merge.approver_enrolled",
)
_SERVICE_ONLY = {
    "merge.evidence_recorded": COLLECTOR_ACTOR,
    "merge.review_requested": SIGNER_ACTOR,
    "merge.review_failed": SIGNER_ACTOR,
    "merge.review_cost": SIGNER_ACTOR,
    "merge.verdict_issued": SIGNER_ACTOR,
    "merge.gate_decided": GATE_ACTOR,
    "merge.queue_reverified": GATE_ACTOR,
    "merge.completed": GATE_ACTOR,
    "merge.unverified_landing": GATE_ACTOR,
}


def test_every_service_type_accepts_only_its_one_actor() -> None:
    services = {COLLECTOR_ACTOR, SIGNER_ACTOR, GATE_ACTOR}
    for event, owner in _SERVICE_ONLY.items():
        assert MERGE_EVENT_ACTORS[event] == frozenset({owner})
        assert actor_allowed(event, owner) is True
        assert check_merge_actor(event, owner) is None
        for other in (services - {owner}) | {"human:admin-1", "system", ""}:
            assert actor_allowed(event, other) is False
            assert check_merge_actor(event, other) == "actor_forbidden"


def test_human_types_refuse_every_service_actor() -> None:
    for event in _HUMAN_TYPES:
        for service in (COLLECTOR_ACTOR, SIGNER_ACTOR, GATE_ACTOR):
            assert actor_allowed(event, service) is False
            assert check_merge_actor(event, service) == "actor_forbidden"
        assert actor_allowed(event, "human:admin-1") is True


def test_key_changed_allows_signer_and_humans_only() -> None:
    assert MERGE_EVENT_ACTORS["merge.key_changed"] == frozenset({SIGNER_ACTOR, "human:*"})
    assert actor_allowed("merge.key_changed", SIGNER_ACTOR) is True
    assert actor_allowed("merge.key_changed", "human:admin-1") is True
    assert actor_allowed("merge.key_changed", GATE_ACTOR) is False
    assert actor_allowed("merge.key_changed", COLLECTOR_ACTOR) is False


@pytest.mark.parametrize("body", ["a", "A", "z9", "a_b", "a-b", "-", "_", "0", "x" * 64])
def test_human_id_body_accepts_the_documented_alphabet_up_to_64(body: str) -> None:
    assert actor_allowed("merge.override", f"human:{body}") is True
    assert check_merge_actor("merge.override", f"human:{body}") is None


@pytest.mark.parametrize(
    "actor",
    [
        "human:",
        "human:" + "x" * 65,
        "human:a:b",
        "human:a b",
        "human:a.b",
        "human:a@b",
        "human:ab\n",
        "\nhuman:ab",
        " human:ab",
        "human:ab ",
        "xhuman:ab",
        "Human:ab",
        "HUMAN:ab",
        "human-ab",
        "humanab",
        "human:é",
        "human:a/b",
        "",
    ],
)
def test_malformed_human_actor_is_refused(actor: str) -> None:
    assert actor_allowed("merge.override", actor) is False
    assert check_merge_actor("merge.override", actor) == "actor_forbidden"
    assert check_merge_actor("merge.key_changed", actor) == "actor_forbidden"


def test_actor_allowed_is_false_for_unknown_and_non_merge_types() -> None:
    for event in ("run.started", "merge.not_a_type", "", "merge.", "MERGE.OVERRIDE"):
        assert actor_allowed(event, GATE_ACTOR) is False
        assert actor_allowed(event, "human:admin-1") is False


def test_check_merge_actor_unknown_type_beats_actor_judgement() -> None:
    assert check_merge_actor("run.started", GATE_ACTOR) == "unknown_event_type"
    assert check_merge_actor("run.started", "") == "unknown_event_type"
    assert check_merge_actor("merge.override", "") == "actor_forbidden"


def test_check_merge_actor_returns_plain_strings() -> None:
    assert check_merge_actor("run.started", "system") == "unknown_event_type"
    assert check_merge_actor("merge.completed", "system") == "actor_forbidden"
    assert check_merge_actor("merge.completed", GATE_ACTOR) is None


def test_constants_and_tables_are_exact() -> None:
    assert HUMAN_ACTORS == frozenset({"human:*"})
    assert GATE_ACTOR == "service:merge-gate"
    assert SIGNER_ACTOR == "service:merge-gate-signer"
    assert COLLECTOR_ACTOR == "service:merge-gate-collector"
    assert len({GATE_ACTOR, SIGNER_ACTOR, COLLECTOR_ACTOR}) == 3
    assert isinstance(MERGE_EVENT_TYPES, tuple) and len(MERGE_EVENT_TYPES) == 15
    assert len(set(MERGE_EVENT_TYPES)) == 15
    assert all(isinstance(t, EventType) for t in MERGE_EVENT_TYPES)
    assert set(MERGE_EVENT_ACTORS) == {t.value for t in MERGE_EVENT_TYPES}
    for event in _HUMAN_TYPES:
        assert MERGE_EVENT_ACTORS[event] == HUMAN_ACTORS
    assert SIGNED_HUMAN_EVENTS == frozenset(_HUMAN_TYPES[:4])
    assert "merge.approver_enrolled" not in SIGNED_HUMAN_EVENTS
    assert "merge.key_changed" not in SIGNED_HUMAN_EVENTS
    assert all(isinstance(v, frozenset) for v in MERGE_EVENT_ACTORS.values())
