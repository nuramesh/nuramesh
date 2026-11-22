"""How a run resolves its models: the pure resolver (L5, ND92, spec/model-governance/).

A real model is used only if it is in the approved catalog, may receive the run's data class, has
an active connection whose endpoint the adapter accepts, has a price in effect, and (for a
verifier) comes from another family than the models that wrote the work it checks (ND48).

`resolve_ref` is one `provider/model` through the steps of spec section 3; the first failing step
wins. `preflight` is the start stage for a whole organization (spec section 6), and
`check_pairing` is verifier independence for one verifier and the models that wrote the work
(spec section 4). Everything here is a pure function of the state it is given; reading that state
from the ledger is `engine.governance`, and the adapter rules (which providers are keyless, which
endpoints a token may go to) are given in as an `Adapter` so core stays cloud-neutral.

The order of the checks inside one model call is not decided here: the kill switch's checks (halt,
paused role, breaker evaluation) come first, then this resolver.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from nuramesh.core.endpoints import residency_problem
from nuramesh.core.model_catalog import (
    STORED_KEY_PROVIDERS,
    CatalogEntry,
    ConnectionRecord,
    ConnectionsState,
    ModelCatalog,
    effective_ref,
    effective_role_default,
    void_broken_free_prices,
)
from nuramesh.core.models import parse_model_ref
from nuramesh.core.org import OPENAI_COMPATIBLE, OrgGraph, RoleKind
from nuramesh.core.prices import (
    PriceBasis,
    PriceBook,
    PriceOverride,
    UnknownPriceError,
    resolve_price,
)
from nuramesh.core.residency import ResidencyError, require_data_residency

LOCAL_PROVIDER = "fake"
"""The local provider: no data leaves the process, so it needs no connection or adapter."""
HOSTED_REASONS = ("hosted needs a secret store", "NURAMESH_HOSTED must")
"""The start of every hosted refusal text of an adapter: core cannot import the engine, so it
recognises that refusal by prefix (a test pins the adapter's texts to these)."""

_OAC = "openai-compatible: "
STORED_OAC_TEXT = "stored keys are not wired for openai-compatible yet"
CONNECTION_CHANGED = f"{_OAC}the connection changed since this model was approved; approve it again"
NO_CONNECTION_OAC = f"{_OAC}the connection of this model does not exist"
DISABLED_OAC = f"{_OAC}the connection is disabled"
P_BOUND = f"{_OAC}the free price is not valid for this connection; declare it again"

ND48 = "a verifier must come from a different model family (ND48)"

HALTED_TEXT = (
    "the organization is halted: no run starts and no model is called until a human lifts it"
)
HALT_UNREADABLE_TEXT = "the halt state could not be read: failing closed"
NO_RECORD = "no model call is recorded"


@dataclass(frozen=True)
class Adapter:
    """What the engine's provider adapters decide, given to the pure resolver."""

    keyless_providers: tuple[str, ...]
    connection_problem: Callable[[str, str | None, str, str, str | None], str | None]
    """Why a connection `(provider, endpoint id, endpoint, auth, key name)` is refused, or None if
    it is fine."""
    endpoint_providers: tuple[str, ...] = ()
    """Providers whose connections are named by an endpoint id and carry their own `auth`
    (`openai-compatible`)."""
    provider_problem: Callable[[str], str | None] | None = None
    """Step 5: why `provider` cannot be used at all here (hosted mode), or None. Fixed text."""
    key_file_providers: tuple[str, ...] = ()
    """Providers whose connection `auth` is `key_file` (the server's key file is the credential);
    they are processed globally at one fixed endpoint (spec r114b 11.3)."""
    credential_check: Callable[[str], str | None] | None = None
    """Step 8a: why the credential of a key-file provider is unusable, or None. Fixed text; never
    the key or its path. Given in so core does no I/O."""

    @property
    def supported(self) -> tuple[str, ...]:
        """Every provider with an adapter."""
        return (*self.keyless_providers, *self.key_file_providers, *self.endpoint_providers)

    def auth_of(self, provider: str) -> str:
        """The `auth` a stored connection of `provider` must carry."""
        return "key_file" if provider in self.key_file_providers else "entra"


@dataclass(frozen=True)
class Halt:
    """The organization's halt state at the moment of the check."""

    halted: bool = False
    unreadable: bool = False

    @property
    def blocks(self) -> bool:
        """Whether nothing starts (halted, or the state could not be read: fail closed)."""
        return self.halted or self.unreadable


@dataclass(frozen=True)
class State:
    """The effective state the resolver reads (spec section 1), at one moment."""

    org: OrgGraph
    catalog: ModelCatalog | None
    connections: ConnectionsState
    prices: PriceBook
    overrides: tuple[PriceOverride, ...]
    adapter: Adapter
    data_class: str
    at: datetime
    assignments: Mapping[tuple[str, str], str] = field(default_factory=dict)
    fingerprints: Mapping[tuple[str, str | None, str], str] = field(default_factory=dict)
    """The connection fingerprint each approved `openai-compatible` entry was approved against."""

    def ref_of(self, agent_id: str) -> str | None:
        """The effective `provider/model` of an agent, or None."""
        return effective_ref(self.org, self.assignments, agent_id).ref

    def family_of(self, ref: str) -> str | None:
        """The family of a catalog entry (its status does not matter), or None if unlisted."""
        entry = self.entry_of(ref)
        return entry.family if entry is not None else None

    def entry_of(self, ref: str) -> CatalogEntry | None:
        """The catalog entry for `ref`, or None."""
        if self.catalog is None:
            return None
        try:
            provider, endpoint_id, model = parse_model_ref(ref)
        except ValueError:
            return None
        return self.catalog.find(provider, model, endpoint_id)


@dataclass(frozen=True)
class Resolution:
    """A `ref` that passed every step."""

    ref: str
    provider: str
    model: str
    family: str | None
    processing: str | None
    endpoint: str | None
    connection_seq: int | None
    set_by: str | None
    set_at: datetime | None
    price_basis: str | None
    endpoint_id: str | None = None
    """The connection's endpoint id (`openai-compatible` only)."""
    auth: str | None = None
    """The connection's `auth` (`openai-compatible` only)."""
    key: str | None = None
    """The connection's key *name* (`openai-compatible` with auth `key` only)."""
    key_ref: str | None = None
    """The id of the connection's stored key (auth `stored` only; never a value)."""

    @property
    def local(self) -> bool:
        """Whether this is the local provider (no connection, nothing leaves the process)."""
        return self.provider == LOCAL_PROVIDER


@dataclass(frozen=True)
class Refusal:
    """Why a `ref` did not resolve: a reason code and its exact detail text (spec section 9)."""

    reason: str
    detail: str


@dataclass(frozen=True)
class Block:
    """A refusal about a subject (an agent, or a role with no agent), ready to be recorded."""

    reason: str
    detail: str
    subject: str
    provider: str
    model: str
    agent: str | None
    role: str
    checked: str | None = None
    gate: str | None = None
    task: str | None = None

    @property
    def text(self) -> str:
        """`model blocked [<reason>] <subject>: <detail>` (spec section 9)."""
        return f"model blocked [{self.reason}] {self.subject}: {self.detail}"


def utc_text(at: datetime) -> str:
    """`YYYY-MM-DDTHH:MM:SSZ`."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_ref(ref: str) -> tuple[str, str]:
    """`(provider, model)` of a ref, for records; a ref that cannot be split is `("", ref)`. For an
    `openai-compatible` ref the model part is `<endpoint id>/<model>` (the text after the
    provider), so a record never merges two endpoints."""
    try:
        parsed = parse_model_ref(ref)
    except ValueError:
        return "", ref
    tail = parsed.model if parsed.endpoint_id is None else f"{parsed.endpoint_id}/{parsed.model}"
    return parsed.provider, tail


def split_ref3(ref: str) -> tuple[str, str | None, str]:
    """`(provider, endpoint_id, model)` of a ref; unsplittable gives `("", None, ref)`."""
    try:
        return parse_model_ref(ref)
    except ValueError:
        return "", None, ref


# -- one ref -----------------------------------------------------------------------------------


def resolve_ref(state: State, ref: str, *, check_price: bool) -> Resolution | Refusal:
    """Steps 2 to 8 of spec section 3 (and 9 when `check_price`) for one `provider/model`.

    The halt (step 1) and verifier independence (step 10) are not about one ref and are handled by
    the caller. The local provider skips steps 5 to 8 (it needs no adapter or connection)."""
    try:
        provider, endpoint_id, model = parse_model_ref(ref)
    except ValueError as exc:
        if ref.startswith(f"{OPENAI_COMPATIBLE}/"):  # U-endpoint-needed or U-id, before step 1
            return Refusal("endpoint_invalid", str(exc))
        provider, endpoint_id, model = "", None, ref
    local = provider == LOCAL_PROVIDER
    catalog = state.catalog
    entry: CatalogEntry | None = None
    if catalog is None:
        if not local:
            return Refusal(
                "no_catalog",
                f"'{ref}' needs an approved model catalog and there is none: an admin adds and "
                "approves models (POST /v1/models/catalog/entries, then .../approve), or the "
                "team configuration seeds one (models:) before the first start",
            )
    else:
        entry = catalog.find(provider, model, endpoint_id)
        if entry is None:
            return Refusal("model_not_in_catalog", f"'{ref}' is not in the approved catalog")
        if entry.status == "paused":
            return Refusal("model_paused", f"'{ref}' is paused in the approved catalog")
        if entry.status == "retired":
            return Refusal("model_retired", f"'{ref}' is retired in the approved catalog")
    record: ConnectionRecord | None = None
    if not local:
        assert entry is not None and catalog is not None  # a real model needs a catalog (above)
        adapter = state.adapter
        if provider not in adapter.supported:
            return Refusal(
                "provider_unsupported",
                f"provider '{provider}' has no adapter in this version "
                f"(supported providers: {', '.join(adapter.supported)})",
            )
        unusable = adapter.provider_problem(provider) if adapter.provider_problem else None
        if unusable is not None:
            return Refusal("endpoint_invalid", unusable)
        try:
            require_data_residency(
                entry.processing, state.data_class, customer_regions=catalog.customer_regions
            )
        except ResidencyError as exc:
            return Refusal(
                "residency_refused",
                f"'{ref}' is processed in {entry.processing} and may not receive "
                f"{state.data_class} data: {exc}",
            )
        if provider in adapter.key_file_providers:
            if entry.processing != "global":
                return Refusal(
                    "residency_refused",
                    f"provider '{provider}' processes data globally; an entry cannot declare "
                    f"{entry.processing}",
                )
            if catalog.customer_regions:
                regions = ", ".join(sorted(catalog.customer_regions))
                return Refusal(
                    "residency_refused",
                    f"the organization's data location is restricted to {regions}; "
                    f"provider '{provider}' processes data globally",
                )
        connection = state.connections.get(provider, endpoint_id)
        record = connection.record
        if record is None and connection.invalid_seq is None:
            if provider in adapter.endpoint_providers:
                return Refusal("no_connection", NO_CONNECTION_OAC)
            return Refusal(
                "no_connection",
                f"no connection for provider '{provider}': an admin connects it "
                f"(PUT /v1/models/connections/{provider}, or the Models screen in the app)",
            )
        if record is not None and record.status == "disabled":
            if provider in adapter.endpoint_providers:
                return Refusal("connection_disabled", DISABLED_OAC)
            return Refusal(
                "connection_disabled", f"the connection for provider '{provider}' is disabled"
            )
        problem = (
            "its last change is not valid"
            if record is None
            else (
                "its credential type does not match the provider"
                if provider not in adapter.endpoint_providers
                and record.auth != adapter.auth_of(provider)
                and not (record.auth == "stored" and provider in STORED_KEY_PROVIDERS)
                else STORED_OAC_TEXT
                if record.auth == "stored" and provider in adapter.endpoint_providers
                else adapter.connection_problem(
                    provider, endpoint_id, record.endpoint, record.auth, record.key
                )
            )
        )
        if problem is not None:
            return Refusal(
                "endpoint_invalid",
                f"the connection for provider '{provider}' cannot be used: {problem}",
            )
        if record is not None and provider in adapter.endpoint_providers:
            if state.fingerprints.get(entry.key) != record.fingerprint:
                return Refusal("connection_changed", CONNECTION_CHANGED)
            residency = residency_problem(record.endpoint, entry.processing)
            if residency is not None:
                return Refusal("residency_refused", residency)
        if provider in adapter.key_file_providers and adapter.credential_check is not None:
            lacking = adapter.credential_check(provider)
            stored = record is not None and record.auth == "stored"
            if lacking is not None and stored:
                # a stored key does not need the key-file setting; only the hosted refusal stays
                if not lacking.removeprefix(f"{provider}: ").startswith(HOSTED_REASONS):
                    lacking = None
            if lacking is not None:
                reason = lacking.removeprefix(f"{provider}: ")
                how = (
                    ""
                    if reason.startswith(HOSTED_REASONS)
                    else "; set NURAMESH_ANTHROPIC_KEY_FILE to the path of a file outside the "
                    "repository, owned by the server user, mode 0400 or 0600 (the owner creates "
                    "it; see docs/model-governance.md)"
                )
                return Refusal(
                    "credential_unavailable",
                    f"no usable API key file for provider '{provider}': {reason}{how}",
                )
    basis: str | None = None
    if check_price:
        try:
            resolved = resolve_price(
                void_broken_free_prices(state.prices, state.connections),
                provider,
                model,
                state.at,
                overrides=state.overrides,
                endpoint_id=endpoint_id,
            )
        except (UnknownPriceError, ValueError):
            if void_broken_free_prices(state.prices, state.connections).blocked_at(
                provider, endpoint_id, model, state.at
            ):
                return Refusal("unknown_price", P_BOUND)
            return Refusal(
                "unknown_price",
                f"'{ref}' has no price in effect at {utc_text(state.at)}; a price is never guessed",
            )
        basis = "provisional" if resolved.basis is PriceBasis.PROVISIONAL else "book"
    return Resolution(
        ref=ref,
        provider=provider,
        model=model,
        family=entry.family if entry is not None else None,
        processing=None if local or entry is None else entry.processing,
        endpoint=record.endpoint if record is not None else None,
        connection_seq=record.seq if record is not None else None,
        set_by=record.set_by if record is not None else None,
        set_at=record.set_at if record is not None else None,
        price_basis=basis,
        endpoint_id=endpoint_id,
        auth=record.auth
        if record is not None and provider in state.adapter.endpoint_providers
        else None,
        key=record.key if record is not None else None,
        key_ref=record.key_ref if record is not None else None,
    )


# -- verifier independence ---------------------------------------------------------------------


@dataclass(frozen=True)
class Authored:
    """One model that wrote (part of) a task's work, and the agent that called it."""

    ref: str
    agent: str


@dataclass(frozen=True)
class GatePair:
    """A verifier gate of a planned task: who verifies whose work, in which task and gate."""

    verifier: str
    worker: str
    task: str
    gate: str


def authors_from_records(records: Sequence[Authored]) -> list[Authored]:
    """The distinct recorded models in order of first occurrence (spec section 4 item 1)."""
    seen: set[str] = set()
    out: list[Authored] = []
    for item in records:
        if item.ref not in seen:
            seen.add(item.ref)
            out.append(item)
    return out


def check_pairing(
    state: State,
    *,
    verifier: str,
    verifier_ref: str,
    worker: str,
    authors: Sequence[Authored] | None,
    declared: bool,
    task: str | None = None,
    gate: str | None = None,
    worker_ref: str | None = None,
) -> Block | None:
    """Verifier independence for one pairing (spec section 4). `authors` is the author set:
    None means there is none (a call whose task has no recorded model); `worker_ref` is the
    worker's effective ref (None for a code-only worker). Returns a `family_conflict` or
    `family_unknown` block, or None when the pair is fine or skipped."""
    provider, model = split_ref(verifier_ref)
    role = state.org.agent(verifier).role

    def block(reason: str, detail: str, checked: str) -> Block:
        return Block(
            reason=reason,
            detail=detail,
            subject=f"agent={verifier}",
            provider=provider,
            model=model,
            agent=verifier,
            role=role,
            checked=checked,
            gate=None if declared else gate,
            task=None if declared else task,
        )

    if state.catalog is None:
        return None  # nothing is evaluated without a catalog (spec section 4 item 5)
    if not authors:
        if declared or worker_ref is None:
            return None  # a declared pairing needs both models; a code-only worker is skipped
        return block(
            "family_unknown",
            f"{NO_RECORD} for task '{task}' of '{worker}' (assigned {worker_ref}), so verifier "
            f"'{verifier}' cannot be checked for independence (ND48)",
            worker,
        )
    fv = state.family_of(verifier_ref)
    for item in authors:
        family = state.family_of(item.ref)
        if family is None or fv is None:
            return block(
                "family_unknown",
                f"the family of '{item.ref}' ({item.agent}) is not known, so verifier "
                f"'{verifier}' cannot be checked for independence (ND48)",
                item.agent,
            )
        if family == fv:
            where = (
                " which it checks"
                if declared
                else f" whose task '{task}' it checks in gate '{gate}'"
            )
            return block(
                "family_conflict",
                f"verifier '{verifier}' uses family '{fv}', the same as '{item.agent}' "
                f"({item.ref}){where}: {ND48}",
                item.agent,
            )
    return None


# -- the start stage ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanTask:
    """What the start stage needs of a planned task: its assignee and its verifier gates."""

    task: str
    assignee: str
    verifier_gates: tuple[tuple[str, str], ...]
    """`(gate id, verifier agent id)` in gate order."""


@dataclass(frozen=True)
class Report:
    """The start-stage answer (also `models check` and `GET /v1/models/resolution`)."""

    blocks: tuple[Block, ...]
    resolutions: tuple[tuple[str, Resolution], ...]
    """`(subject, resolution)` in order: agents, then role-only models. `()` when any block."""

    @property
    def ok(self) -> bool:
        """Whether nothing is blocked."""
        return not self.blocks


def _subjects(state: State) -> list[tuple[str, str | None, str, str]]:
    """`(subject, agent id or None, role id, ref)`: agents with a model, then roles with a default
    model and no agent."""
    out: list[tuple[str, str | None, str, str]] = []
    for agent in state.org.agents:
        ref = state.ref_of(agent.id)
        if ref is not None:
            out.append((f"agent={agent.id}", agent.id, agent.role, ref))
    held = {a.role for a in state.org.agents}
    for role in state.org.roles:
        if role.id in held:
            continue
        ref = effective_role_default(state.org, state.assignments, role.id)
        if ref is not None:
            out.append((f"role={role.id}", None, role.id, ref))
    return out


def preflight(
    state: State,
    halt: Halt,
    *,
    plan: Sequence[PlanTask] = (),
    recorded: Mapping[str, Sequence[Authored]] | None = None,
    resume: bool = False,
) -> Report:
    """The start stage (spec section 6): every agent and role-only model, then every pairing.

    `recorded` maps a task id to the models recorded for it (used on `resume`)."""
    subjects = _subjects(state)
    blocks: list[Block] = []
    resolved: dict[str, Resolution] = {}
    ordered: list[tuple[str, Resolution]] = []
    if halt.blocks:
        detail = HALT_UNREADABLE_TEXT if halt.unreadable and not halt.halted else HALTED_TEXT
        for subject, agent, role, ref in subjects:
            provider, model = split_ref(ref)
            blocks.append(Block("org_halted", detail, subject, provider, model, agent, role))
        if not subjects:
            blocks.append(Block("org_halted", detail, f"org={state.org.id}", "", "", None, ""))
        return Report(tuple(blocks), ())
    for subject, agent, role, ref in subjects:
        result = resolve_ref(state, ref, check_price=True)
        if isinstance(result, Refusal):
            provider, model = split_ref(ref)
            blocks.append(
                Block(result.reason, result.detail, subject, provider, model, agent, role)
            )
            continue
        ordered.append((subject, result))
        if agent is not None:
            resolved[agent] = result
    blocks.extend(_pairing_blocks(state, resolved, plan, recorded or {}, resume))
    if blocks:
        return Report(tuple(blocks), ())
    return Report((), tuple(ordered))


def _pairing_blocks(
    state: State,
    resolved: Mapping[str, Resolution],
    plan: Sequence[PlanTask],
    recorded: Mapping[str, Sequence[Authored]],
    resume: bool,
) -> list[Block]:
    out: list[Block] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for agent in state.org.agents:
        if state.org.role(agent.role).kind is not RoleKind.VERIFIER:
            continue
        for worker in agent.checks:
            declared_key = (agent.id, worker, None, None)
            if declared_key in seen:
                continue
            seen.add(declared_key)
            v, w = resolved.get(agent.id), resolved.get(worker)
            if v is None or w is None:
                continue  # an agent with no model, or one that did not resolve: not evaluated
            found = check_pairing(
                state,
                verifier=agent.id,
                verifier_ref=v.ref,
                worker=worker,
                authors=[Authored(w.ref, worker)],
                declared=True,
            )
            if found is not None:
                out.append(found)
    for item in plan:
        for gate_id, verifier in item.verifier_gates:
            key = (verifier, item.assignee, item.task, gate_id)
            if key in seen:
                continue
            seen.add(key)
            v, w = resolved.get(verifier), resolved.get(item.assignee)
            if v is None or w is None:
                continue
            authors = list(recorded.get(item.task, ())) if resume else []
            authors = authors_from_records(authors)
            if all(a.ref != w.ref for a in authors):
                authors.append(Authored(w.ref, item.assignee))
            found = check_pairing(
                state,
                verifier=verifier,
                verifier_ref=v.ref,
                worker=item.assignee,
                authors=authors,
                declared=False,
                task=item.task,
                gate=gate_id,
                worker_ref=w.ref,
            )
            if found is not None:
                out.append(found)
    return out


Stage = Literal["start", "call"]
