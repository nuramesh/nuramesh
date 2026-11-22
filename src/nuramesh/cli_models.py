"""`nuramesh models ...`: read and restrict the model governance state (L5, ND92).

The commands act on the organization stream of the team's store (the store `serve` uses), so the
CLI writes where the runs read. **The CLI restricts or reads** (spec/model-governance section
10.2): pause, retire, disable, disconnect, and the read-only listings, `check` and the rate-limited
connection `test`. For the local `openai-compatible` provider it also has `connect` and `price`
(an operator on the machine, with `--human`); approving and assigning stay in the signed-in API and
app, because the CLI cannot authenticate a person.

Exit codes follow the spec: a usage error (a bad `--human`, a bad configuration, a subject the
configuration does not have, a missing reason or content store) is 2; a refusal that comes from
ledger state (no such entry or connection, a retired entry, a rate limit, a halt, a block found by
`check`) is 3; a connection test that ran and failed is 1.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from nuramesh import cli_prompts
from nuramesh.cli_rules import add_team
from nuramesh.config import ConfigError
from nuramesh.config.shipped_prices import (
    ShippedPriceError,
    load_shipped,
    price_facts,
    price_warnings,
)
from nuramesh.core.config import ConfigValidationError, NuraMeshConfig
from nuramesh.core.endpoints import FREE_SOURCE
from nuramesh.core.gates import GateKind
from nuramesh.core.model_catalog import (
    connection_key,
    effective_ref,
    effective_role_default,
    same_endpoint,
)
from nuramesh.core.model_governance import PlanTask, Resolution
from nuramesh.core.models import (
    ENDPOINT_NOT_ALLOWED,
    U_ENDPOINT_OTHER,
    format_model_ref,
    parse_model_ref,
)
from nuramesh.core.org import OPENAI_COMPATIBLE
from nuramesh.core.people import is_human_id
from nuramesh.core.price_words import NO_PRICE_BLOCKED
from nuramesh.core.prices import P_LOOPBACK, PriceBasis, UnknownPriceError, resolve_price
from nuramesh.core.provider_presets import PRESETS, get_preset
from nuramesh.core.residency import DATA_CLASSES, ResidencyError, check_processing
from nuramesh.engine import net_guard
from nuramesh.engine.azure_openai import (
    ProbeTransport,
    http_get,
)
from nuramesh.engine.catalog_service import (
    CatalogRefused,
    CatalogService,
    ensure_seed,
    residency_violations,
)
from nuramesh.engine.connection_rules import (
    SUPPORTED_PROVIDERS,
    auth_of,
    connection_problem,
    default_endpoint,
)
from nuramesh.engine.connection_test import ConnectionTester, TestRefused
from nuramesh.engine.credentials import CredentialSource
from nuramesh.engine.governance import Governance, ModelGovernanceError
from nuramesh.engine.key_readers import KeyReaders, key_readers_with_reason
from nuramesh.engine.model_connections import (
    ConnectionRefused,
    ModelConnections,
    StoredKeyHeld,
)
from nuramesh.engine.org_halt import halt_predicate
from nuramesh.engine.org_ledger import LedgerView, OrgLedger
from nuramesh.stores import Store, StoreError
from nuramesh.team import (
    ShippedListInvalid,
    TeamEnv,
    TeamError,
    load_team_config,
    load_team_module,
    setup_team,
)

OK, NOT_DONE, USAGE, REFUSED, STOPPED = 0, 1, 2, 3, 4
HUMAN_TEXT = "--human needs an opaque id like human:u_7k2x9q (no email or name)"
PROBE_TRANSPORT: ProbeTransport = http_get
"""The connection test's HTTP transport (tests replace it; nothing else does)."""


def org_halted(store: Store, org_id: str) -> Callable[[], bool]:
    """The organization-halt predicate for this process: the real halt state of the org stream
    (E19, ND112). It raises when the stream cannot be read, and every user treats that as a halt
    (fail closed). Every halt-gated path (`models test`, `models check`, `run`) asks this."""
    return halt_predicate(store, org_id)


def halt_decision(store: Store, org_id: str) -> tuple[str | None, int | None]:
    """The ONE read that decides the halt refusal: `(why, episode)`. `why` is None when the
    organization is not halted; `episode` is the seq of the governing `org.halted` from that same
    read (T154 R0: the caller's E), or None when the state cannot be read (fail closed, E19
    section 3.5: an unreadable state is a halt in force, the unknown stretch)."""
    from nuramesh.core.model_governance import HALT_UNREADABLE_TEXT, HALTED_TEXT
    from nuramesh.engine.org_halt import HaltWatch

    try:
        state = HaltWatch(store, org_id).state()
    except Exception:
        return HALT_UNREADABLE_TEXT, None
    return (HALTED_TEXT, state.seq) if state.halted else (None, None)


def halt_problem(store: Store, org_id: str) -> str | None:
    """Why the organization is halted for this process, or None. A halt state that cannot be read
    counts as a halt (fail closed, E19 section 3.5). A halt in force at the start is exit
    `STOPPED` (4), ahead of every other refusal (design decision, 2026-10-26)."""
    return halt_decision(store, org_id)[0]


@dataclass
class Context:
    """What a command needs once the configuration is read and the store is open."""

    root: Path
    config: NuraMeshConfig
    store: Store
    ledger: OrgLedger

    @property
    def org_id(self) -> str:
        return self.config.org.id

    def view(self) -> LedgerView:
        return self.ledger.view()


def context_for(root: Path, config: NuraMeshConfig, store: Store) -> Context:
    """A context over an already open store."""
    return Context(root, config, store, OrgLedger(store, config.org.id, config.prices))


def effective_agent_refs(ctx: Context) -> dict[str, str | None]:
    """`agent id -> effective ref` (the ledger over the configuration)."""
    assignments = ctx.view().catalog.assignments
    return {
        a.id: effective_ref(ctx.config.org, assignments, a.id).ref for a in ctx.config.org.agents
    }


NO_CATALOG_TEXT = (
    "the run was not started (model governance): no approved model catalog: there is no approved "
    "real model for this organization. An admin adds and approves models (POST "
    "/v1/models/catalog/entries, then .../approve), or the team configuration seeds one "
    "(models:) before the first start. To run the demo on the fake model, add --fake."
)
NO_CONNECTION_TEXT = (
    "the run was not started (model governance): no usable connection: no approved model has an "
    "active connection with a valid endpoint. An admin connects a provider (PUT "
    "/v1/models/connections/<provider>, or the Models screen in the app). To run the demo on the "
    "fake model, add --fake."
)
ENDPOINT_VARIABLES = {"azure-openai": "AZURE_OPENAI_ENDPOINT", "azure-ai": "AZURE_AI_ENDPOINT"}


