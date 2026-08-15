"""Shared hook-script verification helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

HookInputFormat = Literal["tool_input_command", "command", "copilot_tool_args"]
HookBlockMode = Literal["exit_code_2", "cursor_permission", "copilot_permission"]
ReturnStream = Literal["stderr", "stdout"] | None
HookBlockTester = Callable[[Path, str], bool | tuple[bool, str]]

HOOK_TEST_CASES: tuple[tuple[str, bool], ...] = (
    ("git push --no-verify", True),
    ("git commit --no-verify -m 'test'", True),
    ("git push origin main --no-verify", True),
    ("git push --force origin main --no-verify", True),
    ("git commit -m 'test' --no-verify", True),
    ("git --no-verify push", True),
    ("git commit -n -m 'test'", True),
    ("git commit --amend -n", True),
    ("git -c core.hooksPath=/dev/null push", True),
    ("git -c core.hooksPath=/tmp/hooks push", True),
    ("git -c core.hooksPath=.git/empty commit", True),
    ("git config --local core.hooksPath /dev/null", True),
    ("git config --local core.hooksPath .git/empty", True),
    ("git config --worktree --unset core.hooksPath", True),
    ("git push origin main '--no-verify'", True),
    ("git push origin main --no-veri", True),
    ("git push origin main '--no-verif'", True),
    ("git commit -m test '--no-verify'", True),
    ("git commit --no-veri -m test", True),
    ("git commit -m test '--no-verif'", True),
    ("git commit '-n' -m test", True),
    ("git -c 'core.hooksPath=/tmp/empty-hooks' push origin main", True),
    ("git -C . config core.hooksPath /tmp/empty-hooks", True),
    ("git -C . config --unset core.hooksPath", True),
    ("git config core.hooksPath get", True),
    ("git config --local core.hooksPath get", True),
    ("git config core.hooksPath --get", True),
    ("git config core.hooksPath --get-all", True),
    ("git config rename-section core oldcore", True),
    ("git config remove-section core", True),
    ("git config edit", True),
    ("git config set user.name agent", True),
    ("git config --unset-a core.hooksPath", True),
    ("git config --remove-sect core", True),
    ("git status && git push origin main '--no-verify'", True),
    ("git commit -anm test", True),
    ("git -c color.ui=false status", True),
    ("git --config-env=core.hooksPath=HOOKS push origin main", True),
    ("GIT_CONFIG_COUNT=1 git push origin main", True),
    ("export GIT_CONFIG_COUNT=1; git push origin main", True),
    ('flag=--no-verify; git push origin main "$flag"', True),
    ("git push origin main $(printf %s --no-verify)", True),
    ("(git push origin main --no-verify)", True),
    ("$(git push origin main --no-verify)", True),
    ("`git push origin main --no-verify`", True),
    ("(git -C . config core.hooksPath /tmp/empty-hooks)", True),
    ("$(git -C . config core.hooksPath /tmp/empty-hooks)", True),
    ("`git config rename-section core oldcore`", True),
    ("sh -c 'git -c core.hooksPath=/dev/null commit -m test'", True),
    ("bash -c 'git commit -n -m test'", True),
    ("bash -lc 'git commit -n -m test'", True),
    ("zsh -fc 'git -c core.hooksPath=/dev/null commit -m test'", True),
    ("env sh -c 'git commit --no-verify -m test'", True),
    ("command sh -c 'git config edit'", True),
    ("env FOO=x sh -c 'git commit -n -m test'", True),
    ("env -i sh -c 'git commit --no-verify -m test'", True),
    ("env -u FOO sh -c 'git commit -n -m test'", True),
    ("env -C /tmp sh -c 'git commit -n -m test'", True),
    ("env -P /usr/bin sh -c 'git commit -n -m test'", True),
    ("env -S \"sh -c 'git commit -n -m test'\"", True),
    (r"env -S 'sh\_-c\_git\ commit\ -n\ -m\ test'", True),
    ("env -S 'git push --no-verify'", True),
    (r"env -S 'git\_push\_--no-verify'", True),
    ("env --split-string='git push --no-verify'", True),
    (r"env -iSgit\ push\ --no-verify", True),
    ("env -ivS 'git push --no-verify'", True),
    ("env -uHOME -iS 'git push --no-verify'", True),
    ("bash -c 'env -S \"git push --no-verify\"'", True),
    ("command -- sh -c 'git -c core.hooksPath=/dev/null status'", True),
    ("bash -c -- 'git commit -n -m test'", True),
    ("env sh -c -- 'git -c core.hooksPath=/dev/null status'", True),
    ("git -C . config --local CORE.HOOKSPATH /tmp/empty-hooks", True),
    ("gh pr merge 123", True),
    ("gh pr merge 123 --squash", True),
    ("gh api repos/owner/repo/pulls/123/merge -X PUT", True),
    ("git push origin main", False),
    ("git commit -m 'test'", False),
    ("git status --short", False),
    ("git config --get core.hooksPath", False),
    ("git config --local --get core.hooksPath", False),
    ("git -C . config --get core.hooksPath", False),
    ("git -C . status", False),
    ("git switch -c new-branch", False),
    ("git commit -c HEAD", False),
    ("git commit -mnormal", False),
    ("git commit -mno", False),
    ("git commit -uno", False),
    ("git commit -Cmain", False),
    ("git commit -Fnotes.txt", False),
    ("git commit -Snone", False),
    ("git log -c", False),
    ("git config get core.hooksPath", False),
    ("git config list --show-origin", False),
    ("git config user.name", False),
    ("gh pr create --title 'test'", False),
    ("gh pr view 123", False),
    ("echo 'git push --no-verify'", False),
    ("rg --fixed-strings 'git push --no-verify' docs", False),
    ("ls -la", False),
)


def run_hook_test_cases(
    blocks_hook: HookBlockTester,
    hook_script: Path,
    checks_passed: list[str],
    checks_failed: list[str],
) -> None:
    """Run the shared guardrail hook matrix and record results."""
    for cmd, should_block in HOOK_TEST_CASES:
        result = blocks_hook(hook_script, cmd)
        blocked = result[0] if isinstance(result, tuple) else result
        label = cmd[:30]
        if should_block == blocked:
            checks_passed.append(f"{'blocks' if should_block else 'allows'}:{label}")
        else:
            checks_failed.append(
                f"{'should_block' if should_block else 'wrongly_blocks'}:{label}"
            )


def build_hook_input(command: str, input_format: HookInputFormat) -> str:
    """Build one agent's hook input envelope for a shell command."""
    if input_format == "tool_input_command":
        return json.dumps({"tool_input": {"command": command}})
    if input_format == "command":
        return json.dumps({"command": command})
    return json.dumps(
        {"toolName": "bash", "toolArgs": json.dumps({"command": command})}
    )


def is_blocked(*, returncode: int, stdout: str, block_mode: HookBlockMode) -> bool:
    """Parse one hook execution result into a blocked/allowed decision."""
    if block_mode == "exit_code_2":
        return returncode == 2
    if block_mode == "cursor_permission":
        return _json_stdout(stdout).get("permission") == "deny"
    return _json_stdout(stdout).get("permissionDecision") == "deny"


def _json_stdout(stdout: str) -> dict[str, object]:
    try:
        return json.loads(stdout.strip()) if stdout.strip() else {}
    except json.JSONDecodeError:
        return {}
