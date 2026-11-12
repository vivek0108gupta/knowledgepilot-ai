# Security and cost controls: testing

What was tested, how, and the results, as run on 2026-11-12 on branch
`feature/security-cost-controls`.

## Baseline before this milestone

| Suite | Result |
|---|---|
| Backend (`pytest`, real PostgreSQL + pgvector) | 862 passed |
| Frontend (`vitest`) | 366 passed |

## Results after this milestone

| Check | Result |
|---|---|
| Backend tests | **935 passed** (862 existing + 73 new in `tests/test_cost_controls.py`) |
| Frontend tests | **385 passed** (366 existing + 19 new) |
| Ruff lint and format | clean |
| Prettier, ESLint, TypeScript, `next build` | clean; `/dashboard/workspaces/[workspaceId]/usage` built |
| Alembic | `0007` upgrades, downgrades to `0006` (tables gone) and upgrades again on a scratch database; `alembic check` reports no drift |
| Docker | `docker compose build --no-cache`, all services healthy, `alembic current` = `0007` on the local database, existing data intact |
| Mutation checks | 10 of 10 protections removed one at a time were caught by the tests (two needed tests added first; see below) |
| Real OpenAI streaming test | **20 / 20** (14 earlier checks + 6 usage checks) |
| Real OpenAI 5.2 answering test | 10 / 10 |
| Phase 4 search end-to-end | 22 / 22 |
| Storage failure end-to-end | 23 / 23 |
| Phase 5.1 conversations end-to-end | 18 / 18 |
| Phase 3 documents end-to-end | 32 / 37: the same 5 failures as before this milestone. The script runs without an OpenAI key, and ingestion has needed one since Phase 4 (`embeddings_not_configured`). Not caused by this milestone. |

### Existing tests changed

- `tests/test_isolation.py`: the expected migration head changed from `0006` to `0007`.
- `tests/conftest.py`: the environment now pins generous limits for the whole suite, so tests
  about other features are never refused. `rate_limit_buckets` is emptied between tests.
- `components/chat/chat.test.tsx` is unchanged.
- `lib/conversations.test.ts` gained tests.
- No assertion was weakened or removed.

### End-to-end script changes

These are scratch scripts outside the repository. Their expected head changed to `0007`. The
document and streaming scripts raise the upload and chat rate limits for their throwaway API,
because they make more requests per minute or hour than the defaults allow. That the limits
refuse them otherwise is itself the limits working.

## What the new backend tests cover (`tests/test_cost_controls.py`)

- **Configuration:**
  - the documented defaults;
  - invalid limits refused at startup (zero or negative, a TTL not longer than the lease,
    output cap);
  - cost calculation, prefix matching and unpriced models;
  - the default price table is valid;
  - malformed price tables refused;
  - `hide_parameters` on the engine.
- **Rate limiter** (real PostgreSQL):
  - within the limit, then 429 with `Retry-After`;
  - refused requests not counted;
  - the sliding window and its reset;
  - users and policies counted separately;
  - **20 concurrent requests against a limit of 3: exactly 3 allowed**;
  - the off switch;
  - a database failure gives 503 `usage_unavailable`.
- **Rate-limited endpoints:**
  - `/chat` and `/chat/stream` share the per-user limit, and nothing is stored when refused;
  - the per-workspace limit covers all members;
  - a non-member's requests never count against a workspace;
  - search and upload limits;
  - **5 concurrent uploads against a cap of 2: exactly 2 stored**.
- **Chat metering:**
  - one completed row with the provider's counts, cost, currency, pricing version, message
    link and request ID;
  - the question's embedding as its own row, provider-counted or estimated;
  - a streamed answer is one row, not one per piece;
  - replays add no rows;
  - a concurrent duplicate is reserved once;
  - no-evidence answers charge 0;
  - a response without counts charges the reservation (`unavailable`).
