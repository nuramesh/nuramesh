"""Prompt caching: the per-organization opt-in and the status of a marked call (R1.34, ND312 Q15).

Provider-side prompt caching (Anthropic `cache_control`) leaves the system text of a call with the
provider for about 5 minutes after its last use (each use restarts the clock). That is a data
retention position, so it is **off for every organization until the owner of the organization turns
it on**. Turning it on or off is one `decision.recorded` entry on the organization stream
(`kind: "prompt_cache"`), so the audit trail names who and when. With it off, no marker is ever sent
to a provider and a marked request is recorded as `stripped_not_enabled`.

`decide_cache` is the status of section 5.4 of the design notes, decided before anything is
reserved. It reads no prompt text into the ledger and makes no network call.
"""

import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.models import CacheSupport, ModelRequest
from nuramesh.core.prices import ResolvedPrice
from nuramesh.engine.org_halt import PAGE, org_stream_id
from nuramesh.hosted import HostedConfigError, hosted_from_env
from nuramesh.stores import EventDraft, Store, UnitOfWork

KIND = "prompt_cache"
RETENTION_NOTE = "about 5 minutes after the last use"

CacheStatus = Literal[
    "absent", "applied", "stripped_not_enabled", "stripped_unsupported", "stripped_price_unknown"
]


def _record(
    store: Store, org_id: str, enabled: bool, actor: str, at: datetime | None
) -> LedgerEvent:
    payload: dict[str, Any] = {
        "kind": KIND,
        "enabled": enabled,
        "provider_retention": RETENTION_NOTE,
    }
    if at is not None:
        payload["at"] = at.isoformat()
    with store.transaction() as uow:
        return uow.append(
            EventDraft(
                stream=org_stream_id(org_id),
                type=EventType.DECISION_RECORDED,
                actor=actor,
                payload=payload,
            )
        )


def enable_prompt_cache(
    store: Store, org_id: str, *, actor: str, at: datetime | None = None
) -> LedgerEvent:
    """Turn provider-side prompt caching on for the organization; recorded in the ledger."""
    return _record(store, org_id, True, actor, at)


def disable_prompt_cache(
    store: Store, org_id: str, *, actor: str, at: datetime | None = None
) -> LedgerEvent:
    """Turn it off again; recorded in the ledger."""
    return _record(store, org_id, False, actor, at)


class PromptCacheHosted(Exception):  # noqa: N818 (a state, said in words)
    """Prompt caching cannot be switched on or off in hosted mode until the privacy notes ship
    (ND320 item 13). The text is one line, shown as is."""


HOSTED_REFUSAL = (
    "prompt caching cannot be changed in hosted mode until the privacy notes are published; "
    "nothing was recorded"
)


def refuse_if_hosted(env: Mapping[str, str] | None = None) -> None:
    """Raise `PromptCacheHosted` when `NURAMESH_HOSTED` is 1 **or unreadable** (a typo fails
    closed: it must not let a hosted deployment change a retention position)."""
    try:
        hosted = hosted_from_env(env)
    except HostedConfigError:
        hosted = True
    if hosted:
        raise PromptCacheHosted(HOSTED_REFUSAL)


@dataclass(frozen=True)
class PromptCacheChange:
    """The result of asking for on or off: the state now and whether an entry was written."""

    enabled: bool
    changed: bool
    seq: int | None  # the sequence of the `decision.recorded` entry that set the current state


def set_prompt_cache_in(
    uow: UnitOfWork, org_id: str, enabled: bool, *, actor: str, via: str
) -> PromptCacheChange:
    """Set the opt-in inside the caller's unit of work: reads the organization stream in this
    transaction and writes ONE `decision.recorded` entry only when the state changes (asking for
    the state it already has writes nothing). The time comes from the store's clock."""
    stream = org_stream_id(org_id)
    current, seq, cursor = False, None, 0
    while True:
        batch = uow.events_after(stream, cursor, PAGE)
        for event in batch:
            if (
                event.type == EventType.DECISION_RECORDED.value
                and event.payload.get("kind") == KIND
            ):
                current, seq = event.payload.get("enabled") is True, event.seq
        if batch:
            cursor = batch[-1].seq
        if len(batch) < PAGE:
            break
    if current == enabled:
        return PromptCacheChange(current, False, seq)
    written = uow.append(
        EventDraft(
            stream=stream,
            type=EventType.DECISION_RECORDED,
            actor=actor,
            payload={
                "kind": KIND,
                "enabled": enabled,
                "provider_retention": RETENTION_NOTE,
                "via": via,
            },
        )
    )
    return PromptCacheChange(enabled, True, written.seq)


_SEEN: "weakref.WeakKeyDictionary[Store, dict[str, tuple[int, bool]]]" = weakref.WeakKeyDictionary()
_SEEN_LOCK = threading.Lock()


def prompt_cache_enabled(store: Store, org_id: str) -> bool:
    """Whether the organization opted in: the last `prompt_cache` decision on its stream wins; none
    means off. Incremental: the decision and the stream position are kept per store, so a marked
    call reads only the entries appended since the last one (the ledger is append-only, so a kept
    position never goes stale). Raises what the store raises (the caller fails closed)."""
    stream = org_stream_id(org_id)
    with _SEEN_LOCK:
        cursor, enabled = _SEEN.get(store, {}).get(stream, (0, False))
        while True:
            batch = list(store.read_stream(stream, after_seq=cursor, limit=PAGE))
            for event in batch:
                if (
                    event.type == EventType.DECISION_RECORDED.value
                    and event.payload.get("kind") == KIND
                ):
                    enabled = event.payload.get("enabled") is True
            if batch:
                cursor = batch[-1].seq
            if len(batch) < PAGE:
                break
        _SEEN.setdefault(store, {})[stream] = (cursor, enabled)
        return enabled


@dataclass(frozen=True)
class CacheDecision:
    """The status of a marked call and what its provider declared."""

    status: CacheStatus
    support: CacheSupport

    @property
    def applied(self) -> bool:
        return self.status == "applied"


def decide_cache(
    store: Store,
    org_id: str,
    request: ModelRequest,
    *,
    is_gate: bool,
    support: CacheSupport,
    price: ResolvedPrice,
) -> CacheDecision | None:
    """None when the request did not ask for caching (no `cache` object at all); else the first
    status that holds (5.4, ND312)."""
    if not request.cache_prefix:
        return None
    b = request.cache_breakpoint
    if is_gate or b is None or b == len(request.messages) - 1 or not request.prefix_text:
        return CacheDecision("absent", support)
    try:
        refuse_if_hosted()  # ND320 item 5: no caching in hosted mode, whatever the ledger holds
        enabled = prompt_cache_enabled(store, org_id)
    except Exception:  # hosted, or unreadable: fail closed
        enabled = False
    if not enabled:
        return CacheDecision("stripped_not_enabled", support)
    if support != "explicit":
        return CacheDecision("stripped_unsupported", support)
    if price.cached_input_per_million is None or price.cache_write_per_million is None:
        return CacheDecision("stripped_price_unknown", support)
    return CacheDecision("applied", support)
