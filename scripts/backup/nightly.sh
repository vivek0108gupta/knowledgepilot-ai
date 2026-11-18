#!/usr/bin/env bash
# Nightly backup: database, then document files, each verified before it counts.
# Run from a host or job runner that has the API's environment (DATABASE_URL,
# SUPABASE_URL, SUPABASE_SECRET_KEY), BACKUP_ENCRYPTION_KEY, a PostgreSQL client matching
# the server's major version, and the API code (apps/api). Exits non-zero on any failure,
# so the scheduler can alert. Example crontab (02:15 UTC daily):
#
#   15 2 * * * cd /srv/knowledgepilot/apps/api && BACKUP_DIR=/backups ../../scripts/backup/nightly.sh >> /var/log/kp-backup.log 2>&1
#
# Optional: PG_DUMP / PG_RESTORE command templates (see app/tools/db_backup.py),
# KEEP_DAYS (default 30), KEEP_MIN (default 7), OFFSITE_SYNC (a command run last, for example
# "rclone copy /backups remote:kp-backups --immutable").
set -euo pipefail

: "${BACKUP_DIR:?Set BACKUP_DIR}"
: "${BACKUP_ENCRYPTION_KEY:?Set BACKUP_ENCRYPTION_KEY (never store it in BACKUP_DIR)}"
# The braces in "{database}" would end a ${VAR:-default} expansion early: set defaults apart.
DEFAULT_DUMP='pg_dump --dbname={database}'
DEFAULT_RESTORE='pg_restore --dbname={database}'
PG_DUMP="${PG_DUMP:-$DEFAULT_DUMP}"
PG_RESTORE="${PG_RESTORE:-$DEFAULT_RESTORE}"
umask 077

echo "$(date -u +%FT%TZ) database backup"
uv run python -m app.tools.db_backup create --dir "$BACKUP_DIR/db" --pg-dump "$PG_DUMP"
echo "$(date -u +%FT%TZ) database verify (restore into a scratch database)"
uv run python -m app.tools.db_backup verify --dir "$BACKUP_DIR/db" --pg-restore "$PG_RESTORE"

# Right after the database backup, so every document in it has its file backed up.
echo "$(date -u +%FT%TZ) storage backup"
uv run python -m app.tools.storage_backup create --dir "$BACKUP_DIR/storage"
echo "$(date -u +%FT%TZ) storage verify"
uv run python -m app.tools.storage_backup verify --dir "$BACKUP_DIR/storage"

echo "$(date -u +%FT%TZ) prune database backups (keeps the newest verified ones)"
uv run python -m app.tools.db_backup prune --dir "$BACKUP_DIR/db" \
  --keep-days "${KEEP_DAYS:-30}" --keep-min "${KEEP_MIN:-7}" --apply

if [[ -n "${OFFSITE_SYNC:-}" ]]; then
  echo "$(date -u +%FT%TZ) off-site copy"
  bash -c "$OFFSITE_SYNC"
fi
echo "$(date -u +%FT%TZ) backup complete"
