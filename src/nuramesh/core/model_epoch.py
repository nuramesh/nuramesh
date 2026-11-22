"""Model epochs, the floor and honouring a grant (spec/model-change sections 2 to 5, ND391).

Pure derivations from the organization stream's entries; nothing here writes or reads a store.
A subject (an agent or a role) starts a new **model epoch** at every recorded model change and at
every recorded drift. A grant is honoured only in the epoch it was earned in, except that a grant
whose action kind is on the role's floor is carried across one boundary for a bounded time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nuramesh.core.autonomy import (
    Config,
    Entry,
    decision_kind,
    entry_id,
    parse_config,
    parse_grant,
)

KIND_FLOOR = "model_change_floor_set"
KIND_DRIFT = "model_drift_detected"

CARRY_DAYS = 7
FLOOR_KINDS = frozenset({"delegate", "call_model", "use_tool", "write_file"})

CAUSE_CONFIG = "config"
CAUSE_DRIFT = "drift"

STATE_MALFORMED = "malformed"
STATE_MISMATCH = "epoch_mismatch"
STATE_IN_EPOCH = "in_epoch"
STATE_CHANGED = "epoch_changed"
STATE_CARRIED = "carried"
STATE_CARRY_ENDED = "carry_ended"
HONOURED_STATES = frozenset({STATE_IN_EPOCH, STATE_CARRIED})


@dataclass(frozen=True)
class Boundary:
    """An entry that ends one model epoch of a subject and starts the next (MC1 to MC4)."""

    pos: int
    at: datetime
    entry: str
    cause: str


@dataclass(frozen=True)
class Honour:
    """Whether a grant is honoured at `as_of`, and why (MC12)."""

    state: str
    own_epoch: int | None = None
    boundary: Boundary | None = None
    carry_until: datetime | None = None

    @property
    def honoured(self) -> bool:
        return self.state in HONOURED_STATES


class _Malformed:
    """A model value that is neither a string nor null (MC1): never equal to anything."""

    def __repr__(self) -> str:
        return "<malformed model>"


MALFORMED = _Malformed()
_Model = str | None | _Malformed


def _raw_models(item: Entry) -> dict[str, _Model]:
    """Each agent's model read from the raw payload: a non-string, non-null one is MALFORMED."""
    agents = item.event.payload.get("agents")
    out: dict[str, _Model] = {}
    if isinstance(agents, Mapping):
        for name, info in agents.items():
            model = info.get("model") if isinstance(info, Mapping) else None
            out[str(name)] = model if model is None or isinstance(model, str) else MALFORMED
    return out


def _in_scope(
    config: Config, raw: Mapping[str, _Model], subject_kind: str, subject_id: str
) -> dict[str, _Model]:
    """The models this subject is tracked by in one configuration record."""
    if subject_kind == "role":
        return {a: raw.get(a) for a, (role, _) in config.agents.items() if role == subject_id}
    return {subject_id: raw.get(subject_id)} if subject_id in config.agents else {}


def _differs(old: _Model, new: _Model) -> bool:
    """True when the model changed; a malformed value on either side always counts as a change."""
    return old is MALFORMED or new is MALFORMED or old != new


def boundaries(
    subject_kind: str, subject_id: str, entries: Sequence[Entry]
) -> tuple[Boundary, ...]:
    """The subject's epoch boundaries, in commit order (MC1 to MC4).

    A `config_loaded` record is a boundary when an agent of the subject has a model other than
    its last seen one (for a role: last seen in that role). A `model_drift_detected` entry is a
    boundary for every subject on the named model in the configuration in force; one that names
    no model, or comes before any configuration, is a boundary for every subject. Null is a value;
    a model that is neither a string nor null is malformed and a boundary against any value."""
    seen: dict[str, _Model] = {}
    in_force: dict[str, _Model] | None = None
    found: list[Boundary] = []
    for item in sorted(entries, key=lambda e: e.pos):
        kind = decision_kind(item.event)
        if kind == KIND_DRIFT:
            model = item.event.payload.get("model")
            if in_force is None or not isinstance(model, str) or model in in_force.values():
                found.append(Boundary(item.pos, item.event.at, entry_id(item.event), CAUSE_DRIFT))
            continue
        config = parse_config(item) if kind is not None else None
        if config is None:
            continue
        in_force = _in_scope(config, _raw_models(item), subject_kind, subject_id)
        if any(agent in seen and _differs(seen[agent], model) for agent, model in in_force.items()):
            found.append(Boundary(config.pos, config.at, config.entry, CAUSE_CONFIG))
        seen.update(in_force)
    return tuple(found)


