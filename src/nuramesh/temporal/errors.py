"""Typed failure codes for activities (T72).

An exception message can quote content (a path, a model reply, a title), and Temporal writes
failures into the workflow history. So an activity never lets an exception escape: it raises an
`ApplicationError` whose message **and type** are one of these codes, with no cause and no details.
"""

from collections.abc import Callable

from temporalio.exceptions import ApplicationError

from nuramesh.engine.canon import CanonError
from nuramesh.engine.content import (
    GATE_CODES,
    KEY_DESTROYED,
    KEY_MISSING,
    TOMBSTONE_UNAVAILABLE,
    is_gate_refusal,
)
from nuramesh.engine.run import DurableRequestError

ORG_KEY_DESTROYED = KEY_DESTROYED
ORG_KEY_MISSING = KEY_MISSING
KEY_TOMBSTONE_UNAVAILABLE = TOMBSTONE_UNAVAILABLE
KEY_GATE_CODES = GATE_CODES
"""R1.37: a destroyed key, a lost key and an unreadable tombstone can never succeed on a retry."""
ENGINE_ERROR = "engine_error"
BAD_REQUEST = "bad_request"
"""Not retried: the request can't succeed (an unknown task, a run of another organization)."""
CANON_INTEGRITY = "canon_integrity"
CANON_REJECTED = "canon_rejected"
_CANON_INTEGRITY_CODES = frozenset(
    {"object_corrupt", "object_type_mismatch", "commit_malformed", "tree_malformed", "chain_limit"}
)
_CANON_REJECTED_CODES = frozenset({"object_format_unsupported", "git_too_old", "not_a_commit"})
"""R3.00: a canon that failed verification (or can never be read) is not retried; a missing object,
a timeout and the selection errors stay `engine_error` (retried)."""


def failure(code: str, *, retry: bool) -> ApplicationError:
    """An error that carries only the code."""
    return ApplicationError(code, type=code, non_retryable=not retry)


def guarded[T](call: Callable[[], T]) -> T:
    """Run `call`; turn anything it raises into a coded error (the original is dropped, message
    included). **Only** `DurableRequestError` (a request that can never succeed: an unknown task,
    another run's task) is not retried. Every other failure, `ValueError` subclasses included
    (a transient Git read of the canon, a transition race with an overlapping retry, a store that
    hiccuped), is retried; on the last try a task step puts the task in front of a human."""
    try:
        return call()
    except DurableRequestError:
        code, retry = BAD_REQUEST, False
    except Exception as exc:
        if isinstance(exc, CanonError) and exc.code in _CANON_INTEGRITY_CODES:
            raise failure(CANON_INTEGRITY, retry=False) from None
        if isinstance(exc, CanonError) and exc.code in _CANON_REJECTED_CODES:
            raise failure(CANON_REJECTED, retry=False) from None
        if is_gate_refusal(exc):  # retrying a tombstone refusal repeats the same check
            raise failure(str(getattr(exc, "code", ENGINE_ERROR)), retry=False) from None
        code, retry = ENGINE_ERROR, True
    raise failure(code, retry=retry) from None
