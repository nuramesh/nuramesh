"""Tests set the STORE clock (ND243): production code reads time only from the unit of work."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from content_helpers import hasher_for, hashing_for

from nuramesh.engine.autonomy import record_config
from nuramesh.engine.policy import ModelPolicy, answer_approval, answer_approval_in, check_action

Now = datetime | Callable[[], datetime]


def drive(store: Any, now: Now) -> None:
    """Make the store's clock read `now` (a time, or a callable read at each unit of work)."""
    store._clock = now if callable(now) else (lambda: now)


def check_at(store: Any, *args: Any, now: Now | None = None, **kw: Any) -> Any:
    """`check_action` with the store clock set to `now` first."""
    if now is not None:
        drive(store, now)
    kw.setdefault("hasher", hasher_for(store, args[1]))  # PH-KEY: the store's own keyed hashing
    return check_action(store, *args, **kw)


def answer_at(store: Any, *args: Any, now: Now | None = None, **kw: Any) -> Any:
    """`answer_approval` with the store clock set to `now` first."""
    if now is not None:
        drive(store, now)
    kw.setdefault("hashing", hashing_for(store))
    return answer_approval(store, *args, **kw)


def answer_in_at(uow: Any, *args: Any, **kw: Any) -> Any:
    """`answer_approval_in`. The unit of work already read the store clock when it started, so
    there is no `now`: set the clock with `drive` before `store.transaction()`."""
    return answer_approval_in(uow, *args, **kw)


def model_policy_at(store: Any, *args: Any, now: Now, **kw: Any) -> ModelPolicy:
    """A `ModelPolicy` on a store whose clock is `now`."""
    drive(store, now)
    kw.setdefault("hasher", hasher_for(store, args[1]))
    return ModelPolicy(store, *args, **kw)


def record_config_at(store: Any, *args: Any, now: Now, **kw: Any) -> bool:
    """`record_config` on a store whose clock is `now`."""
    drive(store, now)
    return record_config(store, *args, **kw)