def bare_run_problem(ctx: Context) -> str | None:
    """The bare-run precheck (spec section 10.1 check 3): why a run without `--fake` can't start,
    or None. U1: an approved entry of a keyless provider; U2: one of those providers has an active
    connection with a valid endpoint. Writes nothing."""
    import os

    view = ctx.view()
    catalog = view.catalog.catalog()
    pairs = sorted(
        {
            (e.provider, e.endpoint_id)
            for e in (catalog.entries if catalog else ())
            if e.status == "approved" and e.provider in SUPPORTED_PROVIDERS
        },
        key=lambda pair: (pair[0], pair[1] or ""),
    )
    if not pairs:
        return NO_CATALOG_TEXT
    for provider, endpoint_id in pairs:
        record = view.connections.get(provider, endpoint_id).record
        if (
            record is not None
            and record.status == "active"
            and (provider == OPENAI_COMPATIBLE or record.auth in (auth_of(provider), "stored"))
            and connection_problem(
                provider,
                record.endpoint,
                endpoint_id=endpoint_id,
                auth=record.auth,
                key=record.key,
            )
            is None
        ):
            return None
    lines = [NO_CONNECTION_TEXT]
    for provider, _endpoint_id in pairs:
        variable = ENDPOINT_VARIABLES.get(provider)
        if variable and os.environ.get(variable) and view.connections.get(provider).record is None:
            lines.append(f"{variable} is no longer read: the endpoint comes from the connection")
    return "\n".join(lines)


def blocked_lines(store: Store, run_id: str) -> list[str]:
    """The text of each `model.blocked` entry on the run's stream (what `run` prints after it)."""
    lines: list[str] = []
    after = 0
    while True:
        page = list(store.read_stream(f"run:{run_id}", after_seq=after, limit=500))
        for event in page:
            after = event.seq
            if event.type != "model.blocked":
                continue
            p = event.payload
            who = f"agent={p['agent']}" if p.get("agent") else f"role={p.get('role')}"
            lines.append(f"model blocked [{p.get('reason')}] {who}: {p.get('detail')}")
        if len(page) < 500:
            break
    return lines


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Columns padded to their widest cell, two spaces apart, no trailing spaces."""
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    return "\n".join([line(headers), *(line(r) for r in rows)])


def seed_and_warn(store: Store, config: NuraMeshConfig, *, record_private: bool = True) -> None:
    """`ensure_seed` for this process: seed the catalog from `models:` when the ledger has none and
    print the startup warnings (drift, ignored entries) to stderr. Every process that can call a
    model seeds first, so this is also where `NURAMESH_ALLOW_PRIVATE_NETWORK=1` is written to the
    ledger and armed (R1.30 review 1): `run`, `eval`, `worker`, `models`, `calibrate`.
    `serve` passes `record_private=False`: the API server records it in its own start-up."""
    status = ensure_seed(store, config.org.id, config.models, base_prices=config.prices)
    for message in status.messages:
        print(message, file=sys.stderr)
    if record_private:
        try:
            net_guard.record_allowance_from_env(store, config.org.id)
        except ValueError as exc:  # the guard keeps refusing private hosts
            print(f"{exc}; private hosts stay refused", file=sys.stderr)


def open_context(
    args: argparse.Namespace, *, human: bool = False, record_private: bool = True
) -> Context | int:
    """Check `--human` (before the store is opened), read the configuration, open the store and
    seed. Returns the context, or the exit code after printing why."""
    from nuramesh import cli

    if human and not is_human_id(getattr(args, "human", None) or ""):
        print(HUMAN_TEXT, file=sys.stderr)
        return USAGE
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root, args.config)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    try:
        store = cli._open_server_store(root, args.store)
    except (ImportError, StoreError) as exc:
        print(cli._store_problem(exc), file=sys.stderr)
        return USAGE
    seed_and_warn(store, config, record_private=record_private)
    return Context(root, config, store, OrgLedger(store, config.org.id, config.prices))


def _content(ctx: Context) -> Any:
    """The content store for reasons, or None when it can't be opened (the service then refuses).
    It gets the hosted setting and the organization's `reason_days` (ND195 b)."""
    from nuramesh.engine import content

    try:
        return content.configured_content_service(ctx.store, ctx.config.content)
    except (content.ContentError, ImportError):
        return None


def key_readers_and_reason_for(ctx: Context) -> tuple[KeyReaders | None, str | None]:
    """One reader per org over the secret directory in `NURAMESH_SECRET_DIR`, or None and a fixed
    reason word (no directory, no ledger hashing, or a refused directory): a `stored` connection
    is then refused with `auth_kind` and that reason, fail closed. The cache setting is
    `NURAMESH_KEY_CACHE_SECONDS`."""
    content_service = _content(ctx)
    return key_readers_with_reason(
        ctx.store, None if content_service is None else content_service.ledger_hashing
    )


def key_readers_for(ctx: Context) -> KeyReaders | None:
    """`key_readers_and_reason_for` without the reason."""
    return key_readers_and_reason_for(ctx)[0]


def credentials_for(ctx: Context) -> CredentialSource:
    """The credential source of the CLI's org: stored keys through its reader, or off with the
    reason word in the `auth_kind` refusal."""
    readers, reason = key_readers_and_reason_for(ctx)
    if readers is None:
        return CredentialSource(stored_off_reason=reason)
    return readers.source_for(ctx.org_id)


def _catalog_service(ctx: Context) -> CatalogService:
    return CatalogService(
        ctx.store,
        ctx.config.org,
        ctx.ledger,
        prices=ctx.config.prices,
        overrides=ctx.config.price_overrides,
        content=_content(ctx),
    )


def _connections(ctx: Context, catalog_lock: Any = None) -> ModelConnections:
    return ModelConnections(
        ctx.store,
        ctx.config.org,
        ctx.ledger,
        content=_content(ctx),
        catalog_lock=catalog_lock,
    )


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- views -------------------------------------------------------------------------------------


