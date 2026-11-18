"""Phase 5.7: backup encryption. A backup decrypts only with its key and only if every byte
is as written; truncation, reordering and tampering are detected. Storage backups restore
only missing files."""

# `library` is a fixture shared with test_chat; tests take it as an argument.
# ruff: noqa: F811

import base64
import io
import os
import secrets

import pytest

from app.tools import db_backup
from app.tools.backup_crypto import (
    FRAME,
    BackupCorrupt,
    BackupKeyError,
    Encryptor,
    decrypt_stream,
    encrypt_bytes,
    load_key,
    new_key_file,
)
from tests.test_chat import library  # noqa: F401

KEY = secrets.token_bytes(32)


def sealed(data: bytes, key: bytes = KEY) -> bytes:
    out = io.BytesIO()
    encrypt_bytes(key, data, out)
    return out.getvalue()


def opened(blob: bytes, key: bytes = KEY) -> bytes:
    return b"".join(decrypt_stream(key, io.BufferedReader(io.BytesIO(blob))))


@pytest.mark.parametrize("size", [0, 1, FRAME - 1, FRAME, FRAME + 1, 3 * FRAME + 17])
def test_round_trip_at_frame_boundaries(size: int) -> None:
    data = os.urandom(size)
    blob = sealed(data)
    if size >= 16:  # a shorter random input can occur in any ciphertext by chance
        assert data not in blob
    assert opened(blob) == data


def test_streaming_writes_match_one_write() -> None:
    data = os.urandom(2 * FRAME + 5)
    out = io.BytesIO()
    encryptor = Encryptor(KEY, out)
    for start in range(0, len(data), 333_333):
        encryptor.write(data[start : start + 333_333])
    encryptor.close()
    assert opened(out.getvalue()) == data


def test_a_wrong_key_is_refused() -> None:
    with pytest.raises(BackupCorrupt, match="authentication"):
        opened(sealed(b"tenant data"), secrets.token_bytes(32))


@pytest.mark.parametrize("position", [30, -1, -20])
def test_any_altered_byte_is_detected(position: int) -> None:
    blob = bytearray(sealed(os.urandom(FRAME + 100)))
    blob[position] ^= 1
    with pytest.raises(BackupCorrupt):
        opened(bytes(blob))


def test_truncation_at_a_frame_boundary_is_detected() -> None:
    data = os.urandom(2 * FRAME + 10)
    blob = sealed(data)
    first_frame_end = 9 + 12 + 4 + FRAME + 16
    with pytest.raises(BackupCorrupt):
        opened(blob[: first_frame_end + 4 + FRAME + 16])  # drops the final frame
    with pytest.raises(BackupCorrupt):
        opened(blob[:-5])


def test_appended_data_is_detected() -> None:
    with pytest.raises(BackupCorrupt):
        opened(sealed(b"x") + b"extra")


def test_not_a_backup() -> None:
    with pytest.raises(BackupCorrupt, match="Not a KnowledgePilot backup"):
        opened(b"PGDMP plain dump")


def test_keys_are_validated_and_new_key_files_are_private(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backup.key"
    new_key_file(str(path))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert len(load_key(str(path))) == 32
    with pytest.raises(FileExistsError):
        new_key_file(str(path))  # never overwrites a key
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(BackupKeyError):
        load_key()
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "not base64 !")
    with pytest.raises(BackupKeyError):
        load_key()


def test_prune_keeps_the_newest_verified_backups_and_deletes_nothing_by_default(
    tmp_path, capsys: pytest.CaptureFixture
) -> None:
    import argparse
    import json

    names = []
    for stamp, verified in (
        ("20200101T000000Z", True),
        ("20200102T000000Z", False),
        ("20200103T000000Z", True),
    ):
        name = f"knowledgepilot-{stamp}.dump.enc"
        (tmp_path / name).write_bytes(b"x")
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"verified_at": "t" if verified else None})
        )
        names.append(name)
    (tmp_path / "someone-elses.dump").write_bytes(b"keep me")

    args = argparse.Namespace(dir=str(tmp_path), keep_days=30, keep_min=1, apply=False)
    db_backup.prune(args)
    assert len(list(tmp_path.iterdir())) == 7  # dry run
    assert "Would delete knowledgepilot-20200101" in capsys.readouterr().out

    db_backup.prune(argparse.Namespace(**{**vars(args), "apply": True}))
    left = sorted(p.name for p in tmp_path.iterdir())
    assert names[2] in left and names[0] not in left and names[1] not in left
    assert "someone-elses.dump" in left


class TestStorageBackup:
    """Backs up the fake bucket's files, loses one, and restores only that one."""

    async def test_back_up_verify_and_restore_only_missing_files(
        self,
        db_client,  # noqa: ANN001
        library: dict,
        storage,  # noqa: ANN001
        session_factory,  # noqa: ANN001
        tmp_path,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import argparse
        import json

        import app.core.db as db_module
        import app.services.storage as storage_module
        from app.tools import storage_backup

        monkeypatch.setattr(db_module, "SessionLocal", session_factory)
        monkeypatch.setattr(storage_module, "get_storage", lambda: storage)
        monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(KEY).decode())
        args = argparse.Namespace(dir=str(tmp_path), key_file=None, manifest=None, apply=False)
        original = dict(storage.objects)
        assert len(original) == 3

        assert await storage_backup.create(args) == 0
        assert await storage_backup.verify(args) == 0
        for path in (tmp_path / "objects").iterdir():
            assert oct(path.stat().st_mode & 0o777) == "0o600"
            assert not any(content in path.read_bytes() for content in original.values())

        lost = sorted(original)[0]
        del storage.objects[lost]
        capsys.readouterr()
        assert await storage_backup.restore(args) == 0  # dry run
        assert lost not in storage.objects
        assert json.loads(capsys.readouterr().out)["actions"][0]["action"] == "would_restore"

        assert (
            await storage_backup.restore(argparse.Namespace(**{**vars(args), "apply": True})) == 0
        )
        assert storage.objects == original  # restored exactly; the others untouched

    async def test_a_file_that_does_not_match_its_hash_is_not_backed_up(
        self,
        db_client,  # noqa: ANN001
        library: dict,
        storage,  # noqa: ANN001
        session_factory,  # noqa: ANN001
        tmp_path,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import argparse

        import app.core.db as db_module
        import app.services.storage as storage_module
        from app.tools import storage_backup

        monkeypatch.setattr(db_module, "SessionLocal", session_factory)
        monkeypatch.setattr(storage_module, "get_storage", lambda: storage)
        monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(KEY).decode())
        storage.objects[sorted(storage.objects)[0]] = b"replaced"
        args = argparse.Namespace(dir=str(tmp_path), key_file=None, manifest=None, apply=False)

        assert await storage_backup.create(args) == 1
        assert await storage_backup.verify(args) == 1  # a manifest with problems never verifies
