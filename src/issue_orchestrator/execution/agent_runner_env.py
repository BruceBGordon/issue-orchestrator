"""Environment variable filtering for agent security.

This module provides functions to prepare isolated environments for agent sessions:
- Scrub forbidden environment variables (credentials, tokens)
- Set safe git environment variables
- Build filtered environment dictionaries

Security principle: Agents should not *accidentally* inherit credentials
that could let them perform privileged operations. This is hygiene — it
reduces surprise and blocks unsophisticated misuse — not isolation.

Scope note: scrubbing an env var does NOT stop an agent from reading
the same credential off the filesystem. Same-user agents still share
HOME (``terminal_subprocess.py`` keeps ``isolate_home=False``), so for
example ``ISSUE_ORCHESTRATOR_API_TOKEN`` is stripped from the agent
env but the admin token file remains readable at
``~/.issue-orchestrator/api-token``. Real containment requires
OS-level separation (separate user, container, sandbox). Tracked as
issue #6024.
"""

import os
from typing import Mapping

from ..infra.secret_env import (
    EXTRA_FORBIDDEN_ENV_VARS_ENV,
    GITHUB_APP_PRIVATE_KEY_ENV,
    forbidden_agent_env_vars,
)

# Environment variables that should be scrubbed before agent sessions.
# These are credentials that could allow agents to bypass guardrails.
DEFAULT_FORBIDDEN_ENV_VARS: list[str] = [
    # GitHub tokens
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    # GitHub App credentials
    "GH_APP_ID",
    "GH_APP_PRIVATE_KEY",
    GITHUB_APP_PRIVATE_KEY_ENV,
    "GH_INSTALLATION_ID",
    # OAuth tokens
    "GITHUB_OAUTH_TOKEN",
    # Other potentially dangerous credentials
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    # SSH agent - can forward credentials
    "SSH_AUTH_SOCK",
    # Claude Code nesting detection - must not leak into child agents
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    # Admin bearer token for the loopback Control API. Scrubbing
    # prevents accidental inheritance from the orchestrator's env
    # into the agent subprocess. IMPORTANT: this is hygiene only —
    # the admin token file at ``~/.issue-orchestrator/api-token``
    # remains readable from the agent because agents run under the
    # same user with the real HOME (#6024). Real separation needs
    # OS-level isolation.
    "ISSUE_ORCHESTRATOR_API_TOKEN",
    EXTRA_FORBIDDEN_ENV_VARS_ENV,
]

# Environment variables to set for safe git behavior
GIT_SAFE_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",  # Disable interactive prompts
    "GIT_ASKPASS": "/usr/bin/false",  # Fail any credential requests
}

# Environment variables that must ALWAYS propagate, even in allowlist
# (passthrough_vars) mode. These encode CC-launch-time invariants that
# subprocesses depend on being correct regardless of what any individual
# agent spec chooses to pass through explicitly.
#
# Losing any of these silently breaks the serving path — the failure
# mode is catastrophic (stale code imports, hooks unable to find
# Python) with no log signal — so they are enforced centrally rather
# than trusted to every caller to remember.
ALWAYS_PASSTHROUGH_ENV_VARS: list[str] = [
    # Frozen-snapshot import path set by scripts/start_control_center.sh
    # at CC launch (#5950). Dropping it regresses to the editable
    # install and reintroduces base-repo branch drift into every agent
    # subprocess. See src/issue_orchestrator/infra/cc_snapshot.py.
    "PYTHONPATH",
    "ISSUE_ORCHESTRATOR_CC_SNAPSHOT",
    # Hook Python resolution for foreign target repos (#5944). Required
    # by pre-push hook scripts that live outside the venv and cannot
    # otherwise find the orchestrator's interpreter.
    "ISSUE_ORCHESTRATOR_PYTHON",
    # Scoped bearer token for the loopback Control API (security
    # #5987 F3). Agent subprocesses call back into the orchestrator
    # for preflight-push, exchange-respond and session-resume; this
    # token authorizes ONLY the routes allowed by
    # ``_auth_middleware.is_agent_callback_route``, which every serving
    # surface shares.
    #
    # Scope note: the admin token is scrubbed from agent env, but a
    # deliberately malicious agent running as the same user can still
    # read ``~/.issue-orchestrator/api-token`` directly and bypass
    # the scoping. The callback token is defense in depth — it keeps
    # agents off admin routes in the default path — not a hard
    # privilege boundary. Real isolation requires OS-level
    # separation; tracked as issue #6024.
    "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN",
]


