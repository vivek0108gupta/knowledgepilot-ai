# Phase 5.5: testing

Run on 2026-11-12, branch `feature/phase-5.5-usage-reliability`.

## Baseline (before any change)

| Suite | Result |
|---|---|
| Backend | 935 passed |
| Frontend | 385 passed |
| Migration head | `0007` |

## Results

| Check | Result |
|---|---|
| Backend tests | **977 passed** (935 baseline + 41 in `tests/test_reliability.py` + 1 new case in `tests/test_cost_controls.py`) |
| Frontend tests | **391 passed** (385 + 6) |
| Ruff, Prettier, ESLint, TypeScript, `next build` | clean |
| Migration `0008` | Upgrade, downgrade to `0007` (new tables and column gone), upgrade again on a scratch database; `alembic check`: no drift. Applied by the API container to the local database: head `0008`, the 2 existing usage rows classified `executed_with_usage`, other data intact. |
| Docker | `docker compose build --no-cache`; all services healthy; workers running; `POST …/chat/cancel` without a token is 401 |
| Real OpenAI streaming test | **24 / 24** (the 20 earlier checks + 4 for Phase 5.5) |
| Real OpenAI 5.2 answering test | 10 / 10 |
| Search end-to-end (now through the job queue) | 22 / 22 |
| Storage failures end-to-end | 23 / 23 |
| Conversations end-to-end | 18 / 18 |
| Phase 3 documents end-to-end | 32 / 37, the same 5 failures as before this phase. That script runs without an OpenAI key, which ingestion has needed since Phase 4. |

## Existing tests changed, and why

Three assertions in `tests/test_cost_controls.py` encoded the old accounting that Phase 5.5
was asked to replace. Each now asserts the new behaviour, with stricter checks. None was
removed.

| Test | Before | After |
|---|---|---|
| Missing provider counts | The whole reservation charged | `estimated`, `executed_without_usage`: the prompt sent plus the output received; provider columns empty; less than the reservation |
| A failed answer records what is known (timeout case) | The whole reservation charged | `unavailable`, `unknown`: held at prompt sent + maximum output; provider and estimate columns empty; less than the reservation. A new case: a rate-limited call is `rejected`, 0 |
| Abandoned reservations expire | The reservation charged | Never sent: `none`, 0. A second row that was sent: held at 400 (prompt 300 + output 100). `outcome = abandoned`, `expired_at` set |

Other changes:

- `tests/test_isolation.py`: the expected migration head is now `0008`.
- `components/chat/chat.test.tsx`: the Stop test now also expects the cancel request (for the
  right conversation), and waits for the abort that follows it.
- `tests/fakes.py`: unchanged in this phase.

## New tests (`tests/test_reliability.py`, 41)

- **Query embedding reservation:**
  - a search is reserved, then reconciled to the provider's count;
  - an exhausted quota stops the search before the provider;
  - **10 concurrent searches** against a tight quota: the total charged stays within it, and
    no refused search reached the provider;
  - failures are recorded as `unknown` (held) or `rejected` (0);
  - an answer's question embedding is keyed to its answer;
  - question embeddings take no concurrency slot.
- **Provider outcomes:**
  - a streamed answer without usage (estimated);
  - a timeout with no text (held upper bound);
  - a rate-limited stream (`rejected`, 0);
  - a partial stream;
  - a user's Stop with a cancel request (`cancelled_by_user`);
  - a dropped connection (`client_disconnected`);
  - only the conversation's owner can request cancellation.
- **Recovery:**
  - the sweep recovers others' abandoned reservations (never sent: 0; sent: held);
  - a heartbeat keeps a long operation alive;
  - **4 concurrent sweeps** over 6 rows expire each exactly once;
  - a late result replaces the held charge once;
  - an abandoned concurrency slot is freed.
- **Durable jobs:**
  - an upload creates its job in the same transaction;
  - a claimed job is processed to ready, its usage keyed to the attempt;
  - **8 concurrent claimers on one job: exactly one claim**;
  - enqueueing twice keeps one job;
  - per-workspace running limit, with another workspace still served;
  - a transient failure after a batch was written is retried and succeeds with **no
    duplicate chunks** and one usage row per attempt;
  - retries stop at the limit and fail the document;
  - permanent failures are not retried;
  - **recovery after a process stops**: the lease expires, the job is recovered and finished
    by another worker, and the stopped worker's late result is discarded;
  - startup maintenance recovers jobs and reservations;
  - the worker loop processes queued documents;
  - deleting a document removes its job.
- **Admin controls:**
  - a member's monthly token and daily question limits;
  - only owners set them, only for members, never above the workspace limit, with input
    validation;
  - managers list them;
  - every change is audited with before and after;
  - only managers read the audit log;
  - thresholds record one alert per period;
  - users' daily warnings appear only in their own usage;
  - alerts stay in their workspace;
  - the summary's reported, estimated and held amounts add up to the total.
- **Cross-tenant:**
  - outsiders get 404 on alerts, the audit log and member quotas;
  - members get 403 on manager-only endpoints;
  - another workspace sees none of the alerts.

## Mutation checks

Each protection was removed in turn. All of these were caught:

- the per-workspace running limit when claiming;
- the lease-owner check on results;
- the free expiry of never-sent reservations;
- reserving search embeddings, tested by reserving 0 tokens;
- heartbeats extending expiry;
- late reconciliation;
- member quotas;
- audit logging;
- telling Stop from a disconnect;
- question embeddings taking a slot;
- rate-limited calls charged as unknown.

The attempt limit is enforced twice: `run_job` stops allowing retries on the last attempt, and
`complete()` independently fails a job with no attempts left. Removing one layer leaves the
behaviour unchanged; removing both is caught.

## Real-provider checks added (Phase 5.5)

- **Job queue:** both documents ingested through the durable queue, once each (`succeeded`,
  attempt 1), with usage keyed `document_embedding:{job}:1`.
- **Question embedding:** reserved 13 tokens, reconciled to the 12 OpenAI reported. Keyed to
  its answer.
- **Answer:** `outcome = executed_with_usage`. The prompt counted here was 1,020 tokens; OpenAI
  reported 945.
- **Cancellations:**
  - a dropped connection: `client_disconnected`, estimated;
  - a user's Stop (cancel request, then close): `cancelled_by_user`.
- **No reservation left open** at the end. The logs held no keys, tokens or content.
