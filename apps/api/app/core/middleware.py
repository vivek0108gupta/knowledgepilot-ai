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


# The upload route enforces its own, larger limit while it streams the file.
UPLOAD_ROUTE = re.compile(r"^/api/v1/workspaces/[0-9a-fA-F-]{36}/documents/?$")
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class BodySizeLimit:
    """Refuse oversized request bodies before any of them is read.

    A request with a body must declare its `Content-Length`, and it must be at most
    `max_bytes`. Uvicorn never passes on more than the declared length, so the body the
    application reads is bounded too. Without this, every JSON endpoint would buffer whatever
    a client sent, signed in or not, before authentication or validation ran.
    """

    def __init__(self, app: Callable, max_bytes: int) -> None:  # type: ignore[type-arg]
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:  # type: ignore[type-arg]
        if (
            scope["type"] != "http"
            or scope["method"] not in BODY_METHODS
            or (scope["method"] == "POST" and UPLOAD_ROUTE.match(scope["path"]))
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        chunked = b"chunked" in headers.get(b"transfer-encoding", b"").lower()
        if declared is None and not chunked:
            await self.app(scope, receive, send)  # no body at all
            return
        if declared is None:
            await self._refuse(send, 411, "length_required", "Send a Content-Length header.")
            return
        if not declared.isdigit() or int(declared) > self.max_bytes:
            await self._refuse(
                send,
                413,
                "request_too_large",
                f"The request body can be at most {self.max_bytes:,} bytes.",
            )
            return
        await self.app(scope, receive, send)

    async def _refuse(self, send: Callable, status: int, code: str, message: str) -> None:  # type: ignore[type-arg]
        import json

        body = json.dumps(
            {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
