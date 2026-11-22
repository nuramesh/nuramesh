"""The worker that runs the workflow and its activities (T72)."""

import inspect
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from nuramesh.engine.content import ContentService
from nuramesh.engine.light import RunBuilder
from nuramesh.temporal.activities import EngineFactory, RunActivities
from nuramesh.temporal.client import DEFAULT_TASK_QUEUE
from nuramesh.temporal.workflow import RunWorkflow


def build_worker(
    client: Client,
    factory: EngineFactory,
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
    max_activities: int = 16,
    builder: RunBuilder | None = None,
    content: ContentService | None = None,
) -> Worker:
    """A worker for `task_queue`. Activities are plain blocking functions (the engine is), run in
    a thread pool of `max_activities`. Restarting a worker, or starting another over the same
    store, is safe: see `activities`. The factory takes `(org_id, run_id)`; any other shape is
    refused here with a clear error, not as an opaque retried activity failure. With a `builder`
    the worker also takes start requests (`intake`): it begins the runs the API asked for."""
    try:
        inspect.signature(factory).bind("org", "run")
    except TypeError:
        raise TypeError(
            "the engine factory must take (org_id, run_id): see EngineFactory in "
            "nuramesh.temporal.activities"
        ) from None
    except ValueError:  # no signature available (a builtin): trust it
        pass
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[RunWorkflow],
        activities=RunActivities(factory, builder, content=content).all(),
        activity_executor=ThreadPoolExecutor(max_workers=max_activities),
        max_concurrent_activities=max_activities,
    )
