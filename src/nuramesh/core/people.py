"""How people appear in the ledger: an opaque id, never an email or a name (T42).

The ledger is append-only and hash-chained, so whatever is written into it can never be erased.
People therefore appear only as `human:<opaque id>`, for example `human:u_7k2x9q`. The id carries
no personal data; the mapping from id to email and display name lives in a **mutable user table**
outside the ledger (R1.22). Erasing a person means overwriting their row in that table: every
ledger entry keeps its hash, and the chain still verifies, but the entry no longer points at a
name.

The pattern allows letters, digits, `_` and `-` (up to 64 characters). It deliberately refuses `@`
and `.`, so an email address, or a `first.last` name, can't be written by mistake. (It can't stop a
person from choosing a name-like opaque id; ids should be generated, not chosen.)
"""

import re

HUMAN_ID_BODY = r"[A-Za-z0-9_-]{1,64}"
"""The id part after `human:`."""

HUMAN_ID_PATTERN = rf"^human:{HUMAN_ID_BODY}$"
"""A person's actor id."""

_HUMAN = re.compile(HUMAN_ID_PATTERN)


def is_human_id(value: str) -> bool:
    """Whether `value` is a well-formed person actor id (`human:<opaque id>`)."""
    return _HUMAN.fullmatch(value) is not None
