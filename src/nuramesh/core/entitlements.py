"""Entitlements (ND72): the open core defines the question, never the answer.

Commercial code asks "is this customer entitled to feature X?" through `require`. Open code never
asks (a test enforces it), so no open feature can be gated by accident. The default grants
nothing. There is no price, plan, customer, payment or network call here, and no system clock: a
signed licence file is verified offline (`nuramesh.core.licence`, ND389) and becomes a
`LicenceEntitlements` whose clock is injected; the open core knows the protocol and the enterprise
key names. Cloud key names live in the cloud package and arrive through the plugin seam.
"""

import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Protocol

from nuramesh.core.licence import EXPIRY_GRACE, NOT_BEFORE_SKEW

ENTERPRISE_KEYS: frozenset[str] = frozenset(
    {
        "sso.oidc",
        "sso.saml",
        "rbac.scoped",
        "audit.export_stream",
        "audit.retention_policy",
    }
)

_KEY_SHAPE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class EntitlementRequired(Exception):
    """A commercial action was refused because the feature is not granted (HTTP 403, not 402:
    no payment is implied). Problem code `entitlement_required`."""

    code = "entitlement_required"

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"the feature '{key}' is not enabled")


class Entitlements(Protocol):
    """Answers whether a feature key is granted."""

    def is_granted(self, key: str) -> bool:
        """True only if `key` is granted. An unknown key raises ValueError."""
        ...


def _check(key: str, known: frozenset[str]) -> None:
    if not _KEY_SHAPE.match(key):
        raise ValueError(f"not an entitlement key: {key!r}")
    if key not in known:
        raise ValueError(f"unknown entitlement key: {key!r}")


def _foreign(key: str, known: frozenset[str]) -> bool:
    """A well-formed `cloud.` key asked of a source that knows no `cloud.*` key at all: the
    hosted layer's key, asked of a source built without the cloud names. Asking is answered "not
    granted" (fail closed). A source that does know cloud keys, or a typo in any other namespace,
    still raises."""
    return (
        bool(_KEY_SHAPE.match(key))
        and key.startswith("cloud.")
        and key not in known
        and not any(k.startswith("cloud.") for k in known)
    )


class NoEntitlements:
    """The default everywhere: grants nothing."""

    def __init__(self, known: Iterable[str] = ENTERPRISE_KEYS) -> None:
        self._known = frozenset(known)

    def is_granted(self, key: str) -> bool:
        if _foreign(key, self._known):
            return False
        _check(key, self._known)
        return False


class FixedEntitlements:
    """An explicit, immutable set of granted keys (tests, local demos, and a verified licence)."""

    def __init__(self, keys: Iterable[str], known: Iterable[str] = ENTERPRISE_KEYS) -> None:
        self._known = frozenset(known)
        granted = frozenset(keys)
        for key in granted:
            _check(key, self._known)
        self._granted = granted

    def is_granted(self, key: str) -> bool:
        if _foreign(key, self._known):
            return False
        _check(key, self._known)
        return key in self._granted


def _aware(value: datetime, what: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be an aware datetime")


class LicenceEntitlements:
    """The keys of a verified licence file, granted only inside its time window, re-checked
    against the injected clock on every call.

    `features` outside `known` are ignored and listed in `ignored` (a newer licence must not break
    an older package); a badly shaped feature is a `ValueError`. The window is
    `not_before - NOT_BEFORE_SKEW <= clock() < expires_at + EXPIRY_GRACE`."""

    def __init__(
        self,
        features: Iterable[str],
        not_before: datetime,
        expires_at: datetime,
        clock: Callable[[], datetime],
        known: Iterable[str] = ENTERPRISE_KEYS,
    ) -> None:
        self._known = frozenset(known)
        wanted = frozenset(features)
        for key in wanted:
            if not _KEY_SHAPE.match(key):
                raise ValueError(f"not an entitlement key: {key!r}")
        _aware(not_before, "not_before")
        _aware(expires_at, "expires_at")
        self._granted = wanted & self._known
        self._ignored = wanted - self._known
        self._not_before = not_before
        self._expires_at = expires_at
        self._clock = clock

    @property
    def granted(self) -> frozenset[str]:
        """The licence's features this process knows (granted while the window holds)."""
        return self._granted

    @property
    def ignored(self) -> frozenset[str]:
        """The licence's features this process does not know (never granted, never an error)."""
        return self._ignored

    def _open_now(self) -> bool:
        now = self._clock()
        _aware(now, "the clock's value")
        try:
            return self._not_before - NOT_BEFORE_SKEW <= now < self._expires_at + EXPIRY_GRACE
        except OverflowError:  # a time at the edge of the calendar: fail closed
            return False

    def is_granted(self, key: str) -> bool:
        if _foreign(key, self._known):
            return False
        _check(key, self._known)
        return key in self._granted and self._open_now()


def require(entitlements: Entitlements, key: str) -> None:
    """Raise `EntitlementRequired` unless `key` is granted. Called by commercial code only."""
    if entitlements.is_granted(key) is not True:  # anything but a real True is a refusal
        raise EntitlementRequired(key)
