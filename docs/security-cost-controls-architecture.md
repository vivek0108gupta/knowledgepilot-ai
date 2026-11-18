# Security and cost controls: architecture

How KnowledgePilot AI limits request rates, meters every AI provider operation, and enforces
quotas before spending anything. Everything runs in the existing API and PostgreSQL; there is
no new service. See also [operations](security-cost-controls-operations.md),
[testing](security-cost-controls-testing.md) and the [audit](security-cost-controls-audit.md).

## Components

| Module | Responsibility |
|---|---|
| `app/services/rate_limits.py` | Sliding-window request limits in PostgreSQL. |
| `app/services/usage.py` | The usage ledger: reservations, quota checks, finalisation, estimates. |
| `app/services/pricing.py` | Versioned model prices and cost calculation. Default table in `app/core/config.py`. |
| `app/services/usage_reports.py` | Summaries, history and quota policies, scoped by role. |
| `app/api/v1/usage.py` | The usage and quota endpoints. |
| `app/models/usage.py` | `usage_events`, `workspace_quota_policies`, `rate_limit_buckets` (migration `0007`). |

The controls hook into the existing services at their boundaries; there is no parallel
pipeline:

- `chat.admit`, `chat.begin`, `chat.save` and `chat.release`, used by `/chat` and
  `/chat/stream`;
- `AnswerStream`;
- `search.embed_query`, used by search and by chat retrieval;
- `documents.create` and `documents.retry`;
- `processing.process_document`.

## Database schema (migration `0007`)

**`usage_events`** holds one row per provider operation. It is both the reservation and the
event: two tables would have to be kept in step on every path.

| Column group | Columns | Meaning |
|---|---|---|
| Who | `user_id` (verified token), `workspace_id` (FK, `SET NULL`) | Workspace deletion keeps history and user quotas. |
| What | `operation` (`chat`, `chat_stream`, `query_embedding`, `document_embedding`), `provider`, `model` | `model` is the provider-reported model once known. |
| State | `status` (`reserved`, `completed`, `failed`, `cancelled`, `expired`), `usage_source` (`provider`, `estimated`, `unavailable`, `none`), `error_code` | A check constraint ties `completed_at` to every final status. |
| Reserved | `reserved_input_tokens`, `reserved_output_tokens`, `reserved_cost` | The most the operation may use, set before the call. |
| Provider-reported | `input_tokens`, `output_tokens`, `total_tokens` | Only what the provider said. Never estimates. |
| Estimated | `estimated_input_tokens`, `estimated_output_tokens` | This application's own count, when the provider reported nothing. |
| Charged | `charged_tokens`, `charged_cost` | What counts against quotas: provider numbers, else the estimate, else the reservation. |
| Cost | `estimated_cost`, `currency`, `pricing_version` | `NUMERIC(16,8)`. An estimate, never an invoice. |
| Links | `conversation_id`, `message_id` (unique), `document_id` (FKs, `SET NULL`), `request_id` | One answer has at most one usage row. |
| Idempotency | `idempotency_key` (unique) | One provider operation, one row. |
| Time | `created_at`, `completed_at`, `expires_at` (UTC) | |

Check constraints cover every enumerated value, non-negative tokens and costs, and the
currency format. Indexes cover workspace and time, user and time, open reservations (a
partial index), workspace and operation, and workspace and model.

**`workspace_quota_policies`**: `workspace_id` (PK, FK `CASCADE`), `monthly_token_limit`,
`monthly_cost_limit`, `max_concurrent_operations`, `updated_by`, `updated_at`. These are
overrides that can only tighten the system limits.

**`rate_limit_buckets`**: primary key `(policy, subject_id, window_start)`, plus `count`.
Windows older than the previous one are deleted as new ones are used.

## Request lifecycle (an answer)

```
JWT verified ─► conversation owned by caller? (404 otherwise)
  ─► rate limits: chat:user, chat:workspace            (429 rate_limited)
  ─► BEGIN; lock conversation row
       idempotency: answered already? ─► replay, no reservation, no provider call
       archived / busy checks (409)
       store question, take lease
       reserve: advisory locks user→workspace, expire stale, check limits, insert reservation
             (429 concurrent_generation_limit | workspace_busy | user_quota_exceeded |
                   workspace_quota_exceeded; 503 usage_unavailable)
     COMMIT  ─ question, lease and reservation together, or none of them
  ─► history, query embedding (recorded as its own query_embedding row), retrieval, context
  ─► provider call (stream or not)
  ─► BEGIN; store answer, release lease, finalise reservation (completed, provider counts); COMMIT
     on any failure: release lease + finalise (failed / cancelled) in one transaction
```

