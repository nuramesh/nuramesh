"""The admin writes of the model catalog, prices, regions and assignments (L5, ND92, spec 2).

Every write is a person's (`human:` actor), has a reason (3 to 500 characters) that is kept in the
content store while the ledger keeps its keyed hash, is checked against the **candidate** state with
the same functions the configuration uses (`core.model_catalog`), and appends nothing when it is
refused. A write that would change nothing appends nothing and answers `already`. The check and the
append are one store transaction, so two writers cannot both pass a check that only one may.

Three writes can make an already approved entry accept data it did not accept before, or send its
data somewhere new: an edit of `processing` that widens, a regions write that widens, and the
repoint of a connection (`engine.model_connections`). They pause the entry in the same transaction,
so it is unusable until an admin approves it again with a recorded approval (OWNER CONFIRM: data
location, ND45).

**Lock order:** this service's `lock` first, then the connection service's, then the store.

`ensure_seed` is how the team configuration's `models:` part becomes the catalog: once, in one
transaction, and only while the ledger has no applied catalog entry.
"""

import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from nuramesh.core.endpoints import is_loopback, normalise, residency_problem
from nuramesh.core.ledger import EventType
from nuramesh.core.model_catalog import (
    CATALOG_SEED_ACTOR,
    INSTANT,
    MAX_REGIONS,
    REGION_WRITE_ID,
    CatalogEntry,
    CatalogFold,
    ModelCatalog,
    accepted_classes,
    assignment_problems,
    effective_ref,
    effective_role_default,
    family_problems,
    price_problems,
    seed_payload,
)
from nuramesh.core.models import format_model_ref, parse_model_ref
from nuramesh.core.org import OPENAI_COMPATIBLE, OrgGraph
from nuramesh.core.people import is_human_id
from nuramesh.core.prices import (
    P_LOOPBACK,
    Price,
    PriceBook,
    PriceOverride,
    operator_source_problem,
)
from nuramesh.core.residency import ResidencyError, check_processing
from nuramesh.engine.content import (
    ContentError,
    ContentService,
    KeyDestroyedError,
    OrphanedContentError,
)
from nuramesh.engine.openai_compatible import endpoint_id_problem
from nuramesh.engine.org_ledger import LedgerView, OrgLedger, org_stream
from nuramesh.stores.base import EventDraft, Store

Problem = tuple[str, str]

REASON_TEXT = "a reason (3 to 500 characters) is required"
DRIFT_WARNING = (
    "warning: the models: section of the team configuration is ignored: the catalog in the ledger "
    "is in force (it was seeded from an earlier version of this section, or created through the "
    "admin API); change it with the admin API or nuramesh models pause|retire"
)


def ignored_warning(count: int) -> str:
    """The startup warning for catalog entries in the ledger that the fold did not apply."""
    return (
        f"warning: {count} model.catalog_* entries in the ledger are ignored "
        "(not applied by the fold)"
    )


class CatalogRefused(Exception):  # noqa: N818 (a refusal, said in words)
    """A write was refused and appended nothing. `status` is the HTTP status the API answers and
    `code` its problem code; `problems` are `(path, message)` pairs (spec section 2.6)."""

    def __init__(
        self, status: int, code: str, message: str, problems: Sequence[Problem] = ()
    ) -> None:
        self.status, self.code, self.message = status, code, message
        self.problems = list(problems) or [("$", message)]
        super().__init__(message)


@dataclass(frozen=True)
class CatalogResult:
    """What a write did. `status` is 201 when it created a record, else 200."""

    status: int
    already: bool = False
    events: tuple[str, ...] = ()
    ledger_seq: int | None = None
    entry: CatalogEntry | None = None
    previous: str | None = None
    affected: tuple[str, ...] = ()
    repaused: bool | tuple[str, ...] = False
    price: Price | None = None
    customer_regions: tuple[str, ...] | None = None
    assignment: dict[str, Any] | None = None
    paused_because: str | None = None


@dataclass(frozen=True)
class SeedStatus:
    """What `ensure_seed` found."""

    seeded: bool
    config_drift: bool
    ignored_events: int
    messages: tuple[str, ...] = field(default=())
    """The startup warnings to print or log (drift first, then ignored entries)."""


def _ref(provider: str, model: str, endpoint_id: str | None = None) -> str:
    return format_model_ref(provider, endpoint_id, model)


def _free(input_per_million: str, output_per_million: str) -> bool:
    try:
        return Decimal(input_per_million) == 0 and Decimal(output_per_million) == 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _ident(provider: str, endpoint_id: str | None, model: str) -> dict[str, str]:
    """The payload keys that name an entry (`endpoint_id` only when it has one)."""
    ident = {"provider": provider, "model": model}
    if endpoint_id is not None:
        ident["endpoint_id"] = endpoint_id
    return ident


