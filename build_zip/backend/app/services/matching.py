from rapidfuzz import fuzz
from typing import List, Dict, Any, Tuple
import re, datetime as dt
from contextlib import suppress
import logging

logger = logging.getLogger(__name__)

# Simple configurable weights
WEIGHTS = {
    "merchant": 0.5,
    "amount": 0.3,
    "date": 0.2,
}

MERCHANT_SANITIZE_RE = re.compile(r"[^a-z0-9]+")

def normalize_merchant(name: str | None):
    if not name:
        return None
    name = name.lower().strip()
    name = MERCHANT_SANITIZE_RE.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()

def normalize_amount(a):
    try:
        return round(float(a), 2)
    except Exception:
        return None

def score_match(expense: Dict[str, Any], receipt: Dict[str, Any]) -> Tuple[float, str, Dict[str, float]]:
    score_components: list[Tuple[str, float, float]] = []
    details: Dict[str, float] = {}

    def _parse_date(val: str | None):
        """Parse various date string formats into a date object.
        Supports:
          - ISO: YYYY-MM-DD, YYYY/MM/DD
          - DMY: DD/MM/YYYY, DD-MM-YYYY
          - DMY short year: DD/MM/YY, DD-MM-YY (assume 20xx for 00-79 else 19xx)
        Returns dt.date or None.
        """
        if not val or not isinstance(val, str):
            return None
        s = val.strip().split()[0]  # take first token if time appended
        s = s.replace('.','-')
        # Normalize separators to '-'
        s_norm = re.sub(r'[\/]', '-', s)
        candidates = [s_norm]
        # If format like DD/MM/YY missing, create potential expansion
        # We'll try explicit strptime patterns
        patterns = [
            "%Y-%m-%d", "%Y-%m-%d",  # iso (dup harmless)
            "%d-%m-%Y", "%d-%m-%y",
            "%Y/%m/%d", "%d/%m/%Y", "%d/%m/%y",
        ]
        for p in patterns:
            with suppress(Exception):
                dt_obj = dt.datetime.strptime(s_norm, p)
                year = dt_obj.year
                # Expand 2-digit year heuristics
                if year < 100:
                    if year <= 79:
                        year += 2000
                    else:
                        year += 1900
                    dt_obj = dt_obj.replace(year=year)
                return dt_obj.date()
        # Fallback: try fromisoformat directly
        with suppress(Exception):
            return dt.date.fromisoformat(s_norm)
        return None

    extracted_merchant = receipt.get("extracted_merchant") or receipt.get("extracted_vendor_name")
    if extracted_merchant and expense.get("merchant"):
        rec_merch = normalize_merchant(extracted_merchant)
        exp_merch = normalize_merchant(expense["merchant"])
        merchant_score = fuzz.partial_ratio(rec_merch, exp_merch) / 100.0 if rec_merch and exp_merch else 0.0
    else:
        merchant_score = 0.0
    score_components.append(("merchant", merchant_score, WEIGHTS["merchant"]))
    details["merchant_score"] = merchant_score

    exp_amount = normalize_amount(expense.get("amount"))
    rec_amount = normalize_amount(receipt.get("extracted_amount"))
    if exp_amount is not None and rec_amount is not None:
        delta = abs(exp_amount - rec_amount)
        if exp_amount == 0:
            amount_score = 0.0
        else:
            if delta <= 0.5:
                amount_score = 1.0
            else:
                pct = delta / exp_amount
                amount_score = max(0.0, 1 - (pct / 0.05))
    else:
        amount_score = 0.0
    score_components.append(("amount", amount_score, WEIGHTS["amount"]))
    details["amount_score"] = amount_score

    exp_date_iso = expense.get("date")
    range_start = receipt.get("extracted_service_start")
    range_end = receipt.get("extracted_service_end")
    single_date = receipt.get("extracted_date")
    date_score = 0.0
    try:
        if exp_date_iso:
            exp_date = _parse_date(exp_date_iso)
            if not exp_date:
                raise ValueError("expense date parse failed")
            if range_start and range_end:
                start_d = _parse_date(range_start)
                end_d = _parse_date(range_end)
                if start_d and end_d:
                    if start_d <= exp_date <= end_d:
                        date_score = 1.0
                    else:
                        if exp_date < start_d:
                            delta_days = (start_d - exp_date).days
                        else:
                            delta_days = (exp_date - end_d).days
                        if delta_days == 1:
                            date_score = 0.6
                        elif delta_days == 2:
                            date_score = 0.3
            elif single_date:
                rec_date = _parse_date(single_date)
                if rec_date:
                    delta_days = abs((rec_date - exp_date).days)
                    if delta_days == 0:
                        date_score = 1.0
                    elif delta_days == 1:
                        date_score = 0.6
                    elif delta_days == 2:
                        date_score = 0.3
    except Exception:
        date_score = 0.0
    score_components.append(("date", date_score, WEIGHTS["date"]))
    details["date_score"] = date_score

    total = sum(component * weight for _, component, weight in score_components)
    details["weighted_total"] = total

    rationale_parts = []
    for name, component, weight in score_components:
        rationale_parts.append(f"{name}:{component:.2f}*{weight}")
    if range_start or range_end:
        rationale_parts.append(f"range:{range_start or 'None'}->{range_end or 'None'}")
    if receipt.get("extracted_vendor_name") and not receipt.get("extracted_merchant"):
        rationale_parts.append("vendor_used")
    return total, "; ".join(rationale_parts), details


def propose_matches(expenses: List[Dict[str, Any]], receipts: List[Dict[str, Any]]):
    proposals = []
    for r in receipts:
        # Skip receipts flagged with errors so UI shows empty selection
        if (r.get("error_message") or str(r.get("status", "")).startswith("error_")):
            continue
        # Skip if insufficient extraction (no merchant/vendor AND no amount)
        if not (r.get("extracted_merchant") or r.get("extracted_vendor_name")) and r.get("extracted_amount") is None:
            logger.info("Skipping receipt id=%s due to missing merchant/vendor and amount", r.get("id"))
            continue
        best = None
        logger.debug(
            "Matching receipt id=%s file=%s extracted={merchant:%s vendor:%s amount:%s date:%s svc_start:%s svc_end:%s}",
            r.get("id"), r.get("original_filename"), r.get("extracted_merchant"), r.get("extracted_vendor_name"),
            r.get("extracted_amount"), r.get("extracted_date"), r.get("extracted_service_start"), r.get("extracted_service_end")
        )
        for e in expenses:
            score, rationale, details = score_match(e, r)
            logger.debug(
                "  Expense id=%s merchant=%s amount=%s date=%s => score=%.4f components merchant=%.3f amount=%.3f date=%.3f", 
                e.get("id"), e.get("merchant"), e.get("amount"), e.get("date"), score,
                details.get("merchant_score", 0.0), details.get("amount_score", 0.0), details.get("date_score", 0.0)
            )
            if best is None or score > best[0]:
                best = (score, rationale, e["id"], r["id"], details)
        if best:
            proposals.append({
                "receipt_id": best[3],
                "expense_id": best[2],
                "score": round(best[0], 4),
                "rationale": best[1]
            })
            logger.info(
                "Selected match receipt=%s -> expense=%s total=%.4f rationale=%s details=%s",
                best[3], best[2], best[0], best[1], best[4]
            )
    return proposals
