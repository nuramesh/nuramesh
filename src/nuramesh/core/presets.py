"""Reusable rules: policy presets, gate/eval presets and who uses them (app spec §5; T38).

- A **policy preset** is a named set of permission rules ("Read-only", "Write code, no merge"...).
- A **gate preset** is a named, ordered list of gate templates plus eval suite ids ("Python tests in
  the sandbox", "Security review"). A gate template is a gate without its criteria: the task that
  uses it says which criteria each gate checks (`instantiate_gates`).
- A **preset assignment** says which presets a role uses. Editing a preset therefore changes every
  role that uses it, which is why preset edits are validated like any configuration and recorded
  in the ledger (`engine.changes`).

`resolve_policies` expands the assignments into ordinary role policies: a role's effective rules
are its presets' rules (in the order listed) followed by its own. Evaluation is order-independent
(`deny` beats `ask` beats `allow`, `core.policy`), so **a role can add rules on top of a preset but
can never loosen it: a preset's deny always wins**. `rule_origins` says where each effective rule
came from (`preset:<id>` or `role`), for the UI and for audits.

Presets have immutable ids (slugs) like everything else; names are editable (`core.renames`).
"""

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from nuramesh.core.canonical import canonical_json, content_hash, freeze, thaw
from nuramesh.core.evals import EvalConfig
from nuramesh.core.gate_config import HumanGateConfig
from nuramesh.core.gates import DETERMINISTIC_KINDS, GateKind, GateSpec
from nuramesh.core.org import Name, Slug
from nuramesh.core.policy import Policy, PolicyRule, PolicySet

MAX_RULES = 500  # the engine's limit on one role's policy
Description = Annotated[str, StringConstraints(max_length=500)]


class _Frozen(BaseModel):
    """Immutable, and checked again whenever an instance is passed in or copied: a preset can
    never reach the engine, the hash or the ledger without having passed validation."""

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """A changed copy, validated again (a plain pydantic copy would skip every check)."""
        data: dict[str, Any] = dict(self)
        if update:
            data.update(update)
        return type(self).model_validate(data)


class PolicyPreset(_Frozen):
    """A named set of permission rules."""

    id: Slug
    name: Name
    description: Description = ""
    rules: tuple[PolicyRule, ...] = Field(default=(), max_length=200)

    @property
    def preset_hash(self) -> str:
        """A hash of the whole preset, for the ledger entry that records an edit."""
        return content_hash(self.model_dump(mode="json"))


class GateTemplate(_Frozen):
    """A gate without its criteria: the task using the preset says what each gate checks."""

    id: Slug
    kind: GateKind
    required: bool = True
    config: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("config", mode="after")
    @classmethod
    def _hashable(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        """Read-only all the way down, and it must hash the same everywhere (floats refused)."""
        try:
            canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"the gate config can't be hashed: {exc}") from None
        frozen: Mapping[str, Any] = freeze(value)
        return frozen

    @model_validator(mode="after")
    def _human_gate_is_well_formed(self) -> Self:
        """A human gate's config is checked here too, so an email can't hide in a preset (T42)."""
        if self.kind is GateKind.HUMAN:
            try:
                HumanGateConfig.model_validate(thaw(self.config))
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
                )
                raise ValueError(f"human gate config: {problems}") from None
        return self

    @field_serializer("config")
    def _plain_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Plain data for hashing, JSON and YAML (the stored mapping is read-only)."""
        plain: dict[str, Any] = thaw(config)
        return plain


class GatePreset(_Frozen):
    """A named, ordered list of gate templates and the eval suites that go with them."""

    id: Slug
    name: Name
    description: Description = ""
    gates: tuple[GateTemplate, ...] = Field(min_length=1, max_length=20)
    evals: tuple[Slug, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        ids = [g.id for g in self.gates]
        if len(set(ids)) != len(ids):
            raise ValueError("gate ids must be unique within a preset")
        if len(set(self.evals)) != len(self.evals):
            raise ValueError("an eval suite is listed twice")
        seen_costly = False
        for gate in self.gates:  # D.5: cheap gates first
            if gate.kind in DETERMINISTIC_KINDS:
                if seen_costly:
                    raise ValueError(
                        f"gate '{gate.id}' is a cheap check listed after a verifier or human "
                        "gate: order gates cheapest first"
                    )
            else:
                seen_costly = True
        if not any(g.required for g in self.gates):
            raise ValueError("a gate preset needs at least one required gate")
        return self

    @property
    def preset_hash(self) -> str:
        """A hash of the whole preset."""
        return content_hash(self.model_dump(mode="json"))


class PresetAssignment(_Frozen):
    """The presets one role uses."""

    role: Slug
    policy_presets: tuple[Slug, ...] = ()
    gate_preset: Slug | None = None

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.policy_presets)) != len(self.policy_presets):
            raise ValueError("a policy preset is listed twice")
        return self


class PresetConfig(_Frozen):
    """All presets and their assignments (the `presets` key of a configuration)."""

    policy: tuple[PolicyPreset, ...] = Field(default=(), max_length=500)
    gate: tuple[GatePreset, ...] = Field(default=(), max_length=500)
    assignments: tuple[PresetAssignment, ...] = Field(default=(), max_length=1_000)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for kind, items in (("policy", self.policy), ("gate", self.gate)):
            ids = [p.id for p in items]
            if len(set(ids)) != len(ids):
                raise ValueError(f"{kind} preset ids must be unique")
        roles = [a.role for a in self.assignments]
        if len(set(roles)) != len(roles):
            raise ValueError("a role has more than one preset assignment")
        policy_ids = {p.id for p in self.policy}
        gate_ids = {g.id for g in self.gate}
        for a in self.assignments:
            for pid in a.policy_presets:
                if pid not in policy_ids:
                    raise ValueError(f"role '{a.role}' uses unknown policy preset '{pid}'")
            if a.gate_preset is not None and a.gate_preset not in gate_ids:
                raise ValueError(f"role '{a.role}' uses unknown gate preset '{a.gate_preset}'")
        return self

    def policy_preset(self, preset_id: str) -> PolicyPreset | None:
        """The policy preset with this id."""
        return next((p for p in self.policy if p.id == preset_id), None)

    def gate_preset(self, preset_id: str) -> GatePreset | None:
        """The gate preset with this id."""
        return next((g for g in self.gate if g.id == preset_id), None)

    def assignment_for(self, role: str) -> PresetAssignment | None:
        """The role's preset assignment, if any."""
        return next((a for a in self.assignments if a.role == role), None)


