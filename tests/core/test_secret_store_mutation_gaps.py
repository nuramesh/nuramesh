"""Behaviour pins for `nuramesh.core.secret_store` (mutation gaps, issue 1342). Tests only; the
values are generated at run time."""

import hashlib
import secrets
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nuramesh.core.secret_store import (
    ERROR_CODES,
    FakeSecretStore,
    SecretMeta,
    SecretRef,
    SecretStoreError,
    org_tag,
)
from nuramesh.core.secrets_text import Redacted, reveal_secret

K1 = "mk_" + "1f" * 10
K2 = "mk_" + "2a" * 10


def ref(org: str = "acme", version: int = 1, key: str = K1) -> SecretRef:
    return SecretRef(org_id=org, key_id=key, version=version)


def val() -> Redacted:
    return Redacted(secrets.token_urlsafe(24))


def code_of(call) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(SecretStoreError) as info:
        call()
    return info.value.code


def test_org_tag_is_the_first_sixteen_hex_of_sha256() -> None:
    tag = org_tag("org-x")
    assert len(tag) == 16
    assert tag == hashlib.sha256(b"org-x").hexdigest()[:16]
    assert tag != org_tag("org-y")


def test_name_has_the_fixed_shape() -> None:
    r = ref(org="org-x", version=7)
    assert r.name == f"nm-{org_tag('org-x')}-{'1f' * 10}-v7"
    assert "_" not in r.name


def test_ref_limits() -> None:
    assert ref(version=1).version == 1
    assert ref(version=1_000_000).version == 1_000_000
    for bad in (0, 1_000_001):
        with pytest.raises(ValidationError):
            ref(version=bad)
    assert SecretRef(org_id="o" * 200, key_id=K1, version=1)
    with pytest.raises(ValidationError):
        SecretRef(org_id="o" * 201, key_id=K1, version=1)
    with pytest.raises(ValidationError):
        SecretRef(org_id="", key_id=K1, version=1)
    with pytest.raises(ValidationError):
        SecretRef(org_id="a", key_id=K1, version=1, extra=1)  # type: ignore[call-arg]


def test_meta_defaults_and_extra_forbidden() -> None:
    meta = SecretMeta(ref=ref(), created_at=datetime(2026, 1, 1, tzinfo=UTC), state="enabled")
    assert meta.expires_at is None
    with pytest.raises(ValidationError):
        SecretMeta(
            ref=ref(),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            state="enabled",
            x=1,  # type: ignore[call-arg]
        )


def test_error_codes_texts_and_unknown_code() -> None:
    assert ERROR_CODES == ("unavailable", "not_found", "disabled", "denied", "conflict", "invalid")
    texts = {
        "unavailable": "the secret store is not available",
        "not_found": "the secret was not found",
        "disabled": "the secret is disabled",
        "denied": "the secret store refused access",
        "conflict": "the secret version already exists",
        "invalid": "the secret request is not valid",
    }
    for code, text in texts.items():
        err = SecretStoreError(code)
        assert (err.code, str(err), err.detail) == (code, text, None)
    odd = SecretStoreError("whatever", "why")
    assert odd.code == "invalid"
    assert str(odd) == texts["invalid"]
    assert odd.detail == "why"


def test_default_clock_is_the_fixed_epoch_and_expiry_is_kept() -> None:
    store = FakeSecretStore()
    assert store.put(ref(), val()).created_at == datetime(2026, 1, 1, tzinfo=UTC)
    soon = datetime(2030, 1, 1, tzinfo=UTC)
    meta = store.put(ref(version=2), val(), expires_at=soon)
    assert meta.expires_at == soon
    assert meta.state == "enabled"
    assert store.put(ref(version=3), val()).expires_at is None


def test_injected_clock_is_used() -> None:
    when = datetime(2031, 5, 5, tzinfo=UTC)
    assert FakeSecretStore(clock=lambda: when).put(ref(), val()).created_at == when


def test_put_get_roundtrip_and_conflicts() -> None:
    store = FakeSecretStore()
    value = val()
    store.put(ref(), value)
    assert store.get(ref()) is value
    assert reveal_secret(store.get(ref())) == reveal_secret(value)
    assert code_of(lambda: store.put(ref(), val())) == "conflict"
    store.disable(ref())
    assert code_of(lambda: store.put(ref(), val())) == "conflict"
    assert code_of(lambda: store.get(ref(version=9))) == "not_found"


def test_reserved_conflicts_and_stores_nothing() -> None:
    store = FakeSecretStore()
    store.reserved.add(ref())
    assert code_of(lambda: store.put(ref(), val())) == "conflict"
    assert store.versions("acme", K1) == []
    assert store.put(ref(version=2), val()).ref.version == 2


def test_before_put_hook_receives_the_ref() -> None:
    store = FakeSecretStore()
    seen: list[SecretRef] = []
    store.before_put = seen.append
    store.put(ref(), val())
    assert seen == [ref()]


