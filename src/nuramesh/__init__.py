"""NuraMesh: the control plane for AI agents.

The public library surface is `__all__` (the Python API guide on the docs site says what is
stable and what is not).
Everything is loaded on first use, so `import nuramesh` costs the standard library, pydantic
and `nuramesh.core` only; the HTTP client (`httpx`, the `client` extra) loads with the first
`bind(...)`.
"""

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

__all__ = [
    "ActionKind",
    "ApprovalRequired",
    "ApprovalTimeout",
    "CredentialError",
    "Decision",
    "NuraMeshError",
    "Outcome",
    "PolicyDenied",
    "RoleBinding",
    "ServerUnavailable",
    "__version__",
    "bind",
]

_API = frozenset({"Outcome", "RoleBinding", "bind"})
_POLICY = frozenset({"ActionKind", "Decision"})
_ERRORS = frozenset(
    {
        "ApprovalRequired",
        "ApprovalTimeout",
        "CredentialError",
        "NuraMeshError",
        "PolicyDenied",
        "ServerUnavailable",
    }
)

if TYPE_CHECKING:
    from nuramesh.api.binding import Outcome, RoleBinding, bind
    from nuramesh.core.policy import ActionKind, Decision
    from nuramesh.errors import (
        ApprovalRequired,
        ApprovalTimeout,
        CredentialError,
        NuraMeshError,
        PolicyDenied,
        ServerUnavailable,
    )


def __getattr__(name: str) -> Any:
    """PEP 562: a public name is imported from its module on first use (three fixed modules,
    written out: no import by a computed name)."""
    module: Any
    if name in _API:
        from nuramesh.api import binding as module
    elif name in _POLICY:
        from nuramesh.core import policy as module
    elif name in _ERRORS:
        from nuramesh import errors as module
    else:
        raise AttributeError(f"module 'nuramesh' has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
