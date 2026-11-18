from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var
from app.core.metrics import metrics


class AppError(Exception):
    """An expected failure with a stable, machine-readable code the frontend can branch on."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        # When trying again can succeed after a wait, how long to wait. Sent both as a
        # `Retry-After` header and in the error body, which browsers can always read.
        self.retry_after_seconds = retry_after_seconds
        if retry_after_seconds is not None:
            self.headers["Retry-After"] = str(retry_after_seconds)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, object]] | None = None,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    """Every error leaves the API in this one shape."""
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request_id_var.get(),
    }
    if details is not None:
        error["details"] = details
    if retry_after_seconds is not None:
        error["retry_after_seconds"] = retry_after_seconds
    return JSONResponse(status_code=status_code, content={"error": error})


def internal_error_response() -> JSONResponse:
    # Deliberately generic: the cause goes to the logs, never to the client.
    return error_response(500, "internal_error", "Something went wrong on our side.")


async def handle_app_error(_request: Request, error: AppError) -> JSONResponse:
    # Quota and rate-limit refusals show here as their codes (workspace_quota_exceeded, ...).
    metrics.inc("app_errors_total", code=error.code, status=error.status_code)
    response = error_response(
        error.status_code, error.code, error.message, retry_after_seconds=error.retry_after_seconds
    )
    if error.headers:
        response.headers.update(error.headers)
    return response


async def handle_http_exception(_request: Request, error: StarletteHTTPException) -> JSONResponse:
    code = HTTPStatus(error.status_code).phrase.lower().replace(" ", "_")
    response = error_response(error.status_code, code, str(error.detail))
    if error.headers:
        response.headers.update(error.headers)
    return response


async def handle_validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
    details: list[dict[str, object]] = [
        {"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
        for item in error.errors()
    ]
    return error_response(422, "validation_error", "The request is invalid.", details)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