def test_disable_state_and_get_refusal() -> None:
    store = FakeSecretStore()
    value = val()
    store.put(ref(), value)
    meta = store.disable(ref())
    assert meta.state == "disabled"
    assert meta.ref == ref()
    assert store.versions("acme", K1)[0].state == "disabled"
    assert code_of(lambda: store.get(ref())) == "disabled"
    assert store.disable(ref()).state == "disabled"  # idempotent
    assert code_of(lambda: store.disable(ref(version=4))) == "not_found"
    assert store.stored_values() == {ref(): value}  # the same value stays until destroy


def test_destroy_is_idempotent_and_reput_follows_soft_delete() -> None:
    hard = FakeSecretStore()
    hard.put(ref(), val())
    hard.destroy(ref())
    hard.destroy(ref())
    hard.destroy(ref(version=5))  # absent: no error
    assert hard.versions("acme", K1) == []
    assert hard.put(ref(), val()).state == "enabled"  # default: name is free again

    soft = FakeSecretStore(soft_delete=True)
    soft.put(ref(), val())
    soft.destroy(ref())
    assert soft.versions("acme", K1) == []
    assert code_of(lambda: soft.get(ref())) == "not_found"
    assert code_of(lambda: soft.put(ref(), val())) == "conflict"
    soft.destroy(ref(version=6))  # never existed: not remembered as gone
    assert soft.put(ref(version=6), val()).ref.version == 6


def test_versions_filter_by_org_and_key_and_sort_by_version() -> None:
    store = FakeSecretStore()
    for v in (3, 1, 2):
        store.put(ref(version=v), val())
    store.put(ref(key=K2), val())
    store.put(ref(org="other"), val())
    assert [m.ref.version for m in store.versions("acme", K1)] == [1, 2, 3]
    assert [m.ref.key_id for m in store.versions("acme", K2)] == [K2]
    assert [m.ref.org_id for m in store.versions("other", K1)] == ["other"]
    assert store.versions("nobody", K1) == []


def test_list_versions_is_one_org_sorted_by_key_then_version() -> None:
    store = FakeSecretStore()
    store.put(ref(key=K2, version=1), val())
    store.put(ref(key=K1, version=2), val())
    store.put(ref(key=K1, version=1), val())
    store.put(ref(org="other"), val())
    assert [(m.ref.key_id, m.ref.version) for m in store.list_versions("acme")] == [
        (K1, 1),
        (K1, 2),
        (K2, 1),
    ]
    assert [m.ref.org_id for m in store.list_versions("other")] == ["other"]
    assert store.list_versions("nobody") == []


def test_health_and_call_counts() -> None:
    store = FakeSecretStore()
    assert store.health() is True
    store.put(ref(), val())
    store.get(ref())
    store.get(ref())
    store.versions("acme", K1)
    store.list_versions("acme")
    store.disable(ref())
    store.destroy(ref())
    assert dict(store.calls) == {
        "health": 1,
        "put": 1,
        "get": 2,
        "versions": 1,
        "list_versions": 1,
        "disable": 1,
        "destroy": 1,
    }


def test_fault_injection_per_method_and_wildcard() -> None:
    store = FakeSecretStore()
    store.fail["get"] = "denied"
    assert code_of(lambda: store.get(ref())) == "denied"
    assert store.health() is True  # only the named method fails
    assert store.calls["get"] == 1  # counted before the failure
    store.fail.clear()
    store.fail["*"] = "unavailable"
    for call in (
        lambda: store.health(),
        lambda: store.put(ref(), val()),
        lambda: store.destroy(ref()),
        lambda: store.versions("acme", K1),
        lambda: store.list_versions("acme"),
        lambda: store.disable(ref()),
    ):
        assert code_of(call) == "unavailable"
    store.fail["health"] = "denied"  # a method entry wins over the wildcard
    assert code_of(store.health) == "denied"
    assert store.stored_values() == {}


def test_transaction_probe_counts_calls_made_inside_one() -> None:
    store = FakeSecretStore()
    assert store.seen_in_transaction == 0
    store.health()
    assert store.seen_in_transaction == 0  # no probe set
    state = {"open": False}
    store.in_transaction = lambda: state["open"]
    store.health()
    assert store.seen_in_transaction == 0
    state["open"] = True
    store.health()
    store.health()
    assert store.seen_in_transaction == 2


def test_stored_values_is_a_copy_keyed_by_ref() -> None:
    store = FakeSecretStore()
    value = val()
    store.put(ref(), value)
    view = store.stored_values()
    assert view == {ref(): value}
    view_dict = dict(view)
    view_dict.clear()
    assert len(store.stored_values()) == 1


def test_one_character_org_id_is_allowed() -> None:
    assert SecretRef(org_id="a", key_id=K1, version=1).org_id == "a"


def test_models_are_frozen() -> None:
    r = ref()
    with pytest.raises(ValidationError):
        r.version = 2
    meta = SecretMeta(ref=r, created_at=datetime(2026, 1, 1, tzinfo=UTC), state="enabled")
    with pytest.raises(ValidationError):
        meta.state = "disabled"


def test_meta_accepts_both_states_and_nothing_else() -> None:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    for state in ("enabled", "disabled"):
        assert SecretMeta(ref=ref(), created_at=when, state=state).state == state
    with pytest.raises(ValidationError):
        SecretMeta(ref=ref(), created_at=when, state="destroyed")  # type: ignore[arg-type]
