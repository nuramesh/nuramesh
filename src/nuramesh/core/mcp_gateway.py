"""The governed MCP gateway decision pipeline (#101, #910).

Pure (stdlib and pydantic): no socket, no process, no MCP SDK. Every tool call passes, in order:
identity, run state, names, allow-list, arguments, transport kind, listing, the pin, policy
(default deny, `ask` for writes), the budget pre-check, a scoped credential, the call, and ends in
exactly one final `tool.called` record. Everything outside this module is a port; the engine binds
the ports to the stores, the approvals engine, the budgets, the secret store and the ledger.

Remote MCP servers (`streamable_http`) are a named seam only: G-Q2 forbids
them in R2, so a registration with a remote transport is refused and there is no switch.

Records follow ND190: the server and tool names are readable; anything derived from the arguments
or the result is a keyed hash. No record holds an argument, a result, a description or a
credential, and no exception text of a port reaches a record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from nuramesh.core.canonical import canonical_json
from nuramesh.core.integrations import HASH_PATTERN, SERVER_PATTERN, TOOL_PATTERN, Effect
from nuramesh.core.mcp_pins import (
    DefinitionUnavailable,
    Finding,
    PinConflict,
    PinDecisionError,
    PinStore,
    PinVerdict,
    ToolPin,
    approved_pin,
    check_pin,
    definition_hash,
    diff_definitions,
    first_seen_pin,
    name_shadows,
    rejected_pin,
    scan_definition,
    seeded_pin,
    with_candidate,
)
from nuramesh.core.money import Money
from nuramesh.core.policy import (
    MAX_TARGET,
    POLICY_CONTRACT,
    Action,
    ActionKind,
    Decision,
    PolicySet,
    evaluate,
)
from nuramesh.core.secret_store import ERROR_CODES
from nuramesh.core.secrets_text import Redacted

LOCAL_TRANSPORT_KINDS: Final = frozenset({"in_process", "stdio"})
REMOTE_TRANSPORT_KINDS: Final = frozenset({"streamable_http"})
"""The remote seam: named so the transport PR has one place to open, refused until the owner
reverses ND188 G-Q2."""
MAX_ARGUMENT_BYTES: Final = 262_144
MAX_RESULT_BYTES: Final = 1_048_576
MAX_LISTED_TOOLS: Final = 1_000
RUN_REASONS: Final = ("org_halted", "role_paused", "not_running")
BUDGET_REASONS: Final = ("budget_exceeded", "no_budget")
EVENT_TOOL_CALLED: Final = "tool.called"
EVENT_POLICY_DECIDED: Final = "policy.decided"
EVENT_APPROVAL_REQUESTED: Final = "approval.requested"
EVENT_TOOL_CHANGED: Final = "mcp.tool_changed"
EVENT_TOOL_PINNED: Final = "mcp.tool_pinned"
TOOL_CALLED_KEYS: Final = frozenset(
    {
        "call",
        "outcome",
        "agent",
        "role",
        "server",
        "tool",
        "effects",
        "definition_hash",
        "reason",
        "approval",
        "approvals",
        "args_hash",
        "binding",
        "result_hash",
        "hash_scheme",
        "hash_key_id",
        "hash_org",
        "error_class",
        "duration_ms",
        "credential",
        "budget_hold",
        "hold_open",
    }
)
"""Every key a `tool.called` payload may hold (rule 31)."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ServerRegistration(_Frozen):
    """One allowed MCP server of the organization."""

    id: str
    transport: str
    credential: bool = False
    """Whether calls carry a scoped credential from the secret store."""

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SERVER_PATTERN.fullmatch(value):
            raise ValueError("server id is not a slug")
        return value

    @field_validator("transport")
    @classmethod
    def _local(cls, value: str) -> str:
        if value in REMOTE_TRANSPORT_KINDS:
            raise ValueError("remote MCP servers are not allowed (ND188 G-Q2)")
        if value not in LOCAL_TRANSPORT_KINDS:
            raise ValueError("unknown transport kind")
        return value


