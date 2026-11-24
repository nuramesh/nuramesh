"""R1.33 PR 2, spec 2.2.3: one pure `is_repoint` rule, shared by the service and the history read.

The table is the service's rule for the state BEFORE an entry: Azure and Anthropic repoint on a
changed endpoint; `openai-compatible` also on a changed `auth` or key name, and on a create after
a removal. The first entry of a connection and a removal never repoint, and a removal does not
reset the last well-formed endpoint.
"""

from datetime import UTC, datetime
from typing import Any

from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_catalog import (
    ConnectionsState,
    ProviderConnection,
    apply_connection_event,
    is_repoint,
)

AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
EP_A = "https://acme-east.openai.azure.com"
EP_B = "https://acme-west.cognitiveservices.azure.com"
LLM = "https://llm.acme.example/v1"


def ev(n: int, kind: str, payload: dict[str, Any]) -> LedgerEvent:
    return LedgerEvent.model_construct(
        stream="org:demo", seq=n, type=kind, actor="human:u1", at=AT, run_id=None, task_id=None,
        payload=payload, cost=None, cost_basis=None, prev_hash="0" * 64, hash="1" * 64,
    )  # fmt: skip


def azure(endpoint: str, status: str = "active") -> dict[str, Any]:
    return {"provider": "azure-openai", "auth": "entra", "endpoint": endpoint, "status": status}


def oac(auth: str = "none", key: str | None = None, endpoint: str = LLM) -> dict[str, Any]:
    out: dict[str, Any] = {
        "provider": "openai-compatible", "endpoint_id": "local", "auth": auth,
        "endpoint": endpoint, "status": "active",
    }  # fmt: skip
    if key is not None:
        out["key"] = key
    return out


def fold(*events: LedgerEvent) -> ProviderConnection:
    state = ConnectionsState()
    for event in events:
        apply_connection_event(state, event)
    return next(iter(state.providers.values()), ProviderConnection())


CREATED, UPDATED, REMOVED = (
    "model.connection_created",
    "model.connection_updated",
    "model.connection_removed",
)


def test_the_first_entry_of_a_connection_is_never_a_repoint() -> None:
    assert is_repoint("azure-openai", ProviderConnection(), EP_A, "entra", None) is False
    assert is_repoint("openai-compatible", ProviderConnection(), LLM, "none", None) is False


def test_azure_repoints_on_a_changed_endpoint_only() -> None:
    before = fold(ev(1, CREATED, azure(EP_A)))
    assert is_repoint("azure-openai", before, EP_B, "entra", None) is True
    assert (
        is_repoint("azure-openai", before, EP_A.upper().replace("HTTPS", "https"), "entra", None)
        is False
    )
    assert is_repoint("azure-openai", before, EP_A + "/", "entra", None) is False


def test_a_removal_does_not_reset_the_last_endpoint() -> None:
    gone = fold(ev(1, CREATED, azure(EP_B)), ev(2, REMOVED, {"provider": "azure-openai"}))
    assert is_repoint("azure-openai", gone, EP_A, "entra", None) is True
    assert is_repoint("azure-openai", gone, EP_B, "entra", None) is False


def test_openai_compatible_repoints_on_endpoint_auth_key_or_a_create_after_removal() -> None:
    plain = fold(ev(1, CREATED, oac()))
    assert is_repoint("openai-compatible", plain, LLM, "none", None) is False
    assert is_repoint("openai-compatible", plain, LLM, "key", "kn") is True
    assert is_repoint("openai-compatible", plain, "https://other.example/v1", "none", None) is True
    keyed = fold(ev(1, CREATED, oac("key", "kn")))
    assert is_repoint("openai-compatible", keyed, LLM, "key", "kn") is False
    assert is_repoint("openai-compatible", keyed, LLM, "key", "other") is True
    gone = fold(
        ev(1, CREATED, oac()),
        ev(2, REMOVED, {"provider": "openai-compatible", "endpoint_id": "local"}),
    )
    assert is_repoint("openai-compatible", gone, LLM, "none", None) is True


def test_a_connection_whose_last_write_was_invalid_still_remembers_its_last_good_endpoint() -> None:
    broken = {"provider": "azure-openai", "auth": "api_key", "endpoint": EP_B, "status": "active"}
    state = fold(ev(1, CREATED, azure(EP_A)), ev(2, UPDATED, broken))
    assert is_repoint("azure-openai", state, EP_B, "entra", None) is True
