"""The approved model catalog, its ledger state, assignments and keyless connections (ND92, ND45,
ND48, A3).

**The catalog** says which models an organization may use. Each entry names a provider/model, its
**family** (OpenAI, Meta, ...), its **processing scope** (`core.residency`) and a status. The data
classes an entry accepts are **derived** from its processing scope by the ND45 rule, so a `global`
deployment can never be tagged for customer data.

**Where it lives (ND92).** The team configuration's `models:` part only *seeds* the catalog, once
(`seed_payload`). From then on the catalog is **state in the ledger**: `fold_catalog` folds the
`model.catalog_*` and `model.assignment_set` entries of the organization stream (`org:<org>`) in
order and every reader gets the same answer. Only a person (a `human:` actor) can change what a
model may do; the same entry from an agent, a service or a token is ignored by every reader. (An
actor id is a string: `human:` is a format test, not an authentication. The trust boundary is write
access to the store; a person's authentication is the API's.) An event that fails its own payload
validation is ignored too, so a ledger written around the service cannot make a malformed state
work.

**Assignments** (which catalog model an agent or role uses) are ledger events as well. The effective
model of an agent is, in order: the ledger assignment of the agent, the configuration's
`agent.model`, the ledger assignment of its role, the role's `default_model` (`effective_ref`).

**Rules** shared by the configuration and by every admin write (`price_problems`,
`assignment_problems`, `family_problems`; `catalog_problems` is all three): every assigned model is
an `approved` entry, an approved entry has a price, and a verifier's model is of a **different
family** than the model of every agent it checks (ND48). With no catalog there is no restriction in
a configuration file (existing configurations keep working); a *run* of a real model needs one.

**Connections** are the org admin's keyless provider settings: provider, endpoint, status. There is
no field for a secret anywhere: only providers that authenticate without a stored key may connect
(which providers, and which endpoints a token may be sent to, is decided outside core by the engine
adapter of that provider), and stored API keys are a separate roadmap item (R1.14c). The current
connections are the fold of the `model.connection_*` entries (`fold_connections`); usability is
judged by the resolver (`core.model_governance`), never by the fold.

Pure Python: stdlib and pydantic only.
"""

import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from nuramesh.core.endpoints import fingerprint, is_loopback, normalise
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.models import (
    ENDPOINT_NOT_ALLOWED,
    U_ENDPOINT_NEEDED,
    format_model_ref,
    parse_model_ref,
)
from nuramesh.core.org import OPENAI_COMPATIBLE, EndpointId, OrgGraph, RoleKind, Slug
from nuramesh.core.people import is_human_id
from nuramesh.core.prices import ModelName, Price, PriceBook, PriceOverride
from nuramesh.core.residency import (
    DATA_CLASSES,
    ResidencyError,
    check_processing,
    require_data_residency,
)

_HTTPS_HOST = re.compile(r"https://[a-z0-9][a-z0-9.-]{0,251}[a-z0-9](:[0-9]{1,5})?/?")
"""A plain https destination: no credentials, path or query. Which hosts a provider's token may go
to is decided by the engine adapter for that provider, not here (core is cloud-neutral)."""

Status = Literal["approved", "paused", "retired"]
ConnectionStatus = Literal["active", "disabled"]
_REGION_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
REGION_WRITE_ID = re.compile(r"[a-z][a-z0-9-]{1,39}")
"""A region id an admin may write (`PUT /v1/models/catalog/regions`)."""
MAX_REGIONS = 100
CATALOG_SEED_ACTOR = "system:catalog-seed"
"""The only actor whose `model.catalog_seeded` entry counts."""
INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
"""An effective_from written by an admin: `YYYY-MM-DDTHH:MM:SSZ`."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CatalogEntry(_Frozen):
    """One model an organization may use: who serves it, its family and where it is processed."""

    provider: Slug
    endpoint_id: EndpointId | None = None
    model: ModelName
    family: Slug
    processing: str
    status: Status = "approved"

    @model_validator(mode="after")
    def _endpoint_rule(self) -> Self:
        if self.endpoint_id is not None and self.provider != OPENAI_COMPATIBLE:
            raise ValueError(ENDPOINT_NOT_ALLOWED)
        if self.endpoint_id is None and self.provider == OPENAI_COMPATIBLE:
            raise ValueError(U_ENDPOINT_NEEDED)
        return self

    @field_validator("processing")
    @classmethod
    def _scope(cls, value: str) -> str:
        check_processing(value)  # ResidencyError is a ValueError: canada, global or region:<id>
        return value

    @property
    def ref(self) -> str:
        """The text an agent's model setting uses (`provider/model`, or with the endpoint id)."""
        return format_model_ref(self.provider, self.endpoint_id, self.model)

    @property
    def key(self) -> "_Key":
        """The identity of the entry: `(provider, endpoint_id, model)`."""
        return (self.provider, self.endpoint_id, self.model)


