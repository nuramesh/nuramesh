"""The plugin seam (ND72): the one place the open core meets commercial packages.

A package declares an entry point in the group `nuramesh.plugins` that points at a function
`register(context)`. The open core asks the group, never a package name, so it works the same
with no plugin installed. Loading fails closed: a plugin that cannot be loaded or raises stops
startup with its entry-point name, never silently skipped (a missing licence check must not look
like a free install).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import entry_points
from typing import Any

from nuramesh.core.entitlements import Entitlements, NoEntitlements
from nuramesh.licence import UNCONFIGURED, LicenceReport, load_licence

PLUGIN_GROUP = "nuramesh.plugins"


class PluginError(Exception):
    """A plugin could not be loaded or registered."""


@dataclass(frozen=True)
class RouteRule:
    """The authorization row and the organization class of one route a plugin adds: a
    `nuramesh.core.auth.Permission` value (`read`, `run`, `decide`, `reveal` or `admin`) and a
    route class of `nuramesh.server.routes` (`server`, `run`, `task`, `stream`, ...)."""

    permission: str
    scope: str


CliInstaller = Callable[[Any], None]
"""`install(subparsers)`: add commands to the `nuramesh` parser (an argparse sub-parsers action)."""
RouteInstaller = Callable[[Any], None]
"""`install(kit)`: add routes to the app (`nuramesh.server.extensions.RouteKit`)."""
SettingsFactory = Callable[[Any], Any]
"""`factory(args)`: the plugin's settings for `nuramesh serve`, from the parsed arguments."""


SecretStoreFactory = Callable[[Mapping[str, str]], Any]
"""`factory(env)`: a `nuramesh.core.secret_store.SecretStore` built from plain settings (R1.14c)."""


