"""The published OpenAPI text carries no internal references (contract freeze, #894).

Route docstrings become operation descriptions and model docstrings become schema descriptions,
and they cite task ids, decision numbers and release names (`R1.34`, `ND320`, `T56`, `E19`,
`A85`). Those words mean nothing to a user and would leak the project's bookkeeping into the
public schema and the generated SDK docs, so `scrub_internal_references` removes them from every
`summary`, `description` and `title` of the served schema. A parenthesis that holds only
references goes with its parentheses; a reference inside a prose parenthesis goes alone; a bare
token goes with the space before it. Nothing else is touched, and
`tests/server/test_openapi_public_text.py` fails on any token left in the served or committed file.
"""

import re
from typing import Any, Final

# release names (R1.34, R2.08b), decisions (ND320), tasks (T56), open decisions (OD-9e), pull
# requests (#707), spec rows (E19, A85, D5, H11, C82, SA-21, MC6). A word boundary on both sides:
# `E2E` is text.
INTERNAL_REFERENCE: Final = re.compile(
    r"\b(?:R\d\.\d+[a-z]?|R\d+[a-z]?|ND\d+|T\d{2,3}|OD-\d+[a-z]?|#\d{3,4}|[EADHC]\d{1,3}|(?:SA|MC|GC|PA)-?\d{1,3})\b(?![-A-Za-z])"
)
# a pointer into the spec folder: "(spec R8a)", "(spec 6 step 3)", "(spec/steps/README.md, ...)"
SPEC_POINTER: Final = re.compile(r"\s*\(spec(?:\s|/)[^()]*\)")
_SEP: Final = r"(?:\s*(?:,|;|and|/)\s*|\s+)"
# a parenthesis whose content is references only (with optional "spec", "section N", "review" words)
_REF_PAREN: Final = re.compile(
    r"\s*\((?:(?:spec|section|review|round|part|see)\s+)*"
    + INTERNAL_REFERENCE.pattern
    + r"(?:"
    + _SEP
    + r"(?:(?:spec|section|review|round|part|see)\s+)*"
    + r"(?:"
    + INTERNAL_REFERENCE.pattern
    + r"|\d+(?:\.\d+)*)"
    + r")*\)"
)
# a reference at the end of a prose parenthesis: ", ND45)" -> ")"
_TRAILING_IN_PAREN: Final = re.compile(
    _SEP + INTERNAL_REFERENCE.pattern + r"(?:" + _SEP + INTERNAL_REFERENCE.pattern + r")*(?=\))"
)
# a reference at the start of a prose parenthesis: "(E19 spec ..." is not seen today; a bare one
_BARE: Final = re.compile(r"\s*" + INTERNAL_REFERENCE.pattern)
_LITERAL_KEYS: Final = frozenset({"example", "examples", "default", "enum", "const"})
TEXT_KEYS: Final = frozenset({"summary", "description", "title"})
# a create declares 201 and this 200: the same request found the record already there
ALREADY_200: Final[dict[int | str, dict[str, Any]]] = {
    200: {"description": "Already recorded: the same request found the record there"}
}


def scrub_internal_references(text: str) -> str:
    """The text with every internal reference removed, prose and punctuation kept."""
    out = SPEC_POINTER.sub("", text)
    out = _REF_PAREN.sub("", out)
    out = _TRAILING_IN_PAREN.sub("", out)
    out = _BARE.sub("", out)
    return re.sub(r" +([.,;:)])", r"\1", out)


def scrub_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Scrubs the text members of a schema in place (nested objects and lists included)."""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _LITERAL_KEYS:
                    continue  # sample and default values are data, not prose
                if key in TEXT_KEYS and isinstance(value, str):
                    node[key] = scrub_internal_references(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema
