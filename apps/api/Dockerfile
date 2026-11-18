# syntax=docker/dockerfile:1
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first so this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# The tokenizer's vocabulary is downloaded on first use. Fetch it now, into the image, so
# the running API never depends on that download and starts chunking immediately.
ENV TIKTOKEN_CACHE_DIR=/app/.tiktoken
RUN /app/.venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --create-home --uid 1000 api
USER api

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
# Apply pending migrations, then serve. `exec` makes uvicorn PID 1 so it receives stop signals.
# Hosts such as Render say which port to listen on in PORT; otherwise 8000.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
