"""Mutation pins for the pure model resolver (`nuramesh.core.model_governance`), Part of #1344.

Each test asserts a returned value, a refusal reason and its exact detail text, or a default. The
fixed texts are asserted here on purpose: they are the strings an admin reads in a block. Fake data
only; no model is called."""

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, get_args

import pytest

from nuramesh.core.endpoints import FREE_SOURCE, REMOTE_RESIDENCY
from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_catalog import (
    CatalogEntry,
    ConnectionFacts,
    ModelCatalog,
    fold_connections,
)
from nuramesh.core.model_governance import (
    Adapter,
    Authored,
    Block,
    GatePair,
    Halt,
    PlanTask,
    Refusal,
    Report,
    Resolution,
    Stage,
    State,
    authors_from_records,
    check_pairing,
    preflight,
    resolve_ref,
    split_ref,
    utc_text,
)
from nuramesh.core.models import U_ENDPOINT_NEEDED
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind
from nuramesh.core.prices import Price, PriceBook, PriceOverride

AT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
AZ = "azure-openai/gpt-x"
AZ2 = "azure-openai/gpt-y"
ANT = "anthropic/claude-x"
OAC = "openai-compatible/lab/m1"
KEY_REF = "mk_" + "a" * 20
ND48_TEXT = "a verifier must come from a different model family (ND48)"
CREDENTIAL_HOW = (
    "; set NURAMESH_ANTHROPIC_KEY_FILE to the path of a file outside the repository, owned by "
    "the server user, mode 0400 or 0600 (the owner creates it; see the model governance guide)"
)


def adapter(
    credential: Callable[[str], str | None] | None = None,
    provider_problem: Callable[[str], str | None] | None = None,
) -> Adapter:
    return Adapter(
        keyless_providers=("azure-openai",),
        connection_problem=lambda *_a: None,
        endpoint_providers=("openai-compatible",),
        provider_problem=provider_problem,
        key_file_providers=("anthropic",),
        credential_check=credential,
    )


def event(n: int, **payload: Any) -> LedgerEvent:
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
        payload={"status": "active", **payload},
    )


AZ_CONN = {"provider": "azure-openai", "auth": "entra", "endpoint": "https://a.openai.azure.com"}
ANT_CONN = {"provider": "anthropic", "auth": "key_file", "endpoint": "https://api.example.test"}
OAC_CONN = {
    "provider": "openai-compatible",
    "endpoint_id": "lab",
    "auth": "none",
    "endpoint": "https://models.example.test/v1",
}


def fingerprint_of(payload: dict[str, Any]) -> str:
    return ConnectionFacts(**{"status": "active", **payload}).fingerprint


def entry(ref: str, family: str, processing: str = "global") -> CatalogEntry:
    provider, rest = ref.split("/", 1)
    endpoint_id = None
    if provider == "openai-compatible":
        endpoint_id, rest = rest.split("/", 1)
    return CatalogEntry(
        provider=provider,
        endpoint_id=endpoint_id,
        model=rest,
        family=family,
        processing=processing,
    )


def price(ref: str, **kw: Any) -> Price:
    provider, rest = ref.split("/", 1)
    endpoint_id = None
    if provider == "openai-compatible":
        endpoint_id, rest = rest.split("/", 1)
    base: dict[str, Any] = {
        "provider": provider,
        "endpoint_id": endpoint_id,
        "model": rest,
        "input_per_million": Decimal(1),
        "output_per_million": Decimal(2),
        "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "source": "the list",
    }
    return Price(**{**base, **kw})


def org(*, ada: str | None = AZ, vera: str | None = AZ2, checks: tuple[str, ...] = ()) -> OrgGraph:
    return OrgGraph(
        id="demo",
        name="Demo",
        roles=(
            Role(id="cos", name="Cos", kind=RoleKind.CHIEF_OF_STAFF),
            Role(id="dev", name="Dev", default_model=ANT),
            Role(id="ver", name="Ver", kind=RoleKind.VERIFIER),
            Role(id="ops", name="Ops", default_model=AZ),
            Role(id="idle", name="Idle"),
        ),
        agents=(
            Agent(id="boss", name="Boss", role="cos"),
            Agent(id="ada", name="Ada", role="dev", reports_to="boss", model=ada),
            Agent(id="vera", name="Vera", role="ver", reports_to="boss", model=vera, checks=checks),
        ),
    )


