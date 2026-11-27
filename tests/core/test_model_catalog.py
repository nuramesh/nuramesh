"""The approved model catalog and its rules (ND80 Q6, ND92; ND45, ND48, A3)."""

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from nuramesh.core.config import ConfigValidationError, parse_config
from nuramesh.core.ledger import EventType, LedgerEvent, next_event
from nuramesh.core.model_catalog import (
    ModelCatalog,
    fold_connections,
)
from nuramesh.core.residency import ResidencyError

AT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
WORKER = {"provider": "azure-openai", "model": "gpt-5.4-mini", "family": "openai"}
VERIFIER = {"provider": "azure-ai", "model": "Llama-4-Maverick", "family": "meta"}


def base() -> dict[str, Any]:
    """A small team: a developer (worker) checked by a reviewer (verifier), both with prices."""
    return {
        "version": 1,
        "org": {
            "id": "demo",
            "name": "Demo",
            "roles": [
                {"id": "cos", "name": "Chief", "kind": "chief_of_staff"},
                {"id": "dev", "name": "Dev", "default_model": "azure-openai/gpt-5.4-mini"},
                {
                    "id": "rev",
                    "name": "Rev",
                    "kind": "verifier",
                    "default_model": "azure-ai/Llama-4-Maverick",
                },
            ],
            "agents": [
                {"id": "chief", "name": "Chief", "role": "cos"},
                {"id": "developer", "name": "Developer", "role": "dev", "reports_to": "chief"},
                {
                    "id": "reviewer",
                    "name": "Reviewer",
                    "role": "rev",
                    "reports_to": "chief",
                    "checks": ["developer"],
                },
            ],
        },
        "prices": {
            "prices": [
                {
                    "provider": "azure-openai",
                    "model": "gpt-5.4-mini",
                    "input_per_million": "0.75",
                    "output_per_million": "4.50",
                    "effective_from": "2026-03-01T00:00:00Z",
                    "source": "test price list",
                },
                {
                    "provider": "azure-ai",
                    "model": "Llama-4-Maverick",
                    "input_per_million": "0.25",
                    "output_per_million": "1.00",
                    "effective_from": "2025-06-01T00:00:00Z",
                    "source": "test price list",
                },
            ]
        },
        "models": {
            "entries": [
                {**WORKER, "processing": "global"},
                {**VERIFIER, "processing": "global"},
            ]
        },
    }


def problems(data: dict[str, Any]) -> dict[str, str]:
    with pytest.raises(ConfigValidationError) as caught:
        parse_config(data)
    return {i.path: i.message for i in caught.value.issues}


def test_a_valid_catalog_parses_and_derives_data_classes_from_residency() -> None:
    catalog = parse_config(base()).models
    assert catalog is not None
    worker = catalog.find("azure-openai", "gpt-5.4-mini")
    assert worker is not None and worker.family == "openai" and worker.status == "approved"
    assert catalog.data_classes(worker) == ("synthetic",)  # global: never customer data (ND45)
    with pytest.raises(ResidencyError, match="outside Canada"):
        catalog.require_usable("azure-openai", "gpt-5.4-mini", "customer")
    catalog.require_usable("azure-openai", "gpt-5.4-mini", "synthetic")


@pytest.mark.parametrize(
    ("processing", "regions", "expected"),
    [
        ("global", ["canadaeast"], ("synthetic",)),
        ("canada", [], ("synthetic", "customer")),
        ("region:canadaeast", ["canadaeast"], ("synthetic", "customer")),
        ("region:eastus", ["canadaeast"], ("synthetic",)),
    ],
)
def test_data_classes_follow_the_processing_scope(
    processing: str, regions: list[str], expected: tuple[str, ...]
) -> None:
    catalog = ModelCatalog.model_validate(
        {"customer_regions": regions, "entries": [{**WORKER, "processing": processing}]}
    )
    assert catalog.data_classes(catalog.entries[0]) == expected


def test_an_unlisted_model_is_not_usable() -> None:
    catalog = parse_config(base()).models
    assert catalog is not None
    with pytest.raises(LookupError):
        catalog.require_usable("azure-openai", "gpt-unknown", "synthetic")


