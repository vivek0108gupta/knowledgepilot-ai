"""Reading an uploaded file from a request without trusting how big the client says it is."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.types import Message

from app.core.errors import AppError

# Room for the multipart boundaries and part headers that wrap the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class BodyTooLarge(Exception):
    pass


def too_large() -> AppError:
    return AppError(413, "file_too_large", "Files can be up to 25 MB.")


@asynccontextmanager
async def single_file_upload(
    request: Request, max_file_bytes: int, field: str = "file"
) -> AsyncIterator[UploadFile]:
    """Yield the one file in a multipart request, reading at most `max_file_bytes` of it.

    The limit is enforced twice: on the declared Content-Length, to refuse an oversized
    upload before reading any of it, and on the bytes that actually arrive, because the
    header can be missing or false. Starlette spools the file to a temporary file on disk
    rather than memory, and that file is deleted when this context exits, however it exits.
    """
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise AppError(415, "invalid_upload", "Send the file as multipart/form-data.")

    max_body_bytes = max_file_bytes + MULTIPART_OVERHEAD_BYTES
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > max_body_bytes):
        raise too_large()

    received = 0

    async def limited_receive() -> Message:
        nonlocal received
        message = await request.receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > max_body_bytes:
                raise BodyTooLarge
        return message

    limited_request = Request(request.scope, limited_receive)
    try:
        form = await limited_request.form(max_files=1, max_fields=1)
    except BodyTooLarge:
        raise too_large() from None
    except (MultiPartException, MultipartParseError) as error:
        raise AppError(400, "invalid_upload", "The upload could not be read.") from error

    try:
        upload = form.get(field)
        if not isinstance(upload, UploadFile):
            raise AppError(400, "missing_file", f"Attach a file in the “{field}” field.")
        if upload.size is not None and upload.size > max_file_bytes:
            raise too_large()
        yield upload
    finally:
        await form.close()
