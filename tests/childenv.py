"""The environment a test passes to a child pytest (xdist H-7).

Inside an xdist worker the environment holds `PYTEST_XDIST_*`; with pytest-cov it holds
`COV_CORE_*`; `PYTEST_ADDOPTS` and `COVERAGE_*` may be set by whoever runs the suite. A child
pytest that inherits them believes it is a worker or writes coverage data into the parent's file,
so a child gets the environment without them.
"""

import os
from collections.abc import Mapping

SCRUBBED_PREFIXES = ("PYTEST_XDIST_", "COV_CORE_", "COVERAGE_", "PYTEST_ADDOPTS")


def scrubbed_env(
    base: Mapping[str, str] | None = None, extra_prefixes: tuple[str, ...] = ()
) -> dict[str, str]:
    """`base` (default `os.environ`) without the parallel-run and coverage variables."""
    source = os.environ if base is None else base
    drop = SCRUBBED_PREFIXES + extra_prefixes
    return {k: v for k, v in source.items() if not k.startswith(drop)}