def state(
    refs: dict[str, str] | None = None,
    payloads: tuple[dict[str, Any], ...] | None = None,
    *,
    prices: tuple[Price, ...] | None = None,
    adp: Adapter | None = None,
    organisation: OrgGraph | None = None,
    regions: tuple[str, ...] = (),
    processing: dict[str, str] | None = None,
    fingerprints: dict[Any, str] | None = None,
    overrides: tuple[PriceOverride, ...] = (),
) -> State:
    """`refs` maps a ref to its family; `payloads` are the connection ledger payloads."""
    refs = refs if refs is not None else {AZ: "openai", AZ2: "meta", ANT: "claude"}
    procs = processing or {}
    entries = tuple(entry(r, f, procs.get(r, "global")) for r, f in refs.items())
    chosen = payloads if payloads is not None else (AZ_CONN, ANT_CONN)
    events = tuple(event(i + 1, **p) for i, p in enumerate(chosen))
    book = (
        prices if prices is not None else tuple(price(r) for r in refs if not r.startswith("fake/"))
    )
    return State(
        org=organisation or org(),
        catalog=ModelCatalog(entries=entries, customer_regions=regions),
        connections=fold_connections(events),
        prices=PriceBook(prices=book),
        overrides=overrides,
        adapter=adp or adapter(),
        data_class="synthetic",
        at=AT,
        fingerprints=fingerprints or {},
    )


def refused(result: Resolution | Refusal) -> Refusal:
    assert isinstance(result, Refusal), result
    return result


def resolved(result: Resolution | Refusal) -> Resolution:
    assert isinstance(result, Resolution), result
    return result


# -- the value types are immutable ----------------------------------------------------------------


def test_every_record_is_frozen() -> None:
    st = state()
    block = Block("r", "d", "s", "p", "m", None, "role")
    objects: list[Any] = [
        adapter(),
        Halt(),
        st,
        resolved(resolve_ref(st, AZ, check_price=True)),
        Refusal("r", "d"),
        block,
        Authored(AZ, "ada"),
        GatePair("vera", "ada", "t1", "g"),
        PlanTask("t", "ada", ()),
        Report((), ()),
    ]
    for obj in objects:
        field = dataclasses.fields(obj)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, None)


def test_the_stage_literal_names_start_and_call() -> None:
    assert get_args(Stage) == ("start", "call")


# -- small pure helpers ---------------------------------------------------------------------------


def test_utc_text_converts_to_utc_and_uses_the_z_form() -> None:
    local = datetime(2026, 10, 26, 5, 30, 9, tzinfo=timezone(timedelta(hours=-7)))
    assert utc_text(local) == "2026-10-26T12:30:09Z"


def test_split_ref_keeps_the_endpoint_id_inside_the_model_part() -> None:
    assert split_ref(OAC) == ("openai-compatible", "lab/m1")
    assert split_ref(AZ) == ("azure-openai", "gpt-x")
    assert split_ref("nonsense") == ("", "nonsense")


def test_the_adapter_lists_every_provider_and_names_the_auth_of_each_kind() -> None:
    adp = adapter()
    assert adp.supported == ("azure-openai", "anthropic", "openai-compatible")
    assert adp.auth_of("anthropic") == "key_file"
    assert adp.auth_of("azure-openai") == "entra"


def test_authors_are_distinct_models_in_order_of_first_occurrence() -> None:
    first, again, other = Authored(AZ, "ada"), Authored(AZ, "erin"), Authored(AZ2, "bob")
    assert authors_from_records([first, again, other, again]) == [first, other]
    assert authors_from_records([]) == []


# -- refusal texts and reasons of resolve_ref -----------------------------------------------------


def test_an_unsplittable_openai_compatible_ref_is_an_endpoint_refusal_with_the_parse_text() -> None:
    got = refused(resolve_ref(state(), "openai-compatible/lab", check_price=False))
    assert (got.reason, got.detail) == ("endpoint_invalid", U_ENDPOINT_NEEDED)


