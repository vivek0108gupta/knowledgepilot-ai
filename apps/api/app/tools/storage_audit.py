"""Find (and optionally remove) storage objects no document points at, and documents whose
file is missing.

    python -m app.tools.storage_audit                     # report only (the default)
    python -m app.tools.storage_audit --delete            # also remove orphans older than 1 h
    python -m app.tools.storage_audit --delete --min-age-minutes 0 --prefix <workspace-id>/

How orphans arise: a workspace deleted while storage refused the file deletions; an upload
stored whose database commit failed and whose clean-up also failed; a process stopped between
storing a file and committing its document. A missing file usually means storage lost it or
it was removed by hand.

Safety:
- Report only unless `--delete` is given.
- Objects younger than `--min-age-minutes` (60) are never deleted: an upload in flight stores
  its file before committing its document.
- Just before deleting, every candidate is checked again against the database, so a document
  created meanwhile keeps its file.
- `--prefix` limits the audit to part of the bucket, for example one workspace.
- If the bucket cannot be listed completely, nothing is deleted.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.document import Document
from app.services.storage import Storage, StorageError

logger = logging.getLogger(__name__)


@dataclass
class Report:
    objects: int = 0
    referenced: int = 0
    orphans: list[str] = field(default_factory=list)
    too_recent: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)  # document IDs
    deleted: list[str] = field(default_factory=list)


async def referenced_paths(session: AsyncSession, paths: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(paths), 1000):
        batch = paths[start : start + 1000]
        found.update(
            await session.scalars(
                select(Document.storage_path).where(Document.storage_path.in_(batch))
            )
        )
    return found


async def audit(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    prefix: str = "",
    min_age: timedelta = timedelta(hours=1),
    delete: bool = False,
    now: datetime | None = None,
) -> Report:
    now = now or datetime.now(UTC)
    report = Report()
    objects = await storage.list_objects(prefix)  # raises if incomplete: nothing deleted
    report.objects = len(objects)
    async with session_factory() as session:
        known = await referenced_paths(session, [item.path for item in objects])
        stored = {item.path for item in objects}
        documents = (
            await session.execute(
                select(Document.id, Document.storage_path).where(
                    Document.storage_path.startswith(prefix)
                )
            )
        ).all()
    report.referenced = len(known)
    report.missing_files = sorted(str(doc_id) for doc_id, path in documents if path not in stored)
    for item in objects:
        if item.path in known:
            continue
        old_enough = item.created_at is not None and now - item.created_at >= min_age
        (report.orphans if old_enough else report.too_recent).append(item.path)
    if delete and report.orphans:
        async with session_factory() as session:
            # Checked again just before deleting: a document committed meanwhile keeps its file.
            still_referenced = await referenced_paths(session, report.orphans)
        doomed = [path for path in report.orphans if path not in still_referenced]
        if doomed:
            await storage.delete(doomed)
            report.deleted = doomed
            logger.warning("Storage audit removed %d orphaned object(s)", len(doomed))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--delete", action="store_true", help="remove orphaned objects")
    parser.add_argument("--min-age-minutes", type=int, default=60)
    parser.add_argument("--prefix", default="", help="audit only objects under this path")
    args = parser.parse_args(argv)
    if args.min_age_minutes < 0:
        parser.error("--min-age-minutes cannot be negative")

    from app.core.db import SessionLocal, engine
    from app.core.logging import configure_logging
    from app.services.storage import get_storage

    configure_logging("WARNING")

    async def run() -> Report:
        try:
            return await audit(
                SessionLocal,
                get_storage(),
                prefix=args.prefix,
                min_age=timedelta(minutes=args.min_age_minutes),
                delete=args.delete,
            )
        finally:
            await engine.dispose()

    try:
        report = asyncio.run(run())
    except StorageError:
        print("Storage could not be listed completely; nothing was changed.", file=sys.stderr)
        return 2
    print(f"objects: {report.objects}, referenced by documents: {report.referenced}")
    print(f"orphaned (older than {args.min_age_minutes} min): {len(report.orphans)}")
    for path in report.orphans:
        print(f"  orphan  {path}")
    print(f"unreferenced but too recent to judge: {len(report.too_recent)}")
    print(f"documents whose file is missing: {len(report.missing_files)}")
    for document_id in report.missing_files:
        print(f"  missing {document_id}")
    print(
        f"deleted: {len(report.deleted)}"
        if args.delete
        else "report only: nothing deleted (use --delete)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
