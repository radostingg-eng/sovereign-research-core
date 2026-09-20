#!/bin/bash
set -euo pipefail

source_repo="${1:-$(git rev-parse --show-toplevel)}"
azure_a_auth_mode="${SOVEREIGN_AZURE_A_AUTH_MODE:?SOVEREIGN_AZURE_A_AUTH_MODE is required}"
azure_b_subscription="${SOVEREIGN_AZURE_B_SUBSCRIPTION:-}"
azure_b_auth_mode=""
if [ -n "$azure_b_subscription" ]; then
  azure_b_auth_mode="${SOVEREIGN_AZURE_B_AUTH_MODE:?SOVEREIGN_AZURE_B_AUTH_MODE is required when Azure B is configured}"
fi

validate_auth_mode() {
  local worker_id="$1"
  local auth_mode="$2"
  case "$auth_mode" in
    entra|azure_cli_key|key_vault)
      ;;
    *)
      echo "$worker_id auth mode must be entra, azure_cli_key, or key_vault" >&2
      return 1
      ;;
  esac
}

validate_auth_mode "azure-a" "$azure_a_auth_mode"
if [ -n "$azure_b_subscription" ]; then
  validate_auth_mode "azure-b" "$azure_b_auth_mode"
fi

profile_repo="${SOVEREIGN_EXECUTOR_REPO:-$HOME/.local/share/sovereign-research-executor}"
launch_agents="$HOME/Library/LaunchAgents"
logs="$HOME/Library/Logs"
state_root="${SOVEREIGN_WORKER_STATE_ROOT:-$HOME/.local/state/sovereign-research-workers}"
python_command="${PYTHON_BIN:-$(command -v python3)}"
python_bin="$("$python_command" -c 'import sys; print(sys.executable)')"
azure_cli="${SOVEREIGN_AZURE_CLI_BIN:-$(command -v az)}"
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
  local max_output_tokens="${12}"
  local target_offset="${13}"
  local role="${14}"
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
    -e "s|REPLACE_WITH_AZURE_CLI|$azure_cli|g" \
    -e "s|REPLACE_WITH_SUBSCRIPTION|$subscription|g" \
    -e "s|REPLACE_WITH_ENDPOINT|$endpoint|g" \
    -e "s|REPLACE_WITH_DEPLOYMENT|$deployment|g" \
    -e "s|REPLACE_WITH_MAX_OUTPUT_TOKENS|$max_output_tokens|g" \
    -e "s|REPLACE_WITH_TARGET_OFFSET|$target_offset|g" \
    -e "s|REPLACE_WITH_ROLE|$role|g" \
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
  "$azure_a_auth_mode" \
  "${SOVEREIGN_AZURE_A_RESOURCE_GROUP:-}" \
  "${SOVEREIGN_AZURE_A_ACCOUNT_NAME:-}" \
  "${SOVEREIGN_AZURE_A_KEY_VAULT_NAME:-}" \
  "${SOVEREIGN_AZURE_A_KEY_SECRET_NAME:-}" \
  "${SOVEREIGN_AZURE_A_KEY_VAULT_SUBSCRIPTION:-}" \
  "${SOVEREIGN_AZURE_A_MAX_OUTPUT_TOKENS:-8000}" \
  "${SOVEREIGN_AZURE_A_TARGET_OFFSET:-0}" \
  "${SOVEREIGN_AZURE_A_ROLE:-primary_frame}"

install_optional_a_worker() {
  local suffix="$1"
  local deployment="$2"
  local minute="$3"
  local target_offset="$4"
  local role="$5"
  if [ -z "$deployment" ]; then
    return
  fi
  render_worker \
    "azure-a-$suffix" \
    "${SOVEREIGN_AZURE_A_SUBSCRIPTION:?Azure A subscription required}" \
    "${SOVEREIGN_AZURE_A_ENDPOINT:?Azure A endpoint required}" \
    "$deployment" \
    "$minute" \
    "$azure_a_auth_mode" \
    "${SOVEREIGN_AZURE_A_RESOURCE_GROUP:-}" \
    "${SOVEREIGN_AZURE_A_ACCOUNT_NAME:-}" \
    "${SOVEREIGN_AZURE_A_KEY_VAULT_NAME:-}" \
    "${SOVEREIGN_AZURE_A_KEY_SECRET_NAME:-}" \
    "${SOVEREIGN_AZURE_A_KEY_VAULT_SUBSCRIPTION:-}" \
    "${SOVEREIGN_AZURE_A_MAX_OUTPUT_TOKENS:-8000}" \
    "$target_offset" \
    "$role"
}

install_optional_a_worker \
  "gpt5-mini" \
  "${SOVEREIGN_AZURE_A_GPT5_MINI_DEPLOYMENT:-}" \
  "${SOVEREIGN_AZURE_A_GPT5_MINI_MINUTE:-25}" \
  "${SOVEREIGN_AZURE_A_GPT5_MINI_TARGET_OFFSET:-1}" \
  "${SOVEREIGN_AZURE_A_GPT5_MINI_ROLE:-evidence_map}"

install_optional_a_worker \
  "o4-mini" \
  "${SOVEREIGN_AZURE_A_O4_MINI_DEPLOYMENT:-}" \
  "${SOVEREIGN_AZURE_A_O4_MINI_MINUTE:-30}" \
  "${SOVEREIGN_AZURE_A_O4_MINI_TARGET_OFFSET:-2}" \
  "${SOVEREIGN_AZURE_A_O4_MINI_ROLE:-adversarial_challenge}"

if [ -n "$azure_b_subscription" ]; then
  render_worker \
    "azure-b" \
    "$azure_b_subscription" \
    "${SOVEREIGN_AZURE_B_ENDPOINT:?Azure B endpoint required}" \
    "${SOVEREIGN_AZURE_B_DEPLOYMENT:?Azure B deployment required}" \
    "${SOVEREIGN_AZURE_B_MINUTE:-40}" \
    "$azure_b_auth_mode" \
    "${SOVEREIGN_AZURE_B_RESOURCE_GROUP:-}" \
    "${SOVEREIGN_AZURE_B_ACCOUNT_NAME:-}" \
    "${SOVEREIGN_AZURE_B_KEY_VAULT_NAME:-}" \
    "${SOVEREIGN_AZURE_B_KEY_SECRET_NAME:-}" \
    "${SOVEREIGN_AZURE_B_KEY_VAULT_SUBSCRIPTION:-}" \
    "${SOVEREIGN_AZURE_B_MAX_OUTPUT_TOKENS:-8000}" \
    "${SOVEREIGN_AZURE_B_TARGET_OFFSET:-3}" \
    "${SOVEREIGN_AZURE_B_ROLE:-independent_synthesis}"
fi
