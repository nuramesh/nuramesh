"""One classifier for IP addresses an agent's network action may reach (spec/network-guard, NG-1 to
NG-3, NG-10 (d)). Pure: stdlib `ipaddress` only, no I/O.

The class of an address comes from a fixed table of networks written below. It never asks the
stdlib `is_global`, `is_private` or any other judgement of `ipaddress` (those change between Python
patch releases). The classes in `NEVER` are refused whatever a policy says; the classes in
`POLICY_DECIDED` are decided by the policy with exact-host rules only (`core.policy.evaluate`);
`public` is connected to."""

import ipaddress
from enum import StrEnum
from typing import Final

__all__ = [
    "EMBEDDED_IPV4_NETWORKS",
    "NEVER",
    "POLICY_DECIDED",
    "TABLE",
    "AddressClass",
    "address_target",
    "classify_address",
    "has_exact_host",
    "host_of_target",
]


class AddressClass(StrEnum):
    """The class of one IP address (NG-3)."""

    PUBLIC = "public"
    LOOPBACK = "loopback"
    PRIVATE = "private"
    CGNAT = "cgnat"
    ULA = "ula"
    METADATA = "metadata"
    UNSPECIFIED = "unspecified"
    LINK_LOCAL = "link_local"
    MULTICAST = "multicast"
    RESERVED = "reserved"
    EMBEDDED_IPV4 = "embedded_ipv4"
    UNPARSEABLE = "unparseable"


NEVER: Final = frozenset(
    {
        AddressClass.METADATA,
        AddressClass.UNSPECIFIED,
        AddressClass.LINK_LOCAL,
        AddressClass.MULTICAST,
        AddressClass.RESERVED,
        AddressClass.EMBEDDED_IPV4,
        AddressClass.UNPARSEABLE,
    }
)
"""No policy rule, approval, setting or environment variable makes a connection to these happen."""

POLICY_DECIDED: Final = frozenset(
    {AddressClass.LOOPBACK, AddressClass.PRIVATE, AddressClass.CGNAT, AddressClass.ULA}
)
"""Decided by the policy, with an exact-host allow rule only (NG-10)."""

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network

_C: Final = AddressClass


def _rows(cls: AddressClass, *nets: str) -> tuple[tuple[_Net, AddressClass], ...]:
    return tuple((ipaddress.ip_network(n), cls) for n in nets)


_ALL_V6: Final = ipaddress.IPv6Network("::/0")
_GLOBAL_UNICAST_V6: Final = ipaddress.IPv6Network("2000::/3")

TABLE: Final[tuple[tuple[_Net, AddressClass], ...]] = (
    # first match wins (NG-3); the metadata list is data: one line and one test row to add one
    *_rows(
        _C.METADATA,
        "169.254.169.254/32",
        "fd00:ec2::254/128",
        "fd20:ce::254/128",
        "168.63.129.16/32",
        "100.100.100.200/32",
        "192.0.0.192/32",
    ),
    *_rows(_C.UNSPECIFIED, "0.0.0.0/8", "::/128"),
    *_rows(_C.LOOPBACK, "127.0.0.0/8", "::1/128"),
    *_rows(_C.LINK_LOCAL, "169.254.0.0/16", "fe80::/10"),
    *_rows(_C.PRIVATE, "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"),
    *_rows(_C.CGNAT, "100.64.0.0/10"),
    *_rows(_C.ULA, "fc00::/7"),
    *_rows(_C.MULTICAST, "224.0.0.0/4", "ff00::/8"),
    *_rows(
        _C.RESERVED,
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "240.0.0.0/4",
        "fec0::/10",
        "100::/64",
        "100:0:0:1::/64",
        "2001::/23",
        "2001:db8::/32",
        "3fff::/20",
        "5f00::/16",
    ),
    # every IPv6 address outside 2000::/3 that no row above names
    *((net, _C.RESERVED) for net in _ALL_V6.address_exclude(_GLOBAL_UNICAST_V6)),
)

