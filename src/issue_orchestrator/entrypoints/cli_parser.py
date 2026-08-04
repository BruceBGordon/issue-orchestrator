"""Argparse registration for the issue-orchestrator CLI."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CommandHandler = Callable[[argparse.Namespace], int]


@dataclass(frozen=True)
class CLICommandHandlers:
    """Runtime command handlers used when building the CLI parser.

    The handlers are passed in by ``cli.main`` so tests can continue patching
    ``issue_orchestrator.entrypoints.cli.cmd_*`` before parsing and dispatch.
    """

    start: CommandHandler
    status: CommandHandler
    attach: CommandHandler
    switch: CommandHandler
    dashboard: CommandHandler
    output: CommandHandler
    pause: CommandHandler
    resume: CommandHandler
    tech_lead: CommandHandler
    health_review: CommandHandler
    refresh: CommandHandler
    restart: CommandHandler
    setup: CommandHandler
    init: CommandHandler
    test_reset: CommandHandler
    e2e_reset: CommandHandler
    audit: CommandHandler
    verify: CommandHandler
    setup_hooks: CommandHandler
    setup_guardrails: CommandHandler
    auth: CommandHandler
    keys: CommandHandler
    doctor: CommandHandler
    demo: CommandHandler
    trace: CommandHandler


# The public CLI command surface, declared as data. ``build_parser`` verifies
# the registered commands match this tuple exactly, so the surface cannot drift
# silently; ``docs/user/stability.md`` publishes it with a stability tier per
# command and ``tests/unit/test_public_api_surface_docs.py`` enforces that.
CLI_COMMANDS: tuple[str, ...] = (
    # Runtime
    "start",
    "status",
    "attach",
    "switch",
    "dashboard",
    "output",
    "pause",
    "resume",
    "tech_lead",
    "health-review",
    "refresh",
    "restart",
    # Setup
    "setup",
    "init",
    "test-reset",
    "e2e-reset",
    "audit",
    # Hooks and guardrails
    "verify",
    "setup-hooks",
    "setup-guardrails",
    # Credentials
    "auth",
    "keys",
    # Diagnostics
    "doctor",
    "demo",
    "trace",
)

__all__ = ["CLICommandHandlers", "CLI_COMMANDS", "build_parser"]


def build_parser(handlers: CLICommandHandlers) -> argparse.ArgumentParser:
    """Build the top-level CLI parser and register subcommands."""
    parser = argparse.ArgumentParser(
        description="Orchestrate AI agents working on GitHub issues"
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to config file (default: .issue-orchestrator/config/default.yaml)",
    )
    parser.add_argument(
        "--set",
        action="append",
        help="Override config value (path=value). Use YAML/JSON for lists or dicts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    _register_runtime_commands(subparsers, handlers)
    _register_setup_commands(subparsers, handlers)
    _register_hook_commands(subparsers, handlers)
    _register_auth_commands(subparsers, handlers)
    _register_utility_commands(subparsers, handlers)
    _verify_declared_command_surface(subparsers)

    return parser


def _verify_declared_command_surface(subparsers) -> None:
    """Fail fast when the registered commands drift from ``CLI_COMMANDS``.

    The declared tuple is what the stability doc publishes, so a command that
    exists but is undeclared (or vice versa) is a bug, not a nuance to discover
    later from a user report.
    """
    registered = set(subparsers.choices)
    declared = set(CLI_COMMANDS)
    if registered == declared:
        return
    raise RuntimeError(
        "CLI command surface drifted from CLI_COMMANDS. "
        f"Registered but undeclared: {sorted(registered - declared)}; "
        f"declared but not registered: {sorted(declared - registered)}. "
        "Update CLI_COMMANDS and docs/user/stability.md together."
    )


def _register_runtime_commands(subparsers, handlers: CLICommandHandlers) -> None:
    start_parser = subparsers.add_parser("start", help="Start the orchestrator")
    start_parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Run without dashboard UI (useful for CI/debugging)",
    )
    start_parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Clear test issues, create fresh ones, and run with filter_label=test-data",
    )
    start_parser.add_argument(
        "--milestone", type=str, default=None, help="Filter issues by milestone name"
    )
    start_parser.add_argument(
        "--milestones",
        type=str,
        default=None,
        help="Filter issues by milestone names (comma-separated)",
    )
    start_parser.add_argument(
        "--label",
        type=str,
        default=None,
        help="Filter issues by label (e.g., 'agent:test' for e2e testing)",
    )
    start_parser.add_argument(
        "--issue",
        type=int,
        default=None,
        help="Process only this specific issue number",
    )
    start_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what issues would be processed without launching sessions",
    )
    start_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose DEBUG-level logging to ~/.issue-orchestrator.log",
    )
    start_parser.add_argument(
        "--ui-mode",
        choices=["web"],
        default=None,
        help="UI mode: web (browser dashboard, default)",
    )
    start_parser.add_argument(
        "--port", type=int, default=8080, help="Port for web dashboard (default: 8080)"
    )
    start_parser.add_argument(
        "--api-port",
        type=int,
        default=None,
        dest="api_port",
        help="Port for control API (default: 19080, 0=disabled). Control API is always available regardless of UI mode.",
    )
    start_parser.add_argument(
        "--queue-refresh",
        type=int,
        default=None,
        help="Seconds between queue refreshes from GitHub (default: 600, 0=manual only)",
    )
    start_parser.add_argument(
        "--start-paused",
        action="store_true",
        help="Start with planning/session launch paused while keeping the dashboard available",
    )
    start_parser.add_argument(
        "--gh-audit",
        action="store_true",
        help="Enable GH audit reporting (overrides config)",
    )
    start_parser.add_argument(
        "--gh-audit-events",
        action="store_true",
        help="Emit GH audit events to the event stream (overrides config)",
    )
    start_parser.add_argument(
        "--gh-audit-file",
        type=str,
        default=None,
        help="Path for GH audit report output (supports {pid})",
    )
    start_parser.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help="Max issues to start processing this session (default: 0=unlimited)",
    )
    start_parser.add_argument(
        "--review-label",
        type=str,
        default=None,
        help="Label to add to PRs for review (e.g., 'needs-tech-lead-review')",
    )
    start_parser.add_argument(
        "--review-threshold",
        type=int,
        default=None,
        help="Auto-trigger tech_lead review after N PRs with review label (default: 0=manual only)",
    )
    start_parser.set_defaults(func=handlers.start)

    status_parser = subparsers.add_parser("status", help="Show current status")
    status_parser.set_defaults(func=handlers.status)

    attach_parser = subparsers.add_parser(
        "attach", help="(deprecated) Use web dashboard instead"
    )
    attach_parser.add_argument(
        "issue_number",
        type=int,
        nargs="?",
        default=None,
        help="Optional: switch to this issue's window after attaching",
    )
    attach_parser.set_defaults(func=handlers.attach)

    switch_parser = subparsers.add_parser(
        "switch", help="(deprecated) Use web dashboard instead"
    )
    switch_parser.add_argument(
        "issue_number", type=int, help="GitHub issue number to switch to"
    )
    switch_parser.set_defaults(func=handlers.switch)

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="(deprecated) Use web dashboard instead"
    )
    dashboard_parser.set_defaults(func=handlers.dashboard)

    output_parser = subparsers.add_parser(
        "output", help="Show recent output from an issue's session"
    )
    output_parser.add_argument("issue_number", type=int, help="GitHub issue number")
    output_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=20,
        help="Number of lines to show (default: 20)",
    )
    output_parser.set_defaults(func=handlers.output)

    pause_parser = subparsers.add_parser("pause", help="Pause the orchestrator")
    pause_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port of running orchestrator (default: 8080)",
    )
    pause_parser.set_defaults(func=handlers.pause)

    resume_parser = subparsers.add_parser("resume", help="Resume the orchestrator")
    resume_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port of running orchestrator (default: 8080)",
    )
    resume_parser.set_defaults(func=handlers.resume)

    tech_lead_parser = subparsers.add_parser(
        "tech_lead",
        help="Dispatch the tech lead at specific issue(s) on demand",
    )
    tech_lead_parser.add_argument(
        "issues",
        nargs="+",
        type=int,
        help="Issue number(s) for the tech lead to investigate",
    )
    tech_lead_parser.add_argument(
        "--advise-only",
        action="store_true",
        help="Dial all tech_lead authority to propose (nothing auto-executes)",
    )
    tech_lead_parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="Per-issue seconds to wait for the investigation (default: 1800)",
    )
    tech_lead_parser.set_defaults(func=handlers.tech_lead)

    health_review_parser = subparsers.add_parser(
        "health-review",
        help="Run a whole-board tech_lead health review on demand (walk the floor)",
    )
    health_review_parser.add_argument(
        "--advise-only",
        action="store_true",
        help="Dial all tech_lead authority to propose (nothing auto-executes)",
    )
    health_review_parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="Seconds to wait for the health review to complete (default: 1800)",
    )
    health_review_parser.set_defaults(func=handlers.health_review)

    refresh_parser = subparsers.add_parser(
        "refresh", help="Request immediate refresh of issues from GitHub"
    )
    refresh_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port of running orchestrator (default: 8080)",
    )
    refresh_parser.set_defaults(func=handlers.refresh)

    restart_parser = subparsers.add_parser("restart", help="Restart the orchestrator")
    restart_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port of running orchestrator (default: 8080)",
    )
    restart_parser.add_argument(
        "--ui-mode", choices=["web"], default=None, help="UI mode for new orchestrator"
    )
    restart_parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging"
    )
    restart_parser.set_defaults(func=handlers.restart)


def _register_setup_commands(subparsers, handlers: CLICommandHandlers) -> None:
    setup_parser = subparsers.add_parser(
        "setup", help="Interactive setup wizard for new or existing projects"
    )
    setup_parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project directory to set up (default: prompts interactively)",
    )
    setup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what files would be created/modified without writing them",
    )
    setup_parser.set_defaults(func=handlers.setup)

    init_parser = subparsers.add_parser(
        "init", help="Initialize required GitHub labels"
    )
    init_parser.set_defaults(func=handlers.init)

    reset_parser = subparsers.add_parser(
        "test-reset", help="Reset test environment (teardown + setup)"
    )
    reset_parser.set_defaults(func=handlers.test_reset)

    e2e_reset_parser = subparsers.add_parser(
        "e2e-reset",
        help="Clear all E2E run history (runs, results, logs, timeline events)",
    )
    e2e_reset_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    e2e_reset_parser.set_defaults(func=handlers.e2e_reset)

    audit_parser = subparsers.add_parser(
        "audit", help="Audit queue - show why issues are queued or skipped"
    )
    audit_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    audit_parser.set_defaults(func=handlers.audit)


def _register_hook_commands(subparsers, handlers: CLICommandHandlers) -> None:
    verify_parser = subparsers.add_parser(
        "verify", help="Verify the orchestrator setup works correctly"
    )
    verify_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    verify_parser.add_argument(
        "--test-ai-gate",
        action="store_true",
        help="Test AI gating (hooks/execpolicy) for configured agents",
    )
    verify_parser.add_argument(
        "--ai-gate-timeout",
        type=int,
        default=60,
        help="Timeout in seconds for AI gate tests (default: 60)",
    )
    verify_parser.set_defaults(func=handlers.verify)

    setup_hooks_parser = subparsers.add_parser(
        "setup-hooks", help="Install AI agent hooks in target project"
    )
    setup_hooks_parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target project directory (default: repo_root from config)",
    )
    setup_hooks_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    setup_hooks_parser.set_defaults(func=handlers.setup_hooks)

    setup_guardrails_parser = subparsers.add_parser(
        "setup-guardrails",
        help="Install repo-local guardrails and AI agent hooks",
    )
    setup_guardrails_parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target project directory (default: repo_root from config)",
    )
    setup_guardrails_parser.add_argument(
        "--hooks-dir",
        type=str,
        default=None,
        help="Repo-local hooks directory to use for core.hooksPath (default: existing value or .githooks)",
    )
    setup_guardrails_parser.add_argument(
        "--validation-cmd",
        type=str,
        default=None,
        help="Override validation.publish.cmd when generating scripts/verify-pr.sh",
    )
    setup_guardrails_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    setup_guardrails_parser.set_defaults(func=handlers.setup_guardrails)


def _register_auth_commands(subparsers, handlers: CLICommandHandlers) -> None:
    auth_parser = subparsers.add_parser("auth", help="Manage GitHub authentication")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_action")

    auth_store_parser = auth_subparsers.add_parser(
        "store", help="Store GitHub token in OS keychain"
    )
    auth_store_parser.add_argument(
        "--token", "-t", type=str, help="GitHub token (will prompt if not provided)"
    )

    auth_subparsers.add_parser("clear", help="Clear GitHub token from OS keychain")

    auth_parser.set_defaults(func=handlers.auth)

    keys_parser = subparsers.add_parser("keys", help="Manage AI provider API keys")
    keys_subparsers = keys_parser.add_subparsers(dest="keys_action")

    keys_subparsers.add_parser("list", help="List stored API keys")

    keys_set_parser = keys_subparsers.add_parser(
        "set", help="Store an API key in keyring"
    )
    keys_set_parser.add_argument(
        "key_name", help="Key name (e.g., OPENAI_API_KEY or just 'openai')"
    )

    keys_delete_parser = keys_subparsers.add_parser(
        "delete", help="Remove an API key from keyring"
    )
    keys_delete_parser.add_argument("key_name", help="Key name to remove")

    keys_parser.set_defaults(func=handlers.keys)


def _register_utility_commands(subparsers, handlers: CLICommandHandlers) -> None:
    doctor_parser = subparsers.add_parser(
        "doctor", help="Run diagnostics on configuration and environment"
    )
    doctor_parser.add_argument("--config", "-c", type=str, help="Path to config file")
    doctor_parser.set_defaults(func=handlers.doctor)

    demo_parser = subparsers.add_parser(
        "demo", help="Demonstrate orchestrator features with mock data"
    )
    demo_parser.set_defaults(func=handlers.demo)

    trace_parser = subparsers.add_parser(
        "trace", help="Trace log entries for a specific issue"
    )
    trace_parser.add_argument("issue_number", type=int, help="Issue number to trace")
    trace_parser.set_defaults(func=handlers.trace)
