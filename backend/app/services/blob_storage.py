"""Azure Blob Storage helper using DefaultAzureCredential.

Environment Variables Required:
  AZURE_STORAGE_ACCOUNT_NAME   - Name of the storage account (no protocol)
  AZURE_STORAGE_CONTAINER_NAME - Container to store receipts/invoices (default: receipts)
Optional:
  AZURE_STORAGE_URL            - Full https://account.blob.core.windows.net override (if using national clouds)
  RECEIPT_BLOB_PUBLIC_BASE_URL - If container or CDN exposes public base URL for direct linking (optional)

Auth Strategy:
  Uses DefaultAzureCredential (Managed Identity, Visual Studio Code, Azure CLI login, etc.)
  No account key or connection string is used per requirement.

Functions:
  get_container_client() -> ContainerClient
  upload_bytes(data: bytes, blob_name: str, content_type: str|None) -> str (blob name)
  generate_download_url(blob_name: str) -> str (SAS-less direct URL; relies on private container + API download endpoint or public container)

If container is private (recommended), the API should proxy downloads via a FastAPI endpoint
that streams bytes. For large files, consider using async streaming (not critical for small receipts).

References:
  Azure SDK for Python Storage Blobs: https://learn.microsoft.com/python/api/overview/azure/storage-blob-readme
"""
from __future__ import annotations
import os
from functools import lru_cache
from typing import Optional
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

ACCOUNT_ENV = "AZURE_STORAGE_ACCOUNT_NAME"
CONTAINER_ENV = "AZURE_STORAGE_CONTAINER_NAME"
ALT_URL_ENV = "AZURE_STORAGE_URL"
PUBLIC_BASE_ENV = "RECEIPT_BLOB_PUBLIC_BASE_URL"

DEFAULT_CONTAINER = "receipts"

class BlobConfigError(RuntimeError):
    pass

@lru_cache(maxsize=1)
def _credential():
    # Exclude interactive browser to avoid hanging in server environments
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)

@lru_cache(maxsize=1)
def _service_client() -> BlobServiceClient:
    account_name = os.getenv(ACCOUNT_ENV)
    if not account_name:
        raise BlobConfigError(f"Missing {ACCOUNT_ENV} env var")
    base_url = os.getenv(ALT_URL_ENV) or f"https://{account_name}.blob.core.windows.net"
    return BlobServiceClient(account_url=base_url, credential=_credential())

@lru_cache(maxsize=1)
def get_container_client():
    container_name = os.getenv(CONTAINER_ENV, DEFAULT_CONTAINER)
    client = _service_client().get_container_client(container_name)
    # Ensure container exists (idempotent)
    try:
        client.create_container()
    except Exception:
        # Already exists or insufficient permission; ignore if exists
        pass
    return client

def upload_bytes(data: bytes, blob_name: str, content_type: Optional[str]=None) -> str:
    container = get_container_client()
    # Overwrite behavior: receipts are immutable; using random UUID names so collisions unlikely
    content_settings = None
    if content_type:
        content_settings = ContentSettings(content_type=content_type)
    container.upload_blob(name=blob_name, data=data, overwrite=True, content_settings=content_settings)
    return blob_name

def generate_download_url(blob_name: str) -> str:
    # If public base provided (e.g., CDN or public container) use it directly
    public_base = os.getenv(PUBLIC_BASE_ENV)
    if public_base:
        return f"{public_base.rstrip('/')}/{blob_name}"
    # Otherwise construct standard blob endpoint (may not be directly accessible if private)
    account_name = os.getenv(ACCOUNT_ENV, '')
    if not account_name:
        return blob_name
    container_name = os.getenv(CONTAINER_ENV, DEFAULT_CONTAINER)
    return f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_name}"
