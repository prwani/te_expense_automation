"""Receipt extraction service supporting:
1. Azure Document Intelligence (prebuilt-receipt etc.)
2. Azure Content Understanding (analyzer API)
3. Azure OpenAI (gpt-5-nano) structured JSON extraction + inline matching assist
4. Fallback lightweight heuristic (filename-based) when Azure unavailable

Authentication: prefers Managed Identity / DefaultAzureCredential; falls back to API key env var if provided.

Env Vars (.env):
  AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
  AZURE_DOCUMENT_INTELLIGENCE_KEY (optional if MSI available)
  AZURE_CONTENT_UNDERSTANDING_ENDPOINT (future)
  AZURE_CONTENT_UNDERSTANDING_KEY (future / optional)

Provider values accepted: 'document_intelligence', 'content_understanding', 'gpt5_nano'

Azure OpenAI Env Vars (.env):
    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_KEY (or use Managed Identity via DefaultAzureCredential if configured for the resource)
    AZURE_OPENAI_DEPLOYMENT (deployment name for gpt-5-nano or compatible model)
    AZURE_OPENAI_API_VERSION (default 2024-08-01-preview)
    (Optional) AZURE_OPENAI_TEMPERATURE (default 0.0 for deterministic extraction)
"""
from typing import List, Dict, Any, Optional, Tuple
import datetime as dt
import os
import logging
import time
import json
from io import BytesIO
from contextlib import suppress
import requests
import base64

from azure.identity import DefaultAzureCredential
from azure.core.exceptions import AzureError

from .receipt_loader import load_receipt_bytes  # unified loader (blob or local)

# Document Intelligence SDK (beta version referenced in requirements)
with suppress(ImportError):
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    # Prefer explicit models import path as per current SDK structure
    try:  # type: ignore
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # type: ignore  # noqa
    except Exception:  # pragma: no cover
        AnalyzeDocumentRequest = None  # type: ignore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PROVIDER_DOCUMENT_INTEL = "document_intelligence"
PROVIDER_CONTENT_UNDER = "content_understanding"
PROVIDER_GPT5_NANO = "gpt5_nano"

# Lazy import guard for azure OpenAI
with suppress(ImportError):
    from openai import AzureOpenAI  # type: ignore



def _filename_heuristic(files: List[Dict[str, Any]]):
    extracted = []
    for f in files:
        name = f["original_filename"].lower()
        numbers = ''.join(ch if ch.isdigit() else ' ' for ch in name).split()
        amount = None
        for token in numbers[::-1]:
            try:
                amount = float(token)
                break
            except Exception:
                pass
        extracted.append({
            "original_filename": f["original_filename"],
            "stored_path": f["stored_path"],
            "content_type": f.get("content_type"),
            "extracted_merchant": None,
            "extracted_amount": amount,
            "extracted_date": dt.date.today().isoformat(),
            "status": "extracted" if amount is not None else "extracted_partial",
            "error_message": None
        })
    return extracted


def _get_docintel_client() -> Optional["DocumentIntelligenceClient"]:
    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    if not endpoint:
        return None
    key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    # Prefer MSI / Default credential if no key
    if key:
        from azure.core.credentials import AzureKeyCredential  # local import to avoid dep if unused
        credential = AzureKeyCredential(key)
    else:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    try:
        return DocumentIntelligenceClient(endpoint=endpoint, credential=credential)
    except Exception as e:
        logger.warning("Failed to create Document Intelligence client: %s", e)
        return None


## Removed local _load_receipt_bytes in favor of centralized load_receipt_bytes