def _endpoint_problems(provider: str, endpoint_id: str | None) -> list[Problem]:
    """The check order of spec openai-compatible 2.1 for the endpoint id of a catalog write."""
    code = endpoint_id_problem(provider, endpoint_id)
    return [] if code is None else [("$.endpoint_id", code)]


def subjects_using(
    org: OrgGraph, assignments: Mapping[tuple[str, str], str], ref: str
) -> list[str]:
    """`agent=<id>` for every agent whose effective model is `ref`, then `role=<id>` for every role
    with no agent whose default is `ref`, in organization order."""
    out = [f"agent={a.id}" for a in org.agents if effective_ref(org, assignments, a.id).ref == ref]
    held = {a.role for a in org.agents}
    out += [
        f"role={r.id}"
        for r in org.roles
        if r.id not in held and effective_role_default(org, assignments, r.id) == ref
    ]
    return out


def _slug_problems(body: Mapping[str, Any]) -> list[Problem]:
    out: list[Problem] = []
    provider, model = body.get("provider"), body.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return [("$.provider", "provider and model are required text")]
    endpoint_id = body.get("endpoint_id")
    if endpoint_id is not None and not isinstance(endpoint_id, str):
        return [("$.endpoint_id", "endpoint_id is text")]
    found = _endpoint_problems(provider, endpoint_id)
    if found:  # the check order of 2.1: the endpoint id rule before the name
        return found
    try:
        parse_model_ref(_ref(provider, model, endpoint_id))
        CatalogEntry.model_validate(
            {
                **_ident(provider, endpoint_id, model),
                "family": "x",
                "processing": "global",
            }
        )
    except (ValueError, ValidationError):
        shown = _ref(provider, model, endpoint_id)
        out.append(("$.model", f"'{shown}' is not a valid provider/model name"))
    return out


def residency_violations(
    view: LedgerView, endpoint_id: str | None = None, *, everywhere: bool = True
) -> list[str]:
    """Spec openai-compatible X16: the refs of the `openai-compatible` entries (any status) whose
    connection is remote and whose processing scope is not `global`, ordered by ref. Nothing is
    changed by listing them. With `everywhere` off, only the connection `endpoint_id`."""
    found: list[str] = []
    for entry in view.catalog.entries.values():
        if entry.provider != OPENAI_COMPATIBLE:
            continue
        if not everywhere and entry.endpoint_id != endpoint_id:
            continue
        record = view.connections.get(entry.provider, entry.endpoint_id).record
        if record is not None and residency_problem(record.endpoint, entry.processing):
            found.append(entry.ref)
    return sorted(found)


