"""Integration packs (R2.05): data-only contracts of tools, never a credential or a URL.

A pack lists the tools of one kind of service (`github`, `issues`, `helpdesk`, `crm`) with their
argument names and effects. An organization's `integrations` entry pins the definition hash of every
tool and expands into `mcp.servers` and `mcp.tools` entries; **applying a pack grants nothing**
(default deny stays). Pure: stdlib and pydantic only, no I/O (the engine side reads the files).

Errors name the pack, the tool or the key, never a hash or any other value of the entry.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from nuramesh.core.canonical import canonical_json

ENGINE_BUILD_ACTOR: Final = "system:engine-build"
"""The actor of the acknowledgement entry. The spec says `system:engine_build`, but the ledger actor
grammar (`core.ledger.Actor`, pinned by tests/verifier/test_actor_grammar.py) has no underscore in a
`system:` name; an internal decision record records the hyphen."""
ACK_KIND: Final = "personal_data_acknowledged"
SERVER_PATTERN: Final = re.compile(r"[a-z][a-z0-9-]{0,62}")
TOOL_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]{1,128}")
HASH_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
ENTRY_KEYS: Final = frozenset(
    {"pack", "pack_version", "server", "definition_hashes", "acknowledge_personal_data"}
)
EffectKind = Literal["external_message", "merge"]


class IntegrationError(ValueError):
    """A pack or an integrations entry is invalid. The message never holds a value or a hash."""


class Effect(BaseModel):
    """What a write tool does to the outside world (R2.04 form)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EffectKind
    arg: str = Field(min_length=1, max_length=128)


