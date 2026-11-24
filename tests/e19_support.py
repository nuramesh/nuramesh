"""Shared helpers for the E19 tests: the worked examples of `spec/e19/scenarios.json` and builders
that turn their entries into ledger events (spec/e19/README.md section 14)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nuramesh.core.ledger import GENESIS_HASH, LedgerEvent
from nuramesh.core.money import Money

SCENARIOS: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "spec" / "e19" / "scenarios.json").read_text(encoding="utf-8")
)
DEFAULT_AT = "2026-11-01T00:00:00Z"


def at(text: str) -> datetime:
    """An instant written with `Z` or `+00:00`."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def cases(section: str) -> list[Any]:
    """The scenario cases of one section as `pytest.param`s named by their ids."""
    return [pytest.param(c, id=c["id"]) for c in SCENARIOS[section]]


def event(seq: int, stream: str, raw: dict[str, Any]) -> LedgerEvent:
    """A ledger event from a scenario entry. A missing actor is `system` (never a person); a cost
    that is not a usable amount (`NaN`, text) is kept raw, which a stored ledger could hold."""
    cost = raw.get("cost")
    fields: dict[str, Any] = {
        "stream": stream,
        "seq": seq,
        "type": raw["type"],
        "actor": raw.get("actor", "system"),
        "at": at(raw["at"]) if "at" in raw else at(DEFAULT_AT),
        "run_id": None,
        "task_id": None,
        "payload": raw.get("payload", {}),
        "cost": None,
        "cost_basis": None,
        "prev_hash": GENESIS_HASH,
        "hash": GENESIS_HASH,
    }
    if cost is None:
        return LedgerEvent(**fields)
    try:
        return LedgerEvent(**{**fields, "cost": Money.of(cost)})
    except Exception:
        return LedgerEvent.model_construct(**{**fields, "cost": cost})


def org_events(raws: list[dict[str, Any]], org: str = "acme") -> list[LedgerEvent]:
    """The `org:<id>` stream of a scenario."""
    return [event(i, f"org:{org}", r) for i, r in enumerate(raws, 1)]


def run_events(raws: list[dict[str, Any]]) -> list[LedgerEvent]:
    """The run entries of a scenario (one stream, `run:r1`)."""
    return [event(i, "run:r1", r) for i, r in enumerate(raws, 1)]
