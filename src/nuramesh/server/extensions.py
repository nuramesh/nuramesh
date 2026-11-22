"""What the API gives the routes an installed plugin adds (ND72).

A plugin registers `PluginContext.add_routes(install, rules)`; `create_app` calls `install(kit)`
with a `RouteKit` and the plugin declares its routes on `kit.app` with the helpers the open routes
use, so a plugin route answers, authorizes and scopes exactly like an open one. Every plugin route
needs a rule (`RouteRule`): the route table check refuses a server whose routes and rules differ.
Every plugin route also passes `operation_id=` (verb-first snake_case, such as `get_toy`): it
is not in the core table, so the route table check refuses an id that is ill formed, used by a
core route or shared by two plugin routes.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

from fastapi import FastAPI

from nuramesh.engine.content import ContentService
from nuramesh.plugins import PluginError
from nuramesh.stores import Store


class NamespaceBound:
    """A store (or unit of work) as one plugin sees it: everything is the real thing, except that
    the plugin-record methods work on the plugin's own namespaces only (`PluginError` for any
    other) and `ext_erase` (the organization key's business) is not offered.

    This is a guardrail against a mistake in a trusted, in-process plugin, NOT an isolation
    boundary: a plugin runs in the server's process and can reach the raw store (`_inner`,
    `kit.content.store`). Only install plugins you trust."""

    def __init__(self, inner: Any, namespaces: frozenset[str]) -> None:
        self._inner = inner
        self._namespaces = namespaces

    def _own(self, namespace: str) -> None:
        if namespace not in self._namespaces:
            raise PluginError(f"this plugin does not own the record namespace '{namespace}'")

    def ext_get(self, org_id: str, namespace: str, kind: str, record_id: str) -> Any:
        self._own(namespace)
        return self._inner.ext_get(org_id, namespace, kind, record_id)

    def ext_find(self, org_id: str, namespace: str, kind: str, unique_key: str) -> Any:
        self._own(namespace)
        return self._inner.ext_find(org_id, namespace, kind, unique_key)

    def ext_list(self, org_id: str, namespace: str, kind: str, group_key: str) -> Any:
        self._own(namespace)
        return self._inner.ext_list(org_id, namespace, kind, group_key)

    def ext_put(self, record: Any) -> None:
        self._own(record.namespace)
        self._inner.ext_put(record)

    def ext_erase(self, org_id: str) -> int:
        raise PluginError("plugin records are erased with the organization's key, not by a plugin")

    @contextmanager
    def transaction(self) -> Iterator["NamespaceBound"]:
        with self._inner.transaction() as uow:
            yield NamespaceBound(uow, self._namespaces)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def bound_store(store: Store, namespaces: frozenset[str]) -> Store:
    """`store` as a plugin with these record namespaces sees it."""
    return cast(Store, NamespaceBound(store, namespaces))


@dataclass(frozen=True)
class RouteKit:
    """The app, its store and the dependencies the open routes declare their parameters with.

    `org_of` is the dependency of the caller's organization (or None), `human` the one of a
    signed-in person's id (a token is refused), `caller` the dependency of the `Caller`, `problem`
    the error class every route raises for a refusal (`problem(status, code, message)`),
    `need_run` the run-visibility check (raises the shared 404), `is_org` tells a real
    organization id from the local inspector's.
    `store` is bound to the plugin's own record namespaces (`PluginContext.add_records`). That
    binding is a guardrail for trusted in-process plugins, not an isolation boundary: a plugin
    can still reach the raw store (`kit.content.store`, `kit.store._inner`).
    `settings` holds the plugin settings built by `nuramesh serve`, by namespace."""

    app: FastAPI
    store: Store
    content: ContentService | None
    settings: Mapping[str, Any]
    caller: Callable[..., Any]
    org_of: Callable[..., Any]
    human: Callable[..., Any]
    problem: Callable[..., Exception]
    need_run: Callable[[str | None, str], None]
    is_org: Callable[[Any], bool]
