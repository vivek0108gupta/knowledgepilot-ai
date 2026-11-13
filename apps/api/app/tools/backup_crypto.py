"""Authenticated, streaming encryption for backups (AES-256-GCM in 1 MiB frames).

File layout: MAGIC, a random 12-byte file id, then frames of
`[4-byte big-endian length][ciphertext + 16-byte tag]`. Each frame's nonce is the file id's
first 4 bytes plus an 8-byte frame counter, and its associated data binds the magic, file id,
counter and whether it is the last frame. So reordering, dropping, truncating or altering any
part of the file makes decryption fail; a file that decrypts is exactly what was written.

The key is 32 random bytes, base64-encoded, from BACKUP_ENCRYPTION_KEY or a key file. Keep it
in the secret store, never next to the backups: whoever holds the backups and not the key
learns nothing but their size.
"""

import base64
import os
import secrets
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"KPBACKUP1"
FRAME = 1024 * 1024


class BackupKeyError(Exception):
    pass


class BackupCorrupt(Exception):
    pass


def load_key(key_file: str | None = None) -> bytes:
    raw = Path(key_file).read_text() if key_file else os.environ.get("BACKUP_ENCRYPTION_KEY", "")
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except ValueError as error:
        raise BackupKeyError("The backup key is not valid base64.") from error
    if len(key) != 32:
        raise BackupKeyError(
            "Set BACKUP_ENCRYPTION_KEY (or --key-file) to 32 random bytes, base64-encoded."
        )
    return key


def new_key_file(path: str) -> None:
    """Write a new random key to `path`, readable by its owner only. Never overwrites."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")


def _aad(file_id: bytes, counter: int, final: bool) -> bytes:
    return MAGIC + file_id + struct.pack(">Q?", counter, final)


def _nonce(file_id: bytes, counter: int) -> bytes:
    return file_id[:4] + struct.pack(">Q", counter)


class Encryptor:
    """Write plaintext in; encrypted frames go to `out`. Call `close()` to write the last."""

    def __init__(self, key: bytes, out: BinaryIO) -> None:
        self._aes = AESGCM(key)
        self._out = out
        self._file_id = secrets.token_bytes(12)
        self._counter = 0
        self._buffer = bytearray()
        out.write(MAGIC + self._file_id)

    def write(self, data: bytes) -> None:
        self._buffer += data
        while len(self._buffer) > FRAME:
            self._frame(bytes(self._buffer[:FRAME]), final=False)
            del self._buffer[:FRAME]

    def close(self) -> None:
        self._frame(bytes(self._buffer), final=True)
        self._buffer.clear()

    def _frame(self, plaintext: bytes, *, final: bool) -> None:
        sealed = self._aes.encrypt(
            _nonce(self._file_id, self._counter),
            plaintext,
            _aad(self._file_id, self._counter, final),
        )
        self._out.write(struct.pack(">I", len(sealed)) + sealed)
        self._counter += 1


def decrypt_stream(key: bytes, source: BinaryIO) -> Iterator[bytes]:
    """Yield the plaintext frame by frame. Raises BackupCorrupt on any tampering, a wrong key
    or truncation; only a complete, authentic file ends without an error."""
    aes = AESGCM(key)
    header = source.read(len(MAGIC) + 12)
    if len(header) != len(MAGIC) + 12 or not header.startswith(MAGIC):
        raise BackupCorrupt("Not a KnowledgePilot backup file.")
    file_id = header[len(MAGIC) :]
    counter = 0
    while True:
        length_bytes = source.read(4)
        if len(length_bytes) != 4:
            raise BackupCorrupt("The backup is truncated.")
        (length,) = struct.unpack(">I", length_bytes)
        if length > FRAME + 16:
            raise BackupCorrupt("The backup is corrupt.")
        sealed = source.read(length)
        if len(sealed) != length:
            raise BackupCorrupt("The backup is truncated.")
        trailing = source.peek(1)[:1] if hasattr(source, "peek") else None
        final = trailing == b"" if trailing is not None else None
        for candidate in (final,) if final is not None else (False, True):
            try:
                plaintext = aes.decrypt(
                    _nonce(file_id, counter), sealed, _aad(file_id, counter, candidate)
                )
            except Exception:  # noqa: BLE001 - InvalidTag: wrong key or altered data
                continue
            break
        else:
            raise BackupCorrupt("The backup failed authentication (wrong key or altered).")
        yield plaintext
        counter += 1
        if candidate:
            if source.read(1):
                raise BackupCorrupt("Data follows the end of the backup.")
            return


def encrypt_bytes(key: bytes, data: bytes, out: BinaryIO) -> None:
    encryptor = Encryptor(key, out)
    encryptor.write(data)
    encryptor.close()
