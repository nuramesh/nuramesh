"""Network actions for adapters (#1119): a tool call URL becomes authorize(NETWORK, host[:port]).

Every adapter that lets an agent's tool reach the network calls `authorize_network(ctx, url)`
before the call, so the policy decides on a bare destination and the decision is recorded. The
helper never guesses: a value that cannot be reduced to one canonical `host[:port]` is refused
(fail closed) with the engine's `network_target_not_canonical` denial. The input text (it may carry
userinfo or a token in the query) is never put in an error, a log or the ledger.

Reduction rules (`network_target`):

- scheme: `http`, `https`, `ws`, `wss` (any case); anything else (`file:`, `ftp:`, `javascript:`,
  `https:host`) is refused. A scheme-less value is accepted only as a bare `host:port` (a tool
  argument that is a host name and port); a relative or scheme-less path, `//host` or `?query`
  is refused.
- userinfo (everything up to the last `@` in the authority), path, query and fragment are dropped.
- the host is lower-cased, one trailing dot is removed, an IPv6 literal is written in the
  policy's compressed form. A percent-encoded, non-ASCII (no IDNA: the policy takes ASCII only),
  empty, numeric-spelling (`167772165`, `0x0a000005`) or IPv4-embedding IPv6 host is refused.
- the port is always explicit: the one in the URL, else the scheme's default (`http`/`ws` 80,
  `https`/`wss` 443), so `https://h` and `https://h:443` are both `h:443`. A deny rule by port
  (`h:443`, `*:443`, `h:*`) therefore hits, and a host-only deny rule hits every port (core
  `PolicyRule.matches`). An allow rule written host-only does not match `h:443` and becomes an
  ask: the safe direction. Write allow rules with the port. A bare host with no scheme and no port
  is refused (its port would be a guess).
- redirects: the helper checks only the URL it is given. Adapters must re-authorize every
  redirect hop (each hop is a new target) or turn redirects off.
- whitespace, control characters and backslashes anywhere in the value are refused (parsers
  disagree about them), as is a value over 2048 characters.
- `localhost` (and the /etc/hosts aliases `ip6-localhost`, `ip6-loopback`,
  `localhost.localdomain`, `localhost6`), any `*.localhost` name and unspecified addresses
  (`0.0.0.0/8`, `[::]`) are refused:
  they are other spellings of the loopback, a deny for `127.0.0.1` or `[::1]` would not cover
  them, and a name cannot be mapped to one loopback without DNS. Write the address (`127.0.0.1`,
  `[::1]`) in the tool call. Denying `127.0.0.1` and `[::1]` is NOT enough: the rest of
  `127.0.0.0/8` (`127.0.0.2` to `127.255.255.254`) reaches services bound to those addresses, so
  write the host-only deny `127.*` (plus `[::1]`).
- adapters must never call `ctx.authorize(NETWORK, ...)` directly: core still accepts a target
  such as `localhost:80`, only this helper refuses it.
- the result must pass the policy's own `is_network_destination`.
"""

import ipaddress
from collections.abc import Sequence
from typing import Protocol

from nuramesh.core.policy import ActionKind, is_network_destination
from nuramesh.engine.models import PolicyDenied

__all__ = [
    "NetworkTargetError",
    "authorize_network",
    "authorize_network_redirects",
    "network_target",
]

MAX_URL = 2048
_DEFAULT_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}
# names /etc/hosts commonly maps to the loopback
_LOOPBACK_NAMES = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "localhost.localdomain",
        "localhost6",
        "localhost4",
        "localhost4.localdomain4",
        "localhost6.localdomain6",
    }
)
# Fixed text handed to `authorize` for an unreducible value: not a destination, so the engine
# records `network_target_not_canonical` (a deny) without ever seeing the caller's text.
_UNREDUCIBLE = "<unreducible network target>"


class NetworkTargetError(ValueError):
    """A URL or host that cannot be reduced to one canonical `host[:port]`. The message is fixed:
    it never contains the input."""

    def __init__(self) -> None:
        super().__init__("the value cannot be reduced to a bare host with an optional port")


class _Authorizer(Protocol):
    def authorize(self, kind: ActionKind, target: str) -> None: ...


