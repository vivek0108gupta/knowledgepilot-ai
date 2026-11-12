import logging
from contextvars import ContextVar
from logging.config import dictConfig

# Set per request by the middleware in app.core.middleware; "-" outside a request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Stamps every log record with the current request's ID."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Send all logs, including uvicorn's, to stdout in one format."""
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                    "filters": ["request_id"],
                }
            },
            "root": {"level": level.upper(), "handlers": ["stdout"]},
            "loggers": {
                # Uvicorn's own handlers are replaced so its lines share the format above.
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                # The request middleware logs each request, so uvicorn's access log is redundant.
                "uvicorn.access": {"handlers": [], "propagate": False},
                # These log every outgoing request URL at INFO. Storage URLs contain file
                # names, so only their warnings and errors are kept.
                "httpx": {"level": "WARNING"},
                "httpx2": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "openai": {"level": "WARNING"},
            },
        }
    )
