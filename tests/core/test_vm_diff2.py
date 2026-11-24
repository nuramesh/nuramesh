"""Merge Gate `diff-2` change hash (#944): the tagged form for bytes that are not UTF-8.

`diff-1` wrote a line or a path that is not valid UTF-8 as the string `"hex:"` plus its hex, so a
valid UTF-8 line or path spelled exactly that way (the text `hex:ff`) encoded the same as the byte
`ff`: two different changes had one change hash. `diff-2` writes such a value as the JSON array
`["x", "<lower-case hex>"]`, which no text line or path can equal (spec README section 4, "Change
hash"). Everything else is `diff-1` unchanged, so a change whose lines and paths are all valid
UTF-8 has the same hash under both pins. The frozen `diff-2` vectors below come from an
independent recompute (`tests/data/vm_diff2_recompute.py`, real git, no nuramesh import).
"""

from __future__ import annotations

from typing import Any

import pytest

from nuramesh.core.vm_diff import change_hash

ZERO = "0" * 40
OLD = "a" * 40  # obviously fake blob ids: one repeated character
NEW = "b" * 40


def _modified(path: bytes, minus: bytes, plus: bytes) -> bytes:
    """One modified text file as the pinned render prints it (path unquoted)."""
    return (
        b"diff --git a/" + path + b" b/" + path + b"\n"
        b"index " + OLD.encode() + b".." + NEW.encode() + b" 100644\n"
        b"--- a/" + path + b"\n+++ b/" + path + b"\n"
        b"@@ -1 +1 @@\n-" + minus + b"\n+" + plus + b"\n"
    )


def _added(header_path: bytes, plus: bytes) -> bytes:
    """One added text file; `header_path` is the path as git prints it after `a/` and `b/`
    (a C-quoted path is passed with its quotes removed and is re-quoted here)."""
    quoted = b"\\" in header_path
    a = b'"a/' + header_path + b'"' if quoted else b"a/" + header_path
    b = b'"b/' + header_path + b'"' if quoted else b"b/" + header_path
    return (
        b"diff --git " + a + b" " + b + b"\n"
        b"new file mode 100644\n"
        b"index " + ZERO.encode() + b".." + NEW.encode() + b"\n"
        b"--- /dev/null\n+++ " + b + b"\n"
        b"@@ -0,0 +1 @@\n+" + plus + b"\n"
    )


# --- #944: the collision of diff-1, shown failing on the base ----------------------------------


def test_944_a_text_line_spelled_hex_and_a_byte_line_have_different_change_hashes() -> None:
    """The removed line is the single byte ff in one change and the text `hex:ff` in the other."""
    byte_line = _modified(b"f.txt", b"\xff", b"ok")
    text_line = _modified(b"f.txt", b"hex:ff", b"ok")
    assert change_hash(byte_line) != change_hash(text_line)


def test_944_a_path_spelled_hex_and_a_non_utf8_path_have_different_change_hashes() -> None:
    """An added file whose path is the byte ff (git prints it C-quoted as `\\377`) and an added
    file whose path is the text `hex:ff`, with the same content."""
    byte_path = _added(b"\\377", b"x")
    text_path = _added(b"hex:ff", b"x")
    assert change_hash(byte_path) != change_hash(text_path)


def test_944_an_added_text_line_spelled_hex_differs_from_the_byte_line_too() -> None:
    byte_line = _modified(b"f.txt", b"ok", b"a\xfe\xff")
    text_line = _modified(b"f.txt", b"ok", b"hex:61feff")
    assert change_hash(byte_line) != change_hash(text_line)


def _binary_added(header_path: bytes, quoted: bool) -> bytes:
    """One added binary file (same new blob for every path); `header_path` as git prints it."""
    q = b'"' if quoted else b""
    return (
        b"diff --git " + q + b"a/" + header_path + q + b" " + q + b"b/" + header_path + q + b"\n"
        b"new file mode 100644\n"
        b"index " + ZERO.encode() + b".." + NEW.encode() + b"\n"
        b"GIT binary patch\nliteral 2\nJcmey*000620RR91\n\nliteral 0\nHcmV?d00001\n\n"
    )


