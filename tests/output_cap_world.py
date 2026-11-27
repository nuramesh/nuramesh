"""A small world for the R1.42 output-cap cases (section 9)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from content_helpers import hasher_for, keyed

from nuramesh.core.budgets import Budget, BudgetScope, BudgetWindow, CallContext
from nuramesh.core.models import Message, ModelRequest, ModelResponse
from nuramesh.core.money import Money
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind
from nuramesh.core.prices import Price, PriceBook, PriceOverride, TokenUsage
from nuramesh.engine.budgets import define_budget
from nuramesh.engine.models import ModelResult, call_model
from nuramesh.stores import MemoryStore, Store

AT = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
PA = {"input": "2", "output": "10"}
PB = {"input": "3", "output": "15", "cached": "1.5", "cache_write": "4"}


def content(prompt_bound: int) -> str:
    """Text whose single-message prompt bound is exactly `prompt_bound` (16 bytes of framing)."""
    return "x" * (prompt_bound - 16)


def price_book(p: dict[str, str] | None, model: str = "m") -> PriceBook:
    if p is None:
        return PriceBook(prices=())
    return PriceBook(
        prices=(
            Price(
                provider="fake",
                model=model,
                input_per_million=Decimal(p["input"]),
                output_per_million=Decimal(p["output"]),
                cached_input_per_million=Decimal(p["cached"]) if "cached" in p else None,
                cache_write_per_million=Decimal(p["cache_write"]) if "cache_write" in p else None,
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                source="spec case",
            ),
        )
    )


class Scripted:
    """A provider that answers with fixed usage and finish reason; keeps every request."""

    def __init__(self, input_tokens: int = 90, output_tokens: int = 10, finish: str = "stop"):
        self.calls: list[ModelRequest] = []
        self._usage = (input_tokens, output_tokens)
        self._finish = finish

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(
            text="ok",
            usage=TokenUsage(input_tokens=self._usage[0], output_tokens=self._usage[1]),
            finish_reason=self._finish,  # type: ignore[arg-type]
        )


def org_with(role_cap: int | None, verifier_cap: int | None = None) -> OrgGraph:
    return OrgGraph(
        id="acme",
        name="Acme",
        roles=(
            Role(id="cos", name="CoS", kind=RoleKind.CHIEF_OF_STAFF),
            Role(id="w", name="W", max_tokens=role_cap),
            Role(id="v", name="V", kind=RoleKind.VERIFIER, max_tokens=verifier_cap),
        ),
        agents=(
            Agent(id="boss", name="Boss", role="cos"),
            Agent(id="dev", name="Dev", role="w", reports_to="boss"),
            Agent(id="vera", name="Vera", role="v", reports_to="boss"),
        ),
    )


@dataclass
class Outcome:
    store: Store
    result: ModelResult | None
    error: BaseException | None
    provider: Scripted
    spend_seen: list[Money] = field(default_factory=list)

    def events(self, *types: str) -> list[Any]:
        evs = list(self.store.read_stream("run:r1"))
        return [e for e in evs if not types or e.type in types]

    def types(self) -> list[str]:
        return [e.type for e in self.events()]

    def one(self, type_: str) -> Any:
        found = self.events(type_)
        assert len(found) == 1, (type_, self.types())
        return found[0]


def run_call(
    *,
    budgets: Sequence[tuple[str, BudgetScope, str, str]] = (("b", BudgetScope.RUN, "r1", "1"),),
    price: dict[str, str] | None = PA,
    prompt: int = 100,
    request_tokens: int = 1000,
    role_cap: int | None = None,
    task_cap: int | None = None,
    exempt: bool = False,
    provider: Scripted | None = None,
    agent: str = "dev",
    verifier_cap: int | None = None,
    overrides: tuple[PriceOverride, ...] = (),
    window: BudgetWindow = BudgetWindow.NONE,
    store: Store | None = None,
    now: datetime = AT,
    reservation_id: str = "res1",
    prepare: Callable[[Store], None] | None = None,
) -> Outcome:
    store = store or MemoryStore()
    for bid, scope, scope_id, limit in budgets:
        define_budget(
            store,
            Budget(id=bid, scope=scope, scope_id=scope_id, limit=Money.of(limit), window=window),
            actor="human:owner",
            org_id="acme",
        )
    if prepare is not None:
        prepare(store)
    org = org_with(role_cap, verifier_cap)
    provider = provider or Scripted()
    seen: list[Money] = []
    out = Outcome(store, None, None, provider, seen)
    try:
        out.result = call_model(
            store,
            provider,
            ModelRequest(
                provider="fake",
                model="m",
                messages=(Message(role="user", content=content(prompt)),),
                max_output_tokens=request_tokens,
            ),
            context=CallContext(
                run_id="r1",
                task_id="t1",
                agent_id=agent,
                task_max_tokens=task_cap,
                cap_exempt=exempt,
            ),
            org=org,
            prices=price_book(price),
            overrides=tuple(keyed(o) for o in overrides),
            reservation_id=reservation_id,
            actor=f"agent:{agent}",
            authorize=lambda *_: None,
            authorize_spend=lambda _c, amount: seen.append(amount),
            now=lambda: now,
            hasher=hasher_for(store, org),
        )
    except BaseException as exc:  # the cases inspect the refusal
        out.error = exc
    return out
