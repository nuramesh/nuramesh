"""The request to start a run through the API (R1.20 PR B, ND347 section 4).

The API process stores a **start request** and answers 202; a runner does the checks that need
the team's code. This module is the part that needs no team code: the strict body, the static
shape checks, the run id derivation (MA2) and the plan in content-id form.

Plan text (titles, definitions of done) is customer-shaped data. It never enters the ledger or the
tasks table: the caller stores each text in the content store (kind `plan_text`, one row per run
and text, so two runs never share a row or an expiry) and this module only sees content ids.

stdlib and pydantic only.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from nuramesh.env_alias import read_renamed
from nuramesh.env_bool import EnvBoolError, env_bool

MAX_TASKS: Final = 50
"""Most tasks in one API plan."""
MAX_TITLE: Final = 200
MAX_DOD: Final = 4000
MAX_BODY_BYTES: Final = 64 * 1024

_REF: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ROLE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TEMPLATE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
ROUTE_NAME: Final = "POST /v1/runs"


class PlanProblem(ValueError):
    """The plan's shape is wrong. `code` is fixed; nothing the client sent is in the message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DataClass(StrEnum):
    SYNTHETIC = "synthetic"
    CUSTOMER = "customer"


class PlanItem(BaseModel):
    """One task of an API plan. Ids are assigned by the server (`<run_id>-t<n>`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(max_length=32)
    role: str = Field(max_length=64)
    depends_on: tuple[str, ...] = Field(default=(), max_length=MAX_TASKS)
    template: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=MAX_TITLE)
    definition_of_done: str | None = Field(default=None, max_length=MAX_DOD)


class StartRunBody(BaseModel):
    """`POST /v1/runs`. `data_class` has no default (unknown means customer, ND45)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: tuple[PlanItem, ...] = Field(min_length=1, max_length=10_000)
    data_class: Literal["synthetic", "customer"]


def validate_shape(tasks: Sequence[PlanItem]) -> None:
    """The checks that need no team code: counts, patterns, unique refs, known dependencies, no
    cycle, text or template. Raises `PlanProblem` with a fixed code (`plan_too_many_tasks`,
    `plan_cycle`, `plan_invalid`)."""
    if len(tasks) > MAX_TASKS:
        raise PlanProblem("plan_too_many_tasks")
    refs = [t.ref for t in tasks]
    if len(set(refs)) != len(refs):
        raise PlanProblem("plan_invalid")
    for item in tasks:
        if not _REF.fullmatch(item.ref) or not _ROLE.fullmatch(item.role):
            raise PlanProblem("plan_invalid")
        if len(set(item.depends_on)) != len(item.depends_on) or item.ref in item.depends_on:
            raise PlanProblem("plan_cycle" if item.ref in item.depends_on else "plan_invalid")
        if any(d not in refs for d in item.depends_on):
            raise PlanProblem("plan_invalid")
        inline = item.title is not None or item.definition_of_done is not None
        if item.template is not None:
            if inline or not _TEMPLATE.fullmatch(item.template):
                raise PlanProblem("plan_invalid")
        elif not (item.title and item.title.strip() and item.definition_of_done):
            raise PlanProblem("plan_invalid")
        elif not item.definition_of_done.strip():
            raise PlanProblem("plan_invalid")
    # Kahn: a cycle leaves tasks that never become ready
    waiting = {t.ref: set(t.depends_on) for t in tasks}
    while waiting:
        ready = [r for r, deps in waiting.items() if not deps]
        if not ready:
            raise PlanProblem("plan_cycle")
        for r in ready:
            del waiting[r]
        for deps in waiting.values():
            deps.difference_update(ready)


def derive_run_id(org_id: str, actor_id: str, key_hash: str | None) -> str:
    """The run id of a start (MA2). With an idempotency key it is a function of the whole scope
    (organization, actor, route, key hash), so another user sending the same key gets another run
    and never learns this one's id; without a key it is random and a retry is a new run."""
    if key_hash is None:
        return "run-" + uuid.uuid4().hex
    material = b"\x00".join(part.encode() for part in (org_id, actor_id, ROUTE_NAME, key_hash))
    return "run-" + hashlib.sha256(material).hexdigest()[:32]


def task_id(run_id: str, index: int) -> str:
    """The server-assigned id of the `index`th task (from 1): never client-chosen."""
    return f"{run_id}-t{index}"


def plan_text_envelope(run_id: str, field: str, text: str) -> str:
    """What is stored for one plan text. The run id is inside the stored text, so its keyed
    content id differs per run (RET-PT): two runs with the same title own two rows."""
    return json.dumps({"run": run_id, "field": field, "text": text}, separators=(",", ":"))


def read_plan_text(stored: str, run_id: str) -> str | None:
    """The text out of an envelope, or None if it is not this run's."""
    try:
        data = json.loads(stored)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("run") == run_id and isinstance(data.get("text"), str):
        return str(data["text"])
    return None


class RunStartSettingsError(ValueError):
    """A run-start setting is out of range (the CLI turns it into exit code 2)."""


@dataclass(frozen=True)
class RunStartSettings:
    """The limits of the start route (ND347 4.5 step 6), from the environment."""

    rate_per_minute: int = 60
    """`NURAMESH_RUN_START_RATE`: starts a minute for one organization."""
    max_active: int = 50
    """`NURAMESH_MAX_ACTIVE_RUNS`: unfinished runs plus pending requests for one organization."""
    ttl: timedelta = timedelta(hours=1)
    """`NURAMESH_RUN_START_TTL_SECONDS`: how long a request may wait for a runner."""
    customer_data_enabled: bool = False
    """`NURAMESH_CUSTOMER_DATA_ENABLED=1`: flipped by the owner after W.03 (ND52, ND77)."""

    def __post_init__(self) -> None:
        if self.rate_per_minute < 1 or self.max_active < 1 or self.ttl <= timedelta(0):
            raise RunStartSettingsError("run start limits must be above 0")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "RunStartSettings":
        def number(name: str, default: int) -> int:
            raw = environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                raise RunStartSettingsError(f"{name} must be a whole number") from None

        ttl_name, ttl_raw = read_renamed(
            environ, "NURAMESH_RUN_START_TTL_SECONDS", "NURAMESH_RUN_START_TTL"
        ) or ("NURAMESH_RUN_START_TTL_SECONDS", "")
        ttl = ttl_raw.strip()
        try:
            ttl_value = timedelta(seconds=float(ttl)) if ttl else timedelta(hours=1)
        except (ValueError, OverflowError):
            raise RunStartSettingsError(f"{ttl_name} must be seconds") from None
        try:
            customer_data = env_bool("NURAMESH_CUSTOMER_DATA_ENABLED", environ)
        except EnvBoolError as exc:
            raise RunStartSettingsError(str(exc)) from None
        return cls(
            rate_per_minute=number("NURAMESH_RUN_START_RATE", 60),
            max_active=number("NURAMESH_MAX_ACTIVE_RUNS", 50),
            ttl=ttl_value,
            customer_data_enabled=customer_data,
        )

    @property
    def plan_text_days(self) -> int:
        """Capture expiry of plan text: capture + TTL + 7 days, in whole days, rounded up (MJ3)."""
        seconds = (self.ttl + timedelta(days=7)).total_seconds()
        return max(1, -(-int(seconds) // 86400))
