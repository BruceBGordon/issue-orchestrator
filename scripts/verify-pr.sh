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

if ! "$PYTHON_BIN" -c "import issue_orchestrator" >/dev/null 2>&1; then
  echo >&2 "verify-pr: interpreter cannot import issue_orchestrator: $PYTHON_BIN"
  "$PYTHON_BIN" - >&2 <<'PROBE' || true
import pathlib
import sysconfig

site = pathlib.Path(sysconfig.get_paths()["purelib"])
pointers = sorted(site.glob("*issue_orchestrator*.pth"))
if not pointers:
    print("  no issue_orchestrator editable pointer found in", site)
for pointer in pointers:
    target = pointer.read_text().strip()
    state = "exists" if pathlib.Path(target).exists() else "MISSING"
    print("  " + pointer.name + " -> " + target + " [" + state + "]")
PROBE
  echo >&2 "verify-pr: a MISSING target means that checkout was deleted while"
  echo >&2 "verify-pr: this venv still pointed at it. Repair with:"
  echo >&2 "verify-pr:   cd <issue-orchestrator repo> && uv pip install --python .venv/bin/python -e . --no-deps"
  echo >&2 "verify-pr: or export $PYTHON_ENV_NAME to an interpreter that has it."
  exit 1
fi

echo "verify-pr: running cache-aware pre-push validation"
"$PYTHON_BIN" -m issue_orchestrator.entrypoints.cli_tools.prepush_check -v
