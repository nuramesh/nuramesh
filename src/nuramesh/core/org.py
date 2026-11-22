"""Organization graph: roles, agents and reporting lines.

An organization is a tree of agents. Exactly one agent is the root and holds the Chief of Staff
role; every other agent reports to exactly one manager. Work is delegated only downwards.

There are two separate lines between agents (T39). **Reports to** is the management line: who
delegates to whom. **Checks** is the verification line: which agents' work a verifier agent checks.
They are different fields on purpose, and a verifier may never sit in the reporting chain of an
agent it checks: it isn't the author, anyone above the author, or anyone who answers to the author
(peers and other branches are fine), because a checker tied to the author by a reporting line isn't
independent.

`validate_org` returns every problem at once (the builder shows them all); `OrgGraph` refuses to
exist while any error remains, and `OrgGraph.build` raises `OrgValidationError` with every issue,
including malformed fields. Rules are mirrored by fixtures in `spec/org-validation/`, which the
TypeScript builder runs too.
"""

from collections import deque
from collections.abc import Mapping
from enum import StrEnum
from functools import cached_property
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from nuramesh.core.canonical import content_hash

_MAX_TOKENS = 10**12
"""Same value as `core.prices.MAX_TOKENS` (a test pins it; importing it here would be circular)."""

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$", strict=True)]
"""Stable identifier: lower-case letters, digits, `-` and `_`; 1–63 characters."""

STARTUP_STREAM_ID = "store-startup"
"""The id of the ledger stream `org:store-startup`, where a store records facts about opening
itself (ND326). No organization may have this id."""

RESERVED_ORG_IDS = frozenset({STARTUP_STREAM_ID})
"""Organization ids that name system streams; an organization with one is refused."""


def _not_reserved(value: str) -> str:
    if value in RESERVED_ORG_IDS:
        raise ValueError(f"'{value}' is reserved for a system ledger stream")
    return value


EndpointId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$", strict=True)]
"""Names one connection of the `openai-compatible` provider (`local`, `groq`); never a provider."""

OPENAI_COMPATIBLE = "openai-compatible"
"""The only provider id that carries an endpoint id (spec/openai-compatible 2.1)."""

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class RoleKind(StrEnum):
    """What a role is for. Exactly one agent, the root, has the Chief of Staff role."""

    CHIEF_OF_STAFF = "chief_of_staff"
    WORKER = "worker"
    VERIFIER = "verifier"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)


class Role(_Frozen):
    """A job description shared by one or more agents."""

    id: Slug
    name: Name
    kind: RoleKind = RoleKind.WORKER
    description: str = Field(default="", max_length=2_000)
    instructions: str = Field(default="", max_length=50_000)
    tools: tuple[Slug, ...] = ()
    default_model: str | None = Field(default=None, min_length=1, max_length=200)
    max_tokens: int | None = Field(default=None, strict=True, ge=1, le=_MAX_TOKENS)
    """R1.42: the most output tokens one model call of this role may ask for (absent: no cap)."""


class Agent(_Frozen):
    """One member of the organization. `reports_to` is its manager's id; `None` for the root.
    `checks` lists the agents whose work this agent verifies (the verification line: only agents
    whose role is a verifier may check, and never inside their own reporting chain)."""

    id: Slug
    name: Name
    role: Slug
    reports_to: Slug | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    checks: tuple[Slug, ...] = Field(default=(), max_length=200)


class Severity(StrEnum):
    """Errors make an org invalid; warnings are shown but allowed."""

    ERROR = "error"
    WARNING = "warning"


class IssueCode(StrEnum):
    """Stable codes for validation problems (shared with the builder)."""

    DUPLICATE_ROLE_ID = "duplicate_role_id"
    DUPLICATE_AGENT_ID = "duplicate_agent_id"
    UNKNOWN_ROLE = "unknown_role"
    UNKNOWN_MANAGER = "unknown_manager"
    SELF_REPORT = "self_report"
    NO_ROOT = "no_root"
    MULTIPLE_ROOTS = "multiple_roots"
    ROOT_NOT_CHIEF_OF_STAFF = "root_not_chief_of_staff"
    CHIEF_OF_STAFF_NOT_ROOT = "chief_of_staff_not_root"
    UNKNOWN_CHECKED = "unknown_checked"
    SELF_CHECK = "self_check"
    DUPLICATE_CHECK = "duplicate_check"
    CHECKER_NOT_VERIFIER = "checker_not_verifier"
    VERIFIER_IN_CHAIN = "verifier_in_chain"
    CYCLE = "cycle"
    UNREACHABLE = "unreachable"
    UNUSED_ROLE = "unused_role"
    INVALID_FIELD = "invalid_field"