class PresetChange(_Frozen):
    """One preset created, edited or removed (the ledger records each, T39). `roles` are the roles
    that use it; its rules reach those roles' agents (policy presets) at once."""

    what: Literal["policy_preset", "gate_preset"]
    id: Slug
    change: Literal["created", "updated", "removed"]
    old_hash: str | None = None
    new_hash: str | None = None
    roles: tuple[Slug, ...] = ()  # the roles that use it: every agent holding one is affected

    def ledger_payload(self) -> dict[str, Any]:
        """The `decision.recorded` payload: ids and hashes only."""
        return {
            "kind": "preset_change",
            "what": self.what,
            "id": self.id,
            "change": self.change,
            "old_hash": self.old_hash,
            "new_hash": self.new_hash,
            "roles": list(self.roles),
        }


def preset_content_hash(preset: PolicyPreset | GatePreset) -> str:
    """The hash of a preset without its name (a rename is recorded as a rename)."""
    return content_hash({k: v for k, v in preset.model_dump(mode="json").items() if k != "name"})


_content_hash = preset_content_hash


def preset_changes(before: PresetConfig | None, after: PresetConfig | None) -> list[PresetChange]:
    """What changed between two preset sets, preset by preset, in id order.

    A pure rename is not a change here (it is recorded as a rename). `roles` are the roles that use
    the preset in `after` (in `before` for a removed one), i.e. whose agents' rules just changed.
    """
    old = before or PresetConfig()
    new = after or PresetConfig()
    changes: list[PresetChange] = []
    for what, olds, news in (
        ("policy_preset", {p.id: p for p in old.policy}, {p.id: p for p in new.policy}),
        ("gate_preset", {g.id: g for g in old.gate}, {g.id: g for g in new.gate}),
    ):
        for item_id in sorted(set(olds) | set(news)):
            was, now = olds.get(item_id), news.get(item_id)
            old_hash = _content_hash(was) if was is not None else None
            new_hash = _content_hash(now) if now is not None else None
            if old_hash == new_hash:
                continue
            source = new if now is not None else old
            if what == "policy_preset":
                roles = tuple(a.role for a in source.assignments if item_id in a.policy_presets)
            else:
                roles = tuple(a.role for a in source.assignments if a.gate_preset == item_id)
            changes.append(
                PresetChange(
                    what=what,  # type: ignore[arg-type]
                    id=item_id,
                    change="created" if was is None else "removed" if now is None else "updated",
                    old_hash=old_hash,
                    new_hash=new_hash,
                    roles=tuple(sorted(roles)),
                )
            )
    return changes


def assignment_changes(before: PresetConfig | None, after: PresetConfig | None) -> list[str]:
    """Roles whose preset assignment differs (applying or removing a preset), sorted."""
    old = {a.role: a for a in (before.assignments if before else ())}
    new = {a.role: a for a in (after.assignments if after else ())}
    return sorted(r for r in set(old) | set(new) if old.get(r) != new.get(r))


def assignment_details(
    before: PresetConfig | None, after: PresetConfig | None
) -> list[dict[str, Any]]:
    """What each changed assignment was and became (preset ids, in order), for the ledger: removing
    a deny preset loosens a role, and the record has to say which one."""
    old = {a.role: a for a in (before.assignments if before else ())}
    new = {a.role: a for a in (after.assignments if after else ())}
    details: list[dict[str, Any]] = []
    for role in assignment_changes(before, after):
        was, now = old.get(role), new.get(role)
        details.append(
            {
                "role": role,
                "old_policy_presets": list(was.policy_presets) if was else [],
                "new_policy_presets": list(now.policy_presets) if now else [],
                "old_gate_preset": was.gate_preset if was else None,
                "new_gate_preset": now.gate_preset if now else None,
            }
        )
    return details


