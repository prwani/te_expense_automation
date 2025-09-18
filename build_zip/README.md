# Travel Expense Automation Agent

## Deploy to Azure App Service (ZIP)

This repo includes a simple script to provision Azure resources and deploy the app as a Python ZIP package to a Linux Web App.

Prerequisites:
- Azure CLI installed and logged in (az login)
- A subscription selected (az account set --subscription <SUB_ID>)
- Bash and zip installed

Steps:
1) From the repo root, run the script with your names:

```
./scripts/deploy_to_azure.sh <resource_group> <location> <appservice_plan> <webapp_name>
```

Example:

```
./scripts/deploy_to_azure.sh exp-demo-rg eastus exp-demo-plan exp-demo-web
```

What the script does:
- Creates an Azure Resource Group
- Creates a Linux App Service plan (B1)
- Creates or reuses a Python 3.10 Web App
- Sets the startup command to run `uvicorn backend.app.main:app` on port 8000
- Packages the entire repo and deploys it via `config-zip`

Notes:
- Dependencies: A root `requirements.txt` points to `backend/requirements.txt`, so Oryx builds on the platform.
- SQLite: The app uses an embedded SQLite DB at `backend/app/db/expenses.db`. App Service file system is ephemeral; if the app restarts or moves instances, the DB may be re-seeded at startup. For persistent data, consider Azure Files or a managed DB.
- Frontend: The `frontend/` folder is served by FastAPI from `/static` and HTML routes.
- Logs: Tail logs with:

```
az webapp log tail --name <webapp_name> --resource-group <resource_group>
```

FastAPI + SQLite prototype for uploading receipt images/PDFs, storing them in Azure Blob Storage (Managed Identity / DefaultAzureCredential – no account keys), extracting structured data via Azure Document Intelligence / Content Understanding / Azure OpenAI, proposing matches against existing expense rows, and confirming mappings for expense reporting.

## Current Feature Set
* SQLite schema: `expenses`, `receipts`, `expense_receipts`, `expense_reports`, `report_expenses`, `report_receipts`, `expense_items`
* Azure Blob Storage for all newly uploaded receipts (private container by default) via `DefaultAzureCredential`
* Preloaded sample expenses (seeded automatically)
* Multi-receipt upload (multipart)
* Multiple Azure extraction providers:
  * Document Intelligence (`prebuilt-receipt` or custom via env)
  * Content Understanding (async analyze endpoint with polling)
  * Azure OpenAI (`gpt5_nano` provider) – multi-image prompt producing structured JSON (official `openai` Python SDK w/ Azure endpoint)
* Heuristic filename fallback when Azure config missing or a provider errors
* Structured field extraction: merchant, vendor name, amount, date, optional service period
* Weighted matching (merchant fuzzy similarity + amount tolerance + date ±2 days decay)
* Expense reports: create reports, link expenses + receipts, mark expenses tagged, view report detail UI
* Line item (hotel) itemization using Document Intelligence items or heuristic nightly split
* Automatic best-match proposal per non-error receipt + manual confirmation
* Error propagation: extraction errors return `error_message` and suppress matching
* UI debug/info: raw `debug_fields`, error banners, clickable receipt download links
* Environment diagnostics endpoint `/env-check` (booleans only, no secrets)
* Centralized logging (LOG_LEVEL) + optional debug tracing

## Architecture Overview
* Backend: FastAPI (`backend/app`) with routers: `expenses`, `receipts`, `expense_reports`.
* Blob Storage Abstraction: `services/blob_storage.py` – creates container lazily, uploads bytes, generates deterministic blob names (UUID + ext). Uses `DefaultAzureCredential` only.
* Persistence: SQLite (configurable path), lightweight auto-migration for new columns/tables.
* Extraction Logic: `services/extraction.py`
  * Dispatch to providers (`document_intelligence`, `content_understanding`, `gpt5_nano`, heuristic)
  * Normalized schema with vendor/service period & debug metadata
