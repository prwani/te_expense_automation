from pydantic import BaseModel
from typing import Optional

class ExpenseBase(BaseModel):
    date: str
    category: str
    merchant: str
    amount: float
    amount_in_inr: float
    project_id: str
    billable: int = 0
    payment_method: str
    receipts_attached: int = 0
    tagged: int = 0

class ExpenseCreate(ExpenseBase):
    pass

class ExpenseUpdate(BaseModel):
    date: Optional[str] = None
    category: Optional[str] = None
    merchant: Optional[str] = None
    amount: Optional[float] = None
    amount_in_inr: Optional[float] = None
    project_id: Optional[str] = None
    billable: Optional[int] = None
    payment_method: Optional[str] = None
    receipts_attached: Optional[int] = None
    tagged: Optional[int] = None

class Expense(ExpenseBase):
    id: int

    class Config:
        from_attributes = True

class Receipt(BaseModel):
    id: int
    original_filename: str
    stored_path: str
    content_type: Optional[str]
    extracted_merchant: Optional[str]
    extracted_amount: Optional[float]
    extracted_date: Optional[str]
    extracted_vendor_name: Optional[str] = None
    extracted_service_start: Optional[str] = None
    extracted_service_end: Optional[str] = None
    status: str

    class Config:
        from_attributes = True

class MatchProposal(BaseModel):
    receipt_id: int
    expense_id: int
    score: float
    rationale: str

class MatchConfirmation(BaseModel):
    mappings: list[MatchProposal]