def test_944_a_binary_path_spelled_hex_and_a_non_utf8_path_have_different_change_hashes() -> None:
    from nuramesh.core.vm_diff import change_document, parse_diff

    byte_path = _binary_added(b"\\377", quoted=True)
    text_path = _binary_added(b"hex:ff", quoted=False)
    assert change_hash(byte_path) != change_hash(text_path)
    byte_doc: Any = change_document(parse_diff(byte_path), "diff-2")
    text_doc: Any = change_document(parse_diff(text_path), "diff-2")
    assert byte_doc[0][0] == ["x", "ff"]
    assert text_doc[0][0] == "hex:ff"


# --- the diff-2 document, pins and the frozen diff-1 behaviour ---------------------------------


def _doc2(raw: bytes) -> Any:
    from nuramesh.core.vm_diff import change_document, parse_diff

    return change_document(parse_diff(raw), "diff-2")


def test_diff2_is_the_writer_pin_and_diff1_stays_known() -> None:
    from nuramesh.core.vm_diff import DIFF1_PIN, DIFF2_PIN, DIFF_PIN, KNOWN_DIFF_PINS

    assert DIFF1_PIN == "diff-1"
    assert DIFF2_PIN == "diff-2"
    assert DIFF_PIN == DIFF2_PIN
    assert KNOWN_DIFF_PINS == frozenset({"diff-1", "diff-2"})


def test_diff2_writes_non_utf8_lines_and_paths_as_the_tagged_pair() -> None:
    assert _doc2(_modified(b"f.txt", b"\xff\xfe", b"hex:fffe")) == [
        ["f.txt", "M", "100644", "100644", [["-", ["x", "fffe"]], ["+", "hex:fffe"]]]
    ]
    assert _doc2(_added(b"g\\377x.txt", b"x")) == [
        [["x", "67ff782e747874"], "A", None, "100644", [["+", "x"]]]
    ]


def test_diff2_hex_is_lower_case_and_an_empty_text_line_stays_text() -> None:
    doc = _doc2(_modified(b"f.txt", b"", b"\xab\xcd"))
    assert doc[0][4] == [["-", ""], ["+", ["x", "abcd"]]]


def test_diff1_keeps_its_frozen_encoding_and_its_collision() -> None:
    """`diff-1` is never edited: it still writes `hex:` and still collides; only `diff-2` is
    written now."""
    from nuramesh.core.vm_diff import change_document, parse_diff

    byte_line = _modified(b"f.txt", b"\xff", b"ok")
    text_line = _modified(b"f.txt", b"hex:ff", b"ok")
    doc: Any = change_document(parse_diff(byte_line), "diff-1")
    assert doc[0][4][0] == ["-", "hex:ff"]
    assert change_hash(byte_line, pin="diff-1") == change_hash(text_line, pin="diff-1")


def test_an_unknown_pin_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown diff pin"):
        change_hash(b"", pin="diff-3")


# --- frozen diff-2 vectors (never regenerated; a change is a diff-3) ---------------------------
#
# Each raw diff is real `git diff` output under the pinned environment (git 2.43) and each hash was
# computed by tests/data/vm_diff2_recompute.py, which writes the expected document by hand from
# README section 4 (it imports nothing from nuramesh) and hashes its compact JSON.

