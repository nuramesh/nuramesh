"""Admission of a claimed start request (section 4, R10 to R15).

The early answer of a runner before `begin_run`: is this plan one this runner may start at all?
`admission_code` is the pure rule (the order of the seven checks of R10; the first check that
fails decides the code, and each check is applied to the whole plan before the next is looked
at). `plan_facts` reads what the rule needs from the engine the builder made; `admit` runs both
around the existing budget check (check 6), which is only asked when checks 1 to 5 passed.

Admission is not the guard (R13): every condition is checked again where it matters (the call,
the gate). It reads the ledger and the configuration only. It never reads a key value (R11),
reserves nothing and calls no provider.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from nuramesh.core.gates import GateKind
from nuramesh.core.model_catalog import connection_key
from nuramesh.core.model_governance import Refusal, Resolution, resolve_ref
from nuramesh.env_bool import env_bool

LIGHT: Final = "light"
TEMPORAL: Final = "temporal"
SYNTHETIC: Final = "synthetic"
CUSTOMER_FLAG: Final = "NURAMESH_CUSTOMER_DATA_ENABLED"

REAL_MODEL_NOT_SUPPORTED: Final = "real_model_not_supported"
CUSTOMER_DATA_NOT_ENABLED: Final = "customer_data_not_enabled"
MODEL_NOT_AVAILABLE: Final = "model_not_available"
MODEL_KEY_UNAVAILABLE: Final = "model_key_unavailable"
MODEL_PRICE_UNKNOWN: Final = "model_price_unknown"
SANDBOX_UNAVAILABLE: Final = "sandbox_unavailable"

SANDBOX_KINDS: Final = ("service", "host", "none")

KeyCurrent = Callable[[str, str, str], bool]
"""`(org_id, key_ref, connection key) -> the key fold of that connection is current`. It reads the
ledger fold only and never calls the secret store (R11)."""


def is_real_ref(ref: str | None) -> bool:
    """A real ref: the provider part (the text before the first `/`) is not `fake` (section 2).
    No ref at all is not a model, so it is not a real one."""
    return ref is not None and ref.split("/", 1)[0] != "fake"


def customer_data_enabled(environ: Any) -> bool:
    """The runner's own flag (R10 check 2): read by `env_bool` like the server's."""
    return env_bool(CUSTOMER_FLAG, environ)


@dataclass(frozen=True)
class RealRef:
    """What admission knows about one real ref of the plan."""

    resolves: bool
    """The governed resolver gave an approved entry and a connection for the data class."""
    stored: bool = False
    """The ref's connection has auth `stored`."""
    secret_store: bool = False
    """The runner has a secret store configured."""
    key_current: bool = False
    """The key fold of the connection is `current` (asked only for a `stored` connection)."""
    price_known: bool = False
    """A listed price or a matching override exists."""


@dataclass(frozen=True)
class AdmissionFacts:
    """Everything the rule of R10 reads. `budget_code` is the result of the existing budget
    check (None: it passed); it is looked at only when checks 1 to 5 passed."""

    engine: str
    fake_only: bool
    data_class: str
    customer_enabled: bool
    real_refs: tuple[RealRef, ...]
    command_gate: bool
    sandbox_ready: bool
    budget_code: str | None = None


def precheck_code(facts: AdmissionFacts) -> str | None:
    """Checks 1 to 5 of R10 (everything before the budget check), in order."""
    real = bool(facts.real_refs)
    if real and (facts.engine == TEMPORAL or facts.fake_only):
        return REAL_MODEL_NOT_SUPPORTED
    if real and facts.data_class != SYNTHETIC and not facts.customer_enabled:
        return CUSTOMER_DATA_NOT_ENABLED  # any value but `synthetic` fails closed (ND45)
    if any(not r.resolves for r in facts.real_refs):
        return MODEL_NOT_AVAILABLE
    if any(r.stored and not (r.secret_store and r.key_current) for r in facts.real_refs):
        return MODEL_KEY_UNAVAILABLE
    if any(not r.price_known for r in facts.real_refs):
        return MODEL_PRICE_UNKNOWN
    return None


def sandbox_code(facts: AdmissionFacts) -> str | None:
    """Check 7 of R10: fake plans too (R12); skipped on the Temporal engine."""
    if facts.engine == LIGHT and facts.command_gate and not facts.sandbox_ready:
        return SANDBOX_UNAVAILABLE
    return None


def admission_code(facts: AdmissionFacts) -> str | None:
    """The refusal code of R10, or None when the plan is admitted."""
    return precheck_code(facts) or facts.budget_code or sandbox_code(facts)


@dataclass(frozen=True)
class RunnerAdmission:
    """What a runner process says about itself, once, at its start. The default is the most
    closed one: a fake-only runner with no sandbox and no secret store."""

    engine: str = LIGHT
    fake_only: bool = True
    """Started with `--fake` (R4), or the Temporal worker (R2)."""
    customer_enabled: bool = False
    """The runner's own `NURAMESH_CUSTOMER_DATA_ENABLED=1` (check 2), never the API's."""
    key_current: KeyCurrent | None = None
    """The key fold reader of the runner's secret store; None: no secret store is configured."""
    sandbox: str = "none"
    """Which sandbox the runner chose at start (R40): `service`, `host` or `none`."""
    sandbox_ready: Callable[[], bool] = lambda: False
    """R41, asked when check 7 is reached."""

    def __post_init__(self) -> None:
        if self.engine not in (LIGHT, TEMPORAL):
            raise ValueError("engine must be 'light' or 'temporal'")
        if self.sandbox not in SANDBOX_KINDS:
            raise ValueError("sandbox must be 'service', 'host' or 'none'")

    @property
    def real_capable(self) -> bool:
        """A light runner started without `--fake` (section 2)."""
        return self.engine == LIGHT and not self.fake_only


