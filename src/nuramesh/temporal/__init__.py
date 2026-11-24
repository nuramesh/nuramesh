"""Durable runs on Temporal: the optional extra `nuramesh[temporal]` (R1.20 PR 3, ND55, T72).

This package is the **only** place that imports `temporalio`; `nuramesh.core`, the in-process
engine, the CLI and the library import nothing from it (a test proves it). Import it explicitly:
`from nuramesh.temporal import ...` fails with a clear message if the extra is not installed.

**For evaluation only:** the Temporal dev server in the repository's dev Compose file is for
evaluation only; production needs a real Temporal cluster (ND53 E).
"""

try:
    import temporalio  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised by hand
    raise ImportError(
        "nuramesh.temporal needs the optional extra: pip install 'nuramesh[temporal]'"
    ) from exc

from nuramesh.temporal.activities import EngineFactory, RunActivities
from nuramesh.temporal.client import connect, decide, handle, start_run, stop, workflow_id
from nuramesh.temporal.converter import ScrubbingFailureConverter, data_converter
from nuramesh.temporal.models import RunRef, RunResult
from nuramesh.temporal.worker import build_worker
from nuramesh.temporal.workflow import RunWorkflow

__all__ = [
    "EngineFactory",
    "RunActivities",
    "RunRef",
    "RunResult",
    "RunWorkflow",
    "ScrubbingFailureConverter",
    "build_worker",
    "connect",
    "data_converter",
    "decide",
    "handle",
    "start_run",
    "stop",
    "workflow_id",
]
