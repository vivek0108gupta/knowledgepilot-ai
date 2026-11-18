# Phase 5.8A: pre-deployment checklist

From the pre-deployment safety audit of 2026-11-13. Work through it top to bottom. Each step
gives:

- **Where:** the dashboard location;
- **Set:** the setting, with environment variable **names only** (never paste values into
  chat, git or issues);
- **Expect:** the expected result;
- **Verify:** a command or check;
- **Rollback:** how to undo it.

Placeholders: `<api>` is the Render service host (`*.onrender.com`), `<web>` the Vercel host
(`*.vercel.app`), `<ref>` the Supabase project ref.

---

## A. GitHub preparation

**A1. Remove the two stray test files** (not in git)
- **Where:** local repository root.
- **Set:** delete `conversations.log` and `jwks.json`.
- **Expect:** `git status` lists only `social/` as untracked.
- **Verify:** `git status --short`
- **Rollback:** none needed. They are test output (INFO logs, and one throwaway *public*
  key) with no secrets.

**A2. Choose the deploy branch.** `main` (`5690ef1`) is 9 commits behind
`feature/phase-5.7-launch-prep` (`2f853fe`), which contains every phase.
- **Where:** GitHub → the repository → Pull requests → New (base `main`, compare
  `feature/phase-5.7-launch-prep`).
- **Set:** merge with a **merge commit** or **rebase** (the history is linear; keep the 9
  commits). Then deploy from `main`.
- **Expect:** `main` = `2f853fe`.
- **Verify:** `git fetch && git rev-parse --short origin/main` prints `2f853fe`.
- **Rollback:** GitHub's **Revert** button on the merged pull request (creates a revert
  commit; nothing is deleted).
- *Alternative:* skip the merge and deploy from `feature/phase-5.7-launch-prep` directly.

---

## B. Supabase preparation

**B1. Read-only audit of the existing project** (do this first; it changes nothing)
- **Where:** Supabase dashboard → the project → **SQL Editor**.
- **Set:** run each query. Every one is a `SELECT`.
  ```sql
  -- 1. Tables already in public (the app needs these 15 names to be free)
  select tablename from pg_tables where schemaname = 'public' order by 1;
  -- 2. pgvector available, and its version (HNSW needs 0.5+)
  select name, default_version, installed_version from pg_available_extensions where name = 'vector';
  -- 3. The role the app will use, and its search path (must include the schema holding vector)
  select current_user, current_setting('search_path');
  -- 4. Data API roles exist (expected on Supabase: anon, authenticated)
  select rolname from pg_roles where rolname in ('anon', 'authenticated', 'service_role');
  ```
- **Expect:**
  1. none of `alembic_version, audit_events, conversations, document_chunks,
     document_segments, documents, ingestion_jobs, messages, rate_limit_buckets,
     usage_alerts, usage_events, workspace_member_quotas, workspace_members,
     workspace_quota_policies, workspaces`;
  2. a `default_version` of 0.5 or later;
  3. `postgres`, with a search path that includes `public` (and `extensions`);
  4. all three roles.
- **Verify:** compare with the expected values. **Any clash in query 1 is a blocker:**
  stop and report it. Don't drop anything.
- **Rollback:** not needed (read-only).

**B2. Enable pgvector** (if `installed_version` is empty)
- **Where:** Database → **Extensions** → `vector` → Enable (schema `extensions`).
- **Expect:** query 2 shows an `installed_version`. Migration 0001's
  `CREATE EXTENSION IF NOT EXISTS vector` then does nothing.
- **Rollback:** disable the extension (only if no app tables exist yet).

**B3. Get the session pooler connection** (IPv4, port 5432)
- **Where:** project → **Connect** → Connection string → **Session pooler**.
- **Set:** make `DATABASE_URL` in this form, and keep it only in your password manager and
  Render:
  `postgresql+asyncpg://postgres.<ref>:<password>@<pooler-host>:5432/postgres?ssl=require`
- **Don't use:** the direct connection (IPv6-only on Free), or the transaction pooler (port
  6543). The migration lock is session-level.
- **Expect:** the host ends in `pooler.supabase.com`, and the port is 5432.
- **Rollback:** reset the database password (Project Settings → Database) if the string
  leaks.

