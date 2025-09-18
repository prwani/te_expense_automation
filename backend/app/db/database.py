from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import os

DB_PATH = os.environ.get("EXPENSE_DB_PATH", os.path.join(os.path.dirname(__file__), "expenses.db"))
# Ensure parent directory exists (important when using a custom path like /home/data/expenses.db on App Service)
try:
  db_dir = os.path.dirname(DB_PATH)
  if db_dir and not os.path.isdir(db_dir):
    os.makedirs(db_dir, exist_ok=True)
except Exception as _e:
  # Non-fatal: if directory creation fails, engine creation may still work for relative paths
  pass
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

INIT_SQL = """
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  category TEXT NOT NULL,
  merchant TEXT NOT NULL,
  amount REAL NOT NULL,
  amount_in_inr REAL NOT NULL,
  project_id TEXT NOT NULL,
  billable INTEGER NOT NULL DEFAULT 0,
  payment_method TEXT NOT NULL,
  receipts_attached INTEGER NOT NULL DEFAULT 0,
  tagged INTEGER NOT NULL DEFAULT 0 -- 0 = not yet in any report, 1 = included in a report
);

CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  original_filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  content_type TEXT,
  extracted_merchant TEXT,
  extracted_amount REAL,
  extracted_date TEXT,
  extracted_vendor_name TEXT,
  extracted_service_start TEXT,
  extracted_service_end TEXT,
  status TEXT NOT NULL DEFAULT 'uploaded'
);

CREATE TABLE IF NOT EXISTS expense_receipts (
  expense_id INTEGER NOT NULL,
  receipt_id INTEGER NOT NULL,
  match_score REAL,
  PRIMARY KEY (expense_id, receipt_id),
  FOREIGN KEY (expense_id) REFERENCES expenses(id),
  FOREIGN KEY (receipt_id) REFERENCES receipts(id)
);

-- Expense reports master table
CREATE TABLE IF NOT EXISTS expense_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  interim_approver TEXT,
  approving_manager TEXT,
  purpose TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Junction table linking reports to expenses (many-to-many)
CREATE TABLE IF NOT EXISTS report_expenses (
  report_id INTEGER NOT NULL,
  expense_id INTEGER NOT NULL,
  PRIMARY KEY (report_id, expense_id),
  FOREIGN KEY (report_id) REFERENCES expense_reports(id) ON DELETE CASCADE,
  FOREIGN KEY (expense_id) REFERENCES expenses(id) ON DELETE CASCADE
);

-- Junction table linking reports to receipts (many-to-many)
CREATE TABLE IF NOT EXISTS report_receipts (
  report_id INTEGER NOT NULL,
  receipt_id INTEGER NOT NULL,
  PRIMARY KEY (report_id, receipt_id),
  FOREIGN KEY (report_id) REFERENCES expense_reports(id) ON DELETE CASCADE,
  FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
);

-- Line-item breakdown of an expense (e.g., hotel nightly charges, taxes)
CREATE TABLE IF NOT EXISTS expense_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  expense_id INTEGER NOT NULL,
  item_date TEXT,
  description TEXT NOT NULL,
  amount REAL NOT NULL,
  FOREIGN KEY (expense_id) REFERENCES expenses(id) ON DELETE CASCADE
);

INSERT OR IGNORE INTO expenses (id, date, category, merchant, amount, amount_in_inr, project_id, billable, payment_method, receipts_attached) VALUES
 (1,'2024-07-30','Airfare','American Express Global Business Travel',32274,32274,'0',0,'Amex',1),
 (2,'2024-09-30','Hotel','The Westin',16078,16078,'0',0,'Amex',1),
 (3,'2024-09-24','Hotel','JW Marriott',14898,14898,'0',0,'Amex',1),
 (4,'2024-02-07','Hotel','The Taj Mahal Palace',78942,78942,'0',0,'Amex',1);
"""

def init_db():
  with engine.begin() as conn:
    # Create / seed
    for statement in INIT_SQL.strip().split(";\n\n"):
      stmt = statement.strip()
      if stmt:
        conn.execute(text(stmt))
    # Lightweight migration (only needed for pre-existing DBs missing new columns)
    try:
      existing_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(receipts)"))}
      alter_statements = []
      if "extracted_vendor_name" not in existing_cols:
        alter_statements.append("ALTER TABLE receipts ADD COLUMN extracted_vendor_name TEXT")
      if "extracted_service_start" not in existing_cols:
        alter_statements.append("ALTER TABLE receipts ADD COLUMN extracted_service_start TEXT")
      if "extracted_service_end" not in existing_cols:
        alter_statements.append("ALTER TABLE receipts ADD COLUMN extracted_service_end TEXT")
      # Add 'tagged' column to expenses if missing
      existing_expense_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(expenses)"))}
      if "tagged" not in existing_expense_cols:
        alter_statements.append("ALTER TABLE expenses ADD COLUMN tagged INTEGER NOT NULL DEFAULT 0")
      # Create expense_items table if missing
      existing_tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
      if "expense_items" not in existing_tables:
        conn.execute(text("""
          CREATE TABLE expense_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER NOT NULL,
            item_date TEXT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY (expense_id) REFERENCES expenses(id) ON DELETE CASCADE
          )
        """))
      for alt in alter_statements:
        conn.execute(text(alt))
    except Exception as e:  # pragma: no cover - defensive
      # Non-fatal; log via print (logger not initialized here)
      print(f"[init_db] migration check skipped due to error: {e}")
