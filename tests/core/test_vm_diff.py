"""Merge Gate `diff-1` normaliser (spec section 4 "Change hash", Fable fix F-2); the
`diff-2` tagged form of #944 is tested in `test_vm_diff2.py`.

The change hash is SHA-256 of the canonical JSON of the normalised patch: per file
`[path, status, old_mode, new_mode, lines]` with `lines = [["-"|"+", line], ...]` flattened over
hunks, `["\\", "nonl"]` after a line without a newline, bytes that are not UTF-8 as `hex:...`,
CR kept, no whitespace normalisation, a binary file carrying its new blob id. Context lines, hunk
headers, offsets and `index` lines are dropped, so a clean merge of main keeps the hash while any
added or removed line changes it. Pure stdlib code: git is never called from `nuramesh.core`;
the property test below drives a real git only to render diffs.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nuramesh.core.canonical import canonical_json
from nuramesh.core.vm_diff import (
    DIFF1_PIN,
    DIFF_COMMAND,
    DIFF_PIN,
    DIFF_PIN_CONFIG,
    DiffFormatError,
    change_hash,
    diff1_document,
    diff_hash,
    line_text,
    parse_diff,
)

ZERO = "0" * 40
OLD = "a" * 40  # obviously fake blob ids: one repeated character
NEW = "b" * 40


def _file(
    path: str,
    body: bytes,
    *,
    status: str = "M",
    old_mode: str | None = "100644",
    new_mode: str | None = "100644",
    old_blob: str = OLD,
    new_blob: str = NEW,
) -> bytes:
    """A text file section as `git diff --full-index --no-renames` renders it."""
    head = f"diff --git a/{path} b/{path}\n".encode()
    if status == "A":
        head += f"new file mode {new_mode}\nindex {ZERO}..{new_blob}\n".encode()
        head += f"--- /dev/null\n+++ b/{path}\n".encode()
    elif status == "D":
        head += f"deleted file mode {old_mode}\nindex {old_blob}..{ZERO}\n".encode()
        head += f"--- a/{path}\n+++ /dev/null\n".encode()
    elif old_mode != new_mode:
        head += f"old mode {old_mode}\nnew mode {new_mode}\nindex {old_blob}..{new_blob}\n".encode()
        head += f"--- a/{path}\n+++ b/{path}\n".encode()
    else:
        head += f"index {old_blob}..{new_blob} {old_mode}\n".encode()
        head += f"--- a/{path}\n+++ b/{path}\n".encode()
    return head + body


def norm(raw: bytes) -> Any:
    """The diff-1 document of a render (typed loosely for the assertions below)."""
    return diff1_document(parse_diff(raw))


SIMPLE = _file(
    "src/a.py",
    b"@@ -1,4 +1,4 @@\n one\n-two\n+TWO\n three\n four\n",
)


def test_pin_is_data_for_the_engine_and_names_the_f2_render() -> None:
    assert DIFF_PIN == "diff-2"  # #944; diff-1 stays computable for its frozen vectors
    assert DIFF_COMMAND == ("diff", "--binary", "--full-index", "--no-ext-diff", "--no-renames")
    assert set(DIFF_PIN_CONFIG) == {
        "diff.algorithm=myers",
        "diff.renames=false",
        "diff.noprefix=false",
        "core.quotepath=off",
        "diff.indentHeuristic=false",
        "diff.suppressBlankEmpty=false",
    }


def test_simple_modification_normalises_to_removed_and_added_lines_only() -> None:
    assert norm(SIMPLE) == [["src/a.py", "M", "100644", "100644", [["-", "two"], ["+", "TWO"]]]]


def test_change_hash_is_sha256_of_canonical_json_and_diff_hash_is_of_raw_bytes() -> None:
    doc = norm(SIMPLE)
    assert change_hash(SIMPLE) == hashlib.sha256(canonical_json(doc)).hexdigest()
    assert diff_hash(SIMPLE) == hashlib.sha256(SIMPLE).hexdigest()


def test_golden_vectors_never_regenerated() -> None:
    """Frozen hashes (never-regenerate policy, `fable-contract-freeze.md`): a change here is a
    `diff-2`, never an edit of these constants."""
    assert change_hash(b"") == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    assert change_hash(SIMPLE) == "1d1ed1330077b9256a62a6846ab296d82ee04191d14839faee96edab6541a202"


# --- #945: frozen change-hash vectors, one per diff-1 case --------------------------------
#
# Each raw diff below is real `git diff` output (pinned diff-1 environment, git 2.43) and each hash
# was computed by an INDEPENDENT recompute: tests/data/vm_diff_recompute.py builds the
# expected normalised document by hand from README section 4 (it imports nothing from nuramesh) and
# hashes its compact JSON. These constants are never edited: a change is a `diff-2`.

FROZEN_CHANGE_HASH_VECTORS: dict[str, tuple[bytes, str]] = {
    "binary": (
        (
            b"diff --git a/img.bin b/img.bin\n"
            b"new file mode 100644\n"
            b"index 0000000000000000000000000000000000000000.."
            b"a42dbc1a72fbce9fe3a539a50a83c664959d48b5\n"
            b"GIT binary patch\n"
            b"literal 11\n"
            b"ScmZQzWJ=1+ODwAV&j0`qzypW?\n"
            b"\n"
            b"literal 0\n"
            b"HcmV?d00001\n"
            b"\n"
        ),
        "6c722686b6ce24b8fc1ff8f9266a04e808327b9df63278c445e380240d707de1",
    ),
    "crlf": (
        (
            b"diff --git a/c.txt b/c.txt\n"
            b"index 9cca7e3e36146dc4800a363b5540521c0369a6ac.."
            b"d2eb92c3f437753a118e0fb686bbc3d3bba96b63 100644\n"
            b"--- a/c.txt\n"
            b"+++ b/c.txt\n"
            b"@@ -1 +1 @@\n"
            b"-old\r\n"
            b"+new\r\n"
        ),
        "809d48a9cf1be0fb10a4c3a1556010c30d21f7e84785b782b8ce3476113d73b0",
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
        "ef2bba490bb008ebeb9a6ccdfa2a28ff066ab9e9328e2dca8906ba133c7b3d13",
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
        "6ac3028a312443496ac60c720f48a1578b73f00b8242f72b6b03432a10e1f212",
    ),
    "mode_change": (
        (b"diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"),
        "b03397941d10502cf7da4fe6606b3b0e5d90fe4f5e68313f350df35174f8bada",
    ),
    "nonl": (
        (
            b"diff --git a/f.txt b/f.txt\n"
            b"index 489ce0f857e7634a0eb9f328265a3e91fad49f61.."
            b"3e5126c4e761fd09582fc517918a1601b218dff0 100644\n"
            b"--- a/f.txt\n"
            b"+++ b/f.txt\n"
            b"@@ -1 +1 @@\n"
            b"-old\n"
            b"\\ No newline at end of file\n"
            b"+new\n"
            b"\\ No newline at end of file\n"
        ),
        "6d30acb45cbaab05ab19215e8733f4287fe749317933f629bbb18318549676c1",
    ),
    "quoted_utf8_path": (
        (
            b'diff --git "a/d\\303\\274 r/a b.txt" "b/d\\303\\274 r/a b.txt"\n'
            b"new file mode 100644\n"
            b"index 0000000000000000000000000000000000000000.."
            b"587be6b4c3f93f93c489c0111bba5596147a26cb\n"
            b"--- /dev/null\n"
            b'+++ "b/d\\303\\274 r/a b.txt"\t\n'
            b"@@ -0,0 +1 @@\n"
            b"+x\n"
        ),
        "9997117aaa21d118280e2693ec9ffca90a362f83b3a586c29e5015526001f3c6",
    ),
    "submodule": (
        (
            b"diff --git a/vendor/sub b/vendor/sub\n"
            b"new file mode 160000\n"
            b"index 0000000000000000000000000000000000000000.."
            b"1111111111111111111111111111111111111111\n"
            b"--- /dev/null\n"
            b"+++ b/vendor/sub\n"
            b"@@ -0,0 +1 @@\n"
            b"+Subproject commit 1111111111111111111111111111111111111111\n"
        ),
        "ef051cd679c50cb2bf5828e1f07ddb8439c416e14ef34a766afeec2d95b7d0a2",
    ),
    "symlink": (
        (
            b"diff --git a/lnk b/lnk\n"
            b"new file mode 120000\n"
            b"index 0000000000000000000000000000000000000000.."
            b"4cbb553f3f4ac2ee7b01ff6c951d6bf583c39c15\n"
            b"--- /dev/null\n"
            b"+++ b/lnk\n"
            b"@@ -0,0 +1 @@\n"
            b"+target.txt\n"
            b"\\ No newline at end of file\n"
        ),
        "f1ec79c89c35ff93d59c77e3be0fd570e61cc39507ca578277de677b9d7636d9",
    ),
}


@pytest.mark.parametrize("name", sorted(FROZEN_CHANGE_HASH_VECTORS))
def test_frozen_change_hash_vector(name: str) -> None:
    """Passes by design: pinned from an independent recompute, not from this code (#945). These
    are `diff-1` values, so they are recomputed under `diff-1` (#944 made `diff-2` the writer pin;
    the `diff-2` vectors are in `test_vm_diff2.py`)."""
    raw, expected = FROZEN_CHANGE_HASH_VECTORS[name]
    assert change_hash(raw, pin=DIFF1_PIN) == expected


def test_frozen_vectors_cover_every_case_of_945() -> None:
    assert set(FROZEN_CHANGE_HASH_VECTORS) == {
        "binary",
        "mode_change",
        "submodule",
        "symlink",
        "nonl",
        "crlf",
        "hex_line",
        "hex_path",
        "quoted_utf8_path",
    }
    assert len({h for _, h in FROZEN_CHANGE_HASH_VECTORS.values()}) == 9


def test_944_utf8_only_vectors_hash_the_same_under_diff2_and_the_hex_ones_differ() -> None:
    """`diff-2` changes only the encoding of bytes that are not UTF-8 (#944)."""
    for name, (raw, frozen_diff1) in FROZEN_CHANGE_HASH_VECTORS.items():
        if name in {"hex_line", "hex_path"}:
            assert change_hash(raw) != frozen_diff1, name
        else:
            assert change_hash(raw) == frozen_diff1, name


def test_context_lines_hunk_offsets_and_blob_ids_do_not_change_the_hash() -> None:
    moved = _file(
        "src/a.py",
        b"@@ -11,4 +31,4 @@ def f():\n ONE\n-two\n+TWO\n THREE\n FOUR\n",
        old_blob="c" * 40,
        new_blob="d" * 40,
    )
    assert change_hash(moved) == change_hash(SIMPLE)
    assert diff_hash(moved) != diff_hash(SIMPLE)


def test_hunks_are_flattened_in_file_order() -> None:
    two_hunks = _file(
        "src/a.py",
        b"@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n@@ -20,3 +20,3 @@\n x\n-y\n+Y\n z\n",
    )
    one_hunk = _file(
        "src/a.py",
        b"@@ -1,6 +1,6 @@\n one\n-two\n+TWO\n three\n x\n-y\n+Y\n z\n",
    )
    assert change_hash(two_hunks) == change_hash(one_hunk)
    assert norm(two_hunks)[0][4] == [
        ["-", "two"],
        ["+", "TWO"],
        ["-", "y"],
        ["+", "Y"],
    ]


def test_any_one_added_or_removed_line_changes_the_hash() -> None:
    base = change_hash(SIMPLE)
    extra_add = _file("src/a.py", b"@@ -1,4 +1,5 @@\n one\n-two\n+TWO\n+more\n three\n four\n")
    extra_del = _file("src/a.py", b"@@ -1,4 +1,3 @@\n one\n-two\n+TWO\n-three\n four\n")
    whitespace = _file("src/a.py", b"@@ -1,4 +1,4 @@\n one\n-two\n+TWO \n three\n four\n")
    assert len({base, change_hash(extra_add), change_hash(extra_del), change_hash(whitespace)}) == 4


def test_no_newline_marker_becomes_an_entry_after_its_line() -> None:
    raw = _file("f.txt", b"@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n")
    assert norm(raw)[0][4] == [["-", "old"], ["\\", "nonl"], ["+", "new"]]


def test_cr_is_part_of_the_line_and_crlf_is_not_normalised() -> None:
    crlf = _file("f.txt", b"@@ -1 +1 @@\n-old\r\n+new\r\n")
    lf = _file("f.txt", b"@@ -1 +1 @@\n-old\n+new\n")
    assert norm(crlf)[0][4] == [["-", "old\r"], ["+", "new\r"]]
    assert change_hash(crlf) != change_hash(lf)


def test_non_utf8_bytes_become_hex_entries() -> None:
    assert line_text(b"plain") == "plain"
    assert line_text("grün".encode()) == "grün"
    assert line_text(b"\xff\xfe") == "hex:fffe"
    raw = _file("f.txt", b"@@ -1 +1 @@\n-\xff\xfe\n+ok\n")
    assert norm(raw)[0][4] == [["-", "hex:fffe"], ["+", "ok"]]


def test_status_and_modes_for_added_deleted_and_mode_change() -> None:
    added = _file("n.sh", b"@@ -0,0 +1 @@\n+x\n", status="A", new_mode="100755")
    deleted = _file("o.txt", b"@@ -1 +0,0 @@\n-x\n", status="D")
    chmod = _file("s.sh", b"", old_mode="100644", new_mode="100755")
    assert norm(added) == [["n.sh", "A", None, "100755", [["+", "x"]]]]
    assert norm(deleted) == [["o.txt", "D", "100644", None, [["-", "x"]]]]
    assert norm(chmod) == [["s.sh", "M", "100644", "100755", []]]


def test_binary_file_is_its_new_blob_id_in_both_renders() -> None:
    literal = (
        f"diff --git a/img.png b/img.png\nnew file mode 100644\nindex {ZERO}..{NEW}\n"
        "GIT binary patch\nliteral 4\nLc${NkU|?mn0001&\n\nliteral 0\nHcmV?d00001\n\n"
    ).encode()
    differ = (
        f"diff --git a/img.png b/img.png\nindex {OLD}..{NEW} 100644\n"
        "Binary files a/img.png and b/img.png differ\n"
    ).encode()
    assert norm(literal) == [["img.png", "A", None, "100644", NEW]]
    assert norm(differ) == [["img.png", "M", "100644", "100644", NEW]]
    # a text file after the binary section is still parsed
    assert [f.path for f in parse_diff(literal + SIMPLE)] == ["img.png", "src/a.py"]


def test_quoted_paths_are_unquoted() -> None:
    raw = (
        f'diff --git "a/sp ace\\t\\303\\274.txt" "b/sp ace\\t\\303\\274.txt"\n'
        f"index {OLD}..{NEW} 100644\n"
        '--- "a/sp ace\\t\\303\\274.txt"\n+++ "b/sp ace\\t\\303\\274.txt"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
    ).encode()
    assert parse_diff(raw)[0].path == "sp ace\tü.txt"
    plain = _file("dir with space/x.txt", b"@@ -1 +1 @@\n-a\n+b\n")
    assert parse_diff(plain)[0].path == "dir with space/x.txt"


def test_hunk_counts_decide_where_a_hunk_ends() -> None:
    """A removed line whose text starts with `-- ` looks like a `---` header; counts, not the
    look of a line, decide what belongs to the hunk."""
    raw = _file("f.sql", b"@@ -1,2 +1,2 @@\n--- comment\n+-- comment\n x\n")
    assert norm(raw)[0][4] == [["-", "-- comment"], ["+", "-- comment"]]
    countless = _file("f.txt", b"@@ -1 +1 @@\n-a\n+b\n")
    assert norm(countless)[0][4] == [["-", "a"], ["+", "b"]]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (_file("f", b"@@ -1,3 +1,3 @@\n-a\n+b\n"), "hunk_truncated"),
        (_file("f", b"@@ -1 +1 @@\n-a\n+b\n?junk\n"), "unexpected_line"),
        (
            b"diff --git a/x b/y\nsimilarity index 90%\nrename from x\nrename to y\n",
            "rename_present",
        ),
        (
            f"diff --git a/f b/f\nindex {OLD[:7]}..{NEW[:7]} 100644\n--- a/f\n+++ b/f\n"
            "@@ -1 +1 @@\n-a\n+b\n".encode(),
            "index_not_full",
        ),
        (b"not a diff\n", "unexpected_line"),
        (_file("f", b"@@ bad header @@\n-a\n+b\n"), "unexpected_line"),
    ],
)
def test_malformed_input_is_refused_with_a_code_and_no_line_text(raw: bytes, code: str) -> None:
    with pytest.raises(DiffFormatError) as info:
        parse_diff(raw)
    assert info.value.code == code
    assert "a" * 7 not in str(info.value) and "junk" not in str(info.value)


