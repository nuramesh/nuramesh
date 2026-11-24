"""Merge Gate ledger event types (spec LA-1, LA-2; Fable fix F-8): fifteen additive
`merge.*` `EventType` values and the actor-by-event-type table the append paths enforce. Nothing
in the hashed ledger envelope changes (ledger v1: eleven keys, new data only inside `payload`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter

from nuramesh.core.ledger import EventType, LedgerEvent, TypeName, next_event
from nuramesh.core.vm_events import (
    COLLECTOR_ACTOR,
    GATE_ACTOR,
    HUMAN_ACTORS,
    MERGE_EVENT_ACTORS,
    MERGE_EVENT_TYPES,
    SIGNED_HUMAN_EVENTS,
    SIGNER_ACTOR,
    check_merge_actor,
)

LA_1 = (
    "merge.evidence_recorded",
    "merge.review_requested",
    "merge.review_failed",
    "merge.review_cost",
    "merge.review_reset",
    "merge.verdict_issued",
    "merge.approval_recorded",
    "merge.approval_withdrawn",
    "merge.approver_enrolled",
    "merge.key_changed",
    "merge.gate_decided",
    "merge.queue_reverified",
    "merge.completed",
    "merge.unverified_landing",
    "merge.override",
)


def test_the_fifteen_la_1_types_exist_and_follow_the_grammar() -> None:
    values = {t.value for t in EventType if t.value.startswith("merge.")}
    assert values == set(LA_1) and len(LA_1) == 15
    assert tuple(t.value for t in MERGE_EVENT_TYPES) == LA_1
    adapter = TypeAdapter(TypeName)
    for name in LA_1:
        adapter.validate_python(name)


def test_the_hashed_envelope_is_unchanged() -> None:
    assert set(LedgerEvent.model_fields) == {
        "stream",
        "seq",
        "type",
        "actor",
        "at",
        "run_id",
        "task_id",
        "payload",
        "cost",
        "cost_basis",
        "prev_hash",
        "hash",
    }
    at = datetime(2026, 1, 1, tzinfo=UTC)
    first = next_event(None, stream="org:o1", type=EventType.ORG_HALTED, actor="system", at=at)
    # frozen: the hash of a fixed pre-existing event; a change here means the envelope moved
    assert first.hash == "235a57a0759320a844925e3c7b31cb8623a84e121ea3798984e20d3b29bf04b6"


def test_new_types_append_with_their_actors() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    prev = None
    for type_name in LA_1:
        actors = MERGE_EVENT_ACTORS[type_name]
        actor = "human:admin-1" if "human:*" in actors else sorted(actors)[0]
        prev = next_event(prev, stream="org:o1", type=EventType(type_name), actor=actor, at=at)
        assert prev.type == type_name


def test_actor_table_f8() -> None:
    assert set(MERGE_EVENT_ACTORS) == set(LA_1)
    assert MERGE_EVENT_ACTORS["merge.verdict_issued"] == frozenset({SIGNER_ACTOR})
    assert MERGE_EVENT_ACTORS["merge.review_failed"] == frozenset({SIGNER_ACTOR})
    assert MERGE_EVENT_ACTORS["merge.review_cost"] == frozenset({SIGNER_ACTOR})
    assert MERGE_EVENT_ACTORS["merge.review_requested"] == frozenset({SIGNER_ACTOR})
    assert MERGE_EVENT_ACTORS["merge.evidence_recorded"] == frozenset({COLLECTOR_ACTOR})
    for gate_only in ("merge.gate_decided", "merge.queue_reverified", "merge.completed"):
        assert MERGE_EVENT_ACTORS[gate_only] == frozenset({GATE_ACTOR})
    assert MERGE_EVENT_ACTORS["merge.unverified_landing"] == frozenset({GATE_ACTOR})
    for human in ("merge.review_reset", "merge.approval_recorded", "merge.approval_withdrawn"):
        assert MERGE_EVENT_ACTORS[human] == HUMAN_ACTORS
    assert MERGE_EVENT_ACTORS["merge.override"] == HUMAN_ACTORS
    assert MERGE_EVENT_ACTORS["merge.approver_enrolled"] == HUMAN_ACTORS
    assert SIGNED_HUMAN_EVENTS == frozenset(
        {
            "merge.review_reset",
            "merge.approval_recorded",
            "merge.approval_withdrawn",
            "merge.override",
        }
    )
    assert SIGNER_ACTOR == "service:merge-gate-signer"
    assert GATE_ACTOR == "service:merge-gate"
    assert COLLECTOR_ACTOR == "service:merge-gate-collector"


@pytest.mark.parametrize(
    ("event", "actor", "expected"),
    [
        ("merge.verdict_issued", SIGNER_ACTOR, None),
        ("merge.verdict_issued", GATE_ACTOR, "actor_forbidden"),  # T-I8
        ("merge.verdict_issued", "human:admin-1", "actor_forbidden"),
        ("merge.verdict_issued", "system", "actor_forbidden"),
        ("merge.gate_decided", GATE_ACTOR, None),
        ("merge.gate_decided", SIGNER_ACTOR, "actor_forbidden"),
        ("merge.review_reset", "human:admin-1", None),
        ("merge.review_reset", SIGNER_ACTOR, "actor_forbidden"),
        ("merge.review_reset", "human:", "actor_forbidden"),
        ("merge.review_reset", "humanx:admin-1", "actor_forbidden"),
        ("merge.override", "human:admin-1", None),
        ("merge.override", "operator:root", "actor_forbidden"),
        ("merge.key_changed", SIGNER_ACTOR, None),
        ("merge.key_changed", "human:admin-1", None),
        ("merge.key_changed", GATE_ACTOR, "actor_forbidden"),
        ("run.started", "system", "unknown_event_type"),
        ("merge.not_a_type", GATE_ACTOR, "unknown_event_type"),
    ],
)
def test_check_merge_actor(event: str, actor: str, expected: str | None) -> None:
    assert check_merge_actor(event, actor) == expected


def test_ledger_append_refuses_wrong_merge_actor() -> None:
    """GC-13 / GC-56: VM-2 wires `check_merge_actor` into the ledger append path (the VM-1
    strict-xfail marker is removed), so a human can no longer append a verdict."""
    at = datetime(2026, 11, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        next_event(
            None,
            stream="org:o1",
            type=EventType("merge.verdict_issued"),
            actor="human:example-user",
            at=at,
        )


@pytest.mark.parametrize(
    ("event", "actor", "allowed"),
    [
        ("merge.verdict_issued", SIGNER_ACTOR, True),
        ("merge.verdict_issued", GATE_ACTOR, False),
        ("merge.verdict_issued", "service:merge-gate-signer-2", False),
        ("merge.verdict_issued", "system", False),
        ("merge.review_requested", SIGNER_ACTOR, True),
        ("merge.review_reset", "human:admin-1", True),
        ("merge.review_reset", SIGNER_ACTOR, False),
        ("merge.gate_decided", GATE_ACTOR, True),
        ("merge.gate_decided", SIGNER_ACTOR, False),
    ],
)
def test_ledger_append_enforces_the_actor_table_f8(event: str, actor: str, allowed: bool) -> None:
    """F-8, GC-13: the append path builds an event only for an actor the table allows."""
    at = datetime(2026, 11, 1, tzinfo=UTC)
    kind = EventType(event)
    if allowed:
        built = next_event(None, stream="org:o1", type=kind, actor=actor, at=at)
        assert built.type == event and built.actor == actor
        return
    with pytest.raises(ValueError, match="actor_forbidden"):
        next_event(None, stream="org:o1", type=kind, actor=actor, at=at)


def test_ledger_append_leaves_other_event_types_alone() -> None:
    """The actor table governs `merge.*` only; other events keep their own rules."""
    at = datetime(2026, 11, 1, tzinfo=UTC)
    built = next_event(None, stream="org:o1", type=EventType.RUN_STARTED, actor="system", at=at)
    assert built.type == "run.started"
