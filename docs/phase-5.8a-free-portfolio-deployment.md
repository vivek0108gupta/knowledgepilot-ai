# Phase 5.8A: free portfolio deployment (₹0/month hosting)

A public demo of KnowledgePilot AI for a portfolio, GitHub and interviews, on free tiers
only:

| Part | Free provider |
|---|---|
| Web app | Vercel Hobby |
| API (Docker) | Render Free web service |
| Database, Auth, Storage | the existing Supabase Free project |

**This is not the production architecture.** Production, with several replicas, managed
backups and monitoring, is described in the
[Phase 5.7 deployment runbook](phase-5.7-deployment-runbook.md).

**Hosting costs nothing. OpenAI usage is not free:** every answer and embedding is billed
to your OpenAI account. This phase adds hard caps (below), but set a budget on the OpenAI
project as well.

Status as of 2026-11-13:

- The configuration is ready and verified locally.
- **Nothing has been deployed yet.** Creating the Render and Vercel projects, and running the
  migrations on Supabase, needs your accounts and approval (see "Manual steps").

---

## A. Compatibility report (audited from the source)

| Area | What the code does | Free-tier fit |
|---|---|---|
| Web build | Next.js 16 App Router, `output: "standalone"`, `proxy.ts`, only 3 env vars (`NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`) | ✅ Vercel builds Next.js natively. No static export needed (and it would break `proxy.ts` and the auth callback). |
| Hardcoded URLs | Only a localhost fallback in `lib/api.ts` | ⚠️ **Fixed:** a Vercel build now fails if the API URL is missing or is localhost |
| Sign-up redirect | `emailRedirectTo: ${window.location.origin}/auth/callback` | ✅ Follows the deployed domain. The Supabase redirect allow-list needs the Vercel URL (manual step). |
| API container | `python:3.13-slim`, non-root, migrations then `exec uvicorn`, `--host 0.0.0.0` | ⚠️ **Fixed:** the port was fixed at 8000; it is now `${PORT:-8000}` (Render sets `PORT`) |
| Health | `/api/v1/health` (liveness), `/api/v1/health/ready` (database and pgvector) | ✅ Render uses liveness, so a paused database does not cause a restart loop |
| JWT | ES256/RS256 only, issuer and audience required, Supabase JWKS | ✅ No change; the API holds no Supabase JWT secret |
| CORS | Explicit `CORS_ORIGINS` list | ✅ Set to the Vercel production URL. Preview URLs are not allowed (by design). |
| SSE | `text/event-stream`, `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`; Stop and disconnect recorded separately | ⚠️ **Added:** keep-alive comments after 15 s of silence (the client already ignores comments) |
| Worker | **Not a separate process:** each API process runs the durable ingestion workers in its lifespan (`PROCESSING_CONCURRENCY`) | ✅ Exactly what a single free web service needs (see D) |
| Queue | PostgreSQL `ingestion_jobs`, `SKIP LOCKED` claims, leases, bounded retries, hand-back at shutdown | ✅ Unchanged |
| Migrations | Alembic in the container entrypoint, under a **session-level** advisory lock | ⚠️ Needs a direct or **session-pooler** connection (see E); the transaction pooler (port 6543) would break it |
| Database URL | `DATABASE_URL` (asyncpg); no SSL option set | ✅ `?ssl=require` works (verified: it reaches the driver) |
| **Supabase Data API** | App tables live in `public`. Supabase grants its `anon` role access to new tables there, and the anon key is public. | ❌ **Critical, fixed:** migration 0009 (see E) |
| **Spending** | Per-user daily and per-workspace monthly limits only | ❌ **Fixed:** anyone could sign up repeatedly, so a deployment-wide cap was added (see F) |

Note: the Phase 5.8A brief described "a separate ingestion worker" and "Supabase PostgreSQL"
as the existing setup. In the code, the workers run inside the API process, and development
uses a local PostgreSQL in Docker; only Auth and Storage are on Supabase. The plan below
follows the code.

## B. Web app on Vercel Hobby

- **Project root:** `apps/web`. Framework: Next.js (detected). Build: `next build`.
- **Environment variables** (Production): see `deploy/vercel.env.example`.
  - `NEXT_PUBLIC_API_URL`: the Render URL, `https://<service>.onrender.com`.
  - `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`: the Supabase
    **publishable (anon)** key only. **Never** the secret or service-role key.
