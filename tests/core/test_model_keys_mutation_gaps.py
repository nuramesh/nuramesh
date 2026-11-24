"""Mutation pins for `nuramesh.core.model_keys` (Part of #1194): the supersede grace boundary
(one second before the end, at the end), the refusal codes of `resolve` and `pinned_problem`,
the activation guard, and the fold's answers for keys and versions it does not know."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nuramesh.core.ledger import EventType, LedgerEvent, next_event
from nuramesh.core.model_keys import (
    DEFAULT_GRACE_SECONDS,
    KeyFold,
    KeyVersion,
    StoredKey,
)

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
KEY = "mk_" + "0a" * 10
OWNER = "human:u_o"


class Ledger:
    """Builds a chain of org entries at chosen times."""

    def __init__(self) -> None:
        self.last: LedgerEvent | None = None
        self.events: list[LedgerEvent] = []

    def add(self, kind: EventType, payload: dict[str, Any], at: datetime) -> int:
        self.last = next_event(
            self.last, stream="org:acme", type=kind, actor=OWNER, at=at, payload=payload
        )
        self.events.append(self.last)
        return self.last.seq

    def stored(self, version: int, at: datetime) -> int:
        payload = {"connection": "anthropic", "key_id": KEY, "version": version}
        return self.add(
            EventType.MODEL_KEY_STORED, {**payload, "fingerprint": "f1", "hash_key_id": "k1"}, at
        )

    def activated(self, version: int, at: datetime, grace: int = 86400) -> int:
        payload = {"key_id": KEY, "version": version, "grace_seconds": grace}
        return self.add(EventType.MODEL_KEY_ACTIVATED, payload, at)

    def revoked(self, version: int | str, at: datetime) -> int:
        return self.add(EventType.MODEL_KEY_REVOKED, {"key_id": KEY, "version": version}, at)

    def fold(self) -> KeyFold:
        return KeyFold().apply_all(self.events)


def two_versions() -> Ledger:
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.activated(1, T0 + timedelta(seconds=1))
    ledger.stored(2, T0 + timedelta(seconds=100))
    ledger.activated(2, T0 + timedelta(seconds=110), grace=86400)
    return ledger


GRACE_END = T0 + timedelta(seconds=110) + timedelta(seconds=86400)


def test_grace_until_is_none_until_superseded_then_activation_plus_grace() -> None:
    fold = two_versions().fold()
    assert fold.keys[KEY].versions[2].grace_until is None
    assert fold.keys[KEY].versions[1].grace_until == GRACE_END


def test_the_grace_boundary_is_exact_for_state_and_for_pinned_calls() -> None:
    fold = two_versions().fold()
    started = T0 + timedelta(seconds=50)  # reserved before the successor was activated
    before = GRACE_END - timedelta(seconds=1)
    assert fold.state(KEY, 1, before) == "grace"
    assert fold.pinned_problem(KEY, 1, started, before) is None
    assert fold.state(KEY, 1, GRACE_END) == "expired"
    assert fold.pinned_problem(KEY, 1, started, GRACE_END) == "credential_not_active"


def test_a_call_reserved_after_the_successor_may_not_read_the_old_version_in_grace() -> None:
    fold = two_versions().fold()
    after = T0 + timedelta(seconds=111)
    assert fold.pinned_problem(KEY, 1, after, after) == "credential_not_active"


def test_pinned_problem_codes() -> None:
    ledger = two_versions()
    ledger.revoked(1, T0 + timedelta(seconds=200))
    fold = ledger.fold()
    now = T0 + timedelta(seconds=300)
    assert fold.pinned_problem(KEY, 2, now, now) is None
    assert fold.pinned_problem(KEY, 1, now, now) == "credential_revoked"
    assert fold.pinned_problem(KEY, 9, now, now) == "credential_not_active"
    assert fold.pinned_problem("mk_unknown", 1, now, now) == "credential_not_active"


def test_resolve_codes_for_unknown_current_revoked_and_empty_keys() -> None:
    now = T0 + timedelta(seconds=300)
    assert KeyFold().resolve("mk_unknown", now) == (None, "credential_not_active")
    ledger = two_versions()
    assert ledger.fold().resolve(KEY, now) == (2, None)
    ledger.revoked("all", T0 + timedelta(seconds=200))
    assert ledger.fold().resolve(KEY, now) == (None, "credential_revoked")
    pending = Ledger()
    pending.stored(1, T0)
    assert pending.fold().resolve(KEY, T0) == (None, "credential_not_active")


def test_resolve_when_the_current_version_is_revoked_or_not_current_in_a_folded_state() -> None:
    now = T0 + timedelta(seconds=300)
    stored = T0

    def version(**kw: object) -> KeyVersion:
        return KeyVersion(1, 1, stored, "f", "k", "human:u_o", activated_seq=2, **kw)  # type: ignore[arg-type]

    revoked = StoredKey(KEY, "anthropic", {1: version(revoked=True)}, current_version=1)
    assert KeyFold({KEY: revoked}).resolve(KEY, now) == (None, "credential_revoked")
    expired = version(superseded_at=T0, superseded_grace=0)
    key = StoredKey(KEY, "anthropic", {1: expired}, current_version=1)
    assert KeyFold({KEY: key}).resolve(KEY, now) == (None, "credential_not_active")


def test_unknown_keys_and_versions_have_no_state() -> None:
    fold = two_versions().fold()
    assert fold.state("mk_unknown", 1, T0) is None
    assert fold.state(KEY, 9, T0) is None
    assert fold.read_failed_due("mk_unknown", 1, T0) is False
    assert fold.read_failed_due(KEY, 9, T0) is False
    assert fold.read_failed_due(KEY, 1, T0) is True


def test_a_failed_or_revoked_version_cannot_be_activated() -> None:
    failed = Ledger()
    failed.stored(1, T0)
    failed.add(EventType.MODEL_KEY_TEST_FAILED, {"key_id": KEY, "version": 1}, T0)
    failed.activated(1, T0 + timedelta(seconds=5))
    assert failed.fold().state(KEY, 1, T0 + timedelta(seconds=6)) == "failed"
    revoked = Ledger()
    revoked.stored(1, T0)
    revoked.revoked(1, T0 + timedelta(seconds=2))
    revoked.activated(1, T0 + timedelta(seconds=5))
    folded = revoked.fold()
    assert folded.keys[KEY].versions[1].activated_seq is None
    assert folded.keys[KEY].current_version is None
    twice = Ledger()
    twice.stored(1, T0)
    seq = twice.activated(1, T0 + timedelta(seconds=1), grace=5)
    twice.activated(1, T0 + timedelta(seconds=9), grace=7)
    version = twice.fold().keys[KEY].versions[1]
    assert (version.activated_seq, version.grace_seconds) == (seq, 5)


def test_a_destroyed_entry_marks_the_version_and_keeps_its_cause() -> None:
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.add(
        EventType.MODEL_KEY_DESTROYED,
        {"key_id": KEY, "version": 1, "cause": "sweeper"},
        T0 + timedelta(seconds=1),
    )
    version = ledger.fold().keys[KEY].versions[1]
    assert version.destroyed is True
    assert version.destroyed_cause == "sweeper"


@pytest.mark.parametrize("kind", [EventType.MODEL_KEY_ERASED, EventType.MODEL_KEY_ERASURE_PENDING])
def test_apply_returns_the_fold_for_erasure_records(kind: EventType) -> None:
    ledger = Ledger()
    ledger.add(kind, {"via": "api"}, T0)
    fold = KeyFold()
    assert fold.apply(ledger.events[0]) is fold
    assert fold.erasure_last == kind.value


def test_defaults_of_a_fresh_fold_and_the_default_grace() -> None:
    assert KeyFold().seq == 0
    assert StoredKey("k", "c").last_seq == 0
    fresh = KeyVersion(1, 1, T0, None, None, "human:u_o")
    assert (fresh.grace_seconds, fresh.superseded_grace) == (0, 0)
    assert DEFAULT_GRACE_SECONDS == 24 * 3600
