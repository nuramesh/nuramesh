"""Where a model may process data (ND45, amending ND37): a hard rule, checked before any call.

A provider deployment has a **processing scope**: `canada`, `region:<id>` (a region the customer
chose) or `global` (the provider may process prompts anywhere, as Azure's GlobalStandard SKU does).
A run has a **data class**: `synthetic` (demos, evals and development data that is not a customer's)
or `customer`.

- `global` processing accepts **only** `synthetic` data.
- `canada` accepts any data class.
- `region:<id>` accepts `customer` data only if that region is in the customer's chosen set.

The check is pure (stdlib only), runs before a provider is built or a call is made, and refuses with
`ResidencyError`; there is no override.
"""

import re
from collections.abc import Collection

SYNTHETIC = "synthetic"
CUSTOMER = "customer"
DATA_CLASSES = (SYNTHETIC, CUSTOMER)
_REGION = re.compile(r"region:[a-z0-9][a-z0-9-]{0,62}")


class ResidencyError(ValueError):
    """The processing scope of a deployment does not allow the run's data class."""


def check_processing(processing: str) -> None:
    """`ResidencyError` unless `processing` is `canada`, `global` or `region:<id>`."""
    if processing not in ("canada", "global") and not _REGION.fullmatch(processing):
        raise ResidencyError(
            f"processing scope '{processing}' is not canada, global or region:<id>"
        )


def require_data_residency(
    processing: str, data_class: str, *, customer_regions: Collection[str] = ()
) -> None:
    """Allow a deployment to receive `data_class` data, or raise `ResidencyError`."""
    check_processing(processing)
    if data_class not in DATA_CLASSES:
        raise ResidencyError(f"data class '{data_class}' is not synthetic or customer")
    if data_class == SYNTHETIC or processing == "canada":
        return
    if processing == "global":
        raise ResidencyError(
            "this deployment may process data outside Canada (global): it accepts only "
            "synthetic, non-customer data; customer data must go to a Canadian or "
            "customer-chosen region"
        )
    if processing.removeprefix("region:") not in set(customer_regions):
        raise ResidencyError(
            f"customer data may not be processed in {processing}: it is not one of the "
            "customer's chosen regions"
        )