def build_filtered_env(
    *,
    base_env: Mapping[str, str] | None = None,
    scrub_vars: list[str] | None = None,
    passthrough_vars: list[str] | None = None,
    overrides: dict[str, str] | None = None,
    include_git_safe: bool = True,
) -> dict[str, str]:
    """Build a filtered environment dictionary for subprocess execution.

    This function creates an environment that:
    1. Starts with the base environment (or current process env)
    2. Removes forbidden/scrubbed variables
    3. Applies git-safe settings
    4. Applies caller-provided overrides

    Args:
        base_env: Starting environment (defaults to os.environ)
        scrub_vars: Variables to remove (defaults to DEFAULT_FORBIDDEN_ENV_VARS)
        passthrough_vars: If specified, ONLY these vars are passed through
                         (plus overrides). If None, all non-scrubbed vars pass.
        overrides: Variables to set/override in the final environment
        include_git_safe: Whether to include GIT_TERMINAL_PROMPT=0, etc.

    Returns:
        Filtered environment dictionary ready for subprocess.run(env=...)

    Example:
        env = build_filtered_env(
            scrub_vars=["GH_TOKEN", "AWS_SECRET_ACCESS_KEY"],
            overrides={"CUSTOM_VAR": "value"},
        )
        subprocess.run(cmd, env=env)
    """
    if base_env is None:
        base_env = os.environ

    if scrub_vars is None:
        scrub_vars = DEFAULT_FORBIDDEN_ENV_VARS
    scrub_vars = forbidden_agent_env_vars(scrub_vars)

    # Build the base environment
    scrub_set = set(scrub_vars)
    if passthrough_vars is not None:
        # Allowlist mode: only specified vars pass through, PLUS the
        # always-passthrough set (snapshot PYTHONPATH, ISSUE_ORCHESTRATOR_PYTHON, …).
        # See ALWAYS_PASSTHROUGH_ENV_VARS for the rationale — these are
        # CC-launch-time invariants that must not be lost to an
        # allowlist a caller forgot to update.
        allowed = set(passthrough_vars) | set(ALWAYS_PASSTHROUGH_ENV_VARS)
        env = {k: v for k, v in base_env.items() if k in allowed and k not in scrub_set}
    else:
        # Denylist mode: all vars pass except scrubbed ones
        env = {k: v for k, v in base_env.items() if k not in scrub_set}

    # Apply git-safe settings
    if include_git_safe:
        env.update(GIT_SAFE_ENV)

    # Apply overrides last (highest priority)
    if overrides:
        env.update(overrides)

    return env


def get_forbidden_env_vars() -> list[str]:
    """Get the default list of environment variables that should be scrubbed.

    Returns:
        Copy of the default forbidden env vars list
    """
    return forbidden_agent_env_vars(DEFAULT_FORBIDDEN_ENV_VARS)


def verify_env_scrubbed(
    env: Mapping[str, str],
    forbidden: list[str] | None = None,
) -> dict[str, bool]:
    """Verify that forbidden environment variables are not present.

    This is meant to be run to verify isolation was applied correctly.

    Args:
        env: Environment dictionary to check
        forbidden: Variables to check for (defaults to DEFAULT_FORBIDDEN_ENV_VARS)

    Returns:
        Dict mapping variable names to whether they are absent (True = good)
    """
    if forbidden is None:
        forbidden = get_forbidden_env_vars()

    return {var: var not in env for var in forbidden}


def all_env_scrubbed(
    env: Mapping[str, str],
    forbidden: list[str] | None = None,
) -> bool:
    """Check if all forbidden environment variables are absent.

    Args:
        env: Environment dictionary to check
        forbidden: Variables to check for (defaults to DEFAULT_FORBIDDEN_ENV_VARS)

    Returns:
        True if all forbidden variables are absent
    """
    return all(verify_env_scrubbed(env, forbidden).values())
