#!/usr/bin/env bash
set -euo pipefail

# This script creates Azure resources and deploys the app using the ZIP deploy approach.
# Prereqs:
# - Azure CLI logged in: az login
# - Subscription selected: az account set --subscription <SUB_ID>
# - Bash, zip, and Azure CLI available in PATH
# - From repo root: ./scripts/deploy_to_azure.sh <resource_group> <location> <appservice_plan> <webapp_name>
#
# Example:
#   ./scripts/deploy_to_azure.sh exp-demo-rg eastus exp-demo-plan exp-demo-web
#
# Notes:
# - Uses Python 3.10 on Linux App Service.
# - Deploys full repo so that frontend assets are served by FastAPI.
# - SQLite DB file lives under backend/app/db/expenses.db. App Service is ephemeral; for persistence across restarts,
#   consider enabling Azure Files or a different DB. For demo purposes, reseeding occurs at startup.

if [[ $# -lt 4 ]]; then
  echo "Usage: $0 <resource_group> <location> <appservice_plan> <webapp_name>"
  exit 1
fi

RG_NAME="$1"
LOCATION="$2"
PLAN_NAME="$3"
WEBAPP_NAME="$4"
RUNTIME="PYTHON:3.10"
ZIP_FILE="deploy_package.zip"
# Use a transient hidden staging directory to avoid committing or colliding with source dirs
STAGING_DIR=".staging_package"

# Detect repo root (script dir/..)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

# Create or update resource group
az group create --name "$RG_NAME" --location "$LOCATION" >/dev/null

# Create an App Service plan (Linux)
az appservice plan create \
  --name "$PLAN_NAME" \
  --resource-group "$RG_NAME" \
  --location "$LOCATION" \
  --sku B1 \
  --is-linux >/dev/null

# Create the Web App
if ! az webapp show --resource-group "$RG_NAME" --name "$WEBAPP_NAME" >/dev/null 2>&1; then
  az webapp create \
    --resource-group "$RG_NAME" \
    --plan "$PLAN_NAME" \
    --name "$WEBAPP_NAME" \
    --runtime "$RUNTIME" >/dev/null
else
  echo "WebApp $WEBAPP_NAME already exists; will update config and deploy."
fi

# Configure general settings
az webapp config set \
  --resource-group "$RG_NAME" \
  --name "$WEBAPP_NAME" \
  --use-32bit-worker-process false \
  --always-on true >/dev/null

# Ensure Oryx builds with our requirements.txt (root file already present)
# Set startup command to run gunicorn with uvicorn worker (Oryx will generate startup scripts)
STARTUP_CMD="gunicorn backend.app.main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000"
az webapp config set \
  --resource-group "$RG_NAME" \
  --name "$WEBAPP_NAME" \
  --startup-file "$STARTUP_CMD" >/dev/null


# App settings: prefer production, expose port, adjust log level if needed
az webapp config appsettings set \
  --resource-group "$RG_NAME" \
  --name "$WEBAPP_NAME" \
  --settings \
    WEBSITES_PORT=8000 \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true \
    LOG_LEVEL=INFO \
    PYTHON_ENABLE_GUNICORN_MULTIWORKERS=1 \
    EXPENSE_DB_PATH=/home/data/expenses.db \
    RECEIPT_UPLOAD_DIR=/home/data/uploads >/dev/null

# Enable application logging (optional but useful for troubleshooting)
az webapp log config \
  --resource-group "$RG_NAME" \
  --name "$WEBAPP_NAME" \
  --application-logging filesystem \
  --level information >/dev/null

# Small delay to allow SCM site to recycle before deployment
sleep 10

# Prepare staging directory with vendored dependencies to avoid relying on remote Oryx build
rm -rf "$STAGING_DIR" "$ZIP_FILE"
mkdir -p "$STAGING_DIR/.python_packages/lib/site-packages"

# Copy source tree excluding staging dir and common junk to avoid recursion
tar \
  --exclude="./$STAGING_DIR" \
  --exclude="./.git" \
  --exclude="./__pycache__" \
  --exclude="./uploads" \
  --exclude="./venv" \
  --exclude="./env" \
  --exclude="*.venv*" \
  --exclude="./backend/app/db/expenses.db" \
  -cf - . | (cd "$STAGING_DIR" && tar xf -)

# Clean up pycache/pyc inside staging
find "$STAGING_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGING_DIR" -type f -name "*.pyc" -delete 2>/dev/null || true

# Vendor dependencies into .python_packages
python3 -m pip install --upgrade pip >/dev/null
python3 -m pip install --no-cache-dir --isolated -r requirements.txt -t \"$STAGING_DIR/.python_packages/lib/site-packages\"

# Build zip from staging contents
(cd "$STAGING_DIR" && zip -r "../$ZIP_FILE" . >/dev/null)

# Cleanup staging directory after packaging to keep workspace tidy
rm -rf "$STAGING_DIR"

echo "Deploying $ZIP_FILE to $WEBAPP_NAME (az webapp deploy with retry)..."
max_attempts=3
attempt=1
until az webapp deploy \
  --resource-group "$RG_NAME" \
  --name "$WEBAPP_NAME" \
  --src-path "$ZIP_FILE" \
  --type zip >/dev/null; do
  if [[ $attempt -ge $max_attempts ]]; then
    echo "Deployment failed after $attempt attempts." >&2
    exit 1
  fi
  echo "Deployment attempt $attempt failed. Waiting and retrying..."
  attempt=$((attempt+1))
  sleep 15
done

echo "Deployment completed. Fetching site URL..."
APP_URL=$(az webapp show --resource-group "$RG_NAME" --name "$WEBAPP_NAME" --query defaultHostName -o tsv)

echo "Done. Visit: https://$APP_URL"
echo "Tip: check logs with 'az webapp log tail --name $WEBAPP_NAME --resource-group $RG_NAME'"
