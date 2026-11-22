"""The one reader of `NURAMESH_HOSTED` (ND220): every layer that needs to know whether this
deployment is hosted calls `hosted_from_env`, so no two places can disagree about a value.

The grammar is the shared one (`nuramesh.env_bool`, ND423): on is `1`, `true`, `yes`, `on`; off is
`0`, `false`, `no`, `off`; unset, empty or whitespace-only is not hosted. Anything else is a
configuration error, never guessed: a typo must not silently switch retention between 90 and 396
days. Stdlib only, so any layer may import it."""

from collections.abc import Mapping

from nuramesh.env_bool import EnvBoolError, env_bool

HOSTED_ENV = "NURAMESH_HOSTED"
HOSTED_ENV_PROBLEM = str(EnvBoolError(HOSTED_ENV))


class HostedConfigError(ValueError):
    """`NURAMESH_HOSTED` holds a value that is not a boolean word. The text is one line naming the
    variable (the CLI prints it as is)."""


def hosted_from_env(env: Mapping[str, str] | None = None) -> bool:
    """True when hosted; False when unset or off; `HostedConfigError` otherwise."""
    try:
        return env_bool(HOSTED_ENV, env)
    except EnvBoolError:
        raise HostedConfigError(HOSTED_ENV_PROBLEM) from None