class ToolRegistration(_Frozen):
    """One tool the organization lets agents ask for. The registration, never the server,
    classifies it (`kind`, `effects`)."""

    server: str
    tool: str
    kind: Literal["read", "write"]
    allowed_args: tuple[str, ...] = Field(max_length=64)
    effects: tuple[Effect, ...] = Field(default=(), max_length=8)
    paid: bool = False
    definition_hash: str | None = None
    """A hash pinned in reviewed configuration (rule 15)."""

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if not SERVER_PATTERN.fullmatch(self.server):
            raise ValueError("server is not a slug")
        if not TOOL_PATTERN.fullmatch(self.tool):
            raise ValueError("tool name does not match the tool pattern")
        if len(set(self.allowed_args)) != len(self.allowed_args):
            raise ValueError("allowed_args repeats a name")
        if any(not 1 <= len(a) <= 128 for a in self.allowed_args):
            raise ValueError("an argument name has 1 to 128 characters")
        if (self.kind == "write") != bool(self.effects):
            raise ValueError("a write tool has at least one effect and a read tool none")
        args = [e.arg for e in self.effects]
        if len(set(args)) != len(args) or not set(args) <= set(self.allowed_args):
            raise ValueError("an effect arg must be a distinct member of allowed_args")
        if self.definition_hash is not None and not HASH_PATTERN.fullmatch(self.definition_hash):
            raise ValueError("definition_hash is not 64 lowercase hex")
        return self


class GatewayConfig(_Frozen):
    """The allow-list of ONE organization: its servers and their registered tools."""

    org_id: str = Field(min_length=1, max_length=200)
    servers: tuple[ServerRegistration, ...] = Field(default=(), max_length=100)
    tools: tuple[ToolRegistration, ...] = Field(default=(), max_length=1_000)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [s.id for s in self.servers]
        if len(set(ids)) != len(ids):
            raise ValueError("a server id is repeated")
        keys = [(t.server, t.tool) for t in self.tools]
        if len(set(keys)) != len(keys):
            raise ValueError("a tool is registered twice")
        if any(t.server not in ids for t in self.tools):
            raise ValueError("a tool names a server that is not registered")
        return self

    def server(self, server_id: str) -> ServerRegistration | None:
        """The registration of a server, or None."""
        return next((s for s in self.servers if s.id == server_id), None)

    def tool(self, server_id: str, tool: str) -> ToolRegistration | None:
        """The registration of a tool, or None."""
        return next((t for t in self.tools if (t.server, t.tool) == (server_id, tool)), None)

    def tools_of(self, server_id: str) -> list[ToolRegistration]:
        """The registered tools of a server, by name."""
        return sorted((t for t in self.tools if t.server == server_id), key=lambda t: t.tool)


class CallerIdentity(_Frozen):
    """Who asks: built by the engine from the authenticated agent, never from a request body."""

    org_id: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)


class GatewayRecord(_Frozen):
    """One ledger entry to append (the engine writes it to the run or organization stream)."""

    type: str
    actor: str
    run_id: str | None = None
    task_id: str | None = None
    payload: Mapping[str, Any]
    cost: Money | None = None
    cost_basis: str | None = None


class GatewayResult(_Frozen):
    """What the caller gets. `result` is the server's output: untrusted data, never instructions."""

    outcome: Literal["ok", "error", "blocked"]
    reason: str | None = None
    call_id: str
    approval: str | None = None
    result: Mapping[str, Any] | None = None
    untrusted: Literal[True] = True


class BudgetHold(_Frozen):
    """The budget port's answer to the pre-check."""

    ok: bool
    reason: str | None = None
    hold_id: str | None = None


class CredentialUnavailable(Exception):
    """The credential port could not give a credential. The code is one of the secret store's
    fixed codes; raise it outside an `except` block so no cause rides along."""

    def __init__(self, code: str) -> None:
        self.code = code if code in ERROR_CODES else "unavailable"
        super().__init__("the tool credential is not available")


