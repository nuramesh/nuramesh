"""The `nuramesh` command line (R1.17): init, validate, try, report, ledger verify, eval.

    nuramesh init my-team --template dev-team
    nuramesh validate examples/dev-team/nuramesh.yaml
    nuramesh try examples/dev-team --fake [--approve-as human:you | --interactive]
    nuramesh report <id> --team examples/dev-team
    nuramesh ledger verify --team examples/dev-team
    nuramesh ledger export --team examples/dev-team --out ledger.jsonl  # then nuramesh_verify.py
    nuramesh eval run --team examples/dev-team --suite dev-basics --agent dana --fake
    nuramesh eval status --team examples/dev-team --agent dana

A team folder keeps its store (the ledger and state) in `.nuramesh/runs.db` unless `--store`
says otherwise. Exit codes: 0 when everything is fine (all tasks done; ledger intact), 1 when the
run finished with tasks not done or the ledger is broken (for `eval`: the suite failed or the agent
isn't eligible), 2 for usage or configuration errors, 3 when a policy, budget or approval refused
the work (nothing was spent past a hard stop; a script can tell this from a failed result), 4 when
the organization is halted or the role is paused (a run or `eval run` stopped by the kill switch;
nothing more was spent).

**Approvals:** without a flag, human gates wait (the report lists them). `--interactive` asks you
at the terminal. `--approve-as human:<id>` approves every human gate as that person **without a
review**, for demos and CI; the ledger records who and that it was scripted. Nothing ever
approves itself silently.
"""

import argparse
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nuramesh import cli_invites, cli_models, cli_password_reset
from nuramesh.cli_aliases import command_words, rewrite
from nuramesh.cli_examples import finish_parser
from nuramesh.cli_rules import add_team, apply_rules
from nuramesh.cli_security import (
    CLI_TOKEN_DAYS,
    cmd_agents_credentials,
    cmd_auth_close_org,
    cmd_auth_erase_owner,
    cmd_auth_key_destroy,
    cmd_auth_key_init,
    cmd_auth_key_rotate,
    cmd_auth_mfa_reset,
    cmd_auth_mfa_status,
    cmd_auth_new_setup_token,
    cmd_auth_purge_sessions,
    cmd_auth_recovery_code_issue,
    cmd_auth_tokens,
    cmd_expire_content,
    cmd_org_halt,
    cmd_org_prompt_cache,
)
from nuramesh.config import ConfigError, load_config_file
from nuramesh.config.shipped_prices import ShippedPriceError, load_shipped, today_utc
from nuramesh.core.budgets import Budget
from nuramesh.core.calibration import calibration_problems
from nuramesh.core.canonical import thaw
from nuramesh.core.config import ConfigValidationError, parse_config
from nuramesh.core.gates import GateSpec
from nuramesh.core.ledger import EventType, LedgerEvent, format_time
from nuramesh.core.models import FakeModel
from nuramesh.core.people import is_human_id
from nuramesh.core.tasks import Task
from nuramesh.engine.budgets import define_budget
from nuramesh.engine.content import (
    DESTROYED_MESSAGE,
    ContentError,
    KeyDestroyedError,
    OrphanedContentError,
)
from nuramesh.engine.governance import ModelGovernanceError
from nuramesh.engine.net_guard import allow_private_network
from nuramesh.engine.org_halt import OrgHalted, RolePaused
from nuramesh.engine.org_ledger import org_stream
from nuramesh.engine.run import (
    EngineSettings,
    HumanDecision,
    RunEngine,
    RunIdTaken,
    build_report,
    new_run_id,
)
from nuramesh.paths import state_path
from nuramesh.plugins import PluginContext, PluginError, load_plugins
from nuramesh.secret_env import database_url, read_secret
from nuramesh.stores import (
    ConcurrencyError,
    EventDraft,
    MemoryStore,
    NotFoundError,
    SqliteStore,
    Store,
    StoreError,
)
from nuramesh.stores.base import StoreBusyError
from nuramesh.team import (
    ShippedListInvalid,
    TeamEnv,
    TeamError,
    load_team_config,
    load_team_module,
    setup_team,
)

if TYPE_CHECKING:
    from nuramesh.engine.anchors import AnchorVerification
    from nuramesh.stores.postgres import PostgresStore

OK, NOT_DONE, USAGE, REFUSED, STOPPED, UNAVAILABLE, INTERRUPTED = 0, 1, 2, 3, 4, 5, 130
# Exit 6 ("done with a warning", spec 6.4) is used only by `minter` (`cli_minter.WARN`); this
# module has no command that exits with it, so it has no name here.

# Set when a store problem was a busy database: a command that then exits 2 exits 5 instead (the
# new exit code for "unavailable, retry may work", OD-12).
_BUSY: ContextVar[bool] = ContextVar("nuramesh_cli_busy", default=False)


# The stores one `main` call opened: `main` closes them when the command returns, so an in-process
# caller (a test, an embedding script) is not left holding the database files open.
_OPENED_STORES: ContextVar[list[Any] | None] = ContextVar("nuramesh_cli_stores", default=None)


def _track[T](store: T) -> T:
    opened = _OPENED_STORES.get()
    if opened is not None:
        opened.append(store)
    return store


def _require_database() -> bool:
    """`NURAMESH_REQUIRE_DATABASE=1` (set in every service of the Compose stack, R1.29): the
    team's SQLite file is never opened, and PostgreSQL is opened check-only (no DDL)."""
    from nuramesh.env_bool import env_bool

    return env_bool("NURAMESH_REQUIRE_DATABASE")


_DATABASE_REQUIRED = (
    "NURAMESH_DATABASE_URL is not set and NURAMESH_REQUIRE_DATABASE=1: "
    "run this through `nuramesh-env`, e.g. `docker compose exec api nuramesh-env nuramesh ...`"
)


def _open_failed_code(exc: Exception) -> int:
    """Exit code for a store that could not be opened: 1 when the schema is out of date (the
    message names `nuramesh db migrate`), else 2."""
    from nuramesh.stores.base import SchemaOutOfDate, StoreBusyError

    if isinstance(exc, StoreBusyError):
        return UNAVAILABLE
    return NOT_DONE if isinstance(exc, SchemaOutOfDate) else USAGE