- **Failure metering:**
  - timeouts and rate limits (`unavailable`);
  - authentication and rejected requests (`none`, 0);
  - output-limit cut-offs (`provider`, from the incomplete response);
  - a stream failing part-way (`estimated`, output counted from what was received);
  - an answer that cannot be saved (`failed`, provider counts kept);
  - a client disconnect (`cancelled`, `estimated`);
  - cancelled before the model was called (0).
- **Ledger rules:**
  - a second reservation with the same key gives 409;
  - finalising twice records once;
  - **10 concurrent finalisations record exactly once**;
  - negative tokens refused;
  - database constraints reject malformed rows.
- **Ingestion:**
  - one reserved and reconciled row per document;
  - a document over quota fails with a clear message and no embedding call;
  - a failed embedding records an estimate;
  - **4 concurrent ingestion-slot claims against a cap of 1: exactly 1 succeeds**.
- **Quotas:**
  - the user's daily tokens: refused before the question is stored and before any
    provider call;
  - the daily question limit;
  - the workspace's monthly tokens and spending (reservation, then reconciliation frees the
    unused part);
  - concurrent generations per user, with the slot freed after an answer or a failure;
  - **12 concurrent reservations against a quota for 3: exactly 3 stored, 900 tokens**;
  - abandoned reservations expire, stop blocking and stay charged;
  - search refused once the allowance is spent;
  - deleting a workspace does not reset a user's daily quota.
- **APIs and authorization:**
  - owners and admins see the whole workspace, with user IDs;
  - members see only their own usage;
  - history filters, paging and validation;
  - no content, idempotency keys or request IDs in responses;
  - outsiders get 404 and anonymous callers 401;
  - the same user's workspaces are kept separate;
  - only owners change quotas, only to tighten them, with validation, immediate effect and
    reset to `null`;
  - `/usage/me` requires sign-in;
  - logs hold no question text, answer text, tokens or `Bearer` headers.

## Mutation checks

Each protection was removed in turn and the suite re-run; every removal made a test fail:

| Protection removed | Caught by |
|---|---|
| Advisory locks in `reserve` | concurrent reservations cannot overspend |
| `status = 'reserved'` guard in `finalize` | concurrent finalisations record once *(test added after the first run missed it)* |
| Undoing a refused rate-limit hit | refused requests are not counted |
| Finalising on failure | failure-metering tests |
| Member scope filter | members see only their own usage |
| Owner-only quota changes | only owners change quotas |
| System ceiling on quota changes | only owners change quotas, only to tighten |
| Ingestion lock | two uploads cannot both take the last place *(test added after the first run missed it)* |
| Expiring stale reservations | abandoned reservations expire |
| Estimating cancelled streams | a stream stopped by the client records an estimate |

## Real-provider checks

The real tests used the OpenAI key already in `.env`, against a throwaway database and API
instance. They print no keys.

| Usage check | Result |
|---|---|
| One `chat_stream` row with OpenAI's own counts | 945 in / 182 out, $0.00066920 USD; reserved 8,726 + 1,000 |
| Two documents reserved and reconciled to the provider's counts | 69 of 94 reserved, 116 of 149 reserved |
| Question embedding with the provider's count | 12 tokens |
| Replay | No new usage row |
| Real disconnect mid-stream | `cancelled`, `estimated`: 964 input tokens estimated (the real prompt was about 945), 31 output tokens received |
| Logs | No keys, tokens, `Bearer` headers, questions or document text |

In the browser, with the rebuilt Docker stack:

- a streamed answer recorded 2,452 / 310 tokens, which match the message's own counts;
- the cost was $0.0014768, which matches 2,452 × $0.40/M + 310 × $1.60/M;
- the Usage page showed these numbers from the API;
- after the test conversation was deleted, its usage rows remained, unlinked from it.

## How to run

```bash
cd apps/api && uv run pytest
cd apps/api && uv run pytest tests/test_cost_controls.py
cd apps/web && npm test
```
