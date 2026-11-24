"""Round 6 behaviour pin for `nuramesh.core.auth` found by a wider AST-replay run (tests only):
the base64 decoder refuses a length that no byte string encodes to. Test data is made up for the
test and never key-shaped."""

from nuramesh.core import auth


def test_unb64_refuses_a_length_no_byte_string_encodes_to() -> None:
    # unpadded base64 of whole bytes has a length of 4k, 4k+2 or 4k+3, never 4k+1
    assert auth._unb64("AAAAA") is None
    assert auth._unb64("A") is None
    # the neighbours decode: "AA" is one zero byte, "AAA" two, "AAAA" three
    assert auth._unb64("AA") == b"\x00"
    assert auth._unb64("AAA") == b"\x00\x00"
    assert auth._unb64("AAAA") == b"\x00\x00\x00"