Search: membership is checked, then the rate limit, then `ensure_available` (the query must
fit in what remains). The query is embedded and recorded. Upload: membership, then the rate
limit (before the body is read), then the ingestion cap under a workspace lock. Ingestion:
claim, extract, reserve the document's estimated tokens, embed in batches (accumulating the
provider's counts), then finalise.

## Reservation lifecycle

`reserved` → exactly one of:

| Final status | When | Charged |
|---|---|---|
| `completed` | The operation finished (an answer stored, a document indexed). | Provider counts, or an estimate if none were reported. |
| `failed` | The provider or a later step failed. | Provider counts if any. Otherwise an estimate (streams), the reservation (`unavailable`, a whole response awaited and none came), or 0 (`none`, refused by the provider before any work). |
| `cancelled` | The client disconnected or pressed Stop; a document was deleted mid-way. | An estimate: the prompt sent plus the text received. 0 if the provider was never called. |
| `expired` | Never finalised before `expires_at` (the process stopped). | The reservation, labelled `unavailable`. |

- **Exactly once.** `finalize` updates `WHERE id = … AND status = 'reserved'`, so a second
  call (for example a late `close()` after `save()`), or a concurrent one, changes nothing.
  It also returns `False`.
- **Same transaction as the outcome.** A completed answer's usage is finalised in the
  transaction that stores the answer. If that commit fails, `release` finalises it as
  `failed`, still with the provider's counts.
- **Expiry.** Every reservation sweeps the user's and the workspace's stale reservations
  first. An expired reservation stops counting as running (so it cannot block concurrency
  forever) and keeps its reserved amount charged (the provider may have done the work).
  TTLs: `USAGE_RESERVATION_TTL_SECONDS` (600; validated to exceed the 300 s lease) and
  `INGESTION_RESERVATION_TTL_SECONDS` (3600).
- **Retries.** A retry of a failed answer is a new provider call and gets a new reservation:
  its key includes the lease's start time. A replay of an answered question gets none. The
  same question sent twice at once gets `409 answer_in_progress` before any reservation.
  Provider-level retries inside one call (429/5xx before any text) are one usage row.

## Rate-limit algorithm

Sliding-window counter over two fixed windows, per policy and subject:

```
allowed  ⇔  previous_count × (1 − elapsed / window) + current_count ≤ limit
```

The current window's counter is incremented with one atomic
`INSERT … ON CONFLICT DO UPDATE … RETURNING count`. PostgreSQL serialises that on the row,
so concurrent requests each see a distinct count; a test with 20 concurrent requests and a
limit of 3 lets exactly 3 through. A refused request undoes its increment. `Retry-After` is
the time until the weighted count drops below the limit.

| Policy | Subject | Default |
|---|---|---|
| `chat:user` | verified user | 10 / minute (`/chat` and `/chat/stream` together) |
| `chat:workspace` | the conversation's workspace | 60 / minute |
| `search:user` | verified user | 30 / minute |
| `upload:user` | verified user | 10 / hour |

Subjects are only a verified user ID, or a workspace the caller was just authorised for. A
non-member's requests never count against a workspace: they get 404 first.

## Quota calculation

All in UTC, from `charged_tokens` / `charged_cost` of `usage_events`:

| Limit | Scope | Default | Applies to |
|---|---|---|---|
| Daily tokens | user, every workspace | 500,000 | every operation |
| Daily questions | user | 200 | answers (counted per attempt) |
| Concurrent answers | user | 3 | open, unexpired `chat` / `chat_stream` reservations |
| Monthly tokens | workspace | 5,000,000 | every operation |
| Monthly estimated cost | workspace | 10.00 (pricing currency) | every priced operation |
| Concurrent AI operations | workspace | 10 | open, unexpired reservations of any kind |
| Documents waiting / processing | workspace | 5 | uploads and retries |
| Input per answer | system | 12,000 tokens | the estimated maximum prompt |
| Output per answer | system | `CHAT_MAX_OUTPUT_TOKENS` (1,000) | the model request |

A reservation must fit: `used + reserved ≤ limit`. Workspace overrides take the minimum of
the system limit and the workspace's own. **Concurrency safety:** `reserve` takes
`pg_advisory_xact_lock` on the user and then the workspace (always that order, so there is no
deadlock), then reads the balances, checks, and inserts. A second reservation for the same
user or workspace waits for the first to commit, so both cannot spend the same remainder. A
test reserving 12 × 300 tokens at once against a 1,000-token quota stores exactly 3.

**Reservation sizes.**

- **Answer:** tokens of the question, plus the full context budget (6,000), the history budget
  (2,000), the instructions and 200 tokens of framing, all as input; `CHAT_MAX_OUTPUT_TOKENS`
  as output.
