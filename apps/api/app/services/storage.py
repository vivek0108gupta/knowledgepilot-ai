"""Private file storage.

Only the API talks to storage, using a server-side key. Browsers never get that key or a
public URL: the only way to a file is a short-lived signed URL the API issues after checking
that the caller may read the document.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import BinaryIO, Protocol
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

CHUNK_BYTES = 256 * 1024


class StorageError(AppError):
    """Storage could not do what was asked. Never carries keys, URLs with tokens, or content."""

    def __init__(self, message: str = "File storage is temporarily unavailable.") -> None:
        super().__init__(502, "storage_unavailable", message)


class Storage(Protocol):
    """What the rest of the app needs from file storage. Swappable for a fake in tests."""

    async def upload(self, path: str, file: BinaryIO, content_type: str, size: int) -> None: ...

    async def download_to(self, path: str, destination: BinaryIO) -> None: ...

    async def delete(self, paths: list[str]) -> None: ...

    async def create_signed_url(self, path: str, expires_in: int, download_name: str) -> str: ...


def encode_path(path: str) -> str:
    """Percent-encode an object path for a URL, keeping the slashes between its parts."""
    return "/".join(quote(part, safe="") for part in path.split("/"))


async def read_chunks(file: BinaryIO) -> AsyncIterator[bytes]:
    """Read a file in chunks without blocking the event loop or holding it all in memory."""
    while chunk := await asyncio.to_thread(file.read, CHUNK_BYTES):
        yield chunk


class SupabaseStorage:
    """Supabase Storage over its REST API, restricted to one private bucket."""

    def __init__(
        self,
        base_url: str,
        secret_key: str,
        bucket: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._storage_url = f"{base_url.rstrip('/')}/storage/v1"
        self._bucket = bucket
        self._client = httpx.AsyncClient(
            headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )
        self._bucket_verified = False
        self._verify_lock = asyncio.Lock()

    def _object_url(self, path: str) -> str:
        return f"{self._storage_url}/object/{self._bucket}/{encode_path(path)}"

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as error:
            # Log the kind of failure only: the exception text can include the request URL.
            logger.error("Storage request failed: %s %s", method, type(error).__name__)
            raise StorageError() from error

    def _fail(self, action: str, response: httpx.Response) -> StorageError:
        logger.error("Storage %s failed with status %d", action, response.status_code)
        return StorageError()

    async def _ensure_private_bucket(self) -> None:
        """Refuse to store anything unless the bucket exists and is private."""
        if self._bucket_verified:
            return
        async with self._verify_lock:
            if self._bucket_verified:
                return
            response = await self._request("GET", f"{self._storage_url}/bucket/{self._bucket}")
            if response.status_code in (400, 404):
                logger.error("Storage bucket %r does not exist", self._bucket)
                raise AppError(
                    503,
                    "storage_bucket_missing",
                    "Document storage is not set up yet. Create the storage bucket first.",
                )
            if response.status_code != 200:
                raise self._fail("bucket check", response)
            if response.json().get("public") is not False:
                logger.error("Storage bucket %r is public; refusing to use it", self._bucket)
                raise AppError(
                    503,
                    "storage_bucket_public",
                    "Document storage is misconfigured: the bucket must be private.",
                )
            self._bucket_verified = True

    async def upload(self, path: str, file: BinaryIO, content_type: str, size: int) -> None:
        await self._ensure_private_bucket()
        response = await self._request(
            "POST",
            self._object_url(path),
            content=read_chunks(file),
            headers={
                "Content-Type": content_type,
                "Content-Length": str(size),
                # Never overwrite: every document has its own path, so a collision is a bug.
                "x-upsert": "false",
                "Cache-Control": "private, max-age=0, no-store",
            },
        )
        if response.status_code not in (200, 201):
            raise self._fail("upload", response)

    async def download_to(self, path: str, destination: BinaryIO) -> None:
        try:
            async with self._client.stream("GET", self._object_url(path)) as response:
                if response.status_code != 200:
                    raise self._fail("download", response)
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    await asyncio.to_thread(destination.write, chunk)
        except httpx.HTTPError as error:
            logger.error("Storage request failed: GET %s", type(error).__name__)
            raise StorageError() from error

    async def delete(self, paths: list[str]) -> None:
        """Delete objects. Paths that are already gone are not an error."""
        if not paths:
            return
        response = await self._request(
            "DELETE", f"{self._storage_url}/object/{self._bucket}", json={"prefixes": paths}
        )
        if response.status_code != 200:
            raise self._fail("delete", response)

    async def create_signed_url(self, path: str, expires_in: int, download_name: str) -> str:
        response = await self._request(
            "POST",
            f"{self._storage_url}/object/sign/{self._bucket}/{encode_path(path)}",
            json={"expiresIn": expires_in},
        )
        if response.status_code != 200:
            raise self._fail("sign", response)
        signed_path = response.json().get("signedURL")
        if not isinstance(signed_path, str) or "token=" not in signed_path:
            logger.error("Storage sign returned an unexpected body")
            raise StorageError()
        # `download` makes the browser save the file under its original name instead of
        # rendering it, which also keeps an uploaded file from running as a page.
        return f"{self._storage_url}{signed_path}&download={quote(download_name, safe='')}"

    async def aclose(self) -> None:
        await self._client.aclose()


@lru_cache
def get_storage() -> Storage:
    """The configured storage backend. Fails clearly when storage has not been set up."""
    settings = get_settings()
    secret_key = settings.supabase_secret_key.get_secret_value()
    if not settings.supabase_url or not secret_key:
        raise AppError(
            503,
            "storage_not_configured",
            "Document storage is not configured. Set SUPABASE_URL and SUPABASE_SECRET_KEY.",
        )
    return SupabaseStorage(settings.supabase_url, secret_key, settings.supabase_storage_bucket)