FROZEN_DIFF2_VECTORS: dict[str, tuple[bytes, str]] = {
    "byte_line_ff": (
        (
            b"diff --git a/f.txt b/f.txt\n"
            b"index 29e181ebb88626236ef10f2af5bc94781c8061f1.."
            b"9766475a4185a151dc9d56d614ffb9aaea3bfd42 100644\n"
            b"--- a/f.txt\n"
            b"+++ b/f.txt\n"
            b"@@ -1 +1 @@\n"
            b"-\xff\n"
            b"+ok\n"
        ),
        "d0501f7f0addebabac2701837d2f827d64978c1c4daaa562fd5f2921b39ee288",
    ),
    "byte_path_ff": (
        (
            b'diff --git "a/\\377" "b/\\377"\n'
            b"new file mode 100644\n"
            b"index 0000000000000000000000000000000000000000.."
            b"587be6b4c3f93f93c489c0111bba5596147a26cb\n"
            b"--- /dev/null\n"
            b'+++ "b/\\377"\n'
            b"@@ -0,0 +1 @@\n"
            b"+x\n"
        ),
        "3f5d8e27e13983998fd4fa6f61f6a79547c0f716165056cdc35a821261d2ba68",
    ),
    "hex_line": (
        (
            b"diff --git a/h.txt b/h.txt\n"
            b"index 93fbb4d0f7ae295f67bfdb8f110400065fc36190.."
            b"524fe6629399b9b84518df86abf7782c78ecce49 100644\n"
            b"--- a/h.txt\n"
            b"+++ b/h.txt\n"
            b"@@ -1 +1 @@\n"
            b"-a\xff\n"
            b"+b\xfe\xff\n"
        ),
        "d0c9eb5b1c5d7d13c5c9dab0f32cf969b51447d88e95f23e8fe75748bc5469aa",
    ),
    "hex_path": (
        (
            b'diff --git "a/g\\377x.txt" "b/g\\377x.txt"\n'
            b"new file mode 100644\n"
            b"index 0000000000000000000000000000000000000000.."
            b"587be6b4c3f93f93c489c0111bba5596147a26cb\n"
            b"--- /dev/null\n"
            b'+++ "b/g\\377x.txt"\n'
            b"@@ -0,0 +1 @@\n"
            b"+x\n"
        ),
        "32337f0e2838a748ef03f015d09649c80824a70d9b5369e9841aed414726add4",
    ),
    "text_line_spelled_hex": (
        (
            b"diff --git a/f.txt b/f.txt\n"
            b"index b0a37c6e4a1642c06e55534643f73f9926910e53.."
            b"9766475a4185a151dc9d56d614ffb9aaea3bfd42 100644\n"
            b"--- a/f.txt\n"
            b"+++ b/f.txt\n"
            b"@@ -1 +1 @@\n"
            b"-hex:ff\n"
            b"+ok\n"
        ),
        "f210a95bf8d9a98e0c7074d3be0ff4518f5c00a29680cefbae289e756b5b5198",
    ),
    "text_path_spelled_hex": (
        (
            b"diff --git a/hex:ff b/hex:ff\n"
            b"new file mode 100644\n"
            b"index 0000000000000000000000000000000000000000.."
            b"587be6b4c3f93f93c489c0111bba5596147a26cb\n"
            b"--- /dev/null\n"
            b"+++ b/hex:ff\n"
            b"@@ -0,0 +1 @@\n"
            b"+x\n"
        ),
        "205e31b85eef3ef90e09b1e5d80955e1173359ef2ef034ec14d8254314a30684",
    ),
    "binary_byte_path_ff": (
        (
            b'diff --git "a/\\377" "b/\\377"\n'
            b"new file mode 100644\n"
            b"index 0000000000000000000000000000000000000000.."
            b"5d983463ed9b1bcb008a040949025e83abf576d1\n"
            b"GIT binary patch\n"
            b"literal 2\n"
            b"Jcmey*000620RR91\n\n"
            b"literal 0\n"
            b"HcmV?d00001\n\n"
        ),
        "9537e8018b54e3e1b006fd824efb9b8b6aa7955ffc30c4eb4ba7a8c4376235b7",
    ),
}


@pytest.mark.parametrize("name", sorted(FROZEN_DIFF2_VECTORS))
def test_frozen_diff2_vector(name: str) -> None:
    raw, expected = FROZEN_DIFF2_VECTORS[name]
    assert change_hash(raw) == expected


def test_frozen_diff2_vectors_cover_the_tagged_cases_and_both_collisions() -> None:
    assert set(FROZEN_DIFF2_VECTORS) == {
        "hex_line",
        "hex_path",
        "text_line_spelled_hex",
        "text_path_spelled_hex",
        "byte_line_ff",
        "byte_path_ff",
        "binary_byte_path_ff",
    }
    assert len({h for _, h in FROZEN_DIFF2_VECTORS.values()}) == 7