def test_an_unsupported_provider_lists_the_supported_ones() -> None:
    st = state({"other/m": "x"}, prices=())
    got = refused(resolve_ref(st, "other/m", check_price=False))
    assert got.reason == "provider_unsupported"
    assert got.detail == (
        "provider 'other' has no adapter in this version "
        "(supported providers: azure-openai, anthropic, openai-compatible)"
    )


def test_a_provider_the_adapter_cannot_use_here_is_refused_with_its_fixed_text() -> None:
    st = state(adp=adapter(provider_problem=lambda p: f"{p}: hosted needs a secret store"))
    got = refused(resolve_ref(st, AZ, check_price=False))
    assert (got.reason, got.detail) == (
        "endpoint_invalid",
        "azure-openai: hosted needs a secret store",
    )


def test_a_key_file_provider_must_process_globally() -> None:
    st = state(processing={ANT: "canada"})
    got = refused(resolve_ref(st, ANT, check_price=False))
    assert got.reason == "residency_refused"
    assert (
        got.detail == "provider 'anthropic' processes data globally; an entry cannot declare canada"
    )
    assert isinstance(resolve_ref(state(), ANT, check_price=False), Resolution)


def test_a_key_file_provider_is_refused_when_the_data_location_is_restricted() -> None:
    st = state(regions=("ca-west", "ca-east"))
    got = refused(resolve_ref(st, ANT, check_price=False))
    assert got.reason == "residency_refused"
    assert got.detail == (
        "the organization's data location is restricted to ca-east, ca-west; "
        "provider 'anthropic' processes data globally"
    )


def test_no_connection_text_differs_for_an_endpoint_provider() -> None:
    st = state(payloads=())
    oac = refused(resolve_ref(state({OAC: "local"}, (), prices=()), OAC, check_price=False))
    assert (oac.reason, oac.detail) == (
        "no_connection",
        "openai-compatible: the connection of this model does not exist",
    )
    plain = refused(resolve_ref(st, AZ, check_price=False))
    assert plain.reason == "no_connection"
    assert plain.detail == (
        "no connection for provider 'azure-openai': an admin connects it "
        "(PUT /v1/models/connections/azure-openai, or the Models screen in the app)"
    )


def test_a_disabled_connection_text_differs_for_an_endpoint_provider() -> None:
    oac = state({OAC: "local"}, ({**OAC_CONN, "status": "disabled"},), prices=())
    got = refused(resolve_ref(oac, OAC, check_price=False))
    assert (got.reason, got.detail) == (
        "connection_disabled",
        "openai-compatible: the connection is disabled",
    )
    plain = state(payloads=({**AZ_CONN, "status": "disabled"},))
    got = refused(resolve_ref(plain, AZ, check_price=False))
    assert (got.reason, got.detail) == (
        "connection_disabled",
        "the connection for provider 'azure-openai' is disabled",
    )


def test_an_invalid_last_change_is_named_as_such() -> None:
    st = state(payloads=({**AZ_CONN, "endpoint": "http://plain.example.test"},))
    got = refused(resolve_ref(st, AZ, check_price=False))
    assert (got.reason, got.detail) == (
        "endpoint_invalid",
        "the connection for provider 'azure-openai' cannot be used: its last change is not valid",
    )


def test_a_credential_type_that_does_not_match_the_provider_is_refused() -> None:
    entra_for_a_key_file_provider = state(payloads=({**ANT_CONN, "auth": "entra"},))
    got = refused(resolve_ref(entra_for_a_key_file_provider, ANT, check_price=False))
    assert got.detail == (
        "the connection for provider 'anthropic' cannot be used: "
        "its credential type does not match the provider"
    )
    key_file_for_a_keyless_one = state(payloads=({**AZ_CONN, "auth": "key_file"},))
    got = refused(resolve_ref(key_file_for_a_keyless_one, AZ, check_price=False))
    assert "its credential type does not match the provider" in got.detail


