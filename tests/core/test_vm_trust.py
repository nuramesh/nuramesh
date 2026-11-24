"""Trust-list maintenance for the signer key (spec SV-6, SV-7, 6.7, FR-8; Fable fixes F-7 holder,
F-12 flip protocol: rotate the shadow key and mark it `compromised`): pure functions that produce
the next trust list (serial + 1) and the `merge.key_changed` payload (ids and hashes only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nuramesh.core.vm_formats import (
    KeyIdCollision,
    TrustList,
    format_time,
    payload_hash,
    public_key_id,
)
from nuramesh.core.vm_trust import (
    accept_trust,
    add_signer_key,
    key_changed_payload,
    mark_compromised,
    revoke_key,
    rotate_signer_key,
    signer_key_status,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=10)
PUB1, PUB2, PUB3 = "7" * 64, "8" * 64, "9" * 64
HOLDER = "service:reviewer-a"


def ts(when: datetime) -> str:
    return format_time(when)


def key(public: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key_id": public_key_id(public),
        "role": "verdict-signer",
        "holder": HOLDER,
        "roles": [],
        "public_key": public,
        "custody": "service",
        "valid_from": ts(T0),
        "valid_to": None,
        "revoked_at": None,
        "revoked_seq": None,
        "compromised": False,
    }
    base.update(over)
    return base


def trust(*keys: dict[str, Any], serial: int = 1) -> TrustList:
    return TrustList.model_validate(
        {"version": "vm-trust-1", "org_id": "org-example", "serial": serial, "keys": list(keys)}
    )


def test_signer_key_status_codes() -> None:
    t = trust(key(PUB1))
    kid = public_key_id(PUB1)
    assert signer_key_status(t, kid, "reviewer-a", NOW) is None
    assert signer_key_status(t, "0" * 16, "reviewer-a", NOW) == "key_not_in_trust_list"
    assert signer_key_status(t, kid, "reviewer-b", NOW) == "key_holder"
    assert (
        signer_key_status(trust(key(PUB1, role="attestor", holder=HOLDER)), kid, "reviewer-a", NOW)
        == "key_role"
    )
    assert (
        signer_key_status(trust(key(PUB1, compromised=True)), kid, "reviewer-a", NOW)
        == "key_compromised"
    )
    assert (
        signer_key_status(
            trust(key(PUB1, revoked_at=ts(NOW), revoked_seq=4)), kid, "reviewer-a", NOW
        )
        == "key_revoked"
    )
    assert (
        signer_key_status(trust(key(PUB1, revoked_at=ts(NOW))), kid, "reviewer-a", NOW)
        == "key_entry_invalid"
    )
    assert (
        signer_key_status(trust(key(PUB1, valid_to=ts(NOW))), kid, "reviewer-a", NOW)
        == "key_expired"
    )
    assert signer_key_status(
        trust(key(PUB1, valid_from=ts(NOW + timedelta(1)))), kid, "reviewer-a", NOW
    ) == ("key_not_yet_valid")
    assert signer_key_status(trust(key(PUB1, valid_from=ts(NOW))), kid, "reviewer-a", NOW) is None
    wrong_id = trust(key(PUB1, key_id="1" * 16))
    assert signer_key_status(wrong_id, "1" * 16, "reviewer-a", NOW) == "key_entry_invalid"


def test_add_and_rotate_produce_the_next_serial_and_keep_the_holder() -> None:
    t1 = trust(key(PUB1))
    t2 = add_signer_key(t1, PUB2, holder="service:reviewer-b", now=NOW, reserved={})
    assert t2.serial == 2 and [k.key_id for k in t2.keys] == [
        public_key_id(PUB1),
        public_key_id(PUB2),
    ]
    new = t2.key(public_key_id(PUB2))
    assert new is not None and new.holder == "service:reviewer-b" and new.custody == "service"
    assert new.valid_from == ts(NOW) and new.role == "verdict-signer" and not new.compromised
    assert t1.serial == 1 and len(t1.keys) == 1  # the input is untouched
    t3 = rotate_signer_key(t2, public_key_id(PUB1), PUB3, now=NOW + timedelta(1), reserved={})
    assert t3.serial == 3
    old = t3.key(public_key_id(PUB1))
    fresh = t3.key(public_key_id(PUB3))
    # the old key's window is untouched (the gate checks windows at t = now): it is retired by
    # ledger order with `revoke_key`, so verdicts issued before the entry stay valid (SV-7)
    assert old is not None and old.valid_to is None and old.revoked_seq is None
    assert (
        fresh is not None and fresh.holder == HOLDER and fresh.valid_from == ts(NOW + timedelta(1))
    )
    assert signer_key_status(t3, old.key_id, "reviewer-a", NOW + timedelta(2)) is None
    assert signer_key_status(t3, fresh.key_id, "reviewer-a", NOW + timedelta(2)) is None
    t4 = revoke_key(t3, old.key_id, revoked_at=NOW + timedelta(1), revoked_seq=9)
    assert t4.serial == 4
    assert signer_key_status(t4, old.key_id, "reviewer-a", NOW + timedelta(2)) == "key_revoked"
    with pytest.raises(KeyIdCollision):
        add_signer_key(
            t3, "a" * 64, holder=HOLDER, now=NOW, reserved={public_key_id("a" * 64): "anchor"}
        )
    with pytest.raises(ValueError):
        add_signer_key(t3, PUB3, holder=HOLDER, now=NOW, reserved={})  # duplicate key id
    with pytest.raises(ValueError):
        rotate_signer_key(t3, "0" * 16, "b" * 64, now=NOW, reserved={})  # unknown old key
    with pytest.raises(ValueError):
        add_signer_key(
            t3, "b" * 64, holder="human:someone", now=NOW, reserved={}
        )  # F-7 holder kind


def test_mark_compromised_and_revoke_by_ledger_order() -> None:
    t1 = trust(key(PUB1), key(PUB2, holder="service:reviewer-b"))
    kid1 = public_key_id(PUB1)
    t2 = mark_compromised(t1, kid1)
    k = t2.key(kid1)
    assert t2.serial == 2 and k is not None and k.compromised and k.valid_to is None
    other = t2.key(public_key_id(PUB2))
    assert other is not None and not other.compromised
    assert signer_key_status(t2, kid1, "reviewer-a", NOW) == "key_compromised"
    t3 = revoke_key(t1, kid1, revoked_at=NOW, revoked_seq=77)
    k3 = t3.key(kid1)
    assert k3 is not None and k3.revoked_at == ts(NOW) and k3.revoked_seq == 77
    assert k3.valid_to is None and k3.problem() is None
    assert signer_key_status(t3, kid1, "reviewer-a", NOW) == "key_revoked"
    with pytest.raises(ValueError):
        revoke_key(t1, kid1, revoked_at=NOW, revoked_seq=0)
    with pytest.raises(ValueError):
        mark_compromised(t1, "0" * 16)
    assert mark_compromised(t2, kid1).serial == 3  # idempotent on the key, still a change


def test_key_changed_payload_holds_ids_and_hashes_only() -> None:
    t1 = trust(key(PUB1))
    t2 = rotate_signer_key(t1, public_key_id(PUB1), PUB2, now=NOW, reserved={})
    payload = key_changed_payload(t1, t2, change="rotated", key_id=public_key_id(PUB2))
    assert payload == {
        "org_id": "org-example",
        "serial": 2,
        "previous_serial": 1,
        "trust_sha256": payload_hash(t2),
        "previous_trust_sha256": payload_hash(t1),
        "key_id": public_key_id(PUB2),
        "holder": HOLDER,
        "role": "verdict-signer",
        "change": "rotated",
    }
    assert PUB2 not in repr(payload) and PUB1 not in repr(payload)
    with pytest.raises(ValueError):
        key_changed_payload(t1, t2, change="other", key_id=public_key_id(PUB2))
    with pytest.raises(ValueError):
        key_changed_payload(t1, t1, change="added", key_id=public_key_id(PUB1))  # serial not + 1
    with pytest.raises(ValueError):
        key_changed_payload(t1, t2, change="added", key_id="0" * 16)  # key not in the new list


def test_accept_trust_by_serial() -> None:
    t5 = trust(key(PUB1), serial=5)
    t6 = trust(key(PUB1), key(PUB2, holder="service:reviewer-b"), serial=6)
    assert accept_trust(None, t5) is None
    assert accept_trust(t5, t6) is None
    assert accept_trust(t6, t5) == "trust_rollback"
    assert accept_trust(t5, t5) is None
    assert accept_trust(t5, trust(key(PUB2, holder="service:x"), serial=5)) == "trust_rollback"
    other_org = TrustList.model_validate({**t6.model_dump(mode="json"), "org_id": "org-other"})
    assert accept_trust(t5, other_org) == "trust_org"