- **Document:** its text tokens × chunk size / (chunk size − overlap), plus 16.
- **Query embeddings** are not reserved. They are checked with `ensure_available` and recorded
  afterwards: each is a few tokens, and a lock on every search would serialise searching.

## Cost estimation

`Pricing.cost(model, input, output) = (input × input_per_million + output × output_per_million) / 10⁶`,
quantised to 8 decimal places.

- **Model matching:** on the longest configured name the model starts with, so a dated
  snapshot (`gpt-4.1-mini-2025-04-14`) uses its family's price.
- **Unpriced models:** their cost is `NULL`. Spending limits then cannot see them, but token
  quotas still apply.
- **What is stored:** every row stores `currency` and `pricing_version`, so older rows remain
  traceable after prices change. The default table holds OpenAI list prices as known when it
  was written (version `openai-list-2025-04`, USD). It is not an invoice: no discounts, no
  cached-input pricing, no tax.

## Provider usage limitations and the policy when usage is unknown

- **Completed answer:** the provider reports input and output tokens (Responses API `usage`;
  the stream's `response.completed` event).
- **Answer cut off by the output limit:** the incomplete response still reports usage, and it
  is recorded (`failed`, `provider`).
- **Stopped or disconnected stream:** there is no completed event, so no provider usage. We
  record `estimated`: the prompt we sent plus the tokens of the text received. The provider
  may have generated slightly more before it noticed the disconnect.
- **Non-streamed call that failed without a response** (timeout, 5xx after retries): unknown.
  We charge the reservation (`unavailable`). This is the conservative choice, because the
  provider may have billed it.
- **Refused before any work** (401/403/404, other 4xx): `none`, charged 0.
- **Embeddings:** `usage.prompt_tokens` per request. A batch that failed after being sent is
  counted at its own token count (`estimated`).
- **Estimates use the `cl100k_base` tokenizer,** which is close to, but not the same as, the
  model's tokenizer. They are always labelled `estimated`, never stored in the
  provider-reported columns.

## Failure handling

| Failure | Behaviour |
|---|---|
| Rate limit or quota exceeded | 429 with a specific code, `retry_after_seconds` in the body and `Retry-After` header. Nothing stored, provider not called. |
| Usage database unavailable | 503 `usage_unavailable`. **Fail closed:** the operation does not run unchecked. Recording a completed query embedding never fails the request; that failure is logged. |
| Duplicate reservation key | 409 `duplicate_operation` (the unique constraint). |
| Provider failure, timeout, rate limit | Reservation finalised `failed` with what is known; lease released. |
| Client disconnect / Stop | Generation stopped upstream; reservation finalised `cancelled` with an estimate; lease released at once (shielded from cancellation). |
| Answer cannot be stored | Reservation finalised `failed` with the provider's counts. |
| Process crash | Reservation expires after its TTL; the lease after 300 s. |
| Document over quota at ingestion | Document `failed` with a message naming the limit; no embedding call. |

## API endpoints

| Method and path | Who | Returns |
|---|---|---|
| `GET /api/v1/usage/me` | any signed-in user | Own usage today across workspaces, daily limits. |
| `GET /api/v1/workspaces/{id}/usage` | members | This month: totals, by operation, by model, by day, remaining allowance. Owners and admins: whole workspace. Members: their own (`scope: "own"`) plus the workspace allowance. |
| `GET /api/v1/workspaces/{id}/usage/history` | members | Paginated records (`limit` 1–100, `offset`, `operation`, `status`), newest first. `user_id` only for owners and admins. No text, keys or request IDs. |
| `GET /api/v1/workspaces/{id}/quotas` | members | Effective limits, system ceilings, the workspace's own settings, `can_edit`. |
| `PATCH /api/v1/workspaces/{id}/quotas` | owners | Tighten `monthly_token_limit`, `monthly_cost_limit`, `max_concurrent_operations`; `null` resets to the system limit. A value above the system limit is refused with 422 `quota_above_system_limit`. |

Every workspace endpoint loads the workspace through the caller's own membership. A
workspace ID in the path is never proof of access: non-members get 404 `workspace_not_found`.

## Security hardening in this milestone

- SQLAlchemy `hide_parameters=True`, so bound values (message, answer and document text) never
  appear in logged database errors.
- `Retry-After` is exposed through CORS; it is also in the error body.
- Usage logs carry only IDs, operation names, statuses, codes and counts.
- Quota and pricing configuration is validated at startup: no negative limits, a TTL longer
  than the lease, a valid output cap, a well-formed price table.
- A workspace cannot raise its own limits; the system limits are the ceiling.