def _analyze_with_docintel(files: List[Dict[str, Any]]):
    """Analyze receipts or invoices with Azure Document Intelligence.

    Improvements vs previous version:
      - Supports selectable model via AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID (default prebuilt-receipt)
      - Adds resilient retry (3 attempts, exponential backoff) for transient Azure errors
      - Normalizes field/value extraction storing value + confidence mirroring sample reference
      - Expands field mapping to cover both receipt & invoice models (merchant/vendor, totals, dates)
      - Embeds model_id & version info inside debug_fields for observability
    """
    client = _get_docintel_client()
    model_id = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID", "prebuilt-receipt").strip() or "prebuilt-receipt"

    if not client:
        logger.error("Document Intelligence client unavailable; falling back")
        fallback = _filename_heuristic(files)
        for f in fallback:
            f["status"] = "error_docintel_unavailable"
            f["error_message"] = "Document Intelligence client unavailable (check endpoint/key or managed identity)."
            f.setdefault("debug_fields", {})["model_id"] = model_id
        return fallback

    transient_phrases = ("timeout", "temporarily", "again later", "throttle", "limit")
    results: List[Dict[str, Any]] = []

    merchant_candidates = ["MerchantName", "VendorName", "SupplierName", "CustomerName", "Merchant"]
    amount_candidates = [
        "Total", "GrandTotal", "InvoiceTotal", "AmountDue", "SubTotal", "Subtotal", "TotalTaxInclusive"
    ]
    date_candidates = [
        "TransactionDate", "PurchaseDate", "InvoiceDate", "Date", "ServiceStartDate", "ServiceEndDate", "DueDate"
    ]

    for meta in files:
        raw_fields: Dict[str, Dict[str, Any]] = {}
        merchant = None
        vendor_name = None
        amount: Optional[float] = None
        date_val: Optional[str] = None
        service_start: Optional[str] = None
        service_end: Optional[str] = None
        chosen_sources: Dict[str, str] = {}

        def _parse_date_general(val: str | None):
            if not val or not isinstance(val, str):
                return None
            token = val.strip().split()[0]
            token = token.replace('.', '-')
            token_norm = token.replace('/', '-').strip()
            from datetime import datetime
            patterns = ["%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y"]
            for p in patterns:
                try:
                    dt_obj = datetime.strptime(token_norm, p)
                    y = dt_obj.year
                    if y < 100:
                        if y <= 79:
                            y += 2000
                        else:
                            y += 1900
                        dt_obj = dt_obj.replace(year=y)
                    return dt_obj.date().isoformat()
                except Exception:
                    pass
            try:
                return datetime.fromisoformat(token_norm).date().isoformat()
            except Exception:
                return None

        attempt = 0
        doc_result = None
        while attempt < 3:
            attempt += 1
            try:
                file_bytes = load_receipt_bytes(meta["stored_path"])
                if AnalyzeDocumentRequest:
                    poller = client.begin_analyze_document(
                        model_id,
                        AnalyzeDocumentRequest(bytes_source=file_bytes),  # type: ignore
                    )
                else:  # pragma: no cover
                    poller = client.begin_analyze_document(
                        model_id,
                        {"bytes_source": file_bytes},  # type: ignore
                    )
                doc_result = poller.result()
                break
            except FileNotFoundError as fnf:
                logger.error("DocIntel file missing for %s: %s", meta["original_filename"], fnf)
                fb = _filename_heuristic([meta])[0]
                fb["status"] = "error_file_not_found"
                fb["error_message"] = str(fnf)
                fb["debug_fields"] = {"model_id": model_id, "attempts": attempt}
                results.append(fb)
                doc_result = None
                break
            except AzureError as ae:  # noqa: PERF203
                msg = str(ae).lower()
                is_transient = any(p in msg for p in transient_phrases) and attempt < 3
                logger.warning(
                    "DocIntel analyze attempt %d failed for %s (transient=%s): %s", attempt, meta["original_filename"], is_transient, ae
                )
                if not is_transient:
                    fb = _filename_heuristic([meta])[0]
                    fb["status"] = "error_docintel_analyze"
                    fb["error_message"] = f"Azure Document Intelligence analyze error: {ae}"
                    fb["debug_fields"] = {"model_id": model_id, "attempts": attempt}
                    results.append(fb)
                    doc_result = None
                    break
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.error("DocIntel unexpected error for %s: %s", meta["original_filename"], e)
                fb = _filename_heuristic([meta])[0]
                fb["status"] = "error_docintel_exception"
                fb["error_message"] = f"Unexpected error during analyze: {e}"
                fb["debug_fields"] = {"model_id": model_id, "attempts": attempt}
                results.append(fb)
                doc_result = None
                break

        if not doc_result:
            continue

        try:
            documents = getattr(doc_result, 'documents', None)
            if documents:
                doc0 = documents[0]
                fields = getattr(doc0, 'fields', {}) or {}
                for fname, fval in fields.items():
                    try:
                        value = getattr(fval, 'value', None)
                        if hasattr(value, 'amount'):
                            value = getattr(value, 'amount', None)
                        raw_fields[fname] = {
                            "value": value,
                            "content": getattr(fval, 'content', None),
                            "confidence": getattr(fval, 'confidence', None),
                            "type": getattr(fval, 'type', None),
                        }
                    except Exception:
                        pass

                f_vendor = fields.get("VendorName")
                if f_vendor is not None:
                    with suppress(Exception):
                        _v = getattr(f_vendor, 'value', None)
                        if not _v:
                            _v = getattr(f_vendor, 'content', None)
                            if _v:
                                chosen_sources["extracted_vendor_name"] = "content"
                        if _v:
                            vendor_name = ' '.join(str(_v).split())
                            chosen_sources.setdefault("extracted_vendor_name", "value")
                for key in merchant_candidates:
                    fobj = fields.get(key)
                    if fobj is not None:
                        with suppress(Exception):
                            candidate_val = getattr(fobj, 'value', None)
                            source = "value"
                            if not candidate_val:
                                candidate_val = getattr(fobj, 'content', None)
                                if candidate_val:
                                    source = "content"
                            if candidate_val:
                                merchant = ' '.join(str(candidate_val).split())
                                chosen_sources["extracted_merchant"] = source
                                break

                for key in amount_candidates:
                    fobj = fields.get(key)
                    if fobj is not None:
                        with suppress(Exception):
                            val = getattr(fobj, 'value', None)
                            source = "value"
                            if hasattr(val, 'amount'):
                                val = getattr(val, 'amount', None)
                            if val is None:
                                ctext = getattr(fobj, 'content', None)
                                if ctext:
                                    with suppress(Exception):
                                        cleaned = str(ctext).replace(',', '').strip()
                                        for token in cleaned.split():
                                            try:
                                                val = float(token)
                                                source = "content"
                                                break
                                            except Exception:
                                                pass
                            if val is not None:
                                try:
                                    amount = float(val)
                                    chosen_sources["extracted_amount"] = source
                                except Exception:
                                    pass
                                if amount is not None:
                                    break

                for key in date_candidates:
                    fobj = fields.get(key)
                    if fobj is not None:
                        with suppress(Exception):
                            dval = getattr(fobj, 'value', None)
                            source = "value"
                            if not dval:
                                dval = getattr(fobj, 'content', None)
                                if dval:
                                    source = "content"
                            if dval:
                                if hasattr(dval, 'isoformat'):
                                    dval = dval.isoformat()
                                norm_d = ' '.join(str(dval).split())
                                if key == "ServiceStartDate":
                                    service_start = norm_d
                                    chosen_sources.setdefault("extracted_service_start", source)
                                if key == "ServiceEndDate":
                                    service_end = norm_d
                                    chosen_sources.setdefault("extracted_service_end", source)
                                if not date_val:
                                    date_val = norm_d
                                    chosen_sources.setdefault("extracted_date", source)
        except Exception as e:
            logger.debug("Field extraction issue for %s: %s", meta["original_filename"], e)

        if date_val and len(date_val) > 25:
            date_val = date_val[:25]

        date_val_iso = _parse_date_general(date_val) or date_val
        service_start_iso = _parse_date_general(service_start) or service_start
        service_end_iso = _parse_date_general(service_end) or service_end

        results.append({
            "original_filename": meta["original_filename"],
            "stored_path": meta["stored_path"],
            "content_type": meta.get("content_type"),
            "extracted_merchant": merchant,
            "extracted_amount": amount,
            "extracted_date": date_val_iso or dt.date.today().isoformat(),
            "extracted_vendor_name": vendor_name,
            "extracted_service_start": service_start_iso,
            "extracted_service_end": service_end_iso,
            "status": "extracted",
            "debug_fields": {
                "model_id": model_id,
                "raw_fields": raw_fields,
                "attempts": attempt,
                "chosen_sources": chosen_sources,
            },
            "error_message": None,
        })
    return results