def price_view(
    config: NuraMeshConfig,
    view: LedgerView,
    provider: str,
    model: str,
    at: datetime,
    endpoint_id: str | None = None,
) -> dict[str, Any] | None:
    """The price in effect at `at`, or None (unknown). An owner override is `provisional`."""
    try:
        resolved = resolve_price(
            view.catalog.price_book(config.prices, view.connections),
            provider,
            model,
            at,
            overrides=config.price_overrides,
            endpoint_id=endpoint_id,
        )
    except (UnknownPriceError, ValueError):
        return None
    cached = resolved.cached_input_per_million
    return {
        "currency": resolved.currency.value,
        "input_per_million": str(resolved.input_per_million),
        "output_per_million": str(resolved.output_per_million),
        "cached_input_per_million": None if cached is None else str(cached),
        "provisional": resolved.basis is not PriceBasis.KNOWN,
    }


def _basis(price: dict[str, Any] | None) -> str:
    if price is None:
        return "none"
    return "provisional" if price["provisional"] else "book"


def catalog_body(ctx: Context, drift: bool) -> dict[str, Any]:
    """The catalog as the API answers it (spec section 10.3)."""
    view = ctx.view()
    fold = view.catalog
    catalog = fold.catalog()
    connected = {
        connection_key(r.provider, r.endpoint_id)
        for r in view.connections.records()
        if r.status == "active"
    }
    at = ctx.store.now()
    entries: list[dict[str, Any]] = []
    if catalog is not None:
        book = fold.price_book(ctx.config.prices, view.connections)
        shipped = load_shipped()
        for entry in catalog.entries:
            key = entry.key
            facts = price_facts(
                book, shipped, ctx.config.price_overrides, entry.provider, entry.model, at,
                entry.endpoint_id,
            )  # fmt: skip
            entries.append(
                {
                    "provider": entry.provider,
                    **({"endpoint_id": entry.endpoint_id} if entry.endpoint_id else {}),
                    "model": entry.model,
                    "family": entry.family,
                    "processing": entry.processing,
                    "status": entry.status,
                    "data_classes": list(catalog.data_classes(entry)),
                    "price": price_view(
                        ctx.config, view, entry.provider, entry.model, at, entry.endpoint_id
                    ),
                    "price_origin": facts.origin,
                    "price_status": facts.status,
                    "price_age_days": facts.age_days,
                    "price_retrieved_on": (
                        None if facts.retrieved_on is None else facts.retrieved_on.isoformat()
                    ),
                    "price_stale": facts.stale,
                    "price_set_on": (
                        facts.resolved.price.effective_from.date().isoformat()
                        if facts.origin == "operator" and facts.resolved and facts.resolved.price
                        else None
                    ),
                    "connected": connection_key(entry.provider, entry.endpoint_id) in connected,
                    "origin": fold.origin.get(key),
                    "updated_by": fold.updated_by.get(key),
                    "updated_at": _stamp(fold.updated_at[key]) if key in fold.updated_at else None,
                }
            )
    return {
        "seeded": fold.seeded,
        "config_drift": drift,
        "ignored_events": fold.ignored,
        "customer_regions": list(fold.customer_regions),
        "entries": entries,
    }


def _drift(ctx: Context) -> bool:
    from nuramesh.core.model_catalog import seed_payload

    models = ctx.config.models
    fold = ctx.view().catalog
    if models is None:
        return False
    digest = seed_payload(models)["config_hash"]
    return (fold.seeded and fold.config_hash != digest) or (fold.exists and not fold.seeded)


# -- read-only commands --------------------------------------------------------------------------


def cmd_catalog(args: argparse.Namespace) -> int:
    ctx = open_context(args, record_private=False)  # read-only: reaches no model
    if isinstance(ctx, int):
        return ctx
    body = catalog_body(ctx, _drift(ctx))
    if args.json or args.output == "json":
        print(json.dumps(body, indent=2))
        return OK
    if body["config_drift"]:
        print(
            "warning: the models: section of the team configuration differs from the catalog in "
            "the ledger, which is in force"
        )
    if not body["entries"]:
        print("no catalog")
        return OK
    rows = [
        [
            e.get("ref") or format_model_ref(e["provider"], e.get("endpoint_id"), e["model"]),
            e["family"],
            e["processing"],
            e["status"],
            ",".join(e["data_classes"]) or "-",
            _basis(e["price"]),
            "yes" if e["connected"] else "no",
        ]
        for e in body["entries"]
    ]
    print(table(["MODEL", "FAMILY", "PROCESSING", "STATUS", "DATA", "PRICE", "CONNECTED"], rows))
    for e in body["entries"]:
        ref = e.get("ref") or format_model_ref(e["provider"], e.get("endpoint_id"), e["model"])
        print(f"  {ref}  price: {price_text(e, ref)}")
    missing = sum(1 for e in body["entries"] if e["price_origin"] == "none")
    if missing:
        print(no_price_warning(missing), file=sys.stderr)
    return OK


def print_price_warnings(config: NuraMeshConfig, today: date, store: Store | None = None) -> None:
    """The config-check warnings to stderr (never errors): an ignored override, a stale shipped
    price. An invalid shipped file is `ShippedListInvalid` (exit 3), as everywhere."""
    try:
        shipped = load_shipped()
    except ShippedPriceError as exc:
        raise ShippedListInvalid(f"shipped price list invalid: {exc}") from None
    folded = None
    if store is not None:  # the ledger's `models price` versions count as own prices (T0)
        view = OrgLedger(store, config.org.id, config.prices).view()
        folded = view.catalog.price_book(config.prices, view.connections)
    for message in price_warnings(config, shipped, today, folded):
        print(f"warning: {message}", file=sys.stderr)


def no_price_warning(count: int) -> str:
    """The one stderr line after the catalog listing (`1 model has no price: ...`)."""
    noun, verb, them = ("model", "has", "it") if count == 1 else ("models", "have", "them")
    return f"warning: {count} {noun} {verb} no price: calls to {them} are blocked"


