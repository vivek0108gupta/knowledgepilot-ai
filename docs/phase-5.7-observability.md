# Phase 5.7: observability

What the API now measures, how to collect it, and what still needs a hosted tool. It builds on
the request IDs and content-free logs from earlier phases.

## Design rules

- **Operators only.** Metrics are served at `GET /metrics`, outside the user API.
  - The endpoint does not exist (404) unless `METRICS_TOKEN` is set.
  - With the token set, a request needs `Authorization: Bearer <METRICS_TOKEN>`, compared in
    constant time.
  - A user's Supabase session is refused (tested), so workspace owners, admins and members
    never see operational data.
  - The endpoint is left out of the OpenAPI schema.
- **No identifiers or content in metrics.** Label values are limited to:
  - route templates (`/workspaces/{workspace_id}/search`), never concrete paths; unmatched
    paths become `unmatched`, so scanning with random URLs cannot create new series;
  - HTTP status and error codes;
  - model names, operation kinds and outcomes.
  - Never a user, workspace, document or conversation ID, a question, a key or a URL. A test
    asserts that the output contains no UUID after real requests.
- **Logs stay content-free.** Unchanged since Phase 5.6: IDs, counts and error types only.
  The load test scanned about 10,000 JSON log lines for tokens, keys and document text and
  found none.
- **No new dependency.** The registry (`app/core/metrics.py`) is about 150 lines and writes
  the Prometheus text format.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `METRICS_TOKEN` | empty (off) | Bearer token for `/metrics`. Use a random value of 32+ characters from the secret store. |
| `LOG_FORMAT` | `text` | `json` writes one JSON object per line: `time` (UTC), `level`, `logger`, `request_id`, `message`, and `exception` when there is one. |