**B4. Migrations** (the first Render deploy runs them automatically)
- **What runs:** all 9 migrations, in **one transaction**.
  - PostgreSQL DDL is transactional, so any failure (for example a name clash) rolls back
    **everything**: no half-created schema.
  - Tables are created and locked down inside that same transaction, so they are never
    exposed through the Data API, even for a moment.
- **What 0009 does on Supabase**, for each of the 15 tables:
  1. `REVOKE ALL` from `anon` and `authenticated` (and on the sequences those tables own);
  2. `ENABLE ROW LEVEL SECURITY`, without `FORCE`.
- **Why the backend is unaffected:**
  - the migrations and the API connect as the same role (`postgres`, through the pooler),
    which **owns** the tables;
  - PostgreSQL doesn't apply RLS to a table's owner unless RLS is forced, and it isn't;
  - the API never switches role;
  - **no policies are needed:** no client is meant to read these tables through the Data
    API, and "RLS with no policy" means deny for everyone except the owner and roles that
    bypass RLS.
- **Tested** on a simulated Supabase database: all 15 tables locked down, an unrelated table
  untouched, the owner still working, and downgrade working.
- **Verify** after the first deploy (SQL Editor, read-only):
  ```sql
  select c.relname, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner) as owner,
         has_table_privilege('anon', c.oid, 'SELECT') as anon_can_read
  from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind = 'r' order by 1;
  select version_num from alembic_version;
  ```
  Expect all 15 tables with `relrowsecurity = true`, `relforcerowsecurity = false`, owner
  `postgres`, `anon_can_read = false`, and version `0009`.
- **Rollback:**
  - a failed migration rolls itself back;
  - after a successful one, with only demo data, `alembic downgrade base` (run locally,
    with `DATABASE_URL` set to the session pooler) drops the app's tables. **It deletes the
    demo data;** it touches nothing else;
  - Supabase Free has no automatic backups.

**B5. Auth URLs** (after D, once `<web>` is known)
- **Where:** Authentication → **URL Configuration**.
- **Set:**
  - Site URL `https://<web>`;
  - Redirect URLs: add `https://<web>/auth/callback`.
- **Expect:** the sign-up confirmation email links back to `https://<web>/auth/callback`.
- **Verify:** sign up with an address you control (test 3 in E).
- **Rollback:** restore the previous values (note them before changing).

**B6. Sign-up policy (your decision)**
- **Where:** Authentication → Providers / Sign In settings.
- **Background:** the built-in email sender allows 2 emails per hour on Free.
- **Options:**
  1. one demo account that you create and share;
  2. custom SMTP (Authentication → SMTP Settings);
  3. disable email confirmation. Not recommended: anyone can sign up with any address. The
     budget caps still limit the cost.

---

## C. Render deployment

**Fallback model:** off in the demo (`OPENAI_CHAT_FALLBACK_MODEL` is empty in
`render.yaml`), so every call is priced exactly by the caps. If the model is ever removed,
answers fail with `ai_misconfigured`. **Fix:** Render → Environment → set
`OPENAI_CHAT_MODEL` (for example `gpt-5.4-mini`) → save; it redeploys.

**C0. OpenAI demo key and hard limit** (before C1)
- **Where:**
  - platform.openai.com → **Settings → Projects** → new project "knowledgepilot-demo" →
    **API keys** → create key;
  - the project's **Limits → Spend → Edit spend limit**: set a monthly spend limit and turn
    on **"Enforce a hard limit"**.
- **Set:**
  - a hard limit above the app's monthly cap (the app is set to $3, so for example $5);
  - also check the organisation's limit under **Organization → Limits**.
- **Expect:** requests past the limit fail with `429 project_spend_limit_exceeded`.
  Enforcement isn't instantaneous *(OpenAI docs)*.
- **Rollback:** revoke the key (Project → API keys → Revoke). The API then answers
  `ai_misconfigured`; nothing is billed.

**C1. Create the Blueprint**
- **Where:** dashboard.render.com → **New → Blueprint** → connect the GitHub repo → choose
  the branch from A2.
- **Set:** Render reads `render.yaml`. It will prompt **once**, at creation, for the
  `sync: false` variables:
  - `DATABASE_URL` (B3);
  - `SUPABASE_URL`;
  - `SUPABASE_SECRET_KEY`;
  - `OPENAI_API_KEY` (C0);
  - `CORS_ORIGINS` (a placeholder `https://<web>` for now; final value in C3).
