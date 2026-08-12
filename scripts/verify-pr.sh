#!/usr/bin/env bash
set -euo pipefail

# Managed by issue-orchestrator setup-guardrails: verify-pr
# issue-orchestrator-selection: modes/default/main.yaml

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Preserve the engine's complete runtime selection. A human push has no
# selection, so use the mode that generated this managed fallback. A partial
# environment is never combined with baked values from another mode.
if [ -z "${ISSUE_ORCHESTRATOR_MODE:-}" ] ||
   [ -z "${ISSUE_ORCHESTRATOR_CONFIG_NAME:-}" ] ||
   [ -z "${ISSUE_ORCHESTRATOR_CONFIG_PATH:-}" ]; then
  selected_config_rel=modes/default/main.yaml
  export ISSUE_ORCHESTRATOR_CONFIG_NAME=main.yaml
  export ISSUE_ORCHESTRATOR_CONFIG_PATH="$repo_root/.issue-orchestrator/config/$selected_config_rel"
  export ISSUE_ORCHESTRATOR_MODE=default
fi
PYTHON_ENV_NAME=ISSUE_ORCHESTRATOR_PYTHON
PYTHON_BIN=""

if [ -n "${ISSUE_ORCHESTRATOR_PYTHON:-}" ] && [ -x "${ISSUE_ORCHESTRATOR_PYTHON}" ]; then
  PYTHON_BIN="${ISSUE_ORCHESTRATOR_PYTHON}"
elif [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi

if [ -z "$PYTHON_BIN" ]; then
  echo >&2 "verify-pr: could not find a Python interpreter with issue_orchestrator installed."
  echo >&2 "Rerun issue-orchestrator setup-guardrails or export $PYTHON_ENV_NAME before pushing."
  exit 1
fi

echo "verify-pr: running cache-aware pre-push validation"
"$PYTHON_BIN" -m issue_orchestrator.entrypoints.cli_tools.prepush_check -v
