# OpenTelemetry export (E3)

NuraMesh sends no telemetry and makes no network connection by default. The quickstart and `nuramesh serve` connect to nothing but loopback by default, and the open core never checks a licence over the network. A real model run connects only to the provider you set up. Other connections happen only when you configure them: a remote `NURAMESH_DATABASE_URL`, a Temporal address (`NURAMESH_TEMPORAL_ADDRESS`), email, and an anchor remote. The one export is the OpenTelemetry one below: off until you set an endpoint, and sent only to that endpoint.

NuraMesh can send what happened in a run to your own observability stack as OpenTelemetry traces and metrics.
**Nothing is sent unless you set `NURAMESH_OTEL_ENDPOINT`.** With no endpoint the exporter does not exist: the
OpenTelemetry SDK and exporter are not imported, no thread starts, no socket opens. The standard `OTEL_*` variables are
ignored on purpose, so a variable set for another tool never starts exporting NuraMesh data.

## Turn it on

```bash
uv sync --extra otel                  # from a repository checkout: the SDK and the OTLP/HTTP exporter (the PyPI package ships at launch)
export NURAMESH_OTEL_ENDPOINT=https://otel.example.com:4318
export NURAMESH_OTEL_HEADERS='authorization=Bearer <token>'   # a secret: never logged
nuramesh otel check                      # validates the settings, sends one empty request per signal
nuramesh serve ./team                    # the exporter runs on a thread named nuramesh-otel
nuramesh otel export --once --team ./team [--from-start] [--stream run:<id>]   # one foreground pass
```

The endpoint must be `https://`, or `http://` to `localhost`, `127.0.0.0/8` or `::1` (or any host with
`NURAMESH_OTEL_ALLOW_INSECURE=true`). Credentials, query strings and fragments in the URL are refused. A refused setting
never stops the server: it starts with the exporter off and logs one line `otel export disabled: <code>` (codes:
`bad_scheme`, `insecure_endpoint`, `credentials_in_url`, `query_or_fragment`, `empty_endpoint`, `bad_setting`,
`missing_extra`). The line never holds the URL, a header or a key.

| Variable | Default | Meaning |
|---|---|---|
| `NURAMESH_OTEL_ENDPOINT` | unset = off | OTLP/HTTP base URL; signals go to `/v1/traces` and `/v1/metrics` (gzip, TLS verified) |
| `NURAMESH_OTEL_HEADERS` | none | `name=value,name=value` request headers |
| `NURAMESH_OTEL_SIGNALS` | `traces,metrics` | which signals to export |
| `NURAMESH_OTEL_SAMPLE_RATIO` | `1` | trace sampling 0 to 1, deterministic from the trace id; metrics count every event |
| `NURAMESH_OTEL_PERSON_IDS` | `none` | `none` or `pseudonym` (see below) |
| `NURAMESH_OTEL_PSEUDONYM_KEY_FILE` | `<key dir>/otel-pseudonym.key` | the pseudonym key, mode 0600 |
| `NURAMESH_OTEL_EVENT_HASH` | `true` | add `nuramesh.event_hash` (the ledger entry hash) |
| `NURAMESH_OTEL_COST_METRIC` | `false` | integer `nuramesh.cost.nano_usd` counter |
| `NURAMESH_OTEL_GENAI_ATTRS` | `false` | also add `gen_ai.*` attributes |
| `NURAMESH_OTEL_BATCH_SIZE`, `_POLL_SECONDS`, `_EXPORT_TIMEOUT_SECONDS`, `_MAX_LAG_EVENTS` | 256, 2, 10, 100000 | loop limits |
| `NURAMESH_OTEL_STATE_FILE` | `<key dir>/otel-cursors.json` | per-stream cursors (the only state) |
| `NURAMESH_OTEL_DEPLOYMENT` | unset | optional resource attribute `nuramesh.deployment` |

## What is sent: ids, counts, costs and codes, never content

One trace per run (`run:<id>`) and per organization stream: a run span, a task span per task, a span per model call and
per gate round, with span events for policy decisions, approvals, budget breaches and breaker trips. Every attribute is
on an allowlist (the telemetry spec); every string passes a value guard that refuses free text, e-mail addresses, file
paths and person ids, and counts what it drops (`nuramesh.export.dropped_attributes`). Prompts, outputs, reasons,
error messages, canon and file names are never read. Money is decimal text (`"0.00123400"`), never a float; an unknown
price stays unknown. Span ids are computed from the stream and the ledger sequence, so replaying gives identical spans.

Metrics: `nuramesh.runs.started`, `runs.ended`, `tasks.finished`, `model.calls`, `model.tokens`, `model.latency`,
`model.blocked`, `model.output_capped`, `gate.results`, `approvals.decided`, `policy.decisions`, `budget.breaches`, `breaker.trips`,
`org.halts`, and the exporter's own `nuramesh.export.events`, `.failures`, `.dropped_attributes`, `.gap_events`,
`.lag_events`. Metric attribute keys are bounded; ids of runs, tasks and agents are never metric attributes.