def test_a_stored_key_is_accepted_for_a_provider_that_takes_one() -> None:
    stored = {**ANT_CONN, "auth": "stored", "key_ref": KEY_REF}
    got = resolved(resolve_ref(state(payloads=(stored,)), ANT, check_price=False))
    assert got.provider == "anthropic" and got.key_ref == KEY_REF


def test_a_stored_key_is_not_wired_for_openai_compatible_but_other_auth_resolves() -> None:
    stored = {**OAC_CONN, "auth": "stored", "key_ref": KEY_REF}
    st = state({OAC: "local"}, (stored,), prices=())
    got = refused(resolve_ref(st, OAC, check_price=False))
    assert (got.reason, got.detail) == (
        "endpoint_invalid",
        "the connection for provider 'openai-compatible' cannot be used: "
        "stored keys are not wired for openai-compatible yet",
    )
    fp = {("openai-compatible", "lab", "m1"): fingerprint_of(OAC_CONN)}
    ok = state({OAC: "local"}, (OAC_CONN,), prices=(), fingerprints=fp)
    assert isinstance(resolve_ref(ok, OAC, check_price=False), Resolution)


def test_an_openai_compatible_connection_changed_since_approval_is_refused() -> None:
    st = state({OAC: "local"}, (OAC_CONN,), prices=(), fingerprints={})
    got = refused(resolve_ref(st, OAC, check_price=False))
    assert (got.reason, got.detail) == (
        "connection_changed",
        "openai-compatible: the connection changed since this model was approved; approve it again",
    )
    stale = {("openai-compatible", "lab", "m1"): "0" * 64}
    got = refused(
        resolve_ref(
            state({OAC: "local"}, (OAC_CONN,), prices=(), fingerprints=stale),
            OAC,
            check_price=False,
        )
    )
    assert got.reason == "connection_changed"


def test_a_remote_openai_compatible_endpoint_can_only_declare_global_processing() -> None:
    fp = {("openai-compatible", "lab", "m1"): fingerprint_of(OAC_CONN)}
    st = state({OAC: "local"}, (OAC_CONN,), prices=(), fingerprints=fp, processing={OAC: "canada"})
    got = refused(resolve_ref(st, OAC, check_price=False))
    assert (got.reason, got.detail) == ("residency_refused", REMOTE_RESIDENCY)


def test_an_openai_compatible_resolution_carries_its_connection_facts() -> None:
    keyed = {**OAC_CONN, "auth": "key", "key": "lab-key"}
    fp = {("openai-compatible", "lab", "m1"): fingerprint_of(keyed)}
    got = resolved(
        resolve_ref(
            state({OAC: "local"}, (keyed,), prices=(), fingerprints=fp), OAC, check_price=False
        )
    )
    assert (got.endpoint_id, got.auth, got.key, got.key_ref) == ("lab", "key", "lab-key", None)
    assert (got.processing, got.endpoint, got.connection_seq) == (
        "global",
        "https://models.example.test/v1",
        1,
    )
    plain = resolved(resolve_ref(state(), AZ, check_price=False))
    assert (plain.auth, plain.key, plain.endpoint_id) == (None, None, None)


def test_a_local_model_has_no_processing_even_when_the_catalog_lists_it() -> None:
    st = state({"fake/m1": "fake", AZ: "openai"})
    got = resolved(resolve_ref(st, "fake/m1", check_price=False))
    assert got.local and got.processing is None and got.family == "fake"


# -- the credential of a key-file provider --------------------------------------------------------


def test_a_missing_key_file_credential_is_refused_with_how_to_fix_it() -> None:
    st = state(adp=adapter(credential=lambda p: f"{p}: the key file is missing"))
    got = refused(resolve_ref(st, ANT, check_price=False))
    assert got.reason == "credential_unavailable"
    assert got.detail == (
        "no usable API key file for provider 'anthropic': the key file is missing" + CREDENTIAL_HOW
    )


@pytest.mark.parametrize("reason", ["hosted needs a secret store", "NURAMESH_HOSTED must be set"])
def test_a_hosted_credential_refusal_has_no_key_file_hint(reason: str) -> None:
    st = state(adp=adapter(credential=lambda p: f"{p}: {reason}"))
    got = refused(resolve_ref(st, ANT, check_price=False))
    assert got.reason == "credential_unavailable"
    assert got.detail == f"no usable API key file for provider 'anthropic': {reason}"