* Matching: `services/matching.py` – fuzzy + numeric scoring, returns proposals
* Reports: `routers/expense_reports.py` – create/list reports, show joined expense/receipt/link data
* Itemization: `expenses/{id}/itemize` – attempts true line items via DI else heuristic nightly + tax split
* Frontend: Vanilla JS pages (`frontend/*.html`). `report-detail.html` now renders receipt filename as a clickable link invoking backend download endpoint (`/receipts/{id}/download`).
* Download Flow: Private container -> API streams bytes (no SAS exposure). If you later expose a public/CDN base URL, set `RECEIPT_BLOB_PUBLIC_BASE_URL` and link can point directly there without streaming.
* Logging: Structured with level from env.

## Installation & Run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.app.main:app --reload --port 8000
```
Open http://localhost:8000 (serves `index.html`) or directly open the file in a browser (for static assets). The API is also accessible under http://localhost:8000/docs.

## Environment Variables (.env)
| Variable | Purpose | Notes |
|----------|---------|-------|
| EXPENSE_DB_PATH | SQLite DB path | Defaults to backend/app/db/expenses.db |
| RECEIPT_UPLOAD_DIR | Fallback local storage directory | Used only if blob upload fails (debug) |
| AZURE_STORAGE_ACCOUNT_NAME | Storage account name | Required for blob storage |
| AZURE_STORAGE_CONTAINER_NAME | Blob container name | Default: receipts (auto-created) |
| AZURE_STORAGE_URL | Custom account URL | Optional (sovereign clouds) |
| RECEIPT_BLOB_PUBLIC_BASE_URL | Public base URL/CDN | Optional; if set links go direct (container must be public) |
| AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT | Doc Intelligence endpoint | e.g. https://<resource>.cognitiveservices.azure.com |
| AZURE_DOCUMENT_INTELLIGENCE_KEY | Doc Intelligence key | Omit to use DefaultAzureCredential |
| AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID | Doc Intelligence model id | prebuilt-receipt (default) or prebuilt-invoice etc. |
| AZURE_CONTENT_UNDERSTANDING_ENDPOINT | Content Understanding endpoint | Region/AI Foundry endpoint base (no trailing slash needed) |
| AZURE_CONTENT_UNDERSTANDING_KEY | Content Understanding key | Required for current REST usage |
| AZURE_CONTENT_UNDERSTANDING_ANALYZER_ID | Analyzer ID | Default: prebuilt-documentAnalyzer |
| AZURE_CONTENT_UNDERSTANDING_API_VERSION | API version | Default: 2024-11-01-preview |
| AZURE_OPENAI_ENDPOINT | Azure OpenAI endpoint | e.g. https://my-aoai.openai.azure.com |
| AZURE_OPENAI_KEY | Azure OpenAI key | Omit to use Managed Identity (if enabled) |
| AZURE_OPENAI_DEPLOYMENT | GPT deployment name | Must map to gpt-5-nano (or variant) |
| AZURE_OPENAI_API_VERSION | API version | Default: 2024-08-01-preview |
| AZURE_OPENAI_TEMPERATURE | Extraction creativity | Default 0 (deterministic) |
| LOG_LEVEL | Logging level | INFO (default), DEBUG for verbose |

Example `.env` snippet (including Azure OpenAI & Blob):
```bash
EXPENSE_DB_PATH=./backend/app/db/expenses.db
RECEIPT_UPLOAD_DIR=./backend/uploads
AZURE_STORAGE_ACCOUNT_NAME=myexpensefilestore
AZURE_STORAGE_CONTAINER_NAME=receipts
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://mydocintel.cognitiveservices.azure.com
AZURE_DOCUMENT_INTELLIGENCE_KEY=xxxxxxxxxxxxxxxx
AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID=prebuilt-receipt
AZURE_CONTENT_UNDERSTANDING_ENDPOINT=https://my-cu-westus.cognitiveservices.azure.com
AZURE_CONTENT_UNDERSTANDING_KEY=yyyyyyyyyyyyyyyy
AZURE_CONTENT_UNDERSTANDING_ANALYZER_ID=prebuilt-documentAnalyzer
LOG_LEVEL=INFO
AZURE_OPENAI_ENDPOINT=https://my-aoai.openai.azure.com
AZURE_OPENAI_KEY=zzzzzzzzzzzzzzzz  # Omit if using Managed Identity granted to Azure OpenAI resource
AZURE_OPENAI_DEPLOYMENT=gpt5nano
AZURE_OPENAI_API_VERSION=2024-08-01-preview
```

## Extraction Status & Error Handling
Each receipt record includes:
- `status`: lifecycle / outcome indicator
- `error_message`: human-readable issue (only on failure)
- `debug_fields`: complete field map or full provider JSON payload

Typical statuses:
| Status | Meaning | Matching? |
|--------|---------|-----------|
| extracted | Successful Doc Intelligence extraction | Yes |
| extracted_content_understanding | Successful CU extraction | Yes |
| extracted_partial | Heuristic partial fields only | Yes |
| error_docintel_unavailable | Doc Intelligence client not created | No |
| error_docintel_analyze | Azure DI analyze call failed | No |
| error_docintel_exception | Unexpected DI exception | No |
| error_cu_missing_config | CU endpoint/key absent | No |
| error_cu_analyze | CU analyze or poll failed | No |

Receipts with any `error_*` status (or `error_message`) are omitted from proposals and show a red error block in the UI instead of a match selector.

## Blob Storage & Downloading
Receipts are uploaded as blobs using a UUID filename (retaining original extension). The database `receipts.stored_path` column now stores the blob name (not a filesystem path) for new uploads. The `/receipts/{id}/download` endpoint:

1. Detects if `stored_path` looks like a blob name (no path separators & length >= ~30)
2. Downloads the blob with the container client and streams bytes to the browser with the original filename as attachment
3. Falls back to local file if blob not detected or download fails

If you set `RECEIPT_BLOB_PUBLIC_BASE_URL` and your container/content is publicly accessible (or served via CDN), the frontend can link directly rather than streaming through the API (current implementation still streams to avoid exposing direct paths by default).

## Migration of Existing Local Files
Existing rows inserted before blob integration may have absolute `stored_path` values. A helper script migrates them:

```bash
python -m backend.app.scripts.migrate_local_uploads_to_blob
```

Behavior: uploads each local file to the configured container (new UUID name) and updates `stored_path` to blob name. Local files are NOT deleted automatically. After verifying, you may manually remove the old uploads directory.

## Troubleshooting
| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| 404 on Content Understanding analyze | Wrong endpoint or analyzer ID | Confirm endpoint; adjust `AZURE_CONTENT_UNDERSTANDING_ANALYZER_ID` |
| 401/403 on analyze | Invalid key / insufficient permissions | Regenerate key; verify resource access |
| All receipts show heuristic only | Azure env vars missing | Populate and restart app |
| No debug JSON | Provider returned minimal fields or error; check logs | Increase LOG_LEVEL=DEBUG |
| Matching proposed for errored receipt | (Should not happen) | Ensure frontend bundle updated; clear cache |
| Download 500 error | Blob permission / network issue | Confirm MSI has Storage Blob Data Reader/Contributor; check account name |
| Download 404 after migration | Local path migrated but blob missing | Re-run migration; verify container name |

## Endpoint: /env-check
Returns JSON booleans for presence of Azure endpoints/keys. Example:
```json
{
  "document_intelligence_endpoint": true,
  "content_understanding_endpoint": true,
  "content_understanding_key_present": true
}
```

## Security Considerations
Prototype only: add authentication, authorization, rate limiting, CSRF protections, secret management (Azure Key Vault), tighter CORS, and possibly SAS-based short-lived URLs if serving large files. Use role assignments (Storage Blob Data Contributor) for the Managed Identity executing uploads. No storage keys are used.

## Planned Enhancements
* Report submission workflow & aggregation (approval lifecycle)
* Analyzer listing endpoint & UI selection
* Confidence-driven UI highlighting
* Structured tests (pytest) & CI
* More robust retry/backoff wrappers around all external calls
* Payload size trimming / pagination for debug fields
* Multi-currency conversion & formatting
* SAS-based on-demand temporary download links (optional)

## License
Prototype code for demonstration purposes.
