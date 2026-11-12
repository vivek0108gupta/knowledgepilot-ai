"""The Supabase Storage client against a mocked Storage API. No request leaves the process."""

import io
import json
import logging

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.services import storage as storage_module
from app.services.storage import StorageError, SupabaseStorage, encode_path, get_storage

BASE = "https://project.supabase.co"
SECRET = "sb_secret_test-value-never-real"
BUCKET = "knowledgepilot-documents"
PATH = "ws-1/doc-1/report.pdf"


class Api:
    """A scripted Storage API that records every request it receives."""

    def __init__(self, public: bool | None = False) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.public = public
        self.bad_key = False
        self.status: dict[str, int] = {}
        self.sign_body: dict[str, object] = {
            "signedURL": f"/object/sign/{BUCKET}/{PATH}?token=abc.def"
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        path = request.url.path
        if path == f"/storage/v1/bucket/{BUCKET}":
            if self.bad_key:
                # Exactly what Supabase sends: HTTP 400 with the real status in the body.
                return httpx.Response(
                    400, json={"statusCode": "403", "error": "Unauthorized", "code": "AccessDenied"}
                )
            if self.public is None:
                return httpx.Response(
                    400,
                    json={"statusCode": "404", "error": "Bucket not found", "code": "NoSuchBucket"},
                )
            return httpx.Response(
                self.status.get("bucket", 200), json={"name": BUCKET, "public": self.public}
            )
        if path.startswith(f"/storage/v1/object/sign/{BUCKET}/"):
            return httpx.Response(self.status.get("sign", 200), json=self.sign_body)
        if request.method == "DELETE":
            return httpx.Response(self.status.get("delete", 200), json=[])
        if request.method == "POST":
            return httpx.Response(self.status.get("upload", 200), json={"Key": PATH})
        if request.method == "GET":
            return httpx.Response(self.status.get("download", 200), content=b"file-bytes")
        return httpx.Response(500)

    def client(self) -> SupabaseStorage:
        return SupabaseStorage(BASE, SECRET, BUCKET, transport=httpx.MockTransport(self.handler))

    def calls(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]


async def test_upload_goes_to_the_private_bucket_with_the_server_key() -> None:
    api = Api()

    await api.client().upload(PATH, io.BytesIO(b"%PDF-1.4 data"), "application/pdf", 13)

    assert api.calls() == [
        ("GET", f"/storage/v1/bucket/{BUCKET}"),
        ("POST", f"/storage/v1/object/{BUCKET}/{PATH}"),
    ]
    upload = api.requests[1]
    assert upload.headers["authorization"] == f"Bearer {SECRET}"
    assert upload.headers["apikey"] == SECRET
    assert upload.headers["content-type"] == "application/pdf"
    assert upload.headers["content-length"] == "13"
    assert upload.headers["x-upsert"] == "false"
    assert api.bodies[1] == b"%PDF-1.4 data"


async def test_bucket_privacy_is_checked_once_then_remembered() -> None:
    api = Api()
    client = api.client()

    await client.upload("a/b/1.txt", io.BytesIO(b"1"), "text/plain", 1)
    await client.upload("a/b/2.txt", io.BytesIO(b"2"), "text/plain", 1)

    assert [method for method, _ in api.calls()] == ["GET", "POST", "POST"]


async def test_refuses_to_upload_into_a_public_bucket() -> None:
    api = Api(public=True)

    with pytest.raises(AppError) as caught:
        await api.client().upload(PATH, io.BytesIO(b"x"), "text/plain", 1)

    assert (caught.value.status_code, caught.value.code) == (503, "storage_bucket_public")
    assert [method for method, _ in api.calls()] == ["GET"]  # nothing was uploaded


async def test_missing_bucket_is_reported_as_not_set_up() -> None:
    api = Api(public=None)

    with pytest.raises(AppError) as caught:
        await api.client().upload(PATH, io.BytesIO(b"x"), "text/plain", 1)

    assert caught.value.code == "storage_bucket_missing"


async def test_signed_url_is_short_lived_absolute_and_forces_a_download() -> None:
    api = Api()

    url = await api.client().create_signed_url(PATH, 60, "Quarterly report (final).pdf")

    assert api.calls()[-1] == ("POST", f"/storage/v1/object/sign/{BUCKET}/{PATH}")
    assert json.loads(api.bodies[-1]) == {"expiresIn": 60}
    assert url == (
        f"{BASE}/storage/v1/object/sign/{BUCKET}/{PATH}?token=abc.def"
        "&download=Quarterly%20report%20%28final%29.pdf"
    )
    # Signed, never the public-object URL form.
    assert "/object/public/" not in url
    assert SECRET not in url


async def test_download_name_cannot_inject_extra_query_parameters() -> None:
    api = Api()

    url = await api.client().create_signed_url(PATH, 60, "a.pdf&token=forged#frag")

    assert url.endswith("&download=a.pdf%26token%3Dforged%23frag")
    assert url.count("token=") == 1


async def test_sign_response_without_a_token_is_an_error() -> None:
    api = Api()
    api.sign_body = {"signedURL": f"/object/public/{BUCKET}/{PATH}"}

    with pytest.raises(StorageError):
        await api.client().create_signed_url(PATH, 60, "a.pdf")


async def test_delete_sends_the_paths_as_prefixes() -> None:
    api = Api()

    await api.client().delete(["ws/doc-1/a.pdf", "ws/doc-2/b.txt"])

    assert api.calls()[-1] == ("DELETE", f"/storage/v1/object/{BUCKET}")
    assert json.loads(api.bodies[-1]) == {"prefixes": ["ws/doc-1/a.pdf", "ws/doc-2/b.txt"]}


async def test_delete_of_nothing_makes_no_request() -> None:
    api = Api()

    await api.client().delete([])

    assert api.calls() == []


async def test_download_streams_into_the_destination() -> None:
    api = Api()
    destination = io.BytesIO()

    await api.client().download_to(PATH, destination)

    assert destination.getvalue() == b"file-bytes"
    assert api.calls()[-1] == ("GET", f"/storage/v1/object/{BUCKET}/{PATH}")


@pytest.mark.parametrize("operation", ["upload", "download", "delete", "sign"])
async def test_storage_errors_become_a_502_without_leaking_the_key(
    operation: str, caplog: pytest.LogCaptureFixture
) -> None:
    api = Api()
    api.status[operation] = 500
    client = api.client()

    with caplog.at_level(logging.DEBUG), pytest.raises(StorageError) as caught:
        if operation == "upload":
            await client.upload(PATH, io.BytesIO(b"x"), "text/plain", 1)
        elif operation == "download":
            await client.download_to(PATH, io.BytesIO())
        elif operation == "delete":
            await client.delete([PATH])
        else:
            await client.create_signed_url(PATH, 60, "a.pdf")

    assert (caught.value.status_code, caught.value.code) == (502, "storage_unavailable")
    assert SECRET not in caplog.text
    assert SECRET not in caught.value.message


async def test_network_failure_becomes_a_502_without_leaking_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect, headers={dict(request.headers)}")

    client = SupabaseStorage(BASE, SECRET, BUCKET, transport=httpx.MockTransport(unreachable))

    with caplog.at_level(logging.DEBUG), pytest.raises(StorageError):
        await client.delete([PATH])

    assert SECRET not in caplog.text


def test_object_paths_are_encoded_segment_by_segment() -> None:
    assert encode_path("ws/doc/a b.pdf") == "ws/doc/a%20b.pdf"
    assert encode_path("ws/doc/a?b#c.pdf") == "ws/doc/a%3Fb%23c.pdf"
    # An encoded slash stays encoded, so it cannot become a path separator.
    assert encode_path("ws/doc/a%2Fb.pdf") == "ws/doc/a%252Fb.pdf"


def test_storage_is_unavailable_until_a_secret_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        storage_module, "get_settings", lambda: Settings(supabase_url=BASE, supabase_secret_key="")
    )
    get_storage.cache_clear()

    with pytest.raises(AppError) as caught:
        get_storage()

    assert (caught.value.status_code, caught.value.code) == (503, "storage_not_configured")
    get_storage.cache_clear()


