"""Integration expansion and the personal-data acknowledgement at engine build (R2.05).

`expand_integrations` is pure: it checks the `integrations` entries (`core.integrations`) and the
duplicate rules against hand-written `mcp` entries. `build_integrations` is the build step: expand,
run the other build checks, and only then append the acknowledgements (rule 21) in ONE transaction
that also does the duplicate check, so concurrent builds on one store write exactly one entry and
a failed append keeps none. Nothing calls `build_integrations` yet: the engine build that would call
it needs the call path (R2.04 and the E4 gateway), so the `integrations` config key is refused.

Nothing here opens a socket or imports the `mcp` SDK. Pack files are read with
`importlib.resources`.
The suggested policy fragments are data for templates and are never read here (rule 13).
"""

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any

from nuramesh.core.integrations import (
    ACK_KIND,
    IntegrationEntry,
    IntegrationError,
    Pack,
    acknowledgement_payload,
    duplicate_servers,
    expand_entry,
    parse_pack,
    validate_entry,
)
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.engine.evals import org_stream
from nuramesh.stores import EventDraft, Store

PACK_NAMES = ("github", "issues", "helpdesk", "crm")
PAGE = 1_000


@cache
def load_packs() -> Mapping[str, Pack]:
    """The shipped packs by name (a malformed pack file is a packaging error, caught by tests)."""
    base = resources.files("nuramesh.integrations").joinpath("packs")
    packs: dict[str, Pack] = {}
    for name in PACK_NAMES:
        pack = parse_pack(json.loads(base.joinpath(f"{name}.json").read_text(encoding="utf-8")))
        if pack.name != name:
            raise IntegrationError(f"pack file '{name}.json' holds pack '{pack.name}'")
        packs[name] = pack
    return packs


@dataclass(frozen=True)
class Expansion:
    """The effective `mcp` entries (hand-written first, then the expansion) and the
    acknowledgements to record (in `integrations` order)."""

    servers: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    acknowledgements: list[dict[str, Any]] = field(default_factory=list)


def _entries(raw: Sequence[Mapping[str, Any]]) -> list[tuple[IntegrationEntry, Pack]]:
    packs = load_packs()
    out: list[tuple[IntegrationEntry, Pack]] = []
    for index, item in enumerate(raw):
        name = item.get("pack") if isinstance(item, Mapping) else None
        pack = packs.get(name) if isinstance(name, str) else None
        out.append((validate_entry(item, pack, index), pack))  # type: ignore[arg-type]
    return out


def acknowledgement_payloads(
    raw: Sequence[Mapping[str, Any]], packs: Mapping[str, Pack] | None = None
) -> list[dict[str, Any]]:
    """The acknowledgement payloads for the personal-data entries, in list order."""
    return [acknowledgement_payload(e, p) for e, p in _entries(raw) if p.data_kind == "personal"]


def expand_integrations(
    raw: Sequence[Mapping[str, Any]],
    *,
    hand_servers: Iterable[Mapping[str, Any]] = (),
    hand_tools: Iterable[Mapping[str, Any]] = (),
) -> Expansion:
    """Validate the entries and expand them after the hand-written `mcp` entries. Pure; raises
    `IntegrationError` (naming pack, tool or key, never a value) on any rule violation."""
    checked = _entries(raw)
    dup = duplicate_servers([e for e, _ in checked])
    if dup:
        raise IntegrationError(f"duplicate server id '{dup[0]}' in integrations")
    servers = [dict(s) for s in hand_servers]
    tools = [dict(t) for t in hand_tools]
    taken = {s.get("id") for s in servers}
    pairs = {(t.get("server"), t.get("tool")) for t in tools}
    acks: list[dict[str, Any]] = []
    for entry, pack in checked:
        if entry.server in taken:
            raise IntegrationError(
                f"duplicate server id '{entry.server}' (hand-written mcp.servers)"
            )
        server, expanded = expand_entry(entry, pack)
        for tool in expanded:
            if (tool["server"], tool["tool"]) in pairs:
                raise IntegrationError(
                    f"duplicate registration of tool '{tool['tool']}' on server '{tool['server']}'"
                )
        taken.add(entry.server)
        pairs |= {(t["server"], t["tool"]) for t in expanded}
        servers.append(server)
        tools += expanded
        if pack.data_kind == "personal":
            acks.append(acknowledgement_payload(entry, pack))
    return Expansion(servers, tools, acks)


def _latest_by_server(uow: Any, stream: str) -> dict[str, dict[str, Any]]:
    """The latest `personal_data_acknowledged` payload of each server on the organization stream."""
    latest: dict[str, dict[str, Any]] = {}
    after = 0
    while True:
        page: list[LedgerEvent] = uow.events_after(stream, after, PAGE)
        for event in page:
            after = event.seq
            if (
                event.type == EventType.DECISION_RECORDED.value
                and event.payload.get("kind") == ACK_KIND
            ):
                latest[str(event.payload.get("server"))] = dict(event.payload)
        if len(page) < PAGE:
            return latest


def _same(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    keys = ("pack", "pack_version", "definition_set_hash")
    return all(old.get(k) == new.get(k) for k in keys)


def record_acknowledgements(
    store: Store, org_id: str, payloads: Sequence[Mapping[str, Any]]
) -> list[LedgerEvent]:
    """Append the acknowledgements that are not already the latest for their server, all in one
    transaction (the duplicate check and the append are one serialised step; if any append fails
    none stays). Returns the entries written (possibly none)."""
    if not payloads:
        return []
    stream = org_stream(org_id)
    with store.transaction() as uow:
        latest = _latest_by_server(uow, stream)
        written: list[LedgerEvent] = []
        for payload in payloads:
            server = str(payload["server"])
            old = latest.get(server)
            if old is not None and _same(old, payload):
                continue
            written.append(
                uow.append(
                    EventDraft(
                        stream=stream,
                        type=EventType.DECISION_RECORDED,
                        actor=str(payload["actor"]),
                        payload=dict(payload),
                    )
                )
            )
            latest[server] = dict(payload)
        return written


def build_integrations(
    store: Store,
    org_id: str,
    raw: Sequence[Mapping[str, Any]],
    *,
    hand_servers: Iterable[Mapping[str, Any]] = (),
    hand_tools: Iterable[Mapping[str, Any]] = (),
    checks: Sequence[Callable[[], None]] = (),
) -> Expansion:
    """The build step: expand, run every other build check (`checks`: policy loading and the other
    section 4 rules, each raising on failure), and as the LAST step record the acknowledgements.
    A build that fails any check writes nothing (rule 21)."""
    expansion = expand_integrations(raw, hand_servers=hand_servers, hand_tools=hand_tools)
    for check in checks:
        check()
    record_acknowledgements(store, org_id, expansion.acknowledgements)
    return expansion
