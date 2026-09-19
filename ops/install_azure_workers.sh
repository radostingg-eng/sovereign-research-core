#!/bin/bash
set -euo pipefail

source_repo="${1:-$(git rev-parse --show-toplevel)}"
profile_repo="${SOVEREIGN_EXECUTOR_REPO:-$HOME/.local/share/sovereign-research-executor}"
launch_agents="$HOME/Library/LaunchAgents"
logs="$HOME/Library/Logs"
state_root="${SOVEREIGN_WORKER_STATE_ROOT:-$HOME/.local/state/sovereign-research-workers}"
python_command="${PYTHON_BIN:-$(command -v python3)}"
python_bin="$("$python_command" -c 'import sys; print(sys.executable)')"
template="$source_repo/ops/com.sovereign.azureworker.plist"

render_worker() {
  local worker_id="$1"
  local subscription="$2"
  local endpoint="$3"
  local deployment="$4"
  local minute="$5"
  local auth_mode="$6"
  local resource_group="$7"
  local account_name="$8"
  local key_vault_name="$9"
  local key_secret_name="${10}"
  local key_vault_subscription="${11}"
  local suffix="${worker_id//-/.}"
  local label="com.sovereign.azureworker.$suffix"
  local plist="$launch_agents/$label.plist"
  local log="$logs/$label.log"
  local token_scope="${SOVEREIGN_AZURE_TOKEN_SCOPE:-https://ai.azure.com/.default}"

  if [ -z "$subscription" ] || [ -z "$endpoint" ] || [ -z "$deployment" ]; then
    echo "$worker_id configuration incomplete"
    return 1
  fi
  sed \
    -e "s|REPLACE_WITH_LABEL|$label|g" \
    -e "s|REPLACE_WITH_REPO|$profile_repo|g" \
    -e "s|REPLACE_WITH_WORKER_ID|$worker_id|g" \
    -e "s|REPLACE_WITH_PYTHON|$python_bin|g" \
    -e "s|REPLACE_WITH_SUBSCRIPTION|$subscription|g" \
    -e "s|REPLACE_WITH_ENDPOINT|$endpoint|g" \
    -e "s|REPLACE_WITH_DEPLOYMENT|$deployment|g" \
    -e "s|REPLACE_WITH_TOKEN_SCOPE|$token_scope|g" \
    -e "s|REPLACE_WITH_AUTH_MODE|$auth_mode|g" \
    -e "s|REPLACE_WITH_RESOURCE_GROUP|$resource_group|g" \
    -e "s|REPLACE_WITH_ACCOUNT_NAME|$account_name|g" \
    -e "s|REPLACE_WITH_KEY_VAULT_NAME|$key_vault_name|g" \
    -e "s|REPLACE_WITH_KEY_SECRET_NAME|$key_secret_name|g" \
    -e "s|REPLACE_WITH_KEY_VAULT_SUBSCRIPTION|$key_vault_subscription|g" \
    -e "s|REPLACE_WITH_STATE_ROOT|$state_root|g" \
    -e "s|REPLACE_WITH_MINUTE|$minute|g" \
    -e "s|REPLACE_WITH_LOG|$log|g" \
    "$template" > "$plist"
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "installed $label at minute $minute"
}

mkdir -p "$launch_agents" "$logs" "$state_root"

render_worker \
  "azure-a" \
  "${SOVEREIGN_AZURE_A_SUBSCRIPTION:?Azure A subscription required}" \
  "${SOVEREIGN_AZURE_A_ENDPOINT:?Azure A endpoint required}" \
  "${SOVEREIGN_AZURE_A_DEPLOYMENT:?Azure A deployment required}" \
  "${SOVEREIGN_AZURE_A_MINUTE:-20}" \
  "${SOVEREIGN_AZURE_A_AUTH_MODE:-entra}" \
  "${SOVEREIGN_AZURE_A_RESOURCE_GROUP:-}" \
  "${SOVEREIGN_AZURE_A_ACCOUNT_NAME:-}" \
  "${SOVEREIGN_AZURE_A_KEY_VAULT_NAME:-}" \
  "${SOVEREIGN_AZURE_A_KEY_SECRET_NAME:-}" \
  "${SOVEREIGN_AZURE_A_KEY_VAULT_SUBSCRIPTION:-}"

if [ -n "${SOVEREIGN_AZURE_B_SUBSCRIPTION:-}" ]; then
  render_worker \
    "azure-b" \
    "$SOVEREIGN_AZURE_B_SUBSCRIPTION" \
    "${SOVEREIGN_AZURE_B_ENDPOINT:?Azure B endpoint required}" \
    "${SOVEREIGN_AZURE_B_DEPLOYMENT:?Azure B deployment required}" \
    "${SOVEREIGN_AZURE_B_MINUTE:-40}" \
    "${SOVEREIGN_AZURE_B_AUTH_MODE:-entra}" \
    "${SOVEREIGN_AZURE_B_RESOURCE_GROUP:-}" \
    "${SOVEREIGN_AZURE_B_ACCOUNT_NAME:-}" \
    "${SOVEREIGN_AZURE_B_KEY_VAULT_NAME:-}" \
    "${SOVEREIGN_AZURE_B_KEY_SECRET_NAME:-}" \
    "${SOVEREIGN_AZURE_B_KEY_VAULT_SUBSCRIPTION:-}"
fi