class PluginContext:
    """What a plugin may change. Grows with each hook a commercial feature needs.

    `entitlements` is read-only: only `set_entitlements` changes it, and only once (a verified
    licence file counts as that once: it is set by `licence` before any plugin registers)."""

    def __init__(self) -> None:
        self._entitlements: Entitlements = NoEntitlements()
        self._set_by: str | None = None
        self._licence: LicenceReport = UNCONFIGURED
        self._current: str | None = None
        self._loaded: tuple[str, ...] = ()
        self._cli: list[CliInstaller] = []
        self._routes: list[RouteInstaller] = []
        self._route_owner: dict[int, str | None] = {}
        self._namespaces: dict[str | None, set[str]] = {}
        self._claimed: dict[str, str | None] = {}
        self._rules: dict[tuple[str, str], RouteRule] = {}
        self._denials: dict[tuple[str, str], Any] = {}
        self._settings: dict[str, SettingsFactory] = {}
        self._secret_stores: dict[str, SecretStoreFactory] = {}

    @property
    def entitlements(self) -> Entitlements:
        """The entitlement source (the default grants nothing)."""
        return self._entitlements

    @property
    def licence(self) -> LicenceReport:
        """What the licence loader found (`notice` is the one stderr line, or None)."""
        return self._licence

    @property
    def loaded(self) -> tuple[str, ...]:
        """Names of the plugins that registered, in load order."""
        return self._loaded

    @property
    def cli_installers(self) -> tuple[CliInstaller, ...]:
        """The command installers, in load order."""
        return tuple(self._cli)

    @property
    def route_installers(self) -> tuple[RouteInstaller, ...]:
        """The route installers, in load order."""
        return tuple(self._routes)

    @property
    def route_rules(self) -> Mapping[tuple[str, str], RouteRule]:
        """The authorization rows of the routes plugins add, by `(method, path template)`."""
        return dict(self._rules)

    @property
    def denial_hooks(self) -> Mapping[tuple[str, str], Any]:
        """The refusal notes by route (`nuramesh.server.authz.DenialHook`)."""
        return dict(self._denials)

    @property
    def settings_factories(self) -> Mapping[str, SettingsFactory]:
        """The server settings factories by plugin namespace."""
        return dict(self._settings)

    @property
    def secret_store_factories(self) -> Mapping[str, SecretStoreFactory]:
        """The secret stores plugins offer, by the name `NURAMESH_SECRET_STORE` selects."""
        return dict(self._secret_stores)

    def add_secret_store(self, name: str, factory: SecretStoreFactory) -> None:
        """Offer a secret store under `name` (R1.14c). `file` and `fake` belong to the open core."""
        if name in ("", "file", "fake") or name in self._secret_stores:
            raise PluginError(f"plugin '{self._current}': secret store '{name}' cannot be added")
        self._secret_stores[name] = factory

    def add_commands(self, install: CliInstaller) -> None:
        """Add commands to the `nuramesh` command line."""
        self._cli.append(install)

    def add_routes(
        self, install: RouteInstaller, rules: Mapping[tuple[str, str], RouteRule]
    ) -> None:
        """Add API routes. Every route needs a rule here (default deny): the server refuses to
        start when a served route has none, or a rule has no route."""
        clash = set(rules) & set(self._rules)
        if clash:
            raise PluginError(f"plugin '{self._current}': routes already added: {sorted(clash)}")
        self._routes.append(install)
        self._route_owner[len(self._routes) - 1] = self._current
        self._rules.update(rules)

    def add_records(self, namespace: str) -> None:
        """Claim a namespace of the store's plugin records (`ExtensionRecord.namespace`). The
        routes of this plugin read and write records of its own namespaces only; one namespace
        belongs to one plugin."""
        owner = self._claimed.get(namespace, self._current)
        if namespace in self._claimed and owner != self._current:
            raise PluginError(f"plugin '{self._current}': namespace '{namespace}' is taken")
        self._claimed[namespace] = self._current
        self._namespaces.setdefault(self._current, set()).add(namespace)

    def namespaces_of(self, install: RouteInstaller) -> frozenset[str]:
        """The record namespaces of the plugin that added this route installer."""
        for index, candidate in enumerate(self._routes):
            if candidate is install:
                return frozenset(self._namespaces.get(self._route_owner[index], ()))
        return frozenset()

    def add_denial_hook(self, method: str, path: str, hook: Any) -> None:
        """Be told who was refused on one route: `hook(store, org, actor, code, http, params)`,
        like `nuramesh.server.authz.DenialHook` with the store first."""
        if (method, path) in self._denials:
            raise PluginError(f"plugin '{self._current}': a denial hook for that route exists")
        self._denials[(method, path)] = hook

    def add_settings(self, namespace: str, factory: SettingsFactory) -> None:
        """Settings the plugin's routes read, built by `nuramesh serve` from its arguments."""
        if namespace in self._settings:
            raise PluginError(f"plugin '{self._current}': settings '{namespace}' already added")
        self._settings[namespace] = factory

    def set_entitlements(self, entitlements: Entitlements) -> None:
        """Supply the entitlement source. Only one plugin may do it, and it must be an object
        with an `is_granted(key)` method (anything else fails at startup, not at first use)."""
        if isinstance(entitlements, type) or not callable(
            getattr(entitlements, "is_granted", None)
        ):
            raise PluginError(
                f"plugin '{self._current}': entitlements must be an object with is_granted(key)"
            )
        if self._set_by is not None:
            raise PluginError(
                f"plugin '{self._current}': entitlements were already set by '{self._set_by}'"
            )
        self._entitlements = entitlements
        self._set_by = self._current


def load_plugins(
    env: Mapping[str, str] | None = None, clock: Callable[[], datetime] | None = None
) -> PluginContext:
    """Read the licence file (if one is configured), then load every installed plugin in name
    order and return what they registered. A refused licence never raises: the context keeps
    `NoEntitlements` and `context.licence.notice` says why.
    `env` and `clock` default to the process environment and the UTC clock (tests inject both)."""
    context = PluginContext()
    report, source = load_licence(env, clock)
    context._licence = report
    if source is not None:
        context._entitlements = source
        context._set_by = "licence"
    loaded: list[str] = []
    try:
        points = sorted(entry_points(group=PLUGIN_GROUP), key=lambda p: p.name)
    except Exception as exc:
        raise PluginError(f"the plugin list could not be read: {exc}") from exc
    for point in points:
        context._current = point.name
        try:
            register = point.load()
        except Exception as exc:
            raise PluginError(f"plugin '{point.name}' could not be loaded: {exc}") from exc
        if not callable(register):
            raise PluginError(f"plugin '{point.name}' is not callable")
        try:
            register(context)
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError(f"plugin '{point.name}' failed to register: {exc}") from exc
        loaded.append(point.name)
    context._current = None
    context._loaded = tuple(loaded)
    return context