class ToolTransport(Protocol):
    """A session with ONE server. `transport_kind` is `in_process`, `stdio` or (seam only)
    `streamable_http`."""

    transport_kind: str

    def list_tools(self) -> Sequence[Mapping[str, Any]]: ...

    def call_tool(
        self, tool: str, arguments: Mapping[str, Any], credential: Redacted | None
    ) -> Any: ...


class ApprovalPort(Protocol):
    """Human approvals. An approval answers one binding (one exact call) and is single use."""

    def find_approved(self, identity: CallerIdentity, binding: str) -> str | None: ...

    def request(self, identity: CallerIdentity, binding: str, actions: Sequence[Action]) -> str: ...

    def consume(self, approval_id: str) -> bool: ...

    def request_pin(self, org_id: str, server: str, tool: str, candidate_hash: str) -> str: ...


class BudgetPort(Protocol):
    """Budgets: check before spending (`amount` None: an unpaid call), settle or release after."""

    def hold(self, identity: CallerIdentity, call_id: str, amount: Money | None) -> BudgetHold: ...

    def settle(self, hold_id: str) -> None: ...

    def release(self, hold_id: str) -> None: ...


class PricePort(Protocol):
    """Versioned tool prices; None means unknown (never guessed)."""

    def price_of(self, server: str, tool: str) -> Money | None: ...


class CredentialPort(Protocol):
    """Scoped credentials from the secret store; `scope` is `read` or `write`."""

    def credential_for(self, org_id: str, server: str, scope: str) -> Redacted: ...


class RunStatePort(Protocol):
    """Kill switch, role pause and task liveness: a refusal reason, or None."""

    def refusal(self, identity: CallerIdentity) -> str | None: ...


class GatewayHasher(Protocol):
    """The organization's keyed hasher (PH-KEY in the engine); the kind is part of the message."""

    def __call__(self, kind: str, data: bytes) -> str: ...

    def marker(self) -> dict[str, str]: ...


@dataclass(frozen=True, kw_only=True)
class GatewayPorts:
    """Everything the pipeline touches outside itself."""

    transports: Mapping[str, ToolTransport]
    pins: PinStore
    policies: PolicySet
    approvals: ApprovalPort
    budget: BudgetPort
    prices: PricePort
    credentials: CredentialPort
    run_state: RunStatePort
    hasher: GatewayHasher
    sink: Callable[[GatewayRecord], None]
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    new_id: Callable[[], str]


class _Refused(Exception):
    """A step refused; carries the reason and what the final record adds."""

    def __init__(self, reason: str, **extra: Any) -> None:
        self.reason = reason
        self.extra = extra
        super().__init__(reason)


class GatewayRecordError(Exception):
    """The final `tool.called` record could not be written (rule 33). Raised, never swallowed: a
    call the ledger cannot show must not look like a normal return. It carries the call id, the
    outcome the call had and the sink error's class name (never its message). A call that already
    reached the transport is not undone and its result is withheld."""

    def __init__(self, call_id: str, outcome: str, error_class: str) -> None:
        self.call_id = call_id
        self.outcome = outcome
        self.error_class = error_class
        super().__init__(f"the final record of call {call_id} could not be written")


def _strictest(decisions: Sequence[Decision]) -> Decision:
    for wanted in (Decision.DENY, Decision.ASK, Decision.ALLOW):
        if wanted in decisions:
            return wanted
    return Decision.DENY


