# KnowledgePilot AI

Upload documents, ask questions, and get answers grounded in those documents with citations back to the source page.

Built so far: sign-up and sign-in with Supabase Auth, and workspaces with owner, admin and member roles. Document uploads and RAG come in later phases.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) with Compose, for the full stack.
- To run the apps outside Docker: Node.js 22 or newer, and [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 for you).
- A [Supabase](https://supabase.com) project, for sign-in. See [Authentication setup](#authentication-setup).

## Layout

```
apps/
  web/                 Next.js App Router, TypeScript (strict), Tailwind CSS
    app/               routes: /login, /register, /dashboard/..., /auth/callback
    components/        auth forms, dashboard shell, workspace screens, UI primitives
    lib/api.ts         typed client for the API; sends the access token, validates with Zod
    lib/supabase/      Supabase clients for the browser and for proxy.ts
    proxy.ts           redirects signed-out visitors away from /dashboard (Next.js 16's middleware)
  api/                 FastAPI, SQLAlchemy (async), Alembic, Pytest
    app/api/v1/        HTTP routes, one module per resource
    app/api/deps.py    `CurrentUser`: the reusable dependency that verifies the access token
    app/core/          config, database, logging, errors, request middleware, JWT verification
    app/models/        SQLAlchemy models
    app/schemas/       Pydantic request and response models
    app/services/      business rules: membership, roles, and every authorization check
    alembic/           database migrations
    tests/
docker-compose.yml     database, API, and web app together
.env.example           every environment variable, with placeholder values
```

## Run everything with Docker

```sh
docker compose up --build
```

- Web: http://localhost:3000
- API docs: http://localhost:8000/docs
- Liveness: http://localhost:8000/api/v1/health
- Readiness (database and pgvector): http://localhost:8000/api/v1/health/ready

The API container applies database migrations each time it starts, and the web container waits until the API reports ready.

## Authentication setup

Sign-in uses Supabase Auth. The web app signs users in; the API verifies each request's access token against the project's public signing keys (JWKS), so the API holds no Supabase secret.

1. In the Supabase dashboard, open **Project Settings > API Keys** and copy the project URL and the **publishable** key.
2. Copy `.env.example` to `.env` and set:

   ```sh
   NEXT_PUBLIC_SUPABASE_URL=https://<project-ref>.supabase.co
   NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
   SUPABASE_URL=https://<project-ref>.supabase.co
   ```

3. In **Authentication > URL Configuration**, set the Site URL to where the web app runs (for example `http://localhost:3000`) and add `<that URL>/auth/callback` to the redirect URLs. Confirmation emails send users there.
4. Rebuild: `docker compose up --build`. The two `NEXT_PUBLIC_*` values are compiled into the web bundle, so changing them needs a rebuild.

The project must use asymmetric JWT signing keys (the default for new projects). Check that `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json` lists at least one key. The API accepts ES256 and RS256 tokens only.

Never put the Supabase **secret** or **service-role** key in this project's environment. Nothing here needs it, and a `NEXT_PUBLIC_*` variable would publish it to every visitor.

## Workspaces and roles

Every workspace endpoint requires a valid access token. The API takes the caller's identity from the token alone, never from a user ID in the request.

| Action | Owner | Admin | Member |
|---|---|---|---|
| View the workspace and its members | Yes | Yes | Yes |
| Rename the workspace | Yes | Yes | No |
| Add a member | Any role | As member only | No |
| Remove a member | Anyone | Members only | No |
| Leave the workspace | Yes, unless last owner | Yes | Yes |
| Delete the workspace | Yes | No | No |

A workspace always keeps at least one owner. Someone who is not a member gets `404` for that workspace, the same as if it did not exist. Members are added by user ID for now (each user's ID is on their Settings page); email invitations come later.

Endpoints, all under `/api/v1`: `GET /me`, `GET|POST /workspaces`, `GET|PATCH|DELETE /workspaces/{id}`, `GET|POST /workspaces/{id}/members`, `DELETE /workspaces/{id}/members/{user_id}`.

User IDs are stored as plain UUID columns, not foreign keys to Supabase's `auth.users`, because the application database is a separate Postgres.

## Configuration

Everything is configured through environment variables, and every one has a local default, so no `.env` is needed to start. To change something, copy `.env.example` to `.env` and edit it; Compose reads it automatically. `.env` is gitignored: never commit real values.

Common changes:

- **A port is already taken.** Set `WEB_HOST_PORT`, `API_HOST_PORT`, or `POSTGRES_HOST_PORT`. If you move the web port, set `CORS_ORIGINS` to match (for example `http://localhost:3100`), or the browser will be blocked from calling the API. If you move the API port, set `NEXT_PUBLIC_API_URL` to match and rebuild the web image.
- **Postgres is on host port 5434**, not 5432, so it does not collide with a Postgres you already run.
- `NEXT_PUBLIC_API_URL` is compiled into the browser bundle at build time. It is public by design; never put a secret in a `NEXT_PUBLIC_*` variable.

## Run the apps outside Docker

Start only the database, then each app in its own terminal:

```sh
docker compose up db
```

```sh
cd apps/api && uv sync && uv run alembic upgrade head && uv run uvicorn app.main:app --reload
```

```sh
cd apps/web && npm install && npm run dev
```

Outside Docker each app reads its own file: put the `NEXT_PUBLIC_*` values in `apps/web/.env.local` and `SUPABASE_URL` in `apps/api/.env`. Both are gitignored.

The API reads `apps/api/.env` if present; without one it connects to the Compose database on `localhost:5434`.

## Database migrations

Run these from `apps/api`. The schema changes only through Alembic migrations.

```sh
uv run alembic upgrade head
```

```sh
uv run alembic revision --autogenerate -m "describe the change"
```

```sh
uv run alembic downgrade -1
```

For autogenerate to see a new model, import its module in `app/models/__init__.py`.

## Errors and logging

Every API error has the same shape, and every response carries an `X-Request-ID` header:

```json
{ "error": { "code": "database_unavailable", "message": "Database is unreachable.", "request_id": "…" } }
```

Each log line includes that request ID, so an error a user reports can be matched to its log entry. Unhandled errors return a generic 500; the cause is logged, never sent to the client. Set `LOG_LEVEL` to change verbosity. In the web app, `apiFetch` in `lib/api.ts` turns these responses into a typed `ApiError`, and rejects a success response that does not match its Zod schema.

## Checks

Ruff lints and formats the API; ESLint lints and Prettier formats the web app (`npm run format` to apply).

The API's workspace tests run against real Postgres, so start the database first (`docker compose up -d db`). They create and use a separate `knowledgepilot_test` database and never touch your development data. Authentication tests sign real tokens with a throwaway key and mock the JWKS endpoint, so no test needs a Supabase project or network access. The web tests use Vitest with Testing Library.

```sh
cd apps/api && uv run pytest && uv run ruff check . && uv run ruff format --check .
```

```sh
cd apps/web && npm run typecheck && npm run lint && npm run format:check && npm test && npm run build
```
