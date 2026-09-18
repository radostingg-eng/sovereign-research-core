#!/bin/bash
set -u

repo="${1:?executor repository path is required}"
max_jitter="${HOSTCYCLE_MAX_JITTER_SECONDS:-150}"
python_bin="${PYTHON_BIN:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "hostcycle Python is not executable: $python_bin"
  exit 1
fi
if ! "$python_bin" -c '
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
'; then
  echo "hostcycle requires Python 3.12 or newer: $python_bin"
  exit 1
fi
echo "hostcycle python: $("$python_bin" -c 'import platform,sys; print(f"{sys.executable} {platform.python_version()}")')"

case "$max_jitter" in
  ''|*[!0-9]*)
    echo "HOSTCYCLE_MAX_JITTER_SECONDS must be a non-negative integer"
    exit 1
    ;;
esac

if [ "$max_jitter" -gt 0 ]; then
  sleep "$((RANDOM % (max_jitter + 1)))"
fi

cd "$repo" || exit 1

publish_pending() {
  local unexpected
  unexpected="$(
    git status --porcelain=v1 --untracked-files=all |
      sed 's/^...//' |
      grep -Ev '^(audit/|tool_artifacts/|host_input/FEEDBACK\.json$)' || true
  )"
  if [ -n "$unexpected" ]; then
    echo "unexpected dirty files; refusing to alter executor checkout:"
    echo "$unexpected"
    return 1
  fi

  git add -A audit/
  if [ -d tool_artifacts ]; then
    git add -A tool_artifacts/
  fi
  if [ -f host_input/FEEDBACK.json ]; then
    git add host_input/FEEDBACK.json
  fi
  if ! git diff --cached --quiet; then
    git commit -q -m "audit: recover pending cycle result and host feedback" ||
      return 1
  fi
}

# A prior process may have stopped after writing a receipt but before its
# commit. Preserve that evidence before attempting a pull.
publish_pending || exit 1

git switch --quiet main || {
  echo "cannot switch the dedicated executor checkout to main"
  exit 1
}
git pull --rebase --quiet origin main || {
  echo "pull failed; refusing to execute against a stale tree"
  exit 1
}

"$python_bin" -m runtime.run_host_cycle --input-dir host_input
run_code=$?

"$python_bin" -m runtime.integrity || {
  echo "integrity failed; refusing to publish cycle changes"
  exit 1
}

publish_pending || {
  echo "commit failed; cycle changes remain unpublished"
  exit 1
}

if [ -n "$(git log origin/main..HEAD --oneline 2>/dev/null)" ]; then
  git push --quiet origin main ||
    {
      git pull --rebase --quiet origin main &&
        git push --quiet origin main
    } || {
      echo "push failed after rebase; the receipt exists only in executor clone"
      exit 1
    }
fi

exit "$run_code"
