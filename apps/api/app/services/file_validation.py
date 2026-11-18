"""Decides whether an uploaded file is acceptable, from its bytes rather than its labels.

The filename and the browser's Content-Type are both chosen by whoever sent the request, so
neither is trusted on its own: the extension must be allowed, the content must really be that
format, and a Content-Type that contradicts the content is treated as a spoofing attempt.
"""

import codecs
import hashlib
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO

from app.core.errors import AppError
from app.models.document import MAX_DOCUMENT_BYTES

MAX_FILENAME_LENGTH = 120
CHUNK_BYTES = 256 * 1024

# A DOCX is a zip. Refuse archives that expand far beyond what a real document needs.
MAX_DOCX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_DOCX_ENTRIES = 5_000


class FileKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"


MIME_TYPES: dict[FileKind, str] = {
    FileKind.PDF: "application/pdf",
    FileKind.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    FileKind.TXT: "text/plain",
}

KIND_BY_MIME: dict[str, FileKind] = {mime: kind for kind, mime in MIME_TYPES.items()}

# Content-Types browsers send when they do not know better. They say nothing, so they are
# neither trusted nor treated as a contradiction.
GENERIC_MIME_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


@dataclass(frozen=True, slots=True)
class ValidatedFile:
    """A file that passed every check, with the values to store derived by the server."""

    filename: str  # cleaned name to show people
    storage_name: str  # ASCII-only name used inside the storage path
    kind: FileKind
    mime_type: str  # from the detected format, never from the request
    size_bytes: int
    sha256: str


def rejected(code: str, message: str, status: int = 400) -> AppError:
    return AppError(status, code, message)


def sanitize_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to a safe display name.

    Drops any directory part (so `../../etc/passwd` becomes `passwd`), control and invisible
    characters, and characters that are unsafe in paths or HTTP headers.
    """
    name = unicodedata.normalize("NFKC", raw or "")
    # Keep only the last path component, whichever separator the client's OS used.
    name = re.split(r"[\\/]", name)[-1]
    # Drop control, format (zero-width, bidi override) and unassigned characters.
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r'[<>:"|?*;%&$`\'=]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name in {".", ".."}:
        raise rejected("invalid_filename", "The file needs a name.")

    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        if dot and len(extension) <= 10:
            name = f"{stem[: MAX_FILENAME_LENGTH - len(extension) - 1]}.{extension}"
        else:
            name = name[:MAX_FILENAME_LENGTH]
    return name


def storage_filename(filename: str, kind: FileKind) -> str:
    """An ASCII-only name for the storage path, ending in the verified extension."""
    stem = filename.rsplit(".", 1)[0]
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", ascii_stem).strip("-_")[:80]
    return f"{ascii_stem or 'document'}.{kind.value}"


def kind_from_extension(filename: str) -> FileKind:
    """The format the name claims. Only the final extension counts, so `a.pdf.exe` is an exe."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        return FileKind(extension)
    except ValueError:
        raise rejected(
            "unsupported_file_type", "Only PDF, DOCX and TXT files can be uploaded.", 415
        ) from None


def check_declared_type(declared: str | None, kind: FileKind) -> None:
    """Reject a Content-Type that names a different supported or unsupported format."""
    media_type = (declared or "").split(";")[0].strip().lower()
    if media_type in GENERIC_MIME_TYPES:
        return
    if KIND_BY_MIME.get(media_type) is not kind:
        raise rejected("mime_mismatch", "The file's type does not match its extension.", 415)


def content_mismatch() -> AppError:
    return rejected("content_mismatch", "The file's contents do not match its extension.", 415)


def check_pdf(file: BinaryIO) -> None:
    # The PDF specification allows the header anywhere in the first 1024 bytes.
    file.seek(0)
    if b"%PDF-" not in file.read(1024):
        raise content_mismatch()


def check_docx(file: BinaryIO) -> None:
    file.seek(0)
    if file.read(4) != b"PK\x03\x04":
        raise content_mismatch()
    file.seek(0)
    try:
        with zipfile.ZipFile(file) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            # Any zip starts with PK; a Word document also has these two parts.
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise content_mismatch()
            if len(entries) > MAX_DOCX_ENTRIES or (
                sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES
            ):
                raise rejected(
                    "file_too_complex", "This document is too large to process once unpacked."
                )
    except zipfile.BadZipFile:
        raise content_mismatch() from None


def check_text(file: BinaryIO) -> None:
    """Plain text has no signature, so require what text is: valid UTF-8 with no NUL bytes."""
    file.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        while chunk := file.read(CHUNK_BYTES):
            if b"\x00" in chunk:
                raise content_mismatch()
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise rejected("invalid_text_encoding", "Text files must be saved as UTF-8.", 415) from None


CONTENT_CHECKS = {FileKind.PDF: check_pdf, FileKind.DOCX: check_docx, FileKind.TXT: check_text}


def measure(file: BinaryIO) -> tuple[int, str]:
    """The file's real size and SHA-256, read in chunks."""
    file.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := file.read(CHUNK_BYTES):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def validate_upload(
    raw_filename: str | None, declared_type: str | None, file: BinaryIO
) -> ValidatedFile:
    """Run every check on an uploaded file. Blocking: call it from a worker thread.

    Leaves the file positioned at its start, ready to be stored.
    """
    filename = sanitize_filename(raw_filename)
    kind = kind_from_extension(filename)
    check_declared_type(declared_type, kind)

    # Measured from the bytes received, not from a header the client could have set.
    size, sha256 = measure(file)
    if size == 0:
        raise rejected("empty_file", "The file is empty.")
    if size > MAX_DOCUMENT_BYTES:
        raise rejected("file_too_large", "Files can be up to 25 MB.", 413)

    CONTENT_CHECKS[kind](file)
    file.seek(0)

    return ValidatedFile(
        filename=filename,
        storage_name=storage_filename(filename, kind),
        kind=kind,
        mime_type=MIME_TYPES[kind],
        size_bytes=size,
        sha256=sha256,
    )