class PackTool(BaseModel):
    """One tool of a pack."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    kind: Literal["read", "write"]
    allowed_args: tuple[str, ...] = Field(max_length=64)
    effects: tuple[Effect, ...] = Field(default=(), max_length=8)
    paid: Literal[False] = False

    @field_validator("tool")
    @classmethod
    def _tool_name(cls, value: str) -> str:
        if not TOOL_PATTERN.fullmatch(value):
            raise ValueError("tool name does not match the tool pattern")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "PackTool":
        if len(set(self.allowed_args)) != len(self.allowed_args):
            raise ValueError("allowed_args repeats a name")
        if self.kind == "read" and self.effects:
            raise ValueError("a read tool has no effects")
        if self.kind == "write" and not self.effects:
            raise ValueError("a write tool needs at least one effect")
        args = [e.arg for e in self.effects]
        if len(set(args)) != len(args) or not set(args) <= set(self.allowed_args):
            raise ValueError("an effect arg must be a distinct member of allowed_args")
        return self


class Pack(BaseModel):
    """A versioned, data-only contract (no hash, URL, command, credential or environment name)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    pack_version: int = Field(ge=1)
    data_kind: Literal["code", "personal"]
    tools: tuple[PackTool, ...] = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SERVER_PATTERN.fullmatch(value):
            raise ValueError("pack name is not a slug")
        return value

    @model_validator(mode="after")
    def _unique(self) -> "Pack":
        names = [t.tool for t in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("a tool name is repeated")
        return self


def _problem(error: ValidationError) -> str:
    """Field paths only: pydantic's text can echo the input value."""
    places = sorted({".".join(str(p) for p in e["loc"]) or "(entry)" for e in error.errors()})
    return ", ".join(places)


def parse_pack(data: Mapping[str, Any]) -> Pack:
    """A pack from its JSON mapping; `IntegrationError` (a packaging error) when it is invalid."""
    try:
        return Pack.model_validate(dict(data))
    except ValidationError as error:
        label = data.get("name") if isinstance(data.get("name"), str) else "?"
        raise IntegrationError(f"pack '{label}' is invalid at: {_problem(error)}") from None


def definition_hash_of(tool_object: Mapping[str, Any]) -> str:
    """`sha256` of the canonical JSON of one whole wire tool object (R2.04 definition hash).
    Refuses floats and integers over 2^53 - 1 (`canonical_json`)."""
    return hashlib.sha256(canonical_json(dict(tool_object))).hexdigest()


def definition_set_hash(hashes: Mapping[str, str]) -> str:
    """`sha256` over the canonical JSON of the sorted tool-name to definition-hash map."""
    return hashlib.sha256(canonical_json(dict(sorted(hashes.items())))).hexdigest()


class IntegrationEntry(BaseModel):
    """One `integrations` entry of the organization configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack: str
    pack_version: int
    server: str
    definition_hashes: Mapping[str, str]
    acknowledge_personal_data: Literal[True] | None = None


def validate_entry(raw: Mapping[str, Any], pack: Pack | None, index: int) -> IntegrationEntry:
    """Check one raw entry against its pack (rules 3 to 8). `pack` is None for an unknown pack."""
    where = f"integrations[{index}]"
    if not isinstance(raw, Mapping):
        raise IntegrationError(f"{where} must be a mapping")
    for key in raw:
        if key not in ENTRY_KEYS:
            raise IntegrationError(f"{where}: key '{key}' is not allowed")
    name = raw.get("pack")
    if pack is None:
        raise IntegrationError(f"{where}: unknown pack '{name if isinstance(name, str) else '?'}'")
    if "acknowledge_personal_data" in raw and pack.data_kind != "personal":
        raise IntegrationError(
            f"{where}: acknowledge_personal_data is only for personal packs, not '{pack.name}'"
        )
    if pack.data_kind == "personal" and raw.get("acknowledge_personal_data") is not True:
        raise IntegrationError(
            f"{where}: pack '{pack.name}' holds personal data and needs "
            "acknowledge_personal_data: true"
        )
    given = raw.get("pack_version")
    if type(given) is not int or given != pack.pack_version:
        shown = given if type(given) is int else "?"
        raise IntegrationError(
            f"{where}: pack '{pack.name}' is at version {pack.pack_version} but the entry says "
            f"{shown}; re-pin the hashes and set pack_version: {pack.pack_version}"
        )
    server = raw.get("server")
    if not isinstance(server, str) or not SERVER_PATTERN.fullmatch(server):
        raise IntegrationError(f"{where}: server is not a slug")
    hashes = raw.get("definition_hashes")
    if not isinstance(hashes, Mapping):
        raise IntegrationError(f"{where}: pack '{pack.name}' needs definition_hashes")
    wanted = [t.tool for t in pack.tools]
    for tool in wanted:
        if tool not in hashes:
            raise IntegrationError(
                f"{where}: pack '{pack.name}' has no definition hash for tool '{tool}'"
            )
    for tool in hashes:
        if tool not in wanted:
            raise IntegrationError(
                f"{where}: pack '{pack.name}' has no tool '{tool}' (extra definition hash)"
            )
    for tool in wanted:
        value = hashes[tool]
        if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
            raise IntegrationError(
                f"{where}: pack '{pack.name}' tool '{tool}': the hash is not 64 lowercase hex"
            )
    return IntegrationEntry(
        pack=pack.name,
        pack_version=pack.pack_version,
        server=server,
        definition_hashes=dict(hashes),
        acknowledge_personal_data=True if pack.data_kind == "personal" else None,
    )


def expand_entry(
    entry: IntegrationEntry, pack: Pack
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The `mcp.servers` entry (always `host`) and the `mcp.tools` entries of one integration."""
    server = {"id": entry.server, "transport": "host"}
    tools = [
        {
            "server": entry.server,
            "tool": t.tool,
            "allowed_args": list(t.allowed_args),
            "definition_hash": entry.definition_hashes[t.tool],
            "effects": [{"kind": e.kind, "arg": e.arg} for e in t.effects],
            "paid": False,
        }
        for t in pack.tools
    ]
    return server, tools


def acknowledgement_payload(entry: IntegrationEntry, pack: Pack) -> dict[str, Any]:
    """The `decision.recorded` payload of a personal-data acknowledgement (rule 21): exactly these
    seven keys, no target and no tool text."""
    return {
        "kind": ACK_KIND,
        "pack": pack.name,
        "server": entry.server,
        "pack_version": pack.pack_version,
        "data_kind": "personal",
        "definition_set_hash": definition_set_hash(entry.definition_hashes),
        "actor": ENGINE_BUILD_ACTOR,
    }


def duplicate_servers(entries: Sequence[IntegrationEntry]) -> list[str]:
    """Server ids used by more than one entry."""
    seen: set[str] = set()
    out: list[str] = []
    for e in entries:
        if e.server in seen and e.server not in out:
            out.append(e.server)
        seen.add(e.server)
    return out
