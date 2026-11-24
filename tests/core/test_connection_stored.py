"""`auth stored` and `key_ref` on connection facts (R1.14c spec 4.3; VK29, VK30, VK31, VK69)."""

from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.model_catalog import ConnectionFacts, ConnectionRecord
from nuramesh.core.secret_store import SecretRef, org_tag

REF = "mk_" + "ab" * 10
ANTHROPIC: dict[str, Any] = {
    "provider": "anthropic",
    "endpoint": "https://api.anthropic.com",
    "status": "active",
}
OAC: dict[str, Any] = {
    "provider": "openai-compatible",
    "endpoint_id": "local",
    "endpoint": "https://models.example.com/v1",
    "status": "active",
}


def test_a_stored_connection_needs_a_key_ref_and_only_a_stored_one_may_have_it() -> None:
    ok = ConnectionFacts(**ANTHROPIC, auth="stored", key_ref=REF)
    assert ok.key_ref == REF
    assert ConnectionFacts(**OAC, auth="stored", key_ref=REF).auth == "stored"
    with pytest.raises(ValidationError):
        ConnectionFacts(**ANTHROPIC, auth="stored")
    for other in ("key_file", "entra"):
        with pytest.raises(ValidationError):  # VK29
            ConnectionFacts(**ANTHROPIC, auth=other, key_ref=REF)


def test_a_key_ref_has_exactly_the_server_generated_shape() -> None:
    for bad in ("sk-" + "a" * 40, "x" * 40, "mk_" + "AB" * 10, "mk_" + "ab" * 9, REF + "0", ""):
        with pytest.raises(ValidationError):  # VK31 (the pattern half)
            ConnectionFacts(**ANTHROPIC, auth="stored", key_ref=bad)


def test_stored_pairs_with_key_providers_only() -> None:
    azure: dict[str, Any] = {
        "provider": "azure-openai",
        "endpoint": "https://x.openai.azure.com",
        "status": "active",
    }
    with pytest.raises(ValidationError):  # VK30
        ConnectionFacts(**azure, auth="stored", key_ref=REF)


def test_the_facts_of_a_record_carry_the_ref_and_nothing_else_secret() -> None:
    facts = ConnectionFacts(**ANTHROPIC, auth="stored", key_ref=REF)
    record = ConnectionRecord(
        **facts.model_dump(), set_by="human:u_o", set_at=__import__("datetime").datetime.min, seq=1
    )
    assert record.facts["key_ref"] == REF
    assert record.facts["auth"] == "stored"
    # the model has exactly these fields: a new field of any kind must be a conscious decision
    assert set(ConnectionFacts.model_fields) == {
        "provider",
        "endpoint_id",
        "auth",
        "endpoint",
        "status",
        "key",
        "key_ref",
    }


def test_a_store_name_is_a_valid_vault_name_and_holds_no_org_id() -> None:  # VK69
    ref = SecretRef(org_id="acme", key_id=REF, version=3)
    import re

    assert re.fullmatch(r"nm-[0-9a-f]{16}-[0-9a-f]{20}-v[0-9]+", ref.name)
    assert len(ref.name) <= 127 and "_" not in ref.name and "acme" not in ref.name
    other = SecretRef(org_id="bravo", key_id=REF, version=3)
    assert org_tag("acme") != org_tag("bravo")
    assert ref.name != other.name
    with pytest.raises(ValidationError):
        SecretRef(org_id="acme", key_id="mk_zz", version=1)
    with pytest.raises(ValidationError):
        SecretRef(org_id="acme", key_id=REF, version=0)