def test_a_typechange_renders_as_delete_plus_add_for_the_same_path() -> None:
    raw = _file("link", b"@@ -1 +0,0 @@\n-content\n", status="D") + _file(
        "link", b"@@ -0,0 +1 @@\n+target\n", status="A", new_mode="120000"
    )
    doc = norm(raw)
    assert [(f[0], f[1], f[2], f[3]) for f in doc] == [
        ("link", "D", "100644", None),
        ("link", "A", None, "120000"),
    ]


# --- property test against a real git: a clean merge of main keeps the hash -------------------

GIT = shutil.which("git")


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = _git_env(root.parent)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, data: bool = False) -> bytes:
        assert GIT is not None
        out = subprocess.run(  # noqa: S603
            [GIT, "-c", "commit.gpgsign=false", *args],
            cwd=self.root,
            env=self.env,
            check=True,
            capture_output=True,
            timeout=60,
        )
        return out.stdout

    def write(self, name: str, lines: list[str]) -> None:
        (self.root / name).write_text("".join(f"{line}\n" for line in lines))

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").decode().strip()

    def render(self, head: str) -> bytes:
        base = self.git("merge-base", head, "main").decode().strip()
        pin = [arg for item in DIFF_PIN_CONFIG for arg in ("-c", item)]
        return self.git(*pin, *DIFF_COMMAND, base, head)


