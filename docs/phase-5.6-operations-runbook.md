# Phase 5.6: operations runbook

Procedures for running KnowledgePilot AI in production. Background:
[production readiness audit](phase-5.6-production-readiness-audit.md),
[Phase 5.5 operations](phase-5.5-operations.md),
[cost-control operations](security-cost-controls-operations.md).

## Health at a glance

```sql
-- Ingestion: queued, running and recently failed jobs
SELECT status, count(*) FROM ingestion_jobs GROUP BY status;
SELECT id, document_id, attempts, max_attempts, last_error, leased_until
FROM ingestion_jobs WHERE status = 'running' AND leased_until < now();   -- stalled (should be empty)

-- Usage: open reservations and how this month's usage was counted
SELECT count(*) FROM usage_events WHERE status = 'reserved' AND expires_at < now();  -- overdue (should be empty)
SELECT outcome, usage_source, count(*), sum(charged_tokens) FROM usage_events
WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' GROUP BY 1, 2;
```

Each API process recovers stalled jobs and overdue reservations itself every
`USAGE_SWEEP_INTERVAL_SECONDS`. Non-empty "should be empty" results that persist for more
than a few minutes mean no API process is running maintenance.

## Incidents

| Symptom | Likely cause | Action |
|---|---|---|
| `/health/ready` 503, chat answers 500 | Database unreachable | Restore database connectivity. In-flight ingestion is retried automatically (tested: no job lost). Reservations that could not be closed are recovered by the sweep after their TTL. |
| Uploads answer 502 `storage_unavailable` | Supabase Storage unreachable, or the key was rotated | Check Supabase status and `SUPABASE_SECRET_KEY`. Documents whose download failed are retried automatically. |
| Answers 503 `ai_unavailable` | OpenAI slow, down or rate-limited | Check OpenAI status. Usage of timed-out calls is held conservatively (`outcome = unknown`). |
| Answers 503 `ai_misconfigured` | The OpenAI key, permissions or model were rejected | Check `OPENAI_API_KEY` and `OPENAI_CHAT_MODEL`. Note that gpt-4.1-mini is marked deprecated. |
| Many 429 `workspace_quota_exceeded` | A workspace reached its monthly limit | Expected. Review its usage page. Raise the system limit or remove the workspace's own override if justified. |
| Many 429 `rate_limited` | Bursts above the per-minute limits | Expected for abuse. Review `RATE_LIMIT_*` if legitimate users are affected. |
| Documents stuck `uploaded` | No API process running workers, or the workspace's ingestion slots are full | Check that API processes are up. Jobs show in `ingestion_jobs`. Maintenance re-queues documents without a job. |
| API process will not stop | It should not happen: shutdown waits are bounded (about 10 s) | If killed, its jobs are recovered after `INGESTION_JOB_LEASE_SECONDS`. |
| Requests answer 413 `request_too_large` | The client sent more than `MAX_REQUEST_BODY_BYTES` | Expected. Only raise the limit if a legitimate client needs more. |

## Restarting and deploying API processes

- **Graceful stop (`SIGTERM`):** the process claims no new jobs, lets running jobs finish
  for `INGESTION_SHUTDOWN_GRACE_SECONDS` (5), then hands the rest back to the queue at once,
  without charging an attempt. Another process continues them straight away. Give the
  container at least 15 s to stop.
- **Hard stop or crash:** that process's running jobs are retried by another process after
  their lease (`INGESTION_JOB_LEASE_SECONDS`, 600 s by default). Their usage reservations are
  expired as `abandoned` after the reservation TTL, charged a held upper bound.
- **Rolling deploys are safe:** each replica migrates under an advisory lock, and old and new
  replicas share the queue. Deploy migrations that are backward compatible with the running
  version.

## Backups and restores

Rehearsed on 2026-11-12: a dump of the local database restored into a scratch database with
identical row counts in every table, vectors included.

**Back up** (daily, and before every migration). Use your provider's managed backups where
available. Otherwise:

```bash
pg_dump --format=custom --no-owner --dbname "$DATABASE_URL_SYNC" --file "knowledgepilot-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

- `DATABASE_URL_SYNC` is the database URL without `+asyncpg`.
- Store dumps encrypted, off the database host, with a retention policy. They contain every
  tenant's documents (chunk text), conversations and usage.

**Restore** (into a new, empty database with the `vector` extension available):

```bash
createdb knowledgepilot_restored
pg_restore --no-owner --dbname knowledgepilot_restored knowledgepilot-<timestamp>.dump
```

Then:

1. Point a single API instance at it.
2. Check that `/health/ready` is 200 and `SELECT version_num FROM alembic_version` shows the
   expected head.
3. Spot-check workspaces and documents.
4. Switch traffic over.

**Storage is backed up separately.** Files live in the Supabase bucket; enable Supabase's
backups for it. After a database restore to an earlier point, run the storage audit (below)
to find files without documents. Documents restored without files show as missing.

## Storage consistency audit

```bash
# Report only (safe at any time)
python -m app.tools.storage_audit
# Remove orphaned files older than an hour
python -m app.tools.storage_audit --delete
# Only one workspace's folder, immediately (for example after a failed workspace deletion)
python -m app.tools.storage_audit --delete --min-age-minutes 0 --prefix <workspace-id>/
```

- Run it inside an API container, which has the database URL and storage key.
- It reports:
  - objects with no document (orphans);
  - documents whose file is missing;
  - unreferenced objects too recent to judge (an upload may be in flight).
- It deletes nothing unless `--delete` is given. It re-checks every candidate against the
  database just before deleting, and deletes nothing if the bucket cannot be listed
  completely.
- Suggested schedule: weekly report; `--delete` after review.
- After deleting a workspace, if the logs show "Could not remove … orphaned file(s)", run it
  with `--prefix <workspace-id>/`.

## Pricing changes

1. Check https://developers.openai.com/api/docs/pricing.
2. Set `USAGE_PRICING_JSON` with a **new** `version`, plus `effective_from` and
   `verified_on`.
3. Restart the API.

Existing rows keep the version and cost they were recorded with. Rows still in progress
record the version actually used when they are finalised. The Usage page shows the effective
and verified dates. Costs are estimates, never invoices; cached-input discounts are not
modelled, so estimates err high.

## Alerts

- Recorded in `usage_alerts`, once per subject, kind, threshold and period, so repeated
  requests or polling cannot multiply them.
- Shown on the Usage page and in `GET /usage/me`. There is no e-mail; connect a notifier to
  the table if needed.
- Daily alerts reset at 00:00 UTC, monthly alerts on the 1st (UTC).

## Access and audit

- Quota changes (owners only, tightening only) and member limits are recorded in
  `audit_events`, with before and after values.
- Read them with `GET /api/v1/workspaces/{id}/audit` (owners and admins).
