#!/bin/bash
# Block restricted Git/GitHub commands for Codex PreToolUse.
# Registered in .codex/hooks.json and invoked for Bash tool calls.

set -euo pipefail

input="$(< /dev/stdin)"

fallback_blocks_restricted_command() {
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

hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hook_policy="$hook_dir/block_no_verify.py"
if [[ ! -f "$hook_policy" ]]; then
    echo "BLOCKED: managed Codex hook policy is missing. Run issue-orchestrator setup-hooks." >&2
    exit 2
fi

set +e
"$python_bin" "$hook_policy" --mode codex <<< "$input"
status=$?
set -e
if [[ $status -eq 0 || $status -eq 2 ]]; then
    exit $status
fi

# A standalone repo may not have the installed module or generated helper yet.
# Keep the common bypasses blocked without breaking unrelated commands.
if fallback_blocks_restricted_command "$input"; then
    echo "BLOCKED: hook evaluation failed; blocked restricted command." >&2
    exit 2
fi
exit 0
