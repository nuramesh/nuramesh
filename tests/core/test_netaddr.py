"""#1153 PR 1: the address classifier (NG-1 to NG-4, NG-10 (d), section 9.1)."""

import ipaddress
import re
from pathlib import Path

import pytest
from netaddr_rows import CASES

from nuramesh.core import netaddr
from nuramesh.core.netaddr import (
    NEVER,
    POLICY_DECIDED,
    AddressClass,
    address_target,
    classify_address,
    has_exact_host,
)


@pytest.mark.parametrize(("address", "expected"), CASES)
def test_section_9_1_rows(address: str, expected: AddressClass) -> None:
    assert classify_address(address) is expected


def test_never_and_policy_decided_are_disjoint_and_complete() -> None:
    assert not NEVER & POLICY_DECIDED
    assert NEVER | POLICY_DECIDED | {AddressClass.PUBLIC} == set(AddressClass)
    assert POLICY_DECIDED == {
        AddressClass.LOOPBACK,
        AddressClass.PRIVATE,
        AddressClass.CGNAT,
        AddressClass.ULA,
    }


@pytest.mark.parametrize("bad", [None, 7, b"127.0.0.1", " 8.8.8.8", "8.8.8.8\n", "010.0.0.1"])
def test_classify_never_raises(bad: object) -> None:
    assert classify_address(bad) is AddressClass.UNPARSEABLE  # type: ignore[arg-type]


def test_a_non_ascii_digit_is_unparseable() -> None:
    assert classify_address("١٢٧.0.0.1") is AddressClass.UNPARSEABLE


def test_metadata_wins_over_ula_and_reserved_ranges() -> None:
    assert classify_address("fd20:ce::254") is AddressClass.METADATA
    assert classify_address("192.0.0.192") is AddressClass.METADATA
    assert classify_address("192.0.0.191") is AddressClass.RESERVED


@pytest.mark.parametrize(
    ("address", "port", "expected"),
    [
        ("10.0.0.5", 443, "10.0.0.5:443"),
        ("fd00::5", 443, "[fd00::5]:443"),
        ("FD00:0:0:0:0:0:0:5", 80, "[fd00::5]:80"),
        ("::ffff:10.0.0.1", 8080, "10.0.0.1:8080"),
        ("::ffff:a00:1", 8080, "10.0.0.1:8080"),
        ("64:ff9b::a00:1", 1, "10.0.0.1:1"),
        ("::1", 65535, "[::1]:65535"),
    ],
)
def test_address_target_spelling(address: str, port: int, expected: str) -> None:
    assert address_target(address, port) == expected


@pytest.mark.parametrize(
    ("address", "port"),
    [("example.test", 80), ("fe80::1%eth0", 80), ("10.0.0.1", 0), ("10.0.0.1", 65536)],
)
def test_address_target_refuses_bad_input(address: str, port: int) -> None:
    with pytest.raises(ValueError, match=r"."):
        address_target(address, port)


@pytest.mark.parametrize(
    ("rule", "exact"),
    [
        ("10.0.0.5:443", True),
        ("10.0.0.5:*", True),
        ("[fd00::5]:443", True),
        ("intra.example.test:443", True),
        ("*", False),
        ("*:443", False),
        ("10.*", False),
        ("10.*:443", False),
        ("10.0.0.?:443", False),
        ("[fd00::?]:443", False),
        ("[fd00::*]:443", False),
        ("10.0.0.5", False),
        ("[fd00::5]", False),
        ("10.0.0.5:", False),
        (":443", False),
    ],
)
def test_has_exact_host(rule: str, exact: bool) -> None:
    assert has_exact_host(rule) is exact


def test_host_of_target() -> None:
    assert netaddr.host_of_target("10.0.0.5:443") == "10.0.0.5"
    assert netaddr.host_of_target("10.0.0.5") == "10.0.0.5"
    assert netaddr.host_of_target("[::1]:80") == "::1"
    assert netaddr.host_of_target("a.example.test:80") == "a.example.test"


# --- NG-4 / G-40: tighten only, by range containment against a frozen copy of the legacy rules ---

# Python 3.12.3 (2026-11-16): the networks `ipaddress` refused (`not is_global or is_multicast`)
# and the extra ranges the legacy `engine/net_guard.is_private_address` refused by hand. Frozen as
# data: the test never calls the stdlib table, which drifts between patch releases.
_LEGACY_V4 = (
    "0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/29 "
    "192.0.0.170/31 192.0.2.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 "
    "224.0.0.0/4 240.0.0.0/4 255.255.255.255/32"
).split()
_LEGACY_V6 = (
    "::1/128 ::/128 ::/96 100::/64 2001::/23 2001:2::/48 2001:10::/28 2001:db8::/32 fc00::/7 "
    "fe80::/10 fec0::/10 ::ffff:0:0:0/96 ff00::/8"
).split()
# judged by their embedded IPv4 address (NG-2), so the IPv4 containment below covers them
_LEGACY_UNWRAPPED = ("::ffff:0:0/96", "64:ff9b::/96")


def _collapsed_non_public(version: int) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
        n for n, cls in netaddr.TABLE if cls is not AddressClass.PUBLIC and n.version == version
    ]
    if version == 6:
        nets += list(netaddr.EMBEDDED_IPV4_NETWORKS)
    return list(ipaddress.collapse_addresses(nets))  # type: ignore[type-var]


@pytest.mark.parametrize("version", [4, 6])
def test_every_legacy_refused_network_is_still_refused(version: int) -> None:
    covering = _collapsed_non_public(version)
    frozen = _LEGACY_V4 if version == 4 else _LEGACY_V6
    leftovers = [
        text
        for text in frozen
        if not any(ipaddress.ip_network(text).subnet_of(n) for n in covering)  # type: ignore[arg-type]
    ]
    assert not leftovers, f"loosened: {leftovers}"
    for text in frozen:
        net = ipaddress.ip_network(text)
        for edge in (net[0], net[-1]):
            assert classify_address(str(edge)) is not AddressClass.PUBLIC, (text, str(edge))


def test_unwrapped_legacy_prefixes_hold_on_their_ipv4_space() -> None:
    covering = _collapsed_non_public(4)
    for text in _LEGACY_V4:
        net = ipaddress.ip_network(text)
        assert any(net.subnet_of(n) for n in covering)  # type: ignore[arg-type]
        for edge in (net[0], net[-1]):
            assert classify_address(f"::ffff:{edge}") is not AddressClass.PUBLIC
            assert classify_address(f"64:ff9b::{edge}") is not AddressClass.PUBLIC
    assert _LEGACY_UNWRAPPED  # the two prefixes are judged by their embedded address


# --- the core boundary: pure stdlib ---


def test_module_imports_only_the_stdlib() -> None:
    source = Path(netaddr.__file__).read_text()
    found = set(re.findall(r"^(?:from|import) (\w+)", source, flags=re.M))
    assert found <= {"ipaddress", "enum", "typing"}