def test_a_stored_key_ignores_the_key_file_setting_but_not_the_hosted_refusal() -> None:
    stored = {**ANT_CONN, "auth": "stored", "key_ref": KEY_REF}
    missing = state(payloads=(stored,), adp=adapter(credential=lambda p: f"{p}: file missing"))
    assert isinstance(resolve_ref(missing, ANT, check_price=False), Resolution)
    hosted = state(
        payloads=(stored,), adp=adapter(credential=lambda p: f"{p}: hosted needs a secret store")
    )
    assert refused(resolve_ref(hosted, ANT, check_price=False)).reason == "credential_unavailable"
    fine = state(payloads=(stored,), adp=adapter(credential=lambda _p: None))
    assert isinstance(resolve_ref(fine, ANT, check_price=False), Resolution)


def test_the_credential_check_applies_only_to_key_file_providers() -> None:
    st = state(adp=adapter(credential=lambda _p: "everything: broken"))
    assert isinstance(resolve_ref(st, AZ, check_price=False), Resolution)


# -- prices ---------------------------------------------------------------------------------------


def test_an_unknown_price_names_the_moment_and_never_guesses() -> None:
    got = refused(resolve_ref(state(prices=()), AZ, check_price=True))
    assert (got.reason, got.detail) == (
        "unknown_price",
        f"'{AZ}' has no price in effect at 2026-10-26T12:00:00Z; a price is never guessed",
    )


def test_a_free_price_that_no_longer_holds_has_its_own_text() -> None:
    loopback = {**OAC_CONN, "endpoint": "http://127.0.0.1:9090"}
    fp = {("openai-compatible", "lab", "m1"): fingerprint_of(loopback)}
    free = price(
        OAC,
        input_per_million=Decimal(0),
        output_per_million=Decimal(0),
        source=FREE_SOURCE,
        bound_endpoint="http://127.0.0.1:8080",
    )
    st = state({OAC: "local"}, (loopback,), prices=(free,), fingerprints=fp)
    got = refused(resolve_ref(st, OAC, check_price=True))
    assert (got.reason, got.detail) == (
        "unknown_price",
        "openai-compatible: the free price is not valid for this connection; declare it again",
    )


def test_a_price_override_resolves_as_provisional_and_a_listed_price_as_book() -> None:
    override = PriceOverride(
        provider="azure-openai",
        model="gpt-x",
        input_per_million=Decimal(1),
        output_per_million=Decimal(2),
        set_by="human:u_adm1",
        set_at=AT - timedelta(days=1),
        reason="provisional price",
    )
    st = state(prices=(), overrides=(override,))
    assert resolved(resolve_ref(st, AZ, check_price=True)).price_basis == "provisional"
    assert resolved(resolve_ref(state(), AZ, check_price=True)).price_basis == "book"
    assert resolved(resolve_ref(state(), AZ, check_price=False)).price_basis is None


# -- the start stage ------------------------------------------------------------------------------


def test_the_start_stage_checks_prices() -> None:
    report = preflight(state(prices=()), Halt())
    assert [b.reason for b in report.blocks] == ["unknown_price"] * 3


def test_halt_texts_depend_on_which_state_is_set() -> None:
    both = preflight(state(), Halt(halted=True, unreadable=True))
    assert both.blocks[0].detail == (
        "the organization is halted: no run starts and no model is called until a human lifts it"
    )
    unreadable = preflight(state(), Halt(unreadable=True))
    assert unreadable.blocks[0].detail == "the halt state could not be read: failing closed"
    assert Halt(unreadable=True).blocks and Halt(halted=True).blocks and not Halt().blocks


def test_role_only_models_are_resolved_after_the_agents_and_only_for_roles_without_agents() -> None:
    report = preflight(state(), Halt())
    assert report.ok
    assert [(s, r.ref) for s, r in report.resolutions] == [
        ("agent=ada", AZ),
        ("agent=vera", AZ2),
        ("role=ops", AZ),
    ]
    # `dev` has a default model but an agent holds the role; `idle` has neither
    assert all(s not in ("role=dev", "role=idle") for s, _ in report.resolutions)


