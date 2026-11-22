"""Which secret store a process uses (R1.14c, spec 4.2 and 7, VK67): the plain setting
`NURAMESH_SECRET_STORE` names it.

* unset: the managed file store when `NURAMESH_SECRET_DIR` is set, else no store (stored keys off);
* `file`: the file store, `NURAMESH_SECRET_DIR` required;
* `fake`: an in-memory store for local trials (keys vanish with the process);
* any other name: a store a plugin registered under that name (the Azure Key Vault adapter is
  `azure-keyvault`, registered by a commercial plugin).

A hosted deployment (`NURAMESH_HOSTED=1`) must name a plugin store: `fake`, the file store and no
store at all are refused, so a hosted `stored` connection can never sit on a store that loses or
exposes its keys. Every refusal is a `SecretStoreError` with a fixed detail word: never a path,
never the value of a setting. Stdlib and the open core only (no Azure here)."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from nuramesh.core.secret_store import FakeSecretStore, SecretStore, SecretStoreError
from nuramesh.hosted import HostedConfigError, hosted_from_env

SECRET_STORE_ENV = "NURAMESH_SECRET_STORE"
SECRET_DIR_ENV = "NURAMESH_SECRET_DIR"
StoreFactory = Callable[[Mapping[str, str]], SecretStore]


def _refuse(code: str, detail: str) -> SecretStoreError:
    return SecretStoreError(code, detail)


def _installed_factories() -> Mapping[str, StoreFactory]:
    """The stores installed plugins registered (loaded only when the setting needs one)."""
    from nuramesh.plugins import PluginError, load_plugins

    try:
        return load_plugins().secret_store_factories
    except PluginError:
        raise _refuse("unavailable", "plugin") from None


def select_secret_store(
    env: Mapping[str, str], *, factories: Mapping[str, StoreFactory] | None = None
) -> SecretStore | None:
    """The store the settings name, or None when none is configured (self-hosted only).
    `factories` are the plugin stores by name; when None and one is needed, the installed plugins
    are loaded."""
    try:
        hosted = hosted_from_env(env)
    except HostedConfigError:
        raise _refuse("invalid", "hosted_config") from None
    name = env.get(SECRET_STORE_ENV, "")
    directory = env.get(SECRET_DIR_ENV, "")
    if factories is None and (hosted or name not in ("", "file", "fake")):
        factories = _installed_factories()
    plugin = factories.get(name) if factories and name else None
    if hosted and plugin is None:
        raise _refuse("invalid", "hosted_needs_real_store")
    if plugin is not None:
        if directory:
            raise _refuse("invalid", "secret_dir_conflict")
        return plugin(env)
    if name == "fake":
        return FakeSecretStore()
    if name in ("", "file"):
        if not directory:
            if name == "file":
                raise _refuse("invalid", "secret_dir")
            return None
        from nuramesh.engine.secret_file import FileSecretStore

        return FileSecretStore(directory)
    raise _refuse("invalid", "secret_store")