class OrgIssue(_Frozen):
    """One validation problem. `subject` is the agent or role id it is about, if any."""

    code: IssueCode
    severity: Severity
    message: str
    subject: str | None = None


def _error(code: IssueCode, message: str, subject: str | None = None) -> OrgIssue:
    return OrgIssue(code=code, severity=Severity.ERROR, message=message, subject=subject)


def validate_org(roles: tuple[Role, ...], agents: tuple[Agent, ...]) -> list[OrgIssue]:
    """Return every problem in an organization, errors before warnings. Linear time.

    Order within each severity is not part of the contract (fixtures compare as multisets).
    """
    issues: list[OrgIssue] = []

    role_by_id: dict[str, Role] = {}
    for item in roles:
        if item.id in role_by_id:
            issues.append(
                _error(IssueCode.DUPLICATE_ROLE_ID, f"Role id '{item.id}' is used twice", item.id)
            )
        else:
            role_by_id[item.id] = item

    agent_by_id: dict[str, Agent] = {}
    for agent in agents:
        if agent.id in agent_by_id:
            issues.append(
                _error(
                    IssueCode.DUPLICATE_AGENT_ID, f"Agent id '{agent.id}' is used twice", agent.id
                )
            )
        else:
            agent_by_id[agent.id] = agent

    roots: list[Agent] = []
    manager: dict[str, str] = {}
    for agent in agent_by_id.values():
        role = role_by_id.get(agent.role)
        if role is None:
            issues.append(
                _error(
                    IssueCode.UNKNOWN_ROLE,
                    f"Agent '{agent.id}' has role '{agent.role}', which doesn't exist",
                    agent.id,
                )
            )
        if agent.reports_to is None:
            roots.append(agent)
        elif agent.reports_to == agent.id:
            issues.append(
                _error(IssueCode.SELF_REPORT, f"Agent '{agent.id}' reports to itself", agent.id)
            )
        elif agent.reports_to not in agent_by_id:
            issues.append(
                _error(
                    IssueCode.UNKNOWN_MANAGER,
                    f"Agent '{agent.id}' reports to '{agent.reports_to}', which doesn't exist",
                    agent.id,
                )
            )
        else:
            manager[agent.id] = agent.reports_to
        if (
            role is not None
            and role.kind is RoleKind.CHIEF_OF_STAFF
            and agent.reports_to is not None
        ):
            issues.append(
                _error(
                    IssueCode.CHIEF_OF_STAFF_NOT_ROOT,
                    f"Agent '{agent.id}' has the Chief of Staff role but reports to someone",
                    agent.id,
                )
            )

    if not roots:
        issues.append(
            _error(IssueCode.NO_ROOT, "No agent is at the top (one must report to no one)")
        )
    elif len(roots) > 1:
        names = ", ".join(f"'{a.id}'" for a in roots)
        for root in roots:
            issues.append(
                _error(
                    IssueCode.MULTIPLE_ROOTS,
                    f"Only one agent may be at the top; found {names}",
                    root.id,
                )
            )
    for root in roots:
        role = role_by_id.get(root.role)
        if role is not None and role.kind is not RoleKind.CHIEF_OF_STAFF:
            issues.append(
                _error(
                    IssueCode.ROOT_NOT_CHIEF_OF_STAFF,
                    f"The top agent '{root.id}' must have a Chief of Staff role",
                    root.id,
                )
            )

    issues.extend(_cycle_issues(manager, agent_by_id))
    issues.extend(_check_issues(agent_by_id, role_by_id, manager))

    used_roles = {agent.role for agent in agent_by_id.values()}
    for role_id in role_by_id:
        if role_id not in used_roles:
            issues.append(
                OrgIssue(
                    code=IssueCode.UNUSED_ROLE,
                    severity=Severity.WARNING,
                    message=f"Role '{role_id}' has no agents",
                    subject=role_id,
                )
            )

    issues.sort(key=lambda i: (i.severity is not Severity.ERROR,))
    return issues


def _chain(agent_id: str, manager: Mapping[str, str]) -> list[str]:
    """The agent and its managers upwards; stops on a loop (loops are reported separately)."""
    seen: list[str] = []
    node: str | None = agent_id
    while node is not None and node not in seen:
        seen.append(node)
        node = manager.get(node)
    return seen