def price_text(entry: dict[str, Any], ref: str) -> str:
    """The price part of a catalog line (spec default-prices 6.1). `no price` and `cost unknown`
    are different conditions and are never mixed up."""
    price = entry["price"]
    origin = entry["price_origin"]
    if price is None:
        return (
            "NONE: calls to this model are blocked (no price). Set one: "
            f"nuramesh models price {ref} --input-price N --output-price N "
            "--effective-from UTC --source TEXT"
        )
    rates = f"${price['input_per_million']} in / ${price['output_per_million']} out per 1M"
    if origin == "override":
        return f"PROVISIONAL (override) {rates}"
    if origin == "shipped":
        stale = ""
        if entry["price_stale"]:
            stale = f", list is {entry['price_age_days']} days old"
        return f"{rates} (shipped list, as of {entry['price_retrieved_on']}{stale})"
    return f"{rates} (operator, set {entry['price_set_on']})"


def cmd_assignments(args: argparse.Namespace) -> int:
    ctx = open_context(args, record_private=False)  # read-only: reaches no model
    if isinstance(ctx, int):
        return ctx
    assignments = ctx.view().catalog.assignments
    org = ctx.config.org
    rows: list[list[str]] = []
    for agent in org.agents:
        found = effective_ref(org, assignments, agent.id)
        rows.append([f"agent={agent.id}", found.ref or "-", found.source])
    held = {a.role for a in org.agents}
    for role in org.roles:
        if role.id in held:
            continue
        ref = effective_role_default(org, assignments, role.id)
        source = "none"
        if ref is not None:
            source = "role ledger" if assignments.get(("role", role.id)) else "role config"
        rows.append([f"role={role.id}", ref or "-", source])
    print(table(["SUBJECT", "MODEL", "FROM"], rows))
    return OK


def cmd_connections(args: argparse.Namespace) -> int:
    ctx = open_context(args, record_private=False)  # read-only: reaches no model
    if isinstance(ctx, int):
        return ctx
    state = ctx.view().connections
    views = [
        {
            **r.facts,
            "set_by": r.set_by,
            "set_at": _stamp(r.set_at),
            "seq": r.seq,
        }
        for r in state.records()
    ]
    for provider, found in sorted(state.providers.items()):
        if found.record is None and found.invalid_seq is not None:
            views.append(
                {
                    "provider": provider,
                    "endpoint": "(invalid)",
                    "status": "invalid",
                    "set_by": "-",
                    "set_at": "-",
                    "seq": found.invalid_seq,
                }
            )
    for seq in state.damaged:
        views.append(
            {
                "provider": "openai-compatible",
                "endpoint": "(invalid: the endpoint id cannot be read)",
                "status": "invalid",
                "set_by": "-",
                "set_at": "-",
                "seq": seq,
            }
        )
    views.sort(key=lambda v: str(v["provider"]))
    if args.json:
        print(json.dumps({"connections": views}, indent=2))
        return OK
    if not views:
        print("no connections")
        return OK
    rows = [
        [
            connection_key(
                str(v["provider"]), (str(v["endpoint_id"]) if v.get("endpoint_id") else None)
            ),
            *[str(v[k]) for k in ("endpoint", "status", "set_by", "set_at", "seq")],
        ]
        for v in views
    ]
    print(table(["PROVIDER", "ENDPOINT", "STATUS", "SET_BY", "SET_AT", "SEQ"], rows))
    return OK


# -- restricting commands --------------------------------------------------------------------------


def _usage_or_refused(exc: CatalogRefused) -> int:
    return USAGE if exc.status in (422, 503) else REFUSED


_ENDPOINT_OTHER_TEXT = f"{ENDPOINT_NOT_ALLOWED}: {U_ENDPOINT_OTHER}"
"""An endpoint id on a provider other than openai-compatible (spec openai-compatible 2.1, U15)."""

CLI_ONLY_LOCAL = (
    "the CLI can only {what} the openai-compatible provider; every other provider is "
    "changed through the API or the app (spec/model-governance 10.2)"
)


def cmd_connect(args: argparse.Namespace) -> int:
    """`models connect <team> openai-compatible --endpoint-id ID --endpoint URL --auth none|key
    [--key NAME]`. `--key` is the *name* of an operator key binding (never a key and never a path:
    the binding file is the operator's, `NURAMESH_OPENAI_COMPATIBLE_KEYS`)."""
    ctx = open_context(args, human=True)
    if isinstance(ctx, int):
        return ctx
    provider = args.provider
    if provider != OPENAI_COMPATIBLE:
        if args.endpoint_id is not None:
            print(_ENDPOINT_OTHER_TEXT, file=sys.stderr)
        else:
            print(CLI_ONLY_LOCAL.format(what="connect"), file=sys.stderr)
        return USAGE
    fixed = default_endpoint(provider)
    endpoint, endpoint_id, auth = fixed or args.endpoint, args.endpoint_id, args.auth
    preset = None
    if args.preset is not None:
        preset = get_preset(args.preset)
        if preset is None:
            print(f"unknown preset; `models presets` lists them: {_preset_ids()}", file=sys.stderr)
            return USAGE
        endpoint = endpoint or preset.base_url
        endpoint_id = endpoint_id or preset.id
        auth = auth or ("key" if preset.needs_key else "none")
        if auth == "key" and args.key is None:
            print(
                f"the {preset.id} preset needs a key: pass --key NAME, the name of a key binding "
                "in the operator's binding file (never the key itself)",
                file=sys.stderr,
            )
            return USAGE
    if endpoint is None:
        print("--endpoint is required", file=sys.stderr)
        return USAGE
    try:
        result = _connections(ctx).put(
            provider,
            endpoint,
            by=args.human,
            status=args.status,
            reason=args.reason,
            via="cli",
            endpoint_id=endpoint_id,
            auth=auth,
            key=args.key,
        )
    except ConnectionRefused as exc:
        print(exc.args[0], file=sys.stderr)
        return USAGE if exc.path != "$.reason" else REFUSED
    except CatalogRefused as exc:
        print(exc.message, file=sys.stderr)
        return _usage_or_refused(exc)
    name = provider if endpoint_id is None else f"{provider}/{endpoint_id}"
    if result.result == "already":
        print(f"connection for {name} is already as asked; no change")
        return OK
    note = (
        f"; paused again: {', '.join(result.repaused)}; the connection is disabled until you "
        "enable it and approve those models again"
        if result.repaused
        else ""
    )
    print(f"connection for {name} {result.result}; ledger entry {result.ledger_seq}{note}")
    if preset is not None:
        print(
            f"model name hint: {preset.model_hint}. No price is set: add one with `models price` "
            "(an unknown price stays unknown). Check it with "
            f"`models test --endpoint-id {endpoint_id}`."
        )
        if preset.local:
            print(
                "a local endpoint is refused by the connection test unless the server runs with "
                "NURAMESH_ALLOW_PRIVATE_NETWORK=1"
            )
    return OK


