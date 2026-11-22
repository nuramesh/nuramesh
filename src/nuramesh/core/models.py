"""Model requests, responses and the provider interface (PLAN I.3).

A **provider** is anything with `complete(request) -> ModelResponse`: a real model API behind an
adapter, or the `FakeModel` here (tests never call real model APIs). Providers report the tokens
they used; the engine prices them (`core.prices`) and charges budgets (`core.budgets`).

Prompts and outputs are content (A7, S2): the ledger gets their **hashes**, never the text.
`ModelRequest.content_hash` is the hash of the canonical messages.
"""

import math
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from nuramesh.core.canonical import canonical_json
from nuramesh.core.org import OPENAI_COMPATIBLE, EndpointId, Slug
from nuramesh.core.prices import MAX_TOKENS, ModelName, TokenUsage

ENDPOINT_NOT_ALLOWED = "endpoint_id_not_allowed"
"""The fixed code for an endpoint id on a provider other than `openai-compatible`."""

MAX_MESSAGE_CHARS = 2_000_000
MESSAGE_OVERHEAD_BYTES = 16
"""Framing a provider adds per message (role markers); part of the prompt-size upper bound."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Message(_Frozen):
    """One message of a prompt."""

    role: Literal["system", "user", "assistant"]
    content: Annotated[str, StringConstraints(max_length=MAX_MESSAGE_CHARS)]


class ModelRequest(_Frozen):
    """A call to make: which model, the prompt, and the most output the caller will pay for."""

    provider: Slug
    endpoint_id: EndpointId | None = None
    model: ModelName
    messages: tuple[Message, ...] = Field(min_length=1, max_length=1_000)
    max_output_tokens: int = Field(ge=1, le=MAX_TOKENS, strict=True)
    cache_prefix: bool = False
    """Mark the end of the leading system run as the cacheable prefix (R1.34 Part C). A field of the
    request, not of a message: `prompt_bytes` and the keyed prompt hash do not depend on it. It
    lives in process and is never persisted; off unless a caller sets it."""

    @model_validator(mode="after")
    def _endpoint_only_where_allowed(self) -> "ModelRequest":
        if self.endpoint_id is not None and self.provider != OPENAI_COMPATIBLE:
            raise ValueError(ENDPOINT_NOT_ALLOWED)
        return self

    @property
    def model_ref(self) -> str:
        """The reference text of the requested model (`provider/model`, or with its endpoint id)."""
        return format_model_ref(self.provider, self.endpoint_id, self.model)

    @property
    def prompt_token_bound(self) -> int:
        """An upper bound on the prompt's tokens, for budget reservations before the call.

        No tokenizer produces more tokens than UTF-8 bytes, so the byte count (plus a fixed
        framing allowance per message) can't be exceeded by the provider's own count. It is
        deliberately generous: a reservation holds more than the call will cost, and the rest is
        released when the call is settled.
        """
        raw = sum(len(m.content.encode("utf-8")) + MESSAGE_OVERHEAD_BYTES for m in self.messages)
        return min(raw, MAX_TOKENS)

    @property
    def cache_breakpoint(self) -> int | None:
        """The index of the last message of the leading system run, or None when the first message
        is not a system message (spec/token-savings 5.1)."""
        last: int | None = None
        for i, m in enumerate(self.messages):
            if m.role != "system":
                break
            last = i
        return last

    @property
    def prefix_bytes(self) -> bytes | None:
        """The canonical bytes of messages 0 to the breakpoint (what the keyed prefix hash covers),
        or None with no breakpoint."""
        b = self.cache_breakpoint
        if b is None:
            return None
        return canonical_json([m.model_dump(mode="json") for m in self.messages[: b + 1]])

    @property
    def prefix_text(self) -> str:
        """The joined text of the leading system run (what an adapter sends as `system`)."""
        b = self.cache_breakpoint
        if b is None:
            return ""
        return "\n\n".join(m.content for m in self.messages[: b + 1] if m.content)

    @property
    def prompt_bytes(self) -> bytes:
        """The canonical messages: what the keyed prompt hash covers (PH-KEY R3). The ledger holds
        only that keyed hash, never the text."""
        return canonical_json([m.model_dump(mode="json") for m in self.messages])


class ModelCalledPayload(BaseModel):
    """The fields of a `model.called` ledger payload that readers (the cost views) depend on.

    Other fields (usage, hashes, latency) are tolerated and ignored. `role` is optional and
    additive: entries written before it existed have none (spec/costs section 1). It is a slug,
    never free text.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    agent: str
    role: Slug | None = None
    provider: str
    endpoint_id: str | None = None
    model: str
    outcome: str
    price: Mapping[str, object] | None = None

    @property
    def model_ref(self) -> str:
        """`provider/model`, or with the endpoint id: the model's identity in a record."""
        return format_model_ref(self.provider, self.endpoint_id, self.model)

    @model_validator(mode="after")
    def _price_time_is_a_time(self) -> "ModelCalledPayload":
        self.price_version_at  # noqa: B018 (parse once so a malformed time is a ValidationError)
        return self

    @property
    def price_version_at(self) -> datetime | None:
        """When the price version used took effect (a listed price's `effective_from`, an
        override's `override_set_at`); None if neither is recorded (`price.at` is the call time,
        never a version).
        """
        if self.price is None:
            return None
        for key in ("effective_from", "override_set_at"):
            value = self.price.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"price.{key} must be a time")
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                raise ValueError(f"price.{key} needs a UTC offset")
            return moment
        return None


