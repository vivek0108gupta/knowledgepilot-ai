# Phase 5.6: resilience testing

Run on 2026-11-12 on `feature/phase-5.6-production-readiness`. Audit:
[phase-5.6-production-readiness-audit.md](phase-5.6-production-readiness-audit.md).

## Method

The multi-process scenarios ran from a script (outside the repository) with:

- **Two real API processes** (uvicorn), each running the durable workers, sharing one
  **throwaway PostgreSQL database**. The database is reached through a TCP proxy that the
  script can cut.
- **Real Supabase Storage**, reached through an HTTP proxy that can return 503.
- **A local OpenAI stand-in** for embeddings and the Responses API, both streamed and whole,
  whose delays can be set and which can hang.
- **Locally signed test tokens.**

Short settings made failures quick to observe: a 30 s job lease, a 60 s ingestion reservation
TTL, a 5 s sweep, 0.5 s polling and a 1 s shutdown grace.

All data was temporary:

- The database was dropped at the end.
- The run's files in the real bucket were removed through workspace deletion. Then
  `python -m app.tools.storage_audit --prefix <run workspace>/` confirmed **0 objects** were
  left.
- No existing user data was read or changed. The backup rehearsal read the local database
  only.

## Results: 27 / 27

| # | Scenario | Result |
|---|---|---|
| 1 | Two API processes start against one database | Pass |
| 2 | **12 simultaneous uploads** through both processes | All 12 indexed in about 4 s. Work split 6 / 6 (or 7 / 5) between the processes. Every job ran exactly once (`attempts = 1`). |
| 3 | Integrity after the uploads | No duplicate `(document, chunk_index)`, no stuck jobs, at most one completed indexing record per document |
| 4 | **6 simultaneous chat streams** (6 users, both processes) | 6 / 6 completed, one usage record each |
| 5 | One user's 5 simultaneous answers, limit 3, across processes | 3 ran, 2 refused `concurrent_generation_limit` |
| 6 | **`kill -9`** of the process holding the running jobs | The other process finished them after about 30–34 s (the 30 s lease); the killed attempts re-ran |
| 7 | The killed attempts' usage reservations | Recovered by the sweep: `expired`, `abandoned`, held charge; none left open |
| 8 | Integrity after the crash | No duplicates, no stuck jobs |
| 9 | The crashed process restarts | It rejoins and serves |
| 10 | **Graceful stop (`SIGTERM`) during ingestion** | Unfinished jobs handed back at once (`worker_shutdown`, attempt given back). The other process finished them about 1 s later, not after the 30 s lease. |
| 11 | Integrity after the graceful restart | Pass |
| 12 | **Database unreachable** for about 9 s, during ingestion and requests | `/health/ready` 503. A chat request gets a clean 500 (`internal_error`, no traceback in the body). |
| 13 | Database back | API ready again; every in-flight ingestion job completed (no job lost) |
| 14 | Integrity after the database outage | Pass |
| 15 | **Storage unreachable** during uploads and processing | The upload during the outage refused with 502 `storage_unavailable`; nothing half-stored |
| 16 | Storage back | Documents whose download failed were retried (up to attempt 3) and indexed |
| 17 | **OpenAI timeout** (stand-in hangs): whole answer and stream | `/chat` 503 `ai_unavailable`; the stream ends with an `error` event `ai_unavailable` |
| 18 | Usage for the timed-out answers | `failed`, `outcome = unknown`, `usage_source = unavailable` (held), provider columns empty |
| 19 | **User Stop vs client disconnect** | Recorded as `cancelled_by_user` and `client_disconnected` respectively |
| 20 | **Quota exhaustion** (workspace monthly tokens set to 1) | Answers 429 `workspace_quota_exceeded`; the uploaded document fails with an explanation naming the limit |
| 21 | The quota change is audited | `quota_policy.updated` in the audit log |
| 22 | Integrity at the end | No duplicates, no stuck jobs |
| 23 | Reservations whose end could not be recorded (database down at the time) | Recovered by the sweep after their TTL; none left open |
| 24–25 | Clean-up of the real bucket | The run's prefix: 0 objects |
| 26–27 | Logs of both processes (about 7,000 lines) | No keys, no tokens, no `Bearer` headers |

## Issues found by these tests

1. **Graceful shutdown could hang (fixed, M10).** An earlier run showed a stopping process
   waiting indefinitely, during job hand-back.
   - **The trigger was in the test harness:** it waited for the process with a blocking
     call inside the same event loop that runs its database proxy, so the database was
     unreachable meanwhile.
   - **It exposed a real flaw anyway:** shutdown had no time limit.
   - **Fix:** waits are now bounded, and a job whose task does not stop is left to its lease,
     never handed back while it might still write.
   - The harness now waits without blocking. The scenario passes, and the process exits
     promptly.
2. **The streaming route broke the OpenAPI schema (fixed, M9).** Found while testing the
   production API-docs switch.
3. **The first version of the job hand-back reused an attempt number**, which collided with
   that attempt's usage key (`duplicate_operation`). Fixed before release: the hand-back
   raises `max_attempts` instead of lowering `attempts`. A test covers it.

## Automated tests added (`tests/test_production_readiness.py`, 33)

- **Request size:**
  - oversized body refused 413 before authentication;
  - undeclared length 411;
  - normal requests unaffected;
  - uploads keep their 25 MB limit;
  - the setting is validated.
- **Production settings:** API docs off in production, on in development, and overridable.
- **Concurrent migrations:** three `alembic upgrade head` at once on an empty database all
  succeed, and the version table has exactly one row.
- **Graceful shutdown:**
  - unfinished jobs handed back without losing an attempt, and finished by another worker
    with no duplicate chunks and one usage record per attempt;
  - jobs that finish within the grace period complete;
  - a stopping worker claims nothing.
- **Maintenance:** queues waiting documents that have no job, idempotently.
- **Document lifecycle:**
  - upload and commit succeed;
  - the commit (job creation) fails: no file, document or job is left;
  - the storage upload fails: nothing left;
  - clean-up fails: the orphan is found and removed by the audit;
  - the document, or the whole workspace, is deleted during processing: no chunks, jobs,
    files or open usage left.
- **Storage audit:**
  - a report changes nothing;
  - `--delete` removes only old, unreferenced objects;
  - the prefix limits the scope;
  - missing files are reported;
  - nothing is deleted if the bucket cannot be listed;
  - a document committed during the audit keeps its file;
  - a negative age is refused.
- **Pricing:**
  - finalisation records the price table it actually used;
  - changing prices never reprices history;
  - the table's dates are validated and shown in the summary.
- **Alerts and periods:**
  - repeated requests and polling do not multiply alerts;
  - a new day starts a new daily alert;
  - last month's usage does not count this month;
  - the daily limit applies across all of a user's workspaces;
  - duplicate usage keys are refused across operations.

## Full results

| Check | Result |
|---|---|
| Backend tests | See the final report: the baseline was 977 |
| Frontend tests | 393 passed (391 + 2 for the dashboard's pricing and held labels) |
| Real OpenAI streaming test | 24 / 24 |
| Real OpenAI answering test | 10 / 10 |
| Search, storage failures, conversations end-to-end | 22 / 22, 23 / 23, 18 / 18 |
| Phase 3 documents end-to-end | 32 / 37. The same 5 failures as every phase since Phase 4: the script runs without an OpenAI key. Not a regression. |
| Backup and restore rehearsal | `pg_dump -Fc` of the local database (108 KB, 1 s), restored into a scratch database: every table's row count identical, all 6 vectors present. The scratch database and dump file were removed. |