def accepted_classes(processing: str, customer_regions: Iterable[str]) -> tuple[str, ...]:
    """The data classes a deployment with this processing scope may receive (the ND45 rule)."""
    regions = tuple(customer_regions)
    allowed: list[str] = []
    for data_class in DATA_CLASSES:
        try:
            require_data_residency(processing, data_class, customer_regions=regions)
        except ResidencyError:
            continue
        allowed.append(data_class)
    return tuple(allowed)


class ModelCatalog(_Frozen):
    """The approved catalog (configuration part `models`, and the ledger's folded state)."""

    customer_regions: tuple[str, ...] = Field(default=(), max_length=MAX_REGIONS)
    entries: tuple[CatalogEntry, ...] = Field(default=(), max_length=1_000)

    @field_validator("customer_regions")
    @classmethod
    def _regions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for region in value:
            if not _REGION_ID.fullmatch(region):
                raise ValueError("a customer region is a lowercase id such as canadaeast")
        return value

    @model_validator(mode="after")
    def _unique(self) -> Self:
        seen: set[str] = set()
        for entry in self.entries:
            if entry.ref in seen:
                raise ValueError(f"'{entry.ref}' is listed twice in the catalog")
            seen.add(entry.ref)
        return self

    def find(
        self, provider: str, model: str, endpoint_id: str | None = None
    ) -> CatalogEntry | None:
        """The entry for a model (on this endpoint), or None. Two endpoints are two models."""
        for entry in self.entries:
            if (
                entry.provider == provider
                and entry.endpoint_id == endpoint_id
                and entry.model == model
            ):
                return entry
        return None

    def data_classes(self, entry: CatalogEntry) -> tuple[str, ...]:
        """The data classes this entry may receive, derived by the ND45 rule (never typed)."""
        return accepted_classes(entry.processing, self.customer_regions)

    def require_usable(
        self, provider: str, model: str, data_class: str, endpoint_id: str | None = None
    ) -> CatalogEntry:
        """The entry, if the model is approved and may receive `data_class`; else
        `LookupError` (not listed, or not approved) or `ResidencyError` (ND45)."""
        entry = self.find(provider, model, endpoint_id)
        if entry is None:
            raise LookupError(
                f"'{format_model_ref(provider, endpoint_id, model)}' is not in the approved catalog"
            )
        if entry.status != "approved":
            raise LookupError(f"'{entry.ref}' is {entry.status} in the approved catalog")
        require_data_residency(entry.processing, data_class, customer_regions=self.customer_regions)
        return entry


