"""Pins for core/endpoints.py found by operator-mutant replay (#1350). Fake hosts only."""

import hashlib

from nuramesh.core.endpoints import (
    REMOTE_RESIDENCY,
    fingerprint,
    host_of,
    is_loopback,
    is_remote,
    normalise,
    residency_problem,
)

BROKEN = "http://[::1"  # urlsplit raises ValueError on the unclosed bracket


def test_the_residency_text_is_the_fixed_sentence() -> None:
    expected = "openai-compatible: a remote endpoint can only declare global processing"
    assert REMOTE_RESIDENCY == expected
    assert residency_problem("https://api.example.test/v1", "canada") == expected


def test_a_malformed_endpoint_never_raises_and_is_remote_and_unchanged() -> None:
    assert host_of(BROKEN) is None
    assert is_loopback(BROKEN) is False
    assert is_remote(BROKEN) is True
    assert normalise(BROKEN) == BROKEN


def test_an_endpoint_without_a_host_has_no_host() -> None:
    assert host_of("http:///v1") is None
    assert host_of("not a url") is None


def test_host_of_lowercases_and_keeps_ipv6_brackets() -> None:
    assert host_of("HTTPS://API.Example.TEST:8443/v1") == "api.example.test"
    assert host_of("http://[::1]:8080/v1") == "[::1]"
    assert host_of("http://user@127.0.0.1/v1") == "127.0.0.1"


def test_only_http_and_https_to_a_literal_loopback_are_loopback() -> None:
    assert is_loopback("http://127.0.0.1/v1") and is_loopback("https://127.0.0.1/v1")
    assert not is_loopback("ftp://127.0.0.1/v1")
    assert not is_loopback("ws://[::1]/v1")
    assert not is_loopback("127.0.0.1:11434/v1")
    assert not is_loopback("http://127.0.0.2/v1")
    assert not is_loopback("http://127.0.0.1.example.test/v1")
    assert is_remote("ftp://127.0.0.1/v1") and not is_remote("http://[::1]:1/v1")


def test_normalise_leaves_anything_without_scheme_or_host_untouched() -> None:
    for raw in ("example.test/v1/", "/v1/", "http://", "://host/v1/", ""):
        assert normalise(raw) == raw


def test_normalise_keeps_port_and_path_case_and_strips_every_trailing_slash() -> None:
    assert (
        normalise("HTTP://Host.Example.TEST:8080/Api/V1///")
        == "http://host.example.test:8080/Api/V1"
    )
    assert normalise("https://host.example.test") == "https://host.example.test"
    assert normalise("https://host.example.test/") == "https://host.example.test"
    assert normalise("http://[::1]:11434/v1/") == "http://[::1]:11434/v1"


def test_normalise_strips_only_slashes_not_other_trailing_characters() -> None:
    assert normalise("https://h.example.test/vX/") == "https://h.example.test/vX"
    assert normalise("https://h.example.test/v1/X/") == "https://h.example.test/v1/X"
    assert normalise("https://h.example.test/XX") == "https://h.example.test/XX"


def test_residency_only_blocks_a_remote_endpoint_that_is_not_global() -> None:
    assert residency_problem("https://api.example.test/v1", "global") is None
    assert residency_problem(BROKEN, "canada") == REMOTE_RESIDENCY
    assert residency_problem("http://127.0.0.1:1/v1", "canada") is None


def test_the_fingerprint_hashes_utf8_json_not_ascii_escapes() -> None:
    got = fingerprint("https://h.example.test/v1/", "key", "kéy-name", "üñí")
    canonical = (
        '{"auth":"key","endpoint":"https://h.example.test/v1","endpoint_id":"üñí","key":"kéy-name"}'
    )
    assert got == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    escaped = canonical.replace("üñí", "\\u00fc\\u00f1\\u00ed").replace("kéy", "k\\u00e9y")
    assert got != hashlib.sha256(escaped.encode("utf-8")).hexdigest()


def test_the_fingerprint_hashes_none_as_null_and_normalises_the_endpoint() -> None:
    canonical = '{"auth":"none","endpoint":"http://127.0.0.1:1/v1","endpoint_id":null,"key":null}'
    expected = hashlib.sha256(canonical.encode()).hexdigest()
    assert fingerprint("HTTP://127.0.0.1:1/v1/", "none", None, None) == expected
