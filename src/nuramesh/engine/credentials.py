"""The one credential seam (R1.33 2.1): every upstream credential comes from `CredentialSource`.

The registry of auth kinds is closed: `entra`, `key_file`, `key`, `none` and `stored` (the values
of `ConnectionFacts.auth`). `stored` (R1.14c) is a reference to a key in the secret store; it is
read through a `StoredKeyReader` (version pinning, the ledger decides, one retry), and without a
reader a `stored` connection is refused here, fail closed. None of them holds a
secret: the ledger and the API hold at most a NAME, and the value lives in an operator-owned place
outside the repository. A kind that would hold a secret cannot be registered. A credential is a
`Redacted` value; an error while getting one carries a fixed text only and never chains the
SDK's error.

The seam changes no check, order or fixed text of the readers it calls (the token provider, the
Anthropic key file and the openai-compatible key binding): their `ProviderError` texts pass through.
"""

import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from nuramesh.core.model_catalog import STORED_KEY_PROVIDERS, ConnectionRecord
from nuramesh.core.models import ProviderError
from nuramesh.core.org import OPENAI_COMPATIBLE
from nuramesh.core.secrets_text import CredentialGetter, Redacted
from nuramesh.engine import anthropic
from nuramesh.engine import openai_compatible as oac
from nuramesh.engine.azure_openai import TokenProvider, entra_token_provider
from nuramesh.engine.connection_rules import SUPPORTED_PROVIDERS, auth_of
from nuramesh.engine.key_file import IsAgentUser
from nuramesh.engine.stored_key_read import Pin, StoredKeyReader, StoredKeyRefused


@dataclass(frozen=True)
class AuthKind:
    """One kind of connection login. `holds_secret` is declared by whoever adds a kind."""

    name: str
    source: str
    holds_secret: bool


def build_auth_kinds(kinds: Iterable[AuthKind]) -> Mapping[str, AuthKind]:
    """An immutable registry; a kind that holds a secret is refused (stored keys are R1.14c)."""
    built: dict[str, AuthKind] = {}
    for kind in kinds:
        if kind.holds_secret:
            raise ValueError("an auth kind that holds a secret cannot be registered")
        built[kind.name] = kind
    return types.MappingProxyType(built)


AUTH_KINDS = build_auth_kinds(
    [
        AuthKind("entra", "token provider", holds_secret=False),
        AuthKind("key_file", "the server's Anthropic key file", holds_secret=False),
        AuthKind("key", "an operator key binding, by name", holds_secret=False),
        AuthKind("none", "no credential", holds_secret=False),
        AuthKind("stored", "a key stored in the secret store, by reference", holds_secret=False),
    ]
)


