"""The fixed words for the two price conditions (price spec section 1, #1450).

Two conditions, two phrases, kept apart everywhere (CLI, API, app, docs):

- **No price**: the model has no price, so a call to it is blocked before anything is sent and
  nothing is reserved or charged.
- **Cost unknown**: a price exists but the provider's answer had no usable usage (or a token kind
  without a rate); the call ran, the reserved amount was charged and the cost is recorded as
  unknown, never as zero.

Words only: no I/O, no money arithmetic. The CLI reads these; the app keeps the same two phrases in
`apps/app/src/models/words.ts` and a test (`tests/test_price_words_tie.py`) fails if they differ.
"""

NO_PRICE_BLOCKED = "No price: calls to this model are blocked"
COST_UNKNOWN = "Cost unknown (charged the reservation)"
COST_UNKNOWN_SHORT = "cost unknown"


def cost_unknown_call_words(charged: str | None) -> str:
    """One call whose cost is unknown: `cost unknown (charged the reservation: $0.002)`. With no
    charged amount known the amount part is left out; it is never shown as zero."""
    if charged is None:
        return f"{COST_UNKNOWN_SHORT} (charged the reservation)"
    return f"{COST_UNKNOWN_SHORT} (charged the reservation: ${charged})"


def at_least_words(total: str) -> str:
    """A total that holds at least one unknown cost: `at least $0.0018` (never a bare total)."""
    return f"at least ${total}"


__all__ = [
    "COST_UNKNOWN",
    "COST_UNKNOWN_SHORT",
    "NO_PRICE_BLOCKED",
    "at_least_words",
    "cost_unknown_call_words",
]
