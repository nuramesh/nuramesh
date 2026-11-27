"""The token and cache report (R1.34 Part A, sections 3 and 6): one function
for the route and for `nuramesh savings tokens`, so both answer with the same bytes.

A **read**: it takes the facts from the ledger through `cost_reader` (the same bound and the same
`as_of` rules as the cost views) and does arithmetic in `core.token_view`. It writes no ledger entry
and no store row, calls no network and holds no prompt, answer or hash (spec section 7). It does not
need the `server` extra, so the CLI can use it.
"""

from datetime import UTC, datetime

from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.costs import CURRENCY
from nuramesh.core.token_view import token_rows
from nuramesh.server import cost_reader
from nuramesh.stores.base import Store

GROUP_BYS = ("none", "provider", "model", "role")


def iso(moment: datetime) -> str:
    """UTC text with a `Z`, with microseconds only when there are some."""
    utc = moment.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    return text + (f".{utc.microsecond:06d}" if utc.microsecond else "") + "Z"


def instant(text: str, name: str) -> datetime:
    """A UTC instant from request text; anything else is a 422 `invalid_request`."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise cost_reader.CostReadError(
            422, "invalid_request", f"{name} must be a UTC instant"
        ) from None
    if moment.tzinfo is None:
        raise cost_reader.CostReadError(
            422, "invalid_request", f"{name} needs a UTC offset (for example Z)"
        )
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        raise cost_reader.CostReadError(422, "invalid_request", f"{name} is out of range") from None


def window(
    now: datetime, from_: str | None, to: str | None, as_of: str | None
) -> tuple[datetime, datetime, datetime]:
    """`(start, end, as_of)`: the period defaults to the current month, `as_of` to now."""
    month = datetime(now.year, now.month, 1, tzinfo=UTC)
    following = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    start = instant(from_, "from") if from_ else month
    end = instant(to, "to") if to else following
    if not start < end:
        raise cost_reader.CostReadError(422, "invalid_request", "from must be before to")
    return start, end, instant(as_of, "as_of") if as_of else now


def token_report(
    store: Store,
    config: NuraMeshConfig,
    *,
    group_by: str = "none",
    from_: str | None = None,
    to: str | None = None,
    as_of: str | None = None,
) -> dict[str, object]:
    """The Part A answer: `{organization, currency, as_of, from, to, group_by, rows}`.

    Raises `cost_reader.CostReadError` (422 `invalid_request` or `range_too_large`, 409
    `damaged_run`, ...) for what cannot be answered correctly; never a truncated figure."""
    if group_by not in GROUP_BYS:
        raise cost_reader.CostReadError(
            422, "invalid_request", "group_by must be none, provider, model or role"
        )
    start, end, now = window(store.now(), from_, to, as_of)
    runs = cost_reader.read_org_runs(store, config.org.id, start=start, end=end, as_of=now)
    calls = [t for t in cost_reader.token_facts_of(runs) if start <= t.call.at < end]
    roles = {a.id: a.role for a in config.org.agents}
    return {
        "organization": config.org.id,
        "currency": CURRENCY,
        "as_of": iso(now),
        "from": iso(start),
        "to": iso(end),
        "group_by": group_by,
        "rows": token_rows(calls, roles, group_by),
    }
