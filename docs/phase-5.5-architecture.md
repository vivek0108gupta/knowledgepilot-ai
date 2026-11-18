# Phase 5.5: usage reliability, architecture

What changed on top of the security and cost controls
([architecture](security-cost-controls-architecture.md)), and why. Findings:
[phase-5.5-reliability-audit.md](phase-5.5-reliability-audit.md).

## Database changes (migration `0008`)

New tables and new nullable columns only. No existing value changes; existing usage rows are
classified into the new `outcome` column from what they already record.

| Change | Purpose |
|---|---|
| `usage_events.outcome` | What happened at the provider (below). A check constraint lists the values. |
| `usage_events.provider_called_at`, `prompt_tokens_sent` | When the provider call began, and the prompt counted here at that moment: the basis of a held upper bound. |
| `usage_events.heartbeat_at` | Last sign of life. Each heartbeat moves `expires_at` out. |
| `usage_events.cancel_requested_at` | The user pressed Stop (as opposed to the connection dropping). |
| `usage_events.expired_at`, `late_reconciled_at` | Audit trail of recovery. |
| `workspace_quota_policies.alert_threshold_percent` | A workspace's own alert threshold (1–100). |
| `ingestion_jobs` | Durable jobs; partial unique index: one active job per document. |
| `workspace_member_quotas` | A member's own limits within a workspace. |
| `usage_alerts` | Thresholds crossed, unique per subject, kind, threshold and period. |
| `audit_events` | Append-only record of policy changes (before and after). |

Deletion: jobs, member quotas and alerts go with their workspace or document (`CASCADE`).
Audit events and usage outlive them (`SET NULL`).

## Usage reconciliation

### Two separate questions per record

| `outcome`: what happened at the provider | `usage_source`: where the numbers come from |
|---|---|
| `not_called`: never sent | `none`: 0 charged |
| `rejected`: refused before any work (auth, bad request, rate limit) | `none`: 0 charged |
| `executed_with_usage`: ran and reported usage | `provider`: exact, in `input/output/total_tokens` |
| `executed_without_usage`: a whole response arrived without usage | `estimated`: prompt sent plus output received, counted here |
| `partial_stream`: a stream failed after some output | `estimated`: prompt plus the output received |
| `cancelled_by_user`: Stop, with a cancel request | `estimated`: prompt plus the output received |
| `client_disconnected`: the connection closed | `estimated`: prompt plus the output received |
| `unknown`: a timeout or lost connection with nothing back | `unavailable`: **held** upper bound |
| `abandoned`: never finalised (the process stopped) | `none` if never sent; else `unavailable`, held |

**The held upper bound** is the prompt actually sent (`prompt_tokens_sent`, counted when the
call began) plus the most output the call could have produced (the reserved output). It is
written only to `charged_tokens` / `charged_cost`. It is never written to the provider
columns, nor to the estimate columns. Before Phase 5.5, an unknown result charged the whole
reservation, which includes the unused context and history budgets.

**Rate limits.** The model wrapper's `LLMError.maybe_executed` is `False` when the last attempt
was refused with a rate limit (it did not run), and `True` for timeouts, lost connections and
5xx responses (it may have run).

### The dashboard and API make it visible

`GET /workspaces/{id}/usage` adds two things:

- `accounting`: `provider_reported_tokens`, `estimated_tokens` and `held_tokens` (open
  reservations count as held), which add up to the total, plus `not_charged_records`;
- `outcomes`: counts of records by outcome.

History items carry `outcome`. The dashboard shows the three amounts side by side.

### Query embeddings are now reserved

`search.embed_query` (used by search and by chat retrieval) runs as follows:

1. Reserve the estimated tokens under the usual advisory locks and quota checks. An exhausted
   quota stops the search before the provider.
2. `mark_called`.
3. Embed.
4. Finalise with the provider's count. A failure is finalised `unknown` (failed after retries)
   or `rejected` (refused); a cancellation, `unknown`.

Each search gets a fresh key. A chat answer's question embedding is keyed to the answer's own
reservation (`query_embedding:{answer reservation}`).

Query embeddings take no concurrency slot: they last a fraction of a second, and an answer's
own question embedding must not compete with the answer for its workspace's slot.

## Reservation recovery

- **Heartbeats.**
  - A streamed answer refreshes its reservation every `USAGE_HEARTBEAT_SECONDS` (30) while
    text arrives.
  - A document refreshes after every embedding batch.
  - Each refresh sets `expires_at = now + TTL`, so a long operation that is alive is never
    expired.
  - A non-streamed answer is bounded by its timeouts (45 s × 3 attempts plus backoff), which
    is below the 600 s TTL. Settings validation enforces that TTLs exceed the lease and are at
    least twice the heartbeat interval.