@pytest.mark.skipif(GIT is None, reason="git is needed to render diffs for the property test")
def test_property_clean_merge_of_main_keeps_the_hash_and_one_line_changes_it(
    tmp_path: Path,
) -> None:
    rng = random.Random(20261110)
    for case in range(12):
        root = tmp_path / f"r{case}"
        root.mkdir()
        repo = _Repo(root)
        size = rng.randint(20, 40)
        base_a = [f"a line {i}" for i in range(size)]
        base_b = [f"b line {i}" for i in range(10)]
        repo.write("a.txt", base_a)
        repo.write("b.txt", base_b)
        repo.commit("base")
        repo.git("checkout", "-q", "-b", "pr")
        # the PR edits a window in the middle of a.txt (removes some, adds some)
        start = rng.randint(8, size - 10)
        removed = rng.randint(0, 3)
        added = [f"pr add {case}-{i}" for i in range(rng.randint(1, 4))]
        pr_a = base_a[:start] + added + base_a[start + removed :]
        repo.write("a.txt", pr_a)
        h1 = repo.commit("pr change")
        raw1 = repo.render(h1)
        # main moves: edits far from the PR window in a.txt plus a new file
        repo.git("checkout", "-q", "main")
        main_a = list(base_a)
        main_a = ["main top 1", "main top 2"] + main_a if rng.random() < 0.8 else main_a
        main_a = main_a + ["main bottom"] if rng.random() < 0.8 else main_a
        repo.write("a.txt", main_a)
        repo.write("c.txt", [f"c {case}"])
        repo.commit("main moves")
        repo.git("checkout", "-q", "pr")
        repo.git("merge", "-q", "--no-edit", "main")
        h2 = repo.git("rev-parse", "HEAD").decode().strip()
        raw2 = repo.render(h2)
        assert raw1 != raw2, "the raw render moved (offsets or blob ids)"
        assert change_hash(raw1) == change_hash(raw2), f"case {case}: clean merge changed the hash"
        # one more added line (or one removed added line) changes it
        merged_a = (repo.root / "a.txt").read_text().splitlines()
        if rng.random() < 0.5:
            merged_a.insert(merged_a.index(added[0]), f"pr extra {case}")
        else:
            merged_a.remove(added[0])
        repo.write("a.txt", merged_a)
        h3 = repo.commit("one line")
        assert change_hash(repo.render(h3)) != change_hash(raw2), f"case {case}: edit kept the hash"