def _store(root: Path, override: str | None) -> SqliteStore:
    if _require_database():
        raise StoreError(_DATABASE_REQUIRED)
    path = Path(override) if override else state_path(root, "runs.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return _track(SqliteStore(str(path)))


def _open_cli_store(
    root: Path, override: str | None, *, create: bool, missing_ok: bool = False
) -> "SqliteStore | PostgresStore | None":
    """The store a ledger-touching command uses: the same selection as `serve` (PostgreSQL when
    `NURAMESH_DATABASE_URL` is set, else the team's SQLite file). A read-only command passes
    `create=False`: a missing SQLite file is refused, never made. Prints the problem (never the
    URL) and returns None when the store can't be used; the caller returns USAGE."""
    url = database_url()
    if not url and _require_database():
        print(_DATABASE_REQUIRED, file=sys.stderr)
        return None
    if url and not create:  # read-only: an empty database is refused, its tables never made
        try:
            from nuramesh.stores.postgres import PostgresStore

            present = PostgresStore.has_schema(url)
        except (ImportError, StoreError) as exc:
            print(_store_problem(exc), file=sys.stderr)
            return None
        if not present and not missing_ok:
            print("error: the database holds no ledger yet; nothing was created", file=sys.stderr)
            return None
        if not present:  # eval status: nothing recorded yet is an empty store, made nowhere
            return _track(SqliteStore(":memory:"))
    if not url and not create:
        path = Path(override) if override else state_path(root, "runs.db")
        if not path.is_file() and missing_ok:  # nothing recorded yet: an empty store, made nowhere
            return _track(SqliteStore(":memory:"))
        if not path.is_file():
            print(f"error: no ledger store at {path}; nothing was created", file=sys.stderr)
            return None
    try:
        return _open_server_store(root, override)
    except (ImportError, StoreError) as exc:
        print(_store_problem(exc), file=sys.stderr)
        return None


def _open_existing_store(root: Path, override: str | None) -> "SqliteStore | PostgresStore":
    """A read-only command's store: a missing SQLite file or an empty database is refused, and
    nothing is created."""
    store = _open_cli_store(root, override, create=False)
    if store is None:
        raise StoreError("no ledger store here")
    return store


def _keyed_overrides(config: Any, store: Store) -> Any:
    """The config's price overrides with their reasons keyed for the ledger (T47). Only builds a
    content service (needs the `content` extra and a key directory) when there are overrides."""
    if not config.price_overrides:
        return ()
    from nuramesh.engine.content import (
        KeyDestroyedError,
        OrphanedContentError,
        configured_content_service,
    )

    try:
        return configured_content_service(store).key_overrides(
            config.org.id, config.price_overrides
        )
    except (KeyDestroyedError, OrphanedContentError):
        raise  # `run`, `eval` and the worker factory each say what a destroyed key means for them
    except ContentError as exc:  # no `content` extra, or a key directory the ring refuses
        raise ValueError(f"price overrides need the content store's key: {exc}") from None


def _worker_overrides(config: Any, store: Store) -> tuple[Any, str | None]:
    """The worker factory's overrides, or `()` and the gate code when the key gate refused them."""
    from nuramesh.engine.content import configured_content_service, is_gate_refusal

    if not config.price_overrides:
        return (), None
    try:
        service = configured_content_service(store)
        return service.key_overrides(config.org.id, config.price_overrides), None
    except ContentError as exc:
        if is_gate_refusal(exc):
            return (), str(exc.code)
        raise ValueError(f"price overrides need the content store's key: {exc}") from None


REPAIR_HINT = (
    " The key tombstone's ring copy is missing here; `nuramesh try` does not repair it: "
    "start `nuramesh serve` or `nuramesh worker` once."
)


def _checks_content(store: Store) -> Any:
    """The content service the deny checks ask, or None where the `content` extra is missing (the
    store tombstone alone decides, nothing is captured), or a stand-in that makes every question
    unsure (a service that cannot be built is fail closed: unsure = erased, owner Q2)."""
    from nuramesh.engine.content import ContentExtraMissing, configured_content_service
    from nuramesh.engine.deny_checks import UnavailableContent

    try:
        return configured_content_service(store)
    except ContentExtraMissing:
        return None
    except Exception:
        return UnavailableContent()


def _retired_problem(store: Store, org_id: str, *, repair_hint: bool = False) -> str | None:
    """Why a run must not start under this organization id (it was erased, R1.37, or whether it
    was could not be read: fail closed), or None. With `repair_hint`, a missing ring copy names the
    repair path (state 5: serve or worker)."""
    from nuramesh.engine.deny_checks import erased_denial

    content = _checks_content(store)
    denial = erased_denial(store, org_id, content=content)
    if denial is None:
        return None
    text = denial.text
    if repair_hint and denial.kind == "erased" and content is not None:
        try:
            if content.keys.tombstone(org_id) is None:
                text += REPAIR_HINT
        except Exception:
            pass
    return text


def _reconcile_at_start(service: Any, org_id: str) -> None:
    """The start-up repair of the active organization's key tombstone (R1.37 section 6.1). It never
    stops a start: a ring that can't be read is reported and every use of it refuses."""

    try:
        service.reconcile_tombstones(org_id)
    except (ContentError, StoreError, OSError) as exc:
        print(f"warning: the key tombstones could not be reconciled: {exc}", file=sys.stderr)
    try:  # ND202: a deployment that switches to hosted caps its older reasons at once
        service.clamp_hosted_reasons()
    except (ContentError, StoreError, OSError) as exc:
        print(f"warning: older reasons could not be capped at 90 days: {exc}", file=sys.stderr)


def _reconcile_run_start(store: Store, org_id: str) -> None:
    from nuramesh.engine.content import configured_content_service

    try:
        service = configured_content_service(store)
        if service.retired(org_id) is not None:
            return  # a tombstoned organization: `run` repairs nothing, serve and worker do
    except ContentError:
        return  # no content store (or an unreadable tombstone): nothing to reconcile here
    _reconcile_at_start(service, org_id)


def _record_halt_refusal(store: Store, org_id: str, run_id: str, episode: int | None) -> None:
    """A halt in force at start: write the engine's ONE refusal record (`model.blocked`
    `org_halted`, stage `start`) on the organization stream, or, for an erased organization, only
    the coalesced security-log row (no org-scoped write). Nothing else is written."""
    from nuramesh.engine.content import (
        ContentExtraMissing,
        configured_content_service,
        default_key_dir,
    )
    from nuramesh.engine.halt_refusal import (
        record_halt_refusal,
        ring_tombstone_absent,
    )

    content: Any = None
    cannot_rule_out = False
    try:
        content = configured_content_service(store)
    except ContentExtraMissing:
        # the only special case: no content service, so read the ring tombstone FILE directly;
        # anything but "clearly absent" counts as erased
        try:
            cannot_rule_out = not ring_tombstone_absent(default_key_dir(), store.store_id(), org_id)
        except Exception:
            cannot_rule_out = True
    except Exception:  # every other ContentError (a malformed store id too) and any other error:
        cannot_rule_out = True  # erasure cannot be ruled out, treated as erased (fail closed)
    record_halt_refusal(
        store,
        org_id,
        run_id,
        episode=episode,
        content=content,
        assume_erased=cannot_rule_out,
    )


def _deny_checks(
    store: Store, config: Any, *, command: str = "run", run_id: str = ""
) -> int | None:
    """The refusals that come before anything writes (security order): for
    `run`, a halt in force (exit 4), then an erased organization id (exit 3), then a lost key
    (exit 3); for `eval`, the halt first (every eval, with or without price overrides), then the
    key check (every eval hashes its responses, so it always runs; PH-KEY R5b).
    One function (`engine.deny_checks`) decides; all of it only reads."""
    from nuramesh.engine.deny_checks import deny_checks

    org_id = config.org.id
    from nuramesh.engine.content import default_ledger_hashing
    from nuramesh.engine.halt_refusal import HaltWindowError, halt_window_seconds

    try:  # T154 R12: a bad window is a configuration error, never a silent default
        halt_window_seconds()
    except HaltWindowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return USAGE
    content = _checks_content(store)
    # every run and eval hashes (PH-KEY R5b): the key check always runs, through the ledger
    # hashing when there is no content service (no `cryptography` needed)
    denial = deny_checks(
        store, org_id, content=content, hashing=lambda: default_ledger_hashing(store)
    )
    if denial is None:
        return None
    if denial.is_halt:
        _record_halt_refusal(store, org_id, run_id, denial.halt_seq)  # the ONE record, then exit 4
        if command == "run":
            print(f"the run was not started (model governance): {denial.text}", file=sys.stderr)
        elif command == "calibrate":
            print(
                f"the organization is halted; the calibration made no model call ({denial.text})",
                file=sys.stderr,
            )
        else:  # the eval's established text
            print(
                f"the organization is halted; the eval made no model call ({denial.text})",
                file=sys.stderr,
            )
        return STOPPED
    text = denial.text
    if command == "run" and denial.kind == "erased":
        text = _retired_problem(store, org_id, repair_hint=True) or text
    if command != "run":
        # spec/key-tombstone D21 (ND137): the eval keeps its established text, also for an erased id
        text = _destroyed_message_text(DESTROYED_MESSAGE if denial.kind == "erased" else text)
    print(text if denial.kind == "erased" else _with_code(text, denial.error), file=sys.stderr)
    return REFUSED


def _destroyed_message_text(text: str) -> str:
    return f"the ledger hashes and price overrides need the organization's key: {text}"


def _bail(store: "SqliteStore | PostgresStore | None", text: str) -> int:
    """`_invalid`, closing the store a bare run opened early."""
    if store is not None:
        store.close()
    return _invalid(text)


def _with_code(text: str, exc: BaseException | None) -> str:
    """The refusal text with the gate's reason token (`org_key_missing`, ...) beside it."""
    code = getattr(exc, "code", None)
    return f"{text} [{code}]" if isinstance(code, str) and code else text


def _destroyed_message(exc: Exception) -> str:
    return _with_code(f"price overrides need the content store's key: {exc}", exc)


def _same_budget(stored: Budget, wanted: Budget) -> bool:
    """Equal, treating an unset `on_breach` as the default it stands for."""
    return (
        stored.model_copy(update={"on_breach": None})
        == wanted.model_copy(update={"on_breach": None, "org_id": stored.org_id})
        and stored.breach_mode is wanted.breach_mode
    )


def _define(store: Store, budget: Budget, org_id: str) -> None:
    """Define a budget; an existing one must be exactly the configured one.

    Limits never change silently: if the configuration says something else than what is stored,
    the run is refused (change a stored limit with a human `unblock_budget`, which is ledgered).
    """
    try:
        define_budget(store, budget, actor="service:cli", org_id=org_id)
    except ConcurrencyError:
        stored = store.get_balance(org_id, budget.id)
        if stored is None or not _same_budget(stored.balance.budget, budget):
            raise ValueError(
                f"budget '{budget.id}' already exists with a different limit, scope, window or "
                "on_breach than the configuration says; refusing to run with a limit you may not "
                "expect"
            ) from None


class ScriptedApprover:
    """Approves every human gate as `human_id`, saying in the reason that no one reviewed it."""

    def __init__(self, human_id: str, store: Store) -> None:
        if not is_human_id(human_id):
            raise ValueError(
                "--approve-as needs an opaque id like human:u_7k2x9q (letters, digits, _ and -; "
                "no email or name)"
            )
        self.human_id, self.store = human_id, store

    def __call__(self, task: Task, gate: GateSpec, output: Any) -> HumanDecision:
        reason = f"scripted approval via --approve-as {self.human_id} (no human review)"
        with self.store.transaction() as uow:  # the ledger says this approval wasn't a review
            uow.append(
                EventDraft(
                    stream=f"run:{task.run_id}",
                    type=EventType.DECISION_RECORDED,
                    actor="service:cli",
                    run_id=task.run_id,
                    task_id=task.id,
                    payload={
                        "kind": "scripted_approval",
                        "gate": gate.id,
                        "approver": self.human_id,
                        "human_review": False,
                    },
                )
            )
        return HumanDecision(
            decided_by=self.human_id, findings={c: (True, reason) for c in gate.checks}
        )


class TerminalApprover:
    """Asks at the terminal for each criterion of a human gate."""

    def __init__(self, human_id: str) -> None:
        self.human_id = human_id

    def __call__(self, task: Task, gate: GateSpec, output: Any) -> HumanDecision | None:
        print(f"\nHuman gate '{gate.id}' for task {task.id}: {task.title}")
        shown = output if isinstance(output, str) else json.dumps(output, indent=2, default=str)
        print("Output:\n" + shown[:4_000])
        findings: dict[str, tuple[bool, str]] = {}
        for criterion in task.definition_of_done.criteria:
            if criterion.id not in gate.checks:
                continue
            answer = input(f"  {criterion.text} -- approve? [y/n/skip] ").strip().lower()
            if answer not in ("y", "n"):
                return None  # no decision: the task waits
            reason = ""
            while not reason:
                reason = input("  Reason (required): ").strip()
            findings[criterion.id] = (answer == "y", reason)
        return HumanDecision(decided_by=self.human_id, findings=findings)


def _fake_only_problem(
    config: Any,
    agents: Sequence[str] | None = None,
    refs: Mapping[str, str | None] | None = None,
) -> str | None:
    """`--fake` answers every call from the fake model, so every agent's EFFECTIVE model must be a
    `fake/*` model: otherwise results (costs, evals) would be recorded under a real model that
    never answered. `refs` is `agent -> effective ref` (the ledger's assignments over the
    configuration); without it only the configuration is judged (a first, cheap check)."""
    names = list(agents) if agents is not None else [a.id for a in config.org.agents]
    for name in names:
        ref = refs[name] if refs is not None and name in refs else config.org.model_for(name)
        if ref is not None and not ref.startswith("fake/"):
            return (
                f"--fake can only run agents whose model is fake/*; '{name}' uses {ref}. "
                "Real models are not set up yet."
            )
    return None


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        parsed = parse_config(load_config_file(args.path), shipped=load_shipped().prices)
    except ConfigError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return USAGE
    except ConfigValidationError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except ShippedPriceError as exc:  # fails closed, like every command (exit 3)
        print(f"shipped price list invalid: {exc}", file=sys.stderr)
        return REFUSED
    if args.store or database_url():
        # with a store: an erased organization id is retired (R1.37, F01); read-only, so a missing
        # SQLite file is refused, never made
        opened_check = _open_cli_store(Path(args.path).resolve().parent, args.store, create=False)
        if opened_check is None:
            return USAGE
        check = opened_check
        try:
            retired = _retired_problem(check, parsed.org.id)
        finally:
            check.close()
        if retired:
            print(f"invalid: org_id_retired: {retired}", file=sys.stderr)
            return USAGE
    cli_models.print_price_warnings(parsed, today_utc())
    print(f"{args.path}: valid")
    return OK


def _cap_problem(budgets: Sequence[Any], run_id: str, confirm: str | None) -> str | None:
    """Why the owner's `--confirm-cap` is not acceptable, or None. Only this run's USD run budget
    counts (a per-agent budget caps nothing about the run)."""
    from nuramesh.core.budgets import BudgetScope
    from nuramesh.core.money import Currency

    own = [b for b in budgets if b.scope is BudgetScope.RUN and b.scope_id == run_id]
    if not own or any(b.limit.currency is not Currency.USD for b in own):
        return (
            "this team defines no US-dollar run budget for the run, so there is no cap to confirm"
        )
    cap = max(b.limit.amount for b in own)
    try:
        confirmed = Decimal(confirm) if confirm is not None else None
    except InvalidOperation:
        confirmed = None
    if confirmed is None or not confirmed.is_finite() or confirmed != cap:
        return (
            f"this run is capped at US${cap} (its run budget); confirm you accept spending up "
            f"to that with --confirm-cap {cap}"
        )
    return None


def _real_refs_problem(refs: Mapping[str, str | None]) -> str | None:
    """A run without `--fake` can't answer a `fake/*` model."""
    for agent, ref in refs.items():
        if ref is not None and ref.startswith("fake/"):
            return f"'{agent}' uses {ref}; add --fake to run on the fake model"
    return None


def _hard_exit(code: int) -> None:
    """Leave now, without waiting for threads that are stuck (flushes the output first)."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _money_text(value: Any) -> str:
    """An amount with at least two decimals (30 -> 30.00); finer amounts keep their digits."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    return f"{amount:f}" if -amount.as_tuple().exponent >= 2 else f"{amount:.2f}"  # type: ignore[operator]


def _after(item: Mapping[str, Any]) -> str:
    """What happens next for a budget in an alert, from its own facts, not from what is missing."""
    if item.get("held") is True:
        return "held until a human lifts it"
    if item.get("resets_at"):
        return f"resets at {item['resets_at']}"
    if item.get("window_closed"):
        return "that window had already ended; nothing is blocked"
    if not item.get("window"):
        return "no reset: the lifetime limit is used up (a human can raise it)"
    return "no reset time recorded"


def breach_alerts(store: Store, run_id: str) -> list[str]:
    """`BREACH ALERT` lines for a run's budget hard stops and overspends (T56), one per event and
    budget, in ledger order.

    Read from the run's ledger (`budget.exceeded` hard stops and `budget.breached`), so the line
    and the log-alert rule `budget-breach` see the same facts: the budget, its window, and
    when it resets or that only a human lifts it.
    """
    lines: list[str] = []
    for event in store.read_stream(f"run:{run_id}"):
        if event.type == "budget.exceeded" and event.payload.get("reason") == "hard_stop":
            items = list(event.payload.get("shortfalls") or [])
            verb = "hard stop"
        elif event.type == "budget.breached":
            items = list(event.payload.get("budgets") or [])
            verb = "limit passed"
        else:
            continue
        for item in items:
            if not isinstance(item, Mapping) or "budget" not in item:
                continue
            window = item.get("window")
            where = f"{item.get('scope')} {item.get('scope_id')}" + (
                f", per {window}" if window else ""
            )
            lines.append(
                f"BREACH ALERT: {verb} on budget {item['budget']} ({where}), limit "
                f"{_money_text(item.get('limit'))}; {_after(item)}"
            )
    return lines


def _invalid(text: str) -> int:
    """A malformed request, or a configuration or plan that is wrong by itself (exit 2)."""
    print(f"the run was not started (invalid request): {text}", file=sys.stderr)
    return USAGE


def cmd_run(args: argparse.Namespace) -> int:
    """Run a team folder. Without `--fake` the run is real: it starts only if the organization's
    approved catalog and a usable connection exist (spec/model-governance section 10.1)."""
    if not args.team:
        from nuramesh import cli_prompts

        if not cli_prompts.can_ask(args):
            return cli_prompts.missing("TEAM")
        args.team = cli_prompts.ask("team folder: ")
        if not args.team:
            return cli_prompts.missing("TEAM")
    root = Path(args.team).resolve()
    # 1. arguments (exit 2, before the store is opened)
    if args.provider:
        return _invalid(
            "--provider is gone: the providers a run uses come from its models and their "
            "connections (see PUT /v1/models/connections/<provider>)"
        )
    if args.fake and (args.data or args.confirm_cap):
        return _invalid("--data and --confirm-cap are for real runs (leave out --fake)")
    if bool(args.anchor_repo) != bool(args.anchor_key):
        return _invalid("--anchor-repo and --anchor-key go together")
    if args.anchor_push and not args.anchor_repo:
        return _invalid("--anchor-push needs --anchor-repo and --anchor-key")
    if args.approve_as and args.interactive:
        return _invalid("use either --approve-as or --interactive, not both")
    if args.human and not is_human_id(args.human):
        return _invalid("--human needs an opaque id like human:u_7k2x9q (no email or name)")
    from nuramesh.hosted import HostedConfigError

    try:
        _hosted_env()  # ND220 point 3: a bad NURAMESH_HOSTED is one line, before anything runs
    except HostedConfigError as exc:
        print(exc, file=sys.stderr)
        return USAGE
    if not args.fake:  # openai-compatible 4.1 (minor 8): a set but unusable binding file
        from nuramesh.engine.openai_compatible import startup_problem as binding_problem

        bindings = binding_problem()
        if bindings is not None:
            print(bindings, file=sys.stderr)  # exactly one line (spec openai-compatible, K17)
            return USAGE
    # 2. configuration and team module (layer 1)
    try:
        config = load_team_config(root, args.config)
        module = load_team_module(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        return _invalid(str(exc))
    bare = not args.fake
    run_id = args.run_id or (
        new_run_id()
        if _hosted_env()
        else "run-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    )
    store: SqliteStore | PostgresStore | None = None
    if bare:
        store = _open_cli_store(root, args.store, create=True)
        if store is None:
            return USAGE
        # deny checks first: a halt (exit 4), then an erased org id (exit 3); only reads (R1.37 s9)
        denied = _deny_checks(store, config, run_id=run_id)
        if denied is not None:
            store.close()
            return denied
        cli_models.seed_and_warn(store, config)  # writes: only after every deny check
        # 3. the bare-run precheck (exit 3): a usable catalog and connection must exist
        stopped = cli_models.bare_run_problem(cli_models.context_for(root, config, store))
        if stopped is not None:
            print(stopped, file=sys.stderr)
            store.close()
            return REFUSED
    # 4. the remaining request checks (exit 2)
    refs = cli_models.effective_agent_refs(
        cli_models.context_for(root, config, store or MemoryStore())
    )
    problem = _fake_only_problem(config, refs=refs) if args.fake else _real_refs_problem(refs)
    if problem:
        return _bail(store, problem)
    if bare and not args.data:
        return _bail(
            store,
            "a real run must say what data it sends: --data synthetic (demos, evals, "
            "development) or --data customer; a global deployment accepts only synthetic "
            "data (ND45)",
        )
    if args.anchor_repo:
        from nuramesh.engine.anchors import AnchorError

        try:
            # before the run spends anything
            _anchor_preflight(args.anchor_repo, args.anchor_key, args.anchor_push)
        except (AnchorError, OSError) as exc:
            return _bail(store, f"anchoring can't work, so the run was not started: {exc}")
    if bare:  # confirm the cap on a throwaway store: a refusal must leave no file or entry
        probe = TeamEnv(
            root=root,
            config=config,
            store=MemoryStore(),
            run_id=run_id,
            fake=False,
            sandbox=lambda: None,
        )
        try:
            budgets = setup_team(module, probe).run_budgets
        except TeamError as exc:
            return _bail(store, str(exc))
        problem = _cap_problem(budgets, run_id, args.confirm_cap)
        if problem:
            return _bail(store, problem)
    if store is None:  # --fake: the store is opened only now (checks 1, 2 and 4 write nothing)
        store = _open_cli_store(root, args.store, create=True)
        if store is None:
            return USAGE
        denied = _deny_checks(store, config, run_id=run_id)
        if denied is not None:
            store.close()
            return denied
        cli_models.seed_and_warn(store, config)  # writes: only after every deny check
        # the ledger's assignments are known only now: judge the EFFECTIVE models (exit 2)
        effective = _fake_only_problem(
            config,
            refs=cli_models.effective_agent_refs(cli_models.context_for(root, config, store)),
        )
        if effective:
            return _bail(store, effective)
    if store is not None:
        _reconcile_run_start(store, config.org.id)  # writes: last, after every refusal above

    opened: list[Any] = []  # sandboxes this run opened: Stop kills what they are running
    engines: list[RunEngine] = []  # (filled below) to ask whether the stop hooks already ran

    def sandbox() -> Any:
        from nuramesh.engine.sandbox import Mode, open_sandbox

        box = open_sandbox(
            store, run_id=run_id, mode=Mode.LOCAL, allow_unsandboxed=args.allow_unsandboxed
        )
        opened.append(box)
        if engines and engines[0].stop_hooks_fired and hasattr(box, "cancel"):
            box.cancel()  # opened after the stop: it must not run anything
        return box

    env = TeamEnv(
        root=root, config=config, store=store, run_id=run_id, fake=args.fake, sandbox=sandbox
    )
    try:
        team = setup_team(module, env)
    except TeamError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    if args.fake and not isinstance(team.fake_provider, FakeModel):
        print("--fake needs the team's fake_provider to be a FakeModel", file=sys.stderr)
        return USAGE
    if not callable(team.plan):  # load-time check: a bad calibration suite stops the run first
        found = [
            f"task '{item.task.id}': {problem}"
            for item in team.plan
            for problem in calibration_problems(
                item.task.definition_of_done, config.org, config.calibration
            )
        ]
        if found:
            print("\n".join(found), file=sys.stderr)
            return USAGE
    for budget in (*config.budgets, *team.run_budgets):
        _define(store, budget, config.org.id)
    approver = team.approver
    if args.approve_as:
        approver = ScriptedApprover(args.approve_as, store)
    elif args.interactive:
        approver = TerminalApprover(args.human or "human:operator")
    models_ctx = cli_models.context_for(root, config, store)
    governance = cli_models.governance_for(models_ctx)
    provider: Any = team.fake_provider
    if bare:  # the provider for each call is built from the connection the resolver chose
        from nuramesh.engine.governance import GovernedProviders

        provider = GovernedProviders(
            fake=team.fake_provider, credentials=cli_models.credentials_for(models_ctx)
        )
    if provider is None:
        print("the team has no model provider", file=sys.stderr)
        return USAGE
    try:
        keyed = _keyed_overrides(config, store)
    except (KeyDestroyedError, OrphanedContentError) as exc:
        print(_destroyed_message(exc), file=sys.stderr)
        return REFUSED
    engine = RunEngine(
        store,
        config.org,
        provider=provider,
        prices=config.prices,
        policies=config.effective_policies,
        workers=team.workers,
        approver=approver,
        evals=config.evals,
        presets=config.presets,
        calibration=config.calibration,
        canon=config.canon,
        overrides=keyed,
        gate_runners=team.gate_runners,
        settings=EngineSettings(max_parallel=args.parallel, interrupt_actor=args.human),
        governance=governance,
        data_class=args.data or "synthetic",
        breakers=config.breakers.rules,
        hosted_ids=_hosted_env(),
    )

    def cancel_sandboxes() -> None:
        for box in opened:
            if hasattr(box, "cancel"):
                box.cancel()

    engine.on_stop(cancel_sandboxes)
    engines.append(engine)
    try:
        report = engine.run(run_id, team.plan)
    except (
        RunIdTaken
    ) as exc:  # R3.02 F1: a run id is used once; nothing was written on the run stream
        print(exc, file=sys.stderr)
        return USAGE
    except ModelGovernanceError as exc:  # the start stage refused: nothing was created
        print("the run was not started (model governance):", file=sys.stderr)
        for block in exc.blocks:
            print(block.text, file=sys.stderr)
        return STOPPED if any(b.reason == "org_halted" for b in exc.blocks) else REFUSED
    except KeyboardInterrupt:
        # a second Ctrl-C: the ledger says `aborted`; don't wait for workers that never return
        print(
            f"run {run_id} was ABORTED by a second interrupt (see `nuramesh report`)",
            file=sys.stderr,
        )
        _hard_exit(130)
        return STOPPED  # (only when _hard_exit is replaced in tests)
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render_text())
    for alert in breach_alerts(store, run_id):
        print(alert, file=sys.stderr)
    for line in cli_models.blocked_lines(store, run_id):
        print(f"MODEL BLOCKED: {line}", file=sys.stderr)
    run_code = OK if report.counts == {"done": len(report.tasks)} and report.ledger_ok else NOT_DONE
    if report.stopped:
        print(
            f"run {run_id} was STOPPED by {report.stop_requested_by or 'a human'}; "
            "unfinished tasks were cancelled and nothing more was spent",
            file=sys.stderr,
        )
        run_code = STOPPED
    if args.anchor_repo:
        # the run's stream and its own organization's stream (budgets, kill switch, sign-ins:
        # ND116); another organization in a shared store is left to its own runs and the hourly job
        anchor_streams = [f"run:{run_id}", org_stream(config.org.id)]
        code = _anchor_export(
            store, args.anchor_repo, args.anchor_key, anchor_streams, push=args.anchor_push
        )
        if code != OK:
            return run_code if run_code != OK else code
    return run_code


def cmd_report(args: argparse.Namespace) -> int:
    from nuramesh.cli_everyday import pick_run_for, wants_json

    picked = pick_run_for(args, recent=True)
    if picked is not None:
        return picked
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    store = _open_cli_store(root, args.store, create=False)
    if store is None:
        return USAGE
    from nuramesh.cli_everyday import _own_org

    if not store.tasks_for_run(args.run_id) or not _own_org(store, args.run_id, config.org.id):
        # another organization's run, and a run with no recorded organization, are invisible
        print(f"no run '{args.run_id}' in this store", file=sys.stderr)
        return USAGE
    if wants_json(args):  # the route's body: reasons redacted (GET /v1/runs/{run_id}/report)
        route = build_report(store, config.org, args.run_id, redact_reasons=True)
        print(json.dumps(route.to_dict(), indent=2))
        report = build_report(store, config.org, args.run_id)
    else:
        report = build_report(store, config.org, args.run_id)
        print(json.dumps(report.to_dict(), indent=2) if args.json else report.render_text())
    done = report.counts == {"done": len(report.tasks)}
    return OK if done and report.ledger_ok else NOT_DONE


def _hosted_startup_problem() -> str | None:
    """Why `serve` or `worker` must not start under this `NURAMESH_HOSTED` (ND202), or None."""
    from nuramesh.engine.content import hosted_startup_problem

    return hosted_startup_problem()


def _hosted_env() -> bool:
    """`NURAMESH_HOSTED` as a bool; `HostedConfigError` (one line, names the variable) for a bad
    value. The shared parser lives in `nuramesh.hosted` (ND220)."""
    from nuramesh.hosted import hosted_from_env

    return hosted_from_env()


def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply pending PostgreSQL migrations and exit (R1.29). Needs `NURAMESH_DATABASE_URL`
    (never falls back to SQLite). Idempotent and safe next to another opener (advisory lock).
    `--grant-to ROLE` then gives that service role its least-privilege grants (ND188 N2)."""
    url = database_url()
    if not url:
        print("migrate needs NURAMESH_DATABASE_URL (it never uses SQLite)", file=sys.stderr)
        return USAGE
    try:
        from nuramesh.stores.postgres import PostgresStore
        from nuramesh.stores.postgres_roles import apply_grants, valid_role
    except ImportError:
        print(_store_problem(ImportError()), file=sys.stderr)
        return USAGE
    role = args.grant_to
    if role is not None and not valid_role(role):
        print("--grant-to needs a plain lower-case role name", file=sys.stderr)
        return USAGE
    from nuramesh.stores import postgres_plan

    try:
        plan = postgres_plan.plan_migrations(url)
    except StoreError as exc:
        print(f"couldn't read the database: {exc}", file=sys.stderr)  # never the URL
        return NOT_DONE
    if args.plan:
        print(postgres_plan.render(plan))
        return NOT_DONE if plan.missing_rights else OK
    if plan.missing_rights:
        print(f"refusing to migrate: {plan.missing_rights[0]}", file=sys.stderr)
        return NOT_DONE
    ref, problem = postgres_plan.acknowledgement(plan, args.i_have_a_backup, os.environ)
    if problem:
        print(problem, file=sys.stderr)
        return NOT_DONE
    try:
        store = PostgresStore(url)
    except StoreError as exc:
        print(f"couldn't migrate the database: {exc}", file=sys.stderr)  # never the URL
        return NOT_DONE
    try:
        version = store.schema_version
        if ref is not None and plan.pending:
            store.record_migration_acknowledgement(
                {
                    "kind": "migration_acknowledged",
                    "from_version": plan.current,
                    "to_version": version,
                    "not_additive": [m.number for m in plan.non_additive],
                    "backup": ref,
                }
            )
    finally:
        store.close()
    if role is not None:
        try:
            apply_grants(url, role)
        except Exception as exc:  # noqa: BLE001
            print(f"couldn't grant to {role} ({type(exc).__name__})", file=sys.stderr)
            return NOT_DONE
    print(f"schema at version {version}")
    return OK


def cmd_start(args: argparse.Namespace) -> int:
    """Start a run from a plan file on the host (R1.20 PR B, design decision: option a). Like
    `stop` it writes to the store directly, so the person is a claim (`--human`), not a sign-in:
    the id must be an ACTIVE owner of the team's organization, the ledger records `via: cli` and
    the OS user (no host name), and a hosted deployment refuses it. The plan is the JSON body of
    `POST /v1/runs`; a runner (`nuramesh worker --engine light`) picks the request up."""
    from pydantic import ValidationError

    from nuramesh.core.auth import Role
    from nuramesh.core.content import ContentKind
    from nuramesh.engine.deny_checks import deny_checks
    from nuramesh.engine.org_ledger import org_stream
    from nuramesh.engine.run_engine import LIGHT, EngineSelectionError, select_engine
    from nuramesh.engine.run_request import (
        PlanProblem,
        RunStartSettings,
        RunStartSettingsError,
        StartRunBody,
        derive_run_id,
        validate_shape,
    )
    from nuramesh.engine.run_start import record_start_request
    from nuramesh.engine.start_budget import RunBudgetRefused, check_start_headroom
    from nuramesh.server.recovery import operator_actor
    from nuramesh.stores.base import EventDraft

    def refuse(code: str, text: str, exit_code: int = REFUSED) -> int:
        print(f"refused ({code}): {text}", file=sys.stderr)
        return exit_code

    from nuramesh.hosted import HostedConfigError

    try:
        hosted = _hosted_env()
    except HostedConfigError as exc:
        return refuse("hosted_config_invalid", str(exc), USAGE)
    if hosted:
        return refuse(
            "run_start_cli_hosted", "a hosted deployment starts runs through the API only"
        )
    if not is_human_id(args.human):
        return refuse("human_invalid", "--human needs an opaque id like human:u_7k2x9q", USAGE)
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
        body = StartRunBody.model_validate_json(Path(args.plan).read_text("utf-8"))
        validate_shape(body.tasks)
        settings = RunStartSettings.from_env(os.environ)
        if select_engine(os.environ).engine != LIGHT:
            raise EngineSelectionError("the CLI starts runs on the light engine only")
    except (TeamError, ConfigError, ConfigValidationError, OSError, ValidationError) as exc:
        return refuse("plan_invalid", type(exc).__name__, USAGE)
    except PlanProblem as exc:
        return refuse(exc.code, "the plan is refused", USAGE)
    except (EngineSelectionError, RunStartSettingsError) as exc:
        return refuse("engine_unavailable", str(exc), USAGE)
    if body.data_class == "customer" and not settings.customer_data_enabled:
        return refuse("customer_data_not_enabled", "customer data is not enabled", USAGE)
    org_id = config.org.id
    try:
        store = _open_server_store(root, args.store)
    except (ImportError, StoreError) as exc:
        print(_store_problem(exc), file=sys.stderr)
        return USAGE
    try:
        with store.transaction() as uow:
            user = uow.get_user(org_id, args.human)
        if user is None:
            return refuse("human_unknown", "no such person in this organization")
        if user.erased_at is not None or user.disabled_at is not None:
            return refuse("human_inactive", "that person is disabled or erased")
        if user.role is not Role.OWNER:
            return refuse("human_not_owner", "only an owner may start a run from the host")
        from nuramesh.engine.content import ContentError, configured_content_service

        try:
            service = configured_content_service(store, config.content)
        except ContentError as exc:
            return refuse("content_unavailable", str(exc), USAGE)
        denial = deny_checks(store, org_id, content=service)
        if denial is not None:
            code = "org_halted" if denial.is_halt else f"org_{denial.kind}"
            return refuse(code, denial.text)
        run_id = derive_run_id(org_id, args.human, None)
        try:
            check_start_headroom(store, config.org, run_id)
        except RunBudgetRefused as refused:
            return refuse(refused.code, "the organization has no budget to start a run")
        for item in body.tasks:
            if item.template is not None:
                with store.transaction() as uow:
                    if not service.has_kind(uow, org_id, item.template, ContentKind.TASK_TEMPLATE):
                        return refuse("template_unknown", "a template is unknown", USAGE)
        if args.dry_run:
            print("dry run: every check passed; nothing was written")
            return OK
        with store.transaction() as uow:
            outcome = record_start_request(
                uow,
                service,
                org_id=org_id,
                run_id=run_id,
                actor_id=args.human,
                tasks=body.tasks,
                data_class=body.data_class,
                engine=LIGHT,
                idempotency_key_hash=None,
                plan_text_days=settings.plan_text_days,
            )
            uow.append(
                EventDraft(
                    stream=org_stream(org_id),
                    type=EventType.DECISION_RECORDED,
                    actor=args.human,
                    payload={
                        "kind": "run_start_requested",
                        "run_id": run_id,
                        "via": "cli",
                        "os_user": operator_actor(),  # uid-based, cleaned, never a host name
                    },
                )
            )
        if outcome != "created":
            return refuse("run_id_conflict", "this run id exists", NOT_DONE)
        print(f"requested run {run_id}; a runner (nuramesh worker --engine light) starts it")
        return OK
    finally:
        store.close()


def _stop_body(run_id: str, outcome: Any) -> dict[str, Any]:
    """The keys `POST /v1/runs/{run_id}/stop` answers with (`--output json`, BC4)."""
    req = outcome.request
    answer: dict[str, Any] = {
        "run_id": run_id,
        "status": "stopping",
        "already_requested": outcome.already_requested,
        "requested_by": req.by,
        "requested_at": req.at.isoformat(),
        "ledger_seq": req.seq,
        "via": req.via,
    }
    if req.reason_withheld is not None:
        answer["reason_withheld"] = req.reason_withheld
        answer["stop_reason_withheld"] = req.withheld_text
    return answer


def cmd_stop(args: argparse.Namespace) -> int:
    """Ask a run to stop: appends `run.stop_requested` to its stream (the same event the API's
    Stop writes). The engine ends the run cleanly at its next step."""
    from nuramesh import cli_prompts
    from nuramesh.cli_everyday import pick_run_for, wants_json
    from nuramesh.engine.run_control import RunFinished, request_stop

    if args.human is None and cli_prompts.can_ask(args):
        args.human = cli_prompts.ask("your id (human:<id>): ") or None
    if args.human is None:
        return cli_prompts.missing("--human")
    if not is_human_id(args.human):
        print("--human needs an opaque id like human:u_7k2x9q (no email or name)", file=sys.stderr)
        return USAGE
    picked = pick_run_for(args, recent=False)
    if picked is not None:
        return picked
    if args.reason is None and cli_prompts.can_ask(args):
        args.reason = cli_prompts.ask("reason (optional, empty = none): ") or None
    root = Path(args.team).resolve()
    try:  # the store the worker uses: PostgreSQL when NURAMESH_DATABASE_URL is set
        store = _open_server_store(root, args.store)
    except (ImportError, StoreError) as exc:
        print(_store_problem(exc), file=sys.stderr)
        return USAGE
    first = list(store.read_stream(f"run:{args.run_id}", after_seq=0, limit=1))
    if not first:
        print(f"no run '{args.run_id}' in this store", file=sys.stderr)
        return USAGE
    run_org = first[0].payload.get("org") if first[0].type == "run.started" else None
    team_org: str | None = None
    team_settings = None
    try:
        team_config = load_team_config(root)
        team_org = team_config.org.id
        team_settings = team_config.content
    except (TeamError, ConfigError, ConfigValidationError):
        pass  # a folder without a readable organization can still stop a run
    if team_org is not None and not isinstance(run_org, str):
        print(
            f"run '{args.run_id}' has no recorded organization, so it is not this team's "
            f"('{team_org}'); nothing was changed",
            file=sys.stderr,
        )
        return REFUSED
    if isinstance(run_org, str) and team_org is not None and run_org != team_org:
        print(
            f"run '{args.run_id}' belongs to organization '{run_org}', not this team's "
            f"'{team_org}'; nothing was changed",
            file=sys.stderr,
        )
        return REFUSED
    org_id = run_org if isinstance(run_org, str) else team_org
    content = None
    if args.reason:
        from nuramesh.hosted import HostedConfigError

        try:
            _hosted_env()  # ND195 b: a hosted deployment keeps a reason at most 90 days
        except HostedConfigError as exc:
            print(exc, file=sys.stderr)
            return USAGE
        try:
            from nuramesh.engine.content import configured_content_service

            if org_id is None:
                raise ContentError("the run's organization is unknown")
            content = configured_content_service(store, team_settings)
        except ContentError as exc:
            print(
                f"a reason is stored in the content store, which isn't available: {exc}",
                file=sys.stderr,
            )
            return USAGE
    try:
        outcome = request_stop(
            store,
            args.run_id,
            actor=args.human,
            via="cli",
            reason=args.reason,
            content=content,
            org_id=org_id,
        )
    except NotFoundError:
        print(f"no run '{args.run_id}' in this store", file=sys.stderr)
        return USAGE
    except RunFinished:
        print(f"run '{args.run_id}' has already ended; nothing to stop", file=sys.stderr)
        return REFUSED
    request = outcome.request
    if wants_json(args):  # the route's body (POST /v1/runs/{run_id}/stop)
        print(json.dumps(_stop_body(args.run_id, outcome), indent=2, default=str))
        return OK
    if outcome.already_requested:
        print(f"stop was already requested by {request.by} (ledger entry {request.seq}); no change")
    else:
        print(
            f"stop requested for run {args.run_id} (ledger entry {request.seq}); the run ends "
            "at its next step"
        )
    if request.withheld_text is not None:
        print(request.withheld_text)
    return OK


# -- the kill switch, role pause and circuit breakers (E19, spec/e19/ section 10) ---------------
# The CLI only restricts. It has no login, so `--human` is the caller's own claim (as for `stop`);
# that is acceptable for actions that only stop work. Lifting (resume, reset, disable, enable)
# needs a real proof and is possible only through the signed-in API and the app.

HUMAN_CLAIM = "--human needs an opaque id like human:u_7k2x9q (no email or name)"


def _org_context(args: argparse.Namespace, *, restricting: bool) -> tuple[Any, Store] | int:
    """The team's configuration and its store, or the exit code to return. A restricting command
    checks `--human` first, before the store is opened or created."""
    if restricting and not is_human_id(args.human or ""):
        print(HUMAN_CLAIM, file=sys.stderr)
        return USAGE
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    try:
        store = _open_server_store(root, args.store)
    except (ImportError, StoreError) as exc:
        print(_store_problem(exc), file=sys.stderr)
        return USAGE
    return config, store


def _stamp(state: Any) -> str:
    cause = f"cause {state.cause}" + (f", rule {state.rule}" if state.rule else "")
    return f"by {state.by} at {state.at.isoformat()} ({cause}; ledger entry {state.seq})"


def _org_lines(org_id: str, events: Sequence[Any], roles: Sequence[str] | None) -> list[str]:
    from nuramesh.core.breakers import halt_state, role_states

    halt = halt_state(events)
    lines = [
        f"organization {org_id}: HALTED {_stamp(halt)}"
        if halt.halted
        else f"organization {org_id}: running"
    ]
    states = role_states(events)
    for role in sorted(states):
        if states[role].paused:
            lines.append(f"role {role}: PAUSED {_stamp(states[role])}")
    return lines


def cmd_org_status(args: argparse.Namespace) -> int:
    """Whether the organization is halted and which roles are paused (read-only)."""
    from nuramesh.engine.org_halt import read_org_events
    from nuramesh.server import org_views

    got = _org_context(args, restricting=False)
    if isinstance(got, int):
        return got
    config, store = got
    events = read_org_events(store, config.org.id)
    if args.json or args.output == "json":
        view = org_views.halt_view(store, config.org.id, [r.id for r in config.org.roles], False)
        if args.output == "json":  # GET /v1/org/status answers these keys (BC4)
            from nuramesh.engine.solo_status import solo_status_view

            # an approval row carries no organization: count the rows of this organization's
            # runs only (the run's first entry names it), as the route does
            solo = solo_status_view(
                store,
                config.org.id,
                config.policies.solo_limit,
                lambda run: store.run_owner(f"run:{run}") == config.org.id,
                store.now(),
            )
            view = {**view, "warnings": [], "solo_approval": solo}
        print(json.dumps(view, indent=2))
    else:
        print("\n".join(_org_lines(config.org.id, events, None)))
    return OK


def cmd_roles_pause(args: argparse.Namespace) -> int:
    """Pause one role: its tasks and model calls stop; other roles keep working."""
    from nuramesh.engine.killswitch import NoSuchRole, pause_role

    got = _org_context(args, restricting=True)
    if isinstance(got, int):
        return got
    config, store = got
    try:
        outcome = pause_role(store, config.org, args.role, actor=args.human, via="cli")
    except NoSuchRole:
        print(f"no role '{args.role}' in this team's config", file=sys.stderr)
        return USAGE
    if outcome.already_paused:
        print(
            f"role {args.role} is already paused by {outcome.state.by} "
            f"(ledger entry {outcome.state.seq}); no change"
        )
    else:
        print(
            f"role {args.role} paused by {args.human} (ledger entry {outcome.state.seq}); "
            f"stop requested for {len(outcome.stopped_tasks)} running task(s)"
        )
    return OK


def _breaker_rows(statuses: Sequence[dict[str, Any]]) -> list[list[str]]:
    rows = [["NAME", "SIGNAL", "WINDOW", "LIMIT", "OBSERVED", "STATE", "MODE"]]
    for b in statuses:
        removed = " (removed from config)" if not b["in_config"] else ""
        state = {"ok": "ok", "tripped": "TRIPPED", "disabled": "DISABLED"}[b["state"]] + removed
        if b["signal"] is None:
            mode = "-"
        elif b["dry_run"]:
            mode = "dry-run"
        elif b["action"] == "pause_role":
            mode = f"pause {b['role']}"
        else:
            mode = "halt"
        rows.append(
            [
                b["name"],
                b["signal"] or "-",
                "-" if b["window_seconds"] is None else f"{b['window_seconds']}s",
                b["limit"] or "-",
                b["observed"] or "-",
                state,
                mode,
            ]
        )
    return rows


def _render_table(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]


def cmd_breakers_list(args: argparse.Namespace) -> int:
    """Every circuit breaker with its numbers now (read-only; nothing is written)."""
    from nuramesh.engine.org_halt import read_org_events
    from nuramesh.server import org_views

    got = _org_context(args, restricting=False)
    if isinstance(got, int):
        return got
    config, store = got
    role_ids = [r.id for r in config.org.roles]
    rules = tuple(config.breakers.rules)
    view = org_views.breakers_view(store, config.org.id, role_ids, rules, {}, False, store.now())
    if args.json or args.output == "json":
        if not args.output:  # `--json` keeps today's output; the route also answers `evaluation`
            del view["evaluation"]
        print(json.dumps(view, indent=2))
        return OK
    events = read_org_events(store, config.org.id)
    print(_org_lines(config.org.id, events, None)[0])
    if not view["breakers"]:
        print("no breakers are configured")
        return OK
    print("\n".join(_render_table(_breaker_rows(view["breakers"]))))
    return OK


def cmd_breakers_trip(args: argparse.Namespace) -> int:
    """Trip one breaker by hand (a drill, or to latch a rule): halts the org or pauses the rule's
    role like an automatic trip, unless the rule is a dry run."""
    from nuramesh.engine.killswitch import BreakerDisabled, NoSuchBreaker, trip_breaker

    got = _org_context(args, restricting=True)
    if isinstance(got, int):
        return got
    config, store = got
    try:
        outcome = trip_breaker(
            store,
            config.org,
            list(config.breakers.rules),
            args.name,
            actor=args.human,
            via="cli",
        )
    except NoSuchBreaker:
        print(f"no breaker '{args.name}' in this team's config", file=sys.stderr)
        return USAGE
    except BreakerDisabled:
        print(
            f"breaker {args.name} is disabled; a person must enable it in the app first",
            file=sys.stderr,
        )
        return REFUSED
    if outcome.already_tripped:
        print(f"breaker {args.name} is already tripped; no change")
        return OK
    tail = "; organization halted" if outcome.halted else ""
    if outcome.paused_role:
        tail = f"; role {outcome.paused_role} paused"
    print(f"breaker {args.name} tripped by {args.human} (ledger entry {outcome.seq}){tail}")
    return OK


def export_record(event: LedgerEvent) -> dict[str, Any]:
    """An event as one line of a ledger export: the stored values exactly as hashed (`at` in its
    canonical form, decimals as text, nulls written out), for `verifier/nuramesh_verify.py`
    (ND94, `spec/verify/README.md`)."""
    return {
        "stream": event.stream,
        "seq": event.seq,
        "type": event.type,
        "actor": event.actor,
        "at": format_time(event.at),
        "run_id": event.run_id,
        "task_id": event.task_id,
        "payload": thaw(event.payload),
        "cost": None if event.cost is None else event.cost.to_json(),
        "cost_basis": None if event.cost_basis is None else str(event.cost_basis),
        "prev_hash": event.prev_hash,
        "hash": event.hash,
    }


def cmd_export_ledger(args: argparse.Namespace) -> int:
    """Write every stream's events to a JSON Lines file for an offline check. Never overwrites."""
    store = _open_cli_store(Path(args.team).resolve(), args.store, create=False)
    if store is None:
        return USAGE
    out = Path(args.out)
    lines: list[str] = []
    for stream in store.streams():
        events, _ = store.snapshot(stream)
        lines += [
            json.dumps(export_record(e), ensure_ascii=True, separators=(",", ":")) + "\n"
            for e in events
        ]
    if not lines:
        print("nothing to export: the ledger has no events", file=sys.stderr)
        return USAGE
    try:
        with out.open("x", encoding="utf-8", newline="\n") as handle:
            handle.writelines(lines)
    except FileExistsError:
        print(f"{out} already exists; refusing to overwrite it", file=sys.stderr)
        return USAGE
    except OSError as exc:
        print(f"couldn't write {out}: {exc.strerror}", file=sys.stderr)
        return USAGE
    print(f"exported {len(lines)} events to {out}")
    print(f"check offline with: python3 verifier/nuramesh_verify.py {out}")
    return OK


def cmd_verify_ledger(args: argparse.Namespace) -> int:
    """Check every stream's chain. Without `--expected-head` this finds accidental damage and
    edits inside the chain, not deletion of the newest events or of a whole stream: a chain can
    only be proved against a head kept somewhere else (`--expected-head stream=<hash>`)."""
    store = _open_cli_store(Path(args.team).resolve(), args.store, create=False)
    if store is None:
        return USAGE
    expected: dict[str, str] = {}
    for item in args.expected_head or []:
        stream, _, digest = item.partition("=")
        if not stream or len(digest) != 64:
            print("--expected-head needs stream=<64-hex head hash>", file=sys.stderr)
            return USAGE
        expected[stream] = digest
    broken = 0
    streams = sorted({*store.streams(), *expected})
    if not streams and not args.allow_empty:  # an empty ledger proves nothing: fail closed
        print(
            "error: the ledger has no streams, so nothing was verified "
            "(wrong database? add --allow-empty if an empty ledger is expected)",
            file=sys.stderr,
        )
        return NOT_DONE
    for stream in streams:
        check = store.verify_stream(stream, expected_head=expected.get(stream))
        head = store.head(stream)
        state = "ok" if check.ok else f"BROKEN at seq {check.at_seq}: {check.problem}"
        print(f"{stream}: {check.checked} events, head {head.hash[:16] if head else '-'}… {state}")
        broken += 0 if check.ok else 1
    legacy, no_org = _scan_plain_hashes(store, streams)
    strict = bool(getattr(args, "require_keyed_hashes", False))
    hashes_bad = strict and bool(legacy or no_org)
    if args.anchors:
        anchor_bad = _check_anchors(store, args)
        if anchor_bad < 0:  # couldn't check the anchors at all
            print(f"{len(streams)} streams, {broken} broken; anchors NOT checked")
            _print_hash_report(legacy, no_org, strict)  # still printed before this return
            if broken or hashes_bad:
                return NOT_DONE  # a broken chain is still reported as such
            return USAGE
        broken += anchor_bad
    print(f"{len(streams)} streams, {broken} broken")
    _print_hash_report(legacy, no_org, strict)
    return OK if broken == 0 and not hashes_bad else NOT_DONE


def _scan_plain_hashes(
    store: Store, streams: list[str]
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """PH-KEY R9 (read-only, no content): the (stream, seq) of every legacy row (a plain content
    hash, no `hash_scheme`) and of every keyed row without `hash_org`, sorted."""
    from nuramesh.core.content import classify_hash_row

    legacy: list[tuple[str, int]] = []
    no_org: list[tuple[str, int]] = []
    for stream in streams:
        for event in store.read_stream(stream):
            found = classify_hash_row(event.type, event.payload)
            if found == "legacy":
                legacy.append((stream, event.seq))
            elif found == "no_org":
                no_org.append((stream, event.seq))
    return sorted(legacy), sorted(no_org)


def _print_hash_report(
    legacy: list[tuple[str, int]], no_org: list[tuple[str, int]], strict: bool
) -> None:
    """The R9 lines: keyed rows without `hash_org`, then (with the flag) the first 20 legacy
    rows, then as the LAST line the legacy count."""
    print(f"keyed rows without hash_org: {len(no_org)}")
    for stream, seq in no_org[:20]:
        print(f"  {stream} seq {seq}")
    if strict:
        for stream, seq in legacy[:20]:
            print(f"  legacy {stream} seq {seq}")
    streams_with = len({s for s, _ in legacy})
    print(f"legacy plain content hashes: {len(legacy)} rows in {streams_with} streams")


def _lost_key_events(store: Store) -> bool:
    """True if any organization stream holds a `anchor.key_rotated` event with `chained` false."""
    for stream in store.streams():
        if not stream.startswith("org:"):
            continue
        for event in store.read_stream(stream):
            if event.type == "anchor.key_rotated" and event.payload.get("chained") is False:
                return True
    return False


def _check_anchors(store: Store, args: argparse.Namespace) -> int:
    from nuramesh.engine.anchors import AnchorError, load_public_key, verify_anchor_repository

    if not args.pubkey:
        print("--anchors needs --pubkey (the public key that signed them)", file=sys.stderr)
        return -1
    tail = getattr(args, "tail_valid_until", None)
    if tail is None and (len(args.pubkey) > 1 or _lost_key_events(store)):
        print(
            "anchor check failed: extra trusted keys (or a lost-key rotation in the ledger) "
            "need --tail-valid-until <commit>",
            file=sys.stderr,
        )
        return -1  # the caller turns -1 into USAGE
    try:
        verification = verify_anchor_repository(
            store,
            Path(args.anchors),
            [load_public_key(Path(k)) for k in args.pubkey],
            ref=args.anchor_ref,
            tail_valid_until=tail,
        )
    except (AnchorError, OSError) as exc:
        print(f"anchor check failed: {exc}", file=sys.stderr)
        return -1
    return _print_stream_reports(verification, bool(args.require_anchors))


def _print_stream_reports(verification: "AnchorVerification", require_anchors: bool) -> int:
    """Notes, repository problems, then one line per stream; how many things are bad."""
    bad = 0
    for note in verification.notes:
        print(note)
    for problem in verification.repository_problems:
        bad += 1
        print(f"REPOSITORY {problem.code}: {problem.detail}")
    for report in verification.streams:
        if report.problems:
            bad += 1
            for problem in report.problems:
                print(f"{report.stream}: ANCHOR PROBLEM {problem.code}: {problem.detail}")
        elif not report.anchored:
            bad += 1 if require_anchors else 0
            print(f"{report.stream}: no anchors ({report.unanchored_events} events unanchored)")
        else:
            newer = (
                f", {report.unanchored_events} newer than the last anchor"
                if (report.unanchored_events)
                else ""
            )
            print(f"{report.stream}: {report.anchors} anchors verified{newer}")
    return bad


def _push_key() -> Path:
    """The SSH deploy key for pushing anchors: a path from the environment, never a flag."""
    from nuramesh.engine.anchors import SSH_KEY_ENV, AnchorError

    configured = os.environ.get(SSH_KEY_ENV)
    if not configured:
        raise AnchorError(f"pushing anchors needs {SSH_KEY_ENV} (the path of the deploy key)")
    return Path(configured).expanduser()


def _anchor_preflight(repo: str, key_path: str, push: bool = False, remote: str = "origin") -> None:
    """Everything about an export that can be checked without exporting; raises `AnchorError`."""
    from nuramesh.engine.anchors import (
        AnchorError,
        load_private_key,
        preflight_push,
        preflight_repository,
    )

    root = Path(repo).resolve()
    key = Path(key_path).resolve()
    for parent in key.parents:  # samefile: robust on case-insensitive filesystems
        if parent.exists() and root.exists() and parent.samefile(root):
            raise AnchorError("the private key must not live inside the anchor repository")
    load_private_key(Path(key_path))
    if not push:
        preflight_repository(Path(repo))
        return
    preflight_push(root, _push_key(), remote)  # a clone on main is fine: the branch is created


def _anchor_export(
    store: Store | Callable[[], Store | None],
    repo: str,
    key_path: str,
    streams: list[str] | None,
    push: bool = False,
    remote: str = "origin",
) -> int:
    """`store` may be a function that opens it: it is called only after the preflight checks, so
    a refused export (a bad key or repository) is reported before a missing store."""
    from nuramesh.engine.anchors import (
        AnchorError,
        ensure_anchor_branch,
        export_heads,
        load_private_key,
        push_anchors,
    )

    try:
        _anchor_preflight(repo, key_path, push, remote)
        created = (
            ensure_anchor_branch(Path(repo), remote=remote, ssh_key=_push_key()) if push else False
        )
        opened = store if isinstance(store, Store) else store()
        if opened is None:
            return USAGE
        report = export_heads(opened, Path(repo), load_private_key(Path(key_path)), streams=streams)
    except (AnchorError, OSError) as exc:
        print(f"anchoring failed: {exc}", file=sys.stderr)
        return USAGE
    print(
        f"anchored {len(report.exported)} stream(s), {len(report.unchanged)} unchanged"
        + (f", commit {report.commit[:12]}" if report.commit else "")
        + (", created branch ledger-anchors" if created else "")
    )
    if push:
        try:
            pushed = push_anchors(Path(repo), _push_key(), remote=remote)
        except (AnchorError, OSError) as exc:
            print(f"anchors are committed locally but NOT pushed: {exc}", file=sys.stderr)
            return USAGE
        state = (
            "created on the remote"
            if pushed.created_on_remote
            else ("already up to date" if pushed.up_to_date else "updated")
        )
        print(f"pushed {pushed.branch} to {pushed.remote} ({state})")
    return OK


def _anchor_rotate(team: Path, args: argparse.Namespace) -> int:
    """`anchor rotate`: replace the signing key (R14). Keys are never printed."""
    from nuramesh.core.anchors import anchor_time
    from nuramesh.engine.anchors import (
        AnchorError,
        RotationIncomplete,
        load_private_key,
        rotate_key,
    )

    if bool(args.key) == bool(args.lost_old_key):
        print("give --key (the old private key) or --lost-old-key <id>, not both", file=sys.stderr)
        return USAGE
    since = args.compromised_since
    if since is not None:
        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("no zone")  # never read as local time (default deny)
            since = anchor_time(parsed)
        except ValueError:
            print(
                "--compromised-since needs a time with an explicit zone, such as "
                "2026-11-05T12:00:00Z or 2026-11-05T06:00:00-06:00",
                file=sys.stderr,
            )
            return USAGE
    try:
        config = load_team_config(team)
        new_key = load_private_key(Path(args.new_key))
        old_key = load_private_key(Path(args.key)) if args.key else None
        for path in (args.key, args.new_key):
            if path and Path(args.repo).resolve() in Path(path).resolve().parents:
                raise AnchorError("a private key must not live inside the anchor repository")
        push_key = _push_key() if args.push else None
        store = _open_cli_store(team, args.store, create=False)
        if store is None:
            return USAGE
        report = rotate_key(
            store,
            Path(args.repo),
            new_key,
            org_stream=f"org:{config.org.id}",
            outgoing=old_key,
            lost_old_key=args.lost_old_key,
            compromised_since=since,
            push_key=push_key,
            remote=args.remote,
        )
    except RotationIncomplete as exc:  # F5: committed, a step is missing; a rerun finishes it
        print(f"rotation incomplete: {exc}", file=sys.stderr)
        return UNAVAILABLE
    except (AnchorError, OSError, TeamError, ConfigError, ConfigValidationError) as exc:
        print(f"rotation failed: {exc}", file=sys.stderr)
        return USAGE
    if report.chained:
        print(
            f"key {report.prev_key_id} -> {report.new_key_id}: record {report.n}"
            + (" written" if report.record_written else " already present")
            + (f", commit {report.commit[:12]}" if report.commit else "")
            + (", pushed" if report.pushed else "")
            + ("; ledger event recorded" if report.event_written else "; ledger event present")
        )
    else:
        print(
            f"lost key {report.prev_key_id} -> {report.new_key_id}: ledger event "
            + ("recorded" if report.event_written else "already present")
            + f" (verify with both public keys and --tail-valid-until {report.commit})"
        )
    return OK


def _anchor_verify(args: argparse.Namespace) -> int:
    """`anchor verify TEAM --repo R --pubkey F [--remote NAME|SSH-ADDRESS]` (R1.30 PR 5, H-13).

    With `--remote` the anchors are read from a clone this command owns, advanced fast-forward
    only; a rewritten remote is reported as tampering and exits 1. Without it this is
    `verify-ledger --anchors`. Couldn't check never reads as "clean": an unreachable remote exits
    5, a bad key or other usage problem exits 2."""
    from nuramesh.engine import anchor_remote
    from nuramesh.engine.anchors import (
        AnchorError,
        load_public_key,
        verify_anchor_repository,
    )

    team = Path(args.team).resolve()
    try:
        tail = args.tail_valid_until
        if tail is not None and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tail):
            raise AnchorError("--tail-valid-until needs a full commit id (40 or 64 hex characters)")
        if len(args.pubkey) > 1 and tail is None:
            raise AnchorError("several --pubkey values need --tail-valid-until <commit>")
        if not args.remote:
            for flag, value in (("--expect-tip", args.expect_tip), ("--clone-dir", args.clone_dir)):
                if value:
                    raise AnchorError(f"{flag} only works with --remote")
        else:
            anchor_remote.remote_address(args.remote, Path(args.repo))
            if args.clone_dir:
                clone = Path(args.clone_dir).resolve()
                if clone == team or team in clone.parents:
                    raise AnchorError("the clone directory must not be inside the team folder")
        publics = [load_public_key(Path(k)) for k in args.pubkey]
        store = _open_cli_store(team, args.store, create=False)
        if store is None:
            return USAGE
        if args.remote:
            result = anchor_remote.verify_remote(
                store,
                args.remote,
                publics,
                repo=Path(args.repo),
                clone_dir=Path(args.clone_dir) if args.clone_dir else None,
                ref=args.anchor_ref,
                expect_tip=args.expect_tip,
                tail_valid_until=tail,
                ssh_key=_push_key() if not args.remote.startswith("/") else None,
            )
        else:
            checked = verify_anchor_repository(
                store, Path(args.repo), publics, ref=args.anchor_ref, tail_valid_until=tail
            )
            result = anchor_remote.RemoteVerification(
                streams=checked.streams,
                repository_problems=checked.repository_problems,
                chain=checked.chain,
                extra_keys=checked.extra_keys,
                notes=checked.notes,
            )
    except anchor_remote.RemoteUnavailable as exc:
        print(f"anchor verify failed: {exc}", file=sys.stderr)
        return UNAVAILABLE
    except (AnchorError, OSError) as exc:
        print(f"anchor verify failed: {exc}", file=sys.stderr)
        return USAGE
    bad_streams = sum(1 for r in result.streams if not r.anchored and args.require_anchors)
    ok = result.ok and bad_streams == 0
    if args.json:
        print(json.dumps({**result.to_json(), "ok": ok, "tip": result.tip}, sort_keys=True))
    else:
        _print_stream_reports(result, bool(args.require_anchors))
    return OK if ok else NOT_DONE


def cmd_anchor(args: argparse.Namespace) -> int:
    """`anchor keygen` makes a key pair; `anchor export` signs and commits the current heads."""
    if args.anchor_command == "keygen":
        from nuramesh.engine.anchors import AnchorError, generate_key

        try:
            kid = generate_key(Path(args.private), Path(args.public))
        except (AnchorError, OSError) as exc:
            print(f"keygen failed: {exc}", file=sys.stderr)
            return USAGE
        print(
            f"key {kid}: private key at {args.private} (keep it OUTSIDE Git and the "
            f"database), public key at {args.public} (give this one to verifiers)"
        )
        return OK
    if args.anchor_command == "verify":
        return _anchor_verify(args)
    team = Path(args.team).resolve()
    if args.anchor_command == "rotate":
        return _anchor_rotate(team, args)
    return _anchor_export(
        lambda: _open_cli_store(team, args.store, create=False),
        args.repo,
        args.key,
        args.stream or None,
        push=args.push,
        remote=args.remote,
    )


TEMPLATES = {
    "dev-team": "dev_team",
    "scrum": "scrum",
    "kanban": "kanban",
    "review-board": "review_board",
    "support": "support",
    "sales": "sales",
    "qa": "qa",
    "devops": "devops",
    "code-review": "code_review",
}


def org_id_for_folder(target: Path) -> str:
    """An organization id derived from a folder name (key tombstone G19): lower case letters,
    digits and hyphens, never `demo`, so two inits in different folders get different ids."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", target.resolve().name.lower()).strip("-")[:56].strip("-")
    slug = slug or "team"
    return f"{slug}-team" if slug == "demo" else slug


def _set_template_org_id(target: Path, org_id: str) -> None:
    """Replace the `id:` line directly under `org:` in the copied nuramesh.yaml."""
    import re

    path = target / "nuramesh.yaml"
    if not path.is_file():
        return
    text = path.read_text("utf-8")
    new = re.sub(r"(?m)^(org:\n  id: )\S+$", lambda m: m.group(1) + org_id, text, count=1)
    path.write_text(new, "utf-8")


def cmd_init(args: argparse.Namespace) -> int:
    """Create a team folder from a packaged template (never overwrites a file)."""
    import importlib.resources
    import shutil

    source = importlib.resources.files("nuramesh") / "templates" / TEMPLATES[args.template]
    target = Path(args.dir)
    try:
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            print(
                f"{target} exists and isn't an empty folder; refusing to write into it",
                file=sys.stderr,
            )
            return USAGE
        if target.exists() and any(target.iterdir()):
            print(f"{target} isn't empty; refusing to write into it", file=sys.stderr)
            return USAGE
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".nuramesh", ".env*", ".git")
        with importlib.resources.as_file(source) as folder:
            shutil.copytree(folder, target, dirs_exist_ok=target.exists(), ignore=ignore)
        _set_template_org_id(target, org_id_for_folder(target))
    except OSError as exc:
        print(f"couldn't create {target}: {type(exc).__name__}", file=sys.stderr)
        return USAGE
    print(f"created {target} from the '{args.template}' template. Next:")
    import shlex

    quoted = shlex.quote(str(target))
    print(f"  nuramesh validate {shlex.quote(str(target) + '/nuramesh.yaml')}")
    print(f"  nuramesh try {quoted} --fake --approve-as human:you --run-id first-run")
    print("    (no Podman/Docker/Colima: add --allow-unsandboxed, see the sandbox guide)")
    print(f"  nuramesh serve {quoted}")
    return OK


def cmd_eval(args: argparse.Namespace) -> int:
    """`eval run` answers a suite with an agent's model and records the result; `eval status`
    shows whether an agent is eligible for work under its role's requirements."""
    from nuramesh.core.budgets import BudgetExceededError, CallContext
    from nuramesh.core.evals import SubjectKind, verified
    from nuramesh.core.models import Message, ModelRequest, parse_model_ref
    from nuramesh.engine.evals import agent_eligibility, load_eval_records, run_eval
    from nuramesh.engine.governance import ModelBlocked
    from nuramesh.engine.models import PolicyDenied, call_model
    from nuramesh.engine.policy import ModelPolicy

    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    if config.evals is None:
        print("the configuration has no evals", file=sys.stderr)
        return USAGE
    try:
        config.org.agent(args.agent)
    except KeyError:
        print(f"unknown agent '{args.agent}'", file=sys.stderr)
        return USAGE
    if args.eval_command == "status":
        status_store = _open_cli_store(root, args.store, create=False, missing_ok=True)
        if status_store is None:
            return USAGE
        store = status_store
        result = agent_eligibility(
            store, config.evals, config.org, agent_id=args.agent, presets=config.presets
        )
        suites = {x.id: x for x in config.evals.suites}
        role = config.org.role_of(args.agent).id
        shown = [
            verified(r, suites[r.suite_id]) if r.suite_id in suites else r
            for r in load_eval_records(store, config.org.id)
            if r.subject_id in (args.agent, role)
        ]
        print(f"{args.agent}: {'eligible' if result.eligible else 'NOT eligible'}")
        for reason in result.reasons:
            print(f"  {reason}")
        for r in shown[-5:]:
            print(
                f"  result seq {r.seq} ({r.subject_kind.value} level): suite {r.suite_id} "
                f"score {r.score} {'passed' if r.passed else 'failed'} ({r.at:%Y-%m-%d})"
            )
        return OK if result.eligible else NOT_DONE
    if not args.fake:
        print(
            "eval run is fake-only: no real provider is supported for evals in this version; "
            "use --fake",
            file=sys.stderr,
        )
        return USAGE
    suite = config.evals.suite(args.suite)
    if suite is None:
        print(f"unknown suite '{args.suite}'", file=sys.stderr)
        return USAGE
    if config.org.role_of(args.agent).id != suite.role:
        print(f"suite '{suite.id}' is for role '{suite.role}', not {args.agent}'s", file=sys.stderr)
        return USAGE
    model_ref = config.org.model_for(args.agent)
    if model_ref is None:
        print(f"agent '{args.agent}' has no model", file=sys.stderr)
        return USAGE
    problem = _fake_only_problem(config, [args.agent])
    if problem:
        print(problem, file=sys.stderr)
        return USAGE
    run_id = "eval-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    opened = _open_cli_store(root, args.store, create=True)
    if opened is None:
        return USAGE
    store = opened
    denied = _deny_checks(
        store, config, command="eval", run_id=run_id
    )  # reads only: before any write
    if denied is not None:
        store.close()
        return denied
    cli_models.seed_and_warn(store, config)
    effective = _fake_only_problem(
        config,
        [args.agent],
        cli_models.effective_agent_refs(cli_models.context_for(root, config, store)),
    )
    if effective:
        print(effective, file=sys.stderr)
        store.close()
        return USAGE
    governance = cli_models.governance_for(cli_models.context_for(root, config, store))
    try:
        module = load_team_module(root)
        env = TeamEnv(
            root=root, config=config, store=store, run_id=run_id, fake=True, sandbox=lambda: None
        )
        team = setup_team(module, env)
    except TeamError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    fake = team.fake_provider
    if not isinstance(fake, FakeModel):
        print("--fake needs the team's fake_provider to be a FakeModel", file=sys.stderr)
        return USAGE
    for budget in (*config.budgets, *team.run_budgets):
        _define(store, budget, config.org.id)
    provider, _endpoint_id, model = parse_model_ref(model_ref)
    from nuramesh.engine.content import default_ledger_hashing

    hashing = default_ledger_hashing(store)
    content = None  # ND118: the configuration is kept in the content store where one exists
    try:
        from nuramesh.engine.content import configured_content_service

        service = configured_content_service(store, config.content)
        if service.retired(config.org.id) is None:  # an erased organization keeps hashes only
            content = service
    except ContentError:
        pass
    policy = ModelPolicy(
        store,
        config.effective_policies,
        config.org,
        hasher=hashing.bind(None, config.org.id),
        content=content,
        evals=config.evals,
    )
    try:
        overrides = _keyed_overrides(config, store)
    except (KeyDestroyedError, OrphanedContentError) as exc:
        print(_destroyed_message(exc), file=sys.stderr)
        return REFUSED
    counter = {"n": 0, "done": 0}

    def refused() -> str:
        """The refusal prefix: honest about model calls that already wrote their rows."""
        if counter["done"] == 0:
            return (
                "the eval was refused before any model call finished; ledger rows written "
                "before the refusal (policy or budget decisions) stay recorded"
            )
        return (
            f"the eval was stopped after {counter['done']} model call(s), which stay recorded; "
            "no score was recorded"
        )

    def respond(case: Any) -> str:
        counter["n"] += 1
        outcome = call_model(
            store,
            fake,
            ModelRequest(
                provider=provider,
                model=model,
                messages=(Message(role="user", content=case.prompt),),
                max_output_tokens=400,
            ),
            context=CallContext(run_id=run_id, agent_id=args.agent, cap_exempt=True),
            org=config.org,
            prices=config.prices,
            reservation_id=f"{run_id}-{counter['n']}",
            actor=f"agent:{args.agent}",
            authorize=policy.authorize,
            authorize_spend=policy.authorize_spend,
            overrides=overrides,
            governance=governance,
            hasher=hashing.bind(None, config.org.id),
        )
        counter["done"] += 1
        hashing.flush_alerts()  # a failed ledger hash is alerted once the row is written (R6)
        return outcome.response.text

    try:
        record = run_eval(
            store,
            suite,
            org=config.org,
            kind=SubjectKind.AGENT,
            subject_id=args.agent,
            respond=respond,
            hasher=hashing.bind(None, config.org.id),
            actor="service:cli",
        )
    except ContentError as exc:  # a key lost, replaced or unreadable mid-suite: never a traceback
        print(_with_code(f"{refused()}: {exc}", exc), file=sys.stderr)
        return REFUSED
    except ModelBlocked as exc:
        print(f"{refused()}: model blocked [{exc.reason}]: {exc.detail}", file=sys.stderr)
        return REFUSED
    except (BudgetExceededError, PolicyDenied) as exc:  # ApprovalRequired is a PolicyDenied
        print(f"{refused()}: {type(exc).__name__}", file=sys.stderr)
        return REFUSED
    except OrgHalted:
        print("the organization is halted; the eval made no model call", file=sys.stderr)
        return STOPPED
    except RolePaused as exc:
        print(f"{exc}; the eval made no further model call", file=sys.stderr)
        return STOPPED
    finally:
        hashing.flush_alerts()  # the last alert of the suite (PH-KEY R6, K33)
    print(
        f"suite {suite.id} (v{suite.version}) for {args.agent}: score {record.score} "
        f"({'PASSED' if record.passed else 'FAILED'}; threshold {suite.pass_threshold})"
    )
    for outcome in record.outcomes:
        mark = "pass" if outcome.passed else "FAIL"
        crit = " (critical)" if outcome.critical else ""
        print(f"  [{mark}] {outcome.case_id}{crit}")
    return OK if record.passed else NOT_DONE


def port_is_free(host: str, port: int) -> bool:
    """Can this process bind host:port (`doctor`'s port check; binds, never connects)."""
    import socket

    try:
        with socket.socket() as probe:
            probe.bind((host, port))
    except OSError:
        return False
    return True


def _is_loopback(host: str) -> bool:
    """A literal loopback address, or a name that resolves only to loopback addresses."""
    import ipaddress
    import socket

    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        pass
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return (
        host == "localhost"
        and bool(found)
        and all(ipaddress.ip_address(a).is_loopback for a in found)
    )


def _store_problem(exc: Exception) -> str:
    _BUSY.set(isinstance(exc, StoreBusyError))  # the mark is the LAST store problem's class
    if isinstance(exc, ImportError):
        return "NURAMESH_DATABASE_URL needs: pip install 'nuramesh[postgres]'"
    return f"couldn't open the database: {exc}"  # never includes the URL


def _open_server_store(root: Path, override: str | None) -> "SqliteStore | PostgresStore":
    """The store `serve` and `auth` use: the team's SQLite file, or PostgreSQL when
    `NURAMESH_DATABASE_URL` is set (a secret: read from the environment only)."""
    url = database_url()
    if not url:
        return _store(root, override)
    from nuramesh.stores.postgres import PostgresStore

    return _track(PostgresStore(url, migrate=False))  # only `db migrate` migrates (issue 897)


_CONTAINER_CGROUP_WORDS = ("docker", "containerd", "kubepods", "lxc")


def _in_container(
    env: Mapping[str, str] | None = None, *, root: Path = Path("/"), pid: int | None = None
) -> bool:
    """Whether this process counts as running in a container (SA-0.5, #950). There a
    pseudo-terminal's output lands in the container log, so `serve` never prints the setup URL.

    True when ANY marker is present: `/.dockerenv`, `/run/.containerenv`, the environment variable
    `KUBERNETES_SERVICE_HOST` or `container` (set to anything, the empty string too), a
    `/proc/1/cgroup` that names docker, containerd, kubepods or lxc, or the process being PID 1.
    A marker that cannot be read is not "absent": any failure of the detection also answers True.
    A file that does not exist is absent (macOS has no `/proc`)."""
    env = os.environ if env is None else env
    try:
        if "KUBERNETES_SERVICE_HOST" in env or "container" in env:
            return True
        if (os.getpid() if pid is None else pid) == 1:
            return True
        for marker in (root / ".dockerenv", root / "run" / ".containerenv"):
            try:
                marker.lstat()
            except FileNotFoundError:
                continue
            return True
        try:
            cgroup = (root / "proc" / "1" / "cgroup").read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        return any(word in cgroup for word in _CONTAINER_CGROUP_WORDS)
    except Exception:  # noqa: BLE001 (fail closed: an unreadable marker counts as a container)
        return True


def _stdout_is_terminal() -> bool:
    """Whether standard output is a terminal; a stream that cannot say so is not one."""
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001 (a closed or missing stream is not a terminal)
        return False


def _bind_host(host: str) -> str:
    """The `--host` value for the socket: one pair of brackets around an IPv6 literal is
    stripped (`[::1]` binds `::1`, SA-0.7)."""
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def setup_url_base(
    *,
    auth: bool,
    host: str,
    port: int,
    trusted_proxy: bool,
    allowed_hosts: Sequence[str],
    hosted: bool,
) -> str | None:
    """`http://<host>:<port>` for the one-time setup URL, or None when `serve` must not print one
    (SA-0.5): only with authentication on, a loopback bind, no `--trusted-proxy`, no
    `--allowed-host`, not hosted, standard output a terminal and not in a container. The host is
    the one given to `--host`, an IPv6 literal in exactly one pair of brackets (SA-0.7). Whether
    an owner exists is checked later, by `_announce_setup`."""
    if not auth or trusted_proxy or allowed_hosts or hosted:
        return None
    bare = _bind_host(host)
    if not _is_loopback(bare):
        return None
    if not _stdout_is_terminal():
        return None
    try:
        if _in_container():
            return None
    except Exception:  # noqa: BLE001 (a failed detection counts as a container)
        return None
    shown = f"[{bare}]" if ":" in bare else bare
    return f"http://{shown}:{port}"


def _setup_hint(service: Any, team: str) -> None:
    """How to make the first owner without the setup URL (SA-0.15): standard error, no secret."""
    expires = service.open_setup_token_expiry()
    if expires is not None:
        print(
            f"A setup token for the first owner is open until {expires:%Y-%m-%d %H:%M} UTC; it "
            "cannot be shown again.",
            file=sys.stderr,
        )
    print(
        "No owner exists yet. Create the first owner's setup token in a shell of its own (it is "
        "shown there once, never in this log):\n"
        f"  nuramesh auth new-setup-token --team {team}\n"
        "In a container (Compose): docker compose exec -it <service> nuramesh-env nuramesh auth "
        f"new-setup-token --team {team}\n"
        "In a container (plain Docker): docker exec -it <container> nuramesh-env nuramesh auth "
        f"new-setup-token --team {team}\n"
        "Then open the app's /setup page and paste the token there, or send it to "
        "POST /v1/auth/setup/redeem.",
        file=sys.stderr,
    )


def _announce_setup(service: Any, team: str, *, auth: bool, url_base: str | None) -> None:
    """What `serve` says about the first owner, called once per process and only AFTER the port is
    bound (SA-0.5, SA-0.6, SA-0.9, #950).

    - An owner exists: nothing. A closed organization (T128): its one fixed line, nothing else.
    - Authentication off: nothing about setup.
    - No owner has ever existed and `url_base` is set (`setup_url_base`): every open setup token
      is retired and one new token (900 s) is stored, in one transaction, and exactly one line,
      `<url_base>/setup#token=<token>`, is written straight to standard output (the terminal):
      never through logging, never to standard error. The token is in the fragment, which a
      browser never sends.
    - Otherwise the hint of `_setup_hint` on standard error; no token is made or retired."""
    from nuramesh.core.auth import SERVE_SETUP_TOKEN_LIFETIME

    if service.owner_exists():
        return
    if service.org_closed():
        print("This organization was closed (T128); no owner can be set up.", file=sys.stderr)
        return
    if not auth:
        return
    if url_base is None:
        _setup_hint(service, team)
        return
    outcome = service.issue_setup_token(rotate=True, lifetime=SERVE_SETUP_TOKEN_LIFETIME)
    if outcome.issued is None:  # an owner appeared, or the organization closed, meanwhile
        return
    if outcome.retired:
        print(
            "An earlier setup link or setup token was retired; only the link printed now works.",
            file=sys.stderr,
        )
    sys.stdout.write(
        "No owner exists yet. Open this link in a browser on this machine to create the first "
        "owner (it works once and for 15 minutes; it is not shown again):\n"
        f"{url_base}/setup#token={outcome.issued.token}\n"
    )
    sys.stdout.flush()


def _announcing_server(config: Any, on_bound: Callable[[int], None]) -> Any:
    """A `uvicorn.Server` that calls `on_bound(port)` once, right after its port is bound and
    before it serves (SA-0.9). A bind that fails never reaches the call: uvicorn logs the error
    and exits, so nothing is retired and nothing is printed."""
    import uvicorn

    class AnnouncingServer(uvicorn.Server):
        async def startup(self, sockets: Any = None) -> None:
            await super().startup(sockets=sockets)
            if not self.started or self.should_exit:
                return
            port = int(config.port)
            for server in getattr(self, "servers", []):
                for sock in server.sockets or ():
                    port = int(sock.getsockname()[1])
                    break
                break
            on_bound(port)

    return AnnouncingServer(config)


def open_when_listening(
    host: str,
    port: int,
    url: str,
    *,
    opener: Callable[[str], Any] | None = None,
    wait_seconds: float = 15.0,
) -> threading.Thread:
    """`serve --open`: open `url` in the browser once the port accepts a connection, from a
    daemon thread (the server starts after this returns). Nothing listens within `wait_seconds`:
    nothing is opened. A failing browser launch never stops the server."""

    import contextlib
    import socket
    import time
    import webbrowser

    def run() -> None:
        # The port may accept connections a moment before the app answers (the socket is bound
        # before startup finishes); the browser then waits or reloads, which is harmless.
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            return
        with contextlib.suppress(Exception):  # best effort: the URL is printed as well
            (opener or webbrowser.open)(url)

    thread = threading.Thread(target=run, name="nuramesh-open-browser", daemon=True)
    thread.start()
    return thread


def server_config(app: Any, host: str, port: int, log_level: str = "info") -> Any:
    """How `serve` runs uvicorn. **`proxy_headers=False`**: uvicorn's default would believe
    `X-Forwarded-For` and `-Proto` from 127.0.0.1 and rewrite the client address and scheme
    before the app sees them, defeating the rate limit, lockout and the Secure-cookie decision.
    The app reads those headers itself, and only with `--trusted-proxy`."""
    import uvicorn

    return uvicorn.Config(
        app, host=host, port=port, log_level=log_level, proxy_headers=False, forwarded_allow_ips=""
    )


_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_NAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*$")


def _allowed_host_problem(value: str) -> str | None:
    """Why `value` can't be a `--allowed-host` (a DNS name or an IP literal, no port)."""
    import ipaddress

    if value.startswith("[") != value.endswith("]"):  # both brackets or neither
        return f"--allowed-host {value!r}: a host name or IP address, lower case, without a port"
    try:
        ipaddress.ip_address(value.strip("[]"))
        return None
    except ValueError:
        pass
    if not _HOST_NAME.fullmatch(value):
        return f"--allowed-host {value!r}: a host name or IP address, lower case, without a port"
    return None


def refresh_grace_from_env(env: Mapping[str, str]) -> timedelta:
    """`NURAMESH_REFRESH_GRACE_SECONDS`: whole seconds, 0 to 30 (default 30, T109). Anything else
    is an error that stops the server at start, never a silent fallback."""
    name = "NURAMESH_REFRESH_GRACE_SECONDS"
    raw = env.get(name)
    if raw is None:
        return timedelta(seconds=30)
    if not re.fullmatch(r"[0-9]{1,2}", raw) or int(raw) > 30:
        raise ValueError(f"{name} must be a whole number of seconds from 0 to 30")
    return timedelta(seconds=int(raw))


def decision_sweep_seconds_from_env(env: Mapping[str, str]) -> float:
    """`NURAMESH_DECISION_SWEEP_SECONDS`: a positive whole number of seconds between the reconcile
    sweeps of unmarked human decisions (default 300). Anything else stops the server at start."""
    name = "NURAMESH_DECISION_SWEEP_SECONDS"
    raw = env.get(name)
    if raw is None:
        return 300.0
    if not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) < 1:
        raise ValueError(f"{name} must be a positive whole number of seconds")
    return float(int(raw))


def refresh_successor_key_from_env(env: Mapping[str, str]) -> bytes | None:
    """`NURAMESH_REFRESH_SUCCESSOR_KEY`: the server secret (T109) the refresh grace window derives
    its replayed token pair under: at least 32 random bytes as base64url (padding optional). None
    when unset; anything else that is not a valid key is an error that stops the server."""
    import base64
    import binascii

    name = "NURAMESH_REFRESH_SUCCESSOR_KEY"
    raw = (read_secret(name, env) or "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{name} must be base64url text (at least 32 random bytes)") from None
    if len(key) < 32:
        raise ValueError(f"{name} must hold at least 32 random bytes")
    return key


def trusted_proxies_from_env(env: Mapping[str, str], *, flag: bool) -> Any:
    """`--trusted-proxy` plus `NURAMESH_TRUSTED_PROXIES` (CIDRs or addresses, comma-separated):
    forwarded headers are believed only from those socket peers. The flag alone refuses to start
    (it used to trust every peer, which let a direct client forge its address), and so does a list
    without the flag. Returns the parsed list (empty when the proxy is not trusted)."""
    from nuramesh.server.proxies import ENV, TrustedProxies

    raw = env.get(ENV)
    if not flag:
        if raw is not None:
            raise ValueError(f"{ENV} is set but --trusted-proxy is not: add the flag or unset it")
        return TrustedProxies()
    if raw is None:
        raise ValueError(
            f"--trusted-proxy needs {ENV}: the proxy addresses to trust, as CIDRs or addresses "
            "separated by commas (for example 10.0.0.0/8,192.0.2.7)"
        )
    return TrustedProxies.parse(raw)


def content_expiry_seconds_from_env(env: Mapping[str, str]) -> float:
    """`NURAMESH_CONTENT_EXPIRY_SECONDS`: how often the API server deletes expired content
    (default 3600; 0 turns it off). Anything else stops the server at start."""
    name = "NURAMESH_CONTENT_EXPIRY_SECONDS"
    raw = env.get(name)
    if raw is None:
        return 3600.0
    if not re.fullmatch(r"[0-9]{1,7}", raw):
        raise ValueError(f"{name} must be a whole number of seconds (0 turns expiry off)")
    return float(int(raw))


def key_sweep_seconds_from_env(env: Mapping[str, str]) -> float:
    """`NURAMESH_KEY_SWEEP_SECONDS` (R1.14c): how often the API server runs the stored-key sweeper
    (default 60; 0 turns the periodic run off). Anything else stops the server at start."""
    name = "NURAMESH_KEY_SWEEP_SECONDS"
    raw = env.get(name)
    if raw is None:
        return 60.0
    if not re.fullmatch(r"[0-9]{1,7}", raw):
        raise ValueError(f"{name} must be a whole number of seconds (0 turns the sweep off)")
    return float(int(raw))


def secret_store_from_env(
    env: Mapping[str, str], factories: Mapping[str, Any] | None = None
) -> Any:
    """The secret store `NURAMESH_SECRET_STORE` names (R1.14c, VK67; see
    `nuramesh.engine.secret_select`), or None when none is configured: stored model keys, the
    erasure hook and the sweeper are then off. A bad setting, a bad directory or a hosted
    deployment without a real store raises `SecretStoreError` (fixed reason, no path)."""
    from nuramesh.engine.secret_select import select_secret_store

    return select_secret_store(env, factories=factories)


def _key_sweep_lock(root: Path, override: str | None) -> Any:
    """One sweeper at a time per deployment: an advisory lock in PostgreSQL, else a flock file
    next to the SQLite database."""
    from nuramesh.engine.key_sweeper import FileSweepLock, PostgresSweepLock

    url = database_url()
    if url:
        return PostgresSweepLock(url)
    path = Path(override) if override else state_path(root, "runs.db")
    return FileSweepLock(path.with_name(path.name + ".keysweep.lock"))


def auth_settings_from_env(
    env: Mapping[str, str], *, trusted_proxy: Any, auth: bool, loopback: bool, hosted: bool = False
) -> Any:
    """The `AuthSettings` of `nuramesh serve`: the grace window and its server key from the
    environment. Both are always validated. With `--auth` and the window on (the default, 30 s) the
    key is required; without `--auth` no session is ever issued, so the window is off.

    **First run (#874):** on a loopback server with no trusted proxy (one process on one machine,
    nothing to share a key with) an unset window defaults to off, so sign-in needs no hand-made
    key. A window set on purpose, a proxy and a network address keep the key requirement."""
    from nuramesh.server.sessions import AuthSettings

    grace = refresh_grace_from_env(env)
    key = refresh_successor_key_from_env(env)
    if not auth:
        grace = timedelta(0)
    elif (
        loopback
        and not hosted
        and not trusted_proxy
        and key is None
        and "NURAMESH_REFRESH_GRACE_SECONDS" not in env
    ):
        grace = timedelta(0)
    elif grace > timedelta(0) and key is None:
        raise ValueError(
            "NURAMESH_REFRESH_SUCCESSOR_KEY is required while the refresh grace window is on "
            "(32 or more random bytes as base64url, the same on every replica; make one with "
            "`head -c 32 /dev/urandom | basenc --base64url` and export it); "
            "or set NURAMESH_REFRESH_GRACE_SECONDS=0 to turn the window off"
        )
    from nuramesh.server.mfa_keys import AuthKeyFileError, key_ring_from_env

    try:
        ring = key_ring_from_env(env)
    except AuthKeyFileError as exc:
        raise ValueError(str(exc)) from None
    issuer = env.get("NURAMESH_MFA_ISSUER", "").strip() or "NuraMesh"
    if len(issuer) > 64 or not issuer.isprintable():
        raise ValueError("NURAMESH_MFA_ISSUER must be printable text of at most 64 characters")
    raw_life = env.get("NURAMESH_MFA_CHALLENGE_SECONDS", "").strip()
    try:
        life = int(raw_life) if raw_life else 300
    except ValueError:
        raise ValueError("NURAMESH_MFA_CHALLENGE_SECONDS must be a whole number") from None
    if not 30 <= life <= 900:
        raise ValueError("NURAMESH_MFA_CHALLENGE_SECONDS must be between 30 and 900")
    return AuthSettings(
        trusted_proxy=trusted_proxy,
        refresh_grace=grace,
        refresh_successor_key=key,
        mfa_keys=ring,
        mfa_issuer=issuer,
        mfa_challenge_seconds=life,
    )


def _key_file_start_problem() -> str | None:
    """The server refuses to start with a key file configured that is not usable, or at all when
    hosted (ND156, ND214). Unset means no Anthropic: nothing to check. Fixed text, no path."""
    from nuramesh.engine.anthropic import KEY_FILE_ENV, credential_problem
    from nuramesh.engine.openai_compatible import startup_problem as binding_problem

    # the one start line for a binding file that is set but unusable (openai-compatible 4.1)
    bindings = binding_problem()
    if bindings is not None:
        return bindings
    if not os.environ.get(KEY_FILE_ENV, ""):
        return None
    return credential_problem()


def _auth_flag(value: str) -> bool:
    """The value of `serve --auth`: exactly `on` or `off` (case-sensitive, SA-0.1)."""
    if value == "on":
        return True
    if value == "off":
        return False
    raise argparse.ArgumentTypeError(f"--auth takes on or off, not {value!r}")


def normalize_serve_auth(argv: Sequence[str]) -> list[str]:
    """`serve --auth` as SA-0.1 reads it: the value is the NEXT argument only when that is exactly
    `on` or `off`; otherwise the flag is bare and means `on`, and the next argument stays what it
    was (`serve --auth t` is authentication on for the team folder `t`). Rewrites a separate
    `--auth [on|off]` of the `serve` command to the `--auth=<value>` form argparse cannot
    misread; every other command, `--auth=<anything>` and whatever follows `--` are untouched."""
    from nuramesh.cli_aliases import _split_globals

    given = list(argv)
    split = _split_globals(given)
    if split is None or split[1][:1] != ["serve"]:
        return given
    out: list[str] = list(split[0])
    i = len(split[0])
    while i < len(given):
        word = given[i]
        if word == "--":
            out.extend(given[i:])
            break
        if word == "--auth":
            nxt = given[i + 1] if i + 1 < len(given) else None
            if nxt in ("on", "off"):
                out.append(f"--auth={nxt}")
                i += 2
                continue
            out.append("--auth=on")
            i += 1
            continue
        out.append(word)
        i += 1
    return out


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the API for a team folder's store, and close the store when the server stops
    (or when start-up is refused after the store was opened)."""
    opened: list[Any] = []
    try:
        return _serve(args, opened)
    finally:
        for store in opened:
            getattr(store, "close", lambda: None)()


def _serve(args: argparse.Namespace, opened: list[Any]) -> int:
    """Serve the API for a team folder's store.

    **Authentication is on by default** (SA-0.1, #950): sessions and API tokens are enforced on
    every route (`docs/architecture-platform.md` §11), and a first run on a person's own terminal
    prints the one-time setup URL after the port is bound (`_announce_setup`). **`--auth off`**
    must be typed: loopback only, reads open, every human action refused, a banner in capitals;
    it is refused with `--trusted-proxy`, `--allowed-host`, a network address or a hosted
    deployment (SA-0.2). Only with authentication on may the server listen on a network address,
    and then it also needs `--trusted-proxy` (TLS is terminated by a proxy in front; the server
    then answers https requests only) and at least one `--allowed-host` (SA-0.4)."""
    try:
        import uvicorn  # noqa: F401 (the server extra must be there before anything is opened)

        from nuramesh.server.app import create_app
    except ImportError:
        print("the API needs the extra: pip install 'nuramesh[local]'", file=sys.stderr)
        return USAGE
    hosts = [h.lower() for h in args.allowed_host or []]
    for host in hosts:
        problem = _allowed_host_problem(host)
        if problem is not None:
            print(problem, file=sys.stderr)
            return USAGE
    network = not _is_loopback(args.host)
    if hosts and not args.auth:
        # a local proxy forwarding a public name to 127.0.0.1 would expose an open API
        print("--allowed-host needs authentication on: remove --auth off", file=sys.stderr)
        return USAGE
    if args.open and args.no_app:
        print("--open and --no-app cannot be combined", file=sys.stderr)
        return USAGE
    if network and not args.auth:
        print(
            "refusing to listen on a network address without authentication: remove --auth off "
            "(and add --trusted-proxy and --allowed-host), or use 127.0.0.1.",
            file=sys.stderr,
        )
        return USAGE
    if args.trusted_proxy and not args.auth:
        # SA-0.2: a proxy in front of a server that asks nobody who they are publishes an open API
        print("--trusted-proxy needs authentication on: remove --auth off", file=sys.stderr)
        return USAGE
    if not args.auth:
        # SA-0.2: a hosted server never runs open; a flag that cannot be read counts as hosted
        from nuramesh.hosted import HostedConfigError as _HostedFlagError

        try:
            hosted_and_open = _hosted_env()
        except _HostedFlagError as exc:
            print(
                f"--auth off is refused while the hosted flag cannot be read: {exc}",
                file=sys.stderr,
            )
            return USAGE
        if hosted_and_open:
            print(
                "--auth off is refused on a hosted deployment (NURAMESH_HOSTED=1): a hosted "
                "server never runs without authentication",
                file=sys.stderr,
            )
            return USAGE
    if network and not args.trusted_proxy:
        print(
            "a network address needs TLS in front: run behind a TLS proxy and add --trusted-proxy",
            file=sys.stderr,
        )
        return USAGE
    if network and not hosts:
        print(
            "a network address needs --allowed-host NAME (the host name browsers use)",
            file=sys.stderr,
        )
        return USAGE
    try:
        trusted_proxies = trusted_proxies_from_env(os.environ, flag=args.trusted_proxy)
    except ValueError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return USAGE
    plugin_context = getattr(args, "plugin_context", None)
    if plugin_context is None:
        try:
            plugin_context = load_plugins()
        except PluginError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return USAGE
        if plugin_context.licence.notice is not None:  # a refused licence file is loud, never fatal
            print(plugin_context.licence.notice, file=sys.stderr)
    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    from nuramesh.server.agent_credentials import AgentSettings, SettingsError

    try:  # E2 spec 14: a bad setting stops the server before anything is opened
        agent_settings = AgentSettings.from_env(os.environ)
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return USAGE
    try:
        store = _open_server_store(root, args.store)
    except (ImportError, StoreError) as exc:
        print(_store_problem(exc), file=sys.stderr)
        return _open_failed_code(exc)
    opened.append(store)
    key_problem = _key_file_start_problem()
    if key_problem:
        # a binding file problem is exactly its one fixed line (spec openai-compatible, K17)
        shown = (
            key_problem
            if key_problem.startswith("openai-compatible: ")
            else (f"refusing to start: {key_problem}")
        )
        print(shown, file=sys.stderr)
        return USAGE
    from nuramesh.core.auth import team_key
    from nuramesh.server.app import ALLOWED_HOSTS, AnchorCheck
    from nuramesh.server.sessions import AuthService

    # the catalog is seeded before anything reads it; the API server records the private-network
    # allowance itself (its start-up), so the seed does not
    cli_models.seed_and_warn(store, config, record_private=False)
    cli_models.print_price_warnings(config, store.now().date(), store)

    if bool(args.anchors) != bool(args.pubkey):
        print("--anchors and --pubkey go together", file=sys.stderr)
        return USAGE
    if args.pubkey and len(args.pubkey) > 1 and not args.tail_valid_until:
        print("a second --pubkey needs --tail-valid-until <commit>", file=sys.stderr)
        return USAGE
    if args.tail_valid_until and not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", args.tail_valid_until
    ):
        print(
            "--tail-valid-until needs a full commit id (40 or 64 hex characters)", file=sys.stderr
        )
        return USAGE
    anchor_check = (
        AnchorCheck(
            Path(args.anchors),
            Path(args.pubkey[0]),
            args.anchor_ref,
            tuple(Path(k) for k in args.pubkey[1:]),
            args.tail_valid_until,
        )
        if args.anchors
        else None
    )
    if args.max_live < 1:
        print("--max-live must be at least 1", file=sys.stderr)
        return USAGE
    if not 10 <= args.poll_ms <= 10_000:
        print("--poll-ms must be between 10 and 10000", file=sys.stderr)
        return USAGE
    content = None
    startup_problem = _hosted_startup_problem()
    if startup_problem is not None:
        print(startup_problem, file=sys.stderr)
        return USAGE
    serve_hosted = _hosted_env()
    if serve_hosted:  # ND195 b: the server refuses a reason setting over the hosted maximum
        serve_problems = config.content.problems(hosted=True)
        if serve_problems:
            print(serve_problems[0], file=sys.stderr)
            return USAGE
    try:
        from nuramesh.engine.content import configured_content_service

        content = configured_content_service(store, config.content)
    except ContentError as exc:
        print(f"warning: {exc}; approvals and unblocks will answer 503", file=sys.stderr)
    if content is not None:
        _reconcile_at_start(content, config.org.id)
    from nuramesh.engine.run_engine import LIGHT, EngineSelectionError, select_engine
    from nuramesh.engine.run_request import RunStartSettings, RunStartSettingsError
    from nuramesh.hosted import HostedConfigError

    try:  # R1.20 ND347 section 3: the engine rules run before any port is bound
        engine_choice = select_engine(os.environ)
        run_start_settings = RunStartSettings.from_env(os.environ)
    except (EngineSelectionError, RunStartSettingsError, HostedConfigError) as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return USAGE
    if engine_choice.note:
        logging.getLogger("nuramesh.cli").info(engine_choice.note)
    from nuramesh.engine.starter import RunStarter

    run_starter: RunStarter | None = None
    if engine_choice.engine == LIGHT:
        from nuramesh.engine.light.starter import LightStarter

        run_starter = LightStarter(store)
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            logging.getLogger("nuramesh.cli").warning(
                "the light engine is single-process and not for production load "
                "(no horizontal scale, failover no faster than its 60 s lease)"
            )
    waker: Callable[[str, str], None] | None = None
    if run_starter is not None:
        light = run_starter
        org_for_wake = config.org.id

        def waker(run_id: str, task_id: str) -> None:
            light.wake(org_for_wake, run_id, task_id)

    temporal_address = (
        os.environ.get("NURAMESH_TEMPORAL_ADDRESS", "").strip()
        if engine_choice.engine != LIGHT
        else ""
    )
    if temporal_address:
        try:
            from nuramesh.temporal.security import TemporalSecurityError
            from nuramesh.temporal.starter import StarterConfigError, build_temporal_starter
        except ImportError:
            print(
                "NURAMESH_TEMPORAL_ADDRESS needs: pip install 'nuramesh[temporal]'",
                file=sys.stderr,
            )
            return USAGE
        try:
            from nuramesh.temporal.security import record_plaintext_allowance

            record_plaintext_allowance(store, config.org.id, os.environ, temporal_address)
            # R1.20 spec 6.1/6.4: the server holds the Temporal `RunStarter` (a hosted server
            # needs the Temporal frontend to answer at startup, S13)
            temporal_starter = build_temporal_starter(store, os.environ, hosted=serve_hosted)
        except (TemporalSecurityError, StarterConfigError) as exc:
            print(f"refusing to start: {exc}", file=sys.stderr)
            return USAGE
        run_starter = temporal_starter
        org_for_temporal_wake = config.org.id

        def waker(run_id: str, task_id: str) -> None:
            if not temporal_starter.wake(org_for_temporal_wake, run_id, task_id):
                # no live workflow: raise so the wake is retried and then parked, never `sent`
                raise RuntimeError("no workflow to wake")

    # the address the server is bound to answers to its own name too (127.0.0.2, a LAN address);
    # a wildcard bind names no host
    bound_host = args.host.strip("[]").lower()
    bound = () if bound_host in ("", "0.0.0.0", "::") else (bound_host,)
    try:
        auth_settings = auth_settings_from_env(
            os.environ,
            trusted_proxy=trusted_proxies,
            auth=args.auth,
            loopback=not network,
            hosted=serve_hosted,
        )
    except ValueError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return USAGE
    from nuramesh.server.email_change import email_verify_settings_from_env
    from nuramesh.server.invites import invite_settings_from_env
    from nuramesh.server.password_reset import reset_settings_from_env

    try:
        invite_settings = invite_settings_from_env(os.environ)
        email_verify_settings = email_verify_settings_from_env(os.environ)
        reset_settings = reset_settings_from_env(os.environ)
    except ValueError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return USAGE
    try:
        sweep_seconds = decision_sweep_seconds_from_env(os.environ)
        expiry_seconds = content_expiry_seconds_from_env(os.environ)
        private_network = allow_private_network(os.environ)
        key_sweep_seconds = key_sweep_seconds_from_env(os.environ)
    except ValueError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return USAGE
    from nuramesh.core.secret_store import SecretStoreError

    try:  # R1.14c, VK67: a bad secret store setting stops the server (fixed reason, never the path)
        secret_store = secret_store_from_env(os.environ, plugin_context.secret_store_factories)
    except SecretStoreError as exc:
        print(
            f"refusing to start: NURAMESH_SECRET_STORE / NURAMESH_SECRET_DIR: {exc.detail or exc}",
            file=sys.stderr,
        )
        return USAGE
    key_sweep_lock = None if secret_store is None else _key_sweep_lock(root, args.store)
    from nuramesh.server.mail import MailConfigError, SmtpMailSender, load_mail_config

    try:  # R1.39: a bad mail setting stops the server, naming the variable (never its value)
        mail_config = load_mail_config(os.environ)
    except MailConfigError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return USAGE
    mail_sender = (
        None if mail_config.settings is None else SmtpMailSender(mail_config.settings, store.now)
    )
    setup_service = AuthService(store, config.org.id, auth_settings)
    from nuramesh.server.routes import RouteTableError, check_route_table
    from nuramesh.server.webapp import WebBundleError, load_web_bundle

    try:  # #876: the web app of this install, if it has one; a changed folder serves nothing
        web = None if args.no_app else load_web_bundle()
    except WebBundleError as exc:
        print(
            f"refusing to start: the packaged web app is damaged ({exc}); reinstall nuramesh, "
            "or add --no-app to serve the API alone",
            file=sys.stderr,
        )
        return USAGE

    plugin_settings = {
        name: factory(args) for name, factory in plugin_context.settings_factories.items()
    }
    try:
        app = create_app(
            store,
            config,
            auth=auth_settings,
            require_auth=args.auth,
            invite_settings=invite_settings,
            email_verify_settings=email_verify_settings,
            reset_settings=reset_settings,
            require_tls=network,
            allowed_hosts=(*ALLOWED_HOSTS, *hosts, *bound),
            entitlements=plugin_context.entitlements,
            content=content,
            anchors=anchor_check,
            events_poll_seconds=args.poll_ms / 1000,
            max_live_connections=args.max_live,
            on_human_decision=waker,
            plugins=plugin_context,
            plugin_settings=plugin_settings,
            decision_sweep_seconds=sweep_seconds,
            content_expiry_seconds=expiry_seconds,
            allow_private_network=private_network,
            otel_env=os.environ,
            agent_team=team_key(root.name),
            agent_settings=agent_settings,
            mail_config=mail_config,
            mail_sender=mail_sender,
            starter=run_starter,
            run_start=run_start_settings,
            secret_store=secret_store,
            key_sweep_seconds=key_sweep_seconds,
            key_sweep_lock=key_sweep_lock,
            web=web,
        )
        # the gate again, on the app as it will run (org-scope.md, G3), with the routes the
        # installed plugins declared (ND72): any other route is still refused
        check_route_table(app, app.state.plugin_route_rules, app.state.plugin_route_scopes)
    except RouteTableError as exc:
        print(f"refusing to listen: {exc}", file=sys.stderr)
        return USAGE
    # E2 spec 2.6: before the port opens, in one transaction, end the credentials of agents that
    # left the configuration or changed. If that fails the server does not start: starting anyway
    # would let the agent come back and inherit a credential that was meant to end.
    try:
        ended = app.state.agent_credentials.revoke_absent_agents()
    except Exception:
        print(
            "error: could not revoke agent credentials of agents that are not in the "
            "configuration or have changed; the server did not start",
            file=sys.stderr,
        )
        return USAGE
    if ended:
        logging.getLogger("nuramesh.cli").info(
            "revoked %d agent credentials of agents that are not in the configuration "
            "or have changed",
            ended,
        )
    shown = f"[{args.host}]" if ":" in args.host and not args.host.startswith("[") else args.host
    if args.auth:
        public = f"[{hosts[0]}]" if network and ":" in hosts[0] and hosts[0][0] != "[" else None
        where = (
            f"https://{public or hosts[0]}/v1/health"
            if network
            else f"http://{shown}:{args.port}/v1/health"
        )
        print(f"NuraMesh API on {where} (authentication on: sign in or use an API token)")
    else:
        # SA-0.2: loud, on standard error, in capitals; the start line says the same
        print(
            "WARNING: AUTHENTICATION IS OFF. THIS SERVER IS READ-ONLY: NOBODY CAN SIGN IN AND "
            "NOBODY CAN APPROVE ANYTHING. RESTART WITHOUT THE AUTH OFF FLAG TO TURN IT ON.",
            file=sys.stderr,
        )
        print(
            f"NuraMesh API on http://{shown}:{args.port}/v1/health "
            "(authentication off: localhost only, read-only, nobody can approve anything; "
            "start without --auth off to sign in)"
        )
    child: subprocess.Popen[bytes] | None = None
    if args.with_runner:
        if run_starter is None or run_starter.name != "light":
            print("--with-runner needs the light engine (NURAMESH_RUN_ENGINE)", file=sys.stderr)
            return USAGE
        # a separate process: the API never imports or runs team code (ND347 5.0)
        child = spawn_runner_child(args.team, args.store)

    announced: list[bool] = []
    app_announcer: Callable[[int], None] | None = None

    def on_bound(port: int) -> None:
        """After the bind, once per process: the setup URL on a first run at a terminal, or the
        hint (SA-0.6: never again, whatever calls this a second time)."""
        if announced:
            return
        announced.append(True)
        if app_announcer is not None:
            app_announcer(port)
        try:
            _announce_setup(
                setup_service,
                args.team,
                auth=args.auth,
                url_base=setup_url_base(
                    auth=args.auth,
                    host=args.host,
                    port=port,
                    trusted_proxy=bool(args.trusted_proxy),
                    allowed_hosts=hosts,
                    hosted=serve_hosted,
                ),
            )
        except Exception:  # noqa: BLE001 (no setup link is a delay, never a reason to stop)
            print(
                "could not prepare the first owner's setup link; run "
                f"`nuramesh auth new-setup-token --team {args.team}` in a shell of its own",
                file=sys.stderr,
            )

    if web is not None:
        # #876: the web app is in this install and served at the root of the same address
        def announce_app(port: int) -> None:
            app_url = f"https://{hosts[0]}/" if network else f"http://{shown}:{port}/"
            print(f"Open {app_url}")
            if args.open:
                open_when_listening(args.host, port, app_url)

        if args.port == 0 and not network:
            # the system picks the port at the bind: say and open it from `on_bound`
            app_announcer = announce_app
        else:
            announce_app(args.port)
    elif args.open:
        print(
            "--open: this install has no web app (it comes with the released wheel); "
            "serving the API only",
            file=sys.stderr,
        )
    try:
        _announcing_server(server_config(app, _bind_host(args.host), args.port), on_bound).run()
    finally:
        if child is not None:
            child.terminate()  # SIGTERM: the runner drains within its grace period and exits
            try:
                child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill()
    return OK


def spawn_runner_child(team: str, store: str | None) -> subprocess.Popen[bytes]:
    """The `--with-runner` child; the refresh successor key is not passed on (hygiene, RG-11)."""
    env = {k: v for k, v in os.environ.items() if k != "NURAMESH_REFRESH_SUCCESSOR_KEY"}
    return subprocess.Popen(  # noqa: S603 (our own interpreter and fixed arguments)
        with_runner_command(team, store), env=env
    )


def with_runner_command(team: str, store: str | None) -> list[str]:
    """The runner that `serve --with-runner` starts: the light runner on the team's fake model.
    `spec/real-models` (section 17) lifts the `--fake` requirement of the worker command only; a
    real-capable runner is started by hand (`nuramesh worker --engine light`)."""
    command = [
        sys.executable,
        "-c",
        "import sys; from nuramesh.cli import main; sys.exit(main(sys.argv[1:]))",
        "worker",
        team,
        "--engine",
        "light",
        "--fake",
    ]
    if store:
        command += ["--store", store]
    return command


def runner_credentials(store: Store, content: Any, org_id: str) -> tuple[Any, Any]:
    """The credential source of a real-capable light runner for its organization and its key
    fold question (`spec/real-models` R32): the key readers `nuramesh try` has, over the secret
    store the settings name (`NURAMESH_SECRET_DIR`), read only at the call, after the
    reservation. The wait before the one retry of a read is the one a stop can cut (R33, R38.10).

    Returns `(source, key_current)`. Without a usable secret store `key_current` is None, so a
    `stored` connection is refused at the start (`model_key_unavailable`, R10 check 4) and, fail
    closed, at the call (`auth_kind` with the fixed reason word: `no_secret_dir`,
    `no_ledger_hashing` or `secret_dir_refused`). Nothing else is ever tried (R34). Keyless
    connections and operator key files keep their existing rules (R37)."""
    from nuramesh.engine import key_readers
    from nuramesh.engine.call_signal import retry_wait
    from nuramesh.engine.credentials import CredentialSource

    hashing = getattr(content, "ledger_hashing", None)
    readers, reason = key_readers.key_readers_with_reason(store, hashing, sleep=retry_wait)
    if readers is None:
        return CredentialSource(stored_off_reason=reason or "no_secret_dir"), None
    return readers.source_for(org_id), readers.key_current


class _SetupEntry:
    """The lock of one run's `setup` and how many callers use it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


def worker_no_sandbox() -> Any:
    """The sandbox hook of a worker's teams: command execution is closed in hosted mode."""
    from nuramesh.core.sandbox import SandboxUnavailable

    raise SandboxUnavailable(
        "the worker has no sandbox: command execution is refused in hosted mode, ND24/ND29"
    )


def team_engine_factory(
    root: Path,
    config: Any,
    module: Any,
    store: Store,
    content: Any,
    *,
    real: bool = False,
    runner_calls: bool = False,
    credentials: Any = None,
) -> Callable[[str, str], RunEngine]:
    """The engine factory of `nuramesh worker`: one engine per call over a team folder, on the
    team's fake model, with **no sandbox** (a test-command gate fails with a clear message and the
    task goes to a human: command execution is closed in hosted mode, ND24/ND29).

    `real` (the light runner started without `--fake`, `spec/real-models` R4, R5): the provider
    of each call is built from the connection the resolver chose (`GovernedProviders`), and a
    `fake/*` model is still answered by the team's fake provider. `runner_calls` turns on the
    light runner's call rules (`EngineSettings.runner_calls`). `credentials` is the first value
    of `runner_credentials` (R32); without it a `real` factory reads no stored key (refused
    `auth_kind`, `no_secret_dir`). The Temporal worker passes none of the three.

    `setup` runs once per (organization, run) in this process, not once per activity; the last 32
    runs are kept, so a long-lived worker doesn't grow without bound (each `setup` may create
    scratch files). Team state is therefore shared by one run's tasks inside a process, but not
    across replicas or restarts.
    """
    no_sandbox = worker_no_sandbox
    if real:  # one source for the process (token reuse; a stored key is read per call)
        from nuramesh.engine.credentials import CredentialSource

        if credentials is None:
            credentials = CredentialSource(stored_off_reason="no_secret_dir")

    teams: OrderedDict[tuple[str, str], Any] = OrderedDict()
    teams_lock = threading.Lock()  # guards `teams` and `setting_up` only, never held in `setup`
    sources: dict[tuple[str, str], Any] = {}  # `real`: the provider source of each kept team
    setting_up: dict[tuple[str, str], _SetupEntry] = {}

    def team_for(org_id: str, run_id: str) -> Any:
        """`setup` runs once per run (and org) in this process, not once per activity: team state
        is shared by the run's tasks here, but not across replicas or restarts. The last 32 runs
        are kept, so a long-lived worker doesn't grow without bound. Evicting a run only drops
        the reference: what `setup` created (scratch folders, exit handlers) lives until the
        process ends, because a team has no teardown hook (`docs/containers.md`). A slow `setup`
        blocks only calls for the same run."""
        key = (org_id, run_id)
        with teams_lock:
            if key in teams:
                teams.move_to_end(key)
                return teams[key]
            entry = setting_up.setdefault(key, _SetupEntry())
            entry.users += 1
        try:
            with entry.lock:
                with teams_lock:
                    if key in teams:
                        teams.move_to_end(key)
                        return teams[key]
                env = TeamEnv(
                    root=root,
                    config=config,
                    store=store,
                    run_id=run_id,
                    fake=not real,
                    sandbox=no_sandbox,
                )
                team = setup_team(module, env)
                if not real and not isinstance(team.fake_provider, FakeModel):
                    raise ValueError("--fake needs the team's fake_provider to be a FakeModel")
                if real:  # one source per run: its built providers are reused by every step
                    from nuramesh.engine.governance import GovernedProviders

                    source = GovernedProviders(fake=team.fake_provider, credentials=credentials)
                with teams_lock:
                    teams[key] = team
                    if real:
                        sources[key] = source
                    while len(teams) > 32:
                        sources.pop(teams.popitem(last=False)[0], None)
                return team
        finally:
            with teams_lock:  # the entry goes when its last user leaves, so one run never has
                entry.users -= 1  # two setups at once, even after a failed one
                if entry.users == 0 and setting_up.get(key) is entry:
                    del setting_up[key]

    from nuramesh.engine.governance import Governance

    # one governance for the factory: every engine reads the same organization ledger fold
    governance = Governance(
        store,
        config.org,
        prices=config.prices,
        overrides=config.price_overrides,
        models=config.models,
        halted=cli_models.org_halted(store, config.org.id),
    )

    def factory(org_id: str, run_id: str) -> RunEngine:
        if org_id != config.org.id:
            raise ValueError("this worker serves another organization")
        team = team_for(org_id, run_id)
        provider: Any = team.fake_provider
        if real:
            with teams_lock:
                provider = sources.get((org_id, run_id))
            if provider is None:  # evicted between the two looks (33 runs at once): build again
                from nuramesh.engine.governance import GovernedProviders

                provider = GovernedProviders(fake=team.fake_provider, credentials=credentials)
        # R1.37: a tombstoned organization must not fail the factory (it would fail `finish_run`
        # too and the run would never end): the engine is built in wind-down mode, no overrides,
        # and `run_task` raises the matching code before any model call
        overrides, wind_down = _worker_overrides(config, store)
        return RunEngine(
            store,
            config.org,
            provider=provider,
            prices=config.prices,
            policies=config.effective_policies,
            workers=team.workers,
            approver=team.approver,
            evals=config.evals,
            presets=config.presets,
            calibration=config.calibration,
            canon=config.canon,
            overrides=overrides,
            gate_runners=team.gate_runners,
            content=content,
            content_settings=config.content,  # RET-WIRE R2: the organization's retention
            settings=EngineSettings(wind_down=wind_down, runner_calls=runner_calls),
            governance=governance,
            breakers=config.breakers.rules,
            hosted_ids=_hosted_env(),
        )

    factory.team_for = team_for  # type: ignore[attr-defined]  # the run builder reads the plan
    return factory


class TeamRunBuilder:
    """The Temporal worker's `RunBuilder` (R1.20 PR C): the engine and plan of a claimed start
    request, from the team folder. A request with no plan starts the team's own plan; one that
    carries a plan of its own is built by `ApiRunBuilder` (ND347 4.6, ND348): the same builder,
    codes and checks as the light runner."""

    def __init__(self, factory: Callable[[str, str], RunEngine]) -> None:
        self.factory = factory

    def build(self, request: Any) -> Any:
        from nuramesh.engine.light import ApiRunBuilder, PreparedRun

        if request.plan.strip() not in ("", "[]"):
            return ApiRunBuilder(self.factory).build(request)
        engine = self.factory(request.org_id, request.run_id)
        team = self.factory.team_for(request.org_id, request.run_id)  # type: ignore[attr-defined]
        plan = team.plan(engine.org) if callable(team.plan) else team.plan
        return PreparedRun(engine=engine, plan=list(plan), data_class=request.data_class)


def _cmd_worker_light(args: argparse.Namespace) -> int:
    """`nuramesh worker --engine light`: the runner of the light engine (R1.20, ND347 5.0). The
    one process that runs the team's code for runs started through the API on a self-hosted
    install: it claims start requests, checks the plan against the team and runs the steps. It
    needs no Temporal. With `--fake` it is fake-only: a team with a real model exits 2 and a real
    plan is refused `real_model_not_supported`. Without `--fake` it is real-capable
    (`spec/real-models` R4 to R6): the admission checks of R10 decide per plan, and the fake
    model keeps working. It has no sandbox yet (a plan with a command gate is refused
    `sandbox_unavailable`). It reads stored keys through the secret store of
    `NURAMESH_SECRET_DIR` (R32); without one a `stored` connection is refused
    `model_key_unavailable`."""
    from nuramesh.engine.admission import RunnerAdmission, customer_data_enabled
    from nuramesh.engine.light import ApiRunBuilder, LightConfigError, LightRunner, LightSettings
    from nuramesh.engine.run_engine import LIGHT, EngineSelectionError, select_engine

    team_dir = args.team or os.environ.get("NURAMESH_TEAM_DIR", "")
    if not team_dir:
        print("name the team folder: a TEAM argument or NURAMESH_TEAM_DIR", file=sys.stderr)
        return USAGE
    try:
        os.environ.setdefault("NURAMESH_RUN_ENGINE", LIGHT)
        if select_engine(os.environ).engine != LIGHT:
            raise EngineSelectionError("NURAMESH_RUN_ENGINE selects another engine")
        settings = LightSettings.from_env(os.environ)
    except (EngineSelectionError, LightConfigError) as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return USAGE
    root = Path(team_dir).resolve()
    try:
        config = load_team_config(root)
        module = load_team_module(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    problem = _fake_only_problem(config) if args.fake else None
    if problem:
        print(problem, file=sys.stderr)
        return USAGE
    store: Store
    url = database_url()
    try:
        if url:
            from nuramesh.stores.postgres import PostgresStore

            store = PostgresStore(url, migrate=False)  # services never migrate (issue 897)
        else:
            store = _store(root, args.store)
    except ImportError:
        print("NURAMESH_DATABASE_URL needs: pip install 'nuramesh[postgres]'", file=sys.stderr)
        return USAGE
    except StoreError as exc:
        print(f"couldn't open the database: {exc}", file=sys.stderr)
        return _open_failed_code(exc)
    cli_models.seed_and_warn(store, config)
    effective = (
        _fake_only_problem(
            config,
            refs=cli_models.effective_agent_refs(cli_models.context_for(root, config, store)),
        )
        if args.fake
        else None
    )
    if effective:
        print(effective, file=sys.stderr)
        store.close()
        return USAGE
    try:
        from nuramesh.engine.content import ContentError, configured_content_service

        content = configured_content_service(store, config.content)
    except ContentError as exc:
        print(f"the runner needs the content store: {exc}", file=sys.stderr)
        store.close()
        return USAGE
    _reconcile_at_start(content, config.org.id)
    real = not args.fake
    # R4: the one change for a `--fake` runner is R12, so the runner's call rules and the key
    # readers (R32) are for a real-capable runner only: a fake-only runner opens no secret store.
    # The sandbox choice (R40) is not wired yet: no sandbox (fails closed in admission)
    credentials, key_current = (
        runner_credentials(store, content, config.org.id) if real else (None, None)
    )
    factory = team_engine_factory(
        root, config, module, store, content, real=real, runner_calls=real, credentials=credentials
    )
    admission = RunnerAdmission(
        fake_only=args.fake,
        customer_enabled=customer_data_enabled(os.environ),  # the runner's own flag (R10.2)
        sandbox="none",
        key_current=key_current,  # None without a usable secret store (R10 check 4)
    )
    runner = LightRunner(
        store,
        factory,
        builder=ApiRunBuilder(factory),
        settings=settings,
        content=content,
        admission=admission,
    )
    mode = "real models on" if real else "fake model only"
    print(f"NuraMesh light runner for organization {config.org.id} ({mode})", flush=True)
    try:
        return runner.serve()
    finally:
        store.close()


def cmd_worker(args: argparse.Namespace) -> int:
    """Run a Temporal worker for durable runs (extra `nuramesh[temporal]`). Everything comes from
    flags or `NURAMESH_*` environment variables and the worker refuses to start without a Temporal
    address and a team folder. The Temporal worker runs the team's fake model only, so `--fake` is
    required (`spec/real-models` R2). `--engine light` runs the light engine's runner instead (no
    Temporal), where `--fake` is optional (R4)."""
    if args.engine == "light":
        return _cmd_worker_light(args)
    try:
        from nuramesh.temporal.health import HealthBindError, HealthConfigError, port_from_env
        from nuramesh.temporal.service import (
            WorkerConnectionError,
            connect_timeout_from_env,
            serve,
        )
    except ImportError:
        print("the worker needs the extra: pip install 'nuramesh[temporal]'", file=sys.stderr)
        return USAGE
    try:
        connect_timeout = connect_timeout_from_env(os.environ)
        health_port = port_from_env(os.environ)
    except (ValueError, HealthConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    address = args.address or os.environ.get("NURAMESH_TEMPORAL_ADDRESS", "")
    team_dir = args.team or os.environ.get("NURAMESH_TEAM_DIR", "")
    namespace = args.namespace or os.environ.get("NURAMESH_TEMPORAL_NAMESPACE", "default")
    queue = args.task_queue or os.environ.get("NURAMESH_TEMPORAL_TASK_QUEUE", "nuramesh")
    if not address:
        print("set NURAMESH_TEMPORAL_ADDRESS (or --address), e.g. temporal:7233", file=sys.stderr)
        return USAGE
    if not team_dir:
        print("name the team folder: a TEAM argument or NURAMESH_TEAM_DIR", file=sys.stderr)
        return USAGE
    if not args.fake:
        print(
            "a durable worker for real providers needs the owner's approval first; "
            "use --fake (the team's fake model)",
            file=sys.stderr,
        )
        return USAGE
    if args.max_activities < 1:
        print("--max-activities must be at least 1", file=sys.stderr)
        return USAGE
    if not os.environ.get("NURAMESH_KEY_DIR"):
        print(
            "set NURAMESH_KEY_DIR to a persistent directory shared by every worker replica "
            "(a mounted volume): content keys live there, and a worker restarted without them "
            "can no longer read stored content",
            file=sys.stderr,
        )
        return USAGE
    try:
        from nuramesh.temporal.security import TemporalSecurityError, security_for

        # the dev allowance is armed (ledger entry) once the store is open, below; nothing
        # connects before that, and `connect` checks again
        security_for(address, os.environ, allowance_recorded=True)
    except TemporalSecurityError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return USAGE
    root = Path(team_dir).resolve()
    try:
        config = load_team_config(root)
        module = load_team_module(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    problem = _fake_only_problem(config)
    if problem:
        print(problem, file=sys.stderr)
        return USAGE
    # RET-WIRE R6 (Q2 = a): NURAMESH_HOSTED=1 builds the content service hosted, which caps
    # retention at 90 days; the worker refuses to start with a larger value (opt-in, so it is a
    # required deployment setting; any other value than unset, empty, 0 or 1 is a usage error)
    startup_problem = _hosted_startup_problem()
    if startup_problem is not None:
        print(startup_problem, file=sys.stderr)
        return USAGE
    hosted = _hosted_env()
    if hosted:
        hosted_problems = config.content.problems(hosted=True)
        if hosted_problems:
            print(hosted_problems[0], file=sys.stderr)
            return USAGE
    url = database_url()  # a secret: read from the environment only
    store: Store
    if url:  # SQLite is opened only without a URL, so a read-only team folder works with Postgres
        try:
            from nuramesh.stores.postgres import PostgresStore

            store = PostgresStore(url, migrate=False)  # services never migrate (issue 897)
        except ImportError:
            print("NURAMESH_DATABASE_URL needs: pip install 'nuramesh[postgres]'", file=sys.stderr)
            return USAGE
        except StoreError as exc:
            print(f"couldn't open the database: {exc}", file=sys.stderr)  # never includes the URL
            return _open_failed_code(exc)
    else:
        try:
            store = _store(root, args.store)
        except StoreError as exc:
            print(f"couldn't open the database: {exc}", file=sys.stderr)
            return USAGE
    cli_models.seed_and_warn(store, config)
    try:  # the dev allowance for a non-loopback plaintext Temporal: ledgered before any connect
        from nuramesh.temporal.security import record_plaintext_allowance

        record_plaintext_allowance(store, config.org.id, os.environ, address)
    except (TemporalSecurityError, StoreError) as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        store.close()
        return USAGE
    effective = _fake_only_problem(
        config, refs=cli_models.effective_agent_refs(cli_models.context_for(root, config, store))
    )
    if effective:  # an admin may have assigned a real model since the configuration was written
        print(effective, file=sys.stderr)
        store.close()
        return USAGE
    try:
        from nuramesh.engine.content import configured_content_service

        content = configured_content_service(store, config.content)
    except ContentError as exc:
        print(f"the worker needs the content store: {exc}", file=sys.stderr)
        return USAGE
    _reconcile_at_start(content, config.org.id)

    factory = team_engine_factory(root, config, module, store, content)
    try:  # the team must build and run on its fake model now, not on the first activity
        probe = setup_team(
            module,
            TeamEnv(
                root=root,
                config=config,
                store=MemoryStore(),  # a throwaway store: the check writes nothing real
                run_id="startup-check",
                fake=True,
                sandbox=worker_no_sandbox,
            ),
        )
    except TeamError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except Exception as exc:  # the team's own code: its message may quote anything, name the type
        print(
            f"the team's setup failed at start: {type(exc).__name__} "
            "(run the team with `nuramesh try --fake` to see the details)",
            file=sys.stderr,
        )
        return USAGE
    if not isinstance(probe.fake_provider, FakeModel):
        print("--fake needs the team's fake_provider to be a FakeModel", file=sys.stderr)
        return USAGE

    import asyncio

    print(f"NuraMesh worker on queue {queue} at {address} (namespace {namespace})", flush=True)
    try:
        asyncio.run(
            serve(
                address,
                factory,
                namespace=namespace,
                task_queue=queue,
                max_activities=args.max_activities,
                builder=TeamRunBuilder(factory),
                content=content,
                connect_timeout=connect_timeout,
                health_port=health_port,
            )
        )
    except HealthBindError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except WorkerConnectionError as exc:
        print(str(exc), file=sys.stderr)
        return NOT_DONE
    except Exception as exc:  # e.g. a namespace the server doesn't know: a plain message, no trace
        print(
            f"the worker failed: {type(exc).__name__} (check the namespace, the task queue "
            "and the server)",
            file=sys.stderr,
        )
        return NOT_DONE
    print("worker stopped", flush=True)
    return OK


_TYPED: list[str] = []  # the words of this invocation, for the usage-error scrubber


class _Parser(argparse.ArgumentParser):
    """argparse, except that a usage error of `models key ...` never prints a typed value."""

    def error(self, message: str) -> Any:
        from nuramesh.cli_model_keys import is_key_command, scrub_usage_error

        if is_key_command(_TYPED):
            message = scrub_usage_error(message, _TYPED)
        from nuramesh.cli_suggest import fail, suggest

        text = suggest(self, message, _TYPED)
        if text is not None:
            fail(text)
        super().error(message)


def parser(plugins: PluginContext | None = None) -> argparse.ArgumentParser:
    """The command-line parser; `plugins` adds the commands installed plugins register."""
    plugins = plugins or PluginContext()
    p = _Parser(prog="nuramesh", description="Verified, budgeted AI agent runs.")
    sub = p.add_subparsers(dest="command", required=False)

    v = sub.add_parser("validate", help="check a configuration file")
    v.add_argument("path")
    v.add_argument(
        "--store",
        help="SQLite file (ignored with NURAMESH_DATABASE_URL); refuses an erased org id",
    )
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("try", help="run a team folder")
    r.add_argument("team", nargs="?", help="the team folder (asked on a terminal)")
    r.add_argument("--fake", action="store_true", help="use the fake model (no keys, no cost)")
    # removed (L5, ND92): parsed only to say so; the providers come from the models' connections
    r.add_argument("--provider", action="append", help=argparse.SUPPRESS)
    r.add_argument(
        "--data",
        choices=("synthetic", "customer"),
        help="what data this run sends to the model (a global deployment accepts only synthetic)",
    )
    r.add_argument(
        "--confirm-cap", metavar="USD", help="the run budget you accept spending up to (a real run)"
    )
    r.add_argument("--config", metavar="FILE", help="another config file in the team folder")
    r.add_argument("--run-id")
    r.add_argument(
        "--store",
        help="SQLite file (default: <team>/.nuramesh/runs.db; ignored with NURAMESH_DATABASE_URL)",
    )
    r.add_argument("--approve-as", metavar="HUMAN", help="scripted approvals as human:<id>")
    r.add_argument("--interactive", action="store_true", help="ask for approvals here")
    r.add_argument("--human", metavar="HUMAN", help="your id for --interactive (human:<id>)")
    r.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="LOCAL ONLY: run commands without a container (recorded, not isolated)",
    )
    r.add_argument("--anchor-repo", help="export this run's ledger head here at the end")
    r.add_argument("--anchor-key", help="private key file for --anchor-repo")
    r.add_argument(
        "--anchor-push",
        action="store_true",
        help="also push the anchors (see `anchor export --push`)",
    )
    r.add_argument("--parallel", type=int, default=4)
    r.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    r.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="show a run's report from the ledger")
    add_team(rep)
    rep.add_argument("run_id", nargs="?", metavar="RUN", help="the run's id (asked on a terminal)")
    rep.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    rep.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    rep.set_defaults(func=cmd_report)

    sr = sub.add_parser(
        "start",
        help="request a run from a plan file (host only; needs an owner's id and a runner)",
    )
    sr.add_argument("team")
    sr.add_argument("--plan", required=True, metavar="FILE", help="JSON body of POST /v1/runs")
    sr.add_argument("--human", required=True, metavar="HUMAN", help="an active owner, human:<id>")
    sr.add_argument("--store", help="SQLite file (ignored with NURAMESH_DATABASE_URL)")
    sr.add_argument(
        "--dry-run", action="store_true", help="run every check, write nothing, print the verdict"
    )
    sr.set_defaults(func=cmd_start)

    st = sub.add_parser("stop", help="ask a run to stop (recorded in its ledger)")
    add_team(st)
    st.add_argument("run_id", nargs="?", metavar="RUN", help="the run's id (asked on a terminal)")
    st.add_argument("--human", metavar="HUMAN", help="who is stopping it: human:<id>")
    st.add_argument("--reason", help="why (kept in the content store; the ledger keeps a hash)")
    st.add_argument("--store")
    st.set_defaults(func=cmd_stop)

    from nuramesh.cli_everyday import add_parsers as add_everyday_parsers

    add_everyday_parsers(sub)

    claim = (
        "who is doing it: human:<id>. This is your own claim (the CLI cannot prove who types it, "
        "as for `stop`); it is accepted because this command only restricts. Lifting (resume, "
        "reset, disable, enable) is possible only through the signed-in API and the app."
    )

    og = sub.add_parser(
        "org",
        help="the organization kill switch (status, halt; API and app only: resume) "
        "and the prompt-cache setting (prompt-cache on|off)",
    )
    og_sub = og.add_subparsers(dest="org_command", required=True)
    ogs = og_sub.add_parser("status", help="is the organization halted? which roles are paused?")
    add_team(ogs)
    ogs.add_argument("--store")
    ogs.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    ogs.set_defaults(func=cmd_org_status)
    ogh = og_sub.add_parser(
        "halt", help="halt the whole organization: nothing new starts, open runs are asked to stop"
    )
    add_team(ogh)
    ogh.add_argument("--human", required=True, metavar="HUMAN", help=claim)
    ogh.add_argument("--store")
    ogh.set_defaults(func=cmd_org_halt)
    ogp = og_sub.add_parser(
        "prompt-cache",
        help="turn provider-side prompt caching on or off (off by default; not in hosted mode)",
    )
    ogp.add_argument("state", choices=["on", "off"])
    add_team(ogp)
    ogp.add_argument(
        "--human",
        required=True,
        metavar="HUMAN",
        help="who is doing it: human:<id>. This is your own claim (the CLI cannot prove who "
        "types it); it is ledgered with this name, and the command is refused in hosted mode. "
        "`on` loosens the default, so use it deliberately.",
    )
    ogp.add_argument("--store")
    ogp.set_defaults(func=cmd_org_prompt_cache)

    ro = sub.add_parser("roles", help="pause one role (resume is API and app only)")
    ro_sub = ro.add_subparsers(dest="roles_command", required=True)
    rop = ro_sub.add_parser("pause", help="pause a role; other roles keep working")
    add_team(rop)
    rop.add_argument("role")
    rop.add_argument("--human", required=True, metavar="HUMAN", help=claim)
    rop.add_argument("--store")
    rop.set_defaults(func=cmd_roles_pause)

    bk = sub.add_parser(
        "breakers",
        help="circuit breakers: list and trip (reset, disable, enable: API and app only)",
    )
    bk_sub = bk.add_subparsers(dest="breakers_command", required=True)
    bkl = bk_sub.add_parser("list", help="every breaker with its numbers now (read-only)")
    add_team(bkl)
    bkl.add_argument("--store")
    bkl.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    bkl.set_defaults(func=cmd_breakers_list)
    bkt = bk_sub.add_parser("trip", help="trip one breaker by hand (a drill)")
    add_team(bkt)
    bkt.add_argument("name")
    bkt.add_argument("--human", required=True, metavar="HUMAN", help=claim)
    bkt.add_argument("--store")
    bkt.set_defaults(func=cmd_breakers_trip)

    db = sub.add_parser("db", help="the database: apply migrations")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    mg = db_sub.add_parser(
        "migrate", help="apply pending PostgreSQL migrations (needs NURAMESH_DATABASE_URL)"
    )
    add_team(mg)
    mg.add_argument(
        "--plan",
        action="store_true",
        help="print the pending migrations (not additive ones marked) and the rights the "
        "connecting role lacks; changes nothing",
    )
    mg.add_argument(
        "--i-have-a-backup",
        metavar="REF",
        help="required while a not-additive migration is pending: name the backup (a dump file "
        "or a timestamp); recorded in the ledger. Or set NURAMESH_MIGRATE_ACK to the highest "
        "pending number",
    )
    mg.add_argument(
        "--grant-to",
        metavar="ROLE",
        help="then grant this service role its least-privilege rights (run as the table owner)",
    )
    mg.set_defaults(func=cmd_migrate)

    from nuramesh.cli_keys import add_parsers as add_key_parsers

    content_sub = add_key_parsers(sub, _open_server_store)
    ec = content_sub.add_parser("expire", help="delete stored content whose retention has passed")
    add_team(ec)
    ec.add_argument("--store")
    ec.set_defaults(func=cmd_expire_content)

    from nuramesh.cli_integrations import add_parsers as add_integration_parsers

    add_integration_parsers(sub)
    from nuramesh.cli_minter import add_parsers as add_minter_parsers

    add_minter_parsers(sub, _open_server_store, _open_existing_store)

    from nuramesh.cli_autonomy import add_parsers as add_autonomy_parsers

    add_autonomy_parsers(sub, _open_server_store)

    from nuramesh.cli_otel import add_parsers as add_otel_parsers

    add_otel_parsers(sub, _open_server_store)
    from nuramesh.cli_email import add_parsers as add_email_parsers

    add_email_parsers(sub, _open_server_store)

    from nuramesh.cli_demo import add_parsers as add_demo_parsers
    from nuramesh.cli_doctor import add_parsers as add_doctor_parsers

    add_doctor_parsers(sub)
    add_demo_parsers(sub)

    from nuramesh.cli_savings import add_parsers as add_savings_parsers

    add_savings_parsers(sub, _open_server_store)

    sv = sub.add_parser(
        "serve",
        help="serve the API (authentication on by default; localhost unless behind a TLS proxy)",
        epilog=(
            "environment: NURAMESH_TEMPORAL_ADDRESS (host:port of Temporal; when set, a person's "
            "decision on a human gate also wakes the durable run; needs nuramesh[temporal]) and "
            "NURAMESH_TEMPORAL_NAMESPACE (default 'default')"
        ),
    )
    sv.add_argument("team", nargs="?", help="team folder (default: found as for --team)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    sv.add_argument(
        "--auth",
        nargs="?",
        const=True,
        default=True,
        type=_auth_flag,
        metavar="on|off",
        help="authentication, on by default (a bare --auth means on): every route needs a "
        "signed-in person or an API token. `--auth off` must be typed: localhost only, "
        "read-only, nobody can approve anything",
    )
    sv.add_argument(
        "--allowed-host",
        action="append",
        metavar="NAME",
        help="a Host name the server answers for besides localhost (repeatable)",
    )
    sv.add_argument("--anchors", metavar="REPO", help="anchor repository: verify also checks it")
    sv.add_argument(
        "--pubkey",
        metavar="FILE",
        action="append",
        help="public key that signed the anchors (the root first; repeat for a lost-key rotation)",
    )
    sv.add_argument(
        "--tail-valid-until",
        metavar="COMMIT",
        help="the commit that ends the lost key's anchors (required with a second --pubkey)",
    )
    sv.add_argument("--anchor-ref", default="ledger-anchors", help="the anchor branch to read")
    sv.add_argument(
        "--max-live",
        type=int,
        default=50,
        help="most concurrent live (WebSocket) connections (default 50)",
    )
    sv.add_argument(
        "--poll-ms",
        type=int,
        default=500,
        help="how often live events look for new ledger entries (default 500)",
    )
    sv.add_argument(
        "--trusted-proxy",
        action="store_true",
        help="trust X-Forwarded-Proto/-For (Secure cookies, client IP) from the TLS proxies "
        "listed in NURAMESH_TRUSTED_PROXIES (CIDRs or addresses); refuses to start without it",
    )
    sv.add_argument(
        "--with-runner",
        action="store_true",
        help="also start the light engine's runner as a child process (self-host convenience; "
        "runs on the team's fake model)",
    )
    sv.add_argument(
        "--open",
        action="store_true",
        help="open the web app in the browser once the server listens (needs the web app "
        "that comes with the released wheel)",
    )
    sv.add_argument(
        "--no-app",
        action="store_true",
        help="serve the API only, even when this install has the web app",
    )
    sv.set_defaults(func=cmd_serve)

    wk = sub.add_parser(
        "worker",
        help="run a Temporal worker (nuramesh[temporal])",
    )
    wk.add_argument("team", nargs="?", help="team folder (or NURAMESH_TEAM_DIR)")
    wk.add_argument("--address", help="Temporal server host:port (or NURAMESH_TEMPORAL_ADDRESS)")
    wk.add_argument("--namespace", help="default 'default' (or NURAMESH_TEMPORAL_NAMESPACE)")
    wk.add_argument("--task-queue", help="default 'nuramesh' (or NURAMESH_TEMPORAL_TASK_QUEUE)")
    wk.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    wk.add_argument(
        "--fake",
        action="store_true",
        help="run on the team's fake model only (required on temporal, optional on light)",
    )
    wk.add_argument("--max-activities", type=int, default=16)
    wk.add_argument(
        "--engine",
        choices=("temporal", "light"),
        default="temporal",
        help="temporal (default) or light: the self-host runner for API-started runs, no Temporal",
    )
    wk.set_defaults(func=cmd_worker)

    au = sub.add_parser("auth", help="accounts: first-owner setup token, API tokens")
    au_sub = au.add_subparsers(dest="auth_command", required=True)
    nst = au_sub.add_parser(
        "new-setup-token", help="replace the first-owner setup token and show the new one once"
    )
    add_team(nst)
    nst.add_argument("--store")
    nst.set_defaults(func=cmd_auth_new_setup_token)
    ps = au_sub.add_parser(
        "purge-sessions", help="delete ended sessions and security-log rows past retention"
    )
    add_team(ps)
    ps.add_argument("--store")
    ps.set_defaults(func=cmd_auth_purge_sessions)
    eo = au_sub.add_parser(
        "erase-owner",
        help="operator: hand ownership to someone, then erase a (sole) owner (no HTTP route)",
    )
    add_team(eo)
    eo.add_argument("--store")
    eo.add_argument("--user", required=True, help="the opaque id of the user to erase")
    eo.add_argument("--promote", help="an active user to make owner (their id or email)")
    eo.add_argument("--new-owner-email", help="create a new owner (password typed on the terminal)")
    eo.add_argument("--new-owner-name")
    eo.set_defaults(func=cmd_auth_erase_owner)
    co = au_sub.add_parser(
        "close-org", help="operator: close the organization and erase every user (no HTTP route)"
    )
    add_team(co)
    co.add_argument("--store")
    co.set_defaults(func=cmd_auth_close_org)
    rc = au_sub.add_parser(
        "recovery-code",
        help="operator: the single-use code that erase-owner and close-org ask for",
    )
    rc_sub = rc.add_subparsers(dest="recovery_command", required=True)
    rci = rc_sub.add_parser("issue", help="make a recovery code and show it once (terminal only)")
    add_team(rci)
    rci.add_argument("--store", default=None)
    rci.set_defaults(func=cmd_auth_recovery_code_issue)
    mf = au_sub.add_parser("mfa", help="operator: multi-factor sign-in status and reset")
    mf_sub = mf.add_subparsers(dest="mfa_command", required=True)
    mfs = mf_sub.add_parser("status", help="the policy and who is enrolled (no secrets)")
    add_team(mfs)
    mfs.add_argument("--store")
    mfs.set_defaults(func=cmd_auth_mfa_status)
    mr = mf_sub.add_parser(
        "reset",
        help="operator: remove one person's MFA with a recovery code (no HTTP route)",
    )
    add_team(mr)
    mr.add_argument("--store")
    mr.add_argument("--user", required=True, help="the opaque id of the person")
    mr.set_defaults(func=cmd_auth_mfa_reset)
    ky = au_sub.add_parser(
        "factor-key", help="operator: the auth key that seals TOTP secrets (was `auth key`)"
    )
    ky_sub = ky.add_subparsers(dest="key_command", required=True)
    kyi = ky_sub.add_parser("init", help="make a new key file (mode 0600); never prints the key")
    add_team(kyi)
    kyi.add_argument("--store")
    kyi.add_argument("--file", required=True, help="where to create the key file")
    kyi.set_defaults(func=cmd_auth_key_init)
    kyr = ky_sub.add_parser("rotate", help="re-seal factors from the previous key to the current")
    add_team(kyr)
    kyr.add_argument("--store")
    kyr.set_defaults(func=cmd_auth_key_rotate)
    kyd = ky_sub.add_parser(
        "destroy", help="delete every factor, switch the policy off, erase the key files"
    )
    add_team(kyd)
    kyd.add_argument("--store")
    kyd.add_argument(
        "--confirm",
        metavar="ORG",
        help="an extra check: must equal the organization when given (the question is still "
        "asked on a terminal; --yes skips it)",
    )
    kyd.set_defaults(func=cmd_auth_key_destroy)
    cli_invites.register(au_sub)
    cli_password_reset.register(au_sub)
    tk = au_sub.add_parser("tokens", help="API tokens for programs (scopes read and run)")
    tk_sub = tk.add_subparsers(dest="tokens_command", required=True)
    tc = tk_sub.add_parser("create", help="create a token and show it once (terminal only)")
    add_team(tc)
    tc.add_argument(
        "--name",
        required=True,
        help=(
            "a label for the program or purpose (ci-nightly), NOT a person's name; the ledger "
            "records the token id, never this name; reserved: cli, system, api, engine, tok_*, "
            "erased-*"
        ),
    )
    tc.add_argument(
        "--scope", action="append", required=True, choices=("read", "run"), help="repeatable"
    )
    tc.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"lifetime in days (1 to 30; default {CLI_TOKEN_DAYS})",
    )
    tc.add_argument("--store")
    tl = tk_sub.add_parser("list", help="list tokens (never the secrets)")
    add_team(tl)
    tl.add_argument("--store")
    tr = tk_sub.add_parser("revoke", help="revoke a token at once")
    add_team(tr)
    tr.add_argument("token", help="the token's id or name")
    tr.add_argument("--store")
    trn = tk_sub.add_parser("rename", help="give a token a new name (any state)")
    add_team(trn)
    trn.add_argument("token", help="the token's id or name")
    trn.add_argument(
        "new_name",
        help="a label for the program or purpose, NOT a person's name; reserved: cli, system, "
        "api, engine, tok_*, erased-*",
    )
    trn.add_argument("--store")
    tdl = tk_sub.add_parser("delete", help="delete a revoked or expired token (revoke it first)")
    add_team(tdl)
    tdl.add_argument("token", help="the token's id or name")
    tdl.add_argument("--store")
    tsc = tk_sub.add_parser(
        "scrub-erased",
        help="replace the personal name and issuer of tokens whose issuer was erased before this "
        "version (never revokes)",
    )
    add_team(tsc)
    tsc.add_argument("--store")
    tk.set_defaults(func=cmd_auth_tokens)

    ag = sub.add_parser("agents", help="agent identity: credentials of agents")
    ag_sub = ag.add_subparsers(dest="agents_command", required=True)
    ac = ag_sub.add_parser(
        "credentials",
        help="list or revoke agent credentials (issuing and renewing are API and app only)",
    )
    ac_sub = ac.add_subparsers(dest="credentials_command", required=True)
    acl = ac_sub.add_parser("list", help="list credentials (never the secrets)")
    add_team(acl)
    acl.add_argument("--agent")
    acl.add_argument("--store")
    acr = ac_sub.add_parser("revoke", help="revoke one credential at once")
    add_team(acr)
    acr.add_argument("credential", help="the credential's id")
    acr.add_argument("--store")
    aca = ac_sub.add_parser("revoke-all", help="revoke every live credential of one agent")
    add_team(aca)
    aca.add_argument("--agent", required=True)
    aca.add_argument("--store")
    ac.set_defaults(func=cmd_agents_credentials)

    an = sub.add_parser("anchor", help="sign ledger heads and commit them to an anchor repository")
    an_sub = an.add_subparsers(dest="anchor_command", required=True)
    kg = an_sub.add_parser("keygen", help="create an Ed25519 key pair (private key mode 0600)")
    kg.add_argument("--private", required=True)
    kg.add_argument("--public", required=True)
    ex = an_sub.add_parser("export", help="export the current heads")
    add_team(ex)
    ex.add_argument("--repo", required=True, help="anchor repository (a branch other than main)")
    ex.add_argument("--key", required=True, help="private key file")
    ex.add_argument("--stream", action="append")
    ex.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    ex.add_argument(
        "--push",
        action="store_true",
        help="then push branch ledger-anchors (created on first use, never main) with the SSH "
        "deploy key named by NURAMESH_ANCHOR_SSH_KEY",
    )
    ex.add_argument("--remote", default="origin", help="remote to push to (default origin)")
    ro = an_sub.add_parser("rotate", help="replace the signing key with a signed key change")
    ro.add_argument("team")
    ro.add_argument(
        "--repo", required=True, help="anchor repository (the anchor branch checked out)"
    )
    ro.add_argument("--key", help="the old private key file")
    ro.add_argument("--new-key", required=True, help="the new private key file (anchor keygen)")
    ro.add_argument("--lost-old-key", metavar="KEY_ID", help="the old key is lost: its id")
    ro.add_argument(
        "--compromised-since",
        metavar="TIME",
        help="the first time the old key may have been misused (ISO 8601 with Z or +HH:MM)",
    )
    ro.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    ro.add_argument(
        "--push", action="store_true", help="push the key change with NURAMESH_ANCHOR_SSH_KEY"
    )
    ro.add_argument("--remote", default="origin", help="remote to push to (default origin)")
    av = an_sub.add_parser(
        "verify", help="check the anchors; with --remote, from a clone this command owns"
    )
    av.add_argument("team")
    av.add_argument("--repo", required=True, help="anchor repository (also names the remote)")
    av.add_argument(
        "--pubkey",
        action="append",
        required=True,
        metavar="FILE",
        help="public key (root of trust)",
    )
    av.add_argument(
        "--remote",
        metavar="NAME|SSH-ADDRESS",
        help="read the anchors from this remote (a remote of --repo, or git@host:owner/repo.git); "
        "the deploy key comes from NURAMESH_ANCHOR_SSH_KEY. A rewritten remote is tampering",
    )
    av.add_argument(
        "--tail-valid-until",
        metavar="COMMIT",
        help="full commit id bounding the chain tail; required with more than one --pubkey",
    )
    av.add_argument("--expect-tip", metavar="COMMIT", help="pin a first run to a known commit id")
    av.add_argument("--clone-dir", metavar="DIR", help="the verifier's own clone (default: cache)")
    av.add_argument("--anchor-ref", default="ledger-anchors", help="the anchor branch to read")
    av.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    av.add_argument("--require-anchors", action="store_true", help="no anchors is a failure")
    av.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    an.set_defaults(func=cmd_anchor)
    it = sub.add_parser("init", help="create a team folder from a template")
    it.add_argument("dir")
    it.add_argument("--template", choices=sorted(TEMPLATES), default="dev-team")
    it.set_defaults(func=cmd_init)

    ev = sub.add_parser("eval", help="run eval suites and check eligibility")
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)
    er = ev_sub.add_parser("run", help="answer a suite with an agent's model and record the result")
    add_team(er)
    er.add_argument("--suite", required=True)
    er.add_argument("--agent", required=True)
    er.add_argument("--fake", action="store_true")
    er.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    es = ev_sub.add_parser("status", help="is an agent eligible for work?")
    add_team(es)
    es.add_argument("--agent", required=True)
    es.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    ev.set_defaults(func=cmd_eval)

    from nuramesh.cli_calibrate import add_parsers as add_calibrate_parsers

    add_calibrate_parsers(sub)
    cli_models.register(sub)

    ledger = sub.add_parser("ledger", help="the ledger: check its hash chains, export it")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    vl = ledger_sub.add_parser("verify", help="check every ledger stream's hash chain")
    add_team(vl)
    vl.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    vl.add_argument(
        "--allow-empty",
        action="store_true",
        help="exit 0 when the ledger has no streams (default: an empty ledger fails)",
    )
    vl.add_argument("--anchors", metavar="REPO", help="anchor repository to check against")
    vl.add_argument(
        "--require-keyed-hashes",
        action="store_true",
        help=(
            "exit 1 (not done) if any row holds a plain (legacy) content hash "
            "or a keyed row has no hash_org"
        ),
    )
    vl.add_argument(
        "--pubkey",
        metavar="FILE",
        action="append",
        help="public key that signed the anchors (the root first; repeat for a lost-key rotation)",
    )
    vl.add_argument(
        "--tail-valid-until",
        metavar="COMMIT",
        help="the commit that ends the lost key's anchors (required with a second --pubkey)",
    )
    vl.add_argument(
        "--anchor-ref",
        default="ledger-anchors",
        help="the anchor branch to read (default ledger-anchors; origin/ledger-anchors works)",
    )
    vl.add_argument(
        "--require-anchors",
        action="store_true",
        help="a stream with no anchors is a failure, not a warning",
    )
    vl.add_argument(
        "--expected-head",
        action="append",
        metavar="STREAM=HASH",
        help="a head hash kept elsewhere; the stream must end exactly there",
    )
    vl.set_defaults(func=cmd_verify_ledger)

    xl = ledger_sub.add_parser(
        "export",
        help="write the ledger as JSON Lines for verifier/nuramesh_verify.py",
    )
    add_team(xl)
    xl.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    xl.add_argument("--out", required=True, metavar="FILE", help="a new file (never overwritten)")
    xl.set_defaults(func=cmd_export_ledger)

    for install_commands in plugins.cli_installers:
        install_commands(sub)

    from nuramesh.cli_completion import add_parsers as add_completion_parsers

    add_completion_parsers(sub)
    from nuramesh import __version__

    p.add_argument("--version", action="version", version=f"nuramesh {__version__}")
    finish_parser(p)
    return p


def _close_opened(opened: list[Any]) -> None:
    """Close the stores a command opened, newest first. A close that fails is reported, never
    allowed to change the command's exit code or to skip the stores still to close."""
    for store in reversed(opened):
        close = getattr(store, "close", None)  # the Store protocol has no close; stubs lack it
        if not callable(close):
            continue
        try:
            close()  # twice is harmless: a command may have closed its own
        except Exception as exc:  # noqa: BLE001
            # the type only: a database error text can name the host or the URL
            print(f"warning: could not close a store ({type(exc).__name__})", file=sys.stderr)


def open_commands() -> set[str]:
    """The names of the commands the open parser has (no plugin installed)."""
    for action in parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _hoist_globals(typed: list[str]) -> tuple[list[str], list[str]]:
    """`(argv with the global flags typed before the command moved after it, the flags)`: the
    parser takes them on each command, the person may type them first (section 3.4)."""
    from nuramesh.cli_aliases import _split_globals

    parts = _split_globals(typed)
    if parts is None:
        return typed, []
    head, rest = parts
    return ([*rest, *head] if rest else typed), head


def _bare_args(head: list[str]) -> argparse.Namespace | None:
    """The flags of a bare `nuramesh` (`--team`, `--output`, `--org`, `--yes`, `--dry-run`)."""
    p = argparse.ArgumentParser(prog="nuramesh", add_help=False)
    p.add_argument("--team")
    p.add_argument("--store")
    p.add_argument("--org")
    p.add_argument("--output", choices=("table", "json"))
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    try:
        found, extra = p.parse_known_args(head)
    except SystemExit:
        return None
    return found if not extra else None


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the `nuramesh` command."""
    # Plugins load for `serve` and for a command the open parser does not have (a plugin's), never
    # for an open command: a broken plugin must not stop erasure, shred or anything else (T161).
    from nuramesh.cli_aliases import _split_globals

    given = list(sys.argv[1:] if argv is None else argv)
    split = _split_globals(given)  # skip the global flags and their values (`--team DIR`)
    words = [w for w in (split[1] if split else given) if not w.startswith("-")]
    plugin_context: PluginContext | None = None
    if not words or words[0] not in open_commands() - {"serve"}:
        try:
            plugin_context = load_plugins()
        except PluginError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return USAGE
        if plugin_context.licence.notice is not None:  # a refused licence file is loud, never fatal
            print(plugin_context.licence.notice, file=sys.stderr)
    root = parser(plugin_context)
    typed = list(sys.argv[1:] if argv is None else argv)
    _TYPED[:] = typed
    typed, notice = rewrite(root, typed)
    if notice is not None:
        print(notice, file=sys.stderr)
    typed = normalize_serve_auth(typed)
    typed, head = _hoist_globals(typed)
    if typed == head and "-h" not in typed and "--help" not in typed and "--version" not in typed:
        args = root.parse_args([])  # only global flags (or nothing) were typed
        if not hasattr(args, "func"):
            bare_args = _bare_args(head)
            if bare_args is None:
                root.error("unrecognized arguments: " + " ".join(head))
            from nuramesh.cli_everyday import bare

            return _run_command(bare, bare_args, notice)
        typed = []
    args = root.parse_args(typed)
    args.plugin_context = plugin_context
    if getattr(args, "json", False):
        from nuramesh import cli_aliases

        print(f"`--json` was renamed to `--output json`; {cli_aliases.SUNSET}", file=sys.stderr)
    stop = apply_rules(args, " ".join(command_words(root, typed)))
    if stop is not None:
        return stop
    return _run_command(args.func, args, notice, typed=typed, root=root)


def _run_command(
    func: Callable[[argparse.Namespace], int],
    args: argparse.Namespace,
    notice: str | None,
    *,
    typed: list[str] | None = None,
    root: argparse.ArgumentParser | None = None,
) -> int:
    opened: list[Any] = []
    token = _OPENED_STORES.set(opened)
    busy = _BUSY.set(False)
    try:
        return _run_guarded(func, args, notice, token, opened, typed)
    finally:
        _BUSY.reset(busy)  # also on Ctrl-C and any raise


def _run_guarded(
    func: Callable[[argparse.Namespace], int],
    args: argparse.Namespace,
    notice: str | None,
    token: Any,
    opened: list[Any],
    typed: list[str] | None,
) -> int:
    try:
        code: int = func(args)
    except ShippedListInvalid as exc:  # fails closed: never run with no shipped rows (exit 3)
        print(str(exc), file=sys.stderr)
        code = REFUSED
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = USAGE
        _BUSY.set(False)  # a bad request is not a busy store, whatever was marked before
    except StoreError as exc:  # e.g. NURAMESH_REQUIRE_DATABASE=1 with no URL: never a traceback
        print(f"error: {exc}", file=sys.stderr)
        code = _open_failed_code(exc)
    except KeyboardInterrupt:  # Ctrl-C at a question or anywhere: nothing more is written (P8)
        print(file=sys.stderr)
        code = INTERRUPTED
    except BaseException:
        # An exception is leaving the command: run workers may still be writing (the scheduler
        # shuts its pool down without waiting), so their store stays open; the process is ending.
        _OPENED_STORES.reset(token)
        raise
    if code == USAGE and _BUSY.get():
        code = UNAVAILABLE  # a busy database: unavailable, retry may work (OD-12)
    _OPENED_STORES.reset(token)
    _close_opened(opened)
    if code == UNAVAILABLE and notice is not None:
        from nuramesh.cli_aliases import legacy_exit

        code = legacy_exit(typed or [])  # an old name exits as it did (OD-13)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
