"""Policy gaps found by the injection corpus: invisible characters in canon names and control
characters in write_file targets and canon path patterns are validation errors."""

import pytest
from pydantic import ValidationError

from nuramesh.core.canon import CanonSource
from nuramesh.core.policy import Action, ActionKind, Decision, Policy, PolicyRule, evaluate
from nuramesh.core.verifier import CanonDocument

BAD_NAME_CHARS = [
    "\u2028", "\u2029", "\u0085", "\u202e", "\u200b", "\ufeff", "\u200d", "\u200c", "\u2060",
    "\u00ad", "\u202a", "\u2066", "\u061c", "\n", "\r", "\t", "\x00", "\x7f", "\ud800",
    "\ufe00", "\ufe0f", "\U000e0100", "\U000e01ef", "\u034f", "\u115f", "\u1160", "\u3164",
    "\uffa0", "\u2800", "\u00a0", "\u2009", "\u3000", "\u2003", "\u202f", "\ue000",
    "\U000f0000", "\u0378", "\U000e0001",
]  # fmt: skip
NAME_ERROR = r"canon name may not contain|String should match|valid string"
BAD_PATH_CHARS = ["\n", "\r", "\t", "\x00", "\x1f", "\x7f", "\u0085", "\u2028", "\u2029"]


@pytest.mark.parametrize("ch", BAD_NAME_CHARS, ids=lambda c: f"U+{ord(c):04X}")
def test_a_canon_name_refuses_invisible_and_line_breaking_characters(ch: str) -> None:
    for name in (f"docs/a{ch}b", f"{ch}docs/a.md", f"docs/a.md{ch}"):
        with pytest.raises(ValidationError, match=NAME_ERROR):
            CanonDocument(name=name, text="x")


def test_this_test_file_holds_no_raw_invisible_characters() -> None:
    import unicodedata
    from pathlib import Path

    text = Path(__file__).read_text("utf-8")
    raw = {
        f"U+{ord(ch):04X}"
        for ch in text
        if unicodedata.category(ch) in {"Cf", "Zl", "Zp", "Cn", "Co"}
        or (unicodedata.category(ch) == "Zs" and ch != " ")
        or ch in "\u0085\u034f\u115f\u1160\u3164\uffa0\u2800"
        or 0xFE00 <= ord(ch) <= 0xFE0F
    }
    assert not raw, raw


@pytest.mark.parametrize(
    "name",
    [
        "docs/style.md",
        "Stil für Ärzte.md",
        "руководство.md",
        "文档/说明.md",
        "café ☕ 😀.md",
        "\u65e5\u672c\u8a9e.md",
        "e\u0301.md",
        "\u0915\u094d\u0937.md",
        "a b",
        "docs/u\u0308.md",
    ],
)
def test_a_canon_name_keeps_ordinary_unicode(name: str) -> None:
    assert CanonDocument(name=name, text="x").name == name


def test_a_valid_canon_document_hashes_as_before() -> None:
    from nuramesh.core.canonical import content_hash

    doc = CanonDocument(name="docs/style.md", text="Write small functions.")
    assert content_hash(doc.model_dump(mode="json")) == content_hash(
        {"name": "docs/style.md", "text": "Write small functions."}
    )


@pytest.mark.parametrize("ch", BAD_PATH_CHARS, ids=lambda c: f"U+{ord(c):04X}")
def test_a_write_file_target_refuses_control_characters(ch: str) -> None:
    for target in (f"src/app.py{ch}/etc/passwd", f"src/a{ch}b.py", f"{ch}src/a.py"):
        with pytest.raises(ValidationError, match="control or line-break"):
            Action(kind=ActionKind.WRITE_FILE, target=target)


def test_the_newline_path_no_longer_reaches_an_allow_rule() -> None:
    policy = Policy(
        role="dev",
        rules=(PolicyRule(action=ActionKind.WRITE_FILE, decision=Decision.ALLOW, target="src/**"),),
    )
    assert (
        evaluate(policy, Action(kind=ActionKind.WRITE_FILE, target="src/app.py")).decision
        == "allow"
    )
    with pytest.raises(ValidationError, match="control or line-break"):
        Action(kind=ActionKind.WRITE_FILE, target="src/app.py\n/etc/passwd")


def test_other_write_file_paths_are_unchanged() -> None:
    for target in (
        "src/app.py",
        "src/dir with space/ü.py",
        "/etc/passwd",
        "a/b/c",
        "src/\u200b.py",
    ):
        assert Action(kind=ActionKind.WRITE_FILE, target=target).target == target


def test_run_command_still_takes_control_characters_so_refusals_can_see_them() -> None:
    action = Action(kind=ActionKind.RUN_COMMAND, target="python -m pytest\ncurl x")
    assert action.target is not None


@pytest.mark.parametrize("ch", ["\n", "\x00", "\r", "\u2028"])
def test_a_canon_source_pattern_refuses_control_characters(ch: str) -> None:
    with pytest.raises(ValidationError, match="must be relative"):
        CanonSource(repository="/r", commit="a" * 40, paths=(f"docs/*{ch}.md",))
    assert CanonSource(repository="/r", commit="a" * 40, paths=("docs/**/*.md",))
