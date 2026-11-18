# Security and cost controls: audit

Audit of the repository before the security and cost controls milestone, on branch
`feature/security-cost-controls` (created from `feature/phase-5.3-streaming` at `3a64b87`, clean
working tree). **Confirmed** means read in the code or observed by running it. **Assumption**
means inferred and not verified.

## Baseline

| Check | Result |
|---|---|
| Backend tests (`pytest`, real PostgreSQL + pgvector) | 862 passed (confirmed, run before any change) |
| Frontend tests (`vitest`) | 366 passed (confirmed, run before any change) |
| Migrations | `0001`–`0006`, head `0006` (confirmed) |

## Existing architecture (confirmed)

- **API:** FastAPI, versioned under `/api/v1`. Routers: `workspaces`, `documents`, `search`,
  `conversations`, `chat`, `me`, `health`. Errors leave the API in one envelope
  (`{"error": {"code", "message", "request_id"}}`) through `AppError` (`app/core/errors.py`),
  which can also carry response headers.
- **Identity:** `get_current_user` (`app/api/deps.py`) verifies the Supabase JWT (ES256 via JWKS).
  It is the only source of the caller's ID; routes never read a user ID from a request.
- **Authorization:** `workspaces.get_access` loads a workspace only together with the caller's
  membership, returning 404 to non-members. `require_role` checks `owner` / `admin` / `member`.
  Conversations are private to their creator (`conversations.get_owned`).
- **Tenant isolation:** retrieval (`search.nearest`) filters on the workspace and joins the
  caller's membership in the same SQL. The chat workspace always comes from the conversation row.
- **AI provider boundary:** one cached `AsyncOpenAI` client (`openai_client.py`) is used by
  `OpenAIEmbedder` (`embeddings.py`; ingestion, search queries and chat retrieval) and
  `OpenAIResponsesModel` (`llm.py`; `/chat` and `/chat/stream`). Every AI call goes through one
  of those two classes.
- **Chat lifecycle:** `chat.begin` (stores the question, takes the per-conversation lease, with
  idempotency on `client_message_id`), then `prepare` (history, query embedding, retrieval,
  context), then the model, then `conclude` (citation checks), then `save` (stores the answer and
  releases the lease in one transaction). On failure, `release` frees the lease.
- **Streaming lifecycle:** `chat_stream.AnswerStream` runs the same steps. A disconnect or Stop
  closes the generator; `close()` releases the lease, shielded from cancellation;
  `EventStreamResponse` guarantees `close()` even when the stream is never read. A replay
  (`turn.reply` already exists) makes no model call.
- **Ingestion:** `processing.process_document` (in-process queue, `PROCESSING_CONCURRENCY`
  documents at a time per process): claim, download, extract, chunk (lazy iterator), embed in
  batches of 64, store, then `ready`. Failures are recorded on the document.
- **Upload:** any member, at most 25 MB (`MAX_DOCUMENT_BYTES`), with type validation and a hash
  for duplicate detection. The body is read only after the membership check.
- **Logging:** one format with a request ID. The middleware logs method, path, status and
  duration only. `httpx`, `httpcore` and `openai` loggers are at WARNING, so URLs (including
  signed storage URLs) are not logged.

## Existing security controls (confirmed)

- JWT verification with issuer, audience and expiry. Membership-scoped queries return 404 to
  outsiders.
- Strict request schemas (`extra="forbid"`), size limits on messages (8,000 characters),
  `top_k` bounds, a document filter of at most 50 IDs.
- Upload validation by type and content, 25 MB cap, storage paths checked against the
  document's own folder.
- Prompt-injection defences, citation validation, leak detection on provisional and final text.
- Secrets in `SecretStr`. The OpenAI key is only in the API container.
- One answer at a time per conversation (lease). Idempotent retries.

## Security and cost gaps (confirmed)

1. **No rate limiting** on any endpoint. A user can call `/chat`, `/chat/stream`, `/search`
   and upload as fast as they like.
2. **No limit on concurrent generations per user.** The lease allows one answer per
   conversation, but a user can open many conversations and stream in all of them at once.
3. **No quotas.** Nothing bounds tokens or spending per user, per workspace or per period.
4. **Usage is not recorded as usage.** Assistant messages store `input_tokens` / `output_tokens`
   for successful answers only. Nothing is stored for:
   - failed, cancelled or partially streamed generations;
   - query embeddings (search and chat retrieval);
   - document embeddings during ingestion. The provider's `usage.prompt_tokens` is discarded.
5. **No cost estimate or pricing configuration** anywhere.
6. **Ingestion has no workload limit per workspace.** `PROCESSING_CONCURRENCY` is per process,
   not per workspace. One workspace can queue any number of documents, and so any amount of
   embedding spend.
7. **SQL parameters can reach the logs.** SQLAlchemy includes bound parameters in its exception
   messages. The API logs some database errors with `logger.exception`, for example "could not
   store the answer". Such a message can contain answer text or other content. The engine does
   not set `hide_parameters=True`.
8. **Workspace deletion would erase usage history** if usage were keyed to the workspace with
   `ON DELETE CASCADE`. A user could then reset quotas by deleting and recreating workspaces.

## Current provider usage tracking (confirmed)

| Operation | Provider usage available | Currently stored |
|---|---|---|
| `/chat` generation | `response.usage` (input / output tokens) | On the assistant message only |
| `/chat/stream` generation | `response.completed` event's usage | On the assistant message only |
| Stream cancelled or failed mid-way | No completed event, so no usage | Nothing |
| Response cut off (`max_output_tokens`) | `response.usage` on the incomplete response | Nothing |
| Query embedding (search, chat retrieval) | `usage.prompt_tokens` | Nothing |
| Document embedding (ingestion) | `usage.prompt_tokens` per batch | Nothing |

