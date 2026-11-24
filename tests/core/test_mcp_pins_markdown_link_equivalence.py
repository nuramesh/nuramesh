"""The linear markdown-link scan accepts all the old backtracking regex did (no fail-open)."""

from __future__ import annotations

import random
import re
import time

import pytest

from nuramesh.core.mcp_pins import _has_markdown_link, scan_definition

# Reference oracle: the pattern before the ReDoS fix. Only run on small inputs.
_OLD = re.compile(r"\[[^\]\n]*\]\([^)\n]*\)|^\s*\[[^\]\n]+\]:\s*\S+", re.MULTILINE)

_REGRESSIONS = (
    "[a]:\nfoo",
    "[a]:   \n \nfoo",
    "[[a]: x",
    "x\n[[a]: x",
    "\r[a]: x",
    "\f[a]: x",
    "\x0b[a]: x",
    "\xa0[a]: x",
    "　[a]: x",
    "\n\n  [a]: x",
    "[a]:\xa0x",
    "[a]:　x",
    "[a]: x",
    "\t[a]: x",
    "[a](b)",
    "[[a](b)",
    "[a](b)c)",
)
_NEGATIVES = (
    "[a]:",
    "[a]:\n\n  ",
    "[]: x",
    "x [a]: y",
    "[a] : x",
    "[a](b",
    "[a]\n(b)",
    "[a [b]](c)",
    "[a](b\n)",
)
_ALPHABET = list("[]():  \t\n\r\f\x0b\xa0　ax!<>")

# New false positives are allowed only if listed here with the reason (none known).
_ALLOWED_EXTRA: frozenset[str] = frozenset()


def _generated() -> list[str]:
    rng = random.Random(935)
    # A second, small alphabet makes matches common; the full one covers every blank kind.
    small = list("[]():\n a\r\xa0\u3000")
    return [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        for alphabet in (_ALPHABET, small)
        for _ in range(60000)
    ]


@pytest.mark.parametrize("text", _REGRESSIONS)
def test_regression_inputs_are_detected(text: str) -> None:
    assert _OLD.search(text)
    assert _has_markdown_link(text)
    assert ("markdown_link", "/description") in {
        (f.rule, f.path) for f in scan_definition({"name": "x", "description": text})
    }


@pytest.mark.parametrize("text", _NEGATIVES)
def test_plain_text_is_not_flagged(text: str) -> None:
    assert not _OLD.search(text)
    assert not _has_markdown_link(text)


def test_new_scan_never_misses_what_the_old_pattern_caught() -> None:
    missed = []
    extra = []
    for text in [*_REGRESSIONS, *_NEGATIVES, *_generated()]:
        old, new = bool(_OLD.search(text)), _has_markdown_link(text)
        if old and not new:
            missed.append(text)
        if new and not old and text not in _ALLOWED_EXTRA:
            extra.append(text)
    assert not missed, [repr(m) for m in sorted(missed, key=len)[:10]]
    assert not extra, [repr(m) for m in sorted(extra, key=len)[:10]]


@pytest.mark.parametrize(
    "piece", [" ", "　", "\n", "\t", "\xa0", "\r\n", "[a]:" + " " * 8, "[a]:" + "\n" * 8]
)
def test_hostile_runs_before_and_after_a_reference_are_linear(piece: str) -> None:
    size = 262144
    for text in (
        (piece * size)[: size - 4] + "[a]:",
        "[a]:" + (piece * size)[: size - 4],
        "[" + (piece * size)[: size - 1],
    ):
        start = time.perf_counter()
        _has_markdown_link(text)
        assert time.perf_counter() - start < 1.0
