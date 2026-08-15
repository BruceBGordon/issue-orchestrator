#!/bin/bash
# Block git commit/push --no-verify for Cursor agents
# This hook is called by Cursor beforeShellExecution mechanism
#
# Output: JSON to stdout
#   {"permission": "allow"} = ALLOW the command
#   {"permission": "deny", "userMessage": "..."} = BLOCK the command
#
# Configured in .cursor/hooks.json:
# {
#   "beforeShellExecution": [{
#     "command": ".cursor/hooks/block-no-verify.sh",
#     "output": "json"
#   }]
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
    echo '{"permission": "deny", "userMessage": "BLOCKED: python3 is required for orchestrator hooks. Fix PATH or install python3."}'
    exit 0
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
    output=$("$python_bin" "$repo_local_hook" --mode cursor <<< "$input" 2>/dev/null)
elif [[ -n "$hook_pythonpath" ]]; then
    output=$(PYTHONPATH="$hook_pythonpath${PYTHONPATH:+:$PYTHONPATH}" \
        "$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode cursor <<< "$input" 2>/dev/null)
else
    output=$("$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode cursor <<< "$input" 2>/dev/null)
fi
status=$?
set -e
if [[ $status -ne 0 ]]; then
    # If python exists but the full evaluator cannot import, keep a coarse
    # shell fallback so standalone installs do not brick normal commands.
    if fallback_block_no_verify "$input"; then
        echo '{"permission": "deny", "userMessage": "BLOCKED: hook evaluation failed; blocked restricted command."}'
    else
        echo '{"permission": "allow"}'
    fi
    exit 0
fi

echo "$output"
exit 0
