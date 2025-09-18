"""Unified receipt file loader for either Azure Blob Storage or local filesystem.

This utility abstracts how receipt image/PDF bytes are retrieved, centralizing
blob-versus-local logic so all services (extraction, itemization, download) use
consistent heuristics and error handling.

Heuristic for detecting a blob-stored receipt:
- Path is not absolute AND contains no path separators ('/' or '\\').
  We store only the blob name (UUID + original extension) in DB for new uploads.

Functions:
    load_receipt_bytes(stored_path: str) -> bytes
        Returns the raw bytes of the receipt. Raises FileNotFoundError if cannot
        be located in blob nor locally.

Environment / Dependencies:
- Relies on existing blob_storage.get_container_client() helper if Azure libs
  are installed and environment is configured. Falls back silently to local
  file access if blob download fails for any reason.
"""
from __future__ import annotations

import os
from contextlib import suppress
from typing import Optional

# Optional import of blob_storage helper; we keep it inside function scope to avoid
# import-time errors if azure libs are missing in certain execution contexts.


def _looks_like_blob_name(stored_path: str) -> bool:
    if not stored_path:
        return False
    if os.path.isabs(stored_path):
        return False
    if "/" in stored_path or "\\" in stored_path:
        return False
    return True


def load_receipt_bytes(stored_path: str) -> bytes:
    """Load receipt bytes from blob storage or local filesystem.

    Order of attempts:
        1. If heuristic indicates blob name, try blob download.
        2. Fallback to local filesystem open.

    Raises:
        FileNotFoundError: if neither blob nor local file read succeeded.
    """
    last_err: Optional[Exception] = None

    if _looks_like_blob_name(stored_path):
        with suppress(Exception):
            from . import blob_storage  # type: ignore
            container = blob_storage.get_container_client()
            downloader = container.download_blob(stored_path)
            return downloader.readall()

    # Local fallback
    try:
        with open(stored_path, "rb") as fh:
            return fh.read()
    except Exception as e:  # noqa: PERF203 (clarity over micro-perf)
        last_err = e

    raise FileNotFoundError(f"Receipt file not found: {stored_path} ({last_err})")

__all__ = ["load_receipt_bytes"]
