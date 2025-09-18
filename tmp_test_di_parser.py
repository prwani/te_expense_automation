"""Utility script to analyze a document (invoice/hotel folio) via Azure Document Intelligence.

Input sources:
    1. --blob-url for a document stored in Azure Blob Storage.
             - If the URL contains a SAS (sig=) it is submitted directly (url_source).
             - If it is a private blob URL WITHOUT SAS and the extension is one of (.png,.jpg,.jpeg,.tif,.tiff,.pdf),
                 the script automatically downloads the bytes using DefaultAzureCredential and submits bytes_source.
             - Set FORCE_URL_MODE=1 to force url submission even for private blobs.
    2. --file local JSON response (offline testing of the extractor logic).

Extraction: uses shared `_extract_di_items` for consistent line item parsing.

Environment loading:
    If a `.env` file exists in the project root (current working directory when invoking the script),
    it is automatically loaded via python-dotenv before reading variables.

Environment variables:
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT   (required for live call)
    AZURE_DOCUMENT_INTELLIGENCE_KEY        (optional; if omitted DefaultAzureCredential is used)
    AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID   (optional, defaults to prebuilt-invoice)
    FORCE_URL_MODE=1                       (optional, force url submission even if private blob)

Usage examples:
    python tmp_test_di_parser.py --blob-url "https://acct.blob.core.windows.net/container/file.png"
    python tmp_test_di_parser.py --blob-url "https://acct.blob.core.windows.net/container/file.pdf?<SAS>" --dump-json
    python tmp_test_di_parser.py --file sample_response.json

Outputs human-readable item list plus final single-line JSON {"count":..., "items": [...] }.

Fallback logic:
    If a URL submission fails because the service cannot download the file (InvalidContent / could not download),
    the script automatically retries by downloading the blob with DefaultAzureCredential and resubmitting as bytes.

Debugging:
    Use --verbose for additional exception diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from backend.app.routers.expenses import _extract_di_items, _to_jsonable

# Auto load .env early
try:  # pragma: no cover (simple side-effect)
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()  # Loads from .env in current working directory if present
except Exception:
    pass


def _download_blob_bytes(blob_url: str) -> bytes:
    """Download a blob (no SAS) using DefaultAzureCredential. Supports https://<account>.blob.core.windows.net/container/name.
    Returns the raw bytes. Raises on failure.
    """
    try:
        from azure.storage.blob import BlobClient  # type: ignore
        from azure.identity import DefaultAzureCredential  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"Azure Storage imports failed: {e}") from e
    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    blob_client = BlobClient.from_blob_url(blob_url, credential=cred)
    downloader = blob_client.download_blob()
    return downloader.readall()


def analyze_via_blob_url(blob_url: str, model_id: str | None = None, verbose: bool = False) -> Any:
    """Analyze a document referenced by blob_url.

    Behavior:
      - If URL appears to have a SAS token (contains '?' with 'sig=') OR caller sets FORCE_URL_MODE=1, submit url_source.
      - Else if file extension indicates image/pdf and no SAS token, attempt to download with DefaultAzureCredential and send bytes_source.
      - Fallbacks gracefully between SDK request object and raw dict payload.
    """
    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient  # type: ignore
        from azure.core.credentials import AzureKeyCredential  # type: ignore
        from azure.identity import DefaultAzureCredential  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"Azure SDK imports failed: {e}") from e

    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    if not endpoint:
        raise RuntimeError("Environment variable AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is required for live analysis")
    if not model_id:
        model_id = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID", "prebuilt-invoice")

    if key:
        credential = AzureKeyCredential(key)
    else:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    client = DocumentIntelligenceClient(endpoint=endpoint, credential=credential)

    force_url = os.getenv("FORCE_URL_MODE") == "1"
    lower = blob_url.lower()
    has_sas = ("?" in blob_url and "sig=" in blob_url.lower())
    ext = os.path.splitext(lower.split("?")[0])[1]
    use_bytes = (ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf"}) and not has_sas and not force_url

    print(f"[info] analyze_via_blob_url mode ext={ext} has_sas={has_sas} force_url={force_url} use_bytes={use_bytes}")

    payload_kind = "url"
    payload: Any
    if use_bytes:
        try:
            blob_bytes = _download_blob_bytes(blob_url)
            print(f"[info] Downloaded blob bytes size={len(blob_bytes)}")
            payload_kind = "bytes"
        except Exception as e:
            print(f"[warn] Failed to download blob for bytes mode: {e}; falling back to url submission")
            blob_bytes = None
        if blob_bytes:
            try:
                from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # type: ignore
                payload = AnalyzeDocumentRequest(bytes_source=blob_bytes)
            except Exception as e:
                print(f"[warn] AnalyzeDocumentRequest bytes path failed: {e}; fallback dict")
                payload = {"bytes_source": blob_bytes}
        else:
            payload = {"url_source": blob_url}
    else:
        try:
            from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # type: ignore
            payload = AnalyzeDocumentRequest(url_source=blob_url)
        except Exception as e:
            print(f"[warn] AnalyzeDocumentRequest url path failed: {e}; fallback dict")
            payload = {"url_source": blob_url}

    try:
        poller = client.begin_analyze_document(model_id, payload)
        print(f"[info] Submitted analysis with payload_kind={payload_kind}")
        return poller.result()
    except Exception as e:
        msg = str(e)
        if verbose:
            print(f"[debug] Initial analyze exception: {repr(e)}")
        # If URL submission failed because service could not download, fallback to bytes attempt
        download_fail = any(token in msg.lower() for token in ["could not download", "invalidcontent", "not accessible", "download the file"])
        if payload_kind == "url" and download_fail:
            print("[info] URL submission failed due to download issue; attempting local download + bytes submission fallback")
            try:
                blob_bytes = _download_blob_bytes(blob_url)
                print(f"[info] Fallback downloaded blob bytes size={len(blob_bytes)}")
                try:
                    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # type: ignore
                    payload2 = AnalyzeDocumentRequest(bytes_source=blob_bytes)
                except Exception as e2:
                    if verbose:
                        print(f"[debug] Secondary AnalyzeDocumentRequest import/build failed: {e2}")
                    payload2 = {"bytes_source": blob_bytes}
                poller2 = client.begin_analyze_document(model_id, payload2)
                print("[info] Submitted fallback bytes analysis")
                return poller2.result()
            except Exception as e2:
                if verbose:
                    print(f"[error] Fallback bytes submission also failed: {e2}")
        raise


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_items(raw_result: Any) -> list[dict]:
    try:
        return _extract_di_items(raw_result)
    except Exception as e:  # pragma: no cover (defensive)
        print(f"[error] Extraction failed: {e}")
        return []


def _fallback_parse_from_content(raw_result: Any) -> list[dict]:
    """Heuristic fallback when structured Items not found.

    Strategy:
      - Obtain the big concatenated content field (if present) or join page words.
      - Split into lines.
      - Look for date-prefixed lines (DD-MM-YY or DD-MMM-YY) capturing description and following amount line.
      - Capture tax lines (CGST/SGST/Room Service/etc.).
      - Normalize amounts by stripping commas.
    This is intentionally conservative to avoid inventing data.
    """
    text_blob = None
    # Attempt root['content']
    try:
        if isinstance(raw_result, dict):
            text_blob = raw_result.get('content') or raw_result.get('analyzeResult', {}).get('content')
    except Exception:
        pass
    if text_blob is None:
        # Try documents[0].content maybe (SDK objects not deeply converted) - reuse _to_jsonable
        try:
            serial = _to_jsonable(raw_result)
            if isinstance(serial, dict):
                text_blob = serial.get('content') or serial.get('analyzeResult', {}).get('content')
        except Exception:
            pass
    if not text_blob or not isinstance(text_blob, str):
        return []
    lines = [l.strip() for l in text_blob.splitlines() if l.strip()]
    import re
    date_pattern = re.compile(r"^(\d{2}-[A-Z]{3}-\d{2}|\d{2}-\d{2}-\d{2,4})\b")
    amount_pattern = re.compile(r"^[0-9][0-9,]*\.[0-9]{2}$")
    items: list[dict] = []
    for i, line in enumerate(lines):
        if not date_pattern.match(line):
            continue
        # Combine with next 1-2 lines to find amount at end
        desc_parts = [line]
        look_ahead_limit = 3
        amount_val = None
        item_date = line.split()[0]
        for j in range(1, look_ahead_limit + 1):
            if i + j >= len(lines):
                break
            nxt = lines[i + j]
            if amount_pattern.match(nxt.replace(',', '')):
                # Amount line encountered
                amt_txt = nxt.replace(',', '')
                try:
                    amount_val = float(amt_txt)
                except Exception:
                    pass
                break
            # If a potential description continuation (no date at start and not empty)
            if not date_pattern.match(nxt):
                desc_parts.append(nxt)
        if amount_val is None:
            continue
        # Build description from non-date tokens (skip the first date token on first line)
        first_line_tokens = desc_parts[0].split()
        if len(first_line_tokens) > 1:
            first_line_tokens = first_line_tokens[1:]
        description_tokens = first_line_tokens
        for extra_line in desc_parts[1:]:
            description_tokens.extend(extra_line.split())
        description = ' '.join(description_tokens)[:200]
        if not description:
            continue
        items.append({
            'description': description,
            'amount': amount_val,
            'item_date': item_date,
        })
    # Deduplicate by (desc, amount, date) preserving order
    seen = set()
    deduped: list[dict] = []
    for it in items:
        key = (it['description'], it['amount'], it['item_date'])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    return deduped


def _deep_serialize(obj: Any, *, max_depth: int = 50, _depth: int = 0, _seen: set[int] | None = None):
    """Very deep serialization for debugging.
    - Large max_depth default to capture essentially everything.
    - Cycle protection via object id set.
    - Attempts SDK to_dict() style conversions first.
    - Falls back to attribute enumeration.
    NOTE: This can produce very large output for complex responses.
    """
    if _seen is None:
        _seen = set()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    oid = id(obj)
    if oid in _seen:
        return "<cycle>"
    _seen.add(oid)
    if _depth >= max_depth:
        return "<max_depth>"
    # List/Tuple
    if isinstance(obj, (list, tuple, set)):
        return [_deep_serialize(v, max_depth=max_depth, _depth=_depth + 1, _seen=_seen) for v in obj]
    # Dict
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            try:
                out[str(k)] = _deep_serialize(v, max_depth=max_depth, _depth=_depth + 1, _seen=_seen)
            except Exception as e:  # pragma: no cover
                out[str(k)] = f"<error:{e}>"
        return out
    # Try common conversion methods
    for m in ("to_dict", "as_dict", "as_json"):
        try:
            if hasattr(obj, m) and callable(getattr(obj, m)):
                return _deep_serialize(getattr(obj, m)(), max_depth=max_depth, _depth=_depth + 1, _seen=_seen)
        except Exception:
            pass
    # Use __dict__
    try:
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict) and d:
            return {
                str(k): _deep_serialize(v, max_depth=max_depth, _depth=_depth + 1, _seen=_seen)
                for k, v in d.items()
                if not k.startswith("__")
            }
    except Exception:
        pass
    # Fallback: enumerate attributes (slots, etc.)
    try:
        attrs = [a for a in dir(obj) if not a.startswith("_")]
        snap: dict[str, Any] = {}
        for a in attrs:
            try:
                val = getattr(obj, a)
                if callable(val):
                    continue
                snap[a] = _deep_serialize(val, max_depth=max_depth, _depth=_depth + 1, _seen=_seen)
            except Exception as e:  # pragma: no cover
                snap[a] = f"<error:{e}>"
        if snap:
            return snap
    except Exception:
        pass
    # Last resort string
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze a document via Azure Document Intelligence and print extracted Items.")
    parser.add_argument("--blob-url", dest="blob_url", help="Blob/SAS URL of the document to analyze")
    parser.add_argument("--file", dest="file", help="Path to a local JSON response to parse instead of live call")
    parser.add_argument("--model-id", dest="model_id", help="Override model id (default: prebuilt-invoice)")
    parser.add_argument("--dump-json", dest="dump_json", action="store_true", help="Dump full raw result to response.json")
    parser.add_argument("--verbose", dest="verbose", action="store_true", help="Verbose debug logging including exceptions")
    args = parser.parse_args(argv)

    raw = None
    source = None
    if args.blob_url:
        source = f"blob:{args.blob_url[:60]}..."
        try:
            raw = analyze_via_blob_url(args.blob_url, args.model_id, verbose=args.verbose)
        except Exception as e:
            print(f"[error] Live analysis failed: {e}")
            if not args.file:
                return 2
    if raw is None and args.file:
        source = f"file:{args.file}"
        try:
            raw = load_json_file(args.file)
        except Exception as e:
            print(f"[error] Failed to load local file '{args.file}': {e}")
            return 3
    if raw is None:
        print("[error] No input provided. Use --blob-url or --file.")
        return 1

    # Always write deep response file
    try:
        deep = _deep_serialize(raw, max_depth=60)
        with open("response.json", "w", encoding="utf-8") as f:
            json.dump(deep, f, indent=2, ensure_ascii=False)
        print("[info] Wrote full deep response to response.json")
    except Exception as e:
        print(f"[warn] Failed deep serialization: {e}")

    items = extract_items(raw)
    print(f"Source: {source}")
    print(f"Extracted items count={len(items)}")
    for i, it in enumerate(items):
        print(f"  [{i}] desc={it.get('description')!r} amount={it.get('amount')} date={it.get('item_date')}")

    if not items:
        print("[info] No structured Items extracted; invoking fallback content parser")
        fb = _fallback_parse_from_content(raw)
        print(f"[info] Fallback produced {len(fb)} candidates")
        if fb:
            items = fb
            for i, it in enumerate(items[:10]):
                print(f"  [fb {i}] desc={it.get('description')!r} amount={it.get('amount')} date={it.get('item_date')}")

    # Always emit machine-readable output last (single line JSON)
    print(json.dumps({"count": len(items), "items": items}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
