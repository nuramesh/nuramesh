"""The `RunBuilder` for runs started through the API (R1.20 PR B, ND347 4.6).

Turns a claimed start request (a plan of refs, roles, edges and content ids) into the engine and
the planned tasks. This is where a role name becomes an assignee (the org graph, from team code,
which only a runner or worker process holds), where the role's gate preset becomes the task's
gates, and where a plan that cannot be accepted is refused with a fixed code. Whether the plan's
models may run on this runner is not decided here: that is admission (`engine/admission.py`,
`spec/real-models` R10), which the begin sequence applies to the built plan.

The tasks are made in `text_ref` mode: the tasks table and the ledger hold a placeholder and the
content ids, never the plan's text (4.3). The real text is read from the content store when a
worker, verifier or human gate needs it.
"""

from collections.abc import Callable

from nuramesh.core.canonical import thaw
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateSpec
from nuramesh.core.presets import gate_preset_for_role
from nuramesh.core.tasks import PLACEHOLDER_TITLE, Task, TextRef
from nuramesh.engine.light.runner import PreparedRun, StartRefused
from nuramesh.engine.plan_text import CRITERION_ID, PLACEHOLDER_CRITERION
from nuramesh.engine.run import PlannedTask, RunEngine
from nuramesh.engine.run_request import task_id
from nuramesh.engine.starter import parse_plan
from nuramesh.stores.run_queue import StartRequestRow

EngineFor = Callable[[str, str], RunEngine]


class ApiRunBuilder:
    """Builds the engine and plan of an API start request from `engine_for(org_id, run_id)`."""

    def __init__(self, engine_for: EngineFor) -> None:
        self.engine_for = engine_for

    def build(self, request: StartRequestRow) -> PreparedRun:
        try:
            items = parse_plan(request.plan)
        except ValueError:
            raise StartRefused("plan_invalid") from None
        engine = self.engine_for(request.org_id, request.run_id)
        org = engine.org
        ids = {item.ref: task_id(request.run_id, n) for n, item in enumerate(items, start=1)}
        planned: list[PlannedTask] = []
        for item in items:
            assignee = next(
                (a.id for a in sorted(org.agents, key=lambda a: a.id) if a.role == item.role), None
            )
            if assignee is None:
                raise StartRefused("plan_unknown_role")
            preset = gate_preset_for_role(engine.presets, item.role)
            if preset is None:
                raise StartRefused("plan_gate_preset_missing")
            gates = tuple(
                GateSpec(
                    id=g.id,
                    kind=g.kind,
                    checks=(CRITERION_ID,),
                    required=g.required,
                    config=thaw(g.config),
                )
                for g in preset.gates
            )
            if item.template is not None:
                ref = TextRef(template_id=item.template)
            else:
                ref = TextRef(title_id=item.title_ref, dod_id=item.dod_ref)
            content = engine.content
            for content_id in ref.ids:  # gone before the start: refuse now, not at the gate
                if (
                    content is None
                    or (
                        content.read_template(request.org_id, content_id)
                        if item.template is not None
                        else content.read_plan_text(request.org_id, content_id, request.run_id)
                    )
                    is None
                ):
                    raise StartRefused("plan_text_unavailable")
            planned.append(
                PlannedTask(
                    Task(
                        id=ids[item.ref],
                        run_id=request.run_id,
                        title=PLACEHOLDER_TITLE,
                        definition_of_done=DefinitionOfDone(
                            criteria=(Criterion(id=CRITERION_ID, text=PLACEHOLDER_CRITERION),),
                            gates=gates,
                        ),
                        assignee=assignee,
                        text_ref=ref,
                    ),
                    depends_on=tuple(ids[d] for d in item.depends_on),
                )
            )
        return PreparedRun(engine=engine, plan=planned, data_class=request.data_class)
