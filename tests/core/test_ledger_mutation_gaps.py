"""Mutation-gap tests for `nuramesh.core.ledger` (survivors: strict string fields, the payload
size limit, the timezone check, the `checked` counter and the frozen models)."""

from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.canonical import canonical_json
from nuramesh.core.ledger import (
    MAX_PAYLOAD_BYTES,
    ChainProblem,
    EventType,
    LedgerEvent,
    next_event,
    verify_chain,
)

T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


def _events(n: int) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    previous: LedgerEvent | None = None
    for i in range(n):
        previous = next_event(
            previous,
            stream="run:r1",
            type=EventType.RUN_STARTED,
            actor="system",
            at=T0 + timedelta(seconds=i),
            payload={"i": i},
        )
        out.append(previous)
    return out


def _append(payload: dict[str, Any]) -> LedgerEvent:
    return next_event(
        None, stream="run:r1", type=EventType.RUN_STARTED, actor="system", at=T0, payload=payload
    )


def test_payload_limit_is_exactly_max_payload_bytes() -> None:
    assert MAX_PAYLOAD_BYTES == 256 * 1024
    overhead = len(canonical_json({"k": ""}))
    at_limit = {"k": "x" * (MAX_PAYLOAD_BYTES - overhead)}
    assert len(canonical_json(at_limit)) == MAX_PAYLOAD_BYTES
    assert _append(at_limit).payload["k"] == at_limit["k"]
    with pytest.raises(ValidationError):
        _append({"k": "x" * (MAX_PAYLOAD_BYTES - overhead + 1)})


@pytest.mark.parametrize(
    "field,value",
    [
        ("stream", b"run:r1"),
        ("actor", b"system"),
        ("type", b"run.started"),
        ("prev_hash", b"0" * 64),
        ("hash", b"0" * 64),
        ("cost_basis", b"actual"),
    ],
)
def test_string_fields_are_strict(field: str, value: bytes) -> None:
    good = _events(1)[0].model_dump()
    assert LedgerEvent.model_validate(good)
    with pytest.raises(ValidationError):
        LedgerEvent.model_validate({**good, field: value})


def test_a_timestamp_whose_offset_is_unknown_is_refused() -> None:
    class NoOffset(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, dt: datetime | None) -> str | None:
            return None

    aware_but_unknown = datetime(2026, 1, 1, tzinfo=NoOffset())
    assert aware_but_unknown.tzinfo is not None and aware_but_unknown.utcoffset() is None
    with pytest.raises(ValidationError, match="timezone-aware"):
        next_event(
            None,
            stream="org:acme",
            type=EventType.RUN_STARTED,
            actor="system",
            at=aware_but_unknown,
        )


def test_events_and_reports_are_frozen_and_closed() -> None:
    event = _events(1)[0]
    with pytest.raises(ValidationError):
        event.seq = 2
    with pytest.raises(ValidationError):
        LedgerEvent.model_validate({**event.model_dump(), "extra_field": 1})
    report = verify_chain([event])
    with pytest.raises(ValidationError):
        report.ok = False


def test_checked_counts_only_the_events_before_the_first_problem() -> None:
    ok = verify_chain(_events(3))
    assert ok.ok and ok.checked == 3

    first = _events(3)
    assert verify_chain(first[1:]).checked == 0  # fails on the very first event
    assert verify_chain(first[1:]).problem == ChainProblem.WRONG_SEQ

    gap = [first[0], first[1], first[1]]
    report = verify_chain(gap)
    assert report.problem == ChainProblem.WRONG_SEQ and report.checked == 2

    other = _events(3)
    swapped = [first[0], other[1].model_copy(update={"stream": "run:r2"})]
    report = verify_chain(swapped)
    assert report.problem == ChainProblem.WRONG_STREAM and report.checked == 1

    removed_tail = verify_chain(first[:2], expected_head=first[2].hash)
    assert removed_tail.checked == 2 and not removed_tail.ok


def test_an_empty_stream_with_an_expected_head_reports_seq_zero() -> None:
    report = verify_chain([], expected_head="a" * 64)
    assert not report.ok and report.problem == ChainProblem.BROKEN_LINK
    assert report.checked == 0 and report.at_seq == 0
