"""Team folders: a configuration plus the code that does the work (R1.17, R1.28).

A team folder holds `nuramesh.yaml` (or `.yml` / `.json`: the org, policies, budgets, prices,
canon and evals, checked by `core.config.parse_config`) and `team.py`, which defines

    def setup(env: TeamEnv) -> Team: ...

`setup` returns the run's plan (tasks from the Chief of Staff), a worker per agent, the budgets
that belong to this run, optional gate runners, and the model provider to use with `--fake`.
`team.py` is code you run, like any script: only run team folders you trust.

Real model providers aren't wired in yet: a run without `--fake` is refused with a clear message,
because it would need an API key and would spend money (owner approval first, R1.D).
"""

import importlib.util
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from nuramesh.config import load_config_file
from nuramesh.config.shipped_prices import ShippedPriceError, load_shipped
from nuramesh.core.budgets import Budget
from nuramesh.core.config import NuraMeshConfig, parse_config
from nuramesh.core.gates import GateKind
from nuramesh.core.models import ModelProvider
from nuramesh.engine.run import Approver, GateRunner, PlannedTask, Planner, Worker
from nuramesh.stores.base import Store

CONFIG_NAMES = ("nuramesh.yaml", "nuramesh.yml", "nuramesh.json")


class ShippedListInvalid(Exception):
    """The shipped price list is missing or invalid. Not a `TeamError` or `ValueError` on purpose:
    the CLI maps it to exit 3 for every command (spec default-prices 3), never to a usage error."""


class TeamError(ValueError):
    """A team folder can't be used; the message says why."""


@dataclass
class TeamEnv:
    """What `setup` gets: where the team lives, its checked config, the store and the run."""

    root: Path
    config: NuraMeshConfig
    store: Store
    run_id: str
    fake: bool
    sandbox: Callable[[], Any]
    """Opens the run's sandbox when called (refuses without a container runtime; see
    `engine.sandbox.open_sandbox`). Only teams with test-command gates call it."""


@dataclass
class Team:
    """What a team's `setup` returns."""

    plan: Sequence[PlannedTask] | Planner
    workers: Mapping[str, Worker]
    fake_provider: ModelProvider | None = None
    run_budgets: Sequence[Budget] = ()
    gate_runners: Mapping[GateKind, GateRunner] = field(default_factory=dict)
    approver: Approver | None = None


def find_config(root: Path) -> Path:
    """The team's configuration file."""
    for name in CONFIG_NAMES:
        path = root / name
        if path.is_file():
            return path
    raise TeamError(f"{root} has no {' / '.join(CONFIG_NAMES)}")


def load_team_config(root: Path, name: str | None = None) -> NuraMeshConfig:
    """Read and check the team's configuration (`name`: another file in the team folder, such as
    `real-azure-verifier.yaml`; a plain file name, never a path)."""
    if name is None:
        return _parse_with_shipped(load_config_file(find_config(root)))
    if Path(name).name != name or not name.endswith((".yaml", ".yml", ".json")):
        raise TeamError("--config is a file name in the team folder (.yaml, .yml or .json)")
    path = root / name
    if not path.is_file() or path.is_symlink():
        raise TeamError(f"{root} has no regular file {name}")
    return _parse_with_shipped(load_config_file(path))


def _parse_with_shipped(data: Any) -> NuraMeshConfig:
    """`parse_config` that knows the shipped default prices: a model that has one needs no price
    of the organization's own to be approved (spec default-prices 5.4). Fails closed."""
    try:
        rows = load_shipped().prices
    except ShippedPriceError as exc:
        raise ShippedListInvalid(f"shipped price list invalid: {exc}") from None
    return parse_config(data, shipped=rows)


def with_shipped_prices(config: NuraMeshConfig) -> NuraMeshConfig:
    """`config` whose price book also holds the shipped default prices (spec default-prices 5).
    An invalid or missing shipped file fails closed: the caller never runs with no shipped rows."""
    try:
        book = load_shipped().apply(config.prices)
    except ShippedPriceError as exc:
        raise ShippedListInvalid(f"shipped price list invalid: {exc}") from None
    return config.model_copy(update={"prices": book})


def load_team_module(root: Path) -> ModuleType:
    """Import the folder's `team.py` (it runs its code)."""
    path = root / "team.py"
    if not path.is_file():
        raise TeamError(f"{root} has no team.py")
    name = f"nuramesh_team_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise TeamError(f"{path} can't be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "setup", None)):
        raise TeamError(f"{path} doesn't define setup(env)")
    return module


def setup_team(module: ModuleType, env: TeamEnv) -> Team:
    """Call the team's `setup` and check what it returns."""
    team = module.setup(env)
    if not isinstance(team, Team):
        raise TeamError("setup(env) must return a nuramesh.team.Team")
    return team