def _preset_ids() -> str:
    return ", ".join(p.id for p in PRESETS)


def cmd_presets(args: argparse.Namespace) -> int:
    """`models presets`: the built-in openai-compatible presets (data only, nothing connected)."""
    if args.json:
        print(json.dumps({"presets": [asdict(p) for p in PRESETS]}, indent=2))
        return OK
    for p in PRESETS:
        where = "local, no key" if p.local else "hosted, needs a key"
        print(f"{p.id:<10} {p.base_url}  ({where}; model e.g. {p.model_hint})")
    return OK


def cmd_disable(args: argparse.Namespace) -> int:
    ctx = open_context(args, human=True)
    if isinstance(ctx, int):
        return ctx
    provider = args.provider
    endpoint_id = getattr(args, "endpoint_id", None)
    found = ctx.view().connections.get(provider, endpoint_id)
    if found.record is None:
        if found.invalid_seq is not None:
            print(
                f"the connection for provider '{provider}' cannot be used: its last change is "
                "not valid; an admin connects it again through the API",
                file=sys.stderr,
            )
        else:
            print(f"no connection for provider '{provider}'", file=sys.stderr)
        return REFUSED
    try:
        result = _connections(ctx).put(
            provider,
            found.record.endpoint,
            by=args.human,
            status="disabled",
            via="cli",
            endpoint_id=endpoint_id,
            auth=found.record.auth if provider == OPENAI_COMPATIBLE else None,
            key=found.record.key,
        )
    except ConnectionRefused as exc:
        print(exc.args[0], file=sys.stderr)
        return REFUSED
    if result.result == "already":
        print(f"connection for {provider} is already disabled; no change")
        return OK
    print(f"connection for {provider} disabled; ledger entry {result.ledger_seq}")
    return OK


def cmd_disconnect(args: argparse.Namespace) -> int:
    ctx = open_context(args, human=True, record_private=False)  # reaches no model
    if isinstance(ctx, int):
        return ctx
    provider = args.provider
    if provider not in SUPPORTED_PROVIDERS:
        print(
            f"provider '{provider}' needs a stored key, which is not available; "
            f"the supported providers are {', '.join(SUPPORTED_PROVIDERS)}",
            file=sys.stderr,
        )
        return USAGE
    try:
        removed = _connections(ctx).remove(
            provider,
            by=args.human,
            via="cli",
            endpoint_id=getattr(args, "endpoint_id", None),
            refuse_stored=True,  # decided inside the removal (no owner login here; spec r114c 3, 9)
        )
    except StoredKeyHeld as exc:
        print(exc.args[0], file=sys.stderr)
        return REFUSED
    except ConnectionRefused as exc:
        print(exc.args[0], file=sys.stderr)
        return USAGE
    if not removed:
        print(f"no connection for provider '{provider}'", file=sys.stderr)
        return REFUSED
    print(f"connection for {provider} removed; ledger entry {ctx.view().connections.seq}")
    return OK


def cmd_restrict(args: argparse.Namespace) -> int:
    """`models pause|retire <team> <provider/model>`."""
    ctx = open_context(args, human=True)
    if isinstance(ctx, int):
        return ctx
    try:
        provider, endpoint_id, model = parse_model_ref(args.ref)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    service = _catalog_service(ctx)
    act = service.pause if args.action == "pause" else service.retire
    try:
        result = act(
            actor=args.human,
            provider=provider,
            endpoint_id=endpoint_id,
            model=model,
            reason=args.reason,
            via="cli",
        )
    except CatalogRefused as exc:
        print(exc.message, file=sys.stderr)
        return _usage_or_refused(exc)
    done = "paused" if args.action == "pause" else "retired"
    if result.already:
        print(f"'{args.ref}' is already {done}; no change")
        return OK
    who = ", ".join(result.affected) or "nobody"
    print(
        f"'{args.ref}' {done} by {args.human} (ledger entry {result.ledger_seq}); "
        f"affects: {who}; their next call is blocked"
    )
    return OK


def cmd_price_show(args: argparse.Namespace) -> int:
    """`models price <ref> --show`: every price version of the model, with its origin, date and
    source. Read-only: no `--human`, nothing is written, no model is reached."""
    ctx = open_context(args, record_private=False)
    if isinstance(ctx, int):
        return ctx
    try:
        provider, endpoint_id, model = parse_model_ref(args.ref)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    view = ctx.view()
    book = view.catalog.price_book(ctx.config.prices, view.connections)
    shipped = load_shipped()
    facts = price_facts(
        book, shipped, ctx.config.price_overrides, provider, model, ctx.store.now(), endpoint_id
    )
    versions: list[tuple[str, Any, str]] = []
    if endpoint_id is None:
        for p in book.shipped:
            if (p.provider, p.model) == (provider, model):
                row = next((r for r in shipped.rows if r.price == p), None)
                versions.append(("shipped", p, "" if row is None else str(row.retrieved_on)))
    for p in book.prices:
        if (p.provider, p.endpoint_id, p.model) == (provider, endpoint_id, model):
            versions.append(("operator", p, ""))
    versions.sort(key=lambda v: v[1].effective_from)
    if not versions and facts.resolved is None:
        print(f"{args.ref}: {NO_PRICE_BLOCKED.lower()}")
        return OK
    print(f"{args.ref}: {len(versions)} price version(s); in force now: {facts.origin}")
    for origin, p, retrieved in versions:
        line = (
            f"  {origin}  ${p.input_per_million} in / ${p.output_per_million} out per 1M  "
            f"effective {p.effective_from.date()}"
        )
        if retrieved:
            line += f"  retrieved {retrieved}"
        mark = "  (in force)" if facts.resolved is not None and facts.resolved.price is p else ""
        print(f"{line}  source: {p.source}{mark}")
    r = facts.resolved
    if facts.origin == "override" and r is not None:
        print(
            f"  override (provisional)  ${r.input_per_million} in / "
            f"${r.output_per_million} out per 1M  (in force)"
        )
    return OK


