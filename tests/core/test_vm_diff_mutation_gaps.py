"""Mutation pins for `nuramesh.core.vm_diff` (#1335): header and quoting edge cases that the
main tests leave unchecked. Fake data only."""

from __future__ import annotations

import pytest

from nuramesh.core.vm_diff import DiffFormatError, parse_diff


@pytest.mark.parametrize(
    "header",
    [
        b'diff --git "a/x b/x\n',  # the opening quote never closes
        b'diff --git "a/x"b/x\n',  # no space after the closing quote
        b'diff --git "a/x\\\n',  # a trailing backslash swallows the end
        b'diff --git "a/x"?b/x\n',  # something other than a space after the closing quote
    ],
)
def test_an_unterminated_or_unspaced_quoted_header_is_a_bad_header(header: bytes) -> None:
    with pytest.raises(DiffFormatError) as caught:
        parse_diff(header)
    assert caught.value.code == "bad_header" and caught.value.line == 1
    assert str(caught.value) == "diff: bad_header at line 1"


# --- more edge cases found by the sweep -------------------------------------------------------

import dataclasses  # noqa: E402
import hashlib  # noqa: E402

from nuramesh.core import vm_diff  # noqa: E402
from nuramesh.core.vm_diff import FileChange, change_hash  # noqa: E402

from .test_vm_diff import NEW, OLD, SIMPLE, _file, norm  # noqa: E402


def refused(raw: bytes) -> DiffFormatError:
    with pytest.raises(DiffFormatError) as caught:
        parse_diff(raw)
    return caught.value


def test_unquote_edge_cases() -> None:
    assert vm_diff._unquote(b'""', 1) == b""
    assert vm_diff._unquote(b'"a\\tb"', 1) == b"a\tb"
    assert vm_diff._unquote(b'"\\101"', 1) == b"A"
    for bad in (b'"', b'"abc', b"abc", b'"\\1"', b'"\\18x"', b'"\\118"', b'"\\9"', b'"\\"'):
        with pytest.raises(DiffFormatError) as caught:
            vm_diff._unquote(bad, 4)
        assert caught.value.code == "bad_path_quote" and caught.value.line == 4


def test_quoted_end_positions() -> None:
    assert vm_diff._quoted_end(b'""') == 2
    assert vm_diff._quoted_end(b'"ab" x') == 4
    assert vm_diff._quoted_end(b'"a\\"b" x') == 6  # an escaped quote does not close it
    assert vm_diff._quoted_end(b'"\\""') == 4
    assert vm_diff._quoted_end(b'"abc') == -1
    assert vm_diff._quoted_end(b'"abc\\') == -1


@pytest.mark.parametrize(
    "header",
    [
        b"diff --git a/ b/\n",  # too short
        b"diff --git a/xyz_b/xyz\n",  # no space in the middle
        b"diff --git c/xyz b/xyz\n",  # left name not a/
        b"diff --git a/xyz d/xyz\n",  # right name not b/
        b"diff --git a/xyzz b/xyz\n",  # even length: not two equal names
    ],
)
def test_unquoted_header_shapes_are_bad_headers(header: bytes) -> None:
    error = refused(header)
    assert error.code == "bad_header" and error.line == 1


def test_a_header_whose_two_names_differ_is_a_bad_header_on_its_own_line() -> None:
    error = refused(SIMPLE + b"diff --git a/x b/y\n")
    assert error.code == "bad_header" and error.line == 11


def test_error_line_numbers_are_one_based() -> None:
    assert refused(SIMPLE + b"junk\n").line == 11
    assert refused(b"not a diff\n").line == 1
    assert refused(_file("f", b"@@ -1,3 +1,3 @@\n-a\n+b\n")).code == "hunk_truncated"
    ext = b"diff --git a/f b/f\nsurprise\n"
    error = refused(ext)
    assert (error.code, error.line) == ("unexpected_line", 2)


def test_last_line_without_a_trailing_newline_is_still_read() -> None:
    raw = _file("f", b"@@ -1 +1 @@\n-a\n+b")
    assert norm(raw)[0][4] == [["-", "a"], ["+", "b"]]
    assert norm(_file("f", b"@@ -1 +1 @@\n-a\n+b\n")) == norm(raw)
    assert parse_diff(b"") == []


def test_index_ids_must_both_be_full_length() -> None:
    long_old, long_new = "a" * 64, "b" * 64
    ok = _file("f", b"@@ -1 +1 @@\n-a\n+b\n", old_blob=long_old, new_blob=long_new)
    assert norm(ok)[0][4] == [["-", "a"], ["+", "b"]]
    for old, new in ((OLD, "b" * 7), ("a" * 7, NEW), (OLD, "b" * 65), ("a" * 41, NEW)):
        bad = _file("f", b"@@ -1 +1 @@\n-a\n+b\n", old_blob=old, new_blob=new)
        assert refused(bad).code == "index_not_full"


def test_a_binary_file_without_an_index_is_refused() -> None:
    raw = b"diff --git a/f b/f\nBinary files a/f and b/f differ\n"
    error = refused(raw)
    assert error.code == "binary_without_index" and error.line == 1


def test_hunk_overrun_is_refused_when_only_one_side_overruns() -> None:
    assert refused(_file("f", b"@@ -1 +1 @@\n-a\n-b\n+c\n")).code == "hunk_overrun"


def test_file_change_is_frozen_and_its_document_shape() -> None:
    change = FileChange("p", "M", "100644", "100644", (("-", "a"),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        change.path = "q"  # type: ignore[misc]
    assert change.new_blob is None
    assert change.document() == ["p", "M", "100644", "100644", [["-", "a"]]]
    binary = FileChange("p", "A", None, "100644", None, NEW)
    assert binary.document() == ["p", "A", None, "100644", NEW]


def test_line_text_is_hex_for_invalid_utf8_only() -> None:
    assert vm_diff.line_text(b"caf\xc3\xa9") == "café"
    assert vm_diff.line_text(b"\x80") == "hex:80"
    assert vm_diff.line_text(b"") == ""


def test_error_message_and_attributes() -> None:
    error = DiffFormatError("some_code", 12)
    assert str(error) == "diff: some_code at line 12"
    assert (error.code, error.line) == ("some_code", 12)


def test_pins_are_the_exact_spec_values() -> None:
    assert vm_diff._FULL_ID == {40, 64}
    assert change_hash(b"") == hashlib.sha256(b"[]").hexdigest()


def test_an_unknown_line_inside_a_hunk_is_unexpected() -> None:
    error = refused(_file("f", b"@@ -1,2 +1,2 @@\n-a\n?x\n+b\n"))
    assert error.code == "unexpected_line" and error.line == 7
