"""The environment contract handed to a spawned agent session.

Owns one question: what environment does an agent session start with?
Every session type (coding, review, validation-retry, retrospective,
tech lead) goes through :func:`build_session_env_exports`, so the
contract cannot drift per launch path.

Extracted from ``session_launcher`` — the string is a pure function of
the config and the per-session paths, and giving it its own seam makes
each rule (notably the API-port sentinel below) testable without
standing up a launcher.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

from ..infra.agent_callback_endpoint import resolve_agent_callback_port
from ..infra.env import ENV_PREFIX
from .isolation import build_agent_tool_env_assignments


class SessionEnvConfig(Protocol):
    """The config surface the session environment depends on."""

    control_api_port: int
    config_path: Path | None


def api_port_export(control_api_port: int) -> str:
    """Render the Control API port export agents call back on.

    Resolved through the bound-endpoint owner, so agents get the port
    that is actually listening. ``control_api_port: 0`` means "bind any
    free port" and the supervised engine never writes the real port back
    into ``Config`` — exporting the configured value handed agents an
    unreachable ``localhost:0`` and made every callback fail (#6924).

    When nothing is bound and nothing is configured there is genuinely
    no endpoint, so the variable is omitted rather than set to a
    sentinel that merely looks configured.
    """
    port = resolve_agent_callback_port(control_api_port)
    if port is None:
        return ""
    return f" {ENV_PREFIX}API_PORT='{port}'"


def config_exports(config_path: Path | None) -> str:
    """Render the selected-config exports.

    ``coding-done`` / ``reviewer-done`` must resolve validation from the
    same config file the launcher used, so the name and resolved path
    travel with the session.
    """
    if config_path is None:
        return ""
    return (
        f" {ENV_PREFIX}CONFIG_NAME='{config_path.name}'"
        f" {ENV_PREFIX}CONFIG_PATH='{config_path.resolve()}'"
    )


def build_session_env_exports(
    *,
    config: SessionEnvConfig,
    completion_path: str,
    session_id: str,
    agent_label: str,
    issue_number: int,
    run_dir: Path,
    worktree_path: Path,
) -> str:
    """Build the common env-export string for all session types.

    Includes the orchestrator venv on PATH so ``coding-done`` /
    ``reviewer-done`` is always reachable — even when the target repo is
    a foreign (non-orchestrator) repository with no ``.venv``.

    Also exports orchestrator ``src`` on ``PYTHONPATH`` so subprocess
    commands launched from arbitrary worktree directories can import
    ``issue_orchestrator`` without depending on editable installs.
    """
    orch_bin = Path(sys.executable).parent
    orch_src = Path(__file__).resolve().parents[2]
    runtime_tool_assignments = " ".join(
        build_agent_tool_env_assignments(worktree_path)
    )
    return (
        f"export {ENV_PREFIX}COMPLETION_PATH='{completion_path}'"
        f" {ENV_PREFIX}SESSION_ID='{session_id}'"
        f" {ENV_PREFIX}AGENT_LABEL='{agent_label}'"
        f" {ENV_PREFIX}ISSUE_NUMBER='{issue_number}'"
        f"{config_exports(config.config_path)}"
        f"{api_port_export(config.control_api_port)}"
        f" {ENV_PREFIX}VALIDATION_OUTPUT_DIR='{run_dir}'"
        f" {ENV_PREFIX}RUN_DIR='{run_dir}'"
        f" {ENV_PREFIX}WORKTREE='{worktree_path}'"
        f" {runtime_tool_assignments}"
        f' PYTHONPATH="{orch_src}:${{PYTHONPATH:-}}"'
        f' PATH="{orch_bin}:$PATH"'
    )