def epoch_before(bounds: Sequence[Boundary], pos: int) -> int:
    """The subject's epoch number at a commit position: its boundaries committed before it."""
    return sum(1 for b in bounds if b.pos < pos)


def epoch_at(bounds: Sequence[Boundary], as_of: datetime) -> int:
    """The subject's epoch number at `as_of`: its boundaries with `at <= as_of` (MC5)."""
    return sum(1 for b in bounds if b.at <= as_of)


def evidence_start(
    subject_kind: str, subject_id: str, entries: Sequence[Entry], as_of: datetime
) -> int | None:
    """The position after which an entry can be evidence for a new grant (MC17), or None."""
    visible = [b.pos for b in boundaries(subject_kind, subject_id, entries) if b.at <= as_of]
    return max(visible) if visible else None


def epoch_field(
    subject_kind: str, subject_id: str, entries: Sequence[Entry], pos: int
) -> dict[str, Any]:
    """The `model_epoch` field of a grant written at position `pos` (MC10).

    `n` is the subject's epoch number there; `models` gives the subject's agents their model in
    the configuration in force (the latest record committed before `pos`)."""
    in_force: dict[str, Any] = {}
    for item in sorted(entries, key=lambda e: e.pos):
        if item.pos >= pos:
            break
        config = parse_config(item)
        if config is not None:
            coerced = {a: m for a, (_, m) in config.agents.items()}
            in_force = _in_scope(config, coerced, subject_kind, subject_id)
    bounds = boundaries(subject_kind, subject_id, entries)
    return {"n": epoch_before(bounds, pos), "models": dict(sorted(in_force.items()))}


def floor_at(entries: Sequence[Entry], role: str, pos: int) -> frozenset[str]:
    """The role's floor in force at a boundary at position `pos` (MC7).

    The latest `model_change_floor_set` for the role committed before `pos` decides; an entry
    whose `kinds` is not a sequence of floor kinds is an empty floor (fail closed)."""
    latest: Entry | None = None
    for item in entries:
        if (
            item.pos < pos
            and decision_kind(item.event) == KIND_FLOOR
            and item.event.payload.get("role") == role
            and (latest is None or item.pos > latest.pos)
        ):
            latest = item
    if latest is None:
        return frozenset()
    kinds = latest.event.payload.get("kinds")
    if not isinstance(kinds, (list, tuple)) or not all(
        isinstance(k, str) and k in FLOOR_KINDS for k in kinds
    ):
        return frozenset()
    return frozenset(kinds)


def _carried_matches(carried: Any, derived: Mapping[str, Any]) -> bool:
    """MC11: the epoch a grant carries equals the one derived from the ledger."""
    if not isinstance(carried, Mapping):
        return False
    n, models = carried.get("n"), carried.get("models")
    if not isinstance(n, int) or isinstance(n, bool) or not isinstance(models, Mapping):
        return False
    return bool(n == derived["n"] and dict(models) == derived["models"])


def honour(
    grant_item: Entry, entries: Sequence[Entry], as_of: datetime, *, carry_days: int = CARRY_DAYS
) -> Honour:
    """The honour state of the grant an entry records, at `as_of` (MC12).

    This is one more condition of "active" beside the checks of spec/autonomy 6.2; it never
    makes a grant active that another check ends."""
    if isinstance(carry_days, bool) or not 1 <= carry_days <= CARRY_DAYS:
        raise ValueError(f"carry_days must be 1 to {CARRY_DAYS} (stricter is shorter)")
    grant = parse_grant(grant_item)
    if grant is None:
        return Honour(STATE_MALFORMED)
    bounds = boundaries(grant.subject_kind, grant.subject_id, entries)
    own = epoch_before(bounds, grant.pos)
    payload = grant_item.event.payload
    if "model_epoch" in payload and not _carried_matches(
        payload["model_epoch"],
        epoch_field(grant.subject_kind, grant.subject_id, entries, grant.pos),
    ):
        return Honour(STATE_MISMATCH, own)
    after = [b for b in bounds if b.pos > grant.pos and b.at <= as_of]
    if not after:
        return Honour(STATE_IN_EPOCH, own)
    first = after[0]
    if len(after) > 1 or grant.action_kind not in floor_at(entries, grant.role, first.pos):
        return Honour(STATE_CHANGED, own, first)
    until = min(grant.expires_at, first.at + timedelta(days=carry_days))
    return Honour(STATE_CARRIED if as_of < until else STATE_CARRY_ENDED, own, first, until)