- **Guard:** with `VERCEL=1`, the build fails unless the API URL is `https://` and not
  localhost, and the Supabase values are set. Tested: a localhost URL is refused, an
  `https://` URL is accepted, and local or Docker builds are unaffected.
- **Security headers:** CSP `frame-ancestors`, `X-Frame-Options`, HSTS and others, from
  Phase 5.7's `next.config.ts`.

## C. API on Render Free

`render.yaml` (a Blueprint) defines:

- one **free** Docker web service;
- the health check `/api/v1/health`;
- secrets as `sync: false` (entered in the Render dashboard, never in git);
- a generated `METRICS_TOKEN`;
- free-tier sizing: 1 ingestion worker, a database pool of 3 + 2;
- demo spending limits.

- **Start:** the Dockerfile's `alembic upgrade head && exec uvicorn … --host 0.0.0.0 --port
  ${PORT:-8000}`. Verified in a container with `PORT=10000`.
- **Production behaviour** (verified in that container):
  - `/docs` and `/openapi.json` return 404;
  - HSTS and CSP are present;
  - CORS allows only the configured origin;
  - SIGTERM exits cleanly in about 1 s.
- **SSE through Render's proxy:** no buffering headers, and keep-alive comments every 15 s of
  silence.
  - Render documents no maximum response duration (checked 2026-11-13).
  - The longest normal silence is retrieval plus the provider's first token (about 1 to
    3 s). Keep-alives cover provider retries.
  - The stream deadline is `CHAT_STREAM_TIMEOUT_SECONDS` (120 s).
  - Verified with the real-OpenAI streaming test (24/24) after the change. A disconnect still
    releases the conversation and records `client_disconnected` (tested).

## D. Worker in the same free service

The architecture **already** runs the workers inside the API process, so nothing was
weakened to fit the free tier:

| Requirement | How it holds |
|---|---|
| Durable queue | Jobs live in PostgreSQL, not in memory |
| Atomic claims and lease recovery | `SELECT … FOR UPDATE SKIP LOCKED`; expired leases are taken back by maintenance |
| Idempotency | Each attempt has its own usage key; chunks are replaced per attempt; tested for no duplicates under crashes (Phase 5.6, and the Phase 5.7 load test) |
| Graceful termination | SIGTERM: running jobs get 5 s, then are handed back without losing an attempt; the process exits within about 10 s (in the container test, about 1 s). Render's shutdown delay was not confirmed in its docs; if Render kills it sooner, the jobs are recovered through their leases. |
| Health not blocked | The worker loop is a background task started after the app; `/health` never touches it |
| Worker errors | The loop catches and logs every exception and continues; a job crash is recovered through its lease |

**Free-tier behaviour to know:**

- Render sleeps the service after **15 minutes without inbound requests**. While it is
  asleep, no jobs run.
- Jobs are processed while the service is awake. An upload wakes it, and the page polls the
  document's status, which keeps it awake until indexing finishes.
- A job interrupted by a restart is resumed after its lease (10 minutes) on the next wake.
  Nothing is lost.

## E. Supabase

**Use the session pooler.** The direct connection is IPv6-only on the Free plan. The shared
pooler supports IPv4 in **session mode** (port 5432), which supports the prepared statements
and session-level advisory lock the API uses. Do **not** use transaction mode (port 6543).
*(Supabase docs, checked 2026-11-13.)*

Copy the **Session pooler** string from the project's **Connect** panel. Then change its
scheme to `postgresql+asyncpg://` and add `?ssl=require`. It looks like:

```
DATABASE_URL=postgresql+asyncpg://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres?ssl=require
```

Put it only in Render's environment settings (and your local `.env` for the one-off
migration), never in git or chat.

**Migration 0009, the Data API lockdown (critical).** Supabase publishes the `public`
schema through its Data API, with privileges for the `anon` role, and the anon key is
public. Where those roles exist, the migration:

1. revokes `anon` and `authenticated` privileges on the 15 application tables (and the
   sequences they own);
2. enables RLS on them, with no policies.

The API connects as the tables' owner, which RLS does not restrict, so the app is
unaffected. Nothing else in the project is touched. It does nothing on plain PostgreSQL.

**Verified on a simulated Supabase database** (local PostgreSQL with `anon` and
`authenticated` roles and Supabase-style default grants):

