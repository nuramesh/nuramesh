"""The proxies whose forwarded headers are believed (R1.30 review 1, ND320 item 11).

`X-Forwarded-For` and `X-Forwarded-Proto` are believed only when the socket peer is inside the
operator's list (`NURAMESH_TRUSTED_PROXIES`, CIDRs or single addresses). Anyone else who reaches
the port directly gets the socket's own address and scheme, so forged headers cannot dodge the
rate limit or the lockout or fake https. A bare "trust the proxy" setting with no list is refused
at start; so is an empty or malformed list and `/0` (it would trust the whole internet)."""

import ipaddress
from dataclasses import dataclass
from typing import Final

ENV: Final = "NURAMESH_TRUSTED_PROXIES"

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        found = ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(found, ipaddress.IPv6Address) and found.ipv4_mapped is not None:
        return found.ipv4_mapped  # a v4 peer seen through a dual-stack socket
    return found


@dataclass(frozen=True)
class TrustedProxies:
    """A parsed list of proxy networks; false when empty (no proxy is trusted)."""

    networks: tuple[Network, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.networks)

    @classmethod
    def parse(cls, text: str) -> "TrustedProxies":
        """`10.0.0.0/8, 192.0.2.7, 2001:db8::/32`. `ValueError` (naming the setting, never the
        value) for an empty list, an entry that is not an address or CIDR, or a `/0`."""
        parts = [p.strip() for p in text.split(",")]
        if not any(parts):
            raise ValueError(f"{ENV} is empty: list the proxy addresses (CIDRs) to trust")
        networks: list[Network] = []
        for part in parts:
            try:
                network = ipaddress.ip_network(part, strict=False)
            except ValueError:
                raise ValueError(
                    f"{ENV} holds an entry that is not an IP address or CIDR (comma-separated, "
                    "for example 10.0.0.0/8,192.0.2.7)"
                ) from None
            mapped = (
                network.network_address.ipv4_mapped
                if isinstance(network, ipaddress.IPv6Network)
                else None
            )
            if mapped is not None and network.prefixlen >= 96:
                # ::ffff:10.0.0.0/104 is the v4 network 10.0.0.0/8 (a peer is matched unwrapped)
                network = ipaddress.IPv4Network(f"{mapped}/{network.prefixlen - 96}")
            if network.prefixlen == 0:
                raise ValueError(f"{ENV} must not trust every address (a /0 network)")
            networks.append(network)
        return cls(tuple(networks))

    def trusts(self, host: str | None) -> bool:
        """Is `host` (the socket peer) inside the list? Anything unparseable is not."""
        if not host:
            return False
        found = _address(host)
        return found is not None and any(
            found.version == n.version and found in n for n in self.networks
        )


def trusts_peer(trusted: "TrustedProxies | bool", peer: str | None) -> bool:
    """Whether headers from `peer` are believed. A plain `True` (library and test use; the CLI
    never builds one) trusts every peer."""
    if isinstance(trusted, TrustedProxies):
        return trusted.trusts(peer)
    return trusted
