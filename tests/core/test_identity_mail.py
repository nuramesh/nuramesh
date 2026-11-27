"""The pure part of outgoing email (R1.39; sections 1 and 2)."""

import pytest

from nuramesh.core.identity import (
    DEFAULT_PORTS,
    MailErrorCode,
    MailKind,
    MailSecurity,
    MailSendError,
    MailSettings,
    OutgoingMail,
    address_problem,
    org_name_problem,
    parse_public_url,
)


def test_send_error_text_is_the_code_only() -> None:
    assert str(MailSendError(MailErrorCode.CONNECT)) == "connect"
    assert {c.value for c in MailErrorCode} == {
        "not_configured", "connect", "tls", "auth", "rejected", "timeout", "invalid_message",
    }  # fmt: skip


def test_mail_kinds_are_the_closed_set() -> None:
    assert {k.value for k in MailKind} == {"invite", "reset", "verify", "notice", "test"}


@pytest.mark.parametrize(
    "address",
    [
        "a@example.org\r\nBcc: x@example.org",
        "a@example.org\nBcc: x",
        "a\x00@example.org",
        "jörg@example.org",
        "a@exämple.org",
        "no-at-sign",
        "a@b",
        "",
    ],
)
def test_unusable_addresses_are_refused(address: str) -> None:
    assert address_problem(address)


def test_plain_address_is_fine() -> None:
    assert address_problem("ada.l+x@mail.example.org") is None


def test_repr_hides_address_text_and_settings() -> None:
    mail = OutgoingMail("ada@example.org", MailKind.RESET, "Hello", "link https://x/#t=nmr_abc")
    assert "ada@example.org" not in repr(mail) and "nmr_" not in repr(mail)
    s = MailSettings(
        host="smtp.example.org",
        port=DEFAULT_PORTS[MailSecurity.STARTTLS],
        security=MailSecurity.STARTTLS,
        sender="noreply@example.org",
        public_url="https://app.example.org",
        user="u",
        password_file="/run/secrets/x",
    )
    assert "smtp.example.org" not in repr(s) and "/run/secrets" not in repr(s)


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("https://app.example.org/", "https://app.example.org"),
        ("https://app.example.org:8443", "https://app.example.org:8443"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://127.0.0.1:3000/", "http://127.0.0.1:3000"),
    ],
)
def test_public_url_accepted_and_normalised(given: str, stored: str) -> None:
    assert parse_public_url(given) == stored


@pytest.mark.parametrize(
    "bad",
    [
        "http://app.example.org",
        "ftp://app.example.org",
        "https://user@app.example.org",
        "https://app.example.org/path",
        "https://app.example.org?x=1",
        "https://app.example.org#frag",
        "https://",
        "app.example.org",
        "https://app.example.org:notaport",
        "",
    ],
)
def test_public_url_refused(bad: str) -> None:
    with pytest.raises(ValueError) as caught:
        parse_public_url(bad)
    assert bad == "" or bad not in str(caught.value)


def test_org_name_rules() -> None:
    assert org_name_problem("Acme") is None
    assert org_name_problem("a" * 80) is None
    assert org_name_problem("a" * 81)
    assert org_name_problem("")
    assert org_name_problem("Ac\x07me")
    assert org_name_problem("Ac​me")  # a format (Cf) character


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "::1", "[::1]"])
def test_loopback_hosts_are_the_three_spec_names(host: str) -> None:
    from nuramesh.core.identity import is_loopback_host

    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["127.0.0.2", "127.255.255.254", "0.0.0.0", "::", "10.0.0.1"])
def test_other_addresses_in_the_loopback_range_are_not_loopback(host: str) -> None:
    from nuramesh.core.identity import is_loopback_host

    assert not is_loopback_host(host)
