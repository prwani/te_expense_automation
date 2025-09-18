from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from ..db.database import SessionLocal, engine
from sqlalchemy import text
from ..schemas.expense import Expense, ExpenseCreate, ExpenseUpdate
from typing import List, Optional
import math
import re
import os
from ..services.receipt_loader import load_receipt_bytes
from ..services.blob_storage import generate_download_url  # may be unused after refactor
from contextlib import suppress
from typing import Any
import json
from ..services.aoai_itemize import extract_invoice_line_items

router = APIRouter(prefix="/expenses", tags=["expenses"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/", response_model=List[Expense])
async def list_expenses(tagged: Optional[int] = Query(None, description="Filter by tagged state 0 or 1"), db: Session = Depends(get_db)):
    base_sql = "SELECT * FROM expenses"
    params = {}
    if tagged is not None:
        base_sql += " WHERE tagged = :tagged"
        params["tagged"] = tagged
    base_sql += " ORDER BY date DESC, id DESC"
    rows = db.execute(text(base_sql), params).mappings().all()
    return [dict(r) for r in rows]

@router.post("/", response_model=Expense)
async def create_expense(payload: ExpenseCreate, db: Session = Depends(get_db)):
    data = payload.model_dump()
    # Ensure tagged defaults to 0 if not provided
    if "tagged" not in data:
        data["tagged"] = 0
    res = db.execute(text("""
        INSERT INTO expenses (date, category, merchant, amount, amount_in_inr, project_id, billable, payment_method, receipts_attached, tagged)
        VALUES (:date, :category, :merchant, :amount, :amount_in_inr, :project_id, :billable, :payment_method, :receipts_attached, :tagged)
    """), data)
    new_id = db.execute(text("SELECT last_insert_rowid() as id")).scalar()
    db.commit()
    row = db.execute(text("SELECT * FROM expenses WHERE id = :id"), {"id": new_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to fetch created expense")
    return {k: row[k] for k in row.keys()}

@router.patch("/{expense_id}", response_model=Expense)
async def update_expense(expense_id: int, payload: ExpenseUpdate, db: Session = Depends(get_db)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        row = db.execute(text("SELECT * FROM expenses WHERE id = :id"), {"id": expense_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Expense not found")
        return {k: row[k] for k in row.keys()}
    sets = ", ".join(f"{k} = :{k}" for k in updates.keys())
    updates["id"] = expense_id
    db.execute(text(f"UPDATE expenses SET {sets} WHERE id = :id"), updates)
    # Verify update
    row = db.execute(text("SELECT * FROM expenses WHERE id = :id"), {"id": expense_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Expense not found")
    db.commit()
    return {k: row[k] for k in row.keys()}

@router.post("/{expense_id}/duplicate", response_model=Expense)
async def duplicate_expense(expense_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("SELECT * FROM expenses WHERE id = :id"), {"id": expense_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Expense not found")
    data = dict(row)
    data.pop("id", None)
    if "tagged" not in data:
        data["tagged"] = 0
    res = db.execute(text("""
        INSERT INTO expenses (date, category, merchant, amount, amount_in_inr, project_id, billable, payment_method, receipts_attached, tagged)
        VALUES (:date, :category, :merchant, :amount, :amount_in_inr, :project_id, :billable, :payment_method, :receipts_attached, :tagged)
    """), data)
    new_id = db.execute(text("SELECT last_insert_rowid() as id")).scalar()
    db.commit()
    new_row = db.execute(text("SELECT * FROM expenses WHERE id = :id"), {"id": new_id}).mappings().first()
    if not new_row:
        raise HTTPException(status_code=500, detail="Failed to duplicate expense")
    return {k: new_row[k] for k in new_row.keys()}

@router.post("/bulk", response_model=List[Expense])
async def bulk_upsert(expenses: List[dict], db: Session = Depends(get_db)):
    # Clean, deterministic upsert logic. For each incoming expense dict:
    #  - If it has an id and exists -> update provided (non-null) fields.
    #  - Else insert a new row.
    # Returns the resulting expense rows in the same order as input.
    results: List[dict] = []
    allowed_cols = {
        "date",
        "category",
        "merchant",
        "amount",
        "amount_in_inr",
        "project_id",
        "billable",
        "payment_method",
        "receipts_attached",
        "tagged",
    }
    for idx, item in enumerate(expenses):
        if not isinstance(item, dict):
            continue
        data = {k: v for k, v in item.items() if k in allowed_cols or k == "id"}
        if "tagged" not in data:
            data["tagged"] = 0
        expense_id = data.get("id")
        if expense_id:
            existing = db.execute(text("SELECT id FROM expenses WHERE id = :id"), {"id": expense_id}).scalar()
            if existing:
                update_fields = {k: v for k, v in data.items() if k in allowed_cols and k != "id" and v is not None}
                if update_fields:
                    sets = ", ".join(f"{k} = :{k}" for k in update_fields.keys())
                    update_fields["id"] = expense_id
                    db.execute(text(f"UPDATE expenses SET {sets} WHERE id = :id"), update_fields)
                row = db.execute(text("SELECT * FROM expenses WHERE id = :id"), {"id": expense_id}).mappings().first()
                if row:
                    results.append({k: row[k] for k in row.keys()})
                continue
        # Insert new
        insert_data = {k: data.get(k) for k in allowed_cols}
        db.execute(
            text(
                """
                INSERT INTO expenses (date, category, merchant, amount, amount_in_inr, project_id, billable, payment_method, receipts_attached, tagged)
                VALUES (:date, :category, :merchant, :amount, :amount_in_inr, :project_id, :billable, :payment_method, :receipts_attached, :tagged)
                """
            ),
            insert_data,
        )
        new_id = db.execute(text("SELECT last_insert_rowid() as id")).scalar()
        row = db.execute(text("SELECT * FROM expenses WHERE id = :id"), {"id": new_id}).mappings().first()
        if row:
            results.append({k: row[k] for k in row.keys()})
    db.commit()
    return results

@router.get("/receipt-links")
async def list_expense_receipt_links(
    tagged: Optional[int] = Query(None, description="Filter expenses by tagged state 0 or 1"),
    db: Session = Depends(get_db),
):
    """Return joined expense->receipt link rows with basic expense and receipt info.

    Response shape:
        [
          {
            "expense_id": int,
            "receipt_id": int,
            "match_score": float | None,
            "expense_category": str,
            "expense_amount": float,
            "receipt_original_filename": str,
            "receipt_extracted_amount": float | None,
            "receipt_extracted_date": str | None,
          }, ...
        ]
    """
    filters = []
    params: dict = {}
    if tagged is not None:
        filters.append("e.tagged = :tagged")
        params["tagged"] = tagged
    where_clause = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"""
        SELECT
            er.expense_id,
            er.receipt_id,
            er.match_score,
            e.category AS expense_category,
            e.amount   AS expense_amount,
            r.original_filename AS receipt_original_filename,
            r.extracted_amount  AS receipt_extracted_amount,
            r.extracted_date    AS receipt_extracted_date
        FROM expense_receipts er
        JOIN expenses e ON e.id = er.expense_id
        JOIN receipts r ON r.id = er.receipt_id
        {where_clause}
        ORDER BY er.expense_id DESC, er.receipt_id DESC
    """
    rows = db.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]

@router.get("/{expense_id}/items")
async def get_expense_items(expense_id: int, db: Session = Depends(get_db)):
    exists = db.execute(text("SELECT id, amount, category FROM expenses WHERE id = :id"), {"id": expense_id}).mappings().first()
    if not exists:
        raise HTTPException(status_code=404, detail="Expense not found")
    rows = db.execute(text("SELECT id, expense_id, item_date, description, amount FROM expense_items WHERE expense_id = :id ORDER BY id"), {"id": expense_id}).mappings().all()
    return {"expense_id": expense_id, "total": exists["amount"], "items": [dict(r) for r in rows]}

@router.post("/{expense_id}/itemize")
async def itemize_expense(
    expense_id: int,
    strategy: Optional[str] = Query("auto", description="Strategy: auto|rebuild"),
    provider: Optional[str] = Query("document_intelligence", description="Force provider for re-analysis of linked receipts"),
    db: Session = Depends(get_db),
):
    print(f"[itemize] START expense_id={expense_id} strategy={strategy} provider={provider}")
    exp = db.execute(text("SELECT * FROM expenses WHERE id = :id"), {"id": expense_id}).mappings().first()
    if not exp:
        print(f"[itemize] Expense {expense_id} not found")
        raise HTTPException(status_code=404, detail="Expense not found")
    print(f"[itemize] Expense category={exp.get('category')} amount={exp.get('amount')}")

    if exp["category"].lower() != "hotel":
        print("[itemize] Not a hotel category -> abort")
        raise HTTPException(status_code=400, detail="Itemization currently supported only for Hotel expenses")

    if strategy == "rebuild":
        print("[itemize] Rebuild requested -> deleting existing items")
        db.execute(text("DELETE FROM expense_items WHERE expense_id = :id"), {"id": expense_id})

    existing = db.execute(
        text("SELECT id, expense_id, item_date, description, amount FROM expense_items WHERE expense_id = :id"),
        {"id": expense_id},
    ).mappings().all()
    if existing and strategy != "rebuild":
        print(f"[itemize] Reusing {len(existing)} existing items (strategy={strategy})")
        return {"expense_id": expense_id, "items": [dict(r) for r in existing], "reused": True}
    print(f"[itemize] Existing items count after possible purge: {len(existing)}")

    links = db.execute(
        text(
            """
        SELECT r.id, r.original_filename, r.stored_path, r.extracted_amount, r.extracted_date,
                r.extracted_service_start, r.extracted_service_end
        FROM expense_receipts er JOIN receipts r ON r.id = er.receipt_id
        WHERE er.expense_id = :id
        """
        ),
        {"id": expense_id},
    ).mappings().all()
    print(f"[itemize] Linked receipts found: {len(links)}")
    if links:
        for l in links[:3]:
            print(
                f"[itemize] Receipt id={l['id']} file={l['original_filename']} "
                f"path={l['stored_path']} extracted_amount={l['extracted_amount']} "
                f"service_start={l['extracted_service_start']} service_end={l['extracted_service_end']}"
            )

    total_amount = float(exp["amount"]) if exp["amount"] is not None else 0.0
    print(f"[itemize] Total expense amount={total_amount}")

    # --- New AOAI itemization path ---
    items_to_insert: list[dict] = []
    if not links:
        print("[itemize] No linked receipts -> cannot itemize")
        return {"expense_id": expense_id, "items": [], "reused": False, "warning": "No receipts linked"}

    # Use first linked receipt's stored_path as blob name (upload code stores blob name there when using blob storage)
    primary = links[0]
    blob_name = primary["stored_path"]
    print(f"[itemize] Invoking AOAI itemization for blob={blob_name}")

    raw_text = ""
    with suppress(Exception):
        raw_text = extract_invoice_line_items(blob_name)
    # New: detect structured error envelope from AOAI helper
    if raw_text and raw_text.strip().startswith('{') and '"error"' in raw_text[:120]:
        try:
            err_payload = json.loads(raw_text)
            if isinstance(err_payload, dict) and err_payload.get("error"):
                print(f"[itemize] AOAI error: {err_payload.get('message')}")
                return {"expense_id": expense_id, "items": [], "reused": False, "error": err_payload}
        except Exception:
            pass
    if not raw_text:
        print("[itemize] AOAI returned empty content")
        return {"expense_id": expense_id, "items": [], "reused": False, "warning": "AOAI returned empty"}

    # Strip code fences if present
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        # Remove the first fence line and the last fence line
        parts = cleaned.split("\n")
        if parts[0].startswith("```"):
            parts = parts[1:]
        if parts and parts[-1].startswith("```"):
            parts = parts[:-1]
        cleaned = "\n".join(parts)
    cleaned = cleaned.strip()
    # Sometimes model echoes 'json' label on fence
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].lstrip(':').strip()

    parsed_items = []
    try:
        parsed_items = json.loads(cleaned)
        if not isinstance(parsed_items, list):
            print("[itemize] Parsed JSON not a list")
            parsed_items = []
    except Exception as e:
        print(f"[itemize] JSON parse error: {e}")
        parsed_items = []

    # Normalize fields
    norm: list[dict] = []
    for i, it in enumerate(parsed_items):
        if not isinstance(it, dict):
            continue
        desc = it.get("description") or it.get("item") or it.get("name")
        amount = it.get("debit") or it.get("amount") or it.get("total")
        date_val = it.get("date") or it.get("item_date")
        if amount in (None, ""):
            continue
        try:
            amount_f = float(str(amount).replace(",", "").strip())
        except Exception:
            continue
        if amount_f <= 0:
            continue
        # Normalize date to YYYY-MM-DD if in DD-MM-YY or similar
        norm_date = None
        if isinstance(date_val, str) and date_val.strip():
            dv = date_val.strip()
            # Heuristic conversions
            with suppress(Exception):
                if re.match(r"^\d{2}-\d{2}-\d{2,4}$", dv):
                    parts = dv.split("-")
                    d, m, y = parts
                    if len(y) == 2:
                        y = "20" + y  # naive 20xx assumption
                    norm_date = f"{y}-{m}-{d}"
                elif re.match(r"^\d{4}-\d{2}-\d{2}$", dv):
                    norm_date = dv
        entry = {"description": desc or f"Item {i+1}", "amount": amount_f, "item_date": norm_date}
        norm.append(entry)

    if not norm:
        print("[itemize] No valid items after normalization")
        return {"expense_id": expense_id, "items": [], "reused": False, "warning": "No valid AOAI items"}

    sum_norm = sum(i["amount"] for i in norm)
    print(f"[itemize] AOAI items count={len(norm)} sum={sum_norm}")
    if total_amount > 0 and sum_norm > 0 and abs(sum_norm - total_amount) / total_amount > 0.01:
        scale = total_amount / sum_norm
        print(f"[itemize] Scaling AOAI items by factor={scale}")
        running = 0.0
        for it in norm:
            it["amount"] = round(it["amount"] * scale, 2)
            running += it["amount"]
        drift = round(total_amount - running, 2)
        if abs(drift) >= 0.01 and norm:
            norm[-1]["amount"] = round(norm[-1]["amount"] + drift, 2)
            print(f"[itemize] Adjusted last item for drift {drift}")

    items_to_insert = norm

    print(f"[itemize] Final items_to_insert count={len(items_to_insert)}")
    for idx, it in enumerate(items_to_insert):
        print(f"[itemize] Inserting item {idx}: {it}")
        db.execute(
            text(
                """
            INSERT INTO expense_items (expense_id, item_date, description, amount)
            VALUES (:expense_id, :item_date, :description, :amount)
        """
            ),
            {"expense_id": expense_id, **it},
        )
    db.commit()
    rows = db.execute(
        text("SELECT id, expense_id, item_date, description, amount FROM expense_items WHERE expense_id = :id ORDER BY id"),
        {"id": expense_id},
    ).mappings().all()
    total_items = sum(r["amount"] for r in rows)
    final_drift = round(total_amount - total_items, 2)
    print(f"[itemize] Post-insert total_items={total_items} final_drift={final_drift}")
    if abs(final_drift) >= 0.01 and rows:
        last_id = rows[-1]["id"]
        print(f"[itemize] Adjusting last row id={last_id} by drift={final_drift}")
        db.execute(
            text("UPDATE expense_items SET amount = amount + :d WHERE id = :id"),
            {"d": final_drift, "id": last_id},
        )
        db.commit()
        rows = db.execute(
            text("SELECT id, expense_id, item_date, description, amount FROM expense_items WHERE expense_id = :id ORDER BY id"),
            {"id": expense_id},
        ).mappings().all()
        total_items = sum(r["amount"] for r in rows)
        print(f"[itemize] After drift adjustment new total_items={total_items}")
    print(f"[itemize] COMPLETE expense_id={expense_id} reused=False")
    return {"expense_id": expense_id, "items": [dict(r) for r in rows], "reused": False}
