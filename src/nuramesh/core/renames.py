"""Stable ids, editable names, and the record of a rename (app spec §5; T38).

Every agent, role, preset and template has an **immutable id** (a slug); names are for people and
can change at any time. Ledger entries, budgets, approvals and runs reference ids, so a rename
never breaks them; the UI shows the current name. `rename_in_config` changes exactly one name and
returns a `RenameRecord` (who and when come from the ledger entry that stores it); `id_changes`
compares two configurations and says which ids appeared or disappeared, so an update that would
**remove or re-key** an id that something may still reference can be refused (`check_ids_stable`).
Custom roles are ordinary roles with their own id and name.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.org import Agent, Name, OrgGraph, Role
from nuramesh.core.presets import GatePreset, PolicyPreset, PresetConfig


class _NameCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name


class RenameKind(StrEnum):
    """What can be renamed."""

    ORG = "org"
    ROLE = "role"
    AGENT = "agent"
    POLICY_PRESET = "policy_preset"
    GATE_PRESET = "gate_preset"
    TEMPLATE = "template"


@dataclass(frozen=True)
class RenameRecord:
    """One rename: what, which immutable id, old and new name."""

    kind: RenameKind
    id: str
    old_name: str
    new_name: str

    def ledger_payload(self) -> dict[str, Any]:
        """The `decision.recorded` payload (ids and names are configuration, not content)."""
        return {
            "kind": "rename",
            "what": self.kind.value,
            "id": self.id,
            "old_name": self.old_name,
            "new_name": self.new_name,
        }


def valid_name(name: str) -> str:
    """The name as stored (whitespace stripped, 1–120 characters), or `ValueError`."""
    try:
        return _NameCheck(name=name).name
    except ValidationError:
        raise ValueError("a name is 1 to 120 characters after trimming") from None


def _renamed[M: BaseModel](item: M, name: str) -> M:
    """The same item with a new name, validated again (a plain copy would skip validation)."""
    return type(item).model_validate({**dict(item), "name": name})


def rename_in_config(
    config: NuraMeshConfig, kind: RenameKind, item_id: str, new_name: str
) -> tuple[NuraMeshConfig, RenameRecord]:
    """The configuration with one name changed, and the record. Ids never change.

    Raises `KeyError` for an unknown id and `ValueError` for an invalid or unchanged name.
    """
    name = valid_name(new_name)
    org = config.org
    if kind is RenameKind.ORG:
        if item_id != org.id:
            raise KeyError(item_id)
        old = org.name
        new_org: OrgGraph = org.model_copy(update={"name": name})
        new_config = config.model_copy(update={"org": new_org})
    elif kind is RenameKind.ROLE:
        role = org.role(item_id)
        old = role.name
        roles = tuple(_renamed(r, name) if r.id == item_id else r for r in org.roles)
        new_config = config.model_copy(update={"org": _with(org, roles=roles)})
    elif kind is RenameKind.AGENT:
        agent = org.agent(item_id)
        old = agent.name
        agents = tuple(_renamed(a, name) if a.id == item_id else a for a in org.agents)
        new_config = config.model_copy(update={"org": _with(org, agents=agents)})
    else:
        if kind is RenameKind.TEMPLATE:
            raise ValueError("a template is renamed by saving a new version (core.templates)")
        presets = config.presets or PresetConfig()
        if kind is RenameKind.POLICY_PRESET:
            found = presets.policy_preset(item_id)
            if found is None:
                raise KeyError(item_id)
            old = found.name
            changed = tuple(_renamed(p, name) if p.id == item_id else p for p in presets.policy)
            new_presets = PresetConfig(
                policy=changed, gate=presets.gate, assignments=presets.assignments
            )
        else:
            gate = presets.gate_preset(item_id)
            if gate is None:
                raise KeyError(item_id)
            old = gate.name
            changed_g = tuple(_renamed(g, name) if g.id == item_id else g for g in presets.gate)
            new_presets = PresetConfig(
                policy=presets.policy, gate=changed_g, assignments=presets.assignments
            )
        new_config = config.model_copy(update={"presets": new_presets})
    if old == name:
        raise ValueError("the name is unchanged")
    return new_config, RenameRecord(kind=kind, id=item_id, old_name=old, new_name=name)


def _with(org: OrgGraph, **changes: tuple[Role, ...] | tuple[Agent, ...]) -> OrgGraph:
    return OrgGraph(
        id=org.id,
        name=org.name,
        roles=changes.get("roles", org.roles),  # type: ignore[arg-type]
        agents=changes.get("agents", org.agents),  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class IdChanges:
    """Ids that appeared or disappeared between two configurations, by kind."""

    removed: dict[str, frozenset[str]]
    added: dict[str, frozenset[str]]

    @property
    def any_removed(self) -> bool:
        """Whether any id disappeared."""
        return any(self.removed.values())


def _ids(config: NuraMeshConfig) -> dict[str, frozenset[str]]:
    presets = config.presets or PresetConfig()
    suites = config.evals.suites if config.evals else ()
    return {
        "role": frozenset(r.id for r in config.org.roles),
        "agent": frozenset(a.id for a in config.org.agents),
        "policy_preset": frozenset(p.id for p in presets.policy),
        "gate_preset": frozenset(g.id for g in presets.gate),
        "gate": frozenset(f"{g.id}/{t.id}" for g in presets.gate for t in g.gates),
        "budget": frozenset(b.id for b in config.budgets),
        "eval_suite": frozenset(s.id for s in suites),
    }


def id_changes(old: NuraMeshConfig, new: NuraMeshConfig) -> IdChanges:
    """Which ids each kind lost or gained going from `old` to `new`."""
    before, after = _ids(old), _ids(new)
    return IdChanges(
        removed={k: before[k] - after[k] for k in before},
        added={k: after[k] - before[k] for k in before},
    )


def check_ids_stable(old: NuraMeshConfig, new: NuraMeshConfig) -> None:
    """Refuse an update that drops an id: ledger entries, budgets, approvals and runs may still
    point at it. (Retiring an id is a deliberate, separate operation; adding ids is always fine.)"""
    gone = {k: sorted(v) for k, v in id_changes(old, new).removed.items() if v}
    if gone:
        raise ValueError(f"ids can't be removed or changed, only renamed: {gone}")


__all__ = [
    "GatePreset",
    "IdChanges",
    "PolicyPreset",
    "RenameKind",
    "RenameRecord",
    "check_ids_stable",
    "id_changes",
    "rename_in_config",
    "valid_name",
]
