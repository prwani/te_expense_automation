from fastapi import APIRouter, UploadFile, File, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import os, uuid, shutil
from typing import List
from ..db.database import SessionLocal
from ..services.extraction import extract_from_receipts
from ..services.matching import propose_matches
from ..services import blob_storage
from ..services.receipt_loader import load_receipt_bytes
from io import BytesIO

USE_BLOB = True  # feature flag; if env lacks config will fallback to local

router = APIRouter(prefix="/receipts", tags=["receipts"])

UPLOAD_DIR = os.environ.get("RECEIPT_UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "../../uploads"))
# Ensure upload directory exists even on first boot in App Service
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except Exception:
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/upload")
async def upload_receipts(
    files: List[UploadFile] = File(...),
    provider: str = Query(
        "document_intelligence",
        description="Extraction provider: document_intelligence | content_understanding | gpt5_nano | fallback"
    ),
    db: Session = Depends(get_db)
):
    stored_files = []
    for uf in files:
        original_name = uf.filename or "uploaded"
        ext = os.path.splitext(original_name)[1]
        blob_name = f"{uuid.uuid4().hex}{ext}"
        content_type = uf.content_type
        data = await uf.read()
        stored_path = None
        try:
            if USE_BLOB:
                try:
                    blob_storage.upload_bytes(data, blob_name, content_type)
                    stored_path = blob_name  # reuse stored_path column to hold blob name
                except Exception as e:
                    # Fallback to local storage if blob upload fails
                    stored_path = None
                    raise
        finally:
            if stored_path is None:
                # fallback local save
                dest_path = os.path.join(UPLOAD_DIR, blob_name)
                with open(dest_path, 'wb') as out:
                    out.write(data)
                stored_path = dest_path
        stored_files.append({
            "original_filename": original_name,
            "stored_path": stored_path,
            "content_type": content_type
        })

    extracted = extract_from_receipts(stored_files, provider=provider)

    # Persist receipts (exclude debug_fields) and keep mapping for response enrichment
    created_ids = []
    debug_map = {}
    for rec in extracted:
        debug_map_key = (rec["original_filename"], rec["stored_path"])
        debug_map[debug_map_key] = rec.get("debug_fields")
        error_message = rec.get("error_message")
        # store for later reattachment
        if error_message:
            rec["_error_message"] = error_message
        # Normalize optional fields so SQLAlchemy bind params are always present
        for opt_key in [
            "extracted_vendor_name", "extracted_service_start", "extracted_service_end",
            "extracted_merchant", "extracted_amount", "extracted_date"
        ]:
            if opt_key not in rec:
                rec[opt_key] = None
        rec_db = {k: v for k, v in rec.items() if k not in ("debug_fields", "error_message", "_error_message")}
        res = db.execute(text("""
            INSERT INTO receipts (
                original_filename, stored_path, content_type,
                extracted_merchant, extracted_amount, extracted_date,
                extracted_vendor_name, extracted_service_start, extracted_service_end,
                status)
            VALUES (
                :original_filename, :stored_path, :content_type,
                :extracted_merchant, :extracted_amount, :extracted_date,
                :extracted_vendor_name, :extracted_service_start, :extracted_service_end,
                :status)
        """), rec_db)
        pk = db.execute(text("SELECT last_insert_rowid() as id")).scalar()
        created_ids.append(pk)
    db.commit()

    # Fetch inserted receipts with IDs
    rows = db.execute(text("SELECT * FROM receipts WHERE id IN (:ids)".replace(":ids", ",".join(str(i) for i in created_ids)))).mappings().all()
    receipts = []
    for r in rows:
        d = dict(r)
        key = (d["original_filename"], d["stored_path"])
        if key in debug_map:
            d["debug_fields"] = debug_map[key]
        # recover error message from in-memory extracted list
        for src in extracted:
            if src["original_filename"] == d["original_filename"] and src["stored_path"] == d["stored_path"]:
                if src.get("_error_message"):
                    d["error_message"] = src.get("_error_message")
                break
        receipts.append(d)

    expenses = db.execute(text("SELECT * FROM expenses")).mappings().all()
    expenses_list = [dict(e) for e in expenses]

    proposals = propose_matches(expenses_list, receipts)

    return {"receipts": receipts, "proposals": proposals}

@router.get("/{receipt_id}/download")
async def download_receipt(receipt_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("SELECT id, original_filename, stored_path, content_type FROM receipts WHERE id = :id"), {"id": receipt_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Receipt not found")
    stored_path = row["stored_path"]
    original = row["original_filename"]
    ctype = row.get("content_type") or "application/octet-stream"
    # Heuristic: if stored_path looks like a UUID filename without path, treat as blob name
    try:
        data = load_receipt_bytes(stored_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File data not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load receipt: {e}")
    from fastapi.responses import StreamingResponse
    return StreamingResponse(BytesIO(data), media_type=ctype, headers={
        "Content-Disposition": f"attachment; filename=\"{original}\""
    })

@router.post("/confirm-matches")
async def confirm_matches(data: dict, db: Session = Depends(get_db)):
    mappings = data.get("mappings", [])
    if not isinstance(mappings, list):
        return {"status": "error", "message": "mappings must be a list"}

    inserted = 0
    normalized: list[dict] = []
    for idx, m in enumerate(mappings):
        if not isinstance(m, dict):
            continue  # skip invalid entries silently
        exp_id = m.get("expense_id")
        rec_id = m.get("receipt_id")
        score = m.get("match_score", m.get("score"))  # allow either key from client
        if exp_id is None or rec_id is None:
            # skip incomplete mapping
            continue
        # Ensure score present (nullable allowed, but default to 0.0 if absent)
        if score is None:
            score = 0.0
        normalized.append({
            "expense_id": exp_id,
            "receipt_id": rec_id,
            "match_score": score
        })

    for row in normalized:
        db.execute(text("""
            INSERT OR REPLACE INTO expense_receipts (expense_id, receipt_id, match_score)
            VALUES (:expense_id, :receipt_id, :match_score)
        """), row)
        inserted += 1
    db.commit()
    return {"status": "ok", "mappings_saved": inserted, "received": len(mappings), "processed": len(normalized)}
