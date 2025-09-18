"""One-off migration script to push existing locally stored receipt files into Azure Blob Storage.

Usage (after configuring environment variables and ensuring DefaultAzureCredential works):

  python -m app.scripts.migrate_local_uploads_to_blob

Behavior:
  - Scans receipts table for rows whose stored_path is an absolute local path
  - Uploads file bytes to blob container (name = existing uuid filename if looks like uuid, else new uuid)
  - Updates stored_path column to the blob name
  - Skips rows where file missing (logs warning)

Environment Variables Required:
  AZURE_STORAGE_ACCOUNT_NAME
  (optional) AZURE_STORAGE_CONTAINER_NAME (default receipts)

Safety:
  - Does NOT delete local files after upload (you may remove them manually once satisfied)
  - Idempotent: if stored_path already looks like a blob name (no path separators, length >= 30) it is skipped
"""
from __future__ import annotations
import os, uuid, logging
from sqlalchemy import text
from ..db.database import SessionLocal
from ..services import blob_storage

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger("migrate_uploads")

def is_blob_like(path: str) -> bool:
    if not path:
        return False
    if os.path.isabs(path):
        return False
    if '/' in path or '\\' in path:
        return False
    return len(path) >= 30  # uuid32 + ext

def main():
    db = SessionLocal()
    rows = db.execute(text("SELECT id, original_filename, stored_path, content_type FROM receipts ORDER BY id"))
    updated = 0
    skipped = 0
    for r in rows.mappings():
        rid = r['id']
        stored_path = r['stored_path']
        if is_blob_like(stored_path):
            skipped += 1
            continue
        if not os.path.isfile(stored_path):
            logger.warning("Receipt %s local file missing: %s", rid, stored_path)
            continue
        # derive extension
        ext = os.path.splitext(stored_path)[1]
        blob_name = f"{uuid.uuid4().hex}{ext}" if ext else uuid.uuid4().hex
        with open(stored_path, 'rb') as fh:
            data = fh.read()
        blob_storage.upload_bytes(data, blob_name, r.get('content_type'))
        db.execute(text("UPDATE receipts SET stored_path = :p WHERE id = :id"), {"p": blob_name, "id": rid})
        updated += 1
        if updated % 25 == 0:
            db.commit()
            logger.info("Committed %s updates so far...", updated)
    db.commit()
    logger.info("Migration complete. Updated=%s skipped_already_blob=%s", updated, skipped)

if __name__ == "__main__":
    main()