# NG-2: IPv6 forms that carry an IPv4 address and are refused outright (`::` and `::1` are plain
# addresses and keep their rows; mapped `::ffff:0:0/96` and NAT64 `64:ff9b::/96` are unwrapped)
EMBEDDED_IPV4_NETWORKS: Final = tuple(
    ipaddress.IPv6Network(n)
    for n in (
        "::/96",
        "::ffff:0:0:0/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/32",
    )
)

_NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")
_V4_TABLE: Final = tuple((n, c) for n, c in TABLE if n.version == 4)
_V6_TABLE: Final = tuple((n, c) for n, c in TABLE if n.version == 6)


def _classify_v4(ip: ipaddress.IPv4Address) -> AddressClass:
    for net, cls in _V4_TABLE:
        if ip in net:
            return cls
    return AddressClass.PUBLIC


def _classify_v6(ip: ipaddress.IPv6Address) -> AddressClass:
    if ip.ipv4_mapped is not None:
        return _classify_v4(ip.ipv4_mapped)
    if ip in _NAT64:
        return _classify_v4(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if int(ip) > 1 and any(ip in net for net in EMBEDDED_IPV4_NETWORKS):
        return AddressClass.EMBEDDED_IPV4
    for net, cls in _V6_TABLE:
        if ip in net:
            return cls
    return AddressClass.PUBLIC


def _parse(address: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not isinstance(address, str) or "%" in address:  # a zone id is refused (NG-2)
        return None
    try:
        return ipaddress.ip_address(address)
    except ValueError:
        return None


def classify_address(address: str) -> AddressClass:
    """The class of one IP address text (NG-1 to NG-3). Never raises: text that is not exactly one
    IP address (a zone id, a name, brackets, a short or hex IPv4 spelling) is `unparseable`."""
    ip = _parse(address)
    if ip is None:
        return AddressClass.UNPARSEABLE
    if isinstance(ip, ipaddress.IPv4Address):
        return _classify_v4(ip)
    return _classify_v6(ip)


def address_target(address: str, port: int) -> str:
    """The canonical `<ip>:<port>` (`[v6]:<port>`) of an address, as `network_target` spells it: an
    IPv4-mapped or NAT64 address is spelled as its IPv4 address. `ValueError` for text that is not
    an IP address, or a port outside 1 to 65535."""
    ip = _parse(address)
    if ip is None:
        raise ValueError("not an IP address")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("a port is 1 to 65535")
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if isinstance(ip, ipaddress.IPv6Address):
        return f"[{ip.compressed}]:{port}"
    return f"{ip.compressed}:{port}"


def _split_host_port(target: str) -> tuple[str, str | None]:
    """`(host text, port text or None)` of a destination or rule target. The port part is the text
    after the last `:` outside `[...]`."""
    if target.startswith("["):
        end = target.find("]")
        if end == -1:
            return target, None
        rest = target[end + 1 :]
        return target[: end + 1], rest[1:] if rest.startswith(":") else None
    if ":" in target:
        host, _, port = target.rpartition(":")
        return host, port
    return target, None


def host_of_target(target: str) -> str:
    """The host text of a network destination (`10.0.0.5:443` gives `10.0.0.5`, `[::1]:80` gives
    `::1`, a name stays a name). Pure text, no validation."""
    host, _ = _split_host_port(target)
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def has_exact_host(rule_target: str) -> bool:
    """NG-10 (d): the rule target has a port part and the text before it holds neither `*` nor `?`.
    `10.0.0.5:443` and `10.0.0.5:*` are exact; `*`, `*:443`, `10.*`, `10.*:443`, `10.0.0.?:443`
    and `[fd00::?]:443` are not."""
    host, port = _split_host_port(rule_target)
    if not port or not host:
        return False
    return "*" not in host and "?" not in host
