from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from ..db.database import SessionLocal

router = APIRouter(prefix="/expense-reports", tags=["expense-reports"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/")
async def create_report(payload: dict, db: Session = Depends(get_db)):
    required = ["name", "expense_ids", "receipt_ids"]
    for r in required:
        if r not in payload:
            raise HTTPException(status_code=400, detail=f"Missing field: {r}")

    name = payload.get("name")
    interim = payload.get("interim_approver")
    manager = payload.get("approving_manager")
    purpose = payload.get("purpose")
    expense_ids = payload.get("expense_ids") or []
    receipt_ids = payload.get("receipt_ids") or []

    if not isinstance(expense_ids, list) or not all(isinstance(x, int) for x in expense_ids):
        raise HTTPException(status_code=400, detail="expense_ids must be list[int]")
    if not isinstance(receipt_ids, list) or not all(isinstance(x, int) for x in receipt_ids):
        raise HTTPException(status_code=400, detail="receipt_ids must be list[int]")
    if not expense_ids:
        raise HTTPException(status_code=400, detail="At least one expense required")

    # Insert report
    db.execute(text("""
        INSERT INTO expense_reports (name, interim_approver, approving_manager, purpose)
        VALUES (:name, :interim, :manager, :purpose)
    """), {"name": name, "interim": interim, "manager": manager, "purpose": purpose})
    report_id = db.execute(text("SELECT last_insert_rowid() as id")).scalar()

    # Insert junction rows
    for eid in set(expense_ids):
        db.execute(text("INSERT OR IGNORE INTO report_expenses (report_id, expense_id) VALUES (:r,:e)"), {"r": report_id, "e": eid})
    for rid in set(receipt_ids):
        db.execute(text("INSERT OR IGNORE INTO report_receipts (report_id, receipt_id) VALUES (:r,:v)"), {"r": report_id, "v": rid})
    # Mark all referenced expenses as tagged
    if expense_ids:
        db.execute(text("UPDATE expenses SET tagged = 1 WHERE id IN (" + ",".join(str(int(i)) for i in set(expense_ids)) + ")"))
    db.commit()

    return await get_report(report_id, db)

@router.get("/{report_id}")
async def get_report(report_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("SELECT * FROM expense_reports WHERE id = :id"), {"id": report_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    exp_rows = db.execute(text("""
        SELECT e.* FROM report_expenses re JOIN expenses e ON e.id = re.expense_id WHERE re.report_id = :id
    """), {"id": report_id}).mappings().all()
    rec_rows = db.execute(text("""
        SELECT r.* FROM report_receipts rr JOIN receipts r ON r.id = rr.receipt_id WHERE rr.report_id = :id
    """), {"id": report_id}).mappings().all()
    # Get expense->receipt mappings (only those where both belong to this report)
    link_rows = db.execute(text("""
        SELECT er.expense_id, er.receipt_id, er.match_score
        FROM expense_receipts er
        JOIN report_expenses re ON re.expense_id = er.expense_id AND re.report_id = :id
        JOIN report_receipts rr ON rr.receipt_id = er.receipt_id AND rr.report_id = :id
    """), {"id": report_id}).mappings().all()
    return {
        "id": row["id"],
        "name": row["name"],
        "interim_approver": row["interim_approver"],
        "approving_manager": row["approving_manager"],
        "purpose": row["purpose"],
        "created_at": row["created_at"],
        "expenses": [dict(r) for r in exp_rows],
        "receipts": [dict(r) for r in rec_rows],
        "expense_receipt_links": [dict(r) for r in link_rows]
    }

@router.get("/")
async def list_reports(db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT id, name, interim_approver, approving_manager, purpose, created_at FROM expense_reports ORDER BY id DESC")),
    # rows above returns a tuple because of trailing comma; fix by re-query
    rows = db.execute(text("SELECT id, name, interim_approver, approving_manager, purpose, created_at FROM expense_reports ORDER BY id DESC")).mappings().all()
    out = []
    for row in rows:
        out.append({k: row[k] for k in row.keys()})
    return out
