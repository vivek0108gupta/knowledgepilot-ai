import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import REQUEST_ID_HEADER, request_context

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """On startup, pick up documents whose processing was cut short by the last shutdown."""
    from app.core.db import SessionLocal
    from app.services.processing import get_processing_queue, recover_interrupted

    try:
        # Documents processed before semantic search existed have no chunks. Index them
        # now, but only if embeddings are configured: otherwise they would just fail.
        can_embed = bool(get_settings().openai_api_key.get_secret_value())
        recovered = await recover_interrupted(
            SessionLocal, get_processing_queue(), reindex_unchunked=can_embed
        )
        if recovered:
            logger.info("Re-queued %d document(s) left unprocessed", recovered)
    except Exception:
        # A database that is not ready yet must not stop the API from starting.
        logger.exception("Could not check for unprocessed documents at startup")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    # Middleware added later wraps middleware added earlier, so CORS is outermost and its
    # headers are also present on error responses produced by `request_context`.
    app.middleware("http")(request_context)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
    )

    register_error_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
