# Phase 5.7: backup and recovery strategy

KnowledgePilot keeps its data in two places that must be backed up **together**:

- **PostgreSQL** holds the workspaces, members, documents' metadata, chunks and vectors,
  conversations, usage ledger and audit log.
- **Supabase Storage** holds the original uploaded files. **Supabase database backups and
  PITR do not include Storage objects** (they hold only the metadata), and Storage has no
  object versioning: a deleted file is gone for good *(Supabase docs, checked 2026-11-12)*.

Everything below marked *(measured)* was run on 2026-11-12 against the local development
database and the real Supabase bucket, read-only. The only things written were the backup
files in a scratch directory and temporary scratch databases, which the tools dropped.

## Tools

| Tool | Does |
|---|---|
| `python -m app.tools.db_backup create` | `pg_dump` from an exported snapshot, with row counts from **the same snapshot**. Encrypted while it streams; the plaintext never touches disk. A manifest records the time, server version, schema version, every table's count, the vector count and checksums. |
| `python -m app.tools.db_backup verify` | Authenticates and decrypts the dump, restores it into a new `kp_verify_<time>` database, and compares the schema version, every table's row count and the vector count with the manifest. Then drops that scratch database (the only database the tool ever creates or drops). Marks the manifest `verified_at` only if everything matches. |
| `python -m app.tools.db_backup decrypt` | Streams a backup's plaintext to stdout for `pg_restore`, authenticating it as it goes. Refuses to write to a terminal. |
| `python -m app.tools.db_backup prune` | Removes backups older than `--keep-days`, but always keeps the newest `--keep-min` **verified** ones. A dry run unless `--apply`. Only touches files with this tool's name pattern and a manifest. |
| `python -m app.tools.storage_backup create / verify / restore` | Content-addressed, incremental, encrypted copies of every document's file. Each file is checked against the SHA-256 recorded at upload, so a corrupt or replaced file is reported, not backed up. `restore` uploads only files that are **missing** from the bucket, never overwrites, and is a dry run unless `--apply`. |
| `scripts/backup/nightly.sh` | Runs: database create, database verify, storage create, storage verify, prune, then an optional off-site copy. Exits non-zero on any failure. |

### Encryption

- **Cipher:** AES-256-GCM in authenticated 1 MiB frames (`app/tools/backup_crypto.py`).
- **Tamper evidence:** the nonce and associated data bind each frame's position and whether
  it is the last one. So any altered, reordered, dropped or appended byte, a truncated file,
  or the wrong key fails decryption.
