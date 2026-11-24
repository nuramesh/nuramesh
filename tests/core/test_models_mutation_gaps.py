"""Mutation pins for `nuramesh.core.models` (issue #1267): validators and their refusal messages,
endpoint and provider fallbacks, token estimates, the fake model's cache simulation.
Pure core, no I/O."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.models import (
    ENDPOINT_NOT_ALLOWED,
    MAX_MESSAGE_CHARS,
    MESSAGE_OVERHEAD_BYTES,
    U_ENDPOINT_NEEDED,
    U_ENDPOINT_OTHER,
    U_ID,
    FakeModel,
    Message,
    ModelCalledPayload,
    ModelRef,
    ModelRequest,
    ModelResponse,
    OutputCappedPayload,
    ProviderError,
    ProviderRouter,
    cache_support_of,
    estimate_tokens,
    format_model_ref,
    parse_model_ref,
    payload_model_ref,
)
from nuramesh.core.prices import MAX_TOKENS, TokenUsage

OAC = "openai-compatible"


def msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


def req(
    *messages: Message,
    provider: str = "fake",
    endpoint_id: str | None = None,
    max_output_tokens: int = 100,
    cache_prefix: bool = False,
) -> ModelRequest:
    return ModelRequest(
        provider=provider,
        endpoint_id=endpoint_id,
        model="m",
        messages=messages or (msg("user", "hi"),),
        max_output_tokens=max_output_tokens,
        cache_prefix=cache_prefix,
    )


def refused(**kw: Any) -> str:
    with pytest.raises(ValidationError) as e:
        ModelRequest(**{"provider": "fake", "model": "m", "max_output_tokens": 5, **kw})
    return str(e.value)


# ---- ModelRequest validation -------------------------------------------------------------


def test_endpoint_id_only_on_openai_compatible() -> None:
    with pytest.raises(ValidationError, match=ENDPOINT_NOT_ALLOWED):
        req(provider="anthropic", endpoint_id="ep1")
    assert req(provider=OAC, endpoint_id="ep1").endpoint_id == "ep1"
    assert req(provider=OAC).endpoint_id is None
    assert req(provider="anthropic").endpoint_id is None
    assert ENDPOINT_NOT_ALLOWED == "endpoint_id_not_allowed"


def test_the_endpoint_id_default_is_none() -> None:
    r = ModelRequest(
        provider="fake",
        model="m",
        messages=(msg("user", "x"),),
        max_output_tokens=1,
    )
    assert r.endpoint_id is None and r.cache_prefix is False


def test_max_output_tokens_bounds_and_strictness() -> None:
    assert req(max_output_tokens=1).max_output_tokens == 1
    assert req(max_output_tokens=MAX_TOKENS).max_output_tokens == MAX_TOKENS
    assert "greater than or equal to 1" in refused(
        messages=(msg("user", "x"),), max_output_tokens=0
    )
    assert "less than or equal to" in refused(
        messages=(msg("user", "x"),), max_output_tokens=MAX_TOKENS + 1
    )
    assert refused(messages=(msg("user", "x"),), max_output_tokens=True)
    assert refused(messages=(msg("user", "x"),), max_output_tokens="5")


def test_message_count_bounds() -> None:
    assert len(req(*[msg("user", "x")] * 1_000).messages) == 1_000
    assert "at least 1" in refused(messages=())
    assert "at most 1000" in refused(messages=tuple([msg("user", "x")] * 1_001))


def test_message_length_and_role_bounds() -> None:
    ok = Message(role="user", content="a" * MAX_MESSAGE_CHARS)
    assert len(ok.content) == MAX_MESSAGE_CHARS
    with pytest.raises(ValidationError):
        Message(role="user", content="a" * (MAX_MESSAGE_CHARS + 1))
    with pytest.raises(ValidationError):
        Message(role="tool", content="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ModelResponse(text="a" * (MAX_MESSAGE_CHARS + 1), usage=None)
    assert MESSAGE_OVERHEAD_BYTES == 16 and MAX_MESSAGE_CHARS == 2_000_000


def test_models_are_frozen_and_refuse_extra_fields() -> None:
    m = msg("user", "x")
    with pytest.raises(ValidationError):
        m.content = "y"
    with pytest.raises(ValidationError):
        Message(role="user", content="x", extra="no")  # type: ignore[call-arg]


# ---- ModelRequest views ------------------------------------------------------------------


def test_model_ref_with_and_without_endpoint() -> None:
    assert req(provider="fake").model_ref == "fake/m"
    assert req(provider=OAC, endpoint_id="ep1").model_ref == "openai-compatible/ep1/m"


def test_prompt_token_bound_is_bytes_plus_framing_per_message() -> None:
    r = req(msg("system", "abc"), msg("user", "éé"), msg("assistant", ""))
    assert r.prompt_token_bound == (3 + 16) + (4 + 16) + (0 + 16)
    assert req(msg("user", "")).prompt_token_bound == 16


def test_cache_breakpoint_is_the_last_of_the_leading_system_run() -> None:
    assert req(msg("system", "a")).cache_breakpoint == 0
    assert req(msg("system", "a"), msg("system", "b"), msg("user", "c")).cache_breakpoint == 1
    assert req(msg("user", "c"), msg("system", "a")).cache_breakpoint is None
    assert req(msg("system", "a"), msg("user", "c"), msg("system", "b")).cache_breakpoint == 0


def test_prefix_bytes_and_text_cover_only_the_leading_system_run() -> None:
    r = req(msg("system", "a"), msg("system", ""), msg("system", "b"), msg("user", "c"))
    assert r.prefix_text == "a\n\nb"  # an empty system message adds no blank line
    want = [{"role": "system", "content": c} for c in ("a", "", "b")]
    assert r.prefix_bytes == json.dumps(want, separators=(",", ":"), sort_keys=True).encode()
    none = req(msg("user", "c"), msg("system", "a"))
    assert none.prefix_bytes is None
    assert none.prefix_text == ""
    assert req(msg("system", "only")).prefix_text == "only"


def test_prompt_bytes_cover_every_message() -> None:
    r = req(msg("system", "a"), msg("user", "b"))
    want = [{"role": "system", "content": "a"}, {"role": "user", "content": "b"}]
    assert r.prompt_bytes == json.dumps(want, separators=(",", ":"), sort_keys=True).encode()


# ---- ModelCalledPayload ------------------------------------------------------------------

BASE = {"agent": "a", "provider": "p", "model": "m", "outcome": "ok"}


def payload(**kw: Any) -> ModelCalledPayload:
    return ModelCalledPayload.model_validate({**BASE, **kw})


def test_payload_model_ref_and_extras_tolerated() -> None:
    assert payload().model_ref == "p/m"
    assert payload(endpoint_id="e1").model_ref == "p/e1/m"
    p = payload(role="worker", usage={"input_tokens": 1}, latency_ms=3)
    assert p.role == "worker" and p.model_extra == {"usage": {"input_tokens": 1}, "latency_ms": 3}
    with pytest.raises(ValidationError):
        payload(role="Not A Slug!")


def test_price_version_at_prefers_effective_from_then_override() -> None:
    both = payload(
        price={"effective_from": "2026-01-02T00:00:00Z", "override_set_at": "2026-05-05T00:00:00Z"}
    )
    assert both.price_version_at is not None and both.price_version_at.month == 1
    assert both.price_version_at.utcoffset() is not None
    only = payload(price={"override_set_at": "2026-05-05T10:00:00+00:00"})
    assert only.price_version_at is not None and only.price_version_at.month == 5
    skipped = payload(price={"effective_from": None, "override_set_at": "2026-05-05T10:00:00Z"})
    assert skipped.price_version_at is not None and skipped.price_version_at.month == 5
    assert payload(price={"at": "2026-05-05T10:00:00Z"}).price_version_at is None
    assert payload(price=None).price_version_at is None
    assert payload().price_version_at is None


def test_price_time_must_be_a_string_with_an_offset() -> None:
    with pytest.raises(ValidationError, match=r"price\.effective_from must be a time"):
        payload(price={"effective_from": 5})
    with pytest.raises(ValidationError, match=r"price\.override_set_at must be a time"):
        payload(price={"override_set_at": 5})
    with pytest.raises(ValidationError, match=r"price\.effective_from needs a UTC offset"):
        payload(price={"effective_from": "2026-01-02T00:00:00"})
    with pytest.raises(ValidationError, match=r"price\.override_set_at needs a UTC offset"):
        payload(price={"override_set_at": "2026-01-02T00:00:00"})
    with pytest.raises(ValidationError):
        payload(price={"effective_from": "not a time"})
    got = payload(price={"effective_from": "2026-01-02T03:04:05Z"}).price_version_at
    assert got is not None and got.hour == 3 and got.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


# ---- cache_support_of, response, router --------------------------------------------------


class _Declares:
    def __init__(self, value: object) -> None:
        self.cache_support = value


def test_cache_support_of_reads_only_the_declared_values() -> None:
    assert cache_support_of(_Declares("explicit")) == "explicit"
    assert cache_support_of(_Declares("automatic")) == "automatic"
    assert cache_support_of(_Declares("none")) == "none"
    assert cache_support_of(_Declares("Explicit")) == "none"
    assert cache_support_of(_Declares(None)) == "none"
    assert cache_support_of(object()) == "none"


def test_response_bytes_are_exact_and_finish_defaults_to_stop() -> None:
    r = ModelResponse.model_construct(
        text="h\u00e9\ud800", usage=None, finish_reason="stop"
    )  # lone surrogate
    assert r.response_bytes == b"h\xc3\xa9\xed\xa0\x80"
    assert ModelResponse(text="x", usage=None).finish_reason == "stop"
    assert ModelResponse(text="", usage=TokenUsage(), finish_reason="filtered").text == ""
    with pytest.raises(ValidationError):
        ModelResponse(text="x", usage=None, finish_reason="other")  # type: ignore[arg-type]


def test_router_refuses_an_empty_set_and_copies_its_input() -> None:
    with pytest.raises(ValueError, match="a router needs at least one provider"):
        ProviderRouter({})
    given = {"fake": FakeModel(default=lambda r: "ok")}
    router = ProviderRouter(given)
    given.clear()
    assert router.provider_ids == ("fake",)
    assert router.complete(req()).text == "ok"


def test_router_keys_an_endpoint_request_by_provider_and_endpoint() -> None:
    plain, ep = FakeModel(default=lambda r: "plain"), FakeModel(default=lambda r: "ep")
    router = ProviderRouter({OAC: plain, f"{OAC}/ep1": ep})
    assert router.complete(req(provider=OAC)).text == "plain"
    assert router.complete(req(provider=OAC, endpoint_id="ep1")).text == "ep"
    with pytest.raises(ProviderError, match="no provider is configured for 'openai-compatible'"):
        router.complete(req(provider=OAC, endpoint_id="other"))
    assert len(plain.calls) == 1 and len(ep.calls) == 1


def test_router_cache_support_follows_the_provider_it_routes_to() -> None:
    router = ProviderRouter(
        {
            "a": FakeModel(cache_mode="explicit"),
            OAC: FakeModel(cache_mode="none"),
            f"{OAC}/ep1": FakeModel(cache_mode="automatic"),
        }
    )
    assert router.cache_support_for(req(provider="a")) == "explicit"
    assert router.cache_support_for(req(provider=OAC)) == "none"
    assert router.cache_support_for(req(provider=OAC, endpoint_id="ep1")) == "automatic"
    assert router.cache_support_for(req(provider="unknown")) == "none"
    assert router.cache_support_for(req(provider=OAC, endpoint_id="nope")) == "none"


# ---- model refs --------------------------------------------------------------------------


def test_format_and_payload_model_refs() -> None:
    assert format_model_ref("p", None, "m") == "p/m"
    assert format_model_ref("p", "e", "m") == "p/e/m"
    assert payload_model_ref({"provider": "p", "model": "m"}) == "p/m"
    assert payload_model_ref({"provider": "p", "model": "m", "endpoint_id": "e"}) == "p/e/m"
    assert payload_model_ref({"provider": "p", "model": "m", "endpoint_id": ""}) == "p/m"
    assert payload_model_ref({"provider": "p", "model": "m", "endpoint_id": 7}) == "p/m"
    assert payload_model_ref({"provider": "p", "model": "m", "endpoint_id": None}) == "p/m"
    assert payload_model_ref({}) == "?/?"
    assert payload_model_ref({"endpoint_id": "e"}) == "?/e/?"


GENERIC = "a model is written provider/model, for example anthropic/claude-opus"


@pytest.mark.parametrize(
    "ref", ["", "noslash", "fake/", "fake/   ", "/m", "Fake/m", "-x/m", "a b/m"]
)
def test_a_malformed_ref_is_refused_with_the_example(ref: str) -> None:
    with pytest.raises(ValueError) as e:
        parse_model_ref(ref)
    assert str(e.value) == GENERIC


def test_provider_id_length_bounds() -> None:
    assert parse_model_ref("a" * 63 + "/m").provider == "a" * 63
    with pytest.raises(ValueError, match="provider/model"):
        parse_model_ref("a" * 64 + "/m")
    assert parse_model_ref("a_-9/m") == ModelRef("a_-9", None, "m")
    assert parse_model_ref("0/m").provider == "0"


def test_a_plain_provider_never_reads_an_endpoint_and_strips_the_model() -> None:
    assert parse_model_ref("anthropic/org/m") == ModelRef("anthropic", None, "org/m")
    assert parse_model_ref("fake/  m  ") == ModelRef("fake", None, "m")


def test_openai_compatible_refs() -> None:
    assert parse_model_ref("openai-compatible/ep1/m") == ModelRef(OAC, "ep1", "m")
    assert parse_model_ref("openai-compatible/ep1/ org/m ") == ModelRef(OAC, "ep1", "org/m")
    assert parse_model_ref(f"{OAC}/{'a' * 40}/m").endpoint_id == "a" * 40
    assert parse_model_ref(f"{OAC}/0-a/m").endpoint_id == "0-a"


def test_openai_compatible_refusals_have_their_own_messages() -> None:
    with pytest.raises(ValueError) as e:
        parse_model_ref("openai-compatible/ep1")
    assert str(e.value) == U_ENDPOINT_NEEDED
    for bad in ("A", "-a", "a_b", "a" * 41, ""):
        with pytest.raises(ValueError) as e2:
            parse_model_ref(f"{OAC}/{bad}/m" if bad else f"{OAC}//m")
        assert str(e2.value) == U_ID
    for model in ("", "   "):
        with pytest.raises(ValueError) as e3:
            parse_model_ref(f"{OAC}/ep1/{model}")
        assert str(e3.value) == GENERIC


def test_refusal_texts() -> None:
    assert U_ENDPOINT_NEEDED == (
        "openai-compatible: this provider needs an endpoint id "
        "(openai-compatible/<endpoint id>/<model>)"
    )
    assert U_ID == "openai-compatible: the endpoint id is not valid"
    assert U_ENDPOINT_OTHER == "an endpoint id is only allowed for the openai-compatible provider"


# ---- estimate_tokens and the fake model --------------------------------------------------


def test_estimate_tokens_rounds_up_and_is_never_zero() -> None:
    assert [estimate_tokens(t) for t in ("", "a", "abcd", "abcde", "a" * 8, "a" * 9)] == [
        1,
        1,
        1,
        2,
        2,
        3,
    ]
    assert estimate_tokens("ééé") == 2  # 6 bytes


def test_fake_model_script_order_exceptions_and_default() -> None:
    boom = ProviderError("down")
    fm = FakeModel(
        ["one", boom, ValueError, lambda r: f"echo:{r.messages[-1].content}"],
        default=lambda r: "dflt",
    )
    assert fm.complete(req()).text == "one"
    with pytest.raises(ProviderError) as e:
        fm.complete(req())
    assert e.value is boom
    with pytest.raises(ValueError):
        fm.complete(req())
    assert fm.complete(req(msg("user", "zz"))).text == "echo:zz"
    assert fm.complete(req()).text == "dflt"
    assert fm.complete(req()).text == "dflt"
    assert len(fm.calls) == 6
    with pytest.raises(ProviderError, match="the fake model has no reply left"):
        FakeModel().complete(req())
    empty = FakeModel(["x"])
    empty.complete(req())
    with pytest.raises(ProviderError, match="no reply left"):
        empty.complete(req())


def test_fake_model_usage_and_truncation() -> None:
    fm = FakeModel([])
    fm2 = FakeModel(["a" * 8, "a" * 9, "é" * 20])
    r = fm2.complete(req(msg("user", "x" * 12), max_output_tokens=2))
    assert (r.text, r.finish_reason) == ("a" * 8, "stop")
    assert r.usage == TokenUsage(input_tokens=3, output_tokens=2)
    r = fm2.complete(req(max_output_tokens=2))
    assert (r.text, r.finish_reason) == ("a" * 8, "length")
    assert r.usage is not None and r.usage.output_tokens == 2
    r = fm2.complete(req(max_output_tokens=3))  # 40 bytes cut at 12 bytes, whole characters only
    assert (r.text, r.finish_reason) == ("é" * 6, "length")
    assert fm.calls == []


def test_fake_model_prompt_tokens_are_per_message() -> None:
    r = FakeModel(["ok"]).complete(req(msg("system", "abcde"), msg("user", "abcde")))
    assert r.usage == TokenUsage(input_tokens=4, output_tokens=1)


def test_fake_model_fixed_cached_tokens_are_capped_by_the_prompt() -> None:
    r = FakeModel(["ok"], cached_input_tokens=5).complete(req(msg("user", "a" * 12)))
    assert r.usage == TokenUsage(input_tokens=0, output_tokens=1, cached_input_tokens=3)
    r = FakeModel(["ok"], cached_input_tokens=2).complete(req(msg("user", "a" * 12)))
    assert r.usage == TokenUsage(input_tokens=1, output_tokens=1, cached_input_tokens=2)


def test_fake_model_constructor_refusals() -> None:
    with pytest.raises(ValueError, match="cached_input_tokens can't be negative"):
        FakeModel(cached_input_tokens=-1)
    with pytest.raises(ValueError, match="only valid with cache_mode='none'"):
        FakeModel(cached_input_tokens=1, cache_mode="explicit")
    FakeModel(cached_input_tokens=1, cache_mode="none")
    FakeModel(cached_input_tokens=0, cache_mode="explicit")
    with pytest.raises(ValueError, match="cache_mode is none, explicit or automatic"):
        FakeModel(cache_mode="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_prefix_tokens >= 1"):
        FakeModel(min_prefix_tokens=0)
    assert FakeModel(min_prefix_tokens=1).cache_support == "none"
    for mode in ("none", "explicit", "automatic"):
        assert FakeModel(cache_mode=mode).cache_support == mode


def test_explicit_mode_writes_then_reads_only_when_marked() -> None:
    fm = FakeModel(default=lambda r: "ok", cache_mode="explicit")
    sys_text = "s" * 20  # 5 tokens
    a = req(msg("system", sys_text), msg("user", "u" * 8), cache_prefix=True)
    first = fm.complete(a).usage
    assert first == TokenUsage(input_tokens=2, output_tokens=1, cache_write_tokens=5)
    second = fm.complete(a).usage
    assert second == TokenUsage(input_tokens=2, output_tokens=1, cached_input_tokens=5)
    unmarked = req(msg("system", "t" * 20), msg("user", "u" * 8))
    assert fm.complete(unmarked).usage == TokenUsage(input_tokens=7, output_tokens=1)
    assert fm.complete(unmarked).usage == TokenUsage(input_tokens=7, output_tokens=1)
    no_system = req(msg("user", "u" * 8), cache_prefix=True)
    assert fm.complete(no_system).usage == TokenUsage(input_tokens=2, output_tokens=1)


def test_explicit_mode_needs_the_minimum_prefix_size() -> None:
    fm = FakeModel(default=lambda r: "ok", cache_mode="explicit", min_prefix_tokens=5)
    exactly = req(msg("system", "s" * 20), msg("user", "u"), cache_prefix=True)  # prefix 5 tokens
    assert fm.complete(exactly).usage == TokenUsage(
        input_tokens=1, output_tokens=1, cache_write_tokens=5
    )
    short = req(msg("system", "s" * 19), msg("user", "u"), cache_prefix=True)  # 5 tokens (ceil)
    assert fm.complete(short).usage is not None
    tiny = req(msg("system", "s" * 16), msg("user", "u"), cache_prefix=True)  # 4 tokens
    got = fm.complete(tiny).usage
    assert got == TokenUsage(input_tokens=5, output_tokens=1)
    assert fm.complete(tiny).usage == got  # below the minimum nothing is remembered either


def test_automatic_mode_first_sight_caches_nothing_then_hits() -> None:
    fm = FakeModel(default=lambda r: "ok", cache_mode="automatic")
    a = req(msg("system", "s" * 20), msg("user", "u" * 8))  # no cache_prefix needed
    assert fm.complete(a).usage == TokenUsage(input_tokens=7, output_tokens=1)
    assert fm.complete(a).usage == TokenUsage(
        input_tokens=2, output_tokens=1, cached_input_tokens=5
    )
    other = req(msg("system", "t" * 20), msg("user", "u" * 8))
    assert fm.complete(other).usage == TokenUsage(input_tokens=7, output_tokens=1)
    assert fm.complete(req(msg("user", "u" * 8))).usage == TokenUsage(
        input_tokens=2, output_tokens=1
    )


def test_a_new_instance_forgets_what_was_seen() -> None:
    a = req(msg("system", "s" * 20), msg("user", "u" * 8), cache_prefix=True)
    one = FakeModel(default=lambda r: "ok", cache_mode="explicit")
    one.complete(a)
    assert one.complete(a).usage is not None and one.complete(a).usage.cached_input_tokens == 5  # type: ignore[union-attr]
    fresh = FakeModel(default=lambda r: "ok", cache_mode="explicit")
    assert fresh.complete(a).usage.cache_write_tokens == 5  # type: ignore[union-attr]


def test_explicit_prefix_counts_every_message_up_to_the_breakpoint_only() -> None:
    fm = FakeModel(default=lambda r: "ok", cache_mode="explicit")
    three = req(
        msg("system", "s" * 4), msg("user", "u" * 40), msg("user", "v" * 40), cache_prefix=True
    )
    assert fm.complete(three).usage == TokenUsage(
        input_tokens=20, output_tokens=1, cache_write_tokens=1
    )
    two = req(
        msg("system", "s" * 4), msg("system", "t" * 8), msg("user", "u" * 40), cache_prefix=True
    )
    assert fm.complete(two).usage == TokenUsage(
        input_tokens=10, output_tokens=1, cache_write_tokens=3
    )


def test_the_default_minimum_prefix_is_one_token() -> None:
    fm = FakeModel(default=lambda r: "ok", cache_mode="explicit")
    one_token = req(msg("system", "s"), msg("user", "u"), cache_prefix=True)
    assert fm.complete(one_token).usage == TokenUsage(
        input_tokens=1, output_tokens=1, cache_write_tokens=1
    )


def test_payload_models_are_frozen() -> None:
    called = payload()
    with pytest.raises(ValidationError):
        called.agent = "other"
    capped = OutputCappedPayload(
        stage="applied",
        reservation="r",
        agent="a",
        role="w",
        provider="p",
        model="m",
        requested_max_tokens=5,
        effective_max_tokens=5,
        role_cap=None,
        task_cap=None,
        budget_cap=None,
        binding="task",
        limiting_budget_id=None,
        available=None,
        prompt_token_bound=3,
        price={},
    )
    assert capped.endpoint_id is None and capped.output_tokens is None
    with pytest.raises(ValidationError):
        capped.agent = "other"
    with pytest.raises(ValidationError):
        OutputCappedPayload(**{**capped.model_dump(), "surprise": 1})
