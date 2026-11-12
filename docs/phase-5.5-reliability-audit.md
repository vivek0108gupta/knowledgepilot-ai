# Phase 5.5: usage reliability audit

Audit of `feature/security-cost-controls` at `2cb7080` (clean working tree), done before any
change. Work continues on `feature/phase-5.5-usage-reliability`. **Confirmed** findings were
read in the code or reproduced; **assumptions** are marked.

## Baseline

The test baseline is recorded in [phase-5.5-testing.md](phase-5.5-testing.md). The migration
head is `0007` (confirmed).

## Findings

### 1. Query embeddings are checked, not reserved (confirmed)

- `search.search` calls `usage.ensure_available`, an unlocked read of the balances. It then
  embeds, then records usage with `usage.record`, which inserts an already-final row.
- Chat retrieval (`chat.retrieve` → `search.embed_query`) does the same, without even the
  availability check (the answer's own reservation came first).
- Consequences:
  - concurrent searches all pass the same check and can together exceed a quota;
  - there is no reservation row while the call is in flight;
  - `record` swallows database errors, so a failed insert loses that usage without trace
    beyond a log line;
  - each `record` call uses a random idempotency key, so nothing would stop a duplicate.

### 2. Failure outcomes are coarse (confirmed)

`GenerationMeter.outcome` maps every failure to one of four sources:

| Case | Today | Problem |
|---|---|---|
| Refused before execution (401/403/404/400) | `none`, 0 | Correct. |
| Executed, provider returned usage (output-limit cut-off) | `provider` | Correct. |
| Executed, but the response carried no usage | `unavailable`, charged the **whole reservation** (context and history budgets plus maximum output) | Overstates. The prompt actually sent is known. |
| Timeout or connection error, execution unknown | `unavailable`, whole reservation | Overstates, and is not distinguishable from the row above. |
| Stream failed part-way | `estimated` (prompt plus text received) | Reasonable, but not labelled as partial. |
| User pressed Stop | `cancelled`, `client_disconnected` | Indistinguishable from a dropped connection: the server only sees the connection close. |
| Client disconnect | `cancelled`, `client_disconnected` | Correct. |

No column records *what happened at the provider*, separately from *where the numbers came
from*. The dashboard sums `charged_tokens` without saying how much of it is
reservation-held rather than reported or estimated.

### 3. Reservation expiry is fixed and lazy (confirmed)

- `expires_at = created_at + TTL` (600 s for answers, 3,600 s for documents) and is never
  extended. An ingestion that legitimately runs longer than an hour would be marked expired
  while still running (assumption: rare with the 25 MB upload limit, but possible with slow
  embeddings and retries).
- Stale reservations are expired only inside `reserve`, scoped to the same user or workspace.
  After a crash, a workspace that receives no new requests keeps them open, shown as
  "running", until someone uses it. Nothing sweeps them at startup or periodically.
- An expired reservation keeps its **whole reserved amount** charged, even when the provider
  was never called (for example, a crash during retrieval). That consumes quota permanently
  for the period.
- A late `finalize` (the operation finished after its reservation expired) returns `False`
  and the provider's real usage is discarded.

### 4. Ingestion is not durable (confirmed)

- `InProcessQueue` runs `asyncio` tasks inside the API process. Queued and running work is
  lost on restart. It is only rediscovered by a startup scan of document statuses, which
  re-queues documents that were `uploaded` or stale `processing`.
- Concurrency is per process (`PROCESSING_CONCURRENCY`), not per workspace. Two API processes
  double it; one workspace can occupy every slot.
- There is no automatic retry: a transient embedding or storage failure marks the document
  `failed`, and the user must retry by hand.
- The ingestion cap (`ensure_ingestion_capacity`) takes a workspace advisory lock **before the
  storage upload** and holds it until the commit. This serialises uploads within a workspace
  for the duration of each file transfer.
- The usage idempotency key is `document_embedding:{document_id}:{updated_at}`. That is
  unique per claim, but not tied to a job attempt.

### 5. No admin usage controls beyond workspace limits (confirmed)

- No per-member limits inside a workspace, and no threshold alerts or daily warnings (the
  dashboard computes warnings from the current numbers, but nothing is recorded).
- No audit trail of quota policy changes: `workspace_quota_policies` keeps only the latest
  values and `updated_by`.
- Roles available: `owner`, `admin`, `member`. There is no system-wide administrator role.

### 6. Other observations

- Workspace concurrency counts every open reservation. Reserving query embeddings naively
  would make an answer's own question embedding compete with it for a workspace slot
  (confirmed by reading `workspace_balance`).
- Frontend Stop aborts the `fetch`. There is no server endpoint to request cancellation
  (confirmed).

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Queue technology | PostgreSQL table with `FOR UPDATE SKIP LOCKED` claiming and leases. **No Redis or Celery.** | The database is already required. Jobs are a few per minute, and transactional enqueueing next to the document row is worth more than throughput. |
| Workers | Asyncio loop in each API process, started in the lifespan. | No new deployable. Safe with several processes because claiming is atomic and leased. |
| Per-workspace concurrency | Enforced when a job is claimed, under a workspace advisory lock. | Exact across processes. Uploads are no longer serialised. |
| Upload cap | Unlocked pre-check before the body is read; exact check under the lock only for the final count and commit, after the file is stored. | Removes serialisation during file transfer while keeping the cap exact. |
| Per-user overrides | Per **member within a workspace**, set by the workspace owner, stricter only. | There is no system-admin role. A workspace owner may govern use of their workspace, not a user's global allowance. |
| Alerts | Recorded in the database once per threshold and period, shown in the API and dashboard. **No email.** | No mail infrastructure exists. Recording is a durable base for notifications later. |
| Unknown outcomes | Charge an upper bound (estimated prompt plus maximum output), labelled `held`, never as provider usage. | Never zero when the provider may have billed; never the inflated context budget. |