@pytest.mark.skipif(GIT is None, reason="git is needed to render diffs for the regression test")
def test_vm1_f2_marker_after_a_context_line_is_not_attached_to_the_previous_change(
    tmp_path: Path,
) -> None:
    """Fable M2: a file without a final newline; the PR changes line 8; main inserts lines before
    line 10; a clean merge of main must not change the change hash."""
    repo = _Repo(tmp_path)
    lines = [f"line {i}" for i in range(1, 11)]
    (repo.root / "f.txt").write_text("\n".join(lines))  # no final newline
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "pr")
    pr = list(lines)
    pr[7] = "line 8 changed"
    (repo.root / "f.txt").write_text("\n".join(pr))
    h1 = repo.commit("pr change")
    raw1 = repo.render(h1)
    repo.git("checkout", "-q", "main")
    main = lines[:9] + [f"main {i}" for i in range(5)] + lines[9:]
    (repo.root / "f.txt").write_text("\n".join(main))
    repo.commit("main moves")
    repo.git("checkout", "-q", "pr")
    repo.git("merge", "-q", "--no-edit", "main")
    raw2 = repo.render(repo.git("rev-parse", "HEAD").decode().strip())
    assert raw1 != raw2
    assert change_hash(raw1) == change_hash(raw2)


def test_vm1_f2_marker_is_kept_only_after_a_removed_or_added_line() -> None:
    def one(body: bytes) -> Any:
        head = f"diff --git a/f b/f\nindex {OLD}..{NEW} 100644\n--- a/f\n+++ b/f\n".encode()
        return norm(head + body)

    after_context = one(b"@@ -1,2 +1,2 @@\n-x\n+y\n z\n\\ No newline at end of file\n")
    assert ["\\", "nonl"] not in after_context[0][4]
    after_added = one(b"@@ -1,2 +1,2 @@\n x\n-z\n+y\n\\ No newline at end of file\n")
    assert after_added[0][4][-1] == ["\\", "nonl"]
