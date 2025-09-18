import anyio
from backend.app.routers.receipts import confirm_matches
from backend.app.db.database import SessionLocal, init_db

init_db()

async def main():
    data={'mappings': [
        {'expense_id':2,'receipt_id':1,'score':0.91},
        {'expense_id':3,'receipt_id':1,'match_score':0.5},
        {'expense_id':999,'receipt_id':1}  # invalid expense id but still inserted (no FK enforcement beyond schema)
    ]}
    db = SessionLocal()
    resp = await confirm_matches(data, db)
    print('Response:', resp)
    from sqlalchemy import text
    rows = list(db.execute(text("SELECT * FROM expense_receipts WHERE receipt_id=1")))
    print('Rows:', rows)
    db.close()

anyio.run(main)
