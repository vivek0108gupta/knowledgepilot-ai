# KnowledgePilot AI

Upload documents, ask questions, and get answers grounded in those documents with citations back to the source page.

Built so far: sign-up and sign-in with Supabase Auth, workspaces with owner, admin and member roles, and document upload with text extraction. Embeddings, search and question answering come in later phases.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) with Compose, for the full stack.
- To run the apps outside Docker: Node.js 22 or newer, and [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 for you).
- A [Supabase](https://supabase.com) project, for sign-in and file storage. See [Authentication setup](#authentication-setup) and [Storage setup](#storage-setup).

## Layout

```
apps/
  web/                 Next.js App Router, TypeScript (strict), Tailwind CSS
    app/               routes: /login, /register, /dashboard/..., /auth/callback
    components/        auth forms, dashboard shell, workspace and document screens, UI primitives
    lib/api.ts         typed client for the API; sends the access token, validates with Zod
    lib/supabase/      Supabase clients for the browser and for proxy.ts
    proxy.ts           redirects signed-out visitors away from /dashboard (Next.js 16's middleware)
  api/                 FastAPI, SQLAlchemy (async), Alembic, Pytest
    app/api/v1/        HTTP routes, one module per resource
    app/api/deps.py    `CurrentUser`: the reusable dependency that verifies the access token
    app/core/          config, database, logging, errors, request middleware, JWT verification
    app/models/        SQLAlchemy models
    app/schemas/       Pydantic request and response models
    app/services/      business rules and authorization; storage, file validation,
                       text extraction and the processing pipeline
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

Sign-in needs no secret key. The secret key used for file storage is for the API only (see [Storage setup](#storage-setup)); a `NEXT_PUBLIC_*` variable would publish it to every visitor.

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

Workspace endpoints, all under `/api/v1`: `GET /me`, `GET|POST /workspaces`, `GET|PATCH|DELETE /workspaces/{id}`, `GET|POST /workspaces/{id}/members`, `DELETE /workspaces/{id}/members/{user_id}`.

User IDs are stored as plain UUID columns, not foreign keys to Supabase's `auth.users`, because the application database is a separate Postgres.

## Storage setup

Uploaded files live in a **private** Supabase Storage bucket. Only the API reads or writes it, with a server-side key. Browsers never get that key or a public URL: a download is a signed link the API issues after checking the caller's membership, and it stops working after 60 seconds.

1. In the Supabase dashboard, open **Storage > New bucket** and create:

   | Setting | Value |
   |---|---|
   | Name | `knowledgepilot-documents` |
   | Public bucket | **Off** |
   | File size limit | 25 MB |
   | Allowed MIME types | `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `text/plain` |

   The size and type limits repeat what the API already enforces, as a second barrier.

2. Add **no storage policies**. With none, the publishable key and signed-in users can do nothing in the bucket, which is the intent. The API's key does not need a policy.
3. In **Project Settings > API Keys**, create or copy a **secret** key (`sb_secret_...`; the legacy `service_role` key also works) and put it in `.env`:

   ```sh
   SUPABASE_SECRET_KEY=sb_secret_...
   ```

4. Restart the API: `docker compose up -d api`.

The API checks the bucket before its first upload and refuses to store anything if the bucket is missing or public. Files are stored at `workspace_id/document_id/filename`, built from server-generated IDs and a sanitised filename.

Treat the secret key like a database password: it is only ever set for the API container, never given a `NEXT_PUBLIC_` name, and never committed.

## Documents

**Supported formats:** PDF, DOCX and TXT (UTF-8), up to **25 MB** each. Scanned PDFs are not supported: there is no OCR, so a PDF with no text layer fails with a message saying so. Password-protected PDFs also fail with a clear message.

**What a file must pass.** The browser's filename and Content-Type are never trusted on their own:

- the extension must be `.pdf`, `.docx` or `.txt` (only the last extension counts, so `a.pdf.exe` is refused);
- the contents must really be that format: a PDF header, a zip containing a Word document, or valid UTF-8 text with no NUL bytes;
- a Content-Type that names a different format is treated as spoofing and refused;
- the size is measured from the bytes received and capped while they stream in;
- the same file content can exist once per workspace (a second copy is refused with 409).

**Who can do what.** Any member of a workspace can upload, view and download its documents. A member can delete or retry their own uploads; admins and owners can delete or retry any. Someone outside the workspace gets `404` for its documents, as if they did not exist.

**Processing lifecycle.** After upload, text is extracted in the background:

```
uploaded -> processing -> ready
                       -> failed   (with a reason the uploader can read; can be retried)
```

Extracted text is stored in `document_segments` with a reference back to the source: the page number for a PDF, the paragraph index and nearest heading for a DOCX, and the line range for a TXT. A later phase builds search and citations from these rows; no embeddings are created yet.

**Endpoints**, all under `/api/v1` and all requiring a signed-in workspace member:

| Method and path | Purpose |
|---|---|
| `POST /workspaces/{workspace_id}/documents` | Upload one file as multipart field `file`. Returns 201 with the document. |
| `GET /workspaces/{workspace_id}/documents` | List, newest first. Query: `status`, `limit` (1–100, default 20), `offset`. Returns `{items, total, limit, offset}`. |
| `GET /documents/{document_id}` | One document's details. |
| `GET /documents/{document_id}/status` | Status only, for polling. |
| `GET /documents/{document_id}/download` | A signed link: `{url, expires_in, filename}`. |
| `POST /documents/{document_id}/retry` | Process a failed document again. Returns 202. |
| `DELETE /documents/{document_id}` | Delete the document and its file. Returns 204. |

Upload errors use the standard error shape with these codes: `unsupported_file_type`, `mime_mismatch`, `content_mismatch`, `invalid_text_encoding` (415); `file_too_large` (413); `empty_file`, `invalid_filename`, `missing_file`, `invalid_upload` (400); `duplicate_document` (409); `storage_unavailable` (502); `storage_not_configured`, `storage_bucket_missing`, `storage_bucket_public` (503). Interactive documentation is at `/docs` on the API.

### Known limitations

- **Processing runs inside the API process.** If the API stops while a document is processing, that work is lost. On the next start, documents still waiting are queued again, and one stuck in `processing` for over 10 minutes is reset and can also be retried by hand. There is no automatic retry of failures and no scheduling across several API instances. `ProcessingQueue` in `app/services/processing.py` is the single place to swap in a durable queue with a separate worker.
- **No OCR.** Scanned or image-only PDFs fail.
- **DOCX has no page numbers.** Word files do not store them, so references are by paragraph and heading. Text in headers, footers, footnotes and text boxes is not extracted.
- **Legacy formats are not supported:** `.doc`, `.rtf`, `.odt`, and non-UTF-8 text files.
- **No malware scanning.** Files are checked for format, not for malicious content, and are only ever served as downloads.
- **Download links cannot be revoked early.** A link works for its 60 seconds even if the document is deleted or the member removed in that time.
- **No storage reconciliation job.** Failed operations clean up after themselves, but if storage is unreachable while a workspace is deleted, its files are left behind and the API logs how many.
- **No per-workspace storage quota or upload rate limit yet.**

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

The API's workspace tests run against real Postgres, so start the database first (`docker compose up -d db`). They create and use a separate `knowledgepilot_test` database and never touch your development data. Authentication tests sign real tokens with a throwaway key and mock the JWKS endpoint, and file storage is replaced by an in-memory fake, so no test needs a Supabase project or network access. Test PDF and DOCX files are generated on the fly; none are stored in the repository. The web tests use Vitest with Testing Library.

```sh
cd apps/api && uv run pytest && uv run ruff check . && uv run ruff format --check .
```

```sh
cd apps/web && npm run typecheck && npm run lint && npm run format:check && npm test && npm run build
```
