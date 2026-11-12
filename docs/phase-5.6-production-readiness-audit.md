# Phase 5.6: production readiness audit

Audit of `feature/phase-5.5-usage-reliability` at `d8eacb0` (clean working tree). Work
continues on `feature/phase-5.6-production-readiness`. Findings are **confirmed** (read in the
code or reproduced) unless marked *assumption*. Severity: Critical (data loss, cross-tenant
exposure or secret leak, now), High (likely outage, abuse or silent inconsistency in
production), Medium (a real but bounded risk, or an operational gap), Low (hygiene or known
limitation).

This document was written before any code change. The "Fix" column says what this phase
changes; everything else is documented for later.

## Scope inspected

- Docker Compose and both Dockerfiles.
- API startup and shutdown (`app/main.py` lifespan).
- Database engine and sessions (`app/core/db.py`).
- Alembic history `0001`–`0008` and `env.py`.
- The PostgreSQL job queue and worker (`app/services/jobs.py`, `processing.py`).
- Supabase Storage (`storage.py`) and the document and workspace lifecycles.
- Usage accounting, pricing, rate limits, quotas, alerts and the audit log.
- Configuration (`config.py`, `.env.example`).
- Logging, middleware and error handlers.
- The frontend usage dashboard.

## What is already in good shape (confirmed)

- **Authentication:** every route except health takes the user from a verified Supabase JWT
  (issuer, audience, expiry, JWKS).
- **Tenant isolation:** workspace data is loaded only through the caller's membership (404
  otherwise). Retrieval filters on workspace and membership in one SQL statement. Each job
  carries only the IDs from its document row.
- **Concurrency:**
  - one answer per conversation (lease);
  - reservations under advisory locks;
  - atomic rate-limit upserts;
  - job claims with `SKIP LOCKED` and a per-workspace lock;
  - exactly-once finalisation.

  Each of these is covered by concurrency tests and mutation checks.
- **Secrets:**
  - kept in `SecretStr` and the gitignored `.env`;
  - the OpenAI key exists only in the API container (verified in Phase 5.3);
  - SQL parameters are hidden from logged errors (`hide_parameters=True`);
  - `httpx`, `openai` and `httpcore` log only warnings, so signed URLs are not logged;
  - signed download URLs live 60 s and are issued after an access check.
- **Containers:** both run as non-root (`api` uid 1000, `node`). The web image is a standalone
  build without dev dependencies, and the API image has no dev dependencies.
- **Errors:** one envelope with a request ID. Unhandled errors return a generic 500, with the
  traceback in the logs only.
- **Uploads:** a 25 MB limit enforced on `Content-Length` and while streaming, type
  validation, and storage paths checked against the document's own folder.

## Findings

