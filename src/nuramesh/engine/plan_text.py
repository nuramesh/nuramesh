"""Reading the text of an API-started task from the content store (R1.20 PR B, ND347 4.3).

A task made from an API plan (`Task.text_ref`) holds a fixed placeholder for its title and the
text of its criteria. The engine shows the real text to the worker, the verifier and the human
gate only through `resolve_task_text`, which reads it from the content store under the org key.
If the text is gone (erased, expired, key lost) it raises `PlanTextUnavailable` and the caller
sends the task to a human with the fixed code `plan_text_unavailable`: no gate ever runs against a
placeholder, because a vacuous pass would mark work done.
"""

from typing import Final

from nuramesh.core.gates import Criterion, DefinitionOfDone
from nuramesh.core.tasks import Task
from nuramesh.engine.content import ContentService

PLAN_TEXT_UNAVAILABLE: Final = "plan_text_unavailable"
"""The fixed reason code of a task whose plan text is gone."""
PLACEHOLDER_CRITERION: Final = "(definition of done in the content store)"
CRITERION_ID: Final = "dod"
"""The one criterion of an API task; the role's gate preset says which gates check it."""


class PlanTextUnavailable(Exception):  # noqa: N818 (a signal that fails the task closed)
    """The stored text of a task is gone. Carries no text."""


def resolve_task_text(content: ContentService | None, org_id: str, task: Task) -> Task:
    """`task` with its real title and criterion text, for the worker, verifier and human gate.
    A task without a `text_ref` is returned as it is. The result is never stored."""
    ref = task.text_ref
    if ref is None:
        return task
    if content is None:
        raise PlanTextUnavailable
    if ref.template_id is not None:
        pair = content.read_template(org_id, ref.template_id)
        if pair is None:
            raise PlanTextUnavailable
        title, dod = pair
    else:
        assert ref.title_id is not None and ref.dod_id is not None
        found_title = content.read_plan_text(org_id, ref.title_id, task.run_id)
        found_dod = content.read_plan_text(org_id, ref.dod_id, task.run_id)
        if found_title is None or found_dod is None or not found_title.strip() or not found_dod:
            raise PlanTextUnavailable
        title, dod = found_title, found_dod
    done = DefinitionOfDone(
        criteria=tuple(Criterion(id=c.id, text=dod) for c in task.definition_of_done.criteria),
        gates=task.definition_of_done.gates,
    )
    return task.model_copy(update={"title": title, "definition_of_done": done})
