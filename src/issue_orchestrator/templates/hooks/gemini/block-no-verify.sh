#!/bin/bash
# Block git commit/push --no-verify for Gemini CLI agents
# This hook is called by Gemini CLI PreToolUse mechanism
#
# Exit codes:
#   0 = ALLOW the command to execute
#   2 = BLOCK the command (Claude Code convention)
#
# Configured in .gemini/settings.json:
# {
#   "hooks": {
#     "PreToolUse": [{
#       "matcher": "Bash",
#       "hooks": [{"type": "command", "command": ".gemini/hooks/block-no-verify.sh"}]
#     }]
#   }
# }

set -euo pipefail

input="$(< /dev/stdin)"

# Fail closed if python itself is unavailable; we cannot enforce anything
# without an interpreter.

fallback_block_no_verify() {
    local payload="$1"
    if [[ "$payload" == *"--no-veri"* && "$payload" == *"git"* ]]; then
        return 0
    fi
    if [[ "$payload" == *"gh pr merge"* ]]; then
        return 0
    fi
    if [[ "$payload" == *"gh api"* && "$payload" == *"/merge"* ]]; then
        return 0
    fi
    if [[ "$payload" == *"git commit"* && "$payload" == *"-n"* ]]; then
        return 0
    fi
    if [[ "$payload" == *"git"* && "$payload" == *"core.hooksPath"* ]]; then
        return 0
    fi
    return 1
}

python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" ]]; then
    echo "BLOCKED: python3 is required for orchestrator hooks. Fix PATH or install python3." >&2
    exit 2
fi

hook_pythonpath="${ORCHESTRATOR_HOOK_PYTHONPATH:-}"
repo_local_hook=""
if [[ -z "$hook_pythonpath" ]]; then
    hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    repo_root="$(cd "$hook_dir/../.." && pwd)"
    if [[ -f "$repo_root/scripts/agent-hooks/block_no_verify.py" ]]; then
        repo_local_hook="$repo_root/scripts/agent-hooks/block_no_verify.py"
    fi
    if [[ -d "$repo_root/src/issue_orchestrator" ]]; then
        hook_pythonpath="$repo_root/src"
    fi
fi

set +e
if [[ -n "$repo_local_hook" ]]; then
    "$python_bin" "$repo_local_hook" --mode gemini <<< "$input"
elif [[ -n "$hook_pythonpath" ]]; then
    PYTHONPATH="$hook_pythonpath${PYTHONPATH:+:$PYTHONPATH}" \
        "$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode gemini <<< "$input"
else
    "$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode gemini <<< "$input"
fi
status=$?
set -e
if [[ $status -eq 0 || $status -eq 2 ]]; then
    exit $status
fi

# If python exists but the full evaluator cannot import, keep a coarse shell
# fallback so standalone installs do not brick normal commands.
if fallback_block_no_verify "$input"; then
    echo "BLOCKED: hook evaluation failed; blocked restricted command." >&2
    exit 2
fi
exit 0
