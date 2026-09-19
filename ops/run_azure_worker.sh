#!/bin/bash
set -euo pipefail

repo="${1:?profile repository path is required}"
worker_id="${2:?worker id is required}"
python_bin="${PYTHON_BIN:-python3}"
state_root="${SOVEREIGN_WORKER_STATE_ROOT:-$HOME/.local/state/sovereign-research-workers}"
outbox="$state_root/$worker_id/outbox"

"$python_bin" "$repo/ops/azure_worker.py" \
  --feedback "$repo/host_input/FEEDBACK.json" \
  --outbox-dir "$outbox" \
  --worker-id "$worker_id" \
  --endpoint "${SOVEREIGN_AZURE_ENDPOINT:?azure endpoint is required}" \
  --deployment "${SOVEREIGN_AZURE_DEPLOYMENT:?deployment is required}" \
  --subscription "${SOVEREIGN_AZURE_SUBSCRIPTION:?subscription is required}" \
  --token-scope "${SOVEREIGN_AZURE_TOKEN_SCOPE:-https://ai.azure.com/.default}" \
  --auth-mode "${SOVEREIGN_AZURE_AUTH_MODE:-entra}" \
  --resource-group "${SOVEREIGN_AZURE_RESOURCE_GROUP:-}" \
  --account-name "${SOVEREIGN_AZURE_ACCOUNT_NAME:-}"

"$python_bin" "$repo/ops/publish_research_inbox.py" \
  --profile-root "$repo" \
  --outbox-dir "$outbox"
