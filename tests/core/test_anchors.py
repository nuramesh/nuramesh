"""Anchor data and the comparison rules (R1.04a, ND22): pure, no keys, no Git."""

import json
from datetime import UTC, datetime

import pytest

from nuramesh.core.anchors import (
    Anchor,
    AnchorFileError,
    anchor_filename,
    check_anchors,
    parse_anchor_lines,
    unanchored_events,
)
from nuramesh.core.ledger import EventType, LedgerEvent, next_event

HEAD = "a" * 64


AT = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)


def events(n: int, start: int = 0) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    previous: LedgerEvent | None = None
    for i in range(n):
        previous = next_event(
            previous,
            stream="run:r1",
            type=EventType.RUN_STARTED,
            actor="system",
            at=AT,
            payload={"n": start + i},
        )
        out.append(previous)
    return out


def anchor(seq: int, head: str, at: str = "2026-10-22T10:00:00.000000Z") -> Anchor:
    return Anchor(stream="run:r1", seq=seq, head=head, at=at, key_id="0" * 16, sig="0" * 128)


def test_a_matching_anchor_has_no_problems_and_counts_newer_events() -> None:
    chain = events(5)
    anchors = [anchor(3, chain[2].hash)]
    assert check_anchors("run:r1", anchors, chain) == []
    assert unanchored_events(anchors, chain) == 2
    assert unanchored_events([], chain) == 5 and unanchored_events([], []) == 0


def test_an_edited_event_or_a_removed_tail_is_caught() -> None:
    chain = events(5)
    good = anchor(3, chain[2].hash)
    forged = events(3, start=100)  # a different, internally consistent history
    assert [p.code for p in check_anchors("run:r1", [good], forged)] == ["head_mismatch"]
    short = check_anchors("run:r1", [anchor(5, chain[4].hash)], chain[:3])
    assert [p.code for p in short] == ["stream_shorter"]
    assert [p.code for p in check_anchors("run:r1", [good], [])] == ["stream_shorter"]


def test_anchors_may_not_go_backwards() -> None:
    chain = events(5)
    later = anchor(4, chain[3].hash, "2026-10-22T11:00:00.000000Z")
    earlier_seq = anchor(2, chain[1].hash, "2026-10-22T12:00:00.000000Z")
    assert "anchors_out_of_order" in [
        p.code for p in check_anchors("run:r1", [later, earlier_seq], chain)
    ]
    earlier_time = anchor(5, chain[4].hash, "2026-10-22T09:00:00.000000Z")
    assert "anchors_out_of_order" in [
        p.code for p in check_anchors("run:r1", [later, earlier_time], chain)
    ]


def test_the_file_format_is_strict() -> None:
    a = anchor(1, HEAD)
    assert parse_anchor_lines(a.line() + "\n", stream="run:r1") == [a]
    for bad in ("", "\n\n", "not json", json.dumps({"v": 1}), a.line().replace("run:r1", "run:r2")):
        text = a.line() + "\n" + bad + "\n"
        with pytest.raises(AnchorFileError):
            parse_anchor_lines(text, stream="run:r1")
    with pytest.raises(AnchorFileError):
        parse_anchor_lines("x" * 5000, stream="run:r1")
    extra = json.loads(a.line())
    extra["extra"] = 1
    with pytest.raises(AnchorFileError):
        parse_anchor_lines(json.dumps(extra), stream="run:r1")


def test_anchor_fields_are_validated() -> None:
    for kw in (
        {"seq": 0},
        {"head": "abc"},
        {"at": "yesterday"},
        {"key_id": "xyz"},
        {"sig": "00"},
        {"at": "2026-13-45T10:00:00.000000Z"},
    ):
        base = {
            "stream": "run:r1",
            "seq": 1,
            "head": HEAD,
            "at": "2026-10-22T10:00:00.000000Z",
            "key_id": "0" * 16,
            "sig": "0" * 128,
        }
        with pytest.raises(ValueError):
            Anchor.model_validate({**base, **kw})


def test_filenames_and_the_signed_message() -> None:
    assert (
        anchor_filename("run:r1") == "run__r1.jsonl"
        and anchor_filename("org:acme") == "org__acme.jsonl"
    )
    a = anchor(1, HEAD)
    assert a.message() != anchor(2, HEAD).message() != anchor(1, "b" * 64).message()
    assert a.message() == anchor(1, HEAD).message()
