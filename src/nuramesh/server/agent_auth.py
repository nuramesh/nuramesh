"""Authenticating an agent credential: the `nma_` path of `Authorizer` (E2, spec section 3).

The order is the spec's: shape, lookup (organization and team), active, agent present with the
same fingerprint (every failure the same 401 and one counted failed presentation), then the
per-credential cap of valid calls. Failures are counted in a limiter of their own (30 in 60
seconds per client, 429) that answers failed presentations only, so guesses can never lock a
valid agent out; the cap is a sliding window kept in this process's memory.
"""

import threading
from collections.abc import Callable

from nuramesh.core.auth import AGENT_FAILURES_PER_MINUTE, AgentCredential, CallCap, address_key
from nuramesh.server.agent_credentials import AgentCredentialService
from nuramesh.server.limits import RateLimiter
from nuramesh.server.sessions import AuthProblem

INVALID = "the credential is unknown, expired or revoked"
CAPPED = "too many calls with this credential; try again later"


def invalid_credential() -> AuthProblem:
    """The one answer for every failure of steps 1 to 5 of spec section 3 (and the recheck)."""
    return AuthProblem(
        401,
        "token_invalid",
        INVALID,
        {"WWW-Authenticate": 'Bearer realm="nuramesh", error="invalid_token"'},
    )


class AgentAuth:
    """The agent credential service plus the two limits of section 3."""

    def __init__(
        self,
        service: AgentCredentialService,
        calls_per_minute: int,
        monotonic: Callable[[], float],
    ) -> None:
        self.service = service
        self.failures = RateLimiter(AGENT_FAILURES_PER_MINUTE, 60.0, monotonic)
        self._cap = CallCap(calls_per_minute)
        self._lock = threading.Lock()

    def authenticate(self, presented: str, client: str) -> AgentCredential:
        """The credential behind a presented `nma_` secret, counted against its cap; else
        `AuthProblem` 401 (same for every failure) or 429."""
        found = self.service.lookup(presented)
        if found is None:
            wait = self.failures.allow(address_key(client))
            if wait > 0:
                raise AuthProblem(
                    429,
                    "rate_limited",
                    "too many failed credential attempts; slow down",
                    {"Retry-After": str(max(1, int(wait + 0.999)))},
                )
            raise invalid_credential()
        with self._lock:
            retry = self._cap.admit(found.id, self.service.now())
        if retry is not None:
            raise AuthProblem(429, "rate_limited", CAPPED, {"Retry-After": str(retry)})
        return found