def test_the_catalog_is_optional_and_absence_restricts_nothing() -> None:
    data = base()
    del data["models"]
    data["org"]["roles"][1]["default_model"] = "anything/goes"
    assert parse_config(data).models is None


def test_a_bad_processing_scope_a_duplicate_and_a_bad_status_are_refused() -> None:
    data = base()
    data["models"]["entries"][0]["processing"] = "everywhere"
    assert any(p.startswith("$.models.entries[0]") for p in problems(data))
    data = base()
    data["models"]["entries"].append({**WORKER, "processing": "canada"})
    assert any("twice" in m or "duplicate" in m for m in problems(data).values())
    data = base()
    data["models"]["entries"][0]["status"] = "maybe"
    assert any(p.startswith("$.models.entries[0]") for p in problems(data))


def test_a_price_cannot_be_typed_into_the_catalog() -> None:
    data = base()
    data["models"]["entries"][0]["input_per_million"] = "0.01"
    assert any(p.startswith("$.models.entries[0]") for p in problems(data))


def test_an_approved_model_with_no_price_is_refused_a_paused_one_is_not() -> None:
    data = base()
    data["prices"]["prices"] = data["prices"]["prices"][1:]  # the worker has no price at all
    found = problems(data)
    assert "$.models.entries[0]" in found and "price" in found["$.models.entries[0]"]
    data["models"]["entries"][0]["status"] = "paused"
    data["org"]["roles"][1]["default_model"] = "azure-ai/Llama-4-Maverick"
    data["org"]["agents"][2]["checks"] = []
    data["org"]["roles"][2]["default_model"] = "azure-ai/Llama-4-Maverick"
    parse_config(data)


def test_a_price_override_counts_as_a_price() -> None:
    data = base()
    data["prices"]["prices"] = data["prices"]["prices"][1:]
    data["price_overrides"] = [
        {
            "provider": "azure-openai",
            "model": "gpt-5.4-mini",
            "input_per_million": "1",
            "output_per_million": "5",
            "set_by": "human:owner1",
            "set_at": "2026-10-01T00:00:00Z",
            "reason": "provisional until listed",
        }
    ]
    parse_config(data)


@pytest.mark.parametrize("status", ["paused", "retired"])
def test_an_assignment_must_be_an_approved_entry(status: str) -> None:
    data = base()
    data["models"]["entries"][0]["status"] = status
    found = problems(data)
    assert status in found["$.org.roles[1].default_model"]


def test_an_assignment_outside_the_catalog_is_refused_for_roles_and_agents() -> None:
    data = base()
    data["org"]["roles"][1]["default_model"] = "azure-openai/other"
    data["org"]["agents"][1]["model"] = "azure-openai/gpt-5.4-mini"  # fine
    data["org"]["agents"][0]["model"] = "azure-openai/unlisted"
    found = problems(data)
    assert "not in the approved catalog" in found["$.org.roles[1].default_model"]
    assert "$.org.agents[0].model" in found
    assert "$.org.agents[1].model" not in found


def test_a_malformed_model_reference_is_a_plain_problem() -> None:
    data = base()
    data["org"]["roles"][1]["default_model"] = "no-slash"
    assert "$.org.roles[1].default_model" in problems(data)


def test_a_verifier_must_be_a_different_family_than_the_worker_it_checks() -> None:
    data = base()
    data["models"]["entries"][1]["family"] = "openai"  # same family, different model
    found = problems(data)
    # the verifier's model comes from its role's default, so the path points at the role
    assert "$.org.agents[2].model" not in found
    message = found["$.org.roles[2].default_model"]
    assert "family" in message and "developer" in message


def test_the_family_rule_uses_the_agents_own_model_override() -> None:
    data = base()
    data["org"]["agents"][1]["model"] = "azure-ai/Llama-4-Maverick"  # the worker now meta too
    assert "$.org.roles[2].default_model" in problems(data)  # the verifier's model is the role's
    data = base()
    data["org"]["agents"][2]["model"] = "azure-openai/gpt-5.4-mini"  # the verifier now openai
    assert "$.org.agents[2].model" in problems(data)