def test_a_role_only_model_that_does_not_resolve_is_blocked_with_no_agent() -> None:
    st = state({AZ: "openai", AZ2: "meta", ANT: "claude"}, prices=(price(AZ2), price(ANT)))
    blocks = preflight(st, Halt()).blocks
    assert [(b.subject, b.reason, b.agent, b.role) for b in blocks] == [
        ("agent=ada", "unknown_price", "ada", "dev"),
        ("role=ops", "unknown_price", None, "ops"),
    ]


def test_a_declared_pairing_is_reported_with_the_declared_wording() -> None:
    same = {AZ: "x", AZ2: "x", ANT: "claude"}
    st = state(same, organisation=org(checks=("ada",)))
    blocks = preflight(st, Halt()).blocks
    assert len(blocks) == 1
    block = blocks[0]
    assert (block.reason, block.gate, block.task, block.checked) == (
        "family_conflict",
        None,
        None,
        "ada",
    )
    assert block.detail == (
        f"verifier 'vera' uses family 'x', the same as 'ada' ({AZ}) which it checks: {ND48_TEXT}"
    )
    assert block.text == f"model blocked [family_conflict] agent=vera: {block.detail}"


def test_a_gate_pairing_names_the_task_and_gate_in_its_text() -> None:
    same = {AZ: "x", AZ2: "x", ANT: "claude"}
    blocks = preflight(state(same), Halt(), plan=[PlanTask("t1", "ada", (("g", "vera"),))]).blocks
    assert [b.detail for b in blocks] == [
        f"verifier 'vera' uses family 'x', the same as 'ada' ({AZ}) whose task 't1' it checks "
        f"in gate 'g': {ND48_TEXT}"
    ]
    assert (blocks[0].task, blocks[0].gate) == ("t1", "g")


def test_recorded_authors_count_only_on_resume() -> None:
    families = {AZ: "openai", AZ2: "meta", ANT: "claude", "azure-openai/old": "meta"}
    st = state(families, prices=(price(AZ), price(AZ2), price(ANT)))
    plan = [PlanTask("t1", "ada", (("g", "vera"),))]
    recorded = {"t1": [Authored("azure-openai/old", "erin")]}
    assert preflight(st, Halt(), plan=plan, recorded=recorded).blocks == ()
    resumed = preflight(st, Halt(), plan=plan, recorded=recorded, resume=True).blocks
    assert [(b.reason, b.checked) for b in resumed] == [("family_conflict", "erin")]


def test_on_resume_the_worker_is_judged_even_when_the_record_names_other_models() -> None:
    same = {AZ: "x", AZ2: "x", ANT: "claude", "azure-openai/old": "meta"}
    st = state(same, prices=(price(AZ), price(AZ2), price(ANT)))
    plan = [PlanTask("t1", "ada", (("g", "vera"),))]
    recorded = {"t1": [Authored("azure-openai/old", "erin")]}
    blocks = preflight(st, Halt(), plan=plan, recorded=recorded, resume=True).blocks
    assert [(b.reason, b.checked) for b in blocks] == [("family_conflict", "ada")]


def test_a_gate_whose_worker_has_no_model_is_not_judged_and_does_not_fail() -> None:
    st = state(organisation=org(ada=None))
    plan = [PlanTask("t1", "boss", (("g", "vera"),))]  # `boss` has no model: it is never resolved
    assert preflight(st, Halt(), plan=plan).blocks == ()


def test_check_pairing_reports_a_missing_record_with_the_task_and_worker() -> None:
    block = check_pairing(
        state(),
        verifier="vera",
        verifier_ref=AZ2,
        worker="ada",
        authors=None,
        declared=False,
        task="t1",
        gate="g",
        worker_ref=AZ,
    )
    assert isinstance(block, Block)
    assert block.reason == "family_unknown" and block.checked == "ada"
    assert block.detail == (
        "no model call is recorded for task 't1' of 'ada' (assigned "
        f"{AZ}), so verifier 'vera' cannot be checked for independence (ND48)"
    )