def seed_payload(models: ModelCatalog) -> dict[str, Any]:
    """The payload of the one `model.catalog_seeded` entry for a configuration's `models:` part."""
    dumped = models.model_dump(mode="json")
    for entry in dumped["entries"]:  # additive: an entry without an endpoint id hashes as before
        if entry.get("endpoint_id") is None:
            entry.pop("endpoint_id", None)
    digest = hashlib.sha256(
        json.dumps(dumped, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "source": "config",
        "config_hash": digest,
        "customer_regions": list(dumped["customer_regions"]),
        "entries": list(dumped["entries"]),
    }


# -- the catalog as ledger state -----------------------------------------------------------------

CATALOG_EVENT_TYPES = frozenset(
    {
        EventType.MODEL_CATALOG_SEEDED.value,
        EventType.MODEL_CATALOG_ENTRY_ADDED.value,
        EventType.MODEL_CATALOG_ENTRY_EDITED.value,
        EventType.MODEL_CATALOG_APPROVED.value,
        EventType.MODEL_CATALOG_PAUSED.value,
        EventType.MODEL_CATALOG_RETIRED.value,
        EventType.MODEL_CATALOG_ENTRY_REMOVED.value,
        EventType.MODEL_CATALOG_PRICE_SET.value,
        EventType.MODEL_CATALOG_REGIONS_SET.value,
    }
)
"""Every `model.catalog_*` type: the ones that count as catalog events (seeding, ignored count)."""

_Key = tuple[str, str | None, str]
_STATUSES = ("approved", "paused", "retired")


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _key_of(payload: Mapping[str, Any]) -> _Key | None:
    """`(provider, endpoint_id, model)` of a payload, or None unless all are valid."""
    provider, model = _text(payload, "provider"), _text(payload, "model")
    endpoint_id = payload.get("endpoint_id")
    if (
        provider is None
        or model is None
        or not (endpoint_id is None or isinstance(endpoint_id, str))
    ):
        return None
    try:
        CatalogEntry.model_validate(
            {
                "provider": provider,
                "endpoint_id": endpoint_id,
                "model": model,
                "family": "x",
                "processing": "global",
            }
        )
    except (ValueError, ValidationError):
        return None
    return provider, endpoint_id, model


def _regions_of(value: object) -> tuple[str, ...] | None:
    """A valid, sorted, de-duplicated list of region ids, or None."""
    if not isinstance(value, list | tuple):
        return None
    if not all(isinstance(v, str) and REGION_WRITE_ID.fullmatch(v) for v in value):
        return None
    unique = tuple(sorted(set(value)))
    return unique if len(unique) <= MAX_REGIONS else None


def _entry_of(raw: object, status: str | None = None) -> CatalogEntry | None:
    if not isinstance(raw, Mapping):
        return None
    data = dict(raw)
    if status is not None:
        data["status"] = status
    try:
        return CatalogEntry.model_validate(data)
    except (ValidationError, ValueError):
        return None


def price_of(payload: Mapping[str, Any]) -> Price | None:
    """The price version a `model.catalog_price_set` payload says, or None if it is not valid
    (floats are refused: a price is decimal text)."""
    fields = (
        "provider",
        "endpoint_id",
        "model",
        "currency",
        "input_per_million",
        "output_per_million",
        "cached_input_per_million",
        "cache_write_per_million",
        "effective_from",
        "source",
        "bound_endpoint",
        "bound_epoch",
    )
    data = {k: payload[k] for k in fields if k in payload}
    if not isinstance(data.get("effective_from"), str) or not INSTANT.fullmatch(
        data["effective_from"]
    ):
        return None
    try:
        return Price.model_validate(data)
    except (ValidationError, ValueError, TypeError, ArithmeticError):
        return None


@dataclass
class CatalogFold:
    """The folded catalog state. Build it with `fold_catalog`; do not edit it."""

    entries: dict[_Key, CatalogEntry] = field(default_factory=dict)
    origin: dict[_Key, str] = field(default_factory=dict)  # "seed" or "api"
    updated_by: dict[_Key, str] = field(default_factory=dict)
    updated_at: dict[_Key, datetime] = field(default_factory=dict)
    customer_regions: tuple[str, ...] = ()
    assignments: dict[tuple[str, str], str] = field(default_factory=dict)
    price_versions: list[Price] = field(default_factory=list)
    fingerprints: dict[_Key, str] = field(default_factory=dict)
    """The connection fingerprint each approved `openai-compatible` entry was approved against."""
    seeded: bool = False
    config_hash: str | None = None
    applied: int = 0
    """Honored `model.catalog_*` entries (the seed included)."""
    ignored: int = 0
    """`model.catalog_*` entries the fold did not apply."""
    seq: int = 0
    base_prices: frozenset[tuple[str, str | None, str, datetime]] = frozenset()

    @property
    def exists(self) -> bool:
        """Whether a catalog exists: some catalog event was applied."""
        return self.applied > 0

    def clone(self) -> "CatalogFold":
        """A copy that can be changed without touching this one (entries are immutable)."""
        return dataclasses.replace(
            self,
            entries=dict(self.entries),
            origin=dict(self.origin),
            updated_by=dict(self.updated_by),
            updated_at=dict(self.updated_at),
            assignments=dict(self.assignments),
            price_versions=list(self.price_versions),
            fingerprints=dict(self.fingerprints),
        )

    def catalog(self) -> ModelCatalog | None:
        """The effective catalog, or None when there is none."""
        if not self.exists:
            return None
        return ModelCatalog(
            customer_regions=self.customer_regions, entries=tuple(self.entries.values())
        )

    def price_book(
        self, base: PriceBook, connections: "ConnectionsState | None" = None
    ) -> PriceBook:
        """The configuration's price book plus the honored price versions.

        With `connections` (every consumer that prices a call passes them) a free price (both main
        rates 0) that no longer holds is removed together with every older version of the same
        model, so an invalid newest free price makes the model unpriced and never falls back to an
        older paid version (spec openai-compatible 12.3). A free price holds only while its
        connection is active, still loopback and still at the endpoint it was declared for."""
        book = PriceBook(prices=(*base.prices, *self.price_versions)).with_shipped(base.shipped)
        return book if connections is None else void_broken_free_prices(book, connections)


def void_broken_free_prices(book: PriceBook, connections: "ConnectionsState") -> PriceBook:
    """`book` without every free price that no longer holds. A voided free price still governs
    from its own `effective_from` (it blocks; it never falls back to an older paid version), but
    until that date the price in force today is used (spec openai-compatible 12.3)."""
    broken = [p for p in book.prices if _is_free(p) and not free_price_holds(p, connections)]
    if not broken:
        return book

    gone = {id(p) for p in broken}
    voided = PriceBook(prices=tuple(p for p in book.prices if id(p) not in gone)).with_shipped(
        book.shipped
    )
    times: dict[tuple[str, str | None, str], list[datetime]] = {}
    for b in broken:
        times.setdefault((b.provider, b.endpoint_id, b.model), []).append(b.effective_from)
    carried = {k: book.blocked_since(*k) for k in book.blocked}
    return voided.with_blocked(carried).with_blocked(times)


def _is_free(price: Price) -> bool:
    return price.input_per_million == 0 and price.output_per_million == 0


def free_price_holds(price: Price, connections: "ConnectionsState") -> bool:
    """Whether a free price still applies: its connection exists, is `active`, is a literal
    loopback address, its current normalised endpoint equals the price's `bound_endpoint` and,
    when the price carries one, the connection is still in the epoch (the same endpoint, auth and
    key name since that ledger entry) the price was declared in; a price with none (one from a
    configuration file) holds only for the connection's first epoch. A repoint, even one that
    comes back to the old address, voids it."""
    if price.bound_endpoint is None:
        return False
    connection = connections.get(price.provider, price.endpoint_id)
    record = connection.record
    return (
        record is not None
        and record.status == "active"
        and is_loopback(record.endpoint)
        and normalise(record.endpoint) == normalise(price.bound_endpoint)
        and (
            connection.epochs == 1
            if price.bound_epoch is None
            else connection.facts_seq == price.bound_epoch
        )
    )


def new_fold(base_prices: PriceBook | None = None) -> CatalogFold:
    """An empty fold; `base_prices` is the configuration's price book (a price version already
    there is not added twice)."""
    keys = frozenset(
        (p.provider, p.endpoint_id, p.model, p.effective_from)
        for p in (base_prices.prices if base_prices else ())
    )
    return CatalogFold(base_prices=keys)


def apply_catalog_event(fold: CatalogFold, event: LedgerEvent) -> bool | None:
    """Apply one ledger entry to `fold` (in place). Returns True if the entry was applied, False if
    it is a catalog entry the fold ignored, None if it is not a catalog or assignment entry."""
    kind = event.type
    is_catalog = kind in CATALOG_EVENT_TYPES
    if not is_catalog and kind != EventType.MODEL_ASSIGNMENT_SET.value:
        return None
    fold.seq = max(fold.seq, event.seq)
    ok = _apply(fold, event)
    if is_catalog:
        if ok:
            fold.applied += 1
        else:
            fold.ignored += 1
    return ok


def _apply(fold: CatalogFold, event: LedgerEvent) -> bool:  # noqa: C901 (one branch per type)
    kind, actor = event.type, event.actor
    payload: Mapping[str, Any] = event.payload
    if kind == EventType.MODEL_CATALOG_SEEDED.value:
        return _seed(fold, event)
    if not is_human_id(actor):
        return False
    entries = fold.entries
    stamp = (actor, event.at)

    def touch(key: _Key) -> None:
        fold.updated_by[key], fold.updated_at[key] = stamp

    if kind == EventType.MODEL_CATALOG_ENTRY_ADDED.value:
        key = _key_of(payload)
        status = payload.get("status", "paused")
        if key is None or key in entries or status != "paused":
            return False
        entry = _entry_of(
            {
                k: payload.get(k)
                for k in ("provider", "endpoint_id", "model", "family", "processing")
                if k != "endpoint_id" or k in payload
            },
            "paused",
        )
        if entry is None:
            return False
        entries[key] = entry
        fold.origin[key] = "api"
        touch(key)
        return True
    if kind == EventType.MODEL_CATALOG_ENTRY_EDITED.value:
        key = _key_of(payload)
        if key is None or key not in entries:
            return False
        changes = {k: payload[k] for k in ("family", "processing") if k in payload}
        if not changes:
            return False
        entry = _entry_of({**entries[key].model_dump(), **changes})
        if entry is None:
            return False
        entries[key] = entry
        touch(key)
        return True
    if kind in (
        EventType.MODEL_CATALOG_APPROVED.value,
        EventType.MODEL_CATALOG_PAUSED.value,
        EventType.MODEL_CATALOG_RETIRED.value,
        EventType.MODEL_CATALOG_ENTRY_REMOVED.value,
    ):
        key = _key_of(payload)
        if key is None or key not in entries or payload.get("previous") not in _STATUSES:
            return False
        current = entries[key]
        if kind == EventType.MODEL_CATALOG_ENTRY_REMOVED.value:
            del entries[key]
            fold.origin.pop(key, None)
            fold.updated_by.pop(key, None)
            fold.updated_at.pop(key, None)
            fold.fingerprints.pop(key, None)
            return True
        status = {
            EventType.MODEL_CATALOG_APPROVED.value: "approved",
            EventType.MODEL_CATALOG_PAUSED.value: "paused",
            EventType.MODEL_CATALOG_RETIRED.value: "retired",
        }[kind]
        if current.status == "retired" and status != "retired":
            return False  # retired is terminal
        entries[key] = current.model_copy(update={"status": status})
        recorded = payload.get("fingerprint")
        if status == "approved" and isinstance(recorded, str):
            fold.fingerprints[key] = recorded
        else:
            fold.fingerprints.pop(key, None)
        touch(key)
        return True
    if kind == EventType.MODEL_CATALOG_PRICE_SET.value:
        price = price_of(payload)
        if price is None:
            return False
        version = (price.provider, price.endpoint_id, price.model, price.effective_from)
        if version in fold.base_prices or any(
            (p.provider, p.endpoint_id, p.model, p.effective_from) == version
            for p in fold.price_versions
        ):
            return False
        fold.price_versions.append(price)
        return True
    if kind == EventType.MODEL_CATALOG_REGIONS_SET.value:
        regions = _regions_of(payload.get("customer_regions"))
        if regions is None:
            return False
        fold.customer_regions = regions
        return True
    return _assignment(fold, payload)


def _seed(fold: CatalogFold, event: LedgerEvent) -> bool:
    payload = event.payload
    if event.actor != CATALOG_SEED_ACTOR or fold.applied > 0:
        return False
    raw_entries = payload.get("entries")
    regions = payload.get("customer_regions", [])
    config_hash = payload.get("config_hash")
    if not isinstance(raw_entries, list | tuple) or not isinstance(config_hash, str):
        return False
    if not isinstance(regions, list | tuple):
        return False
    try:
        catalog = ModelCatalog.model_validate(
            {"customer_regions": list(regions), "entries": [dict(e) for e in raw_entries]}
        )
    except (ValidationError, ValueError, TypeError):
        return False
    fold.seeded, fold.config_hash = True, config_hash
    fold.customer_regions = catalog.customer_regions
    for entry in catalog.entries:
        key = entry.key
        fold.entries[key] = entry
        fold.origin[key] = "seed"
        fold.updated_by[key], fold.updated_at[key] = event.actor, event.at
    return True


def _assignment(fold: CatalogFold, payload: Mapping[str, Any]) -> bool:
    scope, subject, model = _text(payload, "scope"), _text(payload, "id"), payload.get("model")
    if scope not in ("agent", "role") or subject is None:
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", subject):
        return False
    if model is None:
        fold.assignments.pop((scope, subject), None)
        return True
    if not isinstance(model, str):
        return False
    try:
        parse_model_ref(model)
    except ValueError:
        return False
    fold.assignments[(scope, subject)] = model
    return True


def fold_catalog(
    events: Iterable[LedgerEvent], base_prices: PriceBook | None = None
) -> CatalogFold:
    """The catalog state the organization stream's entries add up to, in order."""
    fold = new_fold(base_prices)
    for event in events:
        apply_catalog_event(fold, event)
    return fold


# -- effective models and the rules ------------------------------------------------------------

EffectiveSource = Literal["agent ledger", "agent config", "role ledger", "role config", "none"]


@dataclass(frozen=True)
class EffectiveRef:
    """The model an agent uses now and where that setting comes from."""

    ref: str | None
    source: EffectiveSource


def effective_ref(
    org: OrgGraph, assignments: Mapping[tuple[str, str], str], agent_id: str
) -> EffectiveRef:
    """The effective `provider/model` of an agent: its ledger assignment, its configured model,
    its role's ledger assignment, the role's default (ND92 spec section 2.5)."""
    agent = org.agent(agent_id)
    ledger = assignments.get(("agent", agent_id))
    if ledger is not None:
        return EffectiveRef(ledger, "agent ledger")
    if agent.model is not None:
        return EffectiveRef(agent.model, "agent config")
    role_ledger = assignments.get(("role", agent.role))
    if role_ledger is not None:
        return EffectiveRef(role_ledger, "role ledger")
    default = org.role(agent.role).default_model
    if default is not None:
        return EffectiveRef(default, "role config")
    return EffectiveRef(None, "none")


def effective_role_default(
    org: OrgGraph, assignments: Mapping[tuple[str, str], str], role_id: str
) -> str | None:
    """A role's model: its ledger assignment, else its configured default."""
    return assignments.get(("role", role_id)) or org.role(role_id).default_model


def _priced(
    prices: PriceBook, overrides: tuple[PriceOverride, ...]
) -> set[tuple[str, str | None, str]]:
    return {(p.provider, p.endpoint_id, p.model) for p in (*prices.prices, *prices.shipped)} | {
        (o.provider, o.endpoint_id, o.model) for o in overrides
    }


def price_problems(
    catalog: ModelCatalog, prices: PriceBook, overrides: tuple[PriceOverride, ...]
) -> list[tuple[str, str]]:
    """Approved entries with no price version at all (`$.models.entries[i]`)."""
    priced = _priced(prices, overrides)
    return [
        (
            f"$.models.entries[{i}]",
            f"'{entry.ref}' is approved but has no price: add it to the price book "
            "(a price is never guessed) or pause the entry",
        )
        for i, entry in enumerate(catalog.entries)
        if entry.status == "approved" and entry.key not in priced
    ]


def _check_ref(
    catalog: ModelCatalog, ref: str | None, path: str, problems: list[tuple[str, str]]
) -> CatalogEntry | None:
    if ref is None:
        return None
    try:
        provider, endpoint_id, model = parse_model_ref(ref)
    except ValueError as exc:
        problems.append((path, str(exc)))
        return None
    entry = catalog.find(provider, model, endpoint_id)
    if entry is None:
        problems.append((path, f"model '{ref}' is not in the approved catalog"))
        return None
    if entry.status != "approved":
        problems.append((path, f"model '{ref}' is {entry.status} in the approved catalog"))
        return None
    return entry


def _quiet(catalog: ModelCatalog, ref: str | None) -> CatalogEntry | None:
    if ref is None:
        return None
    try:
        provider, endpoint_id, model = parse_model_ref(ref)
    except ValueError:
        return None
    entry = catalog.find(provider, model, endpoint_id)
    return entry if entry is not None and entry.status == "approved" else None


def assignment_problems(
    org: OrgGraph,
    catalog: ModelCatalog,
    assignments: Mapping[tuple[str, str], str] | None = None,
) -> tuple[list[tuple[str, str]], dict[str, tuple[CatalogEntry | None, str]]]:
    """Every effective model must be an approved entry. Returns the problems and, per agent, the
    approved entry it uses (or None) with the path of the setting that names its model."""
    ledger = assignments or {}
    problems: list[tuple[str, str]] = []
    for i, role in enumerate(org.roles):
        _check_ref(
            catalog,
            effective_role_default(org, ledger, role.id),
            f"$.org.roles[{i}].default_model",
            problems,
        )
    role_index = {r.id: i for i, r in enumerate(org.roles)}
    used: dict[str, tuple[CatalogEntry | None, str]] = {}
    for j, agent in enumerate(org.agents):
        chosen = effective_ref(org, ledger, agent.id)
        if chosen.source in ("agent ledger", "agent config"):
            path = f"$.org.agents[{j}].model"
            used[agent.id] = (_check_ref(catalog, chosen.ref, path, problems), path)
        else:  # the role's model was checked above; look it up quietly here
            path = f"$.org.roles[{role_index[agent.role]}].default_model"
            used[agent.id] = (_quiet(catalog, chosen.ref), path)
    return problems, used


def family_problems(
    org: OrgGraph, used: Mapping[str, tuple[CatalogEntry | None, str]]
) -> list[tuple[str, str]]:
    """A verifier must come from another model family than every agent it checks (ND48)."""
    problems: list[tuple[str, str]] = []
    for agent in org.agents:
        if org.role(agent.role).kind is not RoleKind.VERIFIER:
            continue
        mine, path = used.get(agent.id, (None, ""))
        for checked in agent.checks:
            theirs = used.get(checked, (None, ""))[0]
            if mine is not None and theirs is not None and mine.family == theirs.family:
                problems.append(
                    (
                        path,
                        f"verifier '{agent.id}' uses family '{mine.family}', the same as "
                        f"'{checked}' ({theirs.ref}) which it checks: a verifier must come from "
                        "a different model family (ND48)",
                    )
                )
    return problems


def catalog_problems(
    org: OrgGraph,
    catalog: ModelCatalog,
    prices: PriceBook,
    overrides: tuple[PriceOverride, ...],
    assignments: Mapping[tuple[str, str], str] | None = None,
) -> list[tuple[str, str]]:
    """`(path, message)` for every way the state breaks the catalog rules (see the module
    docstring). Paths are configuration paths such as `$.org.roles[1].default_model`. With no
    `assignments` this is the check of a configuration file by itself."""
    found = price_problems(catalog, prices, overrides)
    problems, used = assignment_problems(org, catalog, assignments)
    return found + problems + family_problems(org, used)


# -- connections -------------------------------------------------------------------------------

_Endpoint = Annotated[str, StringConstraints(max_length=300)]


def connection_key(provider: str, endpoint_id: str | None) -> str:
    """The fold's dictionary key of a connection: `provider`, or `provider/endpoint_id`. Internal:
    never parsed back and never a model reference (spec openai-compatible 2.1)."""
    return provider if endpoint_id is None else f"{provider}/{endpoint_id}"


_OAC_ENDPOINT = re.compile(r"https?://[^\s/?#@]+(/[^\s?#]*)?")
"""The shape core accepts for an `openai-compatible` endpoint: no credentials, query or fragment.
The URL rules (which hosts, https versus loopback http) are the engine adapter's."""
_KEY_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_OAC_AUTH = ("none", "key", "stored")
STORED_KEY_PROVIDERS = frozenset({"anthropic", OPENAI_COMPATIBLE})
"""The providers that take a stored key (R1.14c); the keyless providers stay `entra`."""
KEY_REF = re.compile(r"mk_[0-9a-f]{20}")
"""A stored-key reference: server generated, never text a person typed (R1.14c 4.3)."""


class ConnectionFacts(BaseModel):
    """What a `model.connection_created` or `_updated` ledger entry says (and nothing else: there
    is no secret to say). Other keys of the entry (`reason_hash`, `via`) are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    provider: Slug
    endpoint_id: EndpointId | None = None
    auth: Literal["entra", "key_file", "none", "key", "stored"]
    endpoint: _Endpoint = Field(min_length=1)
    status: ConnectionStatus
    key: str | None = None
    """The *name* of an operator key binding (never a path or a secret); `auth "key"` only."""
    key_ref: str | None = None
    """The id of a stored key (`mk_` and 20 hex characters, never a value); `auth "stored"` only."""

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.auth == "stored") != (self.key_ref is not None):
            raise ValueError("a key reference goes with auth stored, and only with it")
        if self.key_ref is not None and not KEY_REF.fullmatch(self.key_ref):
            raise ValueError("the key reference is not valid")
        if self.auth == "stored" and self.provider not in STORED_KEY_PROVIDERS:
            raise ValueError("the credential type does not match the provider")
        if self.provider != OPENAI_COMPATIBLE:
            if self.endpoint_id is not None:
                raise ValueError(ENDPOINT_NOT_ALLOWED)
            if self.auth not in ("entra", "key_file", "stored") or self.key is not None:
                raise ValueError("the credential type does not match the provider")
            if not _HTTPS_HOST.fullmatch(self.endpoint):
                # never quote the value: it may be something that was pasted by mistake
                raise ValueError("the endpoint must be a plain https address")
            return self
        if self.endpoint_id is None:
            raise ValueError(U_ENDPOINT_NEEDED)
        if self.auth not in _OAC_AUTH:
            raise ValueError("the credential type does not match the provider")
        if (self.auth == "key") != (self.key is not None):
            raise ValueError("a key name goes with auth key, and only with it")
        if self.key is not None and not _KEY_NAME.fullmatch(self.key):
            raise ValueError("the key name is not valid")
        if not _OAC_ENDPOINT.fullmatch(self.endpoint):
            raise ValueError("the endpoint must be an http or https address")
        return self

    @property
    def conn_key(self) -> str:
        """The fold's key of this connection."""
        return connection_key(self.provider, self.endpoint_id)

    @property
    def fingerprint(self) -> str:
        """The connection fingerprint an approval is recorded against (spec 2.4)."""
        return fingerprint(self.endpoint, self.auth, self.key, self.endpoint_id)


class ConnectionRecord(ConnectionFacts):
    """A connection now: the facts, who last set them, when, and in which ledger entry."""

    set_by: str
    set_at: datetime
    seq: int

    @property
    def facts(self) -> dict[str, str]:
        """The ledger payload for this connection."""
        out = {
            "provider": self.provider,
            "auth": self.auth,
            "endpoint": self.endpoint,
            "status": self.status,
        }
        if self.endpoint_id is not None:
            out["endpoint_id"] = self.endpoint_id
        if self.key is not None:
            out["key"] = self.key
        if self.key_ref is not None:
            out["key_ref"] = self.key_ref
        return out


def same_endpoint(a: str, b: str, provider: str | None = None) -> bool:
    """Hosts compare lower-cased, without a trailing slash. For `openai-compatible` the scheme and
    host compare lower-cased and the path case-sensitively (spec openai-compatible 2.3)."""
    if provider == OPENAI_COMPATIBLE:
        return normalise(a) == normalise(b)
    return a.lower().rstrip("/") == b.lower().rstrip("/")


@dataclass
class ProviderConnection:
    """What the ledger says about one connection (a provider, or a provider and endpoint id)."""

    record: ConnectionRecord | None = None
    """The current well-formed record (None after a removal or an invalid last write)."""
    invalid_seq: int | None = None
    """The sequence of an invalid last write (a malformed created/updated entry), else None."""
    last_endpoint: str | None = None
    """The last well-formed endpoint ever recorded (a repoint is a change from it)."""
    last_auth: str | None = None
    """The last well-formed `auth` ever recorded."""
    last_key: str | None = None
    """The last well-formed key name ever recorded."""
    facts_seq: int | None = None
    """The ledger sequence at which the current record's endpoint, auth and key name began (a
    change of any of them, a removal or an invalid write starts a new epoch)."""
    epochs: int = 0
    """How many epochs the connection has had. A free price from a configuration file cannot name
    a ledger sequence, so it holds only while this is 1 (never repointed, removed or damaged)."""
    ever: bool = False
    """Whether any created/updated entry (well-formed or not) was ever recorded."""


@dataclass
class ConnectionsState:
    """The folded connection entries of an organization, keyed by `connection_key`."""

    providers: dict[str, ProviderConnection] = field(default_factory=dict)
    seq: int = 0
    damaged: list[int] = field(default_factory=list)
    """The sequences of connection entries that name no connection key (an `openai-compatible`
    entry whose endpoint id is not text): they cannot land on any connection, and are listed by
    `models connections` instead of being skipped silently."""

    def clone(self) -> "ConnectionsState":
        """A copy that can be changed without touching this one."""
        return ConnectionsState(
            providers={p: dataclasses.replace(c) for p, c in self.providers.items()},
            seq=self.seq,
            damaged=list(self.damaged),
        )

    def get(self, provider: str, endpoint_id: str | None = None) -> ProviderConnection:
        """The state of one connection (empty if it never existed)."""
        return self.providers.get(connection_key(provider, endpoint_id), ProviderConnection())

    def records(self) -> list[ConnectionRecord]:
        """The current well-formed connections, by key."""
        return [c.record for _, c in sorted(self.providers.items()) if c.record is not None]


def is_repoint(
    provider: str,
    before: ProviderConnection,
    endpoint: str,
    auth: str,
    key: str | None,
) -> bool:
    """Whether a well-formed created/updated entry with these facts is a repoint, given the state
    `before` it (the one rule: `ModelConnections.put_in` and the connection history both call it).

    The fixed-credential providers: the last well-formed endpoint differs. `openai-compatible`:
    the endpoint, `auth` or key name differs, or the entry creates the connection again after a
    removal. The first entry of a connection never is, and a removal does not reset what was
    last kept."""
    if not before.ever:
        return False
    if provider == OPENAI_COMPATIBLE:
        removed = before.record is None and before.invalid_seq is None
        return (
            removed
            or before.last_endpoint is None
            or not same_endpoint(before.last_endpoint, endpoint, provider)
            or before.last_auth != auth
            or before.last_key != key
        )
    return before.last_endpoint is not None and not same_endpoint(before.last_endpoint, endpoint)


_FORBIDDEN_KEY_PARTS = ("file", "path")


def _connection_name(payload: Mapping[str, Any]) -> tuple[str, str | None] | None:
    """`(provider, endpoint_id)` an entry names, or None when it names no provider as a string."""
    provider, endpoint_id = payload.get("provider"), payload.get("endpoint_id")
    if not isinstance(provider, str) or not provider:
        return None
    if endpoint_id is not None and not isinstance(endpoint_id, str):
        return None
    return provider, endpoint_id


def apply_connection_event(state: ConnectionsState, event: LedgerEvent) -> ConnectionsState:
    """Apply one ledger entry to `state` (in place) and return it. Entries of other types and
    entries that do not name a provider as a string are skipped. A created/updated entry that
    does not satisfy `ConnectionFacts` makes that connection's state *invalid* (a bad write never
    leaves the previous endpoint silently in force). For `openai-compatible` an entry carrying a
    key whose name contains `file` or `path` is invalid too: a connection never holds a path."""
    kind = event.type
    payload: Mapping[str, Any] = event.payload
    written = kind in (
        EventType.MODEL_CONNECTION_CREATED.value,
        EventType.MODEL_CONNECTION_UPDATED.value,
        EventType.MODEL_CONNECTION_REMOVED.value,
    )
    named = payload.get("provider")
    if written and isinstance(named, str) and named and payload.get("endpoint_id") is not None:
        # an endpoint id belongs to openai-compatible and must be text: anything else is a damaged
        # entry. It never lands on a phantom key and is never skipped: it fails closed (ND277)
        if named != OPENAI_COMPATIBLE:
            state.seq = max(state.seq, event.seq)
            entry = state.providers.setdefault(named, ProviderConnection())
            entry.ever = True
            entry.record, entry.invalid_seq, entry.facts_seq = None, event.seq, None
            return state
        if not isinstance(payload["endpoint_id"], str):
            state.seq = max(state.seq, event.seq)
            state.damaged.append(event.seq)
            return state
    name = _connection_name(payload)
    if name is None:
        return state
    provider, endpoint_id = name
    ckey = connection_key(provider, endpoint_id)
    if kind in (
        EventType.MODEL_CONNECTION_CREATED.value,
        EventType.MODEL_CONNECTION_UPDATED.value,
    ):
        state.seq = max(state.seq, event.seq)
        entry = state.providers.setdefault(ckey, ProviderConnection())
        entry.ever = True
        try:
            if provider == OPENAI_COMPATIBLE and any(
                part in str(k).lower() for k in payload for part in _FORBIDDEN_KEY_PARTS
            ):
                raise ValueError("a connection never holds a path")
            facts = ConnectionFacts.model_validate(dict(payload))
        except (ValidationError, ValueError):
            entry.record, entry.invalid_seq, entry.facts_seq = None, event.seq, None
            return state
        entry.invalid_seq = None
        previous = entry.record
        if previous is None or previous.fingerprint != facts.fingerprint:
            entry.facts_seq = event.seq
            entry.epochs += 1
        entry.last_endpoint, entry.last_auth, entry.last_key = (
            facts.endpoint,
            facts.auth,
            facts.key,
        )
        entry.record = ConnectionRecord(
            **facts.model_dump(), set_by=event.actor, set_at=event.at, seq=event.seq
        )
    elif kind == EventType.MODEL_CONNECTION_REMOVED.value:
        state.seq = max(state.seq, event.seq)
        entry = state.providers.setdefault(ckey, ProviderConnection())
        entry.record, entry.invalid_seq, entry.facts_seq = None, None, None
    return state


def fold_connections(events: Iterable[LedgerEvent]) -> ConnectionsState:
    """The connections the events add up to, in order, keyed by `connection_key`."""
    state = ConnectionsState()
    for event in events:
        apply_connection_event(state, event)
    return state
