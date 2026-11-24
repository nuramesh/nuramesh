"""The pure resolver (L5 spec sections 3, 4 and 6): the order of the steps, the local provider, the
author set of a verifier pairing and the start stage's report. The worked cases are in
`tests/engine/test_model_governance_cases.py`."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_catalog import CatalogEntry, ModelCatalog, fold_connections
from nuramesh.core.model_governance import (
    Adapter,
    Authored,
    Block,
    Halt,
    PlanTask,
    Refusal,
    Resolution,
    State,
    check_pairing,
    preflight,
    resolve_ref,
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


def reason(result: Resolution | Refusal) -> str:
    assert isinstance(result, Refusal), result
    return result.reason


def test_a_clean_ref_resolves_with_its_connection() -> None:
    got = resolve_ref(state(), OPENAI, check_price=True)
    assert isinstance(got, Resolution)
    assert (got.family, got.processing, got.endpoint, got.connection_seq, got.price_basis) == (
        "openai",
        "global",
        HOST["azure-openai"],
        1,
        "book",
    )


def test_the_first_failing_step_wins() -> None:
    paused_and_unconnected = state((entry(OPENAI, "openai", "paused"),), events=())
    assert reason(resolve_ref(paused_and_unconnected, OPENAI, check_price=True)) == "model_paused"
    unlisted = state((entry(META, "meta"),))
    assert reason(resolve_ref(unlisted, OPENAI, check_price=True)) == "model_not_in_catalog"
    retired = state((entry(OPENAI, "openai", "retired"),))
    assert reason(resolve_ref(retired, OPENAI, check_price=True)) == "model_retired"
    no_adapter = state((entry("openai/gpt-x", "openai"),), prices=())
    assert (
        reason(resolve_ref(no_adapter, "openai/gpt-x", check_price=True)) == "provider_unsupported"
    )
    customer = state(data_class="customer", events=())
    assert (
        reason(resolve_ref(customer, OPENAI, check_price=True)) == "residency_refused"
    )  # before no_connection
    assert reason(resolve_ref(state(events=()), OPENAI, check_price=True)) == "no_connection"
    disabled = state(events=(connection(1, "azure-openai", status="disabled"),))
    assert reason(resolve_ref(disabled, OPENAI, check_price=True)) == "connection_disabled"
    foreign = state(events=(connection(1, "azure-openai", "https://evil.example.com"),))
    assert reason(resolve_ref(foreign, OPENAI, check_price=True)) == "endpoint_invalid"
    unpriced = state(prices=())
    assert reason(resolve_ref(unpriced, OPENAI, check_price=True)) == "unknown_price"
    assert isinstance(resolve_ref(unpriced, OPENAI, check_price=False), Resolution)  # call stage


def test_no_catalog_refuses_a_real_model_but_not_a_local_one() -> None:
    bare = State(**{**state().__dict__, "catalog": None})
    assert reason(resolve_ref(bare, OPENAI, check_price=True)) == "no_catalog"
    local = resolve_ref(bare, "fake/m1", check_price=False)
    assert isinstance(local, Resolution) and local.local and local.endpoint is None


def test_a_local_model_listed_in_a_catalog_obeys_its_status() -> None:
    listed = state((entry("fake/m1", "fake", "paused"),))
    assert reason(resolve_ref(listed, "fake/m1", check_price=False)) == "model_paused"
    unlisted = state((entry(OPENAI, "openai"),))
    assert reason(resolve_ref(unlisted, "fake/m1", check_price=False)) == "model_not_in_catalog"


def test_customer_data_follows_the_residency_rule_of_the_catalog_entry() -> None:
    canada = state((entry(OPENAI, "openai", processing="canada"),), data_class="customer")
    assert isinstance(resolve_ref(canada, OPENAI, check_price=False), Resolution)
    region = (entry(OPENAI, "openai", processing="region:ca-east"),)
    assert (
        reason(resolve_ref(state(region, data_class="customer"), OPENAI, check_price=False))
        == "residency_refused"
    )
    chosen = state(region, data_class="customer", regions=("ca-east",))
    assert isinstance(resolve_ref(chosen, OPENAI, check_price=False), Resolution)


def pairing(st: State, authors: list[Authored] | None, **kw: Any) -> Block | None:
    base: dict[str, Any] = {
        "verifier": "vera",
        "verifier_ref": META,
        "worker": "ada",
        "declared": False,
        "task": "t1",
        "gate": "g",
        "worker_ref": OPENAI,
    }
    return check_pairing(st, authors=authors, **{**base, **kw})


def test_a_verifier_is_compared_with_the_recorded_authors_in_order() -> None:
    st = state((entry(OPENAI, "openai"), entry(META, "meta"), entry("azure-ai/other", "meta")))
    assert pairing(st, [Authored(OPENAI, "ada")]) is None
    blocked = pairing(st, [Authored(OPENAI, "ada"), Authored("azure-ai/other", "erin")])
    assert blocked is not None and (blocked.reason, blocked.checked) == ("family_conflict", "erin")


def test_an_author_missing_from_the_catalog_is_unknown_never_assumed_fine() -> None:
    blocked = pairing(state(), [Authored("azure-openai/gone", "ada")])
    assert blocked is not None and blocked.reason == "family_unknown"


def test_no_recorded_model_fails_closed_unless_the_worker_has_no_model() -> None:
    blocked = pairing(state(), None)
    assert blocked is not None and blocked.reason == "family_unknown"
    assert pairing(state(), None, worker_ref=None) is None  # code-only worker: skipped
    assert pairing(state(), None, declared=True) is None  # a declared pairing needs both models


def test_nothing_is_evaluated_without_a_catalog() -> None:
    bare = State(**{**state().__dict__, "catalog": None})
    assert pairing(bare, [Authored(OPENAI, "ada")]) is None


def test_the_start_stage_reports_every_block_and_no_partial_list_of_passes() -> None:
    st = state(
        (entry(OPENAI, "openai", "paused"), entry(META, "meta")),
        events=(connection(1, "azure-openai"),),
    )
    report = preflight(st, Halt())
    assert [(b.subject, b.reason) for b in report.blocks] == [
        ("agent=ada", "model_paused"),
        ("agent=vera", "no_connection"),
    ]
    assert report.resolutions == () and not report.ok


def test_a_declared_conflict_never_replaces_a_per_ref_block() -> None:
    same = state((entry(OPENAI, "x"), entry(META, "x")), organisation=org(checks=("ada",)))
    assert [b.reason for b in preflight(same, Halt()).blocks] == ["family_conflict"]
    both = state(
        (entry(OPENAI, "x", "paused"), entry(META, "x")), organisation=org(checks=("ada",))
    )
    assert [b.reason for b in preflight(both, Halt()).blocks] == ["model_paused"]


def test_a_gate_pairing_needs_no_checks_and_no_require_different_model() -> None:
    same = state((entry(OPENAI, "x"), entry(META, "x")))
    plan = [PlanTask("t1", "ada", (("g", "vera"),))]
    blocks = preflight(same, Halt(), plan=plan).blocks
    assert [(b.reason, b.gate, b.task, b.checked) for b in blocks] == [
        ("family_conflict", "g", "t1", "ada")
    ]


def test_a_halt_beats_everything_and_an_unreadable_state_fails_closed() -> None:
    for halt, text in (
        (Halt(halted=True), "is halted"),
        (Halt(unreadable=True), "could not be read"),
    ):
        report = preflight(state(), halt)
        assert [b.reason for b in report.blocks] == ["org_halted", "org_halted"]
        assert text in report.blocks[0].detail


@pytest.mark.parametrize("ref", ["nonsense", "/x", "azure-openai/"])
def test_a_ref_that_cannot_be_split_is_not_in_the_catalog(ref: str) -> None:
    assert reason(resolve_ref(state(), ref, check_price=False)) == "model_not_in_catalog"


def test_state_finds_entries_by_ref_and_says_none_for_what_it_cannot_find() -> None:
    known = state()
    found = known.entry_of(OPENAI)
    assert found is not None and found.family == "openai"
    assert known.family_of(META) == "meta"
    assert known.entry_of("nonsense") is None  # not a provider/model ref
    assert known.family_of("azure-openai/not-listed") is None
    no_catalog = State(
        org=known.org,
        catalog=None,
        connections=known.connections,
        prices=known.prices,
        overrides=(),
        adapter=ADAPTER,
        data_class="synthetic",
        at=AT,
    )
    assert no_catalog.entry_of(OPENAI) is None and no_catalog.family_of(OPENAI) is None


def test_a_halt_with_no_model_to_name_still_blocks_once_for_the_organization() -> None:
    no_models = state(organisation=org(ada=None, vera=None))
    report = preflight(no_models, Halt(halted=True))
    assert [(b.reason, b.subject) for b in report.blocks] == [("org_halted", "org=demo")]
    assert report.resolutions == ()


def test_a_pairing_planned_twice_is_judged_once() -> None:
    same_family = (entry(OPENAI, "openai"), entry(META, "openai"))
    plan = (
        PlanTask("t1", "ada", (("g", "vera"),)),
        PlanTask("t1", "ada", (("g", "vera"),)),
    )
    planned = preflight(state(same_family), Halt(), plan=plan)
    assert [(b.reason, b.task, b.gate) for b in planned.blocks] == [("family_conflict", "t1", "g")]
