"""Shared helpers of the `engine/run.py` mutation-gap files (#1264)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.ledger import EventType
from nuramesh.core.tasks import Task, TextRef

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


def ev(etype: EventType, age: timedelta, **payload: Any) -> Any:
    return SimpleNamespace(type=etype, at=NOW - age, payload=payload)


def text_task(*, with_text: bool = True) -> Task:
    spec = GateSpec(id="deploy", kind=GateKind.HUMAN, checks=("ok",), config={})
    return Task(
        id="t1",
        run_id="r1",
        title="Plan text",
        definition_of_done=DefinitionOfDone(
            criteria=(Criterion(id="ok", text="A human approves"),), gates=(spec,)
        ),
        assignee="ada",
        text_ref=TextRef(title_id="title-1", dod_id="dod-1") if with_text else None,
    )