- **Tested:** 18 unit and integration tests. *(measured)* A real backup with one flipped
  byte, and with its manifest checksum forged to match, was refused ("failed
  authentication"); so was the right file with a wrong key.
- **The key:**
  - 32 random bytes, base64 (`python -m app.tools.db_backup keygen --key-file …`, mode
    600, never overwrites);
  - supplied as `BACKUP_ENCRYPTION_KEY`;
  - kept in the secret store, **never with the backups**;
  - **losing it makes every backup unreadable**, so keep two copies in separate secret
    stores.
- **File permissions:** backup files and manifests are created mode 600. Manifests hold no
  data, only counts, sizes and checksums. Storage manifests list storage paths, which contain
  only IDs and the sanitised file name.

## Schedule and retention

| What | When | Kept | Where |
|---|---|---|---|
| Managed database backups (if the provider has them: Supabase Pro daily, 7 to 30 days; PITR add-on, about 2-minute RPO) | Provider | Provider plan | Provider |
| `nightly.sh`: database dump plus file backup, both verified | Daily, 02:15 UTC | 30 days, never fewer than the newest 7 verified | Backup volume, plus an off-site copy (`OFFSITE_SYNC`, for example rclone to another provider's bucket with object lock or immutability) |
| Manual `db_backup create` plus `verify` | **Before every migration** | Until the next nightly backup is verified | As above |
| Restore drill | Monthly, and after any change to the backup process | | Into a scratch database |

Use the managed backups where the database provider offers them. Run `nightly.sh` as well:
it is the only backup of the Storage files, and the only one that is checked by a restore.

## Measured *(2026-11-12, local data: 15 tables, 35 to 40 rows, 6 vectors, 2 files)*

| Step | Result |
|---|---|
| Database backup | 108 KB dump in 0.34 s, encrypted, 41 bytes of overhead |
| Database verify | Restored into a scratch database in 0.26 s. Schema `0008`, all 15 tables' counts and all 6 vectors identical. Scratch database dropped (none left, checked). |
| Storage backup | 2 of 2 files downloaded, hash-checked and encrypted in 2.0 s. A second run: 0 downloaded, 2 already backed up (0.3 s, incremental). |
| Storage verify | 2 of 2 objects decrypt and match their hashes |
| Storage restore (dry run) | 0 actions: nothing missing from the bucket |
| `nightly.sh` end to end | All steps passed, exit 0 |
| **Restore drill** | Decrypt and restore into a new database: 0.54 s. An API pointed at it was `/health/ready` 200 2.6 s later. `alembic current` was `0008 (head)`, with 2 documents. **3.1 s in total.** The drill database was dropped. |
| Tampered file, wrong key | Both refused |
| Storage restore with `--apply` | Tested with the fake bucket: a deleted file is restored byte-for-byte, the others untouched. A file whose content no longer matches its hash is not backed up and fails verification. |

## RTO and RPO

| | Target | Basis |
|---|---|---|
| **RPO, database** | 24 h with nightly dumps only. About 2 minutes with the provider's PITR. | Nightly schedule; Supabase PITR WAL interval *(docs)* |
| **RPO, files** | 24 h (nightly). A file uploaded and lost before the next run is not recoverable. | No Storage versioning *(docs)* |
| **RTO** | **1 hour** for a full restore of a beta-sized database (under 1 GB) | Measured: 3 s for this data, almost all of it API start-up. Restore time grows roughly with the dump size; at production size, measure it with the monthly drill and update this number. The rest of the hour is provisioning a database, changing configuration and checking. |

These targets suit a **private beta**. For a public launch, enable PITR (or the provider's
equivalent) and run the storage backup more often than daily (it is incremental, so hourly is
cheap), or replicate the bucket.

## Restore procedures

### A. Whole database lost or corrupted

1. Stop the API replicas (or put them in maintenance), so nothing writes to a half-restored
   database.
2. Create an empty database with the `vector` extension available.
3. Pick the newest backup whose manifest has `verified_at` set. Restore it, without
   decrypting to disk:
   ```bash
   python -m app.tools.db_backup decrypt --file /backups/db/knowledgepilot-<time>.dump.enc \
     | pg_restore --no-owner --no-privileges --exit-on-error -d "$NEW_DATABASE_URL_SYNC"
   ```
   Or restore into a scratch database first with `db_backup verify`, which also proves the
   file is good.
4. Point **one** API replica at it (`DATABASE_URL`). Check:
   - `/health/ready` returns 200;
   - `alembic current` shows the expected head (the replica applies newer migrations on
     start if the code is newer);
   - the row counts match the manifest.
5. Reconcile the files (procedure C), then bring the other replicas back.
6. Tell users that changes after the backup time were lost: documents uploaded, messages and
   usage since then.

### B. Files lost (bucket emptied, files deleted)

```bash
python -m app.tools.storage_backup restore --dir /backups/storage            # dry run
python -m app.tools.storage_backup restore --dir /backups/storage --apply
python -m app.tools.storage_audit                                            # confirm
```

Documents whose file is reported `not_in_backup` were uploaded after the last storage
backup. Their chunks are still searchable, but the original cannot be downloaded. Tell the
owners to upload them again.

### C. Database and storage out of step (after any restore)

| Situation | Found by | Action |
|---|---|---|
| A file with no document (the database was restored to before the upload, or a cleanup failed) | `storage_audit` "orphans" | `storage_audit --delete` (it re-checks each candidate against the database, and skips files under an hour old) |
| A document whose file is missing (files lost, or restored from an older storage backup) | `storage_audit` "missing" | `storage_backup restore --apply`. If it is not in any backup, ask the owner to upload it again. |
| Documents stuck `processing` after a restore | `ingestion_jobs` | Nothing: on start-up the API re-queues interrupted documents, and job leases expire |
| Open usage reservations from before the restore | `usage_reservations_overdue` | Nothing: the sweep expires them as `abandoned` |

## What is not covered (remaining risks)

- **The backup schedule is not running anywhere yet.** `nightly.sh` is written and tested,
  but no scheduler, off-site target or alerting is set up. That needs the production host.
- **Restore time at production size is unknown.** The measurements use tiny local data.
- **The local machine's `pg_dump` is version 14 while the server is 17.** The rehearsal used
  the database container's own client. Production backups need a version-17 client (the
  `postgres:17` image works).
- **Losing the key loses every backup.** Keep it in two separate secret stores, and test a
  restore with the stored copy.
- **Backups contain every tenant's data.** Restrict who can read the backup storage, and
  log that access.
- **Deleting a workspace removes its data from the live system, but not from backups**
  until they age out (30 days). State this in the privacy policy.
