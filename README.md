# KnowledgePilot AI

Upload documents, ask questions, and get answers grounded in those documents with citations back to the source page.

This is the Phase 1 foundation: the web app, the API, and a local Postgres with pgvector. Auth, uploads, and RAG come in later phases.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) with Compose, for the full stack.
- To run the apps outside Docker: Node.js 22.18 or newer, and [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 for you).

## Layout

```
apps/
  web/                 Next.js App Router, TypeScript (strict), Tailwind CSS
    app/               routes and layouts
    components/        React components, including the TanStack Query provider
    lib/api.ts         typed client for the API; responses are validated with Zod
  api/                 FastAPI, SQLAlchemy (async), Alembic, Pytest
    app/api/v1/        HTTP routes, one module per resource
    app/core/          config, database, logging, errors, request middleware
    app/models/        SQLAlchemy models
    app/schemas/       Pydantic request and response models
    app/services/      business logic called by the routes
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

```sh
cd apps/api && uv run pytest && uv run ruff check . && uv run ruff format --check .
```

```sh
cd apps/web && npm run typecheck && npm run lint && npm run format:check && npm test && npm run build
```
