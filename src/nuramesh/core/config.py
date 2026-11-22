"""The configuration of an organization as one document (PLAN K.3; decisions A2, T25).

A configuration has an optional `version` (only 1 exists), the **org** (roles and agents), the
**policies** (per role: default deny, A6/T20), **budgets** (caps by run, task, agent or team),
**prices** (the versioned price book, A3), **price_overrides** (owner-approved provisional
prices, A3a) and an optional **canon** source (a Git repository, a pinned commit and path patterns,
T26), optional **evals** (suites per role and which roles require them, T27) and optional
**presets** (reusable permission, gate and eval sets that roles use, T39). It arrives as a
plain mapping (parsed from YAML or JSON outside core, by `nuramesh.config`, which refuses
duplicate keys, floats and unsafe YAML) and is checked here.

- `parse_config(data)` returns a `NuraMeshConfig` or raises `ConfigValidationError` listing
  **every** problem with a path, in one pass: unknown top-level keys, malformed fields, every org
  graph rule (`OrgGraph.build`), and cross-references between the parts (a policy for a role that
  doesn't exist, a budget for an agent or team lead that doesn't exist, duplicate budget ids, two
  overrides for one model). Each part is checked on its own, so a problem in one part never hides
  another; only references **into** the org are skipped while the org itself is invalid. Paths point
  at the problem (`$.budgets[1].id`, `$.org.agents[2]`, `$.policies.policies[0].role`).
- `config_json_schema()` is the JSON Schema generated from the models, for editors and the visual
  builder. It describes **shape** (fields, types, enums); the graph and cross-reference rules above
  need `parse_config`, and it can't express some model rules (whitespace is stripped before length
  checks, floats are refused for money, a timestamp may arrive as a number): `parse_config` is the
  authority, the schema is for editor help. The schema is committed as
  `spec/schema/nuramesh-config.schema.json` and a test fails if it drifts from the models.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from nuramesh.core.breakers import MAX_RULES, BreakerRule
from nuramesh.core.budgets import Budget, BudgetScope
from nuramesh.core.calibration import CalibrationConfig
from nuramesh.core.canon import CanonSource
from nuramesh.core.content import ContentSettings
from nuramesh.core.evals import EvalConfig
from nuramesh.core.model_catalog import ModelCatalog, catalog_problems
from nuramesh.core.org import OrgGraph, OrgValidationError
from nuramesh.core.policy import PolicySet
from nuramesh.core.presets import PresetConfig, resolve_policies
from nuramesh.core.prices import Price, PriceBook, PriceOverride, operator_source_problem

TOP_LEVEL_KEYS = (
    "version",
    "org",
    "policies",
    "budgets",
    "breakers",
    "prices",
    "price_overrides",
    "content",
    "canon",
    "evals",
    "calibration",
    "presets",
    "models",
)


class ConfigIssue(BaseModel):
    """One problem in a configuration: where (`$.org.agents[2].role`) and what."""

    model_config = ConfigDict(frozen=True)

    path: str  # for example `$.budgets[1].scope_id` or `$.org.agents[2]`
    message: str


class ConfigValidationError(ValueError):
    """A configuration has problems; `issues` lists every one."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = issues
        lines = "\n".join(f"- {i.path}: {i.message}" for i in issues)
        super().__init__(f"Configuration is invalid:\n{lines}")


class BreakersConfig(BaseModel):
    """The `breakers:` section (E19, ND106, `spec/e19/README.md` section 7): how often the server
    evaluates, when a failing evaluation halts the organization, and the rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_seconds: int = Field(default=10, ge=1, le=3600)
    halt_after_failed_ticks: int = Field(default=3, ge=1, le=100)
    rules: list[BreakerRule] = Field(default_factory=list, max_length=MAX_RULES)


class _Parts(BaseModel):
    """Every part except the org, validated on its own so each problem gets its real path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    policies: PolicySet = PolicySet()
    budgets: tuple[Budget, ...] = Field(default=(), max_length=10_000)
    breakers: BreakersConfig = BreakersConfig()
    prices: PriceBook = PriceBook()
    price_overrides: tuple[PriceOverride, ...] = Field(default=(), max_length=1_000)
    content: ContentSettings = ContentSettings()
    canon: CanonSource | None = None
    evals: EvalConfig | None = None
    calibration: CalibrationConfig | None = None
    presets: PresetConfig | None = None
    models: ModelCatalog | None = None