| Check | Result |
|---|---|
| Before 0009 | `anon` **could** read `documents` |
| After 0009 | **no** `anon`/`authenticated` privilege on any app table; RLS on all 15 |
| An unrelated table | left alone |
| The app's role | still works |
| Downgrade | works |

A test now fails if a future table is not covered.

**Before running anything against the real project** (needs your approval and access):

1. Check that `public` has no tables with these names: the app would collide with them.
2. Confirm `vector` can be enabled (it can on the Free plan).
3. Run `alembic upgrade head` once, from your machine, with `DATABASE_URL` set to the
   session pooler (or let the first Render deploy run it).
4. Afterwards, check in the dashboard that the app's tables show **RLS enabled**, and that
   the Data API cannot read them.

Existing documents in the **local** database are not moved: the demo starts empty. Upload
**synthetic demo documents only**; do not upload real or personal ones.

**Auth settings** (dashboard, Authentication → URL Configuration):

- Site URL: the Vercel URL.
- Redirect URLs: add `https://<your-app>.vercel.app/auth/callback`.
- The built-in email service sends only **2 emails per hour** on Free. Either create one demo
  account yourself and share it, or set up custom SMTP (allowed on Free). Disabling email
  confirmation is possible but lets anyone sign up with any address. The budget caps limit
  the cost either way.

**Storage:** the private bucket is unchanged; the API refuses a public one. Signed URLs
last 60 s.

## F. Free-tier protection

**Hosting:** every service uses the free plan. No paid plans, domains, disks, workers,
cron jobs or load balancers. The free subdomains are `*.vercel.app` and `*.onrender.com`.

**OpenAI** (this is what can cost money):

| Layer | Setting (demo value) | What it stops |
|---|---|---|
| OpenAI project budget | Set a hard monthly budget in the OpenAI dashboard (for example $5) | The final backstop; applied by OpenAI |
| **Deployment-wide cap (new)** | `GLOBAL_DAILY_COST_LIMIT=0.50`, `GLOBAL_MONTHLY_COST_LIMIT=3.00` | All users and workspaces together. New sign-ups don't get new budget. Refused with `429 service_budget_exhausted`. |
| Per workspace | `WORKSPACE_MONTHLY_COST_LIMIT=1.00`, `WORKSPACE_MONTHLY_TOKEN_LIMIT=500000` | One workspace using everything |
| Per user | `USER_DAILY_TOKEN_LIMIT=100000`, `USER_DAILY_CHAT_LIMIT=30` | One person using everything |
| Rate limits | 5 answers/min, 20 searches/min, 5 uploads/hour per user | Bursts |
| Concurrency | 3 answers per user; 3 ingestion jobs per workspace | Parallel abuse |

How the cap works:

- It is enforced **before** each provider call, under a database lock, counting in-flight
  operations at their reserved upper bound, so concurrent requests cannot overshoot.
- A model missing from the price table is refused while a cap is set.
- **The fallback model is off in the demo** (`OPENAI_CHAT_FALLBACK_MODEL` is empty in
  `render.yaml`). Caps price each call at the configured model before it runs, and a
  fallback to `gpt-5.4-mini` (about 7 to 9 times the price) would be charged only
  afterwards, so it could overshoot them. If `gpt-6-luna` were ever removed, answers would
  fail with `ai_misconfigured` until `OPENAI_CHAT_MODEL` is changed in Render.
- Tested: a daily and a monthly cap stop the third operation; spending in one workspace
  counts against another; a real duplicate is still a 409.

**Quotas and sleeping behaviour** (official pages, checked 2026-11-13):

| Provider | Free limits that matter |
|---|---|
| Render | 750 instance hours per month per workspace (services are suspended when used up); sleeps after 15 min without requests, about 1 min to wake; 512 MB RAM, 0.1 CPU; ephemeral filesystem; no shell, persistent disks, background workers or cron; may be restarted at any time |
| Vercel Hobby | 100 GB fast data transfer, functions up to 300 s, 100 deployments per day; the plan is paused if exceeded. Hobby is for personal projects. The Hobby page itself does not state the non-commercial terms, which are in Vercel's terms; a portfolio is fine. |
| Supabase Free | 500 MB database, 1 GB file storage, 5 GB egress, 50,000 MAU, 2 active projects, 50 MB max upload (the app caps uploads at 25 MB); **paused after 1 week of inactivity**; 2 auth emails per hour with the built-in sender |