def cmd_price(args: argparse.Namespace) -> int:
    """`models price <provider/model> (--free | --input-price N --output-price N)
    --effective-from T --source TEXT --reason TEXT`. `--free` declares a zero price: only for an
    active loopback connection (spec openai-compatible 12); the service refuses anything else.
    `--show` only prints the versions (shipped and operator) and writes nothing."""
    if args.show:
        return cmd_price_show(args)
    ctx = open_context(args, human=True)
    if isinstance(ctx, int):
        return ctx
    try:
        provider, endpoint_id, model = parse_model_ref(args.ref)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    if provider != OPENAI_COMPATIBLE:
        # a free price needs an active loopback openai-compatible connection (12.3, P15)
        print(P_LOOPBACK if args.free else CLI_ONLY_LOCAL.format(what="price"), file=sys.stderr)
        return USAGE
    # `--input`/`--output` are the old spellings of `--input-price`/`--output-price` (freeze B)
    given_in = args.input_price if args.input_price is not None else args.input
    given_out = args.output_price if args.output_price is not None else args.output
    if args.free == (given_in is not None or given_out is not None):
        print(
            "give --free, or both --input-price and --output-price (US$ per million tokens)",
            file=sys.stderr,
        )
        return USAGE
    if not args.free and (given_in is None or given_out is None):
        print("give both --input-price and --output-price", file=sys.stderr)
        return USAGE
    try:
        result = _catalog_service(ctx).set_price(
            actor=args.human,
            provider=provider,
            endpoint_id=endpoint_id,
            model=model,
            input_per_million="0" if args.free else given_in,
            output_per_million="0" if args.free else given_out,
            effective_from=args.effective_from,
            source=args.source,
            reason=args.reason,
            via="cli",
        )
    except CatalogRefused as exc:
        print(exc.message, file=sys.stderr)
        return _usage_or_refused(exc)
    print(f"price for '{args.ref}' recorded; ledger entry {result.ledger_seq}")
    return OK


# -- local-model (ND266 section 3) -----------------------------------------------------------------

LOCAL_MODELS = ("qwen3:8b", "qwen3:4b")
"""The models the `local-model` Compose profile pins (deploy/compose/local-model/pins.sh)."""
LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
LOCAL_ENDPOINT_ID = "local"
LOCAL_PROCESSING_ENV = "NURAMESH_LOCAL_MODEL_PROCESSING"
_LOCAL_REASON = "ND266 local-model profile: Qwen3 via Ollama on this host"


def cmd_local_model(args: argparse.Namespace) -> int:
    """`models local-model <team> --human human:<id> [--model qwen3:8b|qwen3:4b] [--processing
    SCOPE] [--yes]`. The `local-model` Compose profile fills in the values: the loopback connection,
    the catalog entry in the host's region (`--processing` or `NURAMESH_LOCAL_MODEL_PROCESSING`,
    never a default), the declared-free price and the approval. `--dry-run` only shows the
    proposal; without `--yes` a terminal asks (no terminal: exit 2); with `--yes` the named
    human's writes go through the normal services and are
    recorded in the ledger like any other. It assigns nothing: that stays a separate choice."""
    ctx = open_context(args, human=True)
    if isinstance(ctx, int):
        return ctx
    if args.model not in LOCAL_MODELS:
        print(f"'{args.model}' is not a pinned model ({', '.join(LOCAL_MODELS)})", file=sys.stderr)
        return USAGE
    processing = args.processing or os.environ.get(LOCAL_PROCESSING_ENV, "")
    if not processing:
        print(
            f"the host's region is needed: give --processing or set {LOCAL_PROCESSING_ENV} "
            "(canada, global or region:<id>); it is never guessed",
            file=sys.stderr,
        )
        return USAGE
    try:
        check_processing(processing)
    except ResidencyError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    ref = f"{OPENAI_COMPATIBLE}/{LOCAL_ENDPOINT_ID}/{args.model}"
    print(f"local-model profile, proposed for {args.human}:")
    print(f"  connection  {OPENAI_COMPATIBLE}/{LOCAL_ENDPOINT_ID}  {LOCAL_BASE_URL}  (auth none)")
    print(f"  catalog     {ref}  family qwen  processing {processing}")
    print(f"  price       US$0 in and out, source '{FREE_SOURCE}'")
    print("  approval    by you, recorded in the ledger; no agent is assigned")
    existing = ctx.view().connections.get(OPENAI_COMPATIBLE, LOCAL_ENDPOINT_ID).record
    if existing is not None and not same_endpoint(
        existing.endpoint, LOCAL_BASE_URL, OPENAI_COMPATIBLE
    ):
        print(
            f"{OPENAI_COMPATIBLE}/{LOCAL_ENDPOINT_ID} already points at {existing.endpoint}, not "
            f"{LOCAL_BASE_URL}; this command would repoint it and pause that provider's approved "
            "entries, so it refuses (change or remove that connection first)",
            file=sys.stderr,
        )
        return REFUSED
    key = (OPENAI_COMPATIBLE, LOCAL_ENDPOINT_ID, args.model)
    priced = any(
        (p.provider, p.endpoint_id, p.model) == key for p in ctx.view().catalog.price_versions
    )
    if priced:
        print("  price       already set for this entry: kept as it is")
    if args.dry_run:
        print("dry run: nothing was written")
        return OK
    if not args.yes:  # OD-15: was a silent preview (exit 0); now a question, or exit 2
        if not cli_prompts.on_terminal():
            print(
                "nothing was written: confirm on a terminal, or pass --yes to record this as "
                "your approval (--dry-run only shows it)",
                file=sys.stderr,
            )
            return USAGE
        answer = cli_prompts.ask("Record this as your approval? [y/N] ")
        if (answer or "").lower() not in ("y", "yes"):
            print("nothing was written", file=sys.stderr)
            return OK
    service = _catalog_service(ctx)
    try:
        _connections(ctx, service.lock).put(
            OPENAI_COMPATIBLE,
            LOCAL_BASE_URL,
            by=args.human,
            reason=_LOCAL_REASON,
            via="cli",
            endpoint_id=LOCAL_ENDPOINT_ID,
            auth="none",
        )
        fold = ctx.view().catalog
        if key not in fold.entries:
            service.add(
                actor=args.human,
                provider=OPENAI_COMPATIBLE,
                endpoint_id=LOCAL_ENDPOINT_ID,
                model=args.model,
                family="qwen",
                processing=processing,
                reason=_LOCAL_REASON,
                via="cli",
            )
        elif fold.entries[key].processing != processing:
            print(
                f"{ref} is already in the catalog with processing "
                f"'{fold.entries[key].processing}', not '{processing}'; change it first "
                "(the region is never rewritten silently)",
                file=sys.stderr,
            )
            return REFUSED
        if not priced:
            service.set_price(
                actor=args.human,
                provider=OPENAI_COMPATIBLE,
                endpoint_id=LOCAL_ENDPOINT_ID,
                model=args.model,
                input_per_million="0",
                output_per_million="0",
                effective_from=ctx.store.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                source=FREE_SOURCE,
                reason=_LOCAL_REASON,
                via="cli",
            )
        service.approve(
            actor=args.human,
            provider=OPENAI_COMPATIBLE,
            endpoint_id=LOCAL_ENDPOINT_ID,
            model=args.model,
            reason=_LOCAL_REASON,
            via="cli",
        )
    except ConnectionRefused as exc:
        print(exc.args[0], file=sys.stderr)
        return USAGE if exc.path != "$.reason" else REFUSED
    except CatalogRefused as exc:
        print(exc.message, file=sys.stderr)
        return _usage_or_refused(exc)
    price_text = "kept its existing price" if priced else "priced as declared free (local)"
    print(f"{ref} is connected, {price_text} and approved by {args.human}")
    return OK