def test_a_family_is_a_lowercase_slug() -> None:
    data = base()
    data["models"]["entries"][1]["family"] = "OpenAI"
    assert any(p.startswith("$.models.entries[1]") for p in problems(data))


def test_two_checked_agents_each_count() -> None:
    data = copy.deepcopy(base())
    data["org"]["agents"].append(
        {"id": "devops", "name": "DevOps", "role": "dev", "reports_to": "chief"}
    )
    data["org"]["agents"][2]["checks"] = ["developer", "devops"]
    data["org"]["agents"][3]["model"] = "azure-ai/Llama-4-Maverick"  # devops is meta, verifier meta
    found = problems(data)
    assert "devops" in found["$.org.roles[2].default_model"]


def test_core_names_no_cloud_vendor() -> None:
    """Cloud-neutral rule: the Azure endpoint pattern and provider ids live in the engine."""
    from pathlib import Path

    from nuramesh.core import model_catalog

    assert "azure" not in Path(model_catalog.__file__).read_text().lower()


def test_core_connection_facts_are_provider_neutral_and_https_only() -> None:
    from pydantic import ValidationError

    from nuramesh.core.model_catalog import ConnectionFacts

    ok = ConnectionFacts(
        provider="some-provider", auth="entra", status="active", endpoint="https://x.example.com"
    )
    assert ok.auth == "entra"
    for bad in (
        "http://x.example.com",
        "https://user:pw@x.example.com",
        "https://x.example.com/path?key=abc",
        "ftp://x.example.com",
    ):
        with pytest.raises(ValidationError):
            ConnectionFacts(provider="some-provider", auth="entra", status="active", endpoint=bad)
    for missing in ({"auth": "entra"}, {"status": "active"}):  # both are required (spec section 3)
        with pytest.raises(ValidationError):
            ConnectionFacts.model_validate(
                {"provider": "p", "endpoint": "https://x.example.com", **missing}
            )


def event(
    previous: LedgerEvent | None, kind: EventType, payload: dict[str, Any], actor: str = "human:u_1"
) -> LedgerEvent:
    return next_event(previous, stream="org:demo", type=kind, actor=actor, at=AT, payload=payload)


def test_connections_are_the_fold_of_their_events_and_junk_is_skipped() -> None:
    ep = "https://a.openai.azure.com"
    e1 = event(
        None,
        EventType.MODEL_CONNECTION_CREATED,
        {"provider": "azure-openai", "auth": "entra", "endpoint": ep, "status": "active"},
    )
    e2 = event(e1, EventType.AUTH_LOGIN_FAILED, {"anything": 1})
    e3 = event(
        e2,
        EventType.MODEL_CONNECTION_UPDATED,
        {"provider": "azure-openai", "auth": "entra", "endpoint": ep, "status": "disabled"},
        actor="human:u_2",
    )
    e4 = event(e3, EventType.MODEL_CONNECTION_CREATED, {"provider": "azure-ai"})  # malformed
    state = fold_connections([e1, e2, e3, e4])
    assert [r.provider for r in state.records()] == ["azure-openai"]
    assert state.get("azure-ai").invalid_seq == e4.seq  # a malformed write is invalid, not ignored
    got = state.get("azure-openai").record
    assert (
        got is not None
        and got.status == "disabled"
        and got.set_by == "human:u_2"
        and got.endpoint == ep
    )
    e5 = event(e4, EventType.MODEL_CONNECTION_REMOVED, {"provider": "azure-openai"})
    assert fold_connections([e1, e2, e3, e4, e5]).records() == []


def test_the_fold_has_no_accept_callback_every_well_formed_entry_is_state() -> None:
    ep = "https://a.example.com"
    first = event(
        None,
        EventType.MODEL_CONNECTION_CREATED,
        {"provider": "needs-key", "auth": "entra", "endpoint": ep, "status": "active"},
    )
    state = fold_connections([first])
    assert [r.provider for r in state.records()] == ["needs-key"]  # usability is the resolver's
    with pytest.raises(TypeError):
        fold_connections([first], accept=lambda facts: False)  # type: ignore[call-arg]
