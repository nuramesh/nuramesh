"""R1.37 E17/E21: the `count` detail of coalesced security-log rows (ND60 amendment, T122)."""

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from nuramesh.core.auth import COALESCED_TYPES, SecurityLogEntry, coalesced_row_id

AT = datetime(2026, 11, 1, tzinfo=UTC)


def _entry(type_: str, details: dict[str, Any]) -> SecurityLogEntry:
    return SecurityLogEntry.new("acme", AT, type_, actor="system", details=details)


@pytest.mark.parametrize("bad", [0, 1_000_001, -1, "5", True, 5.0])
def test_bad_counts_are_refused(bad: object) -> None:
    with pytest.raises(ValueError):
        _entry("content.capture_refused", {"reason": "tombstone", "count": bad})


@pytest.mark.parametrize("kind", sorted(COALESCED_TYPES))
@pytest.mark.parametrize("good", [1, 1_000_000])
def test_good_counts_on_coalesced_types(kind: str, good: int) -> None:
    assert _entry(kind, {"reason": "tombstone", "count": good}).details["count"] == good


def test_count_on_other_type_and_route_refused() -> None:
    with pytest.raises(ValueError):
        _entry("login.failed", {"count": 1})
    with pytest.raises(ValueError):
        _entry("content.capture_refused", {"route": "/v1/x"})


def test_coalesced_ids_differ_by_type_and_are_deterministic() -> None:
    a = coalesced_row_id("content.capture_refused", "acme", AT, "tombstone")
    b = coalesced_row_id("content.key_resurrected", "acme", AT, "tombstone")
    c = coalesced_row_id("content.capture_failed", "acme", AT, "tombstone")
    assert len({a, b, c}) == 3
    raw = "\0".join(["content.capture_refused", "acme", "2026-11-01", "tombstone"]).encode()
    assert a == hashlib.sha256(raw).hexdigest()[:32]
    assert a == coalesced_row_id("content.capture_refused", "acme", AT, "tombstone")