def _check_issues(
    agent_by_id: Mapping[str, Agent], role_by_id: Mapping[str, Role], manager: Mapping[str, str]
) -> list[OrgIssue]:
    """Problems on the verification line (`Agent.checks`)."""
    issues: list[OrgIssue] = []
    for agent in agent_by_id.values():
        if not agent.checks:
            continue
        role = role_by_id.get(agent.role)
        if role is not None and role.kind is not RoleKind.VERIFIER:
            issues.append(
                _error(
                    IssueCode.CHECKER_NOT_VERIFIER,
                    f"Agent '{agent.id}' checks others but its role '{role.id}' isn't a verifier",
                    agent.id,
                )
            )
        own_chain = set(_chain(agent.id, manager))
        listed: set[str] = set()
        for checked_id in agent.checks:
            if checked_id in listed:
                issues.append(
                    _error(
                        IssueCode.DUPLICATE_CHECK,
                        f"Agent '{agent.id}' lists '{checked_id}' twice in checks",
                        agent.id,
                    )
                )
                continue
            listed.add(checked_id)
            if checked_id == agent.id:
                issues.append(
                    _error(IssueCode.SELF_CHECK, f"Agent '{agent.id}' checks itself", agent.id)
                )
            elif checked_id not in agent_by_id:
                issues.append(
                    _error(
                        IssueCode.UNKNOWN_CHECKED,
                        f"Agent '{agent.id}' checks '{checked_id}', which doesn't exist",
                        agent.id,
                    )
                )
            elif agent.id in _chain(checked_id, manager) or checked_id in own_chain:
                # the verifier is the author or above the author, or answers to the author
                issues.append(
                    _error(
                        IssueCode.VERIFIER_IN_CHAIN,
                        f"Verifier '{agent.id}' is in the reporting chain of '{checked_id}', "
                        "which it checks: a checker must be independent of the work",
                        agent.id,
                    )
                )
    return issues


def _cycle_issues(manager: Mapping[str, str], agent_by_id: Mapping[str, Agent]) -> list[OrgIssue]:
    """Agents on a reporting cycle, and agents whose chain leads into one (never reach the top)."""
    issues: list[OrgIssue] = []
    state: dict[str, int] = {}  # 1 = on the current walk, 2 = finished
    bad: set[str] = set()
    for start in agent_by_id:
        if start in state:
            continue
        walk: list[str] = []
        node: str | None = start
        while node is not None and node not in state:
            state[node] = 1
            walk.append(node)
            node = manager.get(node)
        if node is not None and state[node] == 1:
            cycle = walk[walk.index(node) :]
            members = " → ".join([*cycle, cycle[0]])
            for member in cycle:
                issues.append(
                    _error(IssueCode.CYCLE, f"Reporting lines form a loop: {members}", member)
                )
                bad.add(member)
            leading = walk[: walk.index(node)]
        else:
            leading = walk if node is not None and node in bad else []
        for agent_id in leading:
            bad.add(agent_id)
            issues.append(
                _error(
                    IssueCode.UNREACHABLE,
                    f"Agent '{agent_id}' reports into a loop and never reaches the top",
                    agent_id,
                )
            )
        for agent_id in walk:
            state[agent_id] = 2
    return issues


class OrgValidationError(ValueError):
    """Raised when an organization has errors; `issues` lists every one."""

    def __init__(self, issues: list[OrgIssue]) -> None:
        self.issues = issues
        lines = "\n".join(f"- [{i.code}] {i.message}" for i in issues)
        super().__init__(f"Organization is invalid:\n{lines}")


def _field_issues(data: Mapping[str, Any]) -> list[OrgIssue]:
    """Malformed fields, one issue per pydantic error; subject is the role or agent id if known."""
    issues: list[OrgIssue] = []
    try:
        OrgFields.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = err["loc"]
            subject: str | None = None
            if len(loc) >= 2 and loc[0] in ("roles", "agents") and isinstance(loc[1], int):
                items = data.get(str(loc[0]))
                if isinstance(items, list | tuple) and loc[1] < len(items):
                    item = items[loc[1]]
                    raw_id = item.get("id") if isinstance(item, Mapping) else None
                    subject = raw_id if isinstance(raw_id, str) else None
            path = ".".join(str(part) for part in loc) or "(org)"
            issues.append(_error(IssueCode.INVALID_FIELD, f"{path}: {err['msg']}", subject))
    return issues


class OrgFields(_Frozen):
    """Field rules only, without graph rules (used to report malformed input)."""

    id: Slug
    name: Name
    roles: tuple[Role, ...] = Field(min_length=1)
    agents: tuple[Agent, ...] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id_not_reserved(cls, value: str) -> str:
        return _not_reserved(value)