Each replica keeps its own counters. Scrape every replica (Prometheus service discovery, or
the platform's agent) and sum across replicas. Counters reset when a process restarts;
Prometheus `rate()` and `increase()` handle that.

## What is measured

### Counters and histograms (per process)

| Metric | Labels | Covers |
|---|---|---|
| `http_requests_total` | method, route, status | Request count and HTTP error rates (status ≥ 500, 4xx by route) |
| `http_request_duration_seconds` | method, route | Request latency (histogram, 5 ms to 300 s) |
| `app_errors_total` | code, status | Every error response by code. This includes the **quota and rate-limit refusals**: `workspace_quota_exceeded`, `user_quota_exceeded`, `rate_limited`, `concurrent_generation_limit` and others. |
| `llm_requests_total` | model, mode (`generate` / `stream`), outcome | Chat calls. The outcome is `ok`, an error code (`unavailable` for timeouts, rate limits and 5xx; `misconfigured`; `rejected`; `too_long`; `invalid_response`), or `cancelled` (the client stopped reading). |
| `llm_duration_seconds` | model, mode | Generation duration |
| `llm_first_token_seconds` | model | First-token latency for streamed answers |
| `llm_retries_total` | model, error | Provider retries, by exception type (for example `RateLimitError`, `APITimeoutError`) |
| `llm_fallbacks_total` | model, fallback | Answers moved to the fallback model ([model evaluation](phase-5.7-model-evaluation.md)) |
| `embedding_requests_total` | outcome (`ok` / `retryable` / `failed`) | Embedding failures |
| `embedding_duration_seconds` | | Embedding call duration, retries included |
| `ingestion_jobs_finished_total` | status, reason | Job results: `succeeded/indexed`, `queued/retrying` (a retry), `failed/attempts_exhausted`, `failed/failed`, `failed/document_deleted` |
| `ingestion_job_duration_seconds` | result | Processing time per attempt |
| `ingestion_queue_wait_seconds` | | Time from a job becoming available to being claimed |
| `ingestion_jobs_recovered_total` | | Jobs taken over after a lost lease (a worker crashed) |
| `ingestion_jobs_handed_back_total` | | Jobs handed back at graceful shutdown |
| `usage_operations_total` | operation, status, outcome, source | Finalised usage by outcome. Stream cancellations appear as `outcome="cancelled_by_user"` and `outcome="client_disconnected"`. |
| `usage_tokens_total` | operation, model | Charged tokens |
| `usage_cost_usd_total` | operation, model | Estimated cost (list prices, not an invoice) |
| `usage_reservations_expired_total` | | Reservations expired by the sweep (abandoned operations) |

### Gauges (read at scrape time)

Database gauges cover all replicas, and the database queries are bounded by a 2 s statement
timeout.

| Gauge | Meaning | Healthy |
|---|---|---|
| `ingestion_jobs{status}` | Queue depth by status | `queued` drains |
| `ingestion_oldest_queued_seconds` | Age of the oldest runnable job | Seconds, not minutes |
| `ingestion_jobs_stalled` | Running jobs past their lease | 0 |
| `usage_reservations_open{operation}` | Open reservations | Small, follows traffic |
| `usage_reservations_overdue` | Open reservations past expiry | 0 after a sweep |
| `database_connections` | Connections to this database, all clients | Below `max_connections` with headroom |
| `db_pool_checked_out` | Connections this process holds | At most pool size + overflow (15) |
| `process_cpu_seconds`, `process_peak_resident_bytes`, `process_uptime_seconds` | This process | |
| `metrics_database_up` | 0 if the gauges could not be read | 1 |

## Suggested alerts

| Alert | Expression (PromQL, summed over replicas) | Why |
|---|---|---|
| API errors | `sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) > 0.02` | Something is broken |
| Provider down | `sum(rate(llm_requests_total{outcome="unavailable"}[5m])) > 0.1` | OpenAI is failing or timing out |
| Misconfigured model or key | `increase(llm_requests_total{outcome="misconfigured"}[5m]) > 0` or `increase(llm_fallbacks_total[1h]) > 0` | Key rotated, or model removed |
| Slow first token | `histogram_quantile(0.95, sum by (le) (rate(llm_first_token_seconds_bucket[10m]))) > 5` | Users wait too long for the first word |
| Ingestion backlog | `max(ingestion_oldest_queued_seconds) > 300` | Workers are not keeping up, or none are running |
| Stalled jobs | `max(ingestion_jobs_stalled) > 0` for 15 min | Lease recovery is not running |
| Embedding failures | `sum(rate(embedding_requests_total{outcome!="ok"}[10m])) > 0.05` | Documents are failing to index |
| Abandoned operations | `increase(usage_reservations_expired_total[1h]) > 10` | Processes are crashing |
| Quota pressure | `sum by (code) (increase(app_errors_total{code=~".*quota.*"}[1h]))` | A business signal, not a page |
| Database connections | `max(database_connections) > 0.8 * <max_connections>` | Scale the database or reduce replicas or pool |
| Scrape failing | `up == 0` or `metrics_database_up == 0` | Blind |

## Error tracking and hosted tools (evaluation)

| Option | Fits | Concerns | Recommendation |
|---|---|---|---|
| **Sentry** (or GlitchTip, self-hosted and API-compatible) | Groups exceptions with stack traces and releases; good for the frontend too | Sends local variables and request bodies by default, which here could include questions, document text or tokens | **Recommended before a public launch**, configured with `send_default_pii=False`, `include_local_variables=False`, and a `before_send` filter that drops request bodies and headers. The JSON logs already carry the `request_id` to correlate with. Not added yet: it needs a DSN and a data-processing review. |
| **Prometheus + Grafana** (self-hosted, or Grafana Cloud) | Reads `/metrics` directly; the alerts above are written for it | One more service to run | **Recommended** for metrics. |
| **OpenTelemetry** (traces) | Traces across API, database and OpenAI | Heavier; and spans could capture SQL parameters or URLs if not configured carefully | Later, if latency needs to be investigated span by span. |
| Platform log search (for example the hosting provider, Loki, Datadog) | Use `LOG_FORMAT=json` | Retention and access control | Ship JSON logs; keep them private and set retention, for example 30 days. |

## Verified

- `tests/test_observability.py` (14 tests):
  - the endpoint is off without a token, refuses missing or wrong tokens and users'
    sessions, and is not in the schema;
  - after a real answer over the test database, the output has request, usage, error and
    database metrics, and no IDs, question text or token;
  - histograms render correctly;
  - label values cannot break the format;
  - model call, retry, first-token and cancelled-stream counts;
  - JSON log format.
- **Load test** ([load testing](phase-5.7-load-testing.md)): `/metrics` was scraped from a
  live process under load, and the queue-wait and first-token histograms were read from it.

## Remaining gaps

- No hosted error tracker or dashboard is connected yet (see the recommendations above).
- Metrics are per process and in memory, so a crashed process loses its counters since the
  last scrape. Gauges come from the database and are not affected.
- The frontend has no error reporting beyond the browser console.