def test_the_secret_key_does_not_appear_when_settings_are_printed() -> None:
    settings = Settings(supabase_secret_key=SECRET)

    assert SECRET not in repr(settings)
    assert SECRET not in str(settings.model_dump())


OPERATIONS = ["upload", "download", "delete", "sign"]


async def perform(client: SupabaseStorage, operation: str) -> None:
    if operation == "upload":
        await client.upload(PATH, io.BytesIO(b"x"), "text/plain", 1)
    elif operation == "download":
        await client.download_to(PATH, io.BytesIO())
    elif operation == "delete":
        await client.delete([PATH])
    else:
        await client.create_signed_url(PATH, 60, "a.pdf")


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_a_rejected_key_is_reported_as_a_credentials_problem_not_a_missing_bucket(
    operation: str,
) -> None:
    api = Api()
    api.bad_key = True

    with pytest.raises(AppError) as caught:
        await perform(api.client(), operation)

    assert (caught.value.status_code, caught.value.code) == (503, "storage_credentials_invalid")
    assert SECRET not in caught.value.message


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_every_operation_refuses_a_missing_bucket_before_acting(operation: str) -> None:
    """Storage reports a delete in a missing bucket as a success, so it must never get that far."""
    api = Api(public=None)

    with pytest.raises(AppError) as caught:
        await perform(api.client(), operation)

    assert caught.value.code == "storage_bucket_missing"
    assert [method for method, _ in api.calls()] == ["GET"]  # only the bucket check was sent


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_every_operation_refuses_a_public_bucket_before_acting(operation: str) -> None:
    api = Api(public=True)

    with pytest.raises(AppError) as caught:
        await perform(api.client(), operation)

    assert caught.value.code == "storage_bucket_public"
    assert [method for method, _ in api.calls()] == ["GET"]
