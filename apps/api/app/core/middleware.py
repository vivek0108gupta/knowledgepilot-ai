import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from app.core.errors import internal_error_response
from app.core.logging import request_id_var

logger = logging.getLogger("app.request")

REQUEST_ID_HEADER = "X-Request-ID"
# A caller-supplied ID is echoed into logs and headers, so only accept a safe, bounded one.
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Give each request an ID, log its outcome, and turn unhandled errors into a clean 500."""
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = supplied if SAFE_REQUEST_ID.match(supplied) else uuid.uuid4().hex
    token = request_id_var.set(request_id)
    started = time.perf_counter()

    try:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            response = internal_error_response()

        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "%s %s -> %d in %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        request_id_var.reset(token)
