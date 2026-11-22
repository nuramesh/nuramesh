"""Model governance for runs: the store-backed layer over the pure resolver (L5, ND92).

`core.model_governance` decides; this module reads the effective state from the ledger and the
configuration, writes what the spec says to write, and builds the provider for a resolution.

- **`Governance.begin`** is the start stage (spec section 6): before `run.started` (and before
  `run.resumed`) every agent, every role-only model and every verifier pairing is resolved; any
  block refuses the run, is written as `model.blocked` on the organization stream (all in one
  transaction) and raised as `ModelGovernanceError`. `record_start_use` writes the
  `model.connection_used` entries (stage `start`) right after `run.started`.
- **`Governance.check_call`** is the call stage (spec section 7): the first governed step of
  `call_model` after the kill switch's own checks. A block is written on the
  run stream and raised as `ModelBlocked`; nothing is reserved, requested or spent. A pass yields a
  `Governed` (the resolution and what `model.called` records). For a verifier gate it compares the
  verifier's family with the **recorded** models of the task (`model.called` entries without a
  `gate` key), never with the worker's current assignment.
- **`Governance.record_use`** writes `model.connection_used` (stage `call`) once per connection
  version per run, immediately before the provider call.
- **`GovernedProviders`** builds the provider from the resolved endpoint, scope and data class.
- **`ensure_seed`** (in `engine.catalog_service`) is called by every process before it reads.

Nothing here reads an endpoint from the environment: the endpoint comes only from the connection.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from nuramesh.core.budgets import CallContext
from nuramesh.core.ledger import EventType
from nuramesh.core.model_catalog import ModelCatalog, connection_key
from nuramesh.core.model_governance import (
    Adapter,
    Authored,
    Block,
    Halt,
    PlanTask,
    Refusal,
    Report,
    Resolution,
    State,
    authors_from_records,
    check_pairing,
    preflight,
    resolve_ref,
    split_ref,
    utc_text,
)
from nuramesh.core.models import (
    CacheSupport,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    cache_support_of,
    format_model_ref,
)
from nuramesh.core.org import OPENAI_COMPATIBLE, OrgGraph
from nuramesh.core.prices import PriceBook, PriceOverride
from nuramesh.core.secrets_text import Redacted
from nuramesh.engine import openai_compatible as oac
from nuramesh.engine.anthropic import (
    KEY_FILE_PROVIDERS,
    AnthropicProvider,
    credential_problem,
)
from nuramesh.engine.azure_openai import (
    KEYLESS_PROVIDERS,
    AzureOpenAIProvider,
    TokenProvider,
)
from nuramesh.engine.connection_rules import connection_problem
from nuramesh.engine.credentials import CredentialSource, CredentialUnavailable
from nuramesh.engine.openai_compatible import OpenAICompatibleProvider
from nuramesh.engine.org_halt import halt_predicate
from nuramesh.engine.org_ledger import OrgLedger, org_stream
from nuramesh.stores.base import EventDraft, Store, run_stream

AZURE_ADAPTER = Adapter(
    keyless_providers=KEYLESS_PROVIDERS,
    connection_problem=lambda provider, endpoint_id, endpoint, auth, key: connection_problem(
        provider, endpoint, endpoint_id=endpoint_id, auth=auth, key=key
    ),
    endpoint_providers=(OPENAI_COMPATIBLE,),
    provider_problem=lambda provider: (
        oac.hosted_problem() if provider == OPENAI_COMPATIBLE else None
    ),
    key_file_providers=tuple(sorted(KEY_FILE_PROVIDERS)),
    credential_check=lambda _provider: credential_problem(),
)
GOVERNANCE_ACTOR = "system:model-governance"


class ModelBlocked(Exception):  # noqa: N818 (a refusal, said in words)
    """A model call was refused by governance. `model.blocked` was recorded on the run stream and
    nothing was reserved, requested or spent."""

    def __init__(self, block: Block) -> None:
        self.block = block
        self.reason, self.detail = block.reason, block.detail
        super().__init__(block.text)


class ModelGovernanceError(Exception):
    """A run was refused at its start (or at a resume): the blocks, in order. Each was recorded as
    `model.blocked` on the organization stream."""

    def __init__(self, blocks: Sequence[Block]) -> None:
        self.blocks = tuple(blocks)
        super().__init__("\n".join(b.text for b in self.blocks))

    def to_problem(self) -> dict[str, Any]:
        """The HTTP 409 problem body: code `model_blocked` and the blocks."""
        return {
            "code": "model_blocked",
            "message": "the run was not started: model governance refused it",
            "blocks": [
                {
                    "agent": b.agent,
                    "role": b.role,
                    "provider": b.provider,
                    "model": b.model,
                    "reason": b.reason,
                    "detail": b.detail,
                }
                for b in self.blocks
            ],
        }


@dataclass(frozen=True)
class GateRef:
    """The verifier gate a call belongs to: its id and the worker whose task it checks."""

    gate: str
    assignee: str


@dataclass(frozen=True)
class Governed:
    """A call that passed governance: the resolution and what the call must record."""

    resolution: Resolution
    data_class: str
    customer_regions: tuple[str, ...]
    gate: str | None = None

    def called_extra(self) -> dict[str, Any]:
        """Keys added to every `model.called` entry of this call (spec section 8.3)."""
        extra: dict[str, Any] = {}
        if not self.resolution.local:
            extra["connection_seq"] = self.resolution.connection_seq
            extra["data_class"] = self.data_class
            extra["processing"] = self.resolution.processing
        if self.gate is not None:
            extra["gate"] = self.gate
        return extra


@runtime_checkable
class ProviderSource(Protocol):
    """Builds the provider for a governed call (spec section 7.2)."""

    def provider_for(
        self, resolution: Resolution, *, data_class: str, customer_regions: tuple[str, ...]
    ) -> ModelProvider:
        """The provider that answers `resolution`."""
        ...


class _StoredCall:
    """The provider of one stored-key call. A refusal at READ time (the key revoked, erased or the
    vault down between the pin and the read) is flattened by the adapter into a `ProviderError`;
    here it is turned back into `CredentialUnavailable` with the fixed code, so the call is
    refused exactly as at pin time (VK22): reservation released, no `model.called`. A fresh
    exception: no `__cause__`, no text of the provider's error."""

    def __init__(self, inner: ModelProvider, refusal: list[str]) -> None:
        self._inner = inner
        self._refusal = refusal

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            return self._inner.complete(request)
        except Exception:
            if not self._refusal:
                raise
        # Raised outside the `except` block so the provider's exception is not the `__context__`.
        raise CredentialUnavailable(self._refusal[-1])


