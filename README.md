# KnowledgePilot AI

Upload documents, ask questions, and get answers grounded in those documents with citations back to the source page.

Built so far: sign-up and sign-in with Supabase Auth, workspaces with owner, admin and member roles, document upload with text extraction, semantic search over documents, and private conversations that store the user's messages. AI answers are not implemented yet: they come in the next phase.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) with Compose, for the full stack.
- To run the apps outside Docker: Node.js 22 or newer, and [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 for you).
- A [Supabase](https://supabase.com) project, for sign-in and file storage. See [Authentication setup](#authentication-setup) and [Storage setup](#storage-setup).
- An [OpenAI](https://platform.openai.com) API key, for embeddings. See [Semantic search](#semantic-search).

## Layout

```
apps/
  web/                 Next.js App Router, TypeScript (strict), Tailwind CSS
    app/               routes: /login, /register, /dashboard/..., /auth/callback
    components/        auth forms, dashboard shell, workspace, document, search and chat screens
    lib/api.ts         typed client for the API; sends the access token, validates with Zod
    lib/supabase/      Supabase clients for the browser and for proxy.ts
    proxy.ts           redirects signed-out visitors away from /dashboard (Next.js 16's middleware)
  api/                 FastAPI, SQLAlchemy (async), Alembic, Pytest
    app/api/v1/        HTTP routes, one module per resource
    app/api/deps.py    `CurrentUser`: the reusable dependency that verifies the access token
    app/core/          config, database, logging, errors, request middleware, JWT verification
    app/models/        SQLAlchemy models
    app/schemas/       Pydantic request and response models
    app/services/      business rules and authorization; storage, file validation, text
                       extraction, chunking, embeddings, search, and the processing pipeline
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

The API checks the bucket before it uploads, downloads, signs or deletes anything, and refuses to act if the key is rejected, or the bucket is missing or public. Files are stored at `workspace_id/document_id/filename`, built from server-generated IDs and a sanitised filename.

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

Extracted text is stored in `document_segments` with a reference back to the source: the page number for a PDF, the paragraph index and nearest heading for a DOCX, and the line range for a TXT. It is then chunked and embedded for [semantic search](#semantic-search); a document becomes `ready` only once every chunk has its embedding saved.

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

Upload errors use the standard error shape with these codes: `unsupported_file_type`, `mime_mismatch`, `content_mismatch`, `invalid_text_encoding` (415); `file_too_large` (413); `empty_file`, `invalid_filename`, `missing_file`, `invalid_upload` (400); `duplicate_document` (409); `storage_unavailable` (502); `storage_not_configured`, `storage_credentials_invalid`, `storage_bucket_missing`, `storage_bucket_public` (503). Interactive documentation is at `/docs` on the API.

### Known limitations

- **Processing runs inside the API process.** If the API stops while a document is processing, that work is lost. On the next start, documents still waiting are queued again, and one stuck in `processing` for over 10 minutes is reset and can also be retried by hand. There is no automatic retry of failures and no scheduling across several API instances. `ProcessingQueue` in `app/services/processing.py` is the single place to swap in a durable queue with a separate worker.
- **No OCR.** Scanned or image-only PDFs fail.
- **DOCX has no page numbers.** Word files do not store them, so references are by paragraph and heading. Text in headers, footers, footnotes and text boxes is not extracted.
- **Legacy formats are not supported:** `.doc`, `.rtf`, `.odt`, and non-UTF-8 text files.
- **No malware scanning.** Files are checked for format, not for malicious content, and are only ever served as downloads.
- **Download links cannot be revoked early.** A link works for its 60 seconds even if the document is deleted or the member removed in that time.
- **No storage reconciliation job.** Failed operations clean up after themselves, but if storage is unreachable while a workspace is deleted, its files are left behind and the API logs how many.
- **No per-workspace storage quota or upload rate limit yet.**

## Semantic search

Search finds passages by meaning. It is retrieval only: it returns the stored passages closest to the query, with their sources. No language model reads or rewrites them yet.

### How it works

```
upload -> extract text -> split into chunks -> embed each chunk -> store vectors -> ready
query  -> embed the query -> nearest chunks by cosine distance, in this workspace only
```

| Stage | Where | What it does |
|---|---|---|
| Extraction | `app/services/extraction.py` | Text with page, paragraph or line references (Phase 3). |
| Chunking | `app/services/chunking.py` | Splits text into token-sized chunks. No database, no network. |
| Embedding | `app/services/embeddings.py` | Calls OpenAI. Knows nothing about documents or chunks. |
| Pipeline | `app/services/processing.py` | The only place that joins the three to storage and the database. |
| Search | `app/services/search.py` | Embeds the query and runs the vector query. |

**Chunking strategy.** Sizes are counted in tokens of the embedding model (`cl100k_base`), not characters. A chunk holds up to **800 tokens**, and each chunk after the first repeats up to **100 tokens** from the end of the previous one, so a passage that straddles a boundary is still found whole. Chunks are built from whole sentences (whole lines for text files); a sentence is cut only when it alone is larger than a chunk, and then between words. Both sizes are configurable. Each chunk records where its own text starts: the page for a PDF, the paragraph for a DOCX, the line range for a TXT. A DOCX chunk never has a page number, because Word files do not store one.

**Embeddings.** Model `text-embedding-3-small`, **1536 dimensions**, through the official OpenAI SDK. Chunks are sent in batches of 64. Rate limits, timeouts and server errors are retried up to 5 times with exponential backoff and jitter, honouring a `Retry-After` header; a rejected request (bad key, bad input) is not retried. Every response is checked: one vector per input, each exactly 1536 finite numbers. Neither document text, queries nor the API key are ever logged.

**Storage: pgvector with an HNSW index.** Vectors live in the same PostgreSQL database, in `document_chunks`:

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `document_id`, `workspace_id` | uuid | A composite foreign key to `documents(id, workspace_id)`: the database rejects a chunk whose workspace is not its document's. Both cascade on delete. |
| `chunk_index` | integer | Reading order. Unique per document, so reprocessing cannot duplicate chunks. |
| `content`, `token_count` | text, integer | The passage and its size. |
| `page_number`, `paragraph_index`, `line_start`, `line_end` | integer, nullable | Source reference; only what the format has. |
| `embedding` | `vector(1536)` | Indexed with HNSW using `vector_cosine_ops`. |
| `metadata` | jsonb | Where the chunk ends, and its section heading. Never credentials or user details. |
| `created_at` | timestamptz | |

Search orders by cosine distance (`<=>`) and reports `1 - distance` as the similarity. Because results are filtered by workspace, the query turns on pgvector's iterative index scan so the filter cannot starve it of results.

**Processing lifecycle.** `uploaded -> processing -> ready`, or `-> failed`. Chunks are written in batches while the document is still `processing`, where search cannot see them. If extraction, embedding or the database fails at any point, the chunks already written are deleted and the document is marked `failed` with a reason; it can be retried. A document is never `ready` with missing embeddings, and retrying or a duplicate worker never duplicates chunks. Documents that were `ready` before search existed are re-indexed when the API starts, provided `OPENAI_API_KEY` is set.

### Search endpoint

`POST /api/v1/workspaces/{workspace_id}/search`, for signed-in members of that workspace.

```json
{ "query": "What is the refund policy?", "top_k": 5, "document_ids": [] }
```

| Field | Rules |
|---|---|
| `query` | Required, 1 to 1,000 characters after trimming. |
| `top_k` | Optional, default 5, from 1 to `SEARCH_MAX_TOP_K` (20). |
| `document_ids` | Optional, up to 50. Restricts the search to those documents. |
| `min_similarity` | Optional, 0 to 1. Overrides `SEARCH_MIN_SIMILARITY` for this request. |

```json
{
  "query": "What is the refund policy?",
  "results": [
    {
      "chunk_id": "…",
      "document_id": "…",
      "document_name": "refund-policy.pdf",
      "content": "Customers can request a refund…",
      "similarity": 0.87,
      "source": { "page_number": 4, "paragraph_index": null, "line_start": null, "line_end": null }
    }
  ]
}
```

Only `ready` documents are searched. An empty workspace, or a query nothing resembles, returns `"results": []`. Errors: `401` no valid token; `404` not a member of the workspace (or it does not exist); `422` invalid request; `503 embeddings_not_configured` when `OPENAI_API_KEY` is missing; `503 embeddings_unavailable` when OpenAI cannot be reached.

**Tenant isolation is in the SQL.** The one query that reads chunks filters on the workspace and joins the caller's own membership row. A document ID from another workspace matches nothing, and no request body field can widen the search. The membership check also runs before the query text is sent to OpenAI, so an outsider's query never leaves the server.

**Document text is untrusted.** It is stored, searched and returned as data. The web app renders it as plain text, never as HTML. Anything that later passes retrieved text to a language model must treat it as untrusted input.

### Setting it up

1. Create an API key at [platform.openai.com](https://platform.openai.com/api-keys).
2. Put it in `.env` (never in `.env.example`, and never in a `NEXT_PUBLIC_*` variable):

   ```sh
   OPENAI_API_KEY=sk-...
   ```

3. Restart the API: `docker compose up -d api`. Documents uploaded earlier are indexed at startup.

Without a key the app still runs, but it says so: uploads end as `failed` with "Search indexing is not set up on the server yet", and search returns `503 embeddings_not_configured`. Add the key and use **Try again** on a failed document.

The API image downloads the tokenizer's vocabulary while it is built, so the running container does not fetch it. Outside Docker it is downloaded once, on first use, and cached.

### Limitations of search

- **Not a durable queue.** Indexing runs inside the API process, as in Phase 3. A restart loses work in progress; it is picked up again at the next start.
- **A failed attempt is redone from the start.** Its chunks are deleted, so a retry embeds the whole document again rather than resuming. Identical chunks within one request are embedded once.
- **The similarity cut-off (0.2) is an estimate.** It has not been calibrated against real queries. Tune `SEARCH_MIN_SIMILARITY`, or pass `min_similarity` per request.
- **Vector search only.** No keyword or hybrid search and no re-ranking, so exact codes, names or numbers may rank below loosely related text.
- **One citation point per chunk.** A chunk that spans pages is cited by the page it starts on; where it ends is kept in `metadata`.
- **The embedding size is fixed at 1536** by the column type. A model with another size needs a migration and re-indexing.
- **No rate limit on search yet,** and every search makes one OpenAI call.
- **A very large document is refused** rather than half-indexed, at 20,000 chunks.

## Conversations

Each workspace has a chat area where a user keeps conversations and the messages they write in them. This phase is the foundation only: **messages are stored and shown, but nothing answers them yet.** No language model is called anywhere in this feature, and it works with no OpenAI key. Answer generation will be added in the next phase (Phase 5.2), using the model named by `OPENAI_CHAT_MODEL`.

### Privacy model

**A conversation is private to the user who created it.** Other members of the same workspace, including its owner and admins, cannot list it, read it, rename it, delete it, or write to it. Sharing conversations within a workspace is not implemented.

Two conditions must both hold for any access, and both are part of the single database query that loads a conversation:

1. the caller created the conversation;
2. the caller is still a member of the workspace it belongs to.

So a user who leaves or is removed from a workspace loses access to their conversations there. A conversation that belongs to someone else gets the same `404` as one that does not exist, so conversation IDs cannot be probed. The caller's identity always comes from the verified access token; no request can supply `created_by` or `workspace_id`, and a conversation cannot be moved to another workspace.

### Database tables

`conversations`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `workspace_id` | uuid | Foreign key to `workspaces`, cascades on delete. Set once; nothing updates it. |
| `created_by` | uuid | The Supabase user ID from the verified token. |
| `title` | varchar(120) | Defaults to "New conversation". Length checked by the database. |
| `status` | varchar(16) | `active` or `archived`, checked by the database. |
| `created_at`, `updated_at` | timestamptz | Stored as absolute instants, written in UTC. |

Indexed on `workspace_id`, `created_by` and `updated_at`.

`messages`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `conversation_id` | uuid | Foreign key to `conversations`, cascades on delete. |
| `role` | varchar(16) | `user` or `assistant`. There is no `system` role, by design. |
| `content` | text | Stored exactly as written. Must not be empty. |
| `sequence_number` | integer | Position in the conversation, from 1. Unique per conversation. |
| `client_message_id` | uuid, nullable | Client-generated, for idempotency. Unique per conversation when set. |
| `metadata` | jsonb, nullable | For later use by answers (citations). Never prompts or credentials. |
| `model`, `input_tokens`, `output_tokens` | nullable | Filled in for assistant messages in the next phase. |
| `created_at` | timestamptz | |

The unique constraint on `(conversation_id, sequence_number)` also serves every lookup by conversation.

### Message persistence

- **Only user messages can be created through the API.** The request has no `role` field, and a request that includes one (or a `sequence_number`, `model` or `metadata`) is rejected. Assistant messages will be written by trusted backend code in the next phase; the database accepts them but the public endpoint does not. Prompts given to a model are never stored as messages, so they cannot be read back.
- **Ordering is by `sequence_number`, never by timestamp.** The number is assigned by the server while it holds a row lock on the conversation, so messages sent at the same moment get distinct, consecutive numbers.
- **Duplicates are prevented.** The client sends a `client_message_id` with each message and reuses it when retrying. Sending the same ID again returns the message already stored, with `200` instead of `201`. Reusing an ID for different text is a `409`.
- **Limits.** 1 to 8,000 characters; empty and whitespace-only messages are rejected.
- **Titles.** When the first message arrives in a conversation still called "New conversation", the title becomes that message, trimmed to 60 characters at a word boundary. No model is involved, and a title the user chose is never overwritten.
- **Message text is never logged,** only IDs and lengths.

### Endpoints

All under `/api/v1`, all requiring a valid access token.

| Method and path | Purpose |
|---|---|
| `POST /workspaces/{workspace_id}/conversations` | Create. Body optional: `{"title": "…"}`. Returns 201. |
| `GET /workspaces/{workspace_id}/conversations` | The caller's own conversations, most recently active first. Query: `status` (`active` by default, or `archived`), `limit` (1–100, default 20), `offset`. Includes `message_count`; never message text. |
| `GET /conversations/{conversation_id}` | Metadata only. |
| `PATCH /conversations/{conversation_id}` | Rename: `{"title": "…"}` (1–120 characters). |
| `POST /conversations/{conversation_id}/archive` | Hide from the default list. Archived conversations keep their messages but accept no new ones. |
| `POST /conversations/{conversation_id}/restore` | Make it active again. |
| `DELETE /conversations/{conversation_id}` | Delete it and all its messages. Returns 204. |
| `GET /conversations/{conversation_id}/messages` | The most recent messages, oldest first. Query: `limit` (1–200, default 50), `before` (a sequence number, to page back). Returns `{items, total, has_more}`. |
| `POST /conversations/{conversation_id}/messages` | Add a user message: `{"content": "…", "client_message_id": "uuid"}`. Returns 201, or 200 for a replay. |

Errors use the standard shape: `401` no valid token; `404 conversation_not_found` or `workspace_not_found`; `422 validation_error`; `409 idempotency_conflict` or `conversation_archived`.

### In the web app

Open a workspace and choose **Open chat**, or go to `/dashboard/workspaces/{workspace_id}/chat`. The open conversation is part of the URL, so refreshing keeps it. Enter sends; Shift+Enter starts a new line.

### Limitations of conversations

- **No AI answers yet.** The app says so in the chat area. Nothing calls OpenAI from this feature.
- **Private only.** There is no sharing, and a workspace owner cannot see members' conversations.
- **Leaving a workspace hides your conversations there.** They are kept in the database and reappear if you rejoin; they are deleted only with the workspace or by you.
- **Archive has no screen yet.** The endpoints exist; the web app offers rename and delete.
- **The list shows the 100 most recent conversations.** The API paginates; the sidebar does not page yet.
- **Messages cannot be edited or deleted individually.**
- **No rate limit on sending messages yet.**

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

Migrations so far: `0001` pgvector, `0002` workspaces and members, `0003` documents, `0004` document chunks, `0005` conversations and messages. `0005` only adds two tables and changes no existing one. The API container applies pending migrations when it starts, so `docker compose up --build -d` is enough to upgrade.

For autogenerate to see a new model, import its module in `app/models/__init__.py`.

## Errors and logging

Every API error has the same shape, and every response carries an `X-Request-ID` header:

```json
{ "error": { "code": "database_unavailable", "message": "Database is unreachable.", "request_id": "…" } }
```

Each log line includes that request ID, so an error a user reports can be matched to its log entry. Unhandled errors return a generic 500; the cause is logged, never sent to the client. Set `LOG_LEVEL` to change verbosity. In the web app, `apiFetch` in `lib/api.ts` turns these responses into a typed `ApiError`, and rejects a success response that does not match its Zod schema.

## Checks

Ruff lints and formats the API; ESLint lints and Prettier formats the web app (`npm run format` to apply).

The API's workspace tests run against real Postgres, so start the database first (`docker compose up -d db`). They create and use a separate `knowledgepilot_test` database and never touch your development data. Authentication tests sign real tokens with a throwaway key and mock the JWKS endpoint, file storage is replaced by an in-memory fake, and OpenAI is mocked, so no test needs a Supabase project, an OpenAI key, or calls either service. The embedding client is tested through the real OpenAI SDK against a scripted HTTP response. Vectors are real: chunk and search tests write to a `vector(1536)` column and rank with pgvector's cosine distance. If the test database stops responding, the run aborts at once with a message instead of waiting. Test PDF and DOCX files are generated on the fly; none are stored in the repository. The web tests use Vitest with Testing Library.

```sh
cd apps/api && uv run pytest && uv run ruff check . && uv run ruff format --check .
```

```sh
cd apps/web && npm run typecheck && npm run lint && npm run format:check && npm test && npm run build
```