def _analyze_with_content_understanding(files: List[Dict[str, Any]]):
    """Real Azure Content Understanding call (analyze) per file.

    Expected env vars:
      AZURE_CONTENT_UNDERSTANDING_ENDPOINT
      AZURE_CONTENT_UNDERSTANDING_KEY
      AZURE_CONTENT_UNDERSTANDING_ANALYZER_ID (default prebuilt-documentAnalyzer)
      AZURE_CONTENT_UNDERSTANDING_API_VERSION (default 2024-11-01-preview)
    """
    endpoint = os.getenv("AZURE_CONTENT_UNDERSTANDING_ENDPOINT")
    key = os.getenv("AZURE_CONTENT_UNDERSTANDING_KEY")
    analyzer_id = os.getenv("AZURE_CONTENT_UNDERSTANDING_ANALYZER_ID", "prebuilt-documentAnalyzer")
    api_version = os.getenv("AZURE_CONTENT_UNDERSTANDING_API_VERSION", "2024-11-01-preview")

    if not endpoint or not key:
        logger.error("Content Understanding endpoint/key missing; fallback engaged")
        base = _filename_heuristic(files)
        for b in base:
            b["status"] = "error_cu_missing_config"
            b["debug_fields"] = {"note": "Missing endpoint or key for Content Understanding"}
            b["error_message"] = "Content Understanding config missing. Populate AZURE_CONTENT_UNDERSTANDING_ENDPOINT and KEY."
        return base

    headers_common = {
        "Ocp-Apim-Subscription-Key": key,
        "x-ms-useragent": "expense-agent/0.1",
    }

    def _submit_and_poll(path: str) -> Dict[str, Any]:
        url = f"{endpoint.rstrip('/')}/contentunderstanding/analyzers/{analyzer_id}:analyze?api-version={api_version}"
        data = load_receipt_bytes(path)
        resp = requests.post(
            url,
            headers={**headers_common, "Content-Type": "application/octet-stream"},
            data=data,
            timeout=60,
        )
        resp.raise_for_status()
        op_loc = resp.headers.get("operation-location")
        if not op_loc:
            raise RuntimeError("operation-location header missing")
        start = time.time()
        while True:
            poll = requests.get(op_loc, headers=headers_common, timeout=30)
            poll.raise_for_status()
            body = poll.json()
            status = (body.get("status") or "").lower()
            if status == "succeeded":
                return body
            if status == "failed":
                raise RuntimeError(f"Content Understanding analyze failed: {body}")
            if time.time() - start > 120:
                raise TimeoutError("Content Understanding analyze timeout >120s")
            time.sleep(2)

    results = []
    for meta in files:
        full_payload: Dict[str, Any] = {}
        merchant: Optional[str] = None
        amount: Optional[float] = None
        date_val: Optional[str] = None
        try:
            full_payload = _submit_and_poll(meta["stored_path"])

            def _walk(o):
                nonlocal merchant, amount, date_val
                if isinstance(o, dict):
                    for k, v in o.items():
                        kl = k.lower()
                        if merchant is None and kl in {"merchant", "merchantname", "vendor", "supplier", "merchant_name"}:
                            with suppress(Exception):
                                if isinstance(v, (str, int, float)):
                                    merchant = str(v)
                                elif isinstance(v, dict):
                                    val = v.get("value") or v.get("text") or v.get("content")
                                    if val:
                                        merchant = str(val)
                        if amount is None and kl in {"total", "grandtotal", "amountdue", "subtotal", "amount"}:
                            with suppress(Exception):
                                if isinstance(v, (int, float, str)):
                                    try:
                                        amount = float(str(v).replace(',', ''))
                                    except Exception:
                                        pass
                                elif isinstance(v, dict):
                                    val = v.get("value") or v.get("amount") or v.get("text")
                                    if val:
                                        try:
                                            amount = float(str(val).replace(',', ''))
                                        except Exception:
                                            pass
                        if date_val is None and kl in {"date", "transactiondate", "purchasedate", "invoicedate", "receiptdate"}:
                            with suppress(Exception):
                                if isinstance(v, str):
                                    date_val = v
                                elif isinstance(v, dict):
                                    val = v.get("value") or v.get("text")
                                    if val:
                                        date_val = val
                    for v2 in o.values():
                        _walk(v2)
                elif isinstance(o, list):
                    for i in o:
                        _walk(i)

            _walk(full_payload)
        except Exception as e:
            logger.error("Content Understanding analyze failed for %s: %s", meta["original_filename"], e)
            heuristic = _filename_heuristic([meta])[0]
            heuristic["status"] = "error_cu_analyze"
            heuristic["debug_fields"] = {"error": str(e)}
            heuristic["error_message"] = f"Content Understanding analyze failed: {e}"
            results.append(heuristic)
            continue

        # Normalize date
        if date_val and isinstance(date_val, str):
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
                with suppress(Exception):
                    parsed = dt.datetime.strptime(date_val[:10], fmt)
                    date_val = parsed.date().isoformat()
                    break
        elif date_val and not isinstance(date_val, str):
            with suppress(Exception):
                date_val = date_val.isoformat()

        results.append({
            "original_filename": meta["original_filename"],
            "stored_path": meta["stored_path"],
            "content_type": meta.get("content_type"),
            "extracted_merchant": merchant,
            "extracted_amount": float(amount) if amount is not None else None,
            "extracted_date": date_val or dt.date.today().isoformat(),
            "status": "extracted_content_understanding",
            "debug_fields": full_payload,
            "error_message": None,
        })
    return results