def _bad() -> NetworkTargetError:
    return NetworkTargetError()


def network_target(url: str) -> str:
    """Reduce a URL (or a bare host) to a bare lower-case `host[:port]`; raises
    `NetworkTargetError` when it cannot be done unambiguously."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise _bad()
    if any(ch.isspace() or ch == "\\" or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise _bad()
    scheme = ""
    if "://" in url:
        scheme, _, rest = url.partition("://")
        scheme = scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            raise _bad()
        authority = rest
        for stop in "/?#":
            authority = authority.split(stop, 1)[0]
        authority = authority.rsplit("@", 1)[-1]
    else:
        # a bare host[:port] only: no path, query, fragment, userinfo, colon-less scheme
        if any(ch in url for ch in "/?#@"):
            raise _bad()
        if url.partition(":")[0].lower() in _DEFAULT_PORTS:
            raise _bad()  # `https:443` reads as a scheme with no host, not as host `https`
        authority = url
    return _canonical(authority, scheme)


def _canonical(authority: str, scheme: str) -> str:
    if not authority or "%" in authority or not authority.isascii():
        raise _bad()
    if authority.startswith("["):
        close = authority.find("]")
        if close < 0:
            raise _bad()
        host_text, tail = authority[: close + 1], authority[close + 1 :]
        if tail and not tail.startswith(":"):
            raise _bad()
        try:
            host = f"[{ipaddress.IPv6Address(host_text[1:-1]).compressed}]"
        except ValueError:
            raise _bad() from None
        port = tail[1:] if tail else None
    else:
        host, sep, port_text = authority.partition(":")
        port = port_text if sep else None
        host = host.lower()
        if host.endswith("."):
            host = host[:-1]
    if port == "":
        raise _bad()
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
        if port is None:
            raise _bad()  # a bare host with no port: which port would it be? never guessed
    target = f"{host}:{port}"
    if not is_network_destination(target) or _is_loopback_alias(host):
        raise _bad()
    return target


def _is_loopback_alias(host: str) -> bool:
    """True for a spelling of "this machine" that a deny on `127.0.0.1` or `[::1]` would not
    cover: an unspecified address (`0.0.0.0/8`, `[::]`, which connect to the local host) and
    `localhost` or any `*.localhost` name (which the resolver may map to IPv4 or IPv6 loopback).
    The helper does no DNS, so it cannot pick one loopback for a name: it refuses (fail closed)."""
    if host.startswith("["):
        return ipaddress.IPv6Address(host[1:-1]).is_unspecified
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.IPv4Address(host).packed[0] == 0
    except ValueError:
        return False


def authorize_network(ctx: _Authorizer, url: str) -> str:
    """Check a network-reaching tool call against the role's policy before it runs: reduce `url`
    (see the module text) and call `ctx.authorize(ActionKind.NETWORK, target)`; returns the target
    (the `host:port` that was approved). Every redirect hop is a new target: an adapter must call
    this again for each hop (or not follow redirects). Raises `PolicyDenied` for a value that
    cannot be reduced (the engine records `network_target_not_canonical`; the text is never
    recorded) and whatever
    `authorize` raises (`PolicyDenied`, `ApprovalRequired`) for a policy decision."""
    try:
        target = network_target(url)
    except NetworkTargetError:
        ctx.authorize(ActionKind.NETWORK, _UNREDUCIBLE)
        raise PolicyDenied("a network target must be a bare host with an optional port") from None
    ctx.authorize(ActionKind.NETWORK, target)
    return target


def authorize_network_redirects(ctx: _Authorizer, urls: Sequence[str]) -> tuple[str, ...]:
    """Authorize every URL of a request chain (the first URL and each redirect hop) in order and
    stop at the first refusal: each hop is a new target, so a tool that follows redirects must
    call this (or `authorize_network`) for every `Location` before it connects. Returns the
    approved `host:port` of each URL. An empty chain is refused (nothing was checked). Resolve
    each `Location` against the URL that returned it (`urllib.parse.urljoin`) before passing it:
    a relative or scheme-relative `Location` is refused. Call per hop, before connecting to it."""
    if not urls:
        raise PolicyDenied("a network request has at least one URL")
    return tuple(authorize_network(ctx, url) for url in urls)
