# Phase 5.6: deployment checklist

Work through this before each production deployment. Items marked **(required)** block
going live.

## Before the first deployment

### Secrets and configuration
- [ ] **(required)** Set the secrets in the platform's secret store, never in the repository
  or an image:
  - `OPENAI_API_KEY` (API only; never in the web app);
  - `SUPABASE_SECRET_KEY` (API only);
  - the database password, part of `DATABASE_URL`.
- [ ] **(required)** `ENVIRONMENT=production`. This turns off `/docs`, `/redoc` and
  `/openapi.json`; set `API_DOCS_ENABLED=true` only to publish them on purpose.
- [ ] **(required)** `CORS_ORIGINS` lists exactly the web app's origin(s), with `https://`
  and no wildcards.
- [ ] **(required)** `NEXT_PUBLIC_API_URL` is the public API URL. It is compiled into the web
  bundle, so build the web image with it.
- [ ] **(required)** `SUPABASE_URL`, plus `SUPABASE_JWKS_URL` / `SUPABASE_ISSUER` if you use a
  custom auth domain.
- [ ] **(required)** The bucket `SUPABASE_STORAGE_BUCKET` exists and is **private**. The API
  refuses a public bucket.
- [ ] **(required)** Review the usage limits for your plan (`RATE_LIMIT_*`, `*_QUOTA*`,
  `WORKSPACE_MONTHLY_*`, `USER_DAILY_*`, `MAX_*`). The defaults suit development and a small
  beta.
- [ ] **(required)** Pricing:
  - check the price table against https://developers.openai.com/api/docs/pricing;
  - if it changed, set `USAGE_PRICING_JSON` with a new `version`, `effective_from` and
    `verified_on`;
  - **`gpt-4.1-mini` is marked deprecated by OpenAI:** plan the move to its successor
    (`OPENAI_CHAT_MODEL`, plus its price).
- [ ] `MAX_REQUEST_BODY_BYTES` (1 MiB) suits your clients. Uploads are limited separately, to
  25 MB.

### Infrastructure
- [ ] **(required)** PostgreSQL 15 or later with `pgvector`, managed or with backups (see the
  runbook). Not reachable from the internet. In Compose, the database port binds to
  `127.0.0.1`; keep it that way, or remove the port mapping.
- [ ] **(required)** TLS terminates in front of the API and the web app.
- [ ] **(required)** The reverse proxy allows long-lived responses on
  `/api/v1/conversations/*/chat/stream`: a read timeout of at least 150 s, no response
  buffering. The API sends `X-Accel-Buffering: no` for nginx.
- [ ] **(required)** The reverse proxy limits request bodies to about 26 MB, the upload limit
  plus overhead. The API also enforces its own limits.
- [ ] The container stop timeout is at least 15 s. The API needs up to about 10 s to hand
  running ingestion jobs back. Compose's default is 10 s; set `stop_grace_period: 20s`.
- [ ] Health checks: liveness `GET /api/v1/health`, readiness `GET /api/v1/health/ready`
  (database and pgvector).
- [ ] Logs are shipped and kept. They contain IDs, counts and error types, never secrets or
  content. Keep them private anyway.

### Scaling
- [ ] Several API replicas are safe:
  - migrations take an advisory lock;
  - job claims and usage reservations are atomic across processes;
  - every replica runs ingestion workers (`PROCESSING_CONCURRENCY` each), with per-workspace
    limits that hold across replicas.
- [ ] One uvicorn process per container; scale with replicas.

## Every deployment

- [ ] Backend tests (`uv run pytest`), frontend tests (`npm test`), lint and type checks
  pass.
- [ ] `docker compose build` succeeds. `alembic check` shows no drift against the target
  database.
- [ ] **Back up the database before migrating** (see the runbook).
- [ ] Deploy. The API applies pending migrations on start; replicas wait their turn.
- [ ] After deploying:
  - `/api/v1/health/ready` returns 200;
  - one upload is indexed;
  - one question is answered with sources;
  - the Usage page loads.
- [ ] Watch the logs for `ERROR` and for abandoned or recovered counts for 30 minutes.

## Port note for this development machine

Another application listens on `127.0.0.1:8000`, while Docker serves this API on `[::1]:8000`.
Clients that resolve `localhost` to IPv4 reach the other application. Set `API_HOST_PORT`
(for example `8100`) and `NEXT_PUBLIC_API_URL` to match, or stop the other application.