class CatalogService:
    """Admin writes of the catalog for one organization (module docstring)."""

    def __init__(
        self,
        store: Store,
        org: OrgGraph,
        ledger: OrgLedger,
        *,
        prices: PriceBook,
        overrides: tuple[PriceOverride, ...] = (),
        content: ContentService | None = None,
    ) -> None:
        self.store, self.org, self.ledger = store, org, ledger
        self.prices, self.overrides, self.content = prices, overrides, content
        self.lock = threading.Lock()
        self.stream = org_stream(org.id)

    # -- shared steps ----------------------------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the service lock around a keyed write; the transaction is opened inside it
        (R1.35, lock order: service locks, then the transaction, then the store's lock)."""
        with self.lock:
            yield

    @staticmethod
    def _person(actor: str) -> None:
        if not is_human_id(actor):
            raise CatalogRefused(403, "human_required", "this needs a person (a human actor)")

    @staticmethod
    def _reason(reason: str | None) -> str:
        text = (reason or "").strip()
        if not 3 <= len(text) <= 500:
            raise CatalogRefused(422, "invalid_request", REASON_TEXT, [("$.reason", REASON_TEXT)])
        return text

    def _capture(self, uow: Any, reason: str) -> str:
        if self.content is None:
            raise CatalogRefused(
                503,
                "content_unavailable",
                "a reason is stored in the content store, which isn't available: it isn't "
                "configured",
            )
        try:
            return self.content.capture_reason(uow, self.org.id, reason)
        except (KeyDestroyedError, OrphanedContentError):
            raise  # the route answers 409 org_key_destroyed / org_key_missing (R1.37 section 8)
        except ContentError as exc:
            raise CatalogRefused(
                503,
                "content_unavailable",
                f"a reason is stored in the content store, which isn't available: {exc}",
            ) from None

    def _book(self, view: LedgerView) -> PriceBook:
        return view.catalog.price_book(self.prices)

    def _missing(self, provider: str, model: str, endpoint_id: str | None = None) -> CatalogRefused:
        return CatalogRefused(
            404, "not_found", f"'{_ref(provider, model, endpoint_id)}' is not in the model catalog"
        )

    def _append(
        self,
        uow: Any,
        actor: str,
        reason: str,
        via: str,
        events: Sequence[tuple[EventType, dict[str, Any]]],
    ) -> list[int]:
        """Append the events with the reason's hash; returns their sequences."""
        reason_hash = self._capture(uow, reason)
        seqs: list[int] = []
        for kind, payload in events:
            written = uow.append(
                EventDraft(
                    stream=self.stream,
                    type=kind,
                    actor=actor,
                    payload={**payload, "reason_hash": reason_hash, "via": via},
                )
            )
            seqs.append(written.seq)
        return seqs

    def _residency(
        self, view: LedgerView, provider: str, endpoint_id: str | None, processing: str
    ) -> list[Problem]:
        """Spec openai-compatible 10, points 3 and 4: an entry of a remote connection takes only
        `global` processing. A connection that does not exist yet judges nothing here (the
        resolver is the backstop)."""
        if provider != OPENAI_COMPATIBLE:
            return []
        record = view.connections.get(provider, endpoint_id).record
        found = None if record is None else residency_problem(record.endpoint, processing)
        return [] if found is None else [("$.processing", found)]

    def _fingerprint(self, view: LedgerView, provider: str, endpoint_id: str | None) -> str | None:
        """The fingerprint of the connection an approval is given against, or None."""
        if provider != OPENAI_COMPATIBLE:
            return None
        record = view.connections.get(provider, endpoint_id).record
        return None if record is None else record.fingerprint

    def _free_binding(
        self, view: LedgerView, provider: str, endpoint_id: str | None
    ) -> tuple[tuple[str, int] | None, list[Problem]]:
        """Spec openai-compatible 12.3: a free price is declared only for a connection that
        exists, is active and is a literal loopback address; the service sets the bound endpoint
        and the epoch of the connection's facts."""
        connection = view.connections.get(provider, endpoint_id)
        record = connection.record
        if record is None or record.status != "active":
            return None, [("$", P_LOOPBACK)]
        if not is_loopback(record.endpoint) or connection.facts_seq is None:
            return None, [("$", P_LOOPBACK)]
        return (normalise(record.endpoint), connection.facts_seq), []

    def _effective_catalog(self, fold: CatalogFold) -> ModelCatalog:
        catalog = fold.catalog()
        return catalog if catalog is not None else ModelCatalog()

    # -- entries ---------------------------------------------------------------------------

    def add(
        self,
        *,
        actor: str,
        provider: str,
        model: str,
        endpoint_id: str | None = None,
        family: str,
        processing: str,
        reason: str | None,
        via: str = "api",
    ) -> CatalogResult:
        """Add a model, paused (an added model is unusable until it is approved)."""
        with self.lock, self.store.transaction() as uow:
            return self.add_in(
                uow,
                actor=actor,
                provider=provider,
                model=model,
                endpoint_id=endpoint_id,
                family=family,
                processing=processing,
                reason=reason,
                via=via,
            )

    def add_in(
        self,
        uow: Any,
        *,
        actor: str,
        provider: str,
        model: str,
        endpoint_id: str | None = None,
        family: str,
        processing: str,
        reason: str | None,
        via: str = "api",
    ) -> CatalogResult:
        """Add a model, paused (an added model is unusable until it is approved)."""
        self._person(actor)
        text = self._reason(reason)
        problems = _slug_problems(
            {"provider": provider, "endpoint_id": endpoint_id, "model": model}
        )
        try:
            check_processing(processing)
        except ResidencyError as exc:
            problems.append(("$.processing", str(exc)))
        try:
            entry = CatalogEntry.model_validate(
                {
                    **_ident(provider, endpoint_id, model),
                    "family": family,
                    "processing": processing,
                    "status": "paused",
                }
            )
        except (ValidationError, ValueError):
            entry = None
            if not any(p[0] == "$.family" for p in problems) and not _valid_slug(family):
                problems.append(("$.family", "family must be a lower-case id such as openai"))
        if problems or entry is None:
            raise CatalogRefused(422, "invalid_request", problems[0][1], problems)
        view = self.ledger.view(uow)
        problems = self._residency(view, provider, endpoint_id, processing)
        if problems:
            raise CatalogRefused(422, "invalid_request", problems[0][1], problems)
        if (provider, endpoint_id, model) in view.catalog.entries:
            raise CatalogRefused(
                409, "already_exists", f"'{entry.ref}' is already in the model catalog"
            )
        payload = {
            **_ident(provider, endpoint_id, model),
            "family": family,
            "processing": processing,
            "status": "paused",
        }
        (seq,) = self._append(
            uow, actor, text, via, [(EventType.MODEL_CATALOG_ENTRY_ADDED, payload)]
        )
        return CatalogResult(
            201,
            events=(EventType.MODEL_CATALOG_ENTRY_ADDED.value,),
            ledger_seq=seq,
            entry=entry,
        )

    def edit(
        self,
        *,
        actor: str,
        provider: str,
        model: str,
        endpoint_id: str | None = None,
        reason: str | None,
        family: str | None = None,
        processing: str | None = None,
        via: str = "api",
    ) -> CatalogResult:
        """Change the family and/or processing scope of an entry. A change of `processing` that
        lets an approved entry accept more data pauses it again."""
        with self.lock, self.store.transaction() as uow:
            return self.edit_in(
                uow,
                actor=actor,
                provider=provider,
                model=model,
                endpoint_id=endpoint_id,
                reason=reason,
                family=family,
                processing=processing,
                via=via,
            )

    def edit_in(
        self,
        uow: Any,
        *,
        actor: str,
        provider: str,
        model: str,
        endpoint_id: str | None = None,
        reason: str | None,
        family: str | None = None,
        processing: str | None = None,
        via: str = "api",
    ) -> CatalogResult:
        """Change the family and/or processing scope of an entry. A change of `processing` that
        lets an approved entry accept more data pauses it again."""
        self._person(actor)
        text = self._reason(reason)
        problems = _slug_problems(
            {"provider": provider, "endpoint_id": endpoint_id, "model": model}
        )
        if family is None and processing is None:
            problems.append(("$", "give family or processing to change"))
        if processing is not None:
            try:
                check_processing(processing)
            except ResidencyError as exc:
                problems.append(("$.processing", str(exc)))
        if family is not None and not _valid_slug(family):
            problems.append(("$.family", "family must be a lower-case id such as openai"))
        if problems:
            raise CatalogRefused(422, "invalid_request", problems[0][1], problems)
        view = self.ledger.view(uow)
        key = (provider, endpoint_id, model)
        current = view.catalog.entries.get(key)
        if current is None:
            raise self._missing(provider, model, endpoint_id)
        changes: dict[str, str] = {}
        if family is not None and family != current.family:
            changes["family"] = family
        if processing is not None and processing != current.processing:
            changes["processing"] = processing
        if not changes:
            return CatalogResult(200, already=True, entry=current)
        if "processing" in changes:
            problems = self._residency(view, provider, endpoint_id, changes["processing"])
            if problems:
                raise CatalogRefused(422, "invalid_request", problems[0][1], problems)
        changed = current.model_copy(update=changes)
        candidate = view.catalog.clone()
        candidate.entries[key] = changed
        base_problems = self._family(view.catalog)
        new = [p for p in self._family(candidate) if p not in base_problems]
        if new:
            raise CatalogRefused(422, "invalid_request", new[0][1], new)
        widens = current.status == "approved" and bool(
            set(accepted_classes(changed.processing, view.catalog.customer_regions))
            - set(accepted_classes(current.processing, view.catalog.customer_regions))
        )
        events: list[tuple[EventType, dict[str, Any]]] = [
            (
                EventType.MODEL_CATALOG_ENTRY_EDITED,
                {**_ident(provider, endpoint_id, model), **changes},
            )
        ]
        if widens:
            events.append(
                (
                    EventType.MODEL_CATALOG_PAUSED,
                    {
                        **_ident(provider, endpoint_id, model),
                        "previous": "approved",
                        "because": "processing_widened",
                    },
                )
            )
        seqs = self._append(uow, actor, text, via, events)
        final = changed.model_copy(update={"status": "paused"}) if widens else changed
        return CatalogResult(
            200,
            events=tuple(e[0].value for e in events),
            ledger_seq=seqs[0],
            entry=final,
            repaused=widens,
            paused_because="processing_widened" if widens else None,
        )

    def _family(
        self, fold: CatalogFold, assignments: Mapping[tuple[str, str], str] | None = None
    ) -> list[Problem]:
        catalog = self._effective_catalog(fold)
        _, used = assignment_problems(
            self.org, catalog, fold.assignments if assignments is None else assignments
        )
        return family_problems(self.org, used)

    def approve(
        self,
        *,
        actor: str,
        provider: str,
        model: str,
        reason: str | None,
        via: str = "api",
        endpoint_id: str | None = None,
    ) -> CatalogResult:
        """Approve an entry (a recorded approval: who, when, why). It needs a price."""
        with self.lock, self.store.transaction() as uow:
            return self.approve_in(
                uow,
                actor=actor,
                provider=provider,
                model=model,
                endpoint_id=endpoint_id,
                reason=reason,
                via=via,
            )

    def approve_in(
        self,
        uow: Any,
        *,
        actor: str,
        provider: str,
        model: str,
        endpoint_id: str | None = None,
        reason: str | None,
        via: str = "api",
    ) -> CatalogResult:
        """Approve an entry (a recorded approval: who, when, why). It needs a price."""
        self._person(actor)
        text = self._reason(reason)
        view = self.ledger.view(uow)
        key = (provider, endpoint_id, model)
        current = view.catalog.entries.get(key)
        if current is None:
            raise self._missing(provider, model, endpoint_id)
        recorded = self._fingerprint(view, provider, endpoint_id)
        if current.status == "approved" and (
            recorded is None or view.catalog.fingerprints.get(key) == recorded
        ):
            return CatalogResult(200, already=True, entry=current)
        if current.status == "retired":
            raise CatalogRefused(
                409,
                "retired",
                f"'{current.ref}' is retired: remove it and add it again to use it",
            )
        problems = self._residency(view, provider, endpoint_id, current.processing)
        if problems:
            raise CatalogRefused(422, "invalid_request", problems[0][1], problems)
        candidate = view.catalog.clone()
        approved = current.model_copy(update={"status": "approved"})
        candidate.entries[key] = approved
        catalog = self._effective_catalog(candidate)
        index = list(candidate.entries).index(key)
        found = [
            p
            for p in price_problems(catalog, self._book(view), self.overrides)
            if p[0] == f"$.models.entries[{index}]"
        ]
        if found:
            raise CatalogRefused(422, "invalid_request", found[0][1], found)
        payload: dict[str, Any] = {
            **_ident(provider, endpoint_id, model),
            "previous": current.status,
        }
        if recorded is not None:
            payload["fingerprint"] = recorded
        (seq,) = self._append(uow, actor, text, via, [(EventType.MODEL_CATALOG_APPROVED, payload)])
        return CatalogResult(
            200,
            events=(EventType.MODEL_CATALOG_APPROVED.value,),
            ledger_seq=seq,
            entry=approved,
            previous=current.status,
        )

    def pause(
        self,
        *,
        actor: str,
        provider: str,
        model: str,
        reason: str | None,
        via: str = "api",
        endpoint_id: str | None = None,
    ) -> CatalogResult:
        """Pause an entry at once (always allowed: it restricts). Lists who is affected."""
        return self._restrict(
            actor,
            provider,
            model,
            reason,
            via,
            "paused",
            EventType.MODEL_CATALOG_PAUSED,
            endpoint_id,
        )

    def retire(
        self,
        *,
        actor: str,
        provider: str,
        model: str,
        reason: str | None,
        via: str = "api",
        endpoint_id: str | None = None,
    ) -> CatalogResult:
        """Retire an entry for good (terminal). Lists who is affected."""
        return self._restrict(
            actor,
            provider,
            model,
            reason,
            via,
            "retired",
            EventType.MODEL_CATALOG_RETIRED,
            endpoint_id,
        )

    def pause_in(
        self,
        uow: Any,
        *,
        actor: str,
        provider: str,
        model: str,
        reason: str | None,
        via: str,
        endpoint_id: str | None = None,
    ) -> CatalogResult:
        """`pause` inside the caller's unit of work, with `lock` held (R1.35)."""
        return self._restrict_in(
            uow,
            actor,
            provider,
            model,
            reason,
            via,
            "paused",
            EventType.MODEL_CATALOG_PAUSED,
            endpoint_id,
        )

    def retire_in(
        self,
        uow: Any,
        *,
        actor: str,
        provider: str,
        model: str,
        reason: str | None,
        via: str,
        endpoint_id: str | None = None,
    ) -> CatalogResult:
        """`retire` inside the caller's unit of work, with `lock` held (R1.35)."""
        return self._restrict_in(
            uow,
            actor,
            provider,
            model,
            reason,
            via,
            "retired",
            EventType.MODEL_CATALOG_RETIRED,
            endpoint_id,
        )

    def _restrict(
        self,
        actor: str,
        provider: str,
        model: str,
        reason: str | None,
        via: str,
        status: str,
        kind: EventType,
        endpoint_id: str | None = None,
    ) -> CatalogResult:
        with self.lock, self.store.transaction() as uow:
            return self._restrict_in(
                uow, actor, provider, model, reason, via, status, kind, endpoint_id
            )

    def _restrict_in(
        self,
        uow: Any,
        actor: str,
        provider: str,
        model: str,
        reason: str | None,
        via: str,
        status: str,
        kind: EventType,
        endpoint_id: str | None = None,
    ) -> CatalogResult:
        self._person(actor)
        text = self._reason(reason)
        view = self.ledger.view(uow)
        current = view.catalog.entries.get((provider, endpoint_id, model))
        if current is None:
            raise self._missing(provider, model, endpoint_id)
        affected = tuple(subjects_using(self.org, view.catalog.assignments, current.ref))
        if current.status == status:
            return CatalogResult(200, already=True, entry=current, affected=affected)
        if current.status == "retired":
            raise CatalogRefused(409, "retired", f"'{current.ref}' is retired")
        (seq,) = self._append(
            uow,
            actor,
            text,
            via,
            [(kind, {**_ident(provider, endpoint_id, model), "previous": current.status})],
        )
        return CatalogResult(
            200,
            events=(kind.value,),
            ledger_seq=seq,
            entry=current.model_copy(update={"status": status}),
            previous=current.status,
            affected=affected,
        )

    def remove(
        self,
        *,
        actor: str,
        provider: str,
        model: str,
        reason: str | None,
        via: str = "api",
        endpoint_id: str | None = None,
    ) -> CatalogResult:
        """Remove a paused or retired entry that nothing is assigned to."""
        with self.lock, self.store.transaction() as uow:
            return self.remove_in(
                uow,
                actor=actor,
                provider=provider,
                model=model,
                endpoint_id=endpoint_id,
                reason=reason,
                via=via,
            )

    def remove_in(
        self,
        uow: Any,
        *,
        actor: str,
        provider: str,
        model: str,
        endpoint_id: str | None = None,
        reason: str | None,
        via: str = "api",
    ) -> CatalogResult:
        """Remove a paused or retired entry that nothing is assigned to."""
        self._person(actor)
        text = self._reason(reason)
        view = self.ledger.view(uow)
        current = view.catalog.entries.get((provider, endpoint_id, model))
        if current is None:
            raise self._missing(provider, model, endpoint_id)
        if current.status == "approved":
            message = f"'{current.ref}' must be paused or retired before it is removed"
            raise CatalogRefused(409, "not_paused", message, [("$", message)])
        using = subjects_using(self.org, view.catalog.assignments, current.ref)
        if using:
            message = (
                f"'{current.ref}' is still assigned to {', '.join(using)}; reassign them first"
            )
            raise CatalogRefused(409, "in_use", message, [("$", message)])
        (seq,) = self._append(
            uow,
            actor,
            text,
            via,
            [
                (
                    EventType.MODEL_CATALOG_ENTRY_REMOVED,
                    {**_ident(provider, endpoint_id, model), "previous": current.status},
                )
            ],
        )
        return CatalogResult(
            200,
            events=(EventType.MODEL_CATALOG_ENTRY_REMOVED.value,),
            ledger_seq=seq,
            entry=current,
            previous=current.status,
        )

    # -- prices ----------------------------------------------------------------------------

    def set_price(
        self,
        *,
        actor: str,
        provider: str,
        model: str,
        endpoint_id: str | None = None,
        input_per_million: str,
        output_per_million: str,
        effective_from: str,
        source: str,
        reason: str | None,
        cached_input_per_million: str | None = None,
        cache_write_per_million: str | None = None,
        via: str = "api",
    ) -> CatalogResult:
        """Add one price version (never an edit: a duplicate `effective_from` is refused)."""
        with self.lock, self.store.transaction() as uow:
            return self.set_price_in(
                uow,
                actor=actor,
                provider=provider,
                model=model,
                endpoint_id=endpoint_id,
                input_per_million=input_per_million,
                output_per_million=output_per_million,
                effective_from=effective_from,
                source=source,
                reason=reason,
                cached_input_per_million=cached_input_per_million,
                cache_write_per_million=cache_write_per_million,
                via=via,
            )

    def set_price_in(
        self,
        uow: Any,
        *,
        actor: str,
        provider: str,
        model: str,
        endpoint_id: str | None = None,
        input_per_million: str,
        output_per_million: str,
        effective_from: str,
        source: str,
        reason: str | None,
        cached_input_per_million: str | None = None,
        cache_write_per_million: str | None = None,
        via: str = "api",
    ) -> CatalogResult:
        """Add one price version (never an edit: a duplicate `effective_from` is refused)."""
        self._person(actor)
        text = self._reason(reason)
        problems = _slug_problems(
            {"provider": provider, "endpoint_id": endpoint_id, "model": model}
        )
        view = self.ledger.view(uow)
        bound: tuple[str, int] | None = None
        if not problems and _free(input_per_million, output_per_million):
            bound, free_problems = self._free_binding(view, provider, endpoint_id)
            problems += free_problems
        if not isinstance(effective_from, str) or not INSTANT.fullmatch(effective_from):
            problems.append(
                (
                    "$.effective_from",
                    "effective_from must be an ISO 8601 UTC instant like 2026-03-01T00:00:00Z",
                )
            )
        if isinstance(source, str) and (bad := operator_source_problem(source)):
            problems.append(("$.source", bad))
        price: Price | None = None
        if not problems:
            try:
                price = Price.model_validate(
                    {
                        **_ident(provider, endpoint_id, model),
                        "bound_endpoint": None if bound is None else bound[0],
                        "bound_epoch": None if bound is None else bound[1],
                        "currency": "USD",
                        "input_per_million": input_per_million,
                        "output_per_million": output_per_million,
                        "cached_input_per_million": cached_input_per_million,
                        "cache_write_per_million": cache_write_per_million,
                        "effective_from": effective_from,
                        "source": source,
                    }
                )
            except (ValidationError, ValueError, InvalidOperation, TypeError) as exc:
                problems.append(("$", _price_message(exc)))
        if problems or price is None:
            raise CatalogRefused(422, "invalid_request", problems[0][1], problems)
        if (provider, endpoint_id, model) not in view.catalog.entries:
            raise self._missing(provider, model, endpoint_id)
        version = (provider, endpoint_id, model, price.effective_from)
        if any(
            (p.provider, p.endpoint_id, p.model, p.effective_from) == version
            for p in self._book(view).prices
        ):
            message = (
                f"a price version for '{_ref(provider, model, endpoint_id)}' effective from "
                f"{effective_from} already exists: prices are added, never edited; add a "
                "version with a later effective_from"
            )
            raise CatalogRefused(409, "price_version_exists", message, [("$", message)])
        payload = {
            **_ident(provider, endpoint_id, model),
            "currency": "USD",
            "input_per_million": _dec(price.input_per_million),
            "output_per_million": _dec(price.output_per_million),
            "cached_input_per_million": _dec_or_none(price.cached_input_per_million),
            "cache_write_per_million": _dec_or_none(price.cache_write_per_million),
            "effective_from": effective_from,
            "source": price.source,
            **({"bound_endpoint": price.bound_endpoint} if price.bound_endpoint else {}),
            **({"bound_epoch": price.bound_epoch} if price.bound_epoch else {}),
        }
        (seq,) = self._append(uow, actor, text, via, [(EventType.MODEL_CATALOG_PRICE_SET, payload)])
        return CatalogResult(
            201,
            events=(EventType.MODEL_CATALOG_PRICE_SET.value,),
            ledger_seq=seq,
            price=price,
        )

    # -- regions ---------------------------------------------------------------------------

    def set_regions(
        self, *, actor: str, customer_regions: Sequence[str], reason: str | None, via: str = "api"
    ) -> CatalogResult:
        """Replace the customer's chosen regions (a data-location rule, ND45). Approved
        region-bound entries that now accept customer data are paused again."""
        with self.lock, self.store.transaction() as uow:
            return self.set_regions_in(
                uow, actor=actor, customer_regions=customer_regions, reason=reason, via=via
            )

    def set_regions_in(
        self,
        uow: Any,
        *,
        actor: str,
        customer_regions: Sequence[str],
        reason: str | None,
        via: str = "api",
    ) -> CatalogResult:
        """Replace the customer's chosen regions (a data-location rule, ND45). Approved
        region-bound entries that now accept customer data are paused again."""
        self._person(actor)
        text = self._reason(reason)
        problems: list[Problem] = [
            (f"$.customer_regions[{i}]", f"'{value}' is not a lower-case region id")
            for i, value in enumerate(customer_regions)
            if not isinstance(value, str) or not REGION_WRITE_ID.fullmatch(value)
        ]
        unique = tuple(sorted(set(customer_regions))) if not problems else ()
        if not problems and len(unique) > MAX_REGIONS:
            problems.append(("$.customer_regions", f"at most {MAX_REGIONS} regions are allowed"))
        if problems:
            raise CatalogRefused(422, "invalid_request", problems[0][1], problems)
        view = self.ledger.view(uow)
        old = view.catalog.customer_regions
        if unique == old:
            return CatalogResult(200, already=True, customer_regions=old, repaused=())
        widened = [
            entry
            for entry in view.catalog.entries.values()
            if entry.status == "approved"
            and set(accepted_classes(entry.processing, unique))
            - set(accepted_classes(entry.processing, old))
        ]
        events: list[tuple[EventType, dict[str, Any]]] = [
            (EventType.MODEL_CATALOG_REGIONS_SET, {"customer_regions": list(unique)})
        ]
        for entry in widened:
            events.append(
                (
                    EventType.MODEL_CATALOG_PAUSED,
                    {
                        **_ident(entry.provider, entry.endpoint_id, entry.model),
                        "previous": "approved",
                        "because": "regions_widened",
                    },
                )
            )
        seqs = self._append(uow, actor, text, via, events)
        return CatalogResult(
            200,
            events=tuple(e[0].value for e in events),
            ledger_seq=seqs[0],
            customer_regions=unique,
            repaused=tuple(e.ref for e in widened),
            paused_because="regions_widened" if widened else None,
        )

    # -- assignments -----------------------------------------------------------------------

    def assign(
        self,
        *,
        actor: str,
        scope: str,
        subject: str,
        model: str | None,
        reason: str | None,
        via: str = "api",
    ) -> CatalogResult:
        """Assign a catalog model to an agent or role (None clears the assignment)."""
        with self.lock, self.store.transaction() as uow:
            return self.assign_in(
                uow, actor=actor, scope=scope, subject=subject, model=model, reason=reason, via=via
            )

    def assign_in(
        self,
        uow: Any,
        *,
        actor: str,
        scope: str,
        subject: str,
        model: str | None,
        reason: str | None,
        via: str = "api",
    ) -> CatalogResult:
        """Assign a catalog model to an agent or role (None clears the assignment)."""
        self._person(actor)
        text = self._reason(reason)
        if scope not in ("agent", "role"):
            raise CatalogRefused(
                422,
                "invalid_request",
                "scope is agent or role",
                [("$.scope", "scope is agent or role")],
            )
        known = (
            {a.id for a in self.org.agents} if scope == "agent" else {r.id for r in self.org.roles}
        )
        if subject not in known:
            message = f"no {scope} '{subject}' in the organization"
            raise CatalogRefused(422, "invalid_request", message, [("$.id", message)])
        if model is not None:
            try:
                parse_model_ref(model)
            except ValueError as exc:
                raise CatalogRefused(
                    422, "invalid_request", str(exc), [("$.model", str(exc))]
                ) from None
        view = self.ledger.view(uow)
        assignments = dict(view.catalog.assignments)
        if assignments.get((scope, subject)) == model:
            return CatalogResult(
                200, already=True, assignment={"scope": scope, "id": subject, "model": model}
            )
        if scope == "agent":
            before = effective_ref(self.org, assignments, subject).ref
        else:
            before = effective_role_default(self.org, assignments, subject)
        candidate = dict(assignments)
        if model is None:
            candidate.pop((scope, subject), None)
        else:
            candidate[(scope, subject)] = model
        catalog = self._effective_catalog(view.catalog)
        base_assign, base_used = assignment_problems(self.org, catalog, assignments)
        new_assign, new_used = assignment_problems(self.org, catalog, candidate)
        new = [p for p in new_assign if p not in base_assign]
        new += [
            p
            for p in family_problems(self.org, new_used)
            if p not in family_problems(self.org, base_used)
        ]
        if new:
            raise CatalogRefused(422, "invalid_request", new[0][1], new)
        (seq,) = self._append(
            uow,
            actor,
            text,
            via,
            [
                (
                    EventType.MODEL_ASSIGNMENT_SET,
                    {"scope": scope, "id": subject, "model": model, "previous": before},
                )
            ],
        )
        return CatalogResult(
            201,
            events=(EventType.MODEL_ASSIGNMENT_SET.value,),
            ledger_seq=seq,
            previous=before,
            assignment={"scope": scope, "id": subject, "model": model},
        )


def _valid_slug(value: str) -> bool:
    try:
        CatalogEntry.model_validate(
            {"provider": "x", "model": "x", "family": value, "processing": "global"}
        )
    except (ValidationError, ValueError):
        return False
    return True


def _dec(value: Decimal) -> str:
    return format(value, "f")


def _dec_or_none(value: Decimal | None) -> str | None:
    return None if value is None else _dec(value)


def _price_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        field_name = ".".join(str(p) for p in first["loc"])
        return f"{field_name}: {first['msg']}"
    return "the price is not valid"


def ensure_seed(
    store: Store,
    org_id: str,
    models: ModelCatalog | None,
    *,
    ledger: OrgLedger | None = None,
    base_prices: PriceBook | None = None,
) -> SeedStatus:
    """Seed the catalog from the configuration's `models:` part (module docstring). Returns what
    was found, with the startup warnings the caller prints or logs."""
    reader = ledger or OrgLedger(store, org_id, base_prices)
    stream = org_stream(org_id)
    seeded = False
    with store.transaction() as uow:
        view = reader.view(uow)
        if models is not None and view.catalog.applied == 0:
            uow.append(
                EventDraft(
                    stream=stream,
                    type=EventType.MODEL_CATALOG_SEEDED,
                    actor=CATALOG_SEED_ACTOR,
                    payload=seed_payload(models),
                )
            )
            seeded = True
    view = reader.view()
    fold = view.catalog
    drift = False
    if models is not None and not seeded:
        digest = seed_payload(models)["config_hash"]
        drift = (fold.seeded and fold.config_hash != digest) or (fold.exists and not fold.seeded)
    messages: list[str] = []
    if drift:
        messages.append(DRIFT_WARNING)
    if fold.ignored:
        messages.append(ignored_warning(fold.ignored))
    return SeedStatus(seeded, drift, fold.ignored, tuple(messages))
