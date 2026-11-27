"""The catalog fold rules (L5 spec section 2.2), one rule per test. The worked cases are in
`tests/engine/test_model_governance_cases.py`; these pin the rules one at a time."""

from datetime import UTC, datetime
from typing import Any

from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_catalog import (
    CatalogFold,
    apply_catalog_event,
    fold_catalog,
    new_fold,
)
from nuramesh.core.prices import Price, PriceBook

AT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
ADMIN = "human:u_adm1"
SEED = "system:catalog-seed"
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


def seed(n: int = 1, status: str = "approved", actor: str = SEED) -> LedgerEvent:
    body = {
        "source": "config",
        "config_hash": "x",
        "customer_regions": [],
        "entries": [{**ENTRY, "status": status}],
    }
    return ev(n, "model.catalog_seeded", actor, body)


def status(fold: CatalogFold) -> str | None:
    found = fold.entries.get(("azure-openai", None, "gpt-5.4-mini"))
    return found.status if found else None


def test_only_a_person_changes_a_catalog_entry() -> None:
    events = [
        seed(),
        ev(2, "model.catalog_paused", "agent:developer", {**MINI, "previous": "approved", **H}),
    ]
    fold = fold_catalog(events)
    assert status(fold) == "approved" and fold.ignored == 1 and fold.applied == 1
    events.append(ev(3, "model.catalog_paused", ADMIN, {**MINI, "previous": "approved", **H}))
    assert status(fold_catalog(events)) == "paused"


def test_the_seed_counts_once_only_from_the_seed_actor_and_only_first() -> None:
    assert fold_catalog([seed(actor=ADMIN)]).exists is False  # a person is not the seed
    fold = fold_catalog([seed(), seed(2, status="paused")])
    assert status(fold) == "approved" and fold.ignored == 1  # a second seed is ignored
    added = ev(1, "model.catalog_entry_added", ADMIN, {**ENTRY, "status": "paused", **H})
    assert fold_catalog([added, seed(2)]).seeded is False  # applied events come first: no seed


def test_retired_is_terminal_but_an_entry_can_be_removed_and_added_again() -> None:
    retire = ev(2, "model.catalog_retired", ADMIN, {**MINI, "previous": "approved", **H})
    back = ev(3, "model.catalog_approved", ADMIN, {**MINI, "previous": "retired", **H})
    pause = ev(4, "model.catalog_paused", ADMIN, {**MINI, "previous": "retired", **H})
    assert status(fold_catalog([seed(), retire, back, pause])) == "retired"
    gone = ev(5, "model.catalog_entry_removed", ADMIN, {**MINI, "previous": "retired", **H})
    add = ev(6, "model.catalog_entry_added", ADMIN, {**ENTRY, "status": "paused", **H})
    assert status(fold_catalog([seed(), retire, gone])) is None
    assert status(fold_catalog([seed(), retire, gone, add])) == "paused"


def test_a_status_change_needs_its_previous_and_an_entry_that_exists() -> None:
    no_previous = ev(2, "model.catalog_paused", ADMIN, {**MINI, **H})
    unknown = ev(
        3, "model.catalog_paused", ADMIN, {"provider": "x", "model": "y", "previous": "approved"}
    )
    fold = fold_catalog([seed(), no_previous, unknown])
    assert status(fold) == "approved" and fold.ignored == 2


def test_a_malformed_event_is_ignored_and_counted() -> None:
    bad_processing = ev(
        2,
        "model.catalog_entry_added",
        ADMIN,
        {**ENTRY, "model": "x", "processing": "mars", "status": "paused", **H},
    )
    approved_add = ev(
        3, "model.catalog_entry_added", ADMIN, {**ENTRY, "model": "y", "status": "approved", **H}
    )
    bad_regions = ev(4, "model.catalog_regions_set", ADMIN, {"customer_regions": ["Not Ok"], **H})
    fold = fold_catalog([seed(), bad_processing, approved_add, bad_regions])
    assert list(fold.entries) == [("azure-openai", None, "gpt-5.4-mini")] and fold.ignored == 3


def test_price_versions_are_added_never_edited_and_the_configuration_wins() -> None:
    price = {
        **MINI,
        "currency": "USD",
        "input_per_million": "1.5",
        "output_per_million": "6",
        "cached_input_per_million": None,
        "cache_write_per_million": None,
        "effective_from": "2026-03-01T00:00:00Z",
        "source": "vendor list",
        **H,
    }
    first = ev(2, "model.catalog_price_set", ADMIN, price)
    again = ev(3, "model.catalog_price_set", ADMIN, {**price, "input_per_million": "9"})
    fold = fold_catalog([seed(), first, again])
    assert len(fold.price_versions) == 1 and str(fold.price_versions[0].input_per_million) == "1.5"
    book = PriceBook(
        prices=(
            Price(
                provider="azure-openai",
                model="gpt-5.4-mini",
                input_per_million="1",  # type: ignore[arg-type]
                output_per_million="2",  # type: ignore[arg-type]
                effective_from=datetime(2026, 3, 1, tzinfo=UTC),
                source="the file",
            ),
        )
    )
    assert fold_catalog([seed(), first], base_prices=book).price_versions == []


def test_a_price_with_a_float_is_ignored() -> None:
    price = {
        **MINI,
        "currency": "USD",
        "input_per_million": 1.5,
        "output_per_million": "6",
        "effective_from": "2026-03-01T00:00:00Z",
        "source": "vendor list",
        **H,
    }
    assert (
        fold_catalog([seed(), ev(2, "model.catalog_price_set", ADMIN, price)]).price_versions == []
    )


def test_assignments_keep_the_latest_per_scope_and_null_clears() -> None:
    def assign(n: int, scope: str, subject: str, model: str | None) -> LedgerEvent:
        return ev(
            n,
            "model.assignment_set",
            ADMIN,
            {"scope": scope, "id": subject, "model": model, "previous": None, **H},
        )

    fold = fold_catalog(
        [
            assign(1, "agent", "developer", "azure-openai/gpt-5.4"),
            assign(2, "agent", "developer", "azure-openai/gpt-5.4-mini"),
            assign(3, "role", "dev", "azure-ai/m"),
            assign(4, "role", "dev", None),
        ]
    )
    assert fold.assignments == {("agent", "developer"): "azure-openai/gpt-5.4-mini"}
    assert fold.exists is False  # assignments alone are not a catalog


def test_apply_says_what_it_did() -> None:
    fold = new_fold()
    assert apply_catalog_event(fold, seed()) is True
    assert apply_catalog_event(fold, ev(2, "model.catalog_paused", "agent:x", {**MINI})) is False
    assert apply_catalog_event(fold, ev(3, "run.started", ADMIN, {})) is None


def test_regions_replace_the_list_sorted_as_written() -> None:
    fold = fold_catalog(
        [
            seed(),
            ev(
                2,
                "model.catalog_regions_set",
                ADMIN,
                {"customer_regions": ["ca-central", "ca-east"], **H},
            ),
        ]
    )
    assert fold.customer_regions == ("ca-central", "ca-east")