class Gateway:
    """The gateway of one organization over its configuration and ports."""

    def __init__(self, config: GatewayConfig, ports: GatewayPorts) -> None:
        self._config = config
        self._ports = ports

    # --- listing and pins ------------------------------------------------------------------

    def _transport(self, server: ServerRegistration) -> ToolTransport:
        transport = self._ports.transports.get(server.id)
        if transport is None:
            raise _Refused("connection_unavailable", error_class="SessionNotAvailable")
        if getattr(transport, "transport_kind", None) != server.transport:
            raise _Refused("transport_not_allowed")
        return transport

    def _listing(self, transport: ToolTransport) -> Sequence[Any]:
        name: str | None = None
        listing: Any = None
        try:
            listing = transport.list_tools()
        except Exception as error:
            name = type(error).__name__
        if name is not None:
            raise _Refused("connection_unavailable", error_class=name)
        if (
            not isinstance(listing, Sequence)
            or isinstance(listing, str | bytes)
            or len(listing) > MAX_LISTED_TOOLS
        ):
            raise _Refused("definition_unavailable")
        return listing

    @staticmethod
    def _listed(listing: Sequence[Any], tool: str) -> tuple[Mapping[str, Any], str]:
        found = [t for t in listing if isinstance(t, Mapping) and t.get("name") == tool]
        if len(found) != 1:
            raise _Refused("definition_unavailable")
        try:
            return found[0], definition_hash(found[0])
        except DefinitionUnavailable:
            pass
        raise _Refused("definition_unavailable")

    def _emit(
        self,
        type_: str,
        actor: str,
        identity: CallerIdentity | None,
        payload: Mapping[str, Any],
        cost: Money | None = None,
        cost_basis: str | None = None,
    ) -> None:
        self._ports.sink(
            GatewayRecord(
                type=type_,
                actor=actor,
                run_id=None if identity is None else identity.run_id,
                task_id=None if identity is None else identity.task_id,
                payload=dict(payload),
                cost=cost,
                cost_basis=cost_basis,
            )
        )

    def _findings(self, server: str, tool: str, definition: Mapping[str, Any]) -> list[Finding]:
        """The static-check findings (rule 20) plus the name check against every registered tool
        of the organization (the name is never scanned as prose)."""
        found = list(scan_definition(definition))
        if name_shadows(server, tool, [(t.server, t.tool) for t in self._config.tools]):
            found.append(Finding(rule="name_shadow", path="/name"))
        return sorted(found, key=lambda f: (f.rule, f.path))

    def _pin_once(
        self,
        identity: CallerIdentity,
        registration: ToolRegistration,
        definition: Mapping[str, Any],
        listed: str,
    ) -> tuple[str | None, str | None]:
        ports, org = self._ports, self._config.org_id
        server, tool = registration.server, registration.tool
        pin = ports.pins.get(org, server, tool)
        verdict = check_pin(pin, listed)
        if verdict is PinVerdict.MATCH:
            return None, None
        if pin is not None and verdict in (PinVerdict.PENDING, PinVerdict.BLOCKED):
            if pin.approval_id is None and listed == pin.candidate_hash:
                # the pin was saved but its approval never was (rule 16): open it now
                return self._open_approval(identity, pin, definition, listed)
            if verdict is PinVerdict.PENDING:
                return "definition_unpinned", pin.approval_id
            return "definition_blocked", pin.approval_id
        if verdict is PinVerdict.REJECTED:
            return "definition_rejected", None
        findings = self._findings(server, tool, definition)
        if pin is None and registration.definition_hash == listed and not findings:
            ports.pins.save(
                seeded_pin(org, server, tool, definition, ports.clock()), expected_version=None
            )
            self._emit(
                EVENT_TOOL_PINNED,
                "system",
                identity,
                {
                    "server": server,
                    "tool": tool,
                    "decision": "seeded",
                    "approved_hash": listed,
                    "previous_hash": None,
                    "candidate_hash": listed,
                    "findings": [],
                    "approval": None,
                },
            )
            return None, None
        # Rule 16: the pin is saved FIRST (without an approval); only the writer that won the
        # compare-and-set requests the approval, then attaches it. A lost race opens nothing.
        if pin is None:
            saved = ports.pins.save(
                first_seen_pin(org, server, tool, definition), expected_version=None
            )
        else:
            saved = ports.pins.save(with_candidate(pin, definition), expected_version=pin.version)
        return self._open_approval(identity, saved, definition, listed)

    def _open_approval(
        self,
        identity: CallerIdentity,
        pin: ToolPin,
        definition: Mapping[str, Any],
        listed: str,
    ) -> tuple[str | None, str | None]:
        """Request the pin approval for the candidate of a saved pin, attach it (compare-and-set
        on the pin's version) and write `mcp.tool_changed`."""
        ports = self._ports
        server, tool = pin.server, pin.tool
        approval = ports.approvals.request_pin(pin.org_id, server, tool, listed)
        ports.pins.save(
            pin.model_copy(update={"approval_id": approval}), expected_version=pin.version
        )
        approved = pin.approved_definition
        entries, truncated = (
            ((), False) if approved is None else diff_definitions(approved, definition)
        )
        self._emit(
            EVENT_TOOL_CHANGED,
            f"agent:{identity.agent_id}",
            identity,
            {
                "server": server,
                "tool": tool,
                "change": "first_seen" if approved is None else "changed",
                "approved_hash": pin.approved_hash,
                "candidate_hash": listed,
                "diff": [e.model_dump() for e in entries],
                "truncated": truncated,
                "findings": [f.model_dump() for f in self._findings(server, tool, definition)],
                "approval": approval,
            },
        )
        return ("definition_unpinned" if approved is None else "definition_changed"), approval

    def _pin(
        self,
        identity: CallerIdentity,
        registration: ToolRegistration,
        definition: Mapping[str, Any],
        listed: str,
    ) -> tuple[str | None, str | None]:
        """Rules 10 to 16: None when the listed definition is the approved one, else the refusal
        reason and the open pin approval. A lost compare-and-set re-reads once, then fails
        closed."""
        for _attempt in range(2):
            try:
                return self._pin_once(identity, registration, definition, listed)
            except PinConflict:
                continue
        return "definition_blocked", None

    def review_listing(self, identity: CallerIdentity, server: str) -> dict[str, str]:
        """The per-session compare (rule 17): the pin status of every registered tool of the
        server (`ok` or a refusal reason). Calls no tool."""
        registration = self._config.server(server) if isinstance(server, str) else None
        if registration is None or identity.org_id != self._config.org_id:
            return {}
        tools = self._config.tools_of(server)
        try:
            listing = self._listing(self._transport(registration))
        except _Refused as refused:
            return {t.tool: refused.reason for t in tools}
        out: dict[str, str] = {}
        for tool in tools:
            try:
                definition, listed = self._listed(listing, tool.tool)
            except _Refused as refused:
                out[tool.tool] = refused.reason
                continue
            reason, _approval = self._pin(identity, tool, definition, listed)
            out[tool.tool] = reason or "ok"
        return out

    def visible_tools(self, identity: CallerIdentity, server: str) -> list[dict[str, Any]]:
        """The definitions a model may see (rule 18): registered, approved, unchanged and not
        denied by the role's policy. Reads only; changes no pin and writes no record."""
        registration = self._config.server(server) if isinstance(server, str) else None
        if registration is None or identity.org_id != self._config.org_id:
            return []
        try:
            listing = self._listing(self._transport(registration))
        except _Refused:
            return []
        policy = self._ports.policies.policy_for(identity.role)
        out: list[dict[str, Any]] = []
        for tool in self._config.tools_of(server):
            try:
                definition, listed = self._listed(listing, tool.tool)
            except _Refused:
                continue
            pin = self._ports.pins.get(self._config.org_id, server, tool.tool)
            use = Action(kind=ActionKind.USE_TOOL, target=f"mcp:{server}/{tool.tool}")
            if (
                check_pin(pin, listed) is PinVerdict.MATCH
                and evaluate(policy, use).decision is not Decision.DENY
            ):
                out.append(dict(definition))
        return out

    def decide_pin(
        self, *, server: str, tool: str, candidate_hash: str, approve: bool, by: str, reason: str
    ) -> ToolPin:
        """A human's decision on exactly `candidate_hash` (rule 14). Writes `mcp.tool_pinned`."""
        if not isinstance(reason, str) or not reason.strip():
            raise PinDecisionError("a pin decision needs a reason")
        pin = self._ports.pins.get(self._config.org_id, server, tool)
        if pin is None:
            raise PinDecisionError("there is no pin to decide")
        decide = approved_pin if approve else rejected_pin
        decided = decide(pin, candidate_hash, by, self._ports.clock())
        findings = [
            f.model_dump() for f in self._findings(server, tool, pin.candidate_definition or {})
        ]
        saved = self._ports.pins.save(decided, expected_version=pin.version)  # PinConflict: rule 14
        ports = self._ports
        self._emit(
            EVENT_TOOL_PINNED,
            by,
            None,
            {
                "server": server,
                "tool": tool,
                "decision": "approved" if approve else "rejected",
                "approved_hash": saved.approved_hash,
                "previous_hash": pin.approved_hash,
                "candidate_hash": candidate_hash,
                "findings": findings,
                "approval": pin.approval_id,
                "reason_hash": ports.hasher("pin_reason", reason.strip().encode()),
                **ports.hasher.marker(),
            },
        )
        return saved

    # --- the call --------------------------------------------------------------------------

    def call(
        self, identity: CallerIdentity, server: Any, tool: Any, arguments: Any
    ) -> GatewayResult:
        """One governed `tools/call` (spec section 4). Never raises for a refusal: the result and
        the final `tool.called` record say what happened."""
        ports = self._ports
        call_id = ports.new_id()
        actor = f"agent:{identity.agent_id}"
        base: dict[str, Any] = {"call": call_id, "agent": identity.agent_id, "role": identity.role}
        hold_id: str | None = None
        transport: ToolTransport | None = None
        credential: Redacted | None = None
        started = 0.0
        try:
            if identity.org_id != self._config.org_id:
                raise _Refused("identity_mismatch")
            self._run_state(identity)
            if not (
                isinstance(server, str)
                and isinstance(tool, str)
                and SERVER_PATTERN.fullmatch(server)
                and TOOL_PATTERN.fullmatch(tool)
            ):
                raise _Refused("bad_name")
            base.update(server=server, tool=tool)
            server_reg = self._config.server(server)
            if server_reg is None:
                raise _Refused("server_not_allowed")
            registration = self._config.tool(server, tool)
            if registration is None:
                raise _Refused("unmapped")
            base["effects"] = [e.kind for e in registration.effects]
            args_bytes, effect_actions = self._arguments(registration, arguments)
            transport = self._transport(server_reg)
            definition, listed = self._listed(self._listing(transport), tool)
            base["definition_hash"] = listed
            reason, pin_approval = self._pin(identity, registration, definition, listed)
            if reason is not None:
                raise _Refused(
                    reason, **({} if pin_approval is None else {"approval": pin_approval})
                )

            base["args_hash"] = ports.hasher("tool_args", args_bytes)
            base["binding"] = ports.hasher(
                "tool_call",
                canonical_json(
                    {
                        "org": identity.org_id,
                        "agent": identity.agent_id,
                        "task": identity.task_id,
                        "server": server,
                        "tool": tool,
                        "definition_hash": listed,
                        "arguments": args_bytes.decode(),
                    }
                ),
            )
            base.update(ports.hasher.marker())
            approvals = self._policy(identity, actor, base, server, tool, effect_actions)

            price: Money | None = None
            if registration.paid:
                price = ports.prices.price_of(server, tool)
                if price is None:
                    raise _Refused("price_unknown")
            hold = ports.budget.hold(identity, call_id, price)
            if not hold.ok:
                said = hold.reason or ""
                raise _Refused(said if said in BUDGET_REASONS else "budget_exceeded")
            hold_id = hold.hold_id
            if hold_id is not None:
                base["budget_hold"] = hold_id
            credential, scope = self._credential(server_reg, registration)

            started = ports.monotonic()
            self._run_state(identity)
            for approval in approvals:
                if not ports.approvals.consume(approval):
                    raise _Refused("approval_required", approval=approval)
            base.update(approvals=approvals, credential=scope)
            self._emit(EVENT_TOOL_CALLED, actor, identity, {**base, "outcome": "started"})
        except Exception as error:
            # Rule 33: a refusal, or ANY other failure of a step before the transport is called,
            # ends in one blocked record; the hold (if one was taken) is released.
            refused = (
                error
                if isinstance(error, _Refused)
                else _Refused("gateway_error", error_class=type(error).__name__)
            )
            if hold_id is not None:
                try:
                    ports.budget.release(hold_id)
                except Exception:
                    base["hold_open"] = True
            self._final(
                call_id,
                "blocked",
                actor,
                identity,
                {**base, "outcome": "blocked", "reason": refused.reason, **refused.extra},
                cost_basis="unknown",
            )
            return GatewayResult(
                outcome="blocked",
                reason=refused.reason,
                call_id=call_id,
                approval=refused.extra.get("approval"),
            )

        assert transport is not None
        result: Any = None
        error_class: str | None = None
        try:
            result = transport.call_tool(tool, dict(arguments), credential)
        except Exception as error:
            error_class = type(error).__name__
        try:
            duration = max(0, int((ports.monotonic() - started) * 1000))
        except Exception:
            duration = 0
        settle_failed: str | None = None
        if hold_id is not None:
            try:
                ports.budget.settle(hold_id)  # the transport was called: never under-count spend
            except Exception as error:
                settle_failed = type(error).__name__

        final: dict[str, Any] = {**base, "duration_ms": duration}
        problem: str | None = None
        try:
            if error_class is not None:
                problem = "invoke_failed"
                final["error_class"] = error_class
                result = None
            else:
                result_bytes = self._result_bytes(result)
                if result_bytes is None:
                    problem, result = "bad_result", None
                else:
                    final["result_hash"] = ports.hasher("tool_result", result_bytes)
                    if result.get("isError") is True:
                        problem = "tool_error"
        except Exception as error:
            problem, result = "gateway_error", None
            final["error_class"] = type(error).__name__
        if settle_failed is not None:
            # The spend could not be settled: fail closed. The call happened once; the result is
            # withheld and the record says the hold is still open.
            final["hold_open"] = True
            if problem is None:
                problem, final["error_class"] = "settle_failed", settle_failed
            result = None
        if problem is not None:
            final["reason"] = problem
        outcome: Literal["ok", "error"] = "ok" if problem is None else "error"
        self._final(
            call_id,
            outcome,
            actor,
            identity,
            {**final, "outcome": outcome},
            cost=price,
            cost_basis="unknown"
            if price is None
            else ("actual" if problem is None else "provisional"),
        )
        return GatewayResult(
            outcome=outcome,
            reason=problem,
            call_id=call_id,
            result=None if result is None else dict(result),
        )

    def _final(
        self,
        call_id: str,
        outcome: str,
        actor: str,
        identity: CallerIdentity,
        payload: Mapping[str, Any],
        cost: Money | None = None,
        cost_basis: str | None = None,
    ) -> None:
        """Write the one final record; a sink failure is raised as `GatewayRecordError`."""
        try:
            self._emit(EVENT_TOOL_CALLED, actor, identity, payload, cost, cost_basis)
        except Exception as error:
            raise GatewayRecordError(call_id, outcome, type(error).__name__) from None

    def _run_state(self, identity: CallerIdentity) -> None:
        refusal = self._ports.run_state.refusal(identity)
        if refusal is not None:
            raise _Refused(refusal if refusal in RUN_REASONS else "not_running")

    @staticmethod
    def _arguments(registration: ToolRegistration, arguments: Any) -> tuple[bytes, list[Action]]:
        """Rule 7: the canonical argument bytes and the effect actions, or `bad_argument`."""
        actions: list[Action] = []
        ok = isinstance(arguments, Mapping) and all(
            isinstance(k, str) and k in registration.allowed_args for k in arguments
        )
        data = b""
        if ok:
            try:
                data = canonical_json(dict(arguments))
            except (ValueError, TypeError, RecursionError):
                ok = False
        ok = ok and len(data) <= MAX_ARGUMENT_BYTES
        if ok:
            for effect in registration.effects:
                value = arguments.get(effect.arg)
                if not isinstance(value, str) or not 1 <= len(value) <= MAX_TARGET:
                    ok = False
                    break
                try:
                    actions.append(Action(kind=ActionKind(effect.kind), target=value))
                except ValidationError:
                    ok = False
                    break
        if not ok:
            raise _Refused("bad_argument")
        return data, actions

    def _policy(
        self,
        identity: CallerIdentity,
        actor: str,
        base: Mapping[str, Any],
        server: str,
        tool: str,
        effect_actions: Sequence[Action],
    ) -> list[str]:
        """Rules 21 to 23: the approvals that let the call through (empty when plainly allowed),
        or a refusal. Consumes nothing."""
        ports = self._ports
        policy = ports.policies.policy_for(identity.role)
        use = Action(kind=ActionKind.USE_TOOL, target=f"mcp:{server}/{tool}")
        decisions: list[Decision] = []
        asked: list[Action] = []
        for action in (use, *effect_actions):
            verdict = evaluate(policy, action)
            decision = verdict.decision
            if action is not use and policy is not None and verdict.rule is None:
                decision = Decision.ASK  # rule 22: a write the policy does not mention asks
            payload: dict[str, Any] = {
                "call": base["call"],
                "agent": identity.agent_id,
                "role": identity.role,
                "action": action.kind.value,
                "decision": decision.value,
                "rule": verdict.rule,
                "policy_contract": POLICY_CONTRACT,
            }
            if action is use:
                payload["target"] = use.target
            else:
                assert action.target is not None
                payload["target_hash"] = ports.hasher("action_target", action.target.encode())
                payload.update(ports.hasher.marker())
            self._emit(EVENT_POLICY_DECIDED, actor, identity, payload)
            decisions.append(decision)
            if decision is Decision.ASK:
                asked.append(action)
        overall = _strictest(decisions)
        if overall is Decision.DENY:
            raise _Refused("policy_denied")
        if overall is Decision.ALLOW:
            return []
        binding = str(base["binding"])
        approval = ports.approvals.find_approved(identity, binding)
        if approval is not None:
            return [approval]
        approval = ports.approvals.request(identity, binding, asked)
        self._emit(
            EVENT_APPROVAL_REQUESTED,
            actor,
            identity,
            {
                "call": base["call"],
                "approval": approval,
                "agent": identity.agent_id,
                "role": identity.role,
                "server": server,
                "tool": tool,
                "actions": [a.kind.value for a in asked],
                "binding": binding,
                **ports.hasher.marker(),
            },
        )
        raise _Refused("approval_required", approval=approval)

    def _credential(
        self, server: ServerRegistration, registration: ToolRegistration
    ) -> tuple[Redacted | None, str]:
        """Rule 25: the credential of the tool's own scope, or none for a server without one."""
        if not server.credential:
            return None, "none"
        code: str | None = None
        credential: Redacted | None = None
        try:
            credential = self._ports.credentials.credential_for(
                self._config.org_id, server.id, registration.kind
            )
        except CredentialUnavailable as error:
            code = error.code
        except Exception:
            code = "unavailable"
        if code is not None or not isinstance(credential, Redacted):
            raise _Refused("credential_unavailable", error_class=code or "invalid")
        return credential, registration.kind

    @staticmethod
    def _result_bytes(result: Any) -> bytes | None:
        """The canonical bytes of a result mapping within the size limit, else None (rule 28).
        A result holding a float has none: the float writer is reserved by ND78 (spec section 9,
        decision 8)."""
        if not isinstance(result, Mapping):
            return None
        try:
            data = canonical_json(dict(result))
        except (ValueError, TypeError, RecursionError):
            return None
        return data if len(data) <= MAX_RESULT_BYTES else None
