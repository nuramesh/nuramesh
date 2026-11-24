"""OpenTelemetry export of the ledger (E3, ND100, ND117).

Pure Python: this package imports `nuramesh.core` and the `Store` read API only. The one module
that imports OpenTelemetry is `nuramesh.telemetry.otel`, and only when an endpoint is set.
Nothing here writes the ledger, and the engine never imports this package.
"""
