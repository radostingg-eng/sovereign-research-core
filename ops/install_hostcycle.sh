#!/bin/bash
set -euo pipefail

source_repo="${1:-$(git rev-parse --show-toplevel)}"
executor_repo="${SOVEREIGN_EXECUTOR_REPO:-$HOME/.local/share/sovereign-research-executor}"
launch_agents="$HOME/Library/LaunchAgents"
logs="$HOME/Library/Logs"
plist="$launch_agents/com.sovereign.hostcycle.plist"
label="gui/$(id -u)/com.sovereign.hostcycle"
remote="$(git -C "$source_repo" remote get-url origin)"
python_command="${PYTHON_BIN:-$(command -v python3)}"
python_bin="$("$python_command" -c 'import sys; print(sys.executable)')"

if ! "$python_bin" -c '
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
'; then
  echo "hostcycle requires Python 3.12 or newer: $python_bin"
  exit 1
fi

case "$remote" in
  git@github.com:*) ;;
  *)
    echo "origin must use SSH for non-interactive launchd access: $remote"
    exit 1
    ;;
esac

mkdir -p "$(dirname "$executor_repo")" "$launch_agents" "$logs"

if [ ! -e "$executor_repo/.git" ]; then
  if [ -e "$executor_repo" ]; then
    echo "executor path exists but is not a git clone: $executor_repo"
    exit 1
  fi
  git clone --quiet --branch main --single-branch "$remote" "$executor_repo"
else
  configured_remote="$(git -C "$executor_repo" remote get-url origin)"
  if [ "$configured_remote" != "$remote" ]; then
    echo "executor clone origin differs from source origin"
    exit 1
  fi
  git -C "$executor_repo" switch --quiet main
  git -C "$executor_repo" pull --rebase --quiet origin main
fi

git -C "$executor_repo" config user.name "sovereign-executor"
git -C "$executor_repo" config user.email "noreply@github.com"

escaped_repo="${executor_repo//|/\\|}"
escaped_log="${logs//|/\\|}/sovereign-research-hostcycle.log"
escaped_python="${python_bin//|/\\|}"
sed \
  -e "s|REPLACE_WITH_EXECUTOR_REPO_PATH|$escaped_repo|g" \
  -e "s|REPLACE_WITH_HOSTCYCLE_LOG_PATH|$escaped_log|g" \
  -e "s|REPLACE_WITH_PYTHON_BIN|$escaped_python|g" \
  "$source_repo/ops/com.sovereign.hostcycle.plist" > "$plist"

launchctl bootout "$label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist"

echo "installed $label"
echo "executor clone: $executor_repo"
echo "python: $python_bin"
echo "log: $logs/sovereign-research-hostcycle.log"
