"""Encrypted, incremental backups of the document files in Supabase Storage.

    python -m app.tools.storage_backup create  --dir /backups/storage
    python -m app.tools.storage_backup verify  --dir /backups/storage [--manifest <file>]
    python -m app.tools.storage_backup restore --dir /backups/storage --manifest <file> [--apply]

Supabase's database backups and PITR do not include Storage objects, and Storage has no
object versioning: a deleted file is gone. This tool is the file half of a backup.

create
    For every document in the database, downloads its file, checks it against the document's
    recorded SHA-256 (so a corrupt or replaced file is reported, not backed up), and stores it
    encrypted under `objects/<sha256>.enc`. Content-addressed and incremental: a file already
    in the backup is not downloaded again. Writes `manifest-<time>.json` (paths, hashes and
    sizes; no names or content) listing what this run covered.

verify
    Decrypts every object a manifest lists and checks its SHA-256. Reports missing or corrupt
    objects. Only a manifest that verifies should be relied on.

restore
    For documents that exist in the database but whose file is missing from the bucket,
    uploads the file from the backup (after checking its hash). Never overwrites or deletes
    anything in the bucket. Reports what it would do unless --apply is given.

Run it where the API runs (it needs DATABASE_URL and SUPABASE_SECRET_KEY), with the backup
key in BACKUP_ENCRYPTION_KEY or --key-file. Coordinate it with database backups: run it right
after `db_backup create`, so the database backup's documents all have their files.
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.tools.backup_crypto import BackupCorrupt, Encryptor, decrypt_stream, load_key


def object_path(directory: Path, sha256: str) -> Path:
    return directory / "objects" / f"{sha256}.enc"


def write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


async def documents() -> list[dict[str, Any]]:
    from app.core.db import SessionLocal
    from app.models.document import Document

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Document.id,
                    Document.storage_path,
                    Document.sha256,
                    Document.size_bytes,
                    Document.mime_type,
                ).order_by(Document.created_at)
            )
        ).all()
    return [
        {
            "document_id": str(row.id),
            "storage_path": row.storage_path,
            "sha256": row.sha256,
            "size_bytes": row.size_bytes,
            "mime_type": row.mime_type,
        }
        for row in rows
    ]


def decrypt_object(key: bytes, path: Path) -> bytes:
    with open(path, "rb") as source:
        return b"".join(decrypt_stream(key, source))


async def create(args: argparse.Namespace) -> int:
    from app.services.storage import StorageError, get_storage

    key = load_key(args.key_file)
    directory = Path(args.dir)
    (directory / "objects").mkdir(parents=True, exist_ok=True)
    storage = get_storage()
    started = time.monotonic()
    entries, problems, downloaded, reused = [], [], 0, 0
    for document in await documents():
        target = object_path(directory, document["sha256"])
        if target.exists():
            reused += 1
            entries.append(document)
            continue
        with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as buffer:
            try:
                await storage.download_to(document["storage_path"], buffer)  # type: ignore[arg-type]
            except StorageError:
                problems.append(
                    {"document_id": document["document_id"], "problem": "download_failed"}
                )
                continue
            buffer.seek(0)
            data = buffer.read()
        if hashlib.sha256(data).hexdigest() != document["sha256"]:
            problems.append({"document_id": document["document_id"], "problem": "hash_mismatch"})
            continue
        partial = target.with_suffix(".partial")
        out = io.BytesIO()
        encryptor = Encryptor(key, out)
        encryptor.write(data)
        encryptor.close()
        write_private(partial, out.getvalue())
        os.replace(partial, target)
        downloaded += 1
        entries.append(document)
    close = getattr(storage, "aclose", None)
    if close:
        await close()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    manifest = {
        "format": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "objects": entries,
        "problems": problems,
        "downloaded": downloaded,
        "already_backed_up": reused,
        "seconds": round(time.monotonic() - started, 2),
        "verified_at": None,
    }
    name = f"manifest-{stamp}.json"
    write_private(directory / name, json.dumps(manifest, indent=2).encode())
    print(json.dumps({k: v for k, v in manifest.items() if k != "objects"} | {"manifest": name}))
    return 1 if problems else 0


def latest_manifest(directory: Path, name: str | None) -> Path:
    if name:
        return directory / name
    manifests = sorted(directory.glob("manifest-*.json"))
    if not manifests:
        raise SystemExit("No storage manifest found.")
    return manifests[-1]


async def verify(args: argparse.Namespace) -> int:
    key = load_key(args.key_file)
    directory = Path(args.dir)
    path = latest_manifest(directory, args.manifest)
    manifest = json.loads(path.read_text())
    bad = []
    for entry in manifest["objects"]:
        target = object_path(directory, entry["sha256"])
        try:
            data = decrypt_object(key, target)
        except FileNotFoundError:
            bad.append({"document_id": entry["document_id"], "problem": "missing"})
            continue
        except BackupCorrupt:
            bad.append({"document_id": entry["document_id"], "problem": "corrupt"})
            continue
        if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["size_bytes"]:
            bad.append({"document_id": entry["document_id"], "problem": "hash_mismatch"})
    ok = not bad and not manifest["problems"]
    if ok:
        manifest["verified_at"] = datetime.now(UTC).isoformat()
        write_private(path, json.dumps(manifest, indent=2).encode())
    print(
        json.dumps(
            {"manifest": path.name, "ok": ok, "objects": len(manifest["objects"]), "bad": bad}
        )
    )
    return 0 if ok else 1


async def restore(args: argparse.Namespace) -> int:
    from app.services.storage import get_storage

    key = load_key(args.key_file)
    directory = Path(args.dir)
    manifest = json.loads(latest_manifest(directory, args.manifest).read_text())
    by_hash = {entry["sha256"]: entry for entry in manifest["objects"]}
    storage = get_storage()
    present = {obj.path for obj in await storage.list_objects()}
    actions = []
    for document in await documents():
        if document["storage_path"] in present:
            continue
        if document["sha256"] not in by_hash:
            actions.append({"document_id": document["document_id"], "action": "not_in_backup"})
            continue
        data = decrypt_object(key, object_path(directory, document["sha256"]))
        if hashlib.sha256(data).hexdigest() != document["sha256"]:
            actions.append({"document_id": document["document_id"], "action": "backup_corrupt"})
            continue
        if args.apply:
            # Never overwrites: the upload refuses an existing path.
            await storage.upload(
                document["storage_path"], io.BytesIO(data), document["mime_type"], len(data)
            )
        actions.append(
            {
                "document_id": document["document_id"],
                "action": "restored" if args.apply else "would_restore",
            }
        )
    close = getattr(storage, "aclose", None)
    if close:
        await close()
    print(json.dumps({"applied": args.apply, "actions": actions}))
    return 1 if any(a["action"] in ("not_in_backup", "backup_corrupt") for a in actions) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Encrypted backups of document files")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("create", "verify", "restore"):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True)
        p.add_argument("--key-file")
        if name != "create":
            p.add_argument("--manifest")
    sub.choices["restore"].add_argument("--apply", action="store_true")
    args = parser.parse_args()
    action = {"create": create, "verify": verify, "restore": restore}[args.action]
    return asyncio.run(action(args))


if __name__ == "__main__":
    sys.exit(main())
