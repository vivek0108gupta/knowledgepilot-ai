# Phase 5.5: operations

Running the durable ingestion queue and usage recovery, and the new controls. Earlier
settings and procedures: [security-cost-controls-operations.md](security-cost-controls-operations.md).

## New settings

| Variable | Default | Meaning |
|---|---|---|
| `QUERY_RESERVATION_TTL_SECONDS` | 120 | Reservation lifetime for a question or search embedding. |
| `USAGE_HEARTBEAT_SECONDS` | 30 | How often running streams refresh their reservation. Must be at most half of every reservation TTL. |
| `USAGE_SWEEP_INTERVAL_SECONDS` | 60 | How often each API process expires abandoned reservations and recovers stalled jobs. |
| `USAGE_ALERT_THRESHOLD_PERCENT` | 80 | Default alert threshold (also used for users' daily warnings). Alerts are also recorded at 100%. |
| `INGESTION_WORKERS_PER_WORKSPACE` | 2 | Ingestion jobs running at once per workspace, across all processes. |
| `INGESTION_MAX_ATTEMPTS` | 3 | Attempts per document before it is marked failed. |
| `INGESTION_JOB_LEASE_SECONDS` | 600 | A running job not heard from for this long is taken back. |
| `INGESTION_RETRY_DELAY_SECONDS` | 30 | Delay before a retry, multiplied by the attempt number. |
| `INGESTION_POLL_SECONDS` | 5 | How often idle workers look for jobs. |
| `PROCESSING_CONCURRENCY` | 2 | Existing: jobs one API process runs at once. |
| `MAX_INGESTION_JOBS_PER_WORKSPACE` | 5 | Existing: documents waiting or processing per workspace before uploads are refused. |

## The ingestion queue

- **Where it runs.** Every API process starts workers at startup. Scaling the API scales
  ingestion; per-workspace limits stay exact across processes.
- **Restarts.** Queued jobs wait in the database. A job that was running when its process
  stopped is taken back once its lease (`INGESTION_JOB_LEASE_SECONDS`) expires, then retried.
  Its partial chunks are removed and its usage reservation is expired by the sweep.
- **Inspecting it:**

```sql
SELECT status, count(*) FROM ingestion_jobs GROUP BY status;
SELECT id, document_id, attempts, max_attempts, lease_owner, leased_until, last_error
FROM ingestion_jobs WHERE status IN ('queued', 'running') ORDER BY created_at;
```

- **A document failed after all its attempts:** its status is `failed` with "Processing failed
  repeatedly". Fix the cause (provider or storage availability), then use **Retry** on the
  document, which creates a new job.
- **Move a stuck running job back to the queue at once** instead of waiting for the lease.
  Only do this if its process is known to be gone:

```sql
UPDATE ingestion_jobs SET leased_until = now() WHERE id = '<job id>' AND status = 'running';
```

  The next maintenance pass (within `USAGE_SWEEP_INTERVAL_SECONDS`) requeues it.

## Usage recovery

- **Automatic.** Abandoned reservations are expired at startup and every
  `USAGE_SWEEP_INTERVAL_SECONDS` by every process. One never sent to the provider is charged
  0. One that was sent holds an upper bound (`usage_source = unavailable`). If its real result
  arrives later, it replaces the held charge once (`late_reconciled_at`).
- **Records needing attention** (held, estimated or late-reconciled) this month:

```sql
SELECT outcome, usage_source, count(*), sum(charged_tokens)
FROM usage_events
WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
GROUP BY 1, 2 ORDER BY 4 DESC;
```

- **Do not set held rows to 0 by hand** unless the provider's own usage report shows the
  calls did not run. Held amounts are deliberately conservative.

## Admin controls

- **A member's own limits** (owners): `PUT /api/v1/workspaces/{id}/members/{user_id}/quota`
  with `{"monthly_token_limit": 200000, "daily_question_limit": 50}`. `null` removes a limit.
  Values above the workspace's limits are refused.
- **Alert threshold** (owners): `PATCH /api/v1/workspaces/{id}/quotas` with
  `{"alert_threshold_percent": 90}`.
- **Alerts** are recorded in `usage_alerts` and shown on the Usage page. They are not emailed;
  a mailer can read the table.
- **Audit log** (owners and admins): `GET /api/v1/workspaces/{id}/audit`. Every quota and
  member-limit change, with who made it, and the values before and after.

## Remaining limitations

- No e-mail or webhook notifications for alerts.
- Workers run inside the API processes. A separate worker deployment would reuse
  `app/services/jobs.py` unchanged, but is not set up.
- Estimates use the `cl100k_base` tokenizer. The held bound assumes the maximum output.
- A user's daily limits are system-wide settings; only per-workspace member limits can be set
  by owners.
- Stop is labelled as the user's choice only if the cancel request reaches the server before
  the stream closes (the app waits up to 1.5 s); otherwise it is recorded as a disconnect.