FinishReason = Literal["stop", "length", "filtered"]

CacheSupport = Literal["explicit", "automatic", "none"]
"""What a provider declares about prompt caching (R1.34 5.3): it needs a marker, it caches a stable
prefix unasked, or it does not report cache hits. Declared, never inferred from a name."""


def cache_support_of(provider: object) -> CacheSupport:
    """A plain provider's declared `cache_support`; anything else (or nothing) is `none`."""
    declared = getattr(provider, "cache_support", "none")
    if declared == "explicit":
        return "explicit"
    return "automatic" if declared == "automatic" else "none"


"""`filtered`: the provider billed tokens but gave no usable answer (a content filter, a tool call
we never asked for). The text is empty; the tokens are charged, never hidden."""


class ModelResponse(_Frozen):
    """What a provider returns: the text and the tokens it says it used."""

    text: Annotated[str, StringConstraints(max_length=MAX_MESSAGE_CHARS)]
    usage: TokenUsage | None
    """None: the service answered but its usage was missing or invalid. The cost is unknown and
    the reservation is charged as the upper bound (spec/openai-compatible 7.4), never as zero."""
    finish_reason: FinishReason = "stop"

    @property
    def response_bytes(self) -> bytes:
        """The text exactly as given (no normalisation; a lone surrogate doesn't fail): what the
        keyed response hash covers (PH-KEY R3)."""
        return self.text.encode("utf-8", "surrogatepass")


class ProviderError(Exception):
    """A provider couldn't answer (timeout, rate limit, outage). The call is not charged."""


class ModelProvider(Protocol):
    """The one method every provider adapter implements."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Answer `request`; raise `ProviderError` (or any exception) if it can't."""
        ...


class ProviderRouter:
    """Sends each request to the provider named in it (`request.provider`, and for a provider with
    named endpoints `provider/endpoint_id`: two endpoints are never the same provider).

    A run can use more than one provider, for example a worker on `azure-openai/gpt-5.4-mini` and
    a verifier from another model family on `azure-ai/Llama-4-Maverick-...`. A request for a
    provider that was not given is a `ProviderError` (not charged), never a silent fallback.
    """

    def __init__(self, providers: Mapping[str, ModelProvider]) -> None:
        if not providers:
            raise ValueError("a router needs at least one provider")
        self._providers = dict(providers)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        """The provider ids this router answers for."""
        return tuple(sorted(self._providers))

    def complete(self, request: ModelRequest) -> ModelResponse:
        name = (
            request.provider
            if request.endpoint_id is None
            else f"{request.provider}/{request.endpoint_id}"
        )
        provider = self._providers.get(name)
        if provider is None:
            raise ProviderError(f"no provider is configured for '{request.provider}'")
        return provider.complete(request)

    def cache_support_for(self, request: ModelRequest) -> CacheSupport:
        """What the provider this request goes to declares (an unknown name is `none`)."""
        name = (
            request.provider
            if request.endpoint_id is None
            else f"{request.provider}/{request.endpoint_id}"
        )
        provider = self._providers.get(name)
        return "none" if provider is None else cache_support_of(provider)


class ModelRef(NamedTuple):
    """A parsed model setting. A model's identity is `(provider, endpoint_id, model)`."""

    provider: str
    endpoint_id: str | None
    model: str


def format_model_ref(provider: str, endpoint_id: str | None, model: str) -> str:
    """`provider/model`, or `openai-compatible/<endpoint id>/<model>` when there is an endpoint."""
    return f"{provider}/{model}" if endpoint_id is None else f"{provider}/{endpoint_id}/{model}"


def payload_model_ref(payload: Mapping[str, Any]) -> str:
    """The model text of a ledger payload, for views: `provider/model`, or with the endpoint id.
    A missing or malformed part shows as `?` (a view never fails on an old entry)."""
    provider, model = payload.get("provider", "?"), payload.get("model", "?")
    endpoint_id = payload.get("endpoint_id")
    if isinstance(endpoint_id, str) and endpoint_id:
        return f"{provider}/{endpoint_id}/{model}"
    return f"{provider}/{model}"


_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_ENDPOINT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


_OAC = "openai-compatible: "
U_ENDPOINT_NEEDED = (
    _OAC + "this provider needs an endpoint id (openai-compatible/<endpoint id>/<model>)"
)
U_ID = _OAC + "the endpoint id is not valid"
U_ENDPOINT_OTHER = "an endpoint id is only allowed for the openai-compatible provider"


