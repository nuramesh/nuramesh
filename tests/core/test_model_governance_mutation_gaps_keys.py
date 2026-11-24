"""Mutation pins for `nuramesh.core.model_governance` (Part of #1194): behaviours the farm's wide
replay found unpinned: a halt that is both halted and unreadable, the local provider's missing
processing, the `auth` of a keyless connection and an unsplittable ref. (The immutability pin was
dropped: `test_model_governance_mutation_gaps.py` already freezes every record.)"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_catalog import CatalogEntry, ModelCatalog, fold_connections
from nuramesh.core.model_governance import (
    HALT_UNREADABLE_TEXT,
    HALTED_TEXT,
    Adapter,
    Halt,
    Resolution,
    State,
    preflight,
    resolve_ref,
    split_ref3,
)
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind
from nuramesh.core.prices import Price, PriceBook

AT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
OPENAI, META = "azure-openai/gpt-5.4-mini", "azure-ai/llama"
HOST = {
    "azure-openai": "https://a.openai.azure.com",
    "azure-ai": "https://b.cognitiveservices.azure.com",
}
ADAPTER = Adapter(
    keyless_providers=("azure-openai", "azure-ai"),
    connection_problem=lambda provider, _id, endpoint, _auth, _key: (
        None if endpoint == HOST.get(provider) else "the endpoint must be https://<name>..."
    ),
)


def connection(
    n: int, provider: str, endpoint: str | None = None, status: str = "active"
) -> LedgerEvent:
    return LedgerEvent.model_construct(
        stream="org:demo",
        seq=n,
        type="model.connection_created",
        actor="human:u_adm1",
        at=AT,
        run_id=None,
        task_id=None,
        cost=None,
        cost_basis=None,
        prev_hash="0" * 64,
        hash="1" * 64,
        payload={
            "provider": provider,
            "auth": "entra",
            "endpoint": endpoint or HOST[provider],
            "status": status,
        },
    )


def entry(
    ref: str, family: str, status: str = "approved", processing: str = "global"
) -> CatalogEntry:
    provider, model = ref.split("/", 1)
    return CatalogEntry(
        provider=provider,
        model=model,
        family=family,
        processing=processing,
        status=status,  # type: ignore[arg-type]
    )


def org(**models: Any) -> OrgGraph:
    return OrgGraph(
        id="demo",
        name="Demo",
        roles=(
            Role(id="cos", name="Cos", kind=RoleKind.CHIEF_OF_STAFF),
            Role(id="dev", name="Dev"),
            Role(id="ver", name="Ver", kind=RoleKind.VERIFIER),
        ),
        agents=(
            Agent(id="boss", name="Boss", role="cos"),
            Agent(
                id="ada", name="Ada", role="dev", reports_to="boss", model=models.get("ada", OPENAI)
            ),
            Agent(
                id="vera",
                name="Vera",
                role="ver",
                reports_to="boss",
                model=models.get("vera", META),
                checks=models.get("checks", ()),
            ),
        ),
    )


def state(
    entries: tuple[CatalogEntry, ...] | None = None,
    *,
    events: tuple[LedgerEvent, ...] | None = None,
    data_class: str = "synthetic",
    prices: tuple[str, ...] = (OPENAI, META),
    organisation: OrgGraph | None = None,
    regions: tuple[str, ...] = (),
) -> State:
    if entries is None:
        entries = (entry(OPENAI, "openai"), entry(META, "meta"))
    evs = (
        events if events is not None else (connection(1, "azure-openai"), connection(2, "azure-ai"))
    )
    return State(
        org=organisation or org(),
        catalog=ModelCatalog(entries=entries, customer_regions=regions),
        connections=fold_connections(evs),
        prices=PriceBook(
            prices=tuple(
                Price(
                    provider=r.split("/")[0],
                    model=r.split("/")[1],
                    input_per_million=Decimal(1),
                    output_per_million=Decimal(2),
                    effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                    source="the list",
                )
                for r in prices
            )
        ),
        overrides=(),
        adapter=ADAPTER,
        data_class=data_class,
        at=AT,
    )


def test_a_halt_that_is_halted_and_unreadable_reports_the_halt_not_the_read_failure() -> None:
    both = preflight(state(), Halt(halted=True, unreadable=True))
    assert {block.detail for block in both.blocks} == {HALTED_TEXT}
    only_unreadable = preflight(state(), Halt(unreadable=True))
    assert {block.detail for block in only_unreadable.blocks} == {HALT_UNREADABLE_TEXT}
    only_halted = preflight(state(), Halt(halted=True))
    assert {block.detail for block in only_halted.blocks} == {HALTED_TEXT}


def test_a_local_model_listed_and_approved_has_no_processing() -> None:
    listed = state((entry("fake/m1", "fake", processing="canada"),))
    got = resolve_ref(listed, "fake/m1", check_price=False)
    assert isinstance(got, Resolution) and got.local
    assert got.processing is None
    assert got.connection_seq is None and got.auth is None


def test_a_keyless_connection_carries_no_auth_into_the_resolution() -> None:
    got = resolve_ref(state(), OPENAI, check_price=False)
    assert isinstance(got, Resolution)
    assert got.connection_seq == 1  # a connection record was read ...
    assert got.auth is None  # ... but `auth` is only reported for endpoint providers


@pytest.mark.parametrize("ref", ["nonsense", "openai-compatible/", ""])
def test_a_ref_that_cannot_be_split_comes_back_whole_without_a_provider(ref: str) -> None:
    assert split_ref3(ref) == ("", None, ref)