| ID | Severity | Finding | Evidence | Fix in this phase |
|---|---|---|---|---|
| H1 | **High** | **Unbounded JSON request bodies.** Only the upload route limits its body. Every JSON route (`/chat`, `/chat/stream`, `/search`, quotas…) lets the server buffer whatever the client sends, before authentication or validation. An unauthenticated client can exhaust the API's memory. | Reproduced against the running container: a POST to `/conversations/{id}/chat` declaring 200 MB, with no token, got no response after 5 MB had been sent; the server was still reading. | Yes: ASGI middleware refuses bodies over `MAX_REQUEST_BODY_BYTES` (1 MiB) with `413`, both on `Content-Length` and while streaming. Uploads keep their own limit. |
| H2 | **High** | **Orphaned storage objects cannot be detected or cleaned.** Objects are left behind when: a workspace is deleted and storage then refuses the file deletes (logged, then left); an upload is stored, its commit fails, and the clean-up delete also fails; or the process dies between storage upload and commit. Nothing lists the bucket or compares it with the database. | `documents.delete_stored_files` and `workspaces` delete paths log and continue. `Storage` has no list operation. | Yes: a list operation for storage, and `python -m app.tools.storage_audit` (dry run by default; `--delete` with a minimum age and an optional prefix) that reports orphaned objects and documents whose file is missing. |
| H3 | **High** | **Worker shutdown is not graceful.** On shutdown, `JobQueue.stop()` cancels only the polling loop. Jobs in flight die with the process. Their documents stay `processing` and their jobs `running` until the lease expires (10 minutes), and each loses an attempt. | `jobs.py` `stop()`. | Yes: stop claiming, wait up to `INGESTION_SHUTDOWN_GRACE_SECONDS` for running jobs, then cancel the rest and hand them back at once without charging an attempt. Their documents return to `uploaded`. |
| H4 | **High** | **Concurrent migrations.** The API container runs `alembic upgrade head` on every start. With several replicas starting together, two can run the same migration at once. Alembic takes no lock. | `Dockerfile` CMD; `alembic/env.py`. | Yes: `env.py` takes a PostgreSQL advisory lock for the duration of the migration, so replicas run it one at a time and the later ones find nothing to do. |
| M1 | Medium | **The chat model is marked deprecated by OpenAI.** The prices in the table match OpenAI's official pricing page (checked 2026-11-12): gpt-4.1-mini $0.40 / $1.60 per 1M input / output tokens; text-embedding-3-small $0.02 per 1M. But gpt-4.1-mini's model page now marks it **Deprecated**. | [Pricing](https://developers.openai.com/api/docs/pricing), [gpt-4.1-mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini), [text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small) | Partly: the price table records when its prices took effect and when they were last verified, and both are returned with usage. The model change itself is a configuration and product decision (`OPENAI_CHAT_MODEL`), left to the owner. |
| M2 | Medium | **Cached-input pricing is not modelled** ($0.10 / 1M for gpt-4.1-mini). Cached prompts are costed at the full input price, so estimates can be **high**, never low. | Pricing page; `pricing.py` has one input price per model. | No. Documented. |
| M3 | Medium | **Finalisation can mix price tables.** `finalize` costs an operation with the price table current at finalisation, but leaves the `pricing_version` set at reservation. If prices change while an operation runs, or before a late result, the row claims one version but uses another. | `usage.finalize`. | Yes: finalisation records the version and currency it actually used. |
| M4 | Medium | **Held usage is mislabelled in the dashboard history.** `usage_source = unavailable` is labelled "Unknown (reserved amount)", although Phase 5.5 holds an upper bound, not the reservation. The headline meters include held tokens without saying so. | `usage-dashboard.tsx`. | Yes: labelled "Held (outcome unknown)"; the meters say they include held usage. |
| M5 | Medium | **API documentation is public in production.** `/docs`, `/redoc` and `/openapi.json` are served whatever `ENVIRONMENT` is set to. | `main.py`. | Yes: off when `ENVIRONMENT=production`, unless `API_DOCS_ENABLED=true`. |
| M6 | Medium | **The database port is published on every interface** with the default password (`5434` on `0.0.0.0`). Fine on a laptop, an exposure on any server that uses this Compose file. | `docker-compose.yml`. | Yes: bound to `127.0.0.1` by default (`POSTGRES_BIND_ADDRESS` to change it). |
| M7 | Medium | **Waiting documents without a job are only noticed at startup.** Phase 5.5 creates jobs with their documents, but a document left `uploaded` with no active job (for example from before 5.5) is picked up only when an API process starts. | `main.py` → `recover_interrupted`. | Yes: the periodic maintenance also enqueues them. Enqueueing is idempotent. |
| M8 | Medium | **No documented backup and restore.** The database holds every tenant's data, usage history and jobs. No procedure has been written or tested. | Repository. | Yes, as documentation: a procedure, rehearsed with a dump of the local database restored into a scratch database (no user data changed). |
| M9 | Medium | **The API schema could not be generated** (found while testing H5/M5). `/openapi.json` (and so `/docs`) returned 500 since Phase 5.3: FastAPI cannot describe the streaming route's custom response class without an explicit status code. | Reproduced against the running container (500), and route by route. | Yes: the route declares `status_code=200` and `response_model=None`; the schema now generates (29 paths) and a test covers it. |
| M10 | Medium | **Shutdown could wait indefinitely** (found while testing H3). The first version of the graceful shutdown waited for cancelled jobs and for their hand-back without a time limit; a job stuck on an unreachable database could keep the process from exiting. | Seen in the resilience test (whose own database proxy was blocked at the time). | Yes: bounded waits (5 s to stop, 5 s to hand back, 3 s lock timeout). A job that does not stop is **not** handed back (its task might still write); its lease expires and it is recovered. |
| M11 | Medium | **An empty optional setting stopped the API from starting** (found during the Docker rebuild, in this phase's own change). Compose passes an unset variable as `""`, which the new `API_DOCS_ENABLED` setting rejected as a boolean. | The API container exited at startup. | Yes: an empty value means "use the default"; a test covers it. |
| L1 | Low | Another process on this machine listens on `127.0.0.1:8000`; Docker serves the API on IPv6 `:8000`. Clients that resolve `localhost` to IPv4 first reach the wrong service. Environment-specific, not a code defect. | `lsof`. | No; set `API_HOST_PORT` (see the deployment checklist). |
| L2 | Low | Alerts are recorded but not delivered (no e-mail or webhook integration exists). | Phase 5.5 decision. | No (out of scope by request). |
| L3 | Low | Workers run inside API processes; there is no separate worker deployment. | `main.py`. | No; documented. |
| L4 | Low | Token estimates use `cl100k_base`; the held bound assumes maximum output. | `usage.py`. | No; documented. |
| L5 | Low | Rate-limit buckets of idle subjects keep up to two rows per policy until reused. | `rate_limits.py`. | No; an optional clean-up query is documented. |
| L6 | Low | Each container runs one uvicorn worker process; scaling is by replicas. | `Dockerfile`. | No; documented. |

No Critical findings: no cross-tenant path, secret exposure or data-loss path was found.

## Verification plan

**Concurrency and worker durability** (resilience script, with several real API processes, a
real database, real Supabase Storage and a local OpenAI stand-in):

- concurrent uploads and streams;
- two or more worker processes;
- `kill -9` during ingestion, then a restart;
- a graceful restart;
- the database becoming unreachable (through a TCP proxy that can be cut);
- storage becoming unreachable (through a proxy that can be cut);
- OpenAI timeouts;
- user cancellation and client disconnect;
- quota exhaustion;
- recovery after restart.

**Document lifecycle** (automated tests):

- upload and commit succeed;
- upload succeeds but the commit fails;
- the upload fails;
- job creation fails;
- the document is deleted during processing;
- the workspace is deleted during processing;
- storage clean-up fails;
- a retry after partial processing;
- the orphan audit itself.

**Usage and alerts** (automated tests):

- provider-reported, estimated and held usage;
- cancelled operations and unknown outcomes;
- late results;
- duplicate idempotency keys;
- pricing versions, and that historical cost is unchanged when prices change;
- alert idempotency under repeated requests and polling;
- daily and monthly resets;
- member overrides and the global daily limit;
- unauthorised changes, and audit events.

**Regression:** the full backend and frontend suites, the earlier end-to-end scripts, the real
OpenAI test, a Docker rebuild and the migration checks.


## Verdict: **Ready for private beta**

### Evidence for

- **No Critical findings.** No cross-tenant path, secret exposure or data-loss path was
  found.
- **All four High findings are fixed and verified against the running system:**
  - an unauthenticated 200 MB body is refused at once with 413;
  - orphaned storage is detectable, and removable with safeguards;
  - graceful shutdown hands jobs back in about 1 s;
  - concurrent migrations take turns, tested with 3 at once.
- **Resilience: 27 / 27** across two real processes, covering:
  - a hard crash and recovery;
  - a graceful restart;
  - database and storage outages;
  - OpenAI timeouts;
  - cancellation and disconnects;
  - quota exhaustion;
  - concurrent uploads and streams.

  No job was lost, no chunk duplicated, and every reservation was eventually closed.
- **Tests:**
  - backend 1,011 / 1,011 and frontend 393 / 393;
  - real OpenAI 24 / 24 and 10 / 10;
  - every earlier end-to-end script at its previous result.
- **Backups:** a backup and restore was rehearsed, with identical row counts.

### Why not a controlled public launch yet

1. **The chat model, gpt-4.1-mini, is marked deprecated by OpenAI.** A move to its successor
   must be chosen, re-tested (prompts, citation quality) and priced.
2. **No alert delivery or operational monitoring.** Alerts are recorded but not sent. There
   are no metrics, error tracking or uptime alarms; an operator only learns of trouble from
   logs.
3. **Backups are a rehearsed procedure, not a scheduled, monitored job;** Storage backups
   are not configured either. The restore time objective has not been measured at realistic
   data size.
4. **Not yet done:**
   - load testing beyond a handful of concurrent users;
   - an external security review or penetration test.
5. **Cost estimates** ignore cached-input pricing (they err high) and are not reconciled
   against the provider's invoice.

Each of these is a deployment or operations task rather than a code defect in what was
audited. Closing items 1 to 3 would justify a controlled public launch.