def extract_from_receipts(files: List[Dict[str, Any]], provider: str):
    provider = (provider or "").lower()
    if provider == PROVIDER_DOCUMENT_INTEL:
        return _analyze_with_docintel(files)
    if provider == PROVIDER_CONTENT_UNDER:
        return _analyze_with_content_understanding(files)
    if provider == PROVIDER_GPT5_NANO:
        return _analyze_with_gpt5_nano(files)
    # default fallback
    fb = _filename_heuristic(files)
    return fb


def _analyze_with_gpt5_nano(files: List[Dict[str, Any]]):
    """Use Azure OpenAI (gpt-5-nano deployment) to extract key invoice fields.

    Strategy:
      - Convert each image to base64 (small receipts). (For PDFs or large images this may need chunking.)
      - Provide system prompt instructing model to output strict JSON list with one object per input file.
      - Extract fields: merchant_name, vendor_name, date (or date_range), total_value, currency, and attempt a service_start/service_end if a range.
      - Return normalized schema consistent with other providers (extracted_merchant, extracted_amount, extracted_date,...)
    Env Vars:
      AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY / (MSI), AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION
    """
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
    temperature = float(os.getenv("AZURE_OPENAI_TEMPERATURE", "0"))
    key = os.getenv("AZURE_OPENAI_KEY")

    if not endpoint or not deployment:
        logger.error("Azure OpenAI endpoint or deployment missing; using heuristic fallback")
        base = _filename_heuristic(files)
        for b in base:
            b["status"] = "error_gpt5_missing_config"
            b["error_message"] = "Azure OpenAI configuration missing (AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_DEPLOYMENT)."
        return base

    # NOTE: Image Handling (Data URI)
    # --------------------------------
    # Azure OpenAI chat/completions multimodal messages expect image parts formatted as:
    #   {"type": "image_url", "image_url": {"url": "data:<mime>;base64,<encoded>"}}
    # We embed the receipt images directly as base64 data URIs to avoid having to host them
    # or generate SAS URLs. This is suitable for small receipt images (< ~1MB each). For larger
    # documents: consider resizing/compression or using a temporary blob store to keep request
    # payload sizes manageable and reduce latency.
    # If additional formats (PDF, TIFF) appear, add conversion before base64 encoding.

    # Build API client
    client: Optional["AzureOpenAI"] = None
    try:
        # OpenAI SDK for Azure expects azure_endpoint + api_key (or azure_ad_token)
        if key:
            client = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
        else:
            # Attempt to retrieve token via Managed Identity
            token = DefaultAzureCredential(exclude_interactive_browser_credential=True).get_token("https://cognitiveservices.azure.com/.default")
            client = AzureOpenAI(azure_endpoint=endpoint, azure_ad_token=token.token, api_version=api_version)
    except Exception as e:
        logger.error("Failed to create Azure OpenAI client: %s", e)
        fallback = _filename_heuristic(files)
        for f in fallback:
            f["status"] = "error_gpt5_client"
            f["error_message"] = f"Azure OpenAI client init failed: {e}"
        return fallback

    # Prepare images and minimal metadata for LLM
    image_payloads: List[Tuple[str, str, str]] = []  # (filename, mime, base64data)
    for meta in files:
        try:
            raw = load_receipt_bytes(meta["stored_path"])
            # rudimentary mime detection (PNG/JPEG) fallback to application/octet-stream -> default to image/png in prompt
            mime = "image/png"
            if raw.startswith(b"\x89PNG"):
                mime = "image/png"
            elif raw[6:10] in (b"JFIF", b"Exif") or raw.startswith(b"\xFF\xD8"):
                mime = "image/jpeg"
            b64 = base64.b64encode(raw).decode("utf-8")
            image_payloads.append((meta["original_filename"], mime, b64))
        except Exception as e:
            logger.warning("Failed to load file %s for LLM: %s", meta["original_filename"], e)
            heur = _filename_heuristic([meta])[0]
            heur["status"] = "error_gpt5_file_read"
            heur["error_message"] = f"Could not read file for GPT extraction: {e}"
            return [heur]

    system_prompt = (
        "You are an AI assistant that helps extract useful information from invoices and receipts. "
        "For each provided image, extract fields: merchant-name (or vendor-name), date (or date-range), total-value, "
        "currency (3-letter if present), and if a service period is shown include service_start and service_end (ISO). "
        "Return strictly valid JSON array where each element has: filename, merchant_name, vendor_name, total_value, "
        "currency, date, service_start, service_end, notes (optional). If both merchant and vendor are present keep both. "
        "Prefer numeric total_value without currency symbols. Dates should be YYYY-MM-DD if parseable; for ranges either fill date with earliest date and service_start/service_end accordingly."
    )

    # Build multi-part user content using the new schema:
    # Each image must be represented as a content part with type "image_url" and
    # an object {"image_url": {"url": "data:<mime>;base64,<...>"}}. We default
    # to PNG mime; if future enhancement is needed we can inspect magic bytes.
    user_message_content: List[Dict[str, Any]] = [
        {"type": "text", "text": "Extract structured data from these receipt images and output JSON as specified."}
    ]
    user_message_content.extend([
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}} for _, mime, b64 in image_payloads
    ])

    # The OpenAI SDK expects an iterable of message dicts; add type ignore to placate static checker.
    messages = [  # type: ignore[var-annotated]
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message_content},
    ]

    # Invoke chat completion (multi-modal) - azure.ai.openai v1 pattern
    if client is None:  # defensive, though earlier return should catch
        fb = _filename_heuristic(files)
        for f in fb:
            f["status"] = "error_gpt5_client_none"
            f["error_message"] = "Azure OpenAI client unexpectedly None"
        return fb

    try:
        response = client.chat.completions.create(  # type: ignore[arg-type]
            model=deployment,
            temperature=temperature,
            max_completion_tokens=800,
            messages=messages,
        )
    except Exception as e:
        logger.error("Azure OpenAI chat completion failed: %s", e)
        fb = _filename_heuristic(files)
        for f in fb:
            f["status"] = "error_gpt5_completion"
            f["error_message"] = f"Azure OpenAI chat failure: {e}"
        return fb

    raw_text: Optional[str] = None
    try:
        choice0 = response.choices[0]
        raw_text = getattr(choice0.message, "content", None)  # type: ignore[attr-defined]
        if raw_text is None:
            raise ValueError("No message content returned from model")
    except Exception as e:
        logger.error("Unexpected Azure OpenAI response format: %s", e)
        fb = _filename_heuristic(files)
        for f in fb:
            f["status"] = "error_gpt5_response"
            f["error_message"] = f"Azure OpenAI response parse error: {e}"
        return fb

    parsed: List[Dict[str, Any]] = []
    try:
        text_clean = raw_text.strip() if raw_text else ""
        if text_clean.startswith("```"):
            lines = text_clean.splitlines()[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text_clean = "\n".join(lines)
        parsed = json.loads(text_clean)
        if not isinstance(parsed, list):
            raise ValueError("Top-level JSON not a list")
    except Exception as e:
        logger.error("Failed to parse GPT JSON: %s | raw=%s", e, (raw_text or "")[:400])
        fb = _filename_heuristic(files)
        for f in fb:
            f["status"] = "error_gpt5_json"
            f["error_message"] = f"Azure OpenAI JSON parse error: {e}"
            f.setdefault("debug_fields", {})["raw_text"] = (raw_text or "")[:400]
        return fb

    # Build mapping from filename to extracted structure
    mapping: Dict[str, Dict[str, Any]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        fname = item.get("filename") or item.get("file") or item.get("image")
        if not fname:
            continue
        mapping[str(fname)].update(item) if fname in mapping else mapping.setdefault(str(fname), item)

    results = []
    for meta in files:
        raw_item = mapping.get(meta["original_filename"]) or {}
        merchant = raw_item.get("merchant_name") or raw_item.get("merchant")
        vendor = raw_item.get("vendor_name") or raw_item.get("vendor")
        total_value = raw_item.get("total_value") or raw_item.get("total") or raw_item.get("amount")
        currency = raw_item.get("currency")
        service_start = raw_item.get("service_start")
        service_end = raw_item.get("service_end")
        date_val = raw_item.get("date") or raw_item.get("invoice_date")
        # Normalize amount
        amount = None
        if total_value is not None:
            with suppress(Exception):
                amount = float(str(total_value).replace(",", "").replace(currency or "", ""))
        # Normalize date simple: keep as-is (other pipeline will attempt parse)
        results.append({
            "original_filename": meta["original_filename"],
            "stored_path": meta["stored_path"],
            "content_type": meta.get("content_type"),
            "extracted_merchant": merchant,
            "extracted_vendor_name": vendor,
            "extracted_amount": amount,
            "extracted_date": date_val or dt.date.today().isoformat(),
            "extracted_service_start": service_start,
            "extracted_service_end": service_end,
            "status": "extracted_gpt5_nano",
            "debug_fields": {"raw_item": raw_item, "currency": currency, "model": "gpt-5-nano", "raw_text_truncated": raw_text[:400] if raw_text else None},
            "error_message": None
        })
    return results
