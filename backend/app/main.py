from fastapi import FastAPI, Response, HTTPException
from .db.database import init_db
from .routers import expenses, receipts, expense_reports
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
import logging
from dotenv import load_dotenv

# Load .env before anything else so env vars are available to extraction service
load_dotenv()

init_db()

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("startup")

cu_ep = os.getenv("AZURE_CONTENT_UNDERSTANDING_ENDPOINT")
cu_key = os.getenv("AZURE_CONTENT_UNDERSTANDING_KEY")
doc_ep = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
logger.info("DocIntel endpoint present=%s | CU endpoint present=%s | CU key present=%s", bool(doc_ep), bool(cu_ep), bool(cu_key))

app = FastAPI(title="Travel Expense Automation API")

# Determine frontend directory (../frontend relative to this file)
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))
INDEX_FILE = os.path.join(FRONTEND_DIR, "index.html")

if os.path.isfile(INDEX_FILE):
    # Mount entire frontend directory at /static
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(expenses.router)
app.include_router(receipts.router)
app.include_router(expense_reports.router)

@app.get("/env-check")
async def env_check():
    # Return booleans only; never echo secrets
    return {
        "document_intelligence_endpoint": bool(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")),
        "content_understanding_endpoint": bool(os.getenv("AZURE_CONTENT_UNDERSTANDING_ENDPOINT")),
        "content_understanding_key_present": bool(os.getenv("AZURE_CONTENT_UNDERSTANDING_KEY")),
    }

@app.get("/")
async def root():
    if os.path.isfile(INDEX_FILE):
        return FileResponse(INDEX_FILE)
    return {"message": "Expense API running"}

@app.get("/index.html")
async def index_html():
    if os.path.isfile(INDEX_FILE):
        return FileResponse(INDEX_FILE)
    return {"detail": "index.html not found"}

@app.get("/expenses.html")
async def expenses_html():
    expenses_file = os.path.join(FRONTEND_DIR, "expenses.html")
    if os.path.isfile(expenses_file):
        return FileResponse(expenses_file)
    return {"detail": "expenses.html not found"}

# Generic passthrough for other html files we added (tagged-expenses, reports, report-detail)
@app.get("/{page_name}.html")
async def serve_html_page(page_name: str):
    # Avoid overriding already-defined endpoints (index, expenses handled separately)
    if page_name in {"index", "expenses"}:
        raise HTTPException(status_code=404, detail="Reserved page")
    file_path = os.path.join(FRONTEND_DIR, f"{page_name}.html")
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail=f"{page_name}.html not found")

@app.get("/favicon.ico")
async def favicon():
    # Serve favicon if present; otherwise return 204 to suppress 404 noise
    fav_path = os.path.join(FRONTEND_DIR, "favicon.ico")
    if os.path.isfile(fav_path):
        return FileResponse(fav_path)
    return Response(status_code=204)

@app.post("/admin/reseed")
async def admin_reseed():
    """Re-run init_db to recreate tables & seed expenses after DB removal.
    Safe idempotent reseed (INSERT OR IGNORE used in init script)."""
    init_db()
    return {"status": "ok", "message": "Database reseeded"}

@app.post("/admin/reset-data")
async def admin_reset_data():
    """Delete all dynamic data and restore only original seed expense rows.

    This will:
      - Delete all rows from: expense_receipts, receipts, report_expenses, report_receipts, expense_reports, expense_items
      - Delete all expenses except the original seed IDs (1-4) then re-run seeding to ensure they exist.
    """
    from sqlalchemy import text
    from .db.database import engine
    seed_ids = {1,2,3,4}
    with engine.begin() as conn:
        # Order matters due to FKs (even if not enforced strictly in SQLite without PRAGMA foreign_keys=on)
        conn.execute(text("DELETE FROM expense_receipts"))
        conn.execute(text("DELETE FROM report_expenses"))
        conn.execute(text("DELETE FROM report_receipts"))
        conn.execute(text("DELETE FROM expense_items"))
        conn.execute(text("DELETE FROM receipts"))
        conn.execute(text("DELETE FROM expense_reports"))
        # Delete expenses not in seed list
        existing_ids = {row[0] for row in conn.execute(text("SELECT id FROM expenses"))}
        to_delete = [i for i in existing_ids if i not in seed_ids]
        if to_delete:
            id_csv = ",".join(str(i) for i in to_delete)
            conn.execute(text(f"DELETE FROM expenses WHERE id IN ({id_csv})"))
        # Ensure seed rows exist and have tagged=0
        conn.execute(text("UPDATE expenses SET tagged = 0 WHERE id IN (1,2,3,4)"))
    # Rerun init to reinsert any missing seed rows (INSERT OR IGNORE ensures no duplicates)
    init_db()
    return {"status": "ok", "message": "All dynamic data cleared; seed expenses restored"}