- **Generated:** `METRICS_TOKEN` (random 256-bit).
- **Expect:**
  - the service plan shows **Free**;
  - the build log shows the Docker build, then `Running upgrade 0008 -> 0009`, then
    `Uvicorn running on http://0.0.0.0:10000`;
  - the health check `/api/v1/health` passes.
- **Verify:**
  ```bash
  curl -s https://<api>/api/v1/health/ready
  ```
  Expect `{"status":"ok","database":"ok","pgvector":"…"}`. Allow about 1 minute if it was
  asleep.
- **Rollback:** Service → **Settings → Suspend** (stops it, keeps the configuration), or
  **Delete Service**. A bad deploy: **Events / Deploys** → redeploy the previous commit.
- **Note:** Render auto-deploys every push to that branch (the default trigger is
  `commit`).

**C2. Verify the production behaviour of the API**
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<api>/docs        # expect 404
curl -s -D - -o /dev/null https://<api>/api/v1/health | grep -iE "strict-transport|content-security"
curl -s -o /dev/null -w "%{http_code}\n" https://<api>/metrics     # expect 401 without the token
```

**C3. Final CORS value** (after D)
- **Where:** Render → the service → **Environment**.
- **Set:** `CORS_ORIGINS` = `https://<web>` exactly. No trailing slash: the browser's Origin
  header has none, and it must match exactly.
- **Expect:** the service redeploys.
- **Verify:**
  ```bash
  curl -s -D - -o /dev/null -X OPTIONS -H "Origin: https://<web>" -H "Access-Control-Request-Method: POST" https://<api>/api/v1/health | grep -i access-control-allow-origin
  ```
  Expect `https://<web>`. With `-H "Origin: https://example.com"`, expect no header.
- **Rollback:** restore the previous value.

---

## D. Vercel deployment

**D1. Create the project**
- **Where:** vercel.com → **Add New → Project** → import the repo → **Root Directory:
  `apps/web`** (framework detected: Next.js).
- **Set** (Environment Variables → Production; names from `deploy/vercel.env.example`):
  - `NEXT_PUBLIC_API_URL` = `https://<api>`;
  - `NEXT_PUBLIC_SUPABASE_URL`;
  - `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` (the **publishable/anon** key only, never the
    secret key).
- **Expect:**
  - the build passes. If the API URL is missing, not `https://`, or localhost, the build
    **fails on purpose** with a clear message;
  - the plan is Hobby.
- **Verify:** open `https://<web>`: the landing page shows "API online" after the API wakes.
- **Rollback:** Deployments → a previous deployment → **Promote to Production** / Instant
  Rollback. Or Project → Settings → Delete.
- **Note:** preview deployments use other URLs that CORS doesn't allow. That's expected;
  test on production only.

---

## E. End-to-end verification (synthetic documents only)

| # | Test | Expected |
|---|---|---|
| 1 | `https://<web>` loads | Landing page, "API online" |
| 2 | `curl https://<api>/api/v1/health/ready` | 200, `database: ok` |
| 3 | Sign up or sign in | The confirmation link returns to `https://<web>/auth/callback`; the dashboard loads |
| 4 | Create a workspace | Listed on the dashboard |
| 5 | Upload a synthetic PDF | 201, status goes Queued → Processing → **Ready** |
| 6 | Search a phrase from it | Passages with % match and page |
| 7 | Ask a question | The answer **streams**; a citation shows document and page |
| 8 | Ask again, press **Stop** mid-answer | Stops; Try again works |
| 9 | Usage page | The operations are listed, with costs |
| 10 | Rate limit: send 6 questions within a minute | The 6th is refused (`rate_limited`) |
| 11 | Isolation: open the workspace URL from a **second account** | 404 |
| 12 | Delete the document | Removed; the Supabase Storage bucket no longer has the file |
| 13 | Data API lockdown | `curl "https://<ref>.supabase.co/rest/v1/documents?select=id" -H "apikey: <publishable key>"` returns an error or empty (permission denied), **never rows** |
| 14 | Queue recovery | Render → **Restart service** right after an upload: the document still reaches Ready after the restart |
| 15 | Sleep | After 20 idle minutes, the next request takes about 1 minute, then works |
| 16 | Budget refusal (optional) | Temporarily set `GLOBAL_DAILY_COST_LIMIT` to `0.0001`: the next question gets `service_budget_exhausted`. Then restore the value. |

Record the results. Don't claim the deployment is verified until 1 to 13 pass.