def parse_model_ref(ref: str) -> ModelRef:
    """Split an agent's model setting into `ModelRef(provider, endpoint_id, model)` (`ValueError`
    otherwise).

    The provider is the lowercase id used by the price book (`anthropic`, `openai`, `fake`); the
    model is whatever that provider calls it (it may itself contain slashes). Only the
    `openai-compatible` provider has an endpoint id: `openai-compatible/<endpoint id>/<model>`;
    for every other provider no endpoint id is read from the text (`anthropic/org/m` is the model
    `org/m`).
    """
    provider, sep, rest = ref.partition("/")
    if not sep or not rest.strip() or not _PROVIDER_ID.fullmatch(provider):
        raise ValueError("a model is written provider/model, for example anthropic/claude-opus")
    if provider != OPENAI_COMPATIBLE:
        return ModelRef(provider, None, rest.strip())
    endpoint_id, sep, model = rest.partition("/")
    if not sep:
        raise ValueError(U_ENDPOINT_NEEDED)
    if not _ENDPOINT_ID.fullmatch(endpoint_id):
        raise ValueError(U_ID)
    if not model.strip():
        raise ValueError("a model is written provider/model, for example anthropic/claude-opus")
    return ModelRef(provider, endpoint_id, model.strip())


def estimate_tokens(text: str) -> int:
    """The fake model's token count: one token per 4 UTF-8 bytes, rounded up (never zero)."""
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


Reply = str | BaseException | type[BaseException] | Callable[[ModelRequest], str]


class FakeModel:
    """A deterministic provider for tests and reference runs. It never touches the network.

    Replies come from a script, one per call, in order: a string is the answer, an exception is
    raised, a function is called with the request. When the script is used up, `default` answers
    (or `ProviderError` if there is none). Token counts follow `estimate_tokens`; an answer longer
    than `max_output_tokens` is cut and finishes with `length`. Every request is kept in `calls`.
    """

    def __init__(
        self,
        script: Iterable[Reply] = (),
        *,
        default: Callable[[ModelRequest], str] | None = None,
        cached_input_tokens: int = 0,
        cache_mode: CacheSupport = "none",
        min_prefix_tokens: int = 1,
    ) -> None:
        if cached_input_tokens < 0:
            raise ValueError("cached_input_tokens can't be negative")
        if cached_input_tokens and cache_mode != "none":
            raise ValueError("cached_input_tokens is only valid with cache_mode='none'")
        if cache_mode not in ("none", "explicit", "automatic") or min_prefix_tokens < 1:
            raise ValueError("cache_mode is none, explicit or automatic; min_prefix_tokens >= 1")
        self.cache_support: CacheSupport = cache_mode
        self._min_prefix = min_prefix_tokens
        self._seen: set[str] = set()
        self._script = list(script)
        self._default = default
        self._cached = cached_input_tokens
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if self._script:
            reply = self._script.pop(0)
        elif self._default is not None:
            reply = self._default
        else:
            raise ProviderError("the fake model has no reply left")
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, type):
            raise reply()
        text = reply if isinstance(reply, str) else reply(request)
        finish: FinishReason = "stop"
        if estimate_tokens(text) > request.max_output_tokens:
            text = text.encode("utf-8")[: request.max_output_tokens * 4].decode("utf-8", "ignore")
            finish = "length"
        prompt = sum(estimate_tokens(m.content) for m in request.messages)
        cached, written = min(self._cached, prompt), 0
        if self.cache_support != "none":
            cached, written = self._simulate_cache(request, prompt)
        return ModelResponse(
            text=text,
            usage=TokenUsage(
                input_tokens=prompt - cached - written,
                output_tokens=estimate_tokens(text),
                cached_input_tokens=cached,
                cache_write_tokens=written,
            ),
            finish_reason=finish,
        )

    def _simulate_cache(self, request: ModelRequest, total: int) -> tuple[int, int]:
        """(cached, written) tokens under the declared mode (spec/token-savings 5.9). Remembering is
        by exact text with no expiry; a new instance models expiry."""
        b = request.cache_breakpoint
        if b is None:
            return 0, 0
        prefix = sum(estimate_tokens(m.content) for m in request.messages[: b + 1])
        text = request.prefix_text
        seen = text in self._seen
        if self.cache_support == "explicit":
            if not request.cache_prefix or prefix < self._min_prefix:
                return 0, 0
            self._seen.add(text)
            return (prefix, 0) if seen else (0, prefix)
        self._seen.add(text)  # automatic: first sight caches nothing, later sights hit
        return (prefix, 0) if seen else (0, 0)


class OutputCappedPayload(BaseModel):
    """The payload of a `model.output_capped` entry (R1.42, spec/r142 section 6). Strict: a field
    nobody declared is refused. No prompt or response text is ever in it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Literal["applied", "hit", "not_applied"]
    reservation: str
    agent: str
    role: str
    provider: str
    endpoint_id: str | None = None
    model: str
    requested_max_tokens: int
    effective_max_tokens: int
    role_cap: int | None
    task_cap: int | None
    budget_cap: int | None
    binding: Literal["task", "role", "budget"]
    limiting_budget_id: str | None
    available: Mapping[str, object] | str | None
    prompt_token_bound: int
    price: Mapping[str, object]
    output_tokens: int | None = None
    finish_reason: str | None = None