# -- test ------------------------------------------------------------------------------------------


def cmd_test(args: argparse.Namespace) -> int:
    ctx = open_context(args, human=True)
    if isinstance(ctx, int):
        return ctx
    provider = args.provider
    endpoint_id = getattr(args, "endpoint_id", None)
    if provider != OPENAI_COMPATIBLE and endpoint_id is not None:
        print(_ENDPOINT_OTHER_TEXT, file=sys.stderr)
        return USAGE
    found = ctx.view().connections.get(provider, endpoint_id)
    if found.record is None and found.invalid_seq is None:
        print(f"no connection for provider '{provider}'", file=sys.stderr)
        return REFUSED
    tester = ConnectionTester(
        ctx.store,
        ctx.org_id,
        ctx.ledger,
        credentials=credentials_for(ctx),
        halted=org_halted(ctx.store, ctx.org_id),
        transport=PROBE_TRANSPORT,
    )
    try:
        result = tester.test(provider, by=args.human, via="cli", endpoint_id=endpoint_id)
    except TestRefused as exc:
        print(exc.text, file=sys.stderr)
        return STOPPED if exc.code == "org_halted" else REFUSED
    if result.ok:
        print(
            f"connection test for {provider}: OK in {result.latency_ms} ms: {result.text}; "
            f"ledger entry {result.ledger_seq}"
        )
        return OK
    print(
        f"connection test for {provider}: FAILED [{result.code}] in {result.latency_ms} ms: "
        f"{result.text}; ledger entry {result.ledger_seq}"
    )
    return NOT_DONE


# -- check -----------------------------------------------------------------------------------------


def plan_of(config: NuraMeshConfig, root: Path) -> tuple[PlanTask, ...] | str:
    """The team's verifier pairings, from its plan set up on a throwaway memory store (nothing is
    written), or the text of why the team module could not be set up."""
    from nuramesh.stores import MemoryStore

    try:
        module = load_team_module(root)
        team = setup_team(
            module,
            TeamEnv(
                root=root,
                config=config,
                store=MemoryStore(),
                run_id="models-check",
                fake=False,
                sandbox=lambda: None,
            ),
        )
    except TeamError as exc:
        return str(exc)
    return plan_tasks(team.plan(config.org) if callable(team.plan) else team.plan)


def plan_tasks(plan: Sequence[Any]) -> tuple[PlanTask, ...]:
    """What the start stage needs of a plan: each task's assignee and its verifier gates."""
    out: list[PlanTask] = []
    for planned in plan:
        task = planned.task
        gates = tuple(
            (spec.id, str(spec.config["verifier"]))
            for spec in task.definition_of_done.gates
            if spec.kind == GateKind.VERIFIER and "verifier" in spec.config
        )
        out.append(PlanTask(task.id, task.assignee, gates))
    return tuple(out)


def governance_for(ctx: Context) -> Governance:
    """The governance object for this process (the same ledger view as the other commands)."""
    return Governance(
        ctx.store,
        ctx.config.org,
        prices=ctx.config.prices,
        overrides=ctx.config.price_overrides,
        ledger=ctx.ledger,
        models=ctx.config.models,
        halted=org_halted(ctx.store, ctx.org_id),
    )


def resolution_line(subject: str, r: Resolution) -> str:
    """`  <subject>  <ref>  <family>  <processing>  <endpoint>  connection <seq>  <price basis>`."""
    return "  " + "  ".join(
        [
            subject,
            r.ref,
            r.family or "-",
            r.processing or "local",
            r.endpoint or "-",
            f"connection {r.connection_seq}" if r.connection_seq is not None else "local",
            r.price_basis or "-",
        ]
    )


def _blocked_code(blocks: Sequence[Any]) -> int:
    """3, or 4 when the organization halt is among the blocks (E19 wins)."""
    return STOPPED if any(b.reason == "org_halted" for b in blocks) else REFUSED


def cmd_check(args: argparse.Namespace) -> int:
    ctx = open_context(args)
    if isinstance(ctx, int):
        return ctx
    plan = plan_of(ctx.config, ctx.root)
    if isinstance(plan, str):
        print(plan, file=sys.stderr)
        return USAGE
    report = governance_for(ctx).preflight(args.data, plan)
    violations = residency_violations(ctx.view())
    if args.json:
        problem = ModelGovernanceError(report.blocks).to_problem() if report.blocks else None
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "residency_violations": violations,
                    "blocks": problem["blocks"] if problem else [],
                    "resolutions": [
                        {
                            "subject": s,
                            "ref": r.ref,
                            "family": r.family,
                            "processing": r.processing,
                            "endpoint": r.endpoint,
                            "connection_seq": r.connection_seq,
                            "price_basis": r.price_basis,
                        }
                        for s, r in report.resolutions
                    ],
                },
                indent=2,
            )
        )
        return OK if report.ok else _blocked_code(report.blocks)
    if violations:
        print(
            "these entries break the remote-endpoint residency rule (a remote endpoint takes only "
            "global processing): " + ", ".join(violations)
        )
    if not report.ok:
        print("blocked:")
        for block in report.blocks:
            print(block.text)
        return _blocked_code(report.blocks)
    print(f"all models resolve for {args.data} data")
    for subject, r in report.resolutions:
        print(resolution_line(subject, r))
    return OK


