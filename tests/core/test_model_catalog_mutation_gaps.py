"""Pins for core/model_catalog.py behaviours that the other catalog tests only check by truthiness
or not at all (mutation gaps, #1193): the exact True/False/None results of the folds, the retired
state, the free-price test and the approved-only lookup."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from nuramesh.core.endpoints import FREE_SOURCE
from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_catalog import (
    ConnectionsState,
    ModelCatalog,
    _is_free,
    _quiet,
    apply_catalog_event,
    apply_connection_event,
    fold_catalog,
    new_fold,
)
from nuramesh.core.prices import Price

AT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
ADMIN = "human:u_adm1"
MINI = {"provider": "azure-openai", "model": "gpt-5.4-mini"}
ENTRY = {**MINI, "family": "openai", "processing": "global"}
KEY = ("azure-openai", None, "gpt-5.4-mini")
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


def seeded_fold(status: str = "approved") -> Any:
    body = {
        "source": "config",
        "config_hash": "x",
        "customer_regions": [],
        "entries": [{**ENTRY, "status": status}],
    }
    fold = new_fold()
    assert apply_catalog_event(fold, ev(1, "model.catalog_seeded", "system:catalog-seed", body))
    return fold


def apply(fold: Any, kind: str, payload: dict[str, Any], n: int = 2) -> Any:
    return apply_catalog_event(fold, ev(n, kind, ADMIN, {**payload, **H}))


# -- _is_free (line 398: `and` must not become `or`) ---------------------------------------------


def _price(inp: str, out: str, **extra: Any) -> Price:
    return Price(
        provider="azure-openai",
        model="gpt-5.4-mini",
        input_per_million=Decimal(inp),
        output_per_million=Decimal(out),
        effective_from=AT,
        **{"source": "vendor list", **extra},
    )


def test_is_free_needs_both_rates_zero() -> None:
    assert _is_free(_price("0", "6")) is False
    assert _is_free(_price("1.5", "0")) is False
    assert _is_free(_price("1.5", "6")) is False
    free = _price("0", "0", source=FREE_SOURCE, bound_endpoint="http://127.0.0.1:8000")
    assert _is_free(free) is True


# -- exact results of the catalog fold (lines 469, 487, 493, 505, 520, 540, 544, 582) -----------


def test_adding_an_entry_twice_is_refused_with_false() -> None:
    fold = seeded_fold("paused")
    assert apply(fold, "model.catalog_entry_added", ENTRY) is False  # 469: key already there
    fold = new_fold()
    assert apply(fold, "model.catalog_entry_added", ENTRY) is True
    assert apply(fold, "model.catalog_entry_added", ENTRY, 3) is False


def test_editing_an_unknown_entry_returns_false() -> None:
    fold = seeded_fold()
    other = {"provider": "azure-openai", "model": "gpt-other", "family": "x"}
    assert apply(fold, "model.catalog_entry_edited", other) is False  # 487
    assert fold.entries[KEY].family == "openai"


def test_editing_without_changes_returns_false() -> None:
    fold = seeded_fold()
    assert apply(fold, "model.catalog_entry_edited", dict(MINI)) is False  # 493
    assert apply(fold, "model.catalog_entry_edited", {**MINI, "family": "other"}, 3) is True
    assert fold.entries[KEY].family == "other"


def test_an_edit_that_makes_the_entry_invalid_returns_false() -> None:
    fold = seeded_fold()
    result = apply(fold, "model.catalog_entry_edited", {**MINI, "processing": "mars"})
    assert result is False  # 493
    assert fold.entries[KEY].processing == "global"


def test_status_change_with_a_bad_previous_returns_false() -> None:
    fold = seeded_fold()
    assert apply(fold, "model.catalog_paused", {**MINI, "previous": "bogus"}) is False  # 505
    assert fold.entries[KEY].status == "approved"
    assert apply(fold, "model.catalog_paused", {**MINI, "previous": "approved"}, 3) is True
    assert fold.entries[KEY].status == "paused"


def test_a_retired_entry_cannot_be_approved_again() -> None:
    fold = seeded_fold("retired")
    result = apply(fold, "model.catalog_approved", {**MINI, "previous": "retired"})
    assert result is False  # 520: retired is terminal
    assert fold.entries[KEY].status == "retired"
    assert apply(fold, "model.catalog_paused", {**MINI, "previous": "retired"}, 3) is False
    assert apply(fold, "model.catalog_retired", {**MINI, "previous": "retired"}, 4) is True


def test_a_price_version_returns_true_and_a_repeat_false() -> None:
    price = {
        **MINI,
        "currency": "USD",
        "input_per_million": "1.5",
        "output_per_million": "6",
        "effective_from": "2026-03-01T00:00:00Z",
        "source": "vendor list",
    }
    fold = seeded_fold()
    assert apply(fold, "model.catalog_price_set", price) is True  # 540
    assert [str(p.input_per_million) for p in fold.price_versions] == ["1.5"]
    assert apply(fold, "model.catalog_price_set", price, 3) is False


def test_regions_set_returns_true_and_a_bad_list_false() -> None:
    fold = seeded_fold()
    assert apply(fold, "model.catalog_regions_set", {"customer_regions": ["eu", "ca"]}) is True
    assert fold.customer_regions == ("ca", "eu")  # 544
    assert apply(fold, "model.catalog_regions_set", {"customer_regions": "ca"}, 3) is False
    assert fold.customer_regions == ("ca", "eu")


def test_assignment_with_an_unknown_scope_is_refused_with_false() -> None:
    fold = new_fold()
    kind = "model.assignment_set"
    good = {"scope": "role", "id": "writer", "model": "azure-openai/gpt-5.4-mini"}
    assert apply(fold, kind, {**good, "scope": "team"}) is False  # 582
    assert apply(fold, kind, {**good, "id": None}, 3) is False
    assert apply(fold, kind, {**good, "id": "Bad Id"}, 5) is False  # 582: not a slug
    assert fold.assignments == {}
    assert apply(fold, kind, good, 4) is True
    assert fold.assignments == {("role", "writer"): "azure-openai/gpt-5.4-mini"}


def test_apply_returns_none_for_other_entries() -> None:
    assert apply_catalog_event(new_fold(), ev(1, "run.started", ADMIN, {})) is None


# -- _quiet (line 698: `and` must not become `or`) ---------------------------------------------


def test_quiet_lookup_returns_only_an_approved_entry() -> None:
    ref = "azure-openai/gpt-5.4-mini"

    def catalog(status: str) -> ModelCatalog:
        return ModelCatalog.model_validate({"entries": [{**ENTRY, "status": status}]})

    assert _quiet(catalog("approved"), ref) == catalog("approved").entries[0]
    assert _quiet(catalog("paused"), ref) is None
    assert _quiet(catalog("retired"), ref) is None
    assert _quiet(catalog("approved"), "azure-openai/not-listed") is None
    assert _quiet(catalog("approved"), None) is None


# -- connection fold returns the state itself (lines 998, 1019) ----------------------------------


def test_a_damaged_endpoint_id_returns_the_state_and_is_listed() -> None:
    state = ConnectionsState()
    entry = ev(
        7,
        "model.connection_created",
        ADMIN,
        {"provider": "openai-compatible", "endpoint_id": 5, "auth": "none", "endpoint": "x"},
    )
    assert apply_connection_event(state, entry) is state  # 998
    assert state.damaged == [7]
    assert state.seq == 7
    assert state.providers == {}


def test_an_invalid_connection_write_returns_the_state() -> None:
    state = ConnectionsState()
    bad = ev(
        4,
        "model.connection_created",
        ADMIN,
        {"provider": "azure-openai", "auth": "entra", "endpoint": "not a url", "status": "active"},
    )
    assert apply_connection_event(state, bad) is state  # 1019
    conn = state.get("azure-openai")
    assert conn.record is None and conn.invalid_seq == 4 and conn.ever is True


def test_fold_catalog_counts_applied_and_ignored() -> None:
    seed = ev(
        1,
        "model.catalog_seeded",
        "system:catalog-seed",
        {"source": "config", "config_hash": "x", "customer_regions": [], "entries": [ENTRY]},
    )
    bad = ev(2, "model.catalog_paused", ADMIN, {**MINI, "previous": "bogus", **H})
    fold = fold_catalog([seed, bad])
    assert (fold.applied, fold.ignored) == (1, 1)