def preset_renames(
    before: PresetConfig | None, after: PresetConfig | None
) -> list[tuple[Literal["policy_preset", "gate_preset"], str, str, str]]:
    """Presets that exist in both and changed name: (what, id, old name, new name)."""
    old, new = before or PresetConfig(), after or PresetConfig()
    found: list[tuple[Literal["policy_preset", "gate_preset"], str, str, str]] = []
    for p in new.policy:
        was = old.policy_preset(p.id)
        if was is not None and was.name != p.name:
            found.append(("policy_preset", p.id, was.name, p.name))
    for g in new.gate:
        gwas = old.gate_preset(g.id)
        if gwas is not None and gwas.name != g.name:
            found.append(("gate_preset", g.id, gwas.name, g.name))
    return found


def resolve_policies(policies: PolicySet, presets: PresetConfig | None) -> PolicySet:
    """Every role's effective policy: its presets' rules (in order), then its own rules."""
    if presets is None or not presets.assignments:
        return policies
    own = {p.role: p for p in policies.policies}
    roles = list(dict.fromkeys([*own, *(a.role for a in presets.assignments)]))
    out: list[Policy] = []
    for role in roles:
        base = own.get(role)
        assignment = presets.assignment_for(role)
        rules: list[PolicyRule] = []
        for pid in assignment.policy_presets if assignment else ():
            preset = presets.policy_preset(pid)
            assert preset is not None  # PresetConfig validated the reference
            rules.extend(preset.rules)
        rules.extend(base.rules if base else ())
        if len(rules) > MAX_RULES:
            raise ValueError(
                f"role '{role}' would have {len(rules)} rules; the limit is {MAX_RULES}"
            )
        out.append(Policy(role=role, rules=tuple(rules), approvers=base.approvers if base else ()))
    limit = policies.solo_approvals_per_hour  # SA-9: the limit is the organization's, not a role's
    if limit is None:
        return PolicySet(policies=tuple(out))
    return PolicySet(policies=tuple(out), solo_approvals_per_hour=limit)


def rule_origins(policies: PolicySet, presets: PresetConfig | None, role: str) -> list[str]:
    """Where each effective rule of `role` came from, in order: `preset:<id>` or `role`."""
    own = policies.policy_for(role)
    origins: list[str] = []
    if presets is not None:
        assignment = presets.assignment_for(role)
        for pid in assignment.policy_presets if assignment else ():
            preset = presets.policy_preset(pid)
            assert preset is not None
            origins.extend([f"preset:{pid}"] * len(preset.rules))
    origins.extend(["role"] * (len(own.rules) if own else 0))
    return origins


def instantiate_gates(
    preset: GatePreset, checks: Mapping[str, Sequence[str]]
) -> tuple[GateSpec, ...]:
    """Real gates for a task from a preset: `checks[gate id]` names the criteria each gate checks.

    Every gate in the preset needs an entry (a gate that checks nothing is meaningless), and an
    entry for a gate that isn't in the preset is an error (a typo would silently drop a gate).
    """
    known = {g.id for g in preset.gates}
    extra = sorted(set(checks) - known)
    if extra:
        raise ValueError(f"checks for gates the preset doesn't have: {extra}")
    missing = [g.id for g in preset.gates if not checks.get(g.id)]
    if missing:
        raise ValueError(f"say which criteria these gates check: {missing}")
    return tuple(
        GateSpec(
            id=g.id,
            kind=g.kind,
            checks=tuple(checks[g.id]),
            required=g.required,
            config=thaw(g.config),
        )
        for g in preset.gates
    )


def gates_for_role(
    presets: PresetConfig | None, role: str, checks: Mapping[str, Sequence[str]]
) -> tuple[GateSpec, ...] | None:
    """The gates a task for `role` must carry, built from the role's gate preset.

    `None` when the role has no gate preset (the caller writes its own gates). Otherwise the same
    result and errors as `instantiate_gates`.
    """
    preset = gate_preset_for_role(presets, role)
    if preset is None:
        return None
    return instantiate_gates(preset, checks)


def gate_preset_for_role(presets: PresetConfig | None, role: str) -> GatePreset | None:
    """The gate preset a role uses, if any."""
    if presets is None:
        return None
    assignment = presets.assignment_for(role)
    if assignment is None or assignment.gate_preset is None:
        return None
    return presets.gate_preset(assignment.gate_preset)


def effective_suites(
    evals: EvalConfig | None, presets: PresetConfig | None, role: str
) -> tuple[str, ...]:
    """The eval suite ids a role must pass: its own requirements, then its gate preset's.

    Each id appears once. An id that `evals` doesn't define is not satisfiable (checked by
    `check_eligibility`, which fails closed).
    """
    ids: list[str] = [s.id for s in evals.required_for(role)] if evals is not None else []
    preset = gate_preset_for_role(presets, role)
    if preset is not None:
        ids.extend(sid for sid in preset.evals if sid not in ids)
    return tuple(ids)
