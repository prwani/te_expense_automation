import os
import base64
import mimetypes
from typing import Tuple

import httpx
from dotenv import load_dotenv
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.storage.blob import BlobServiceClient
      
load_dotenv()  # Load .env variables

# Storage account / container from .env
ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
CONTAINER_NAME = os.getenv("AZURE_STORAGE_CONTAINER_NAME")


def fetch_blob_to_data_uri(blob_name: str, container: str, account_name: str | None) -> str:
    if not account_name:
        raise SystemExit(
            "AZURE_STORAGE_ACCOUNT_NAME not set. Please define it in your .env file."
        )
    if not container:
        raise SystemExit(
            "AZURE_STORAGE_CONTAINER_NAME not set. Please define it in your .env file."
        )
    account_url = f"https://{account_name}.blob.core.windows.net"
    credential = DefaultAzureCredential()
    try:
        service = BlobServiceClient(account_url=account_url, credential=credential)
        blob_client = service.get_blob_client(container=container, blob=blob_name)
        downloader = blob_client.download_blob()
        data = downloader.readall()
    except Exception as e:  # Broad on purpose to surface credential/blob errors succinctly
        raise SystemExit(
            f"Failed to download blob '{blob_name}' from container '{container}' in account '{account_name}': {e}"
        )

    # Derive MIME type (fallback to image/png if unknown)
    mime, _ = mimetypes.guess_type(blob_name)
    if not mime:
        mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _extract_text_from_message(msg) -> str:
    """Normalize message content (string or list of parts) into plain text."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):  # multimodal parts
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text", ""))
        return "".join(texts).strip()
    return ""


def extract_invoice_line_items(blob_name: str) -> str:
    """Entry point: Given a blob name, download image/PDF and return model's extracted line items JSON text.

    Environment variables used (via .env or shell):
      AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_VERSION, AZURE_OPENAI_DEPLOYMENT
      AZURE_STORAGE_ACCOUNT_NAME, AZURE_STORAGE_CONTAINER_NAME
    """
    # Resolve OpenAI settings (fallbacks preserved for backward compatibility)
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv(
        "ENDPOINT_URL", "https://pw-ai-foundry.openai.azure.com/"
    )
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv(
        "DEPLOYMENT_NAME", "gpt-4.1-mini"
    )
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )

    client = AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
    )

    data_uri = fetch_blob_to_data_uri(blob_name, CONTAINER_NAME, ACCOUNT_NAME)

    messages = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "You are expert in understanding hotel invoices. Given an invoice image or PDF, "
                        "extract only the debit (charge) line-items with positive amounts. Return STRICT JSON "
                        "array of objects with: date, description, reference (if any), debit (number)."
                    ),
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Invoice image follows."},
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": "Return JSON only."},
            ],
        },
    ]

    completion = client.chat.completions.create(
        model=deployment,
        messages=messages,
        max_tokens=4096,
        temperature=0.2,
        top_p=0.9,
        frequency_penalty=0,
        presence_penalty=0,
        stream=False,
    )

    for choice in completion.choices:
        if hasattr(choice, "message") and choice.message:
            extracted = _extract_text_from_message(choice.message)
            if extracted:
                return extracted
    return ""  # No content


if __name__ == "__main__":
    # Allow overriding via env; fallback to previous default blob name
    blob_name = os.getenv("BLOB_NAME", "f79da3cb94654da382db87daa77d4a98.png")
    result = extract_invoice_line_items(blob_name)
    print(result)
