"""Migration 11 (R3.02a, ND90): budgets move to the key (organization, budget id).

The SQL stores run the same steps in their migration transaction (this file has no SQL):

1. read the `budget.defined` entries of `org:` streams, earliest first (time, stream, sequence);
2. the owner of a budget id is the organization of its earliest entry; none: unattributed (`None`);
   a run-scoped budget with no such entry belongs to the organization named by its run's
   `run.started` entry (the old rule R8); with neither: unattributed (fails closed);
3. each balance row is copied under (owner, id) with `org_id` in its data (every number, window
   and version unchanged); each reservation row gets the owner of its first budget as `org_id`;
4. the copied row count must equal the old one, or the migration fails and nothing changes.

Pure functions over strings and JSON: nothing here reads a database.
"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from nuramesh.core.budgets import org_key
from nuramesh.stores.base import StoreError


class MigrationError(StoreError):
    """The budget key migration found something it will not guess about; nothing was changed."""


def owners_from_definitions(definitions: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Budget id to organization from `(stream, payload JSON)` of `budget.defined` entries given
    earliest first; the first entry of an id wins. Streams that are not `org:<id>` are ignored."""
    owners: dict[str, str] = {}
    for stream, payload in definitions:
        if not stream.startswith("org:") or len(stream) == 4:
            continue
        found = json.loads(payload).get("budget")
        if isinstance(found, str) and found not in owners:
            owners[found] = stream[4:]
    return owners


def resolve_owners(
    definitions: Iterable[tuple[str, str]],
    balances: Iterable[str],
    run_started: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Budget id to organization: `owners_from_definitions`, then for a run-scoped balance with no
    definition entry the `org` of its run's first `run.started` entry (`run:<id>` stream, payload
    JSON). A run-scoped budget whose run has no such entry stays unowned (fails closed)."""
    owners = owners_from_definitions(definitions)
    runs: dict[str, str] = {}
    for stream, payload in run_started:
        org = json.loads(payload).get("org")
        if stream.startswith("run:") and isinstance(org, str) and org:
            runs.setdefault(stream[4:], org)
    for data in balances:
        budget = json.loads(data)["budget"]
        if budget["id"] not in owners and budget.get("scope") == "run":
            run_org = runs.get(str(budget.get("scope_id")))
            if run_org is not None:
                owners[budget["id"]] = run_org
    return owners


def balance_row(data: str, owners: Mapping[str, str]) -> tuple[str, str, str]:
    """`(organization key, budget id, data)` for a balance row: the budget gets its `org_id`."""
    body: dict[str, Any] = json.loads(data)
    budget = body["budget"]
    owner = owners.get(budget["id"])
    budget["org_id"] = owner
    return org_key(owner), str(budget["id"]), json.dumps(body, separators=(",", ":"))


def reservation_data(data: str, owners: Mapping[str, str]) -> str:
    """A reservation row's data with `org_id` set to the owner of its first budget (None if that
    budget is unattributed or the reservation names none)."""
    body: dict[str, Any] = json.loads(data)
    ids = body.get("budget_ids") or []
    if body.get("status") == "open" and len({owners.get(i) for i in ids}) > 1:
        raise MigrationError(
            f"open reservation '{body.get('id')}' holds money in budgets of more than one "
            "organization (the shared-id bug this migration fixes); settle or release it with the "
            "previous version first"
        )
    first = owners.get(ids[0]) if ids else None
    body["org_id"] = first
    # a closed reservation of the shared-id bug may hold money in budgets of two owners: each
    # budget keeps its own, so a late cost lands on the right budget (Reservation.budget_orgs)
    body["budget_orgs"] = [
        {"budget_id": i, "org_id": owners.get(i)} for i in ids if owners.get(i) != first
    ]
    return json.dumps(body, separators=(",", ":"))
