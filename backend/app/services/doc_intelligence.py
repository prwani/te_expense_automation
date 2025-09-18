"""Azure Document Intelligence utility functions.

This module centralizes the logic to analyze a document (invoice/hotel folio)
using Azure Document Intelligence, mirroring the robust behavior implemented in
the standalone script `tmp_test_di_parser.py`:

- Accept a blob URL (with or without SAS). If SAS is present, submit URL directly.
- If URL is private (no SAS) and file extension suggests image/PDF, download
  bytes using DefaultAzureCredential and submit as bytes.
- If URL submission fails due to the service not being able to download the file,
  automatically retry by downloading bytes and resubmitting.
- Parse line items using the shared `_extract_di_items` to maintain consistency.

Environment variables:
  AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT (required)
  AZURE_DOCUMENT_INTELLIGENCE_KEY      (optional; if omitted uses DefaultAzureCredential)
  AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID (optional; defaults to prebuilt-invoice)
  FORCE_URL_MODE=1                     (optional; force URL submission and skip bytes optimization)

Optional deep dump:
  Callers can pass `deep_dump_path` to write a full-depth JSON dump of the raw
  analysis result for auditing/debugging (disabled by default).
"""
from __future__ import annotations

from typing import Any, Optional
import os
import json

# Note: avoid importing routers.expenses at module import time to prevent circular imports.
_EXTRACTOR_REF = None  # lazy holder to _extract_di_items


def _download_blob_bytes(blob_url: str) -> bytes:
    """Download a blob using DefaultAzureCredential and return raw bytes."""
    from azure.identity import DefaultAzureCredential  # type: ignore
    from azure.storage.blob import BlobClient  # type: ignore

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    blob_client = BlobClient.from_blob_url(blob_url, credential=cred)
    downloader = blob_client.download_blob()
    return downloader.readall()


def _deep_serialize(obj: Any, *, max_depth: int = 50, _depth: int = 0, _seen: set[int] | None = None):
    """Very deep serialization for debugging; cycle-safe and attribute-aware."""
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
    if isinstance(obj, (list, tuple, set)):
        return [_deep_serialize(v, max_depth=max_depth, _depth=_depth + 1, _seen=_seen) for v in obj]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            try:
                out[str(k)] = _deep_serialize(v, max_depth=max_depth, _depth=_depth + 1, _seen=_seen)
            except Exception as e:  # pragma: no cover
                out[str(k)] = f"<error:{e}>"
        return out
    for m in ("to_dict", "as_dict", "as_json"):
        try:
            if hasattr(obj, m) and callable(getattr(obj, m)):
                return _deep_serialize(getattr(obj, m)(), max_depth=max_depth, _depth=_depth + 1, _seen=_seen)
        except Exception:
            pass
    try:
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict) and d:
            return {str(k): _deep_serialize(v, max_depth=max_depth, _depth=_depth + 1, _seen=_seen) for k, v in d.items() if not str(k).startswith("__")}
    except Exception:
        pass
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
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


def analyze_document_from_blob_url(
    blob_url: str,
    *,
    model_id: Optional[str] = None,
    deep_dump_path: Optional[str] = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Analyze a document via Azure Document Intelligence and return parsed items and raw.

    Returns a dict with keys:
      - items: list[dict] of extracted items {description, amount, item_date}
      - raw:   the raw SDK result (not JSON-serialized)

    The function reads endpoint/key/model from environment variables documented above.
    """
    from azure.ai.documentintelligence import DocumentIntelligenceClient  # type: ignore
    from azure.core.credentials import AzureKeyCredential  # type: ignore
    from azure.identity import DefaultAzureCredential  # type: ignore

    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    if not endpoint:
        raise RuntimeError("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is required")
    if not model_id:
        model_id = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID", "prebuilt-invoice")

    credential: Any
    if key:
        credential = AzureKeyCredential(key)
    else:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    client = DocumentIntelligenceClient(endpoint=endpoint, credential=credential)

    force_url = os.getenv("FORCE_URL_MODE") == "1"
    lower = blob_url.lower()
    has_sas = ("?" in lower and "sig=" in lower)
    ext = os.path.splitext(lower.split("?")[0])[1]
    use_bytes = (ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf"}) and not has_sas and not force_url

    payload: Any
    payload_kind = "url"
    if use_bytes:
        try:
            blob_bytes = _download_blob_bytes(blob_url)
            payload_kind = "bytes"
        except Exception as e:
            if verbose:
                print(f"[docint] Could not pre-download blob, falling back to URL: {e}")
            blob_bytes = None
        if blob_bytes is not None:
            try:
                from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # type: ignore
                payload = AnalyzeDocumentRequest(bytes_source=blob_bytes)
            except Exception:
                payload = {"bytes_source": blob_bytes}
        else:
            try:
                from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # type: ignore
                payload = AnalyzeDocumentRequest(url_source=blob_url)
            except Exception:
                payload = {"url_source": blob_url}
    else:
        try:
            from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # type: ignore
            payload = AnalyzeDocumentRequest(url_source=blob_url)
        except Exception:
            payload = {"url_source": blob_url}

    try:
        poller = client.begin_analyze_document(model_id, payload)
        raw_result = poller.result()
    except Exception as e:
        msg = str(e).lower()
        download_fail = any(tok in msg for tok in ["could not download", "invalidcontent", "not accessible", "download the file"])
        if payload_kind == "url" and download_fail:
            if verbose:
                print("[docint] URL analysis failed due to service download; retrying with bytes...")
            blob_bytes = _download_blob_bytes(blob_url)
            try:
                from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # type: ignore
                payload2 = AnalyzeDocumentRequest(bytes_source=blob_bytes)
            except Exception:
                payload2 = {"bytes_source": blob_bytes}
            poller2 = client.begin_analyze_document(model_id, payload2)
            raw_result = poller2.result()
        else:
            raise

    # Optional deep dump
    if deep_dump_path:
        try:
            deep = _deep_serialize(raw_result, max_depth=60)
            with open(deep_dump_path, "w", encoding="utf-8") as fh:
                json.dump(deep, fh, indent=2, ensure_ascii=False)
        except Exception as e:
            if verbose:
                print(f"[docint] Deep dump failed: {e}")

    global _EXTRACTOR_REF
    if _EXTRACTOR_REF is None:
        # Lazy import to avoid circular import during app startup
        from ..routers.expenses import _extract_di_items as __extract
        _EXTRACTOR_REF = __extract
    items = _EXTRACTOR_REF(raw_result)
    return {"items": items, "raw": raw_result}


__all__ = ["analyze_document_from_blob_url"]
