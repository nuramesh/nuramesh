"""The pure fold of the `model.key_*` entries (R1.14c spec 4.3, 6, 7): states and their time
boundaries, human-only authority, the numbering rule, the conditional-append check (VK11, VK12,
VK15, VK20, VK35, VK57, VK65, VK66, VK68, VK71)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from nuramesh.core.ledger import EventType, LedgerEvent, next_event
from nuramesh.core.model_keys import KEY_PAYLOAD_KEYS, SWEEPER_ACTOR, KeyFold

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
KEY = "mk_" + "0a" * 10
OWNER = "human:u_o"


class Ledger:
    """Builds a chain of org entries at chosen times."""

    def __init__(self) -> None:
        self.last: LedgerEvent | None = None
        self.events: list[LedgerEvent] = []

    def add(
        self, kind: EventType, payload: dict[str, Any], at: datetime, actor: str = OWNER
    ) -> int:
        self.last = next_event(
            self.last, stream="org:acme", type=kind, actor=actor, at=at, payload=payload
        )
        self.events.append(self.last)
        return self.last.seq

    def stored(self, version: int, at: datetime, *, fp: str = "f1", hash_key: str = "k1") -> int:
        return self.add(
            EventType.MODEL_KEY_STORED,
            {
                "connection": "anthropic",
                "key_id": KEY,
                "version": version,
                "fingerprint": fp,
                "hash_scheme": "hmac-v1",
                "hash_key_id": hash_key,
                "hash_org": "acme",
                "via": "api",
            },
            at,
        )

    def activated(self, version: int, at: datetime, grace: int = 86400, actor: str = OWNER) -> int:
        return self.add(
            EventType.MODEL_KEY_ACTIVATED,
            {"key_id": KEY, "version": version, "grace_seconds": grace},
            at,
            actor,
        )

    def revoked(self, version: int | str, at: datetime, actor: str = OWNER) -> int:
        return self.add(EventType.MODEL_KEY_REVOKED, {"key_id": KEY, "version": version}, at, actor)

    def fold(self) -> KeyFold:
        return KeyFold().apply_all(self.events)


def two_versions() -> Ledger:
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.activated(1, T0 + timedelta(seconds=1))
    ledger.stored(2, T0 + timedelta(seconds=100))
    ledger.activated(2, T0 + timedelta(seconds=110), grace=86400)
    return ledger


def test_grace_ends_at_activated_at_plus_grace_and_that_instant_is_expired() -> None:  # VK11
    fold = two_versions().fold()
    end = T0 + timedelta(seconds=110) + timedelta(seconds=86400)
    assert fold.state(KEY, 2, end) == "current"
    assert fold.state(KEY, 1, end - timedelta(seconds=1)) == "grace"
    assert fold.state(KEY, 1, end) == "expired"
    assert fold.state(KEY, 1, end + timedelta(seconds=1)) == "expired"


def test_grace_zero_expires_the_old_version_at_once() -> None:  # VK12
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.activated(1, T0)
    ledger.stored(2, T0 + timedelta(seconds=5))
    ledger.activated(2, T0 + timedelta(seconds=6), grace=0)
    assert ledger.fold().state(KEY, 1, T0 + timedelta(seconds=6)) == "expired"


def test_pending_becomes_abandoned_only_after_600_seconds() -> None:  # VK65
    ledger = Ledger()
    ledger.stored(1, T0)
    fold = ledger.fold()
    assert fold.state(KEY, 1, T0 + timedelta(seconds=540)) == "pending"
    assert fold.state(KEY, 1, T0 + timedelta(seconds=600)) == "pending"
    assert fold.state(KEY, 1, T0 + timedelta(seconds=601)) == "abandoned"
    assert fold.has_live_pending(KEY, T0 + timedelta(seconds=600))
    assert not fold.has_live_pending(KEY, T0 + timedelta(seconds=601))


def test_a_late_activation_of_an_abandoned_version_is_ignored() -> None:  # VK65
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.activated(1, T0 + timedelta(seconds=700))
    fold = ledger.fold()
    assert fold.state(KEY, 1, T0 + timedelta(seconds=701)) == "abandoned"
    assert fold.resolve(KEY, T0 + timedelta(seconds=701)) == (None, "credential_not_active")


def test_an_activation_of_a_failed_or_revoked_version_is_ignored() -> None:  # VK66
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.add(EventType.MODEL_KEY_TEST_FAILED, {"key_id": KEY, "version": 1, "code": "x"}, T0)
    ledger.activated(1, T0 + timedelta(seconds=1))
    now = T0 + timedelta(seconds=5)
    assert ledger.fold().state(KEY, 1, now) == "failed"
    ledger.stored(2, T0 + timedelta(seconds=2))
    ledger.revoked("all", T0 + timedelta(seconds=3))
    ledger.activated(2, T0 + timedelta(seconds=4))
    fold = ledger.fold()
    assert fold.state(KEY, 2, now) == "revoked"
    assert fold.resolve(KEY, now) == (None, "credential_revoked")


def test_only_a_human_changes_state() -> None:  # VK35
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.activated(1, T0, actor=SWEEPER_ACTOR)
    assert ledger.fold().state(KEY, 1, T0) == "pending"
    ledger.activated(1, T0 + timedelta(seconds=1))
    ledger.revoked("all", T0 + timedelta(seconds=2), actor=SWEEPER_ACTOR)
    ledger.revoked(1, T0 + timedelta(seconds=2), actor="service:tok_1")
    assert ledger.fold().state(KEY, 1, T0 + timedelta(seconds=3)) == "current"
    outsider = Ledger()
    outsider.add(
        EventType.MODEL_KEY_STORED,
        {"connection": "anthropic", "key_id": KEY, "version": 1, "fingerprint": "f"},
        T0,
        actor="agent:a1",
    )
    assert outsider.fold().key(KEY) is None


def test_the_first_key_destroyed_per_version_is_kept_and_counts_for_numbering() -> None:  # VK78
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.add(
        EventType.MODEL_KEY_DESTROYED,
        {"key_id": KEY, "version": 1, "cause": "rotation_lost"},
        T0,
        actor=SWEEPER_ACTOR,
    )
    ledger.add(EventType.MODEL_KEY_DESTROYED, {"key_id": KEY, "version": 1, "cause": "other"}, T0)
    ledger.add(EventType.MODEL_KEY_DESTROYED, {"key_id": KEY, "version": 4}, T0)
    fold = ledger.fold()
    key = fold.key(KEY)
    assert key is not None
    assert key.versions[1].destroyed_cause == "rotation_lost"
    assert fold.next_version(KEY, 0) == 5  # max(A=1, B=0, C=4) + 1
    assert fold.next_version(KEY, 7) == 8  # B counts too
    assert fold.next_version("mk_" + "ff" * 10, 0) == 1


def test_numbers_are_never_reused_after_a_failed_test() -> None:  # VK14
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.activated(1, T0)
    ledger.stored(2, T0)
    ledger.add(EventType.MODEL_KEY_TEST_FAILED, {"key_id": KEY, "version": 2, "code": "x"}, T0)
    assert ledger.fold().next_version(KEY, 0) == 3


def test_the_duplicate_check_sees_enabled_versions_of_the_loaded_hash_key_only() -> (
    None
):  # VK07, VK71
    ledger = Ledger()
    ledger.stored(1, T0, fp="aaa", hash_key="k1")
    ledger.activated(1, T0)
    ledger.stored(2, T0 + timedelta(seconds=1), fp="bbb", hash_key="k1")
    ledger.activated(2, T0 + timedelta(seconds=2), grace=100)
    ledger.stored(3, T0 + timedelta(seconds=3), fp="ccc", hash_key="k0")
    ledger.activated(3, T0 + timedelta(seconds=4), grace=100)
    now = T0 + timedelta(seconds=10)
    fold = ledger.fold()
    assert fold.enabled_fingerprints("k1", now) == {"aaa", "bbb"}  # v1 is in grace, v3 is k0
    assert fold.enabled_fingerprints("other", now) == set()
    later = T0 + timedelta(seconds=200)
    assert fold.enabled_fingerprints("k1", later) == set()  # v1 and v2 expired; v3 is k0


def test_revoking_a_non_current_version_leaves_the_current_one_serving() -> None:  # VK20
    ledger = two_versions()
    ledger.revoked(1, T0 + timedelta(seconds=200))
    fold = ledger.fold()
    now = T0 + timedelta(seconds=201)
    assert fold.resolve(KEY, now) == (2, None)
    assert fold.state(KEY, 1, now) == "revoked"


def test_a_new_call_after_revoke_all_is_refused_and_a_later_set_continues_numbering() -> None:
    ledger = two_versions()
    ledger.revoked("all", T0 + timedelta(seconds=200))
    assert ledger.fold().resolve(KEY, T0 + timedelta(seconds=250)) == (None, "credential_revoked")
    ledger.stored(3, T0 + timedelta(seconds=300))
    ledger.activated(3, T0 + timedelta(seconds=301))
    fold = ledger.fold()
    assert fold.resolve(KEY, T0 + timedelta(seconds=302)) == (3, None)  # revoke-all spared v3


def test_a_pinned_call_may_read_its_grace_version_but_never_the_expired_or_a_later_start() -> None:
    fold = two_versions().fold()
    activation = T0 + timedelta(seconds=110)
    during = activation + timedelta(seconds=10)
    assert fold.pinned_problem(KEY, 1, activation - timedelta(seconds=5), during) is None
    assert fold.pinned_problem(KEY, 1, activation + timedelta(seconds=1), during) == (
        "credential_not_active"
    )
    after = activation + timedelta(seconds=86400)
    assert fold.pinned_problem(KEY, 1, activation - timedelta(seconds=5), after) == (
        "credential_not_active"
    )
    assert fold.pinned_problem(KEY, 2, activation + timedelta(seconds=1), after) is None


def test_a_retry_after_a_revoke_is_refused_as_revoked() -> None:  # VK68
    ledger = two_versions()
    ledger.revoked("all", T0 + timedelta(seconds=150))
    fold = ledger.fold()
    assert fold.pinned_problem(KEY, 1, T0, T0 + timedelta(seconds=151)) == "credential_revoked"


def test_the_conditional_append_check() -> None:  # VK76, VK77
    ledger = Ledger()
    ledger.stored(1, T0)
    seq1 = ledger.activated(1, T0 + timedelta(seconds=1))
    now = T0 + timedelta(seconds=2)
    assert not ledger.fold().append_refused(KEY, seq1, now)
    ledger.stored(2, T0 + timedelta(seconds=3))  # a winner is pending
    assert ledger.fold().append_refused(KEY, seq1, now)
    ledger.activated(2, T0 + timedelta(seconds=4))  # the winner has finished (rule iii)
    assert ledger.fold().append_refused(KEY, seq1, now)
    head = ledger.last.seq if ledger.last else 0
    assert not ledger.fold().append_refused(KEY, head, now)
    ledger.revoked("all", T0 + timedelta(seconds=5))  # a revoke after the head (rule ii)
    assert ledger.fold().append_refused(KEY, head, now)


def test_an_abandoned_pending_version_no_longer_blocks_a_new_append() -> None:  # VK65
    ledger = Ledger()
    seq = ledger.stored(1, T0)
    assert ledger.fold().append_refused(KEY, seq, T0 + timedelta(seconds=300)) is True
    assert ledger.fold().append_refused(KEY, seq, T0 + timedelta(seconds=600)) is True
    assert ledger.fold().append_refused(KEY, seq, T0 + timedelta(seconds=601)) is False


def test_erasure_verification_is_voided_by_a_later_erased_or_pending_record() -> None:  # VK79
    ledger = Ledger()
    assert ledger.fold().erasure_last is None
    ledger.add(EventType.MODEL_KEY_ERASED, {"count": 3}, T0, actor=SWEEPER_ACTOR)
    ledger.add(EventType.MODEL_KEY_ERASURE_VERIFIED, {}, T0, actor=SWEEPER_ACTOR)
    assert ledger.fold().erasure_last == "model.key_erasure_verified"
    ledger.add(EventType.MODEL_KEY_ERASURE_PENDING, {}, T0)
    assert ledger.fold().erasure_last == "model.key_erasure_pending"


def test_read_failures_are_limited_to_one_per_60_second_window() -> None:  # VK24
    ledger = Ledger()
    ledger.stored(1, T0)
    ledger.activated(1, T0)
    assert ledger.fold().read_failed_due(KEY, 1, T0)
    ledger.add(EventType.MODEL_KEY_READ_FAILED, {"key_id": KEY, "version": 1, "code": "x"}, T0)
    fold = ledger.fold()
    assert not fold.read_failed_due(KEY, 1, T0 + timedelta(seconds=59))
    assert fold.read_failed_due(KEY, 1, T0 + timedelta(seconds=60))


def test_unknown_or_damaged_entries_never_raise() -> None:
    ledger = Ledger()
    ledger.add(EventType.MODEL_KEY_STORED, {"key_id": 5, "version": "x"}, T0)
    ledger.add(EventType.MODEL_KEY_ACTIVATED, {"key_id": KEY, "version": True}, T0)
    ledger.add(EventType.MODEL_KEY_REVOKED, {"version": 1}, T0)
    ledger.add(EventType.MODEL_KEY_DESTROYED, {"key_id": KEY, "version": "1"}, T0)
    assert ledger.fold().keys.get(KEY) is None or not ledger.fold().keys[KEY].versions


def test_every_key_event_type_has_a_closed_payload_key_set() -> None:
    key_events = {e.value for e in EventType if e.value.startswith("model.key_")}
    assert key_events == set(KEY_PAYLOAD_KEYS)
    for keys in KEY_PAYLOAD_KEYS.values():
        assert not {"value", "key", "secret", "reason", "text"} & keys


def test_a_destroyed_version_cannot_be_stored_afterwards() -> None:  # Opus review m1
    ledger = Ledger()
    seq = ledger.stored(1, T0)
    ledger.activated(1, T0 + timedelta(seconds=1))
    head = ledger.last.seq if ledger.last else seq
    now = T0 + timedelta(seconds=2)
    assert not ledger.fold().append_refused(KEY, head, now, version=2)
    ledger.add(
        EventType.MODEL_KEY_DESTROYED,
        {"key_id": KEY, "version": 2, "via": "sweeper", "cause": "orphan"},
        T0 + timedelta(seconds=3),
        actor="service:key_sweeper",
    )
    assert ledger.fold().append_refused(KEY, head, now, version=2)
    assert not ledger.fold().append_refused(KEY, head, now, version=3)