`nuramesh.gate.results` has the attributes `gate`, `outcome` and `kind` (closed: `test_command`, `rule`, `schema`,
`verifier`, `human`, `custom`, or `unknown` for a result with no matching `gate.started` or an unrecognised kind). Build a
verifier view in your backend from this metric attribute (`kind="verifier"`), not from the span attribute
`nuramesh.gate.kind`: spans may be sampled out, the metric counts every gate result. The ledger and API stay the
authoritative source (metrics may double count).

## People

By default the export carries **no person identifier**: a `human:` or `operator:` actor is exported as
`nuramesh.actor.kind` only. With `NURAMESH_OTEL_PERSON_IDS=pseudonym` such an actor also gets a keyed pseudonym
(`nuramesh.actor.pseudonym`, HMAC-SHA256 under a per-deployment key, and `nuramesh.actor.pseudonym_key_id`).
`nuramesh otel rotate-pseudonym-key` replaces the key; every pseudonym changes from the next cycle.

Pseudonyms are **still personal data**: whoever holds the key and the user table can link them back to a person, and
even without the key one pseudonym follows one person. When you point the exporter at an observability vendor, that
vendor becomes **your processor** for what the export carries; the default export carries no person identifiers.

`nuramesh.event_hash` is the ledger entry's own hash. It reveals nothing about content without the chain and lets an
engineer find the entry; set `NURAMESH_OTEL_EVENT_HASH=false` to remove it.

## Failure never reaches a run

The exporter only reads the ledger (it writes nothing) and runs on its own thread. A failing, slow or hung collector
cannot change a run, a budget, a gate or the ledger: the cursor stays behind unacknowledged events, retries back off
1, 2, 4, 8, 16, 32, then 60 seconds (each with up to 20% added jitter), and every failure is counted. If the exporter falls more than
`NURAMESH_OTEL_MAX_LAG_EVENTS` behind, it skips ahead, counts the gap and logs `otel export gap: <stream> <from> <to>`;
`--from-start` replays the skipped events. On shutdown it flushes for up to 5 seconds.

A stream with a ledger entry whose stored time is not a real time (a row written outside the product) is skipped
and reported, not retried as a failure: the exporter logs `otel export skipped a damaged stream: <stream>` once,
lists it in the cycle result and `snapshot()["damaged"]`, keeps that stream's cursor where it was and exports the
streams after it. The stream is met again each cycle, and its unread entries stay counted in the lag gauge.
A stream whose *head* has a damaged time is different: the real stores leave it out of the heads listing (logged by
the store), so the exporter neither exports it nor counts its entries in the lag gauge until the entry is repaired. If
a reader's heads call itself raised, the whole cycle would fail (counted and backed off like any store failure),
because the heads are read in one call.

## Dashboards in your backend

NuraMesh's own dashboards (`/dashboards` in the web app, `GET /v1/dashboards/*`) are computed from the ledger on read.
**The ledger and that API are authoritative.** Metrics may double count (an export is at least once, and a run replayed
into a second backend counts twice), so a figure from your backend can differ from the API; treat it as a trend. NuraMesh
ships no vendor-specific dashboard file. The queries below are PromQL-style and assume your collector turns the metric
`nuramesh.gate.results` into `nuramesh_gate_results_total`; the exact names depend on your backend's translation of
dots, units and counter suffixes, so check them once there. `$window` is your dashboard range, for example `7d`.

| View | Query | Notes |
| --- | --- | --- |
| Pass rate | `sum(increase(nuramesh_gate_results_total{outcome="passed"}[$window])) / (sum(increase(nuramesh_gate_results_total{outcome="passed"}[$window])) + sum(increase(nuramesh_gate_results_total{outcome="failed"}[$window])))` | Same formula as the API: `error` is not a verdict and is left out of the ratio. Split by adding `by (gate)`. |
| Verifier fail rate | the same query, over results whose gate `kind` is `verifier` | Query `nuramesh_gate_results_total{kind="verifier"}` by `outcome` (the metric attribute `kind`; see above). A count of failed submissions, not proof the verifier was right. |
| Cost | `sum(increase(nuramesh_cost_nano_usd_total[$window])) / 1e9` | Needs `NURAMESH_OTEL_COST_METRIC=true`. Approximate: whole nano-dollars, and unknown prices add nothing. The API gives the exact `Decimal` per currency. |
| p95 latency | `histogram_quantile(0.95, sum by (le) (increase(nuramesh_model_latency_milliseconds_bucket[$window])))` | Estimated from histogram buckets. The API gives the exact nearest-rank figure from the ledger. |
| Approvals waiting | not available from metrics | Waiting is state (a request with no decision yet), which counters do not give. Use `GET /v1/dashboards/approvals` or the Approvals dashboard. |

Nothing in NuraMesh reads these figures back from your backend.

## Sample collector configuration

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  debug: {}
service:
  pipelines:
    traces: { receivers: [otlp], exporters: [debug] }
    metrics: { receivers: [otlp], exporters: [debug] }
```
