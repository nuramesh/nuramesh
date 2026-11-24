"""Count the work a call does (executed lines), so a test can bound work instead of seconds.

Rule: no wall-clock thresholds in CI tests. A speed claim is a bound on work
(lines run, calls made); the number is the same on a busy machine and on a quiet one.
"""

import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any


def lines_run(module: ModuleType, call: Callable[..., object], *args: Any) -> int:
    """How many lines of `module`'s source file run while `call(*args)` executes."""
    filename = module.__file__
    lines = 0

    def count(frame: Any, event: str, arg: Any) -> Any:
        nonlocal lines
        if event == "line" and frame.f_code.co_filename == filename:
            lines += 1
        return count

    previous = sys.gettrace()  # coverage's tracer: restore it, or the run is unmeasured
    sys.settrace(count)
    try:
        call(*args)
    finally:
        sys.settrace(previous)
    return lines
