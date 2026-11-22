"""`GET /v1/savings/tokens` (R1.34 Part A, `spec/token-savings` section 6): tokens by kind, the
cache hit rate and what the cache saved, per provider, model or role.

Read only: no body, no ledger entry, no store write, the caller's organization only. The arithmetic
is `nuramesh.core.token_view`; `token_report` takes the facts from the ledger, like the cost views.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query

from nuramesh.core.config import NuraMeshConfig
from nuramesh.server import cost_reader
from nuramesh.server.operation_ids import operation_id_for
from nuramesh.server.token_report import token_report
from nuramesh.stores.base import Store

Problem = Callable[..., Exception]


def install_savings(
    app: FastAPI,
    store: Store,
    need_config: Callable[[], NuraMeshConfig],
    problem: Problem,
    dependencies: Sequence[Any] = (),
) -> None:
    """Add the savings route. `problem(status, code, detail)` builds the API's error."""
    router = APIRouter(
        dependencies=list(dependencies), generate_unique_id_function=operation_id_for
    )
    Config = Annotated[NuraMeshConfig, Depends(need_config)]
    Text = Annotated[str | None, Query(max_length=64)]

    @router.get("/v1/savings/tokens")
    def tokens(
        cfg: Config,
        group_by: Annotated[str, Query(pattern=r"^(none|role|provider|model)$")] = "none",
        from_: Annotated[Text, Query(alias="from")] = None,
        to: Text = None,
        as_of: Text = None,
    ) -> dict[str, Any]:
        """Token and cache figures for `[from, to)`: one row per group, exact sums, unknown stays
        unknown."""
        try:
            return dict(
                token_report(store, cfg, group_by=group_by, from_=from_, to=to, as_of=as_of)
            )
        except cost_reader.CostReadError as exc:
            raise problem(exc.status, exc.code, exc.detail) from None

    # plain routes, not `include_router` (see `costs_api`)
    app.router.routes.extend(router.routes)


__all__ = ["install_savings"]
