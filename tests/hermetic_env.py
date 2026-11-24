"""Shared helper (#1110): a test subprocess environment without the push/prepush skip switches."""

import os

# Local skip switches for the push scripts: a parent shell that exports them must not
# change what a test sees. Every subprocess a test spawns gets an environment without them.
SKIP_ENV_VARS = (
    "PREPUSH_SKIP_FOR_TESTS",
    "PREPUSH_SKIP_APPS_FOR_TESTS",
    "PUSH_TESTS_IN_SMALL_LANE",
)


def hermetic_env() -> dict[str, str]:
    """The parent environment minus the prepush/push skip switches."""
    return {k: v for k, v in os.environ.items() if k not in SKIP_ENV_VARS}