**Risks the caps don't cover:** storage abuse. Uploads count against the 1 GB, limited
only by 5 uploads per hour per user. For a public demo, consider making sign-up
invite-only, or sharing one demo account.

## G. End-to-end QA

**Done locally** (2026-11-13; Docker and real providers where stated):

| # | Check | Result | How |
|---|---|---|---|
| 1 | Production container serves | ✅ | `PORT=10000`, `ENVIRONMENT=production`; readiness 200 |
| 2 | Docs hidden, HSTS, CSP, CORS | ✅ | Same container |
| 3 | Upload, ingestion, worker, retrieval, RAG answer, citations, SSE | ✅ | Real OpenAI + Supabase streaming end-to-end: 24/24 |
| 4 | Stop and disconnect | ✅ | `tests/test_chat_stream.py` (disconnect at 3 points, Stop) |
| 5 | Usage accounting, rate limits, quotas | ✅ | Backend suite; the Phase 5.7 load test |
| 6 | Tenant isolation | ✅ | Backend isolation tests |
| 7 | Document deletion | ✅ | Backend tests; the storage audit is clean (2 objects, both referenced) |
| 8 | Restart and queue recovery | ✅ | Phase 5.6 resilience (27/27) and the Phase 5.7 load test (`kill -9`: 24/24 indexed once) |
| 9 | Supabase Data API lockdown | ✅ | Simulated Supabase roles (above) |
| 10 | Budget caps | ✅ | `tests/test_free_deployment.py` |

**Not done (needs the live deployment):** the same 16 checks against the real Render,
Vercel and Supabase URLs. Run the checklist in "After deploying" below once the services
exist. Nothing here claims a live deployment that has not happened.

## Manual steps (in order)

1. **Remove the two stray test files** at the repository root, `conversations.log` and
   `jwks.json` (left by an end-to-end test run). They contain no secrets, but should not be
   committed.
2. **Commit and push** the Phase 5.6, 5.7 and 5.8A work to GitHub. Neither the 5.6 nor the 5.7
   branch is on GitHub yet, and `main` is 8 commits behind. Render and Vercel deploy from a
   GitHub branch.
3. **OpenAI:** create a separate API key (or project) for the demo, with a hard monthly
   budget.
4. **Supabase:** run the checks in E. Then set the Auth URLs once the Vercel URL is known.
5. **Render:** New → Blueprint → choose the repo and branch. Enter the `sync: false` values:
   `DATABASE_URL` (session pooler), `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `OPENAI_API_KEY`,
   and `CORS_ORIGINS` (the Vercel URL; you can update it after step 6). Make sure the plan
   says **Free**.
6. **Vercel:** New Project → the repo → Root Directory `apps/web` → set the three
   `NEXT_PUBLIC_*` variables → Deploy. On Hobby.
7. Set `CORS_ORIGINS` on Render to the final Vercel URL. Redeploy.
8. **Run "After deploying".**

### After deploying (record the results)

- [ ] `https://<api>.onrender.com/api/v1/health/ready` → 200 (wait about 1 min if it was asleep)
- [ ] `/docs` → 404; response headers include HSTS
- [ ] The Vercel site loads; sign up (or sign in to the demo account); the email link
      returns to the Vercel `/auth/callback`
- [ ] Create a workspace; upload a **synthetic** PDF; it reaches Ready
- [ ] Search finds it; ask a question; the answer streams and cites a page; Stop works
- [ ] The Usage page shows the operations
- [ ] A second account cannot open the first account's workspace URL (404)
- [ ] Delete the document; the storage audit shows no orphan
- [ ] Wait 20 minutes (the service sleeps); upload again; it is processed after the wake
- [ ] Supabase dashboard: app tables show RLS enabled

## Free-tier limitations (for the README)

- The first request after 15 idle minutes takes about a minute while the API wakes.
- Documents are processed only while the API is awake.
- The Supabase project pauses after a week without activity; restore it from the dashboard.
- Sign-up emails are limited to 2 per hour, unless custom SMTP is set up.
- One instance: no high availability, and Render may restart it at any time. Jobs survive
  restarts.
- AI usage is capped per day and per month; when the cap is reached, the demo answers
  `service_budget_exhausted` until the next period.
- Backups: the scheduled backup in the Phase 5.7 strategy is not running (no free scheduler).
  Supabase Free has no automatic backups either. Don't keep anything valuable in the demo.