- **Sweeps.** `usage.sweep_expired` runs in every API process at startup and every
  `USAGE_SWEEP_INTERVAL_SECONDS` (60). Every reservation also sweeps its own user and
  workspace first. Rows are taken `FOR UPDATE SKIP LOCKED`, so concurrent sweeps never expire
  the same reservation twice; a test runs 4 sweeps concurrently over 6 rows and gets exactly 6.
- **Charging on expiry.**
  - Never sent (`provider_called_at` empty): **0**. A crash before the call no longer consumes
    quota until the period resets.
  - Sent: the held upper bound, `usage_source = unavailable`.
  - Either way: `status = expired`, `outcome = abandoned`, `expired_at` set.
- **Late results.** If the operation finishes after its reservation expired, its real outcome
  replaces the held charge **once**, and `late_reconciled_at` is set. A second attempt changes
  nothing. Every other finalisation still happens exactly once (`WHERE status = 'reserved'`,
  the row locked).

## Durable ingestion jobs

### Job lifecycle

```
upload ─► document + queued job (one transaction)
queued ──claim (SKIP LOCKED, per-workspace lock, attempts+1, lease)──► running
running ──ready──► succeeded
running ──permanent failure (extraction, quota exhausted, provider rejected)──► failed
running ──transient failure, attempts left──► queued (available_at = now + delay × attempt)
running ──transient failure, none left──► failed, document failed
running ──lease expired (worker stopped)──► queued, or failed if no attempts left
```

### Rules

- **Claiming** is `SELECT … FOR UPDATE SKIP LOCKED` on the oldest available job. Then, under a
  per-workspace advisory lock, the claimer counts that workspace's running jobs against
  `INGESTION_WORKERS_PER_WORKSPACE` (2). Exact across any number of processes; a test with 8
  concurrent claimers on one job gets exactly one claim.
- **Leases.**
  - The worker extends `leased_until` after every batch.
  - A stopped worker's job is recovered by any process's maintenance.
  - Results are written only `WHERE lease_owner = worker AND status = 'running'`, so a worker
    that lost its job cannot overwrite the next attempt.
- **Idempotency.**
  - An attempt's usage key is `document_embedding:{job}:{attempt}`.
  - Each attempt removes earlier chunks before writing.
  - The document claim (`uploaded → processing`) is atomic.
  - Retries and recoveries therefore leave neither duplicate chunks nor duplicate usage. A
    test fails after a batch was written, retries, and finds each chunk once and two usage
    rows, `failed` then `completed`.
- **Tenant isolation.** A job carries only the IDs from the document row. Processing re-checks
  the storage path belongs to that workspace and document.
- **Workers** run inside each API process (lifespan), up to `PROCESSING_CONCURRENCY` jobs per
  process. They wake on enqueue and poll every `INGESTION_POLL_SECONDS`.
  `recover_interrupted` still runs at startup and enqueues documents from before jobs existed;
  enqueueing is idempotent.
- **Upload cap without serialising uploads.**
  - An unlocked pre-check refuses before the body is read.
  - The exact check (`MAX_INGESTION_JOBS_PER_WORKSPACE`) runs under the workspace lock **after**
    the file is stored, just before the commit, so the lock is held for milliseconds, not
    during a transfer.
  - If refused there, the stored file is removed.

**Why not Redis or Celery:** the database is already required. Jobs are a handful per minute.
Creating the job in the same transaction as its document matters more than queue throughput,
and a second system would bring its own persistence, monitoring and failure modes.

## Admin usage controls

| Capability | Who | Endpoint |
|---|---|---|
| Member's own limits in a workspace (`monthly_token_limit`, `daily_question_limit`), never above the workspace's | owners set; owners and admins view | `PUT /workspaces/{id}/members/{user_id}/quota`, `GET /workspaces/{id}/members/quotas` |
| Workspace alert threshold (1–100 %) | owners | `PATCH /workspaces/{id}/quotas` (`alert_threshold_percent`) |
| Recorded alerts: workspace monthly tokens and cost at the threshold and at 100 %, users' daily tokens | members see the workspace's, and their own daily ones | `GET /workspaces/{id}/usage/alerts`, `GET /usage/me` (`alerts`) |
| Audit log of policy changes (actor, action, target, before and after) | owners and admins | `GET /workspaces/{id}/audit` |
| Record a user's Stop | the conversation's owner | `POST /conversations/{id}/chat/cancel` |

- Member limits are checked inside `reserve`, under the same locks, as `429 member_quota_exceeded`.
- Alerts are recorded when usage is finalised, with `INSERT … ON CONFLICT DO NOTHING` on the
  unique key, so concurrent finalisations cannot duplicate them.
- Audit events are written in the same transaction as the change they record.
- There is no system-wide administrator role in this application, so per-user limits are per
  member within a workspace. A user's global daily limits remain configuration.