class OrgGraph(_Frozen):
    """A valid organization: it can't exist while any error remains.

    Use `OrgGraph.build(data)` to get an `OrgValidationError` listing every issue. Direct
    construction or `model_validate` raise pydantic's `ValidationError` instead. `model_copy`
    re-validates; never use `model_construct`, which skips validation.
    """

    id: Slug
    name: Name
    roles: tuple[Role, ...] = Field(min_length=1)
    agents: tuple[Agent, ...] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id_not_reserved(cls, value: str) -> str:
        return _not_reserved(value)

    @model_validator(mode="after")
    def _check(self) -> Self:
        errors = [i for i in validate_org(self.roles, self.agents) if i.severity is Severity.ERROR]
        if errors:
            raise OrgValidationError(errors)
        return self

    @classmethod
    def build(cls, data: Mapping[str, Any]) -> Self:
        """Validate `data` and return the org, or raise `OrgValidationError` with every issue.

        Malformed fields are reported as `invalid_field`; graph rules run once fields are valid.
        """
        field_issues = _field_issues(data)
        if field_issues:
            raise OrgValidationError(field_issues)
        shape = OrgFields.model_validate(data)
        errors = [
            i for i in validate_org(shape.roles, shape.agents) if i.severity is Severity.ERROR
        ]
        if errors:
            raise OrgValidationError(errors)
        return cls.model_validate(data)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """A changed copy, re-validated (a plain pydantic copy would skip validation).

        `deep` is accepted for compatibility and ignored: the models are immutable.
        """
        data: dict[str, Any] = dict(self)
        if update:
            data.update(update)
        return type(self).model_validate(data)

    @cached_property
    def _agents(self) -> dict[str, Agent]:
        return {agent.id: agent for agent in self.agents}

    @cached_property
    def _roles(self) -> dict[str, Role]:
        return {role.id: role for role in self.roles}

    @cached_property
    def _reports(self) -> dict[str, tuple[str, ...]]:
        children: dict[str, list[str]] = {agent.id: [] for agent in self.agents}
        for agent in self.agents:
            if agent.reports_to is not None:
                children[agent.reports_to].append(agent.id)
        return {key: tuple(sorted(value)) for key, value in children.items()}

    @property
    def warnings(self) -> list[OrgIssue]:
        """Non-blocking issues, such as roles nobody holds."""
        return validate_org(self.roles, self.agents)

    @property
    def chief_of_staff(self) -> Agent:
        """The root agent."""
        return next(agent for agent in self.agents if agent.reports_to is None)

    def agent(self, agent_id: str) -> Agent:
        """The agent with this id; `KeyError` if there is none."""
        return self._agents[agent_id]

    def role(self, role_id: str) -> Role:
        """The role with this id; `KeyError` if there is none."""
        return self._roles[role_id]

    def role_of(self, agent_id: str) -> Role:
        """The role held by an agent."""
        return self._roles[self.agent(agent_id).role]

    def model_for(self, agent_id: str) -> str | None:
        """The agent's model, falling back to its role's default."""
        agent = self.agent(agent_id)
        return agent.model if agent.model is not None else self.role_of(agent_id).default_model

    def checkers_of(self, agent_id: str) -> tuple[str, ...]:
        """Ids of the verifier agents that check this agent's work (the verification line)."""
        self.agent(agent_id)
        return tuple(sorted(a.id for a in self.agents if agent_id in a.checks))

    def direct_reports(self, agent_id: str) -> tuple[str, ...]:
        """Ids of agents reporting directly to this one, sorted."""
        self.agent(agent_id)
        return self._reports[agent_id]

    def subordinates(self, agent_id: str) -> tuple[str, ...]:
        """Every agent below this one (breadth first). Its team, for team budgets."""
        found: list[str] = []
        queue = deque(self.direct_reports(agent_id))
        while queue:
            current = queue.popleft()
            found.append(current)
            queue.extend(self._reports[current])
        return tuple(found)

    def chain_of_command(self, agent_id: str) -> tuple[str, ...]:
        """This agent and its managers up to the Chief of Staff."""
        chain = [agent_id]
        current = self.agent(agent_id)
        while current.reports_to is not None:
            chain.append(current.reports_to)
            current = self._agents[current.reports_to]
        return tuple(chain)

    def can_delegate(self, from_agent: str, to_agent: str) -> bool:
        """Work may only go down the graph: `to_agent` must be below `from_agent`."""
        if from_agent == to_agent:
            return False
        return from_agent in self.chain_of_command(to_agent)[1:]

    def version_hash(self) -> str:
        """SHA-256 of the organization's content.

        Reordering roles or agents doesn't change it, and neither does adding a new optional field
        later (fields at their default values are left out).
        """
        data = self.model_dump(mode="json", exclude_defaults=True)
        data["roles"] = sorted(data["roles"], key=lambda r: r["id"])
        data["agents"] = sorted(data["agents"], key=lambda a: a["id"])
        return content_hash(data)
