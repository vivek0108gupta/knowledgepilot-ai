# Phase 5.7: deployment runbook (staging and production)

How to deploy, check and roll back KnowledgePilot AI. It extends the
[Phase 5.6 deployment checklist](phase-5.6-deployment-checklist.md) and
[operations runbook](phase-5.6-operations-runbook.md); where they overlap, this document wins.

## Environments

| | Staging | Production |
|---|---|---|
| Purpose | Rehearse every release and migration on production-like configuration | Customers |
| Database | Its own PostgreSQL 17 + pgvector instance | Its own instance, managed backups or PITR |
| Supabase | **A separate Supabase project** (auth users, JWKS, bucket) | Its own project |
| OpenAI | A separate API key (or project) with a **low monthly budget** | Its own key, with a budget alert set in OpenAI |
| Secrets | Own values; never copied from production | Own values |
| Data | Synthetic or volunteered test data only; **never a copy of production** | Customer data |
| `ENVIRONMENT` | `staging` (or `production` to rehearse production behaviour) | `production` |
| API docs | May be on (`API_DOCS_ENABLED=true`) | **Off.** Only turn on with written authorisation. |
| Domain | for example `staging.app.example.com`, `staging-api.example.com` | `app.example.com`, `api.example.com` |

Separate Supabase projects mean a staging token can never be accepted by production (a
different issuer and JWKS) and staging cannot see production files.

## Configuration

Start from `.env.example`. The values that **must** be set per environment:

| Setting | Notes |
|---|---|
| `ENVIRONMENT` | `production` turns the API docs off and turns HSTS on |
| `DATABASE_URL` | `postgresql+asyncpg://…`, a private address, TLS if the provider supports it |
| `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_STORAGE_BUCKET` | The bucket must be private (the API refuses a public one) |
| `SUPABASE_JWKS_URL`, `SUPABASE_ISSUER` | Only with a custom auth domain |
| `OPENAI_API_KEY` | API only |
| `OPENAI_CHAT_MODEL`, `OPENAI_CHAT_FALLBACK_MODEL`, `CHAT_REASONING_EFFORT` | Defaults: `gpt-6-luna`, `gpt-5.4-mini`, `low` ([model evaluation](phase-5.7-model-evaluation.md)) |
| `CORS_ORIGINS` | Exactly the web app's `https://` origin(s) |
| `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Compiled into the web bundle: **build a separate web image per environment** |
| `METRICS_TOKEN` | Random, 32+ characters; reach `/metrics` only from the monitoring network |
| `LOG_FORMAT=json`, `LOG_LEVEL=INFO` | For the log shipper |
| `RATE_LIMIT_*`, `*_LIMIT`, `MAX_*` | Review for the plan; the defaults suit a small beta |
| `BACKUP_ENCRYPTION_KEY` | **Only** on the backup job, never on the API or web app |

### Secrets

- Keep them in the platform's secret store (or a vault), never in the repository, an image or
  a build log. `NEXT_PUBLIC_*` values are not secrets; everything else above is.
- Who can read them: the deploy pipeline and the on-call operator. Log access.
- **Rotation** (yearly, and at once if exposed, a person leaves, or a key shows in a log):

  | Secret | How |
  |---|---|
  | `OPENAI_API_KEY` | Create a new key, update the secret, roll the API replicas, then revoke the old key |
  | `SUPABASE_SECRET_KEY` | Create a new secret key in Supabase, update, roll, revoke the old one |
  | Database password | Add the new password (or user), update `DATABASE_URL`, roll, remove the old one |
  | `METRICS_TOKEN` | Update the API and the scraper together |
  | `BACKUP_ENCRYPTION_KEY` | Generate a new key for **new** backups; keep the old key for as long as backups made with it are retained |

## Migration order

Migrations run with `alembic upgrade head` in the container entrypoint, under an advisory
lock, so replicas starting together wait their turn (tested in Phase 5.6).

1. **Back up first** and verify it:
   `python -m app.tools.db_backup create` then `verify` ([backup strategy](phase-5.7-backup-strategy.md)).
2. Check the migration on staging with a copy of the **staging** database (never production
   data).
3. Write migrations to be **backward compatible** with the running version: add columns
   nullable or with defaults; remove columns and constraints only in a later release, after
   no running code uses them. Old and new replicas share the database during a rolling
   deploy.
4. `alembic check` against the target shows no drift after deploying.

## Deploying

Order: **database migration → API → workers → web app.** Workers run inside the API
processes, so deploying the API deploys them too.

1. **CI** on the release commit:
   - backend: `uv run ruff check`, `uv run ruff format --check`, `uv run pytest`;
   - frontend: `npm run lint`, `npx tsc --noEmit`, `npm test`, `npm run build`;
   - `pip-audit` and `npm audit --omit=dev` with no high or critical findings;
   - images built and tagged with the commit SHA (never `latest`).
2. **Staging:** deploy the same images, then run the smoke test below. For releases that
   touch answering or ingestion, also run `python -m app.tools.model_eval` (10 calls, a few
   cents) and one real upload.
3. **Production backup** (step 1 of the migration order).
4. **API:** roll the replicas one at a time (or blue-green):
   - stop timeout at least **20 s** (`stop_grace_period: 20s`): a stopping replica hands its
     ingestion jobs back within about 10 s;
   - the platform waits for **readiness** (`GET /api/v1/health/ready`, 200) before sending
     traffic to a new replica; liveness is `GET /api/v1/health`;
   - the proxy keeps streams open for at least 150 s with buffering off on
     `/api/v1/conversations/*/chat/stream`.
5. **Web app:** deploy the image built for this environment.
6. **Smoke test** (below), then watch for 30 minutes.

### Smoke test (after every deploy)

| Check | Expect |
|---|---|
| `GET /api/v1/health/ready` | 200, `database: ok`, a `pgvector` version |
| `GET /docs` and `/openapi.json` (production) | 404 |
| `GET /metrics` without the token | 401; with it, 200 and `metrics_database_up 1` |
| Response headers | `Strict-Transport-Security`, `X-Content-Type-Options`, `Content-Security-Policy` |
| Sign in, upload a small document | It reaches `ready` |
| Ask a question about it | An answer with a citation; the Usage page shows the operation |
| Logs | No `ERROR` lines after start-up |

### Watch (first 30 minutes, then the alerts take over)

From [observability](phase-5.7-observability.md): the 5xx rate,
`llm_requests_total{outcome!="ok"}`, `llm_fallbacks_total`, `ingestion_oldest_queued_seconds`,
`ingestion_jobs_stalled`, `usage_reservations_overdue`, and `database_connections`.

## Rollback

| What went wrong | Roll back by | Notes |
|---|---|---|
| A new API or web image misbehaves | Redeploy the previous image tags | Safe while migrations are backward compatible (rule 3) |
| A migration is wrong | Prefer a **forward fix** migration. If data was damaged: restore the pre-deploy backup (backup strategy, procedure A) | `alembic downgrade` only if the migration's downgrade was tested on staging; it may lose data written since |
| The chat model misbehaves or is removed | Set `OPENAI_CHAT_MODEL=gpt-5.4-mini` (or `gpt-4.1-mini` until 2027-01-14) and restart | No code change; parameters follow the model. A removed model already falls back automatically. |
| Costs spike | Lower `WORKSPACE_MONTHLY_*`, `USER_DAILY_*`, `RATE_LIMIT_*` and restart; owners can tighten their own workspace at once | |
| Storage files lost | `storage_backup restore --apply` | |

Keep the previous two image tags deployable, and record each deploy (time, SHAs, migration
head) in the change log.

## Domain and TLS

- Public DNS points at the platform's load balancer. Terminate TLS 1.2+ there, with
  automatically renewed certificates (for example Let's Encrypt or the platform's
  certificates). Alert 14 days before expiry if renewal is not automatic.
- Redirect HTTP to HTTPS. The API sends HSTS in production, and the web app always does.
- The database, `/metrics` and the backup storage are **not** public.
- Set the Supabase Auth site URL and redirect URLs to the production web origin only.

## Incident response

| Severity | Examples | Response |
|---|---|---|
| **SEV1** | Data exposed across workspaces, a leaked secret, the API down for everyone | Page on-call at once. Contain first (rotate the secret, scale to zero or block the route), then investigate. Notify affected customers as required by law and contract. |
| **SEV2** | Answers or uploads failing for many users, the provider down | Within 30 minutes. Communicate status. The [Phase 5.6 incident table](phase-5.6-operations-runbook.md#incidents) lists symptoms and actions. |
| **SEV3** | One workspace affected, slow ingestion | Next working day |

For every SEV1 and SEV2:

1. One person leads; another keeps a timeline.
2. Preserve evidence: logs (they carry `request_id`), metrics, and the release in production.
   Do not delete data while investigating.
3. Mitigate, then fix.
4. Write a post-incident review within five working days.

**A leaked secret:** rotate it (table above). Then search the logs and provider usage for its
misuse, and find out how it leaked.

## Verification record

Run on 2026-11-12 and 2026-11-13 on `feature/phase-5.7-launch-prep`, on the local machine
with Docker Compose. Real Supabase and real OpenAI were used where stated.

| Check | Result |
|---|---|
| Backend tests (`uv run pytest`) | **1,075 passed** (Phase 5.6 final: 1,011). 64 new tests: model selection 27, observability 15, backups 18, security headers 4. |
| Backend lint and format (`ruff check`, `ruff format --check`) | Clean |
| Frontend tests (`vitest`) | **393 passed** |
| Frontend lint, `tsc --noEmit`, `npm run build` | Clean; build succeeds |
| Docker: `compose build`, `up`, `/health/ready` | Healthy, schema `0008 (head)`. `alembic check`: no drift. |
| Live headers | The API sends `nosniff`, `X-Frame-Options`, CSP and `no-store`. The web app sends CSP, `X-Frame-Options` and HSTS, with no `X-Powered-By`. `/metrics` is 404 with no token configured. |
| **Real OpenAI**, streamed answers (end-to-end, `gpt-6-luna`) | **24/24** |
| **Real OpenAI**, whole answers (end-to-end, `gpt-6-luna`) | **10/10** |
| Real model evaluation (5 configurations × 10 questions) | All 10/10; see [model evaluation](phase-5.7-model-evaluation.md) |
| Search, storage failures, conversations (end-to-end) | 22/22, 23/23, 18/18 |
| Phase 3 documents (end-to-end) | 32/37: the same 5 failures as every phase since Phase 4 (the script runs without an OpenAI key). Not a regression. |
| Backups | Database backup verified by restore; storage backup verified; `nightly.sh` end to end; restore drill 3.1 s ([backup strategy](phase-5.7-backup-strategy.md)) |
| Load test | 10 checks; see [load testing](phase-5.7-load-testing.md) for which run is valid, and why |
| Real bucket after all runs | 2 objects, both referenced; 0 orphans; nothing deleted |
| Secret scan of the 38 changed and new files | None of the real secret values found |
| Dependency audits | `pip-audit`: none. `npm audit --omit=dev`: 0. Dev only: 8 moderate. |

**Not exercised:** none of this runbook's procedures has run on a real staging or production
platform. None exists yet.
