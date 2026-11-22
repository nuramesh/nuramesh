"""The data converter every client and worker of a durable run uses (T72).

`ScrubbingFailureConverter` is the second lock behind `errors.guarded`: whatever exception reaches
Temporal, the text that user code put in it never does. Every application failure in the chain
keeps only its type name (a code, or a class name) as its message, and no stack trace. SDK
messages ("Activity task failed", timeouts) are fixed texts and stay.
"""

import dataclasses

import temporalio.api.failure.v1
from temporalio.converter import DataConverter, DefaultFailureConverter, PayloadConverter


class ScrubbingFailureConverter(DefaultFailureConverter):
    """Replaces the message of every application failure by its type name."""

    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: temporalio.api.failure.v1.Failure,
    ) -> None:
        super().to_failure(exception, payload_converter, failure)
        _scrub(failure)


def _scrub(failure: temporalio.api.failure.v1.Failure) -> None:
    current: temporalio.api.failure.v1.Failure | None = failure
    while current is not None and current.ByteSize():
        if current.HasField("application_failure_info"):
            current.message = current.application_failure_info.type or "error"
            current.stack_trace = ""
        current = current.cause if current.HasField("cause") else None


def data_converter() -> DataConverter:
    """The default converter, with failures scrubbed."""
    return dataclasses.replace(
        DataConverter.default, failure_converter_class=ScrubbingFailureConverter
    )
