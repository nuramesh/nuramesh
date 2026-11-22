"""`Redacted`: a wrapper for a credential value that never prints, dumps or pickles (R1.33 2.1.2).

Pure (stdlib and pydantic). The only way to read the value is the one reveal function below,
which the engine calls where it sets an `Authorization` header.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

PLACEHOLDER = "<redacted>"


class Redacted:
    """A credential value. Every text and serialized form is `<redacted>`; equality is identity."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Redacted is immutable")

    def __repr__(self) -> str:
        return PLACEHOLDER

    def __str__(self) -> str:
        return PLACEHOLDER

    def __format__(self, format_spec: str) -> str:
        return PLACEHOLDER

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def __copy__(self) -> Redacted:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Redacted:
        return self

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Redacted value cannot be pickled")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("a Redacted value cannot be pickled")

    def __getstate__(self) -> NoReturn:
        raise TypeError("a Redacted value has no state to copy or dump")

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.is_instance_schema(
            cls,
            serialization=core_schema.plain_serializer_function_ser_schema(lambda _v: PLACEHOLDER),
        )


def reveal_secret(value: Redacted) -> str:
    """The wrapped value. Call only where the credential is put on the wire."""
    return str(object.__getattribute__(value, "_value"))


CredentialGetter = Callable[[], Redacted | None]
"""What an adapter is given to get its credential at the moment it needs one: a `Redacted` value,
or `None` when the connection takes no credential (`auth none`). Built by `CredentialSource`."""