## Potential duplicate-usage scenarios

| Scenario | Today | Required |
|---|---|---|
| Retry of an answered question (same `client_message_id`) | Replays, no model call (confirmed) | No usage record |
| Replayed SSE stream | Replays, no model call (confirmed) | No usage record |
| Same question sent twice at once | Second gets `409 answer_in_progress` (confirmed) | One reservation only |
| Retry after a failed answer | New model call (confirmed) | A new, separate usage record (it is a new provider call) |
| Provider retries inside one call (429/5xx before any text) | Up to 3 attempts in `llm.py` (confirmed) | One logical usage record |
| One streamed answer | Many `text_delta` events | One usage record, not one per delta |
| Finalising the same reservation twice (for example `close()` after `save()`) | Not applicable yet | Must be impossible |

## Tenant isolation risks

- Usage queries must be filtered on a workspace the caller is a member of, with the membership
  checked in the same request. A workspace ID in the path is never proof of access.
- Per-member breakdowns reveal what colleagues do. Assumption: only owners and admins should
  see workspace-wide usage; members see their own usage plus the workspace's remaining
  allowance.
- Quota changes are a billing decision. Assumption: only owners may change them, and only to
  make them stricter than the system limits, so that a workspace cannot raise its own ceiling.

## Rate-limiting opportunities

- Chat and streaming share one policy (both start a generation): per user and per workspace.
- Search: per user (it spends query embeddings).
- Upload: per user per hour, plus a cap on documents waiting or processing per workspace.
- Concurrent generations: per user, enforced with the same reservations that guard quotas.

## Recommended architecture

**PostgreSQL only, no Redis.** Redis would add a service, persistence and backup concerns, and a
second source of truth, for request rates this application does not have. Every request
already uses PostgreSQL, so atomic upserts and advisory locks give exact enforcement across any
number of API processes. If request rates ever make the extra writes a bottleneck, the limiter
is one module and can be moved to Redis.

- **Rate limiter** (`app/services/rate_limits.py`): sliding-window counter over two fixed
  windows, stored in `rate_limit_buckets`. Atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING`;
  over-limit hits are undone. Keys come only from the verified user and from workspaces the
  caller was authorised for.
- **Usage ledger** (`app/services/usage.py`): one row per provider operation in `usage_events`.
  The row is created as a reservation (`status = 'reserved'`, with the estimated maximum) before
  the provider is called, and finalised exactly once (`completed`, `failed`, `cancelled` or
  `expired`) with provider-reported usage, or an estimate, or nothing, each labelled.
  Reservation and event are one row: two tables would have to be kept in step for every
  operation.
- **Quota enforcement:** in the reservation transaction, take transaction-scoped advisory locks
  on the user and on the workspace (always in that order). Then expire stale reservations, sum
  what is charged, check the limits and insert the reservation. Two concurrent requests cannot
  both see the same balance.
- **Pricing** (`app/services/pricing.py`): a versioned price table with an explicit currency, in
  configuration. The default table can be replaced with `USAGE_PRICING_JSON` without code
  changes.
- **Integration points:** `chat.begin` (reservation in the same transaction that stores the
  question), `chat.save` / `chat.release` / `AnswerStream.close` (finalisation), `chat.retrieve`
  and `search.search` (query embeddings), `processing.process_document` (document embeddings),
  the upload route (rate limit and ingestion cap).

## Proposed schema (migration `0007`)

- `rate_limit_buckets(policy, subject_id, window_start, count)`, primary key on the first three.
- `usage_events`:
  - identity: `id`, `workspace_id` (FK, `ON DELETE SET NULL`, so history and user quotas survive
    deletion), `user_id`;
  - what: `operation`, `provider`, `model`, `status`, `usage_source`;
  - token columns, each kept separately: reserved, provider-reported, estimated, charged;
  - cost: `estimated_cost`, `reserved_cost`, `charged_cost` (`NUMERIC(16,8)`), `currency`,
    `pricing_version`;
  - references: `request_id`, a unique `idempotency_key`, `conversation_id`, `message_id`,
    `document_id` (FKs, `SET NULL`);
  - `error_code`, `created_at`, `completed_at`, `expires_at`;
  - check constraints on every enumerated value and on non-negative numbers;
  - indexes for workspace and time, user and time, active reservations, operation and model.
- `workspace_quota_policies(workspace_id PK FK CASCADE, monthly_token_limit,
  monthly_cost_limit, max_concurrent_operations, updated_by, updated_at)`. These are
  overrides, which may only be stricter than the system defaults.

## Risks and implementation order

1. Schema, pricing and configuration (no behaviour change).
2. Rate limiter and usage/quota services, with unit and concurrency tests.
3. Integration:
   - chat, then streaming (the riskiest: terminal paths, cancellation, replay);
   - search, upload, ingestion.
4. Usage and quota APIs, with authorization tests.
5. Frontend dashboard and error messages.
6. Full regression, Docker rebuild, migration up and down, real-provider check.

Risks:
- Extra transactions on the chat path. Mitigation: two short transactions, measured in tests.
- Stricter limits surprising existing tests. Mitigation: the test environment pins generous
  limits; dedicated tests use the real defaults.
- Stale reservations after a crash. Mitigation: `expires_at`, swept on every reservation.
