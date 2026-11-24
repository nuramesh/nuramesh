"""The provider router: each request goes to the provider it names (ND48)."""

import pytest

from nuramesh.core.models import (
    FakeModel,
    Message,
    ModelRequest,
    ProviderError,
    ProviderRouter,
)


def request(provider: str, model: str = "m") -> ModelRequest:
    return ModelRequest(
        provider=provider,
        model=model,
        messages=(Message(role="user", content="hi"),),
        max_output_tokens=10,
    )


def test_each_request_goes_to_its_own_provider() -> None:
    worker, verifier = FakeModel(default=lambda r: "W"), FakeModel(default=lambda r: "V")
    router = ProviderRouter({"azure-openai": worker, "azure-ai": verifier})
    assert router.complete(request("azure-openai")).text == "W"
    assert router.complete(request("azure-ai", "llama")).text == "V"
    assert [c.provider for c in worker.calls] == ["azure-openai"]
    assert [c.model for c in verifier.calls] == ["llama"]
    assert router.provider_ids == ("azure-ai", "azure-openai")


def test_an_unknown_provider_is_a_provider_error_never_a_fallback() -> None:
    worker = FakeModel(default=lambda r: "W")
    router = ProviderRouter({"azure-openai": worker})
    with pytest.raises(ProviderError, match="no provider is configured for 'azure-ai'"):
        router.complete(request("azure-ai"))
    assert worker.calls == []  # nothing was sent to the wrong provider


def test_a_router_needs_a_provider() -> None:
    with pytest.raises(ValueError):
        ProviderRouter({})