class CredentialUnavailable(Exception):  # noqa: N818 (named in the spec)
    """No credential could be had. The text is fixed: the code only."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code if reason is None else f"{code} ({reason})")
        self.code = code
        self.reason = reason  # a fixed word (never a path or a value): why `stored` is off


def _pair_ok(provider: str, auth: str, *, stored: bool = False) -> bool:
    """Whether `auth` is one a connection of `provider` may carry (the resolver's own pairing).
    `stored` is allowed only for the providers that may hold a stored key (the core's
    `STORED_KEY_PROVIDERS`, VK30), and only when a reader is configured."""
    if provider not in SUPPORTED_PROVIDERS:
        return False
    if auth == "stored":
        return stored and provider in STORED_KEY_PROVIDERS
    if provider == OPENAI_COMPATIBLE:
        return auth in ("none", "key")
    return auth == auth_of(provider)


class CredentialSource:
    """Hands out credentials. The readers are injected in tests; the defaults are the ones main
    has: the Entra token provider, `engine.anthropic.key_file_provider` and the openai-compatible
    binding (`engine.openai_compatible.key_for`)."""

    def __init__(
        self,
        *,
        entra: TokenProvider | None = None,
        key_file: Callable[[], str] | None = None,
        binding: Callable[[str, str], str] | None = None,
        agent: IsAgentUser | None = None,
        stored: StoredKeyReader | None = None,
        stored_off_reason: str | None = None,
    ) -> None:
        self._stored = stored
        self._stored_off = stored_off_reason  # why there is no reader (`key_readers_from_env`)
        self._entra = entra
        self._key_file: Callable[[], str] = key_file or anthropic.key_file_provider
        self._binding = binding
        self._agent = agent

    def _token(self) -> str:
        if self._entra is None:
            self._entra = entra_token_provider()
        return self._entra()

    def _bound(self, endpoint: str, name: str) -> str:
        if self._binding is not None:
            return self._binding(endpoint, name)
        return oac.key_for(endpoint, name, agent=self._agent)

    def pin_for(self, key_ref: str, *, connection: str) -> Pin:
        """Resolve the stored-key version a NEW call will use, at reservation (spec 6 step 8).
        The connection is required; an empty one is refused (no identity, fail closed)."""
        if self._stored is None:
            raise CredentialUnavailable("auth_kind", self._stored_off)
        if not connection:
            raise CredentialUnavailable("credential_not_active")
        code = ""
        pin: Pin | None = None
        try:
            pin = self._stored.pin(key_ref, connection=connection)
        except StoredKeyRefused as exc:
            code = exc.code
        except Exception:
            code = "credential_unavailable"  # any other error: a fixed code, no text, no cause
        if pin is None:
            raise CredentialUnavailable(code or "credential_unavailable")
        return pin

    def credential_pinned(self, pin: Pin) -> Redacted:
        """The value of exactly the version `pin` resolved; the fold is checked again first."""
        if self._stored is None:
            raise CredentialUnavailable("auth_kind", self._stored_off)
        code = ""
        value: Redacted | None = None
        try:
            value = self._stored.read(pin)
        except StoredKeyRefused as exc:
            code = exc.code
        except Exception:
            code = "credential_unavailable"  # any other error: a fixed code, no text, no cause
        if value is None:
            raise CredentialUnavailable(code or "credential_unavailable")
        return value

    def credential_for(
        self,
        provider: str,
        auth: str,
        endpoint: str,
        key: str | None = None,
        *,
        key_ref: str | None = None,
        connection: str | None = None,
    ) -> Redacted | None:
        """The credential for a connection's facts, or None for `auth none`. The registry and the
        provider/auth pairing are checked first: before any token request or file read."""
        if auth not in AUTH_KINDS or not _pair_ok(provider, auth, stored=self._stored is not None):
            off = self._stored_off if auth == "stored" and self._stored is None else None
            raise CredentialUnavailable("auth_kind", off)
        if auth == "none":
            return None
        if auth == "stored":
            if not key_ref:
                raise CredentialUnavailable("key_name")
            if not connection:
                raise CredentialUnavailable("credential_not_active")  # fail closed: no identity
            return self.credential_pinned(self.pin_for(key_ref, connection=connection))
        if auth == "key" and not key:
            raise CredentialUnavailable("key_name")
        failed = False
        value: object = None
        try:
            if auth == "entra":
                value = self._token()
            elif auth == "key_file":
                value = self._key_file()
            else:
                assert key is not None
                value = self._bound(endpoint, key)
        except ProviderError:
            raise  # a fixed text of the reader itself (for example the missing extra)
        except Exception:
            failed = True  # the SDK's message can hold a tenant, a user or a token fragment
        if failed or not isinstance(value, str):
            # raised outside the except block: no __cause__ and no __context__
            raise CredentialUnavailable("token" if auth == "entra" else "key")
        return Redacted(value)

    def credential(self, connection: ConnectionRecord) -> Redacted | None:
        """The credential for a connection."""
        return self.credential_for(
            connection.provider,
            connection.auth,
            connection.endpoint,
            connection.key,
            key_ref=connection.key_ref,
            connection=connection.conn_key,
        )

    def getter_for(
        self,
        provider: str,
        auth: str,
        endpoint: str,
        key: str | None = None,
        *,
        key_ref: str | None = None,
        connection: str | None = None,
    ) -> CredentialGetter:
        """What an adapter is given: it asks for the credential when it needs one."""
        return lambda: self.credential_for(
            provider, auth, endpoint, key, key_ref=key_ref, connection=connection
        )

    def getter(self, connection: ConnectionRecord) -> CredentialGetter:
        """`getter_for` the facts of `connection`."""
        return self.getter_for(
            connection.provider,
            connection.auth,
            connection.endpoint,
            connection.key,
            key_ref=connection.key_ref,
            connection=connection.conn_key,
        )
