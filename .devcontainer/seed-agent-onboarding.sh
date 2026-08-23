#!/usr/bin/env bash
# Pre-seed provider CLI state that a PTY-driven agent session cannot answer.
#
# The orchestrator drives `claude` as an INTERACTIVE PTY session, not `claude -p`.
# That distinction matters twice, and both traps look like a hung session rather
# than an error:
#
#   1. Claude Code pushes its onboarding step whenever OAuth is enabled, without
#      first checking whether a token is already present. A valid
#      ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN does not skip it. Seeding
#      hasCompletedOnboarding is the documented community workaround for an
#      issue that has been open since 2025-10.
#   2. By default, `~/.claude.json` lives OUTSIDE `~/.claude`. When
#      CLAUDE_CONFIG_DIR is set, Claude relocates the state file into that
#      directory instead. Seeding the wrong layout leaves onboarding active.
#
# Idempotent: never overwrites an existing value, so a real login is preserved.
set -euo pipefail

if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
    config="$CLAUDE_CONFIG_DIR"
    state="$config/.claude.json"
else
    config="$HOME/.claude"
    state="$HOME/.claude.json"
fi
mkdir -p "$config"

python3 - "$state" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text())
except FileNotFoundError:
    data = {}

if not isinstance(data, dict):
    raise ValueError(f"{path} must contain a JSON object")

# Only fill in what is absent. A codespace resumed after a real interactive
# login must keep that login's account and trust state untouched.
changed = False
for key, value in (("hasCompletedOnboarding", True),):
    if key not in data:
        data[key] = value
        changed = True

if changed:
    path.write_text(json.dumps(data, indent=2))
    print(f"seeded {path}")
else:
    print(f"{path} already initialised — left alone")
PY

echo "Provider onboarding state ready."
echo
echo "Credentials are NOT set here. Set them as Codespaces secrets:"
echo "  ISSUE_ORCH_GITHUB_TOKEN   (required)"
echo "  ANTHROPIC_API_KEY  or  CLAUDE_CODE_OAUTH_TOKEN   (one of these)"
echo "If using ANTHROPIC_API_KEY, run claude once and approve the key before"
echo "starting the engine. CLAUDE_CODE_OAUTH_TOKEN needs no approval preflight."
echo "See docs/user/codespaces.md."
