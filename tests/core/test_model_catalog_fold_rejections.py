"""The catalog and connection folds refuse malformed ledger entries and leave the state as it was
(L5 spec section 2.2). Each test writes a bad entry after a good state and checks both that the
entry was counted as ignored and that nothing it asked for took effect."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_catalog import (
    ConnectionsState,
    ModelCatalog,
    apply_connection_event,
    fold_catalog,
    price_of,
)

AT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
ADMIN = "human:u_adm1"
MINI = {"provider": "azure-openai", "model": "gpt-5.4-mini"}
ENTRY = {**MINI, "family": "openai", "processing": "global"}
H = {"reason_hash": "h", "via": "api"}


def ev(n: int, kind: str, actor: str, payload: dict[str, Any]) -> LedgerEvent:
    return LedgerEvent.model_construct(
        stream="org:demo",
        seq=n,
        type=kind,
        actor=actor,
        at=AT,
        run_id=None,
        task_id=None,
        payload=payload,
        cost=None,
        cost_basis=None,
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def seed(n: int = 1) -> LedgerEvent:
    body = {
        "source": "config",
        "config_hash": "x",
        "customer_regions": [],
        "entries": [{**ENTRY, "status": "approved"}],
    }
    return ev(n, "model.catalog_seeded", "system:catalog-seed", body)


def status(fold: Any) -> str | None:
    found = fold.entries.get(("azure-openai", None, "gpt-5.4-mini"))
    return found.status if found else None


def ignored_after(*bad: Any) -> Any:
    """Fold the seed (one approved entry) and then `bad`; the seed alone is applied."""
    fold = fold_catalog([seed(), *bad])
    assert fold.applied == 1 and fold.ignored == len(bad)
    return fold


def test_a_catalog_with_a_badly_spelled_customer_region_is_refused() -> None:
    with pytest.raises(ValidationError, match="lowercase id"):
        ModelCatalog.model_validate({"customer_regions": ["Canada East"], "entries": []})


def test_a_paused_model_is_not_usable_and_the_error_says_its_status() -> None:
    catalog = ModelCatalog.model_validate({"entries": [{**ENTRY, "status": "paused"}]})
    with pytest.raises(LookupError, match="paused in the approved catalog"):
        catalog.require_usable("azure-openai", "gpt-5.4-mini", "internal")


def test_a_change_that_names_no_model_or_a_badly_named_one_is_ignored() -> None:
    no_model = ev(
        2, "model.catalog_paused", ADMIN, {"provider": "azure-openai", "previous": "approved"}
    )
    bad_name = ev(
        3,
        "model.catalog_paused",
        ADMIN,
        {"provider": "Azure OpenAI", "model": "m", "previous": "approved"},
    )
    assert status(ignored_after(no_model, bad_name)) == "approved"


def test_a_regions_write_that_is_not_a_list_changes_no_regions() -> None:
    fold = ignored_after(
        ev(2, "model.catalog_regions_set", ADMIN, {"customer_regions": "canadaeast", **H})
    )
    assert fold.customer_regions == ()


def test_a_price_with_a_malformed_effective_time_is_not_a_price() -> None:
    price = {
        "provider": "azure-openai",
        "model": "gpt-5.4-mini",
        "currency": "USD",
        "input_per_million": "1",
        "output_per_million": "2",
        "effective_from": "yesterday",
        "source": "list price",
    }
    assert price_of(price) is None
    assert (
        ignored_after(ev(2, "model.catalog_price_set", ADMIN, {**price, **H})).price_versions == []
    )


def test_an_edit_with_nothing_to_change_or_an_invalid_result_keeps_the_entry() -> None:
    nothing = ev(2, "model.catalog_entry_edited", ADMIN, {**MINI, **H})
    mars = ev(3, "model.catalog_entry_edited", ADMIN, {**MINI, "processing": "mars", **H})
    fold = ignored_after(nothing, mars)
    assert fold.entries[("azure-openai", None, "gpt-5.4-mini")].processing == "global"


@pytest.mark.parametrize(
    "body",
    [
        {"config_hash": "x", "customer_regions": [], "entries": "not a list"},
        {"config_hash": 7, "customer_regions": [], "entries": []},
        {"config_hash": "x", "customer_regions": "canadaeast", "entries": []},
        {"config_hash": "x", "customer_regions": [], "entries": [{**ENTRY, "processing": "mars"}]},
        {"config_hash": "x", "customer_regions": ["Not Ok"], "entries": []},
        {"config_hash": "x", "customer_regions": [], "entries": [5]},
    ],
)
def test_a_seed_that_does_not_parse_seeds_nothing_and_a_later_valid_seed_still_counts(
    body: dict[str, Any],
) -> None:
    bad = ev(1, "model.catalog_seeded", "system:catalog-seed", body)
    fold = fold_catalog([bad])
    assert fold.seeded is False and fold.entries == {} and fold.ignored == 1
    assert status(fold_catalog([bad, seed(2)])) == "approved"


@pytest.mark.parametrize(
    "body",
    [
        {"scope": "team", "id": "dana", "model": "azure-openai/gpt-5.4-mini"},
        {"scope": "agent", "model": "azure-openai/gpt-5.4-mini"},
        {"scope": "agent", "id": "Dana Smith", "model": "azure-openai/gpt-5.4-mini"},
        {"scope": "agent", "id": "dana", "model": 5},
        {"scope": "agent", "id": "dana", "model": "no-slash"},
    ],
)
def test_an_assignment_with_a_bad_scope_subject_or_model_changes_nothing(
    body: dict[str, Any],
) -> None:
    good = ev(
        2,
        "model.assignment_set",
        ADMIN,
        {"scope": "agent", "id": "dana", "model": "azure-openai/gpt-5.4-mini", **H},
    )
    fold = fold_catalog([seed(), good, ev(3, "model.assignment_set", ADMIN, {**body, **H})])
    assert fold.assignments == {("agent", "dana"): "azure-openai/gpt-5.4-mini"}


def test_a_connection_entry_without_a_provider_name_is_skipped() -> None:
    state = ConnectionsState()
    entry = ev(1, "model.connection_created", ADMIN, {"endpoint": "https://x.openai.azure.com"})
    assert apply_connection_event(state, entry) is state
    assert state.providers == {} and state.seq == 0
