"""A model's identity is (provider, endpoint_id, model); the zero-price rule (the design notes
2.1, 12.2, 16a). Core only: no engine, no network."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.endpoints import (
    fingerprint,
    is_loopback,
    is_remote,
    normalise,
    residency_problem,
)
from nuramesh.core.gate_config import _same_model
from nuramesh.core.model_catalog import CatalogEntry, ModelCatalog
from nuramesh.core.models import (
    Message,
    ModelRequest,
    ModelResponse,
    format_model_ref,
    parse_model_ref,
)
from nuramesh.core.prices import Price, PriceBook, PriceOverride, UnknownPriceError, resolve_price

AT = datetime(2026, 7, 1, tzinfo=UTC)
LOCAL = "http://127.0.0.1:11434/v1"


def free(**kw: Any) -> Price:
    base: dict[str, Any] = {
        "provider": "openai-compatible",
        "endpoint_id": "local",
        "model": "m1",
        "input_per_million": Decimal(0),
        "output_per_million": Decimal(0),
        "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "source": "declared free (local)",
        "bound_endpoint": LOCAL,
    }
    return Price(**{**base, **kw})


def paid(**kw: Any) -> Price:
    base: dict[str, Any] = {
        "provider": "openai-compatible",
        "endpoint_id": "paid",
        "model": "m1",
        "input_per_million": Decimal(1),
        "output_per_million": Decimal(2),
        "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "source": "test price",
    }
    return Price(**{**base, **kw})


def test_model_refs_parse_into_the_identity_triple() -> None:
    assert parse_model_ref("openai-compatible/local/m1") == ("openai-compatible", "local", "m1")
    assert parse_model_ref("openai-compatible/local/org/m") == (
        "openai-compatible",
        "local",
        "org/m",
    )
    assert parse_model_ref("openai-compatible/local/llama3.1:8b").model == "llama3.1:8b"
    assert parse_model_ref("anthropic/org/m") == ("anthropic", None, "org/m")
    assert format_model_ref("openai-compatible", "local", "m1") == "openai-compatible/local/m1"
    for bad in (
        "openai-compatible/m1",
        "openai-compatible/Local/m1",
        "openai-compatible/A_b/m",
        f"openai-compatible/{'a' * 41}/m",
        "openai-compatible/local/",
        "@x/y",
        "openai-compatible@local/m",
    ):
        with pytest.raises(ValueError):
            parse_model_ref(bad)
    assert parse_model_ref(f"openai-compatible/{'a' * 40}/m").endpoint_id == "a" * 40


def test_an_endpoint_id_is_only_for_the_openai_compatible_provider() -> None:
    for provider in ("anthropic", "azure-openai", "fake", "unknown"):
        msg = Message(role="user", content="x")
        with pytest.raises(ValidationError, match="endpoint_id_not_allowed"):
            ModelRequest(
                provider=provider, endpoint_id="x", model="m", messages=(msg,), max_output_tokens=5
            )
        with pytest.raises(ValidationError, match="endpoint_id_not_allowed"):
            CatalogEntry(
                provider=provider, endpoint_id="x", model="m", family="f", processing="global"
            )
        with pytest.raises(ValidationError, match="endpoint_id_not_allowed"):
            Price(
                provider=provider,
                endpoint_id="x",
                model="m",
                input_per_million=Decimal(1),
                output_per_million=Decimal(1),
                effective_from=AT,
                source="test price",
            )
        with pytest.raises(ValidationError, match="endpoint_id_not_allowed"):
            PriceOverride(
                provider=provider,
                endpoint_id="x",
                model="m",
                input_per_million=Decimal(1),
                output_per_million=Decimal(1),
                set_by="human:a",
                set_at=AT,
                reason="because",
            )
    with pytest.raises(ValidationError):
        Price(
            provider="openai-compatible@local",
            model="m1",
            input_per_million=Decimal(1),
            output_per_million=Decimal(1),
            effective_from=AT,
            source="test price",
        )
    with pytest.raises(ValidationError):
        CatalogEntry(provider="openai-compatible", model="m", family="f", processing="global")


def test_the_same_model_name_on_two_endpoints_is_two_models() -> None:
    a = CatalogEntry(
        provider="openai-compatible", endpoint_id="a", model="m1", family="f", processing="global"
    )
    b = CatalogEntry(
        provider="openai-compatible", endpoint_id="b", model="m1", family="f", processing="global"
    )
    catalog = ModelCatalog(entries=(a, b))
    assert catalog.find("openai-compatible", "m1", "a") is a
    assert catalog.find("openai-compatible", "m1", "b") is b
    assert catalog.find("openai-compatible", "m1") is None
    assert a.key != b.key and a.ref == "openai-compatible/a/m1"
    # the "require a different model" gate: endpoints a and b are different models
    assert not _same_model("openai-compatible/a/m1", "openai-compatible/b/m1")
    assert _same_model("openai-compatible/a/M1", "openai-compatible/a/m1")
    assert _same_model("anthropic/x", "anthropic/X")


def test_prices_are_per_endpoint() -> None:
    book = PriceBook(prices=(free(), paid()))
    assert resolve_price(book, "openai-compatible", "m1", AT, endpoint_id="local").cost_basis
    assert (
        resolve_price(book, "openai-compatible", "m1", AT, endpoint_id="paid").input_per_million
        == 1
    )
    with pytest.raises(UnknownPriceError):
        resolve_price(book, "openai-compatible", "m1", AT, endpoint_id="other")
    with pytest.raises(UnknownPriceError):
        resolve_price(book, "openai-compatible", "m2", AT, endpoint_id="local")
    with pytest.raises(UnknownPriceError):
        resolve_price(book, "anthropic", "m1", AT)


def test_a_zero_price_must_be_declared_free_and_bound_to_loopback() -> None:
    assert free().bound_endpoint == LOCAL
    assert free(cached_input_per_million=Decimal(0)).output_per_million == 0
    for bad in (
        {"source": "test"},
        {"source": "Declared free (local)"},
        {"bound_endpoint": None},
    ):
        with pytest.raises(ValidationError, match="zero price must be declared free"):
            free(**bad)
    with pytest.raises(ValidationError, match="only allowed for an active local"):
        free(bound_endpoint="https://paid.example/v1")
    with pytest.raises(ValidationError, match="only a zero price has a bound endpoint"):
        paid(bound_endpoint=LOCAL)


def test_input_zero_with_a_nonzero_output_is_an_ordinary_price() -> None:
    p = paid(input_per_million=Decimal(0), source="whatever")
    assert p.bound_endpoint is None


def test_an_override_can_never_be_free() -> None:
    kw: dict[str, Any] = {
        "provider": "fake",
        "model": "m",
        "set_by": "human:a",
        "set_at": AT,
        "reason": "because",
    }
    with pytest.raises(ValidationError, match="override cannot be zero"):
        PriceOverride(input_per_million=Decimal(0), output_per_million=Decimal(0), **kw)
    assert PriceOverride(input_per_million=Decimal(0), output_per_million=Decimal(1), **kw)


def test_the_free_price_block_names_its_source_and_endpoint() -> None:
    payload = resolve_price(
        PriceBook(prices=(free(),)), "openai-compatible", "m1", AT, endpoint_id="local"
    ).ledger_payload()
    assert payload["source"] == "declared free (local)" and payload["bound_endpoint"] == LOCAL
    assert payload["endpoint_id"] == "local" and payload["basis"] == "known"


def test_loopback_normalisation_and_residency() -> None:
    assert is_loopback("http://127.0.0.1:1/v1") and is_loopback("https://[::1]:8/v1")
    assert not is_loopback("http://localhost:1/v1") and is_remote("https://api.example.com/v1")
    assert normalise("HTTPS://API.Example.com/V1/") == "https://api.example.com/V1"
    assert residency_problem("https://api.example.com/v1", "canada")
    assert residency_problem("https://api.example.com/v1", "global") is None
    assert residency_problem(LOCAL, "canada") is None


def test_the_fingerprint_is_the_documented_sha256() -> None:
    import hashlib
    import json

    expected = hashlib.sha256(
        json.dumps(
            {"auth": "none", "endpoint": LOCAL, "endpoint_id": "local", "key": None},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert fingerprint(LOCAL + "/", "none", None, "local") == expected and len(expected) == 64
    assert fingerprint(LOCAL, "none", None, "local") != fingerprint(
        LOCAL + "x", "none", None, "local"
    )
    assert fingerprint("https://a.example/V1", "key", "k", "x") != fingerprint(
        "https://a.example/v1", "key", "k", "x"
    )  # the path is case-sensitive


def test_a_response_must_say_its_usage_explicitly() -> None:
    with pytest.raises(ValidationError):
        ModelResponse(text="x")  # type: ignore[call-arg]
    assert ModelResponse(text="x", usage=None).usage is None