TEMPORAL_ADMISSION: Final = RunnerAdmission(engine=TEMPORAL, fake_only=True)
"""The Temporal worker: the fake model only at launch (R2, owner Q2)."""


def plan_refs(engine: Any, plan: Iterable[Any]) -> tuple[str, ...]:
    """The effective model refs of the plan, in first-seen order: every task's assignee and every
    verifier a gate of a task names (section 2)."""
    seen: dict[str, None] = {}
    for item in plan:
        task = item.task
        agents = [task.assignee] if task.assignee else []
        for gate in task.definition_of_done.gates:
            verifier = _config_of(gate).get("verifier")
            if isinstance(verifier, str):
                agents.append(verifier)
        for agent in agents:
            ref = engine.effective_ref(agent)
            if ref is not None:
                seen.setdefault(ref)
    return tuple(seen)


def has_command_gate(plan: Iterable[Any]) -> bool:
    """A gate of kind `test_command` anywhere in the plan (admission looks at gate kinds only)."""
    return any(
        gate.kind is GateKind.TEST_COMMAND
        for item in plan
        for gate in item.task.definition_of_done.gates
    )


def _config_of(gate: Any) -> dict[str, Any]:
    config = gate.config
    try:
        return dict(config)
    except (TypeError, ValueError):
        return {}


def _real_ref(
    engine: Any, org_id: str, ref: str, data_class: str, at: datetime, runner: RunnerAdmission
) -> RealRef:
    governance = getattr(engine, "governance", None)
    if governance is None:
        return RealRef(resolves=False)  # no resolver: a real ref cannot be approved (fail closed)
    try:
        state = governance.state(data_class, at)
        found = resolve_ref(state, ref, check_price=False)
    except Exception:
        return RealRef(resolves=False)  # the resolver refuses it, for any reason
    if isinstance(found, Refusal):
        return RealRef(resolves=False)
    assert isinstance(found, Resolution)
    stored = found.key_ref is not None
    current = False
    if stored and runner.key_current is not None:
        try:
            current = bool(
                runner.key_current(
                    org_id, found.key_ref or "", connection_key(found.provider, found.endpoint_id)
                )
            )
        except Exception:
            current = False  # an unreadable fold is not `current`
    try:
        priced = not isinstance(resolve_ref(state, ref, check_price=True), Refusal)
    except Exception:
        priced = False
    return RealRef(
        resolves=True,
        stored=stored,
        secret_store=runner.key_current is not None,
        key_current=current,
        price_known=priced,
    )


def plan_facts(
    engine: Any,
    org_id: str,
    plan: Sequence[Any],
    *,
    data_class: str,
    runner: RunnerAdmission,
    at: datetime,
) -> AdmissionFacts:
    """Read the facts of R10 for a checked plan. A real ref is resolved only on a runner that
    could run it (check 1 refuses the others before any resolver read is needed)."""
    real = tuple(r for r in plan_refs(engine, plan) if is_real_ref(r))
    ask = runner.real_capable
    return AdmissionFacts(
        engine=runner.engine,
        fake_only=runner.fake_only,
        data_class=data_class,
        customer_enabled=runner.customer_enabled,
        real_refs=tuple(
            _real_ref(engine, org_id, ref, data_class, at, runner)
            if ask
            else RealRef(resolves=False)
            for ref in real
        ),
        command_gate=has_command_gate(plan),
        sandbox_ready=False,  # asked by `admit` only when check 7 is reached
    )


def admit(
    engine: Any,
    org_id: str,
    plan: Sequence[Any],
    *,
    data_class: str,
    runner: RunnerAdmission,
    at: datetime,
    budget_check: Callable[[], str | None],
) -> str | None:
    """Apply R10 to a checked plan: the refusal code, or None. `budget_check` is the existing
    check per planned task (check 6): it runs only after checks 1 to 5 passed, and the sandbox is
    asked (check 7) only after the budget check passed."""
    facts = plan_facts(engine, org_id, plan, data_class=data_class, runner=runner, at=at)
    code = precheck_code(facts) or budget_check()
    if code is not None:
        return code
    if facts.engine != LIGHT or not facts.command_gate:
        return None
    try:
        ready = bool(runner.sandbox_ready())
    except Exception:
        ready = False  # a probe that fails is "not ready"
    return None if ready else SANDBOX_UNAVAILABLE


def key_fold_current(fold: Any, key_ref: str, connection: str, now: datetime) -> bool:
    """Whether the key fold says the stored key of `connection` is `current` now (check 4): the
    same questions the reader asks at a pin, on the fold alone. Never a store call (R11)."""
    key = fold.key(key_ref)
    if fold.erasure_last is not None or key is None or not key.versions:
        return False
    if not connection or key.connection != connection:
        return False
    version, problem = fold.resolve(key_ref, now)
    if problem is not None or version is None:
        return False
    if version in key.destroyed_versions or version not in key.versions:
        return False
    return not key.versions[version].destroyed
