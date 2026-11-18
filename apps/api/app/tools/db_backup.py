"""Encrypted PostgreSQL backups, verified by restoring them.

    python -m app.tools.db_backup keygen  --key-file /secure/backup.key
    python -m app.tools.db_backup create  --dir /backups
    python -m app.tools.db_backup verify  --dir /backups [--file <name>.dump.enc]
    python -m app.tools.db_backup prune   --dir /backups --keep-days 30 --keep-min 7 [--apply]
    python -m app.tools.db_backup decrypt --file <name>.dump.enc | pg_restore -d <new database>

create
    Opens a read-only REPEATABLE READ transaction, exports its snapshot, counts every table's
    rows (and stored vectors) in that snapshot, and runs `pg_dump --snapshot` from the same
    snapshot, so the counts describe exactly what the dump holds. The custom-format dump is
    encrypted as it streams (see backup_crypto) and never touches disk unencrypted. A manifest
    (`<name>.json`: time, schema version, counts, sizes, checksum; no data) is written beside it.

verify
    Decrypts (authenticating every byte), restores into a new scratch database named
    `kp_verify_<timestamp>`, and checks that the schema version, every table's row count and
    the vector count match the manifest. Then drops the scratch database: the only database
    this tool ever creates or drops. Prints the restore time (an RTO measurement).
    A backup is only reported good after this succeeds.

prune
    Lists backups older than --keep-days, always keeping the newest --keep-min verified ones.
    Deletes nothing without --apply, and only files this tool wrote (name pattern + manifest).

The PostgreSQL client must match the server's major version. `--pg-dump` / `--pg-restore`
are command templates; `{database}` is replaced by the database name. Connection details for
the default commands come from DATABASE_URL through libpq environment variables (never the
command line). Example with the Compose database's own client (socket, no password):

    --pg-dump "docker compose exec -T db pg_dump -U knowledgepilot -d {database}"
    --pg-restore "docker compose exec -T db pg_restore -U knowledgepilot -d {database}"
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.tools.backup_crypto import BackupCorrupt, Encryptor, decrypt_stream, load_key, new_key_file

NAME = re.compile(r"^knowledgepilot-(\d{8}T\d{6}Z)\.dump\.enc$")
SCRATCH_PREFIX = "kp_verify_"


def connection() -> dict[str, Any]:
    url = make_url(get_settings().database_url)
    return {
        "host": url.host,
        "port": url.port or 5432,
        "user": url.username,
        "password": url.password,
        "database": url.database,
    }


def libpq_env(conn: dict[str, Any]) -> dict[str, str]:
    env = {**os.environ}
    for key, value in (
        ("PGHOST", conn["host"]),
        ("PGPORT", conn["port"]),
        ("PGUSER", conn["user"]),
        ("PGPASSWORD", conn["password"]),
    ):
        if value is not None:
            env[key] = str(value)
    return env


def command(template: str, database: str, extra: list[str]) -> list[str]:
    return [part.replace("{database}", database) for part in shlex.split(template)] + extra


async def snapshot_counts(db: asyncpg.Connection) -> dict[str, Any]:
    tables = [
        row["tablename"]
        for row in await db.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
    ]
    counts = {}
    for table in tables:
        quoted = table.replace('"', '""')
        counts[table] = await db.fetchval(f'SELECT count(*) FROM public."{quoted}"')
    version = (
        await db.fetchval("SELECT version_num FROM alembic_version")
        if "alembic_version" in tables
        else None
    )
    vectors = (
        await db.fetchval("SELECT count(embedding) FROM document_chunks")
        if "document_chunks" in tables
        else None
    )
    return {"tables": counts, "alembic_version": version, "vectors": vectors}


async def create(args: argparse.Namespace) -> int:
    key = load_key(args.key_file)
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"knowledgepilot-{stamp}.dump.enc"
    target, partial = directory / name, directory / f".{name}.partial"
    conn = connection()
    started = time.monotonic()
    db = await asyncpg.connect(**conn, timeout=10)
    try:
        tx = db.transaction(isolation="repeatable_read", readonly=True)
        await tx.start()
        snapshot = await db.fetchval("SELECT pg_export_snapshot()")
        counts = await snapshot_counts(db)
        server = await db.fetchval("SHOW server_version")
        dump = command(
            args.pg_dump,
            conn["database"],
            ["--format=custom", "--no-owner", "--no-privileges", f"--snapshot={snapshot}"],
        )
        plain_hash, plain_size = hashlib.sha256(), 0
        # Owner-only, like the key: the size and existence of backups are still information.
        with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            encryptor = Encryptor(key, out)
            process = subprocess.Popen(  # noqa: S603 - an operator-supplied command template
                dump, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=libpq_env(conn)
            )
            assert process.stdout is not None
            while chunk := await asyncio.to_thread(process.stdout.read, 1024 * 1024):
                plain_hash.update(chunk)
                plain_size += len(chunk)
                encryptor.write(chunk)
            stderr = (await asyncio.to_thread(process.stderr.read)).decode(errors="replace")  # type: ignore[union-attr]
            code = await asyncio.to_thread(process.wait)
            if code != 0 or plain_size == 0:
                raise RuntimeError(f"pg_dump failed ({code}): {stderr.strip()[-500:]}")
            encryptor.close()
            out.flush()
            os.fsync(out.fileno())
        await tx.rollback()
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        raise
    finally:
        await db.close()
    os.replace(partial, target)
    encrypted_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "format": 1,
        "file": name,
        "created_at": datetime.now(UTC).isoformat(),
        "database": conn["database"],
        "server_version": server,
        "dump_bytes": plain_size,
        "dump_sha256": plain_hash.hexdigest(),
        "encrypted_bytes": target.stat().st_size,
        "encrypted_sha256": encrypted_hash,
        "seconds": round(time.monotonic() - started, 2),
        **counts,
        "verified_at": None,
    }
    manifest_file = directory / f"{name}.json"
    manifest_file.write_text(json.dumps(manifest, indent=2))
    manifest_file.chmod(0o600)
    print(json.dumps({"created": name, "dump_bytes": plain_size, "seconds": manifest["seconds"]}))
    print("Not yet verified: run `verify` before relying on it.", file=sys.stderr)
    return 0


async def verify(args: argparse.Namespace) -> int:
    key = load_key(args.key_file)
    directory = Path(args.dir)
    if args.file:
        target = directory / args.file
    else:
        candidates = sorted(p for p in directory.iterdir() if NAME.match(p.name))
        if not candidates:
            print("No backups found.", file=sys.stderr)
            return 1
        target = candidates[-1]
    manifest_path = directory / f"{target.name}.json"
    manifest = json.loads(manifest_path.read_text())
    report: dict[str, Any] = {"file": target.name, "ok": False}

    if hashlib.sha256(target.read_bytes()).hexdigest() != manifest["encrypted_sha256"]:
        report["error"] = "checksum mismatch: the file changed since it was written"
        print(json.dumps(report))
        return 1

    conn = connection()
    scratch = SCRATCH_PREFIX + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    admin = await asyncpg.connect(**{**conn, "database": args.maintenance_db}, timeout=10)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="kp-verify-") as work:
            plain = Path(work) / "restore.dump"
            digest = hashlib.sha256()
            descriptor = os.open(plain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as out, open(target, "rb") as source:
                for chunk in decrypt_stream(key, source):
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != manifest["dump_sha256"]:
                raise BackupCorrupt("The decrypted dump does not match the manifest.")
            decrypted = time.monotonic()

            await admin.execute(f'CREATE DATABASE "{scratch}"')
            restore = command(
                args.pg_restore, scratch, ["--no-owner", "--no-privileges", "--exit-on-error"]
            )
            with open(plain, "rb") as dump_file:
                result = await asyncio.to_thread(
                    subprocess.run,  # noqa: S603 - an operator-supplied command template
                    restore,
                    stdin=dump_file,
                    capture_output=True,
                    env=libpq_env(conn),
                )
            if result.returncode != 0:
                raise RuntimeError(
                    f"pg_restore failed: {result.stderr.decode(errors='replace')[-500:]}"
                )
            restored = time.monotonic()
        check = await asyncpg.connect(**{**conn, "database": scratch}, timeout=10)
        try:
            found = await snapshot_counts(check)
        finally:
            await check.close()
        mismatches = {
            table: {"expected": expected, "restored": found["tables"].get(table)}
            for table, expected in manifest["tables"].items()
            if found["tables"].get(table) != expected
        }
        report.update(
            {
                "alembic_version": found["alembic_version"],
                "tables": len(found["tables"]),
                "rows": sum(found["tables"].values()),
                "vectors": found["vectors"],
                "mismatches": mismatches,
                "decrypt_seconds": round(decrypted - started, 2),
                "restore_seconds": round(restored - decrypted, 2),
                "total_seconds": round(time.monotonic() - started, 2),
            }
        )
        report["ok"] = (
            not mismatches
            and found["alembic_version"] == manifest["alembic_version"]
            and found["vectors"] == manifest["vectors"]
        )
    except (BackupCorrupt, RuntimeError, asyncpg.PostgresError) as error:
        report["error"] = str(error)
    finally:
        # Only the scratch database this run created.
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        await admin.close()
    if report["ok"]:
        manifest["verified_at"] = datetime.now(UTC).isoformat()
        manifest["verify"] = {k: report[k] for k in ("restore_seconds", "total_seconds")}
        manifest_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(report))
    return 0 if report["ok"] else 1


def prune(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    backups = []
    for path in sorted(directory.iterdir()):
        match = NAME.match(path.name)
        manifest = directory / f"{path.name}.json"
        if not match or not manifest.exists():
            continue
        created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        verified = json.loads(manifest.read_text()).get("verified_at") is not None
        backups.append((created, path, manifest, verified))
    # The newest verified backups are never pruned, however old.
    newest_verified = [p for _, p, _, verified in sorted(backups, reverse=True) if verified]
    keep = set(newest_verified[: args.keep_min])
    cutoff = datetime.now(UTC) - timedelta(days=args.keep_days)
    doomed = [(p, m) for created, p, m, _ in backups if created < cutoff and p not in keep]
    for path, manifest in doomed:
        print(("Deleting " if args.apply else "Would delete ") + path.name)
        if args.apply:
            path.unlink()
            manifest.unlink()
    if not doomed:
        print("Nothing to prune.")
    return 0


def decrypt(args: argparse.Namespace) -> int:
    """Stream the decrypted dump to stdout, for `| pg_restore`. Fails, with nothing usable
    written, if the file is altered or the key is wrong (pg_restore then sees a short dump
    and stops)."""
    key = load_key(args.key_file)
    out = sys.stdout.buffer
    if out.isatty():
        print("Refusing to write a database dump to a terminal; pipe it.", file=sys.stderr)
        return 1
    with open(args.file, "rb") as source:
        for chunk in decrypt_stream(key, source):
            out.write(chunk)
    out.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Encrypted, verified PostgreSQL backups")
    sub = parser.add_subparsers(dest="action", required=True)
    decrypt_parser = sub.add_parser("decrypt")
    decrypt_parser.add_argument("--file", required=True)
    decrypt_parser.add_argument("--key-file")
    keygen = sub.add_parser("keygen")
    keygen.add_argument("--key-file", required=True)
    for name in ("create", "verify", "prune"):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True)
        p.add_argument("--key-file")
    sub.choices["create"].add_argument("--pg-dump", default="pg_dump --dbname={database}")
    sub.choices["verify"].add_argument("--pg-restore", default="pg_restore --dbname={database}")
    sub.choices["verify"].add_argument("--file")
    sub.choices["verify"].add_argument("--maintenance-db", default="postgres")
    sub.choices["prune"].add_argument("--keep-days", type=int, default=30)
    sub.choices["prune"].add_argument("--keep-min", type=int, default=7)
    sub.choices["prune"].add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "keygen":
        new_key_file(args.key_file)
        print(f"Wrote a new key to {args.key_file} (mode 600). Store it in your secret store.")
        return 0
    if args.action == "decrypt":
        return decrypt(args)
    if args.action == "prune":
        if args.keep_min < 1:
            parser.error("--keep-min must be at least 1")
        return prune(args)
    return asyncio.run(create(args) if args.action == "create" else verify(args))


if __name__ == "__main__":
    sys.exit(main())