class NuraMeshConfig(BaseModel):
    """A valid configuration (see the module docstring)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    org: OrgGraph
    policies: PolicySet = PolicySet()
    budgets: tuple[Budget, ...] = Field(default=(), max_length=10_000)
    breakers: BreakersConfig = BreakersConfig()
    prices: PriceBook = PriceBook()
    price_overrides: tuple[PriceOverride, ...] = Field(default=(), max_length=1_000)
    content: ContentSettings = ContentSettings()
    canon: CanonSource | None = None
    evals: EvalConfig | None = None
    calibration: CalibrationConfig | None = None
    presets: PresetConfig | None = None
    models: ModelCatalog | None = None

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        problems = (
            shipped_source_problems(self.prices)
            + duplicate_problems(self.budgets, self.price_overrides)
            + org_reference_problems(
                self.org, self.policies, self.budgets, self.evals, self.presets
            )
            + breaker_problems(
                [(r.id, r.role) for r in self.breakers.rules], {r.id for r in self.org.roles}
            )
        )
        if self.models is not None:
            problems += model_catalog_problems(
                self.org, self.models, self.prices, self.price_overrides
            )
        if problems:
            raise ValueError("; ".join(f"{p.path}: {p.message}" for p in problems))
        return self

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """A changed copy, re-validated (a plain pydantic copy would skip every cross-check).

        `deep` is accepted for compatibility and ignored: the models are immutable.
        """
        data: dict[str, Any] = dict(self)
        if update:
            data.update(update)
        return type(self).model_validate(data)

    @property
    def effective_policies(self) -> PolicySet:
        """What the engine enforces: each role's presets' rules, then its own (`core.presets`).
        Always use this, never `policies`, to decide what an agent may do."""
        return resolve_policies(self.policies, self.presets)


def shipped_source_problems(prices: PriceBook) -> list[ConfigIssue]:
    """A configured price may not call itself the shipped list (origin comes from provenance)."""
    return [
        ConfigIssue(path=f"$.prices[{i}].source", message=msg)
        for i, p in enumerate(prices.prices)
        if (msg := operator_source_problem(p.source))
    ]


def duplicate_problems(
    budgets: tuple[Budget, ...], overrides: tuple[PriceOverride, ...]
) -> list[ConfigIssue]:
    """Duplicate budget ids and duplicate overrides (these need no org)."""
    issues: list[ConfigIssue] = []
    seen: set[str] = set()
    for i, budget in enumerate(budgets):
        if budget.id in seen:
            issues.append(
                ConfigIssue(path=f"$.budgets[{i}].id", message=f"duplicate budget id '{budget.id}'")
            )
        seen.add(budget.id)
    overridden: set[tuple[str, str]] = set()
    for i, override in enumerate(overrides):
        key = (override.provider, override.model)
        if key in overridden:
            issues.append(
                ConfigIssue(
                    path=f"$.price_overrides[{i}]",
                    message=f"two overrides for {override.provider}/{override.model}",
                )
            )
        overridden.add(key)
    return issues


def model_catalog_problems(
    org: OrgGraph,
    catalog: ModelCatalog,
    prices: PriceBook,
    overrides: tuple[PriceOverride, ...],
) -> list[ConfigIssue]:
    """The approved-catalog rules (`core.model_catalog`) as configuration issues."""
    return [
        ConfigIssue(path=path, message=message)
        for path, message in catalog_problems(org, catalog, prices, overrides)
    ]


def breaker_problems(
    rules: Sequence[tuple[object, object]], roles: set[str] | None
) -> list[ConfigIssue]:
    """Duplicate rule names, and (with the org's role ids) rules scoped to an unknown role. Works
    on `(id, role)` pairs so it also runs on rules that failed their own checks."""
    issues: list[ConfigIssue] = []
    seen: set[object] = set()
    for i, (name, role) in enumerate(rules):
        if isinstance(name, str):
            if name in seen:
                issues.append(
                    ConfigIssue(
                        path=f"$.breakers.rules[{i}].id",
                        message=f"duplicate breaker rule id '{name[:80]}'",
                    )
                )
            seen.add(name)
        if roles is not None and isinstance(role, str) and role not in roles:
            issues.append(
                ConfigIssue(
                    path=f"$.breakers.rules[{i}].role", message=f"unknown role '{role[:80]}'"
                )
            )
    return issues


def _raw_breaker_pairs(raw: object) -> list[tuple[object, object]]:
    """`(id, role)` of each rule in a raw `breakers` section, whatever else is wrong with it."""
    rules = raw.get("rules") if isinstance(raw, Mapping) else None
    if not isinstance(rules, list):
        return []
    return [(r.get("id"), r.get("role")) if isinstance(r, Mapping) else (None, None) for r in rules]


def org_reference_problems(
    org: OrgGraph,
    policies: PolicySet,
    budgets: tuple[Budget, ...],
    evals: EvalConfig | None = None,
    presets: PresetConfig | None = None,
) -> list[ConfigIssue]:
    """References from the other parts into the org: policy roles, budget agents and team leads,
    preset assignments and the eval suites that gate presets name."""
    issues: list[ConfigIssue] = []
    roles = {r.id for r in org.roles}
    agents = {a.id for a in org.agents}
    for i, policy in enumerate(policies.policies):
        if policy.role not in roles:
            issues.append(
                ConfigIssue(
                    path=f"$.policies.policies[{i}].role",
                    message=f"policy for unknown role '{policy.role}'",
                )
            )
    for i, budget in enumerate(budgets):
        if budget.scope is BudgetScope.ORG and budget.scope_id != org.id:
            issues.append(
                ConfigIssue(
                    path=f"$.budgets[{i}].scope_id",
                    message=(
                        f"an org budget must name this org ('{org.id}'), not '{budget.scope_id}'"
                    ),
                )
            )
        if budget.scope in (BudgetScope.AGENT, BudgetScope.TEAM) and budget.scope_id not in agents:
            kind = "agent" if budget.scope is BudgetScope.AGENT else "team lead"
            issues.append(
                ConfigIssue(
                    path=f"$.budgets[{i}].scope_id",
                    message=f"{kind} '{budget.scope_id}' isn't in the org",
                )
            )
    if evals is not None:
        for i, suite in enumerate(evals.suites):
            if suite.role not in roles:
                issues.append(
                    ConfigIssue(
                        path=f"$.evals.suites[{i}].role",
                        message=f"suite for unknown role '{suite.role}'",
                    )
                )
        for i, requirement in enumerate(evals.requirements):
            if requirement.role not in roles:
                issues.append(
                    ConfigIssue(
                        path=f"$.evals.requirements[{i}].role",
                        message=f"requirement for unknown role '{requirement.role}'",
                    )
                )
    if presets is not None:
        suite_roles = {s.id: s.role for s in evals.suites} if evals is not None else {}
        suites = set(suite_roles)
        for i, assignment in enumerate(presets.assignments):
            if assignment.role not in roles:
                issues.append(
                    ConfigIssue(
                        path=f"$.presets.assignments[{i}].role",
                        message=f"presets assigned to unknown role '{assignment.role}'",
                    )
                )
        for i, gate_preset in enumerate(presets.gate):
            for suite_id in gate_preset.evals:
                if suite_id not in suites:
                    issues.append(
                        ConfigIssue(
                            path=f"$.presets.gate[{i}].evals",
                            message=f"gate preset '{gate_preset.id}' names eval suite "
                            f"'{suite_id}', which the configuration doesn't define",
                        )
                    )
                    continue
                suite_role = suite_roles[suite_id]
                for assignment in presets.assignments:
                    if assignment.gate_preset == gate_preset.id and assignment.role != suite_role:
                        issues.append(
                            ConfigIssue(
                                path=f"$.presets.gate[{i}].evals",
                                message=f"gate preset '{gate_preset.id}' names eval suite "
                                f"'{suite_id}' for role '{suite_role}', but role "
                                f"'{assignment.role}' uses the preset",
                            )
                        )
        try:
            resolve_policies(policies, presets)
        except ValueError as exc:
            issues.append(ConfigIssue(path="$.presets", message=str(exc)))
    return issues


def _path(location: tuple[Any, ...], root: str = "$") -> str:
    out = root
    for part in location:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


_ROLE_CAP = re.compile(r"^roles\.(\d+)\.max_tokens:")


def _role_cap_path(message: str) -> str | None:
    """`$.org.roles[i].max_tokens` for a malformed role cap (R1.42), else None."""
    found = _ROLE_CAP.match(message)
    return f"$.org.roles[{found.group(1)}].max_tokens" if found else None


def _org_path(org: Any, subject: str | None) -> str:
    """Best-effort location of an org issue's subject (a role or agent id) in the input."""
    if subject and isinstance(org, Mapping):
        for key in ("agents", "roles"):
            items = org.get(key)
            if isinstance(items, list):
                for index, item in enumerate(items):
                    if isinstance(item, Mapping) and item.get("id") == subject:
                        return f"$.org.{key}[{index}]"
    return "$.org"


INTEGRATIONS_NOT_ACCEPTED: Final = "integrations are not accepted yet: "
_PACK_NAMES: Final = ("github", "issues", "helpdesk", "crm")


def _integrations_message(data: Mapping[str, Any]) -> str:
    """The fail-closed answer for an `integrations` section: it names each integration."""
    entries = data.get("integrations")
    names: list[str] = []
    if isinstance(entries, list):
        for item in entries:
            pack = item.get("pack") if isinstance(item, Mapping) else None
            if isinstance(pack, str):
                if pack in _PACK_NAMES:  # never echo a string the client chose
                    names.append(pack)
    shown = ", ".join(f"'{n}'" for n in names[:10]) or "the named integrations"
    return (
        f"{INTEGRATIONS_NOT_ACCEPTED}{shown} cannot be used because the call path is not "
        "built (it needs R2.04 and the E4 gateway); remove the integrations section"
    )


def parse_config(data: Mapping[str, Any], *, shipped: Iterable[Price] = ()) -> NuraMeshConfig:
    """Check a configuration mapping; raise `ConfigValidationError` with every problem.

    `shipped` are the shipped default prices (spec default-prices 5); core does no I/O, so the
    caller reads the package file. A model with a shipped price needs no price of the
    organization's own to be approved."""
    if not isinstance(data, Mapping):
        raise ConfigValidationError(
            [ConfigIssue(path="$", message="the configuration must be a mapping")]
        )
    issues: list[ConfigIssue] = []
    for key in data:
        if key == "integrations":
            issues.append(ConfigIssue(path="$.integrations", message=_integrations_message(data)))
        elif key not in TOP_LEVEL_KEYS:
            shown = str(key)[:80]
            issues.append(
                ConfigIssue(
                    path=f"$.{shown}", message=f"unknown key (allowed: {', '.join(TOP_LEVEL_KEYS)})"
                )
            )
    org: OrgGraph | None = None
    if "org" not in data:
        issues.append(ConfigIssue(path="$.org", message="the configuration needs an org"))
    else:
        try:
            org = OrgGraph.build(data["org"])
        except OrgValidationError as exc:
            for issue in exc.issues:
                cap_path = _role_cap_path(issue.message)
                issues.append(
                    ConfigIssue(
                        path=cap_path or _org_path(data["org"], issue.subject),
                        message=f"[{issue.code}] {issue.message}",
                    )
                )
        except (ValidationError, TypeError, AttributeError) as exc:
            issues.append(ConfigIssue(path="$.org", message=f"malformed: {type(exc).__name__}"))
    rest = {k: v for k, v in data.items() if k in TOP_LEVEL_KEYS and k != "org"}
    parts: _Parts | None = None
    try:
        parts = _Parts.model_validate(rest)
    except ValidationError as exc:
        for err in exc.errors():
            issues.append(ConfigIssue(path=_path(err["loc"]), message=err["msg"]))
    rows = tuple(shipped)
    book = parts.prices.with_shipped(rows) if parts is not None and rows else None
    if parts is not None:
        issues += shipped_source_problems(parts.prices)
        issues += duplicate_problems(parts.budgets, parts.price_overrides)
        if org is not None:
            issues += org_reference_problems(
                org, parts.policies, parts.budgets, parts.evals, parts.presets
            )
            if parts.models is not None:
                issues += model_catalog_problems(
                    org, parts.models, book or parts.prices, parts.price_overrides
                )
    # breaker names and role scopes are checked on the raw section, so a rule that failed its own
    # checks still reports a duplicate name (one validator, spec/e19 section 7)
    issues += breaker_problems(
        _raw_breaker_pairs(rest.get("breakers")), {r.id for r in org.roles} if org else None
    )
    # With an invalid org, references into it can't be checked; the org's own problems come first.
    if issues:
        raise ConfigValidationError(issues)
    assert org is not None and parts is not None
    return NuraMeshConfig(
        version=parts.version,
        org=org,
        policies=parts.policies,
        budgets=parts.budgets,
        breakers=parts.breakers,
        prices=book or parts.prices,
        price_overrides=parts.price_overrides,
        content=parts.content,
        canon=parts.canon,
        evals=parts.evals,
        calibration=parts.calibration,
        presets=parts.presets,
        models=parts.models,
    )


def config_json_schema() -> dict[str, Any]:
    """The JSON Schema of a configuration, generated from the models (shape rules only)."""
    schema: dict[str, Any] = NuraMeshConfig.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "NuraMesh configuration"
    return schema