class GovernedProviders:
    """The standard `ProviderSource`: the team's fake provider for `fake` models, and an
    `AzureOpenAIProvider` built from the resolved endpoint for the keyless Azure providers, cached
    by `(provider, endpoint, processing, data_class)` so a repointed connection builds a new one."""

    def __init__(
        self,
        *,
        fake: ModelProvider | None = None,
        token_provider: TokenProvider | None = None,
        credentials: CredentialSource | None = None,
        build: Callable[..., ModelProvider] | None = None,
    ) -> None:
        self._fake = fake
        if token_provider is not None and credentials is not None:
            raise ValueError("give a token_provider or credentials, not both")
        self._credentials = credentials or CredentialSource(entra=token_provider)
        self._build = build
        self._cache: dict[tuple[object, ...], ModelProvider] = {}
        self._lock = threading.Lock()

    def cache_support_for(self, resolution: Resolution) -> CacheSupport:
        """What the provider that answers `resolution` declares about prompt caching, without
        building it (R1.34 5.3): a local resolution answers the fake's mode, `anthropic` is
        `explicit`, `openai-compatible` is `none`, the Azure providers are `automatic`."""
        if resolution.local:
            return cache_support_of(self._fake)
        if resolution.provider == OPENAI_COMPATIBLE:
            return "none"
        if resolution.provider in KEY_FILE_PROVIDERS:
            return AnthropicProvider.cache_support
        return AzureOpenAIProvider.cache_support

    def _stored_provider(self, resolution: Resolution, data_class: str) -> ModelProvider:
        """The provider of a call whose connection uses a STORED key (R1.14c, VK10, VK22).

        The version is pinned HERE, once, when the call starts (after its reservation), and the
        provider is built for this one call and never cached: every read of this call, including
        a retry, gets exactly the pinned version, and the next call pins again (a rotation
        between two calls is picked up; a rotation during a call is not). A refusal
        (`CredentialUnavailable`, a fixed code) is raised here, inside the call's `try`, so the
        reservation is released and nothing is spent."""
        assert resolution.endpoint is not None and resolution.processing is not None
        if resolution.provider not in KEY_FILE_PROVIDERS:
            raise CredentialUnavailable("auth_kind")  # openai-compatible takes no stored key yet
        pin = self._credentials.pin_for(
            resolution.key_ref or "",
            connection=connection_key(resolution.provider, resolution.endpoint_id),
        )
        credentials = self._credentials
        refusal: list[str] = []

        def getter() -> Redacted:
            try:
                return credentials.credential_pinned(pin)
            except CredentialUnavailable as exc:
                refusal.append(exc.code)  # only the fixed code; the provider flattens the error
                raise

        return _StoredCall(
            AnthropicProvider(
                getter,
                data_class=data_class,
                processing=resolution.processing,
                provider_id=resolution.provider,
            ),
            refusal,
        )

    def provider_for(
        self, resolution: Resolution, *, data_class: str, customer_regions: tuple[str, ...]
    ) -> ModelProvider:
        if resolution.local:
            if self._fake is None:
                raise ProviderError("no fake model is configured")
            return self._fake
        assert resolution.endpoint is not None and resolution.processing is not None
        if resolution.key_ref is not None:
            return self._stored_provider(resolution, data_class)
        key = (
            resolution.provider,
            resolution.endpoint_id,
            resolution.endpoint,
            resolution.auth,
            resolution.key,
            resolution.processing,
            data_class,
        )
        with self._lock:
            found = self._cache.get(key)
            if found is None:
                if resolution.provider == OPENAI_COMPATIBLE:
                    assert resolution.endpoint_id is not None and resolution.auth is not None
                    # the constructor re-checks the URL rules and the residency of the endpoint
                    found = OpenAICompatibleProvider(
                        endpoint_id=resolution.endpoint_id,
                        endpoint=resolution.endpoint,
                        auth=resolution.auth,
                        key=resolution.key,
                        credential=self._credentials.getter_for(
                            OPENAI_COMPATIBLE, resolution.auth, resolution.endpoint, resolution.key
                        ),
                        data_class=data_class,
                        processing=resolution.processing,
                    )
                    self._cache[key] = found
                    return found
                if resolution.provider in KEY_FILE_PROVIDERS:
                    found = AnthropicProvider(
                        self._credentials.getter_for(
                            resolution.provider, "key_file", resolution.endpoint
                        ),
                        data_class=data_class,
                        processing=resolution.processing,
                        provider_id=resolution.provider,
                    )
                    self._cache[key] = found
                    return found
                make = self._build or AzureOpenAIProvider
                found = make(
                    resolution.endpoint,
                    self._credentials.getter_for(resolution.provider, "entra", resolution.endpoint),
                    data_class=data_class,
                    provider_id=resolution.provider,
                    processing=resolution.processing,
                    customer_regions=customer_regions,
                )
                self._cache[key] = found
            return found