# -- parser ----------------------------------------------------------------------------------------


def register(sub: Any) -> None:
    """Add `models` and its subcommands to the parser's `sub` (called by `cli.parser`)."""
    models = sub.add_parser("models", help="model catalog, assignments and connections")
    ms = models.add_subparsers(dest="models_command", required=True)

    def common(p: argparse.ArgumentParser, *, human: bool = False) -> None:
        add_team(p)
        p.add_argument("--config", metavar="FILE", help="another config file in the team folder")
        p.add_argument("--store", help="SQLite file (default: <team>/.nuramesh/runs.db)")
        if human:
            p.add_argument("--human", metavar="HUMAN", help="who is acting: human:<id>")

    for name, func, helptext in (
        ("catalog", cmd_catalog, "the effective model catalog"),
        ("assignments", cmd_assignments, "the model each agent and role uses now"),
        ("connections", cmd_connections, "the provider connections"),
    ):
        p = ms.add_parser(name, help=helptext)
        common(p)
        if name != "assignments":
            p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
        else:
            p.set_defaults(json=False)
        p.set_defaults(func=func)

    for name, func, helptext in (
        ("disable", cmd_disable, "disable a provider connection (keeps its endpoint)"),
        ("disconnect", cmd_disconnect, "remove a provider connection (not one with a stored key)"),
        ("test", cmd_test, "test a connection (one metadata request, no model call)"),
    ):
        p = ms.add_parser(name, help=helptext)
        common(p, human=True)
        p.add_argument("provider")
        p.add_argument(
            "--endpoint-id", dest="endpoint_id", help="the connection's id (openai-compatible)"
        )
        p.set_defaults(func=func)

    cn = ms.add_parser(
        "connect", help="create or change an openai-compatible connection (a repoint pauses)"
    )
    common(cn, human=True)
    cn.add_argument("provider")
    cn.add_argument("--endpoint-id", dest="endpoint_id", help="the connection's id (a slug)")
    cn.add_argument("--endpoint", help="the base URL (not for providers with a fixed endpoint)")
    cn.add_argument("--auth", choices=("none", "key"), help="openai-compatible: none or key")
    cn.add_argument(
        "--key",
        metavar="NAME",
        help="the NAME of an operator key binding (never a key and never a file path)",
    )
    cn.add_argument(
        "--preset",
        metavar="ID",
        help="prefill endpoint, id and auth from a preset (`models presets` lists them)",
    )
    cn.add_argument("--status", choices=("active", "disabled"), default="active")
    cn.add_argument("--reason", help="why (a repoint needs 3 to 500 characters)")
    cn.set_defaults(func=cmd_connect)

    pr0 = ms.add_parser("presets", help="the built-in openai-compatible presets (data only)")
    pr0.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    pr0.set_defaults(func=cmd_presets)

    for name in ("pause", "retire"):
        p = ms.add_parser(name, help=f"{name} a catalog entry (restricts at once)")
        common(p, human=True)
        p.add_argument("ref", metavar="provider/model")
        p.add_argument("--reason", help="why (3 to 500 characters; kept in the content store)")
        p.set_defaults(func=cmd_restrict, action=name)

    pr = ms.add_parser(
        "price", help="add a price for an openai-compatible entry (or declare it free)"
    )
    common(pr, human=True)
    pr.add_argument("ref", metavar="provider/model")
    pr.add_argument("--free", action="store_true", help="a zero price (an active loopback only)")
    pr.add_argument(
        "--input-price", dest="input_price", metavar="N", help="US$ per million input tokens"
    )
    pr.add_argument(
        "--output-price", dest="output_price", metavar="N", help="US$ per million output tokens"
    )
    # The old spellings (released): hidden, same meaning; `--output` here is a price, not a format.
    pr.add_argument("--input", dest="input", help=argparse.SUPPRESS)
    pr.add_argument("--output", dest="output", help=argparse.SUPPRESS)
    pr.add_argument("--effective-from", dest="effective_from", metavar="UTC")
    pr.add_argument("--source", help="where the price comes from")
    pr.add_argument(
        "--show", action="store_true", help="only list this model's price versions (writes nothing)"
    )
    pr.add_argument("--reason", help="why (3 to 500 characters)")
    pr.set_defaults(func=cmd_price)
    parse_plain = pr.parse_known_args

    def parse_price(
        arg_strings: Sequence[str] | None = None, namespace: argparse.Namespace | None = None
    ) -> tuple[argparse.Namespace, list[str]]:
        """`--effective-from` and `--source` are required unless `--show` only reads."""
        found, rest = parse_plain(arg_strings, namespace)
        names = (("--effective-from", found.effective_from), ("--source", found.source))
        missing = [name for name, value in names if not value]
        if found.show:
            writes = [
                flag
                for flag, value in (
                    ("--free", found.free),
                    ("--input-price", found.input_price or found.input),
                    ("--output-price", found.output_price or found.output),
                    ("--effective-from", found.effective_from),
                    ("--source", found.source),
                    ("--reason", found.reason),
                )
                if value
            ]
            if writes:
                pr.error(
                    "--show only lists the versions and writes nothing; drop " + ", ".join(writes)
                )
        elif missing:
            pr.error("the following arguments are required: " + ", ".join(missing))
        return found, rest

    pr.parse_known_args = parse_price

    lm = ms.add_parser(
        "local-model",
        help="set up the local-model Compose profile's connection, entry, free price and approval",
    )
    common(lm, human=True)
    lm.add_argument("--model", default="qwen3:8b", help="qwen3:8b (default) or qwen3:4b")
    lm.add_argument(
        "--processing",
        metavar="SCOPE",
        help=f"the host's region: canada, global or region:<id> (default: ${LOCAL_PROCESSING_ENV})",
    )
    lm.add_argument(
        "--yes",
        action="store_true",
        help="record it without asking (without it a terminal asks; no terminal: exit 2)",
    )
    lm.set_defaults(func=cmd_local_model)

    from nuramesh import cli_model_keys

    cli_model_keys.register(ms)

    ck = ms.add_parser("check", help="would a run of this team start? (the start-stage check)")
    common(ck)
    ck.add_argument("--data", required=True, choices=DATA_CLASSES)
    ck.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    ck.set_defaults(func=cmd_check)