def plain_provider_block(subject: str, ref: str) -> Block:
    """A fake or plain provider was given a real model: it could only pretend to be it."""
    provider, model = split_ref(ref)
    agent = subject.removeprefix("agent=") if subject.startswith("agent=") else None
    return Block(
        "plain_provider_for_real_model",
        f"'{ref}' is a real model but this run's provider is a fake or plain one; it cannot answer "
        "as a real model (assign a fake/* model, or run with a real provider)",
        subject,
        provider,
        model,
        agent,
        "",
    )


@dataclass
class _RunUse:
    seen: set[tuple[str, int]] = field(default_factory=set)
    loaded: bool = False


class Governance:
    """Governance for one organization against one store (module docstring)."""

    def __init__(
        self,
        store: Store,
        org: OrgGraph,
        *,
        prices: PriceBook,
        overrides: tuple[PriceOverride, ...] = (),
        ledger: OrgLedger | None = None,
        models: ModelCatalog | None = None,
        halted: Callable[[], bool] | None = None,
        adapter: Adapter = AZURE_ADAPTER,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store, self.org = store, org
        self.prices, self.overrides, self.models = prices, overrides, models
        self.ledger = ledger or OrgLedger(store, org.id, prices)
        self.halted = halted if halted is not None else halt_predicate(store, org.id)
        self.adapter, self.clock = adapter, clock or store.now
        self._uses: dict[str, _RunUse] = {}
        self._data_class: dict[str, str] = {}
        self._lock = threading.Lock()

    # -- state -----------------------------------------------------------------------------

    def halt(self) -> Halt:
        """The organization's halt state now (an unreadable state fails closed)."""
        try:
            return Halt(halted=bool(self.halted()))
        except Exception:
            return Halt(unreadable=True)

    def state(self, data_class: str, at: datetime | None = None) -> State:
        """The effective state the resolver reads, at `at` (default now)."""
        view = self.ledger.view()
        return State(
            org=self.org,
            catalog=view.catalog.catalog(),
            connections=view.connections,
            prices=view.catalog.price_book(self.prices, view.connections),
            overrides=self.overrides,
            adapter=self.adapter,
            data_class=data_class,
            at=at or self.clock(),
            assignments=dict(view.catalog.assignments),
            fingerprints=dict(view.catalog.fingerprints),
        )

    def data_class_of(self, run_id: str) -> str:
        """The data class `run.started` recorded for the run (customer if none: ND45)."""
        with self._lock:
            known = self._data_class.get(run_id)
        if known is not None:
            return known
        value = "customer"  # fail closed (ND45): a run with no recorded class is not assumed safe
        first = list(self.store.read_stream(run_stream(run_id), after_seq=0, limit=1))
        if first and first[0].type == EventType.RUN_STARTED.value:
            recorded = first[0].payload.get("data_class")
            if isinstance(recorded, str):
                value = recorded
            with self._lock:
                self._data_class[run_id] = value
        return value

    def price_book(self) -> PriceBook:
        """The configuration's prices plus the honored price versions of the ledger: what a call
        is priced with (so the resolver and `call_model` never disagree about a price)."""
        view = self.ledger.view()
        return view.catalog.price_book(self.prices, view.connections)

    def effective_ref(self, agent_id: str) -> str | None:
        """The model an agent uses now (the ledger over the configuration)."""
        return self.state("synthetic").ref_of(agent_id)

    # -- the start stage ---------------------------------------------------------------------

    def recorded_models(self, run_id: str, tasks: Sequence[str]) -> dict[str, list[Authored]]:
        """The models recorded as having written each task's work: its `model.called` entries
        without a `gate` key (spec section 4)."""
        out: dict[str, list[Authored]] = {}
        for task in tasks:
            out[task] = authors_from_records(self._authors(run_id, task))
        return out

    def _authors(self, run_id: str, task_id: str) -> list[Authored]:
        found: list[Authored] = []
        for event in self.store.task_events(run_id, task_id):
            if event.type != EventType.MODEL_CALLED.value or "gate" in event.payload:
                continue
            provider, model = event.payload.get("provider"), event.payload.get("model")
            agent = event.payload.get("agent")
            if isinstance(provider, str) and isinstance(model, str) and isinstance(agent, str):
                endpoint_id = event.payload.get("endpoint_id")
                found.append(
                    Authored(
                        format_model_ref(
                            provider, endpoint_id if isinstance(endpoint_id, str) else None, model
                        ),
                        agent,
                    )
                )
        return found

    def preflight(
        self,
        data_class: str,
        plan: Sequence[PlanTask] = (),
        *,
        run_id: str | None = None,
        resume: bool = False,
    ) -> Report:
        """The start stage, read-only (also `models check` and `GET /v1/models/resolution`)."""
        recorded = self.recorded_models(run_id, [p.task for p in plan]) if resume and run_id else {}
        return preflight(
            self.state(data_class), self.halt(), plan=plan, recorded=recorded, resume=resume
        )

    def begin(
        self,
        run_id: str,
        data_class: str,
        plan: Sequence[PlanTask],
        *,
        resume: bool = False,
        plain_provider: bool = False,
    ) -> Report:
        """The start stage before `run.started`/`run.resumed`: raises `ModelGovernanceError` (and
        records the blocks on the organization stream) if anything is blocked. With
        `plain_provider` (the run's provider is not a `ProviderSource`, e.g. `--fake`) every
        effective model must be local: a plain provider can answer no real model."""
        report = self.preflight(data_class, plan, run_id=run_id, resume=resume)
        blocks = list(report.blocks)
        if plain_provider:
            for subject, resolution in report.resolutions:
                if not resolution.local:
                    blocks.append(plain_provider_block(subject, resolution.ref))
        if blocks:
            at = self.clock()
            with self.store.transaction() as uow:
                for block in blocks:
                    uow.append(
                        EventDraft(
                            stream=org_stream(self.org.id),
                            type=EventType.MODEL_BLOCKED,
                            actor=GOVERNANCE_ACTOR,
                            payload=_block_payload(block, "start", run_id, data_class, at, None),
                        )
                    )
            raise ModelGovernanceError(blocks)
        return report

    def record_start_use(
        self, run_id: str, report: Report, data_class: str, *, actor: str = GOVERNANCE_ACTOR
    ) -> None:
        """Write the `model.connection_used` entries (stage `start`), one per distinct real
        provider in order of first appearance (spec section 6.4)."""
        order: list[str] = []
        groups: dict[str, list[tuple[str, Resolution]]] = {}
        for subject, resolution in report.resolutions:
            if resolution.local:
                continue
            if resolution.provider not in groups:
                order.append(resolution.provider)
            groups.setdefault(resolution.provider, []).append((subject, resolution))
        for provider in order:
            rows = groups[provider]
            first = rows[0][1]
            assert first.connection_seq is not None
            if self._used(run_id, provider, first.connection_seq):
                continue
            self._write_use(
                run_id,
                actor,
                "start",
                first,
                models=[r.ref for _, r in rows],
                agents=[s.removeprefix("agent=") for s, _ in rows if s.startswith("agent=")],
                processing={r.ref: r.processing or "" for _, r in rows},
                data_class=data_class,
            )

    # -- the call stage ----------------------------------------------------------------------

    def check_call(
        self,
        context: CallContext,
        request: ModelRequest,
        *,
        role: str,
        actor: str,
        gate: GateRef | None = None,
        at: datetime | None = None,
        plain_provider: bool = False,
    ) -> Governed:
        """Steps 2 to 8 and 10 of spec section 3 for one call (before policy, price, reservation).
        A block is recorded on the run stream and raised as `ModelBlocked`."""
        when = at or self.clock()
        data_class = self.data_class_of(context.run_id)
        state = self.state(data_class, when)
        ref = request.model_ref
        outcome = resolve_ref(state, ref, check_price=False)
        if isinstance(outcome, Refusal):
            self._refuse(
                Block(
                    outcome.reason,
                    outcome.detail,
                    f"agent={context.agent_id}",
                    *split_ref(ref),
                    context.agent_id,
                    role,
                ),
                context,
                actor,
                data_class,
                when,
            )
        assert isinstance(outcome, Resolution)
        if plain_provider and not outcome.local:
            self._refuse(
                plain_provider_block(f"agent={context.agent_id}", ref),
                context,
                actor,
                data_class,
                when,
            )
        if gate is not None:
            assert context.task_id is not None  # a gate call is always about a task
            authors = authors_from_records(self._authors(context.run_id, context.task_id))
            worker_ref = state.ref_of(gate.assignee)
            found = check_pairing(
                state,
                verifier=context.agent_id,
                verifier_ref=ref,
                worker=gate.assignee,
                authors=authors or None,
                declared=False,
                task=context.task_id,
                gate=gate.gate,
                worker_ref=worker_ref,
            )
            if found is not None:
                self._refuse(found, context, actor, data_class, when)
        regions = state.catalog.customer_regions if state.catalog is not None else ()
        return Governed(outcome, data_class, regions, gate=gate.gate if gate is not None else None)

    def _refuse(
        self, block: Block, context: CallContext, actor: str, data_class: str, at: datetime
    ) -> None:
        with self.store.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=run_stream(context.run_id),
                    type=EventType.MODEL_BLOCKED,
                    actor=actor,
                    run_id=context.run_id,
                    task_id=context.task_id,
                    payload=_block_payload(
                        block, "call", context.run_id, data_class, at, context.task_id
                    ),
                )
            )
        raise ModelBlocked(block)

    def record_use(
        self, context: CallContext, governed: Governed, *, actor: str = GOVERNANCE_ACTOR
    ) -> None:
        """Write `model.connection_used` (stage `call`) if this connection version is new to the
        run (spec section 8.2). Call it immediately before `provider.complete`."""
        resolution = governed.resolution
        if resolution.local or resolution.connection_seq is None:
            return
        if self._used(context.run_id, resolution.provider, resolution.connection_seq):
            return
        self._write_use(
            context.run_id,
            actor,
            "call",
            resolution,
            models=[resolution.ref],
            agents=[context.agent_id],
            processing={resolution.ref: resolution.processing or ""},
            data_class=governed.data_class,
            task_id=context.task_id,
        )

    def provider_for(self, governed: Governed, source: ProviderSource) -> ModelProvider:
        """The provider to call for a governed call, built from the resolved endpoint."""
        return source.provider_for(
            governed.resolution,
            data_class=governed.data_class,
            customer_regions=governed.customer_regions,
        )

    # -- connection_used bookkeeping ---------------------------------------------------------

    def _used(self, run_id: str, provider: str, seq: int) -> bool:
        """Whether `(provider, seq)` is already recorded for the run (and mark it as recorded)."""
        with self._lock:
            use = self._uses.setdefault(run_id, _RunUse())
            if not use.loaded:
                for event in self.store.read_stream(run_stream(run_id), after_seq=0):
                    if event.type == EventType.MODEL_CONNECTION_USED.value:
                        p, s = event.payload.get("provider"), event.payload.get("connection_seq")
                        if isinstance(p, str) and isinstance(s, int):
                            use.seen.add((p, s))
                use.loaded = True
            if (provider, seq) in use.seen:
                return True
            use.seen.add((provider, seq))
            return False

    def _write_use(
        self,
        run_id: str,
        actor: str,
        stage: str,
        resolution: Resolution,
        *,
        models: list[str],
        agents: list[str],
        processing: Mapping[str, str],
        data_class: str,
        task_id: str | None = None,
    ) -> None:
        assert resolution.set_at is not None
        try:
            with self.store.transaction() as uow:
                uow.append(
                    EventDraft(
                        stream=run_stream(run_id),
                        type=EventType.MODEL_CONNECTION_USED,
                        actor=actor,
                        run_id=run_id,
                        task_id=task_id,
                        payload={
                            "stage": stage,
                            "provider": resolution.provider,
                            **(
                                {"endpoint_id": resolution.endpoint_id}
                                if resolution.endpoint_id is not None
                                else {}
                            ),
                            "endpoint": resolution.endpoint,
                            "connection_seq": resolution.connection_seq,
                            "set_by": resolution.set_by,
                            "set_at": utc_text(resolution.set_at),
                            "models": sorted(set(models)),
                            "agents": sorted(set(agents)),
                            "data_class": data_class,
                            "processing": dict(sorted(processing.items())),
                        },
                    )
                )
        except BaseException:
            with self._lock:  # not written: the next call tries again
                self._uses.get(run_id, _RunUse()).seen.discard(
                    (resolution.provider, resolution.connection_seq or 0)
                )
            raise


def _block_payload(
    block: Block,
    stage: str,
    run_id: str,
    data_class: str,
    at: datetime,
    task_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stage": stage,
        "run": run_id,
        "agent": block.agent,
        "role": block.role,
        "provider": block.provider,
        "model": block.model,
        "reason": block.reason,
        "detail": block.detail,
        "data_class": data_class,
        "at": at.isoformat(),
    }
    if stage == "call" and task_id is not None:
        payload["task"] = task_id
    if block.checked is not None:
        payload["checked"] = block.checked
    if block.gate is not None:
        payload["gate"] = block.gate
    if block.task is not None:
        payload["task"] = block.task
    return payload


def lines_of(blocks: Sequence[Block]) -> list[str]:
    """The text of each block, one per line (what the CLI prints)."""
    return [b.text for b in blocks]


__all__ = [
    "AZURE_ADAPTER",
    "GOVERNANCE_ACTOR",
    "GateRef",
    "Governance",
    "Governed",
    "GovernedProviders",
    "ModelBlocked",
    "ModelGovernanceError",
    "ProviderSource",
    "split_ref",
]
