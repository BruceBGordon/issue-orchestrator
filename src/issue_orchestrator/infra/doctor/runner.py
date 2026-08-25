"""Unified diagnostics for issue-orchestrator.

This module provides a single doctor function that both CLI and web can call.
"""

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import Config
from ...ports.command_runner import CommandRunner
from .types import DoctorResult
from .checks import (
    ai,
    clock_sync,
    config as config_checks,
    e2e,
    github,
    guardrails,
    hooks,
    milestones as milestone_checks,
    review,
    schema as schema_checks,
    tech_lead as tech_lead_checks,
    workspace,
)


logger = logging.getLogger(__name__)


def _timed(name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a doctor check, log its elapsed time, and return its result.

    Cold-start latency lives partly in these checks (especially the GitHub
    auth probe), so per-check timing makes it obvious which one dominates.
    """
    start = time.time()
    try:
        return fn(*args, **kwargs)
    finally:
        logger.info("[STARTUP_TIMING] doctor_check=%s elapsed=%.3fs", name, time.time() - start)


def run_doctor(
    config: Optional[Config] = None,
    config_path: Optional[Path] = None,
    runner: Optional[CommandRunner] = None,
) -> DoctorResult:
    """Run all diagnostic checks.

    Args:
        config: Optional pre-loaded config (used by web when orchestrator is running)
        config_path: Optional path to config file (used by CLI)
        runner: Optional CommandRunner for executing test commands (guardrails check)

    Returns:
        DoctorResult with all check results
    """
    result = DoctorResult()

    config, config_checks_list, should_stop = _timed(
        "load_config_with_checks", config_checks.load_config_with_checks, config, config_path
    )
    result.checks.extend(config_checks_list)
    result.checks.extend(_timed("github_auth", github.check_github_auth, config))
    result.checks.extend(_timed("ai_provider_clis", ai.check_ai_provider_clis))
    if should_stop or config is None:
        return result

    result.checks.extend(_timed("ai_keys", ai.check_ai_keys, config))

    result.checks.extend(_timed("config_validation", config_checks.check_config_validation, config))
    result.checks.extend(_timed("config_schema", config_checks.check_config_schema, config))
    result.checks.extend(_timed("template_variables", config_checks.check_template_variables, config))
    result.checks.extend(_timed("repository_config", config_checks.check_repository_config, config))
    result.checks.extend(_timed("worktree_remediation", config_checks.check_worktree_remediation, config))
    result.checks.extend(_timed("milestones", milestone_checks.check_milestones, config))

    result.checks.extend(_timed(
        "working_directory", workspace.check_working_directory, runner, config.repo_root
    ))
    result.checks.extend(_timed("hook_dependencies", workspace.check_hook_dependencies, config.repo_root))
    result.checks.append(
        _timed(
            "python_environment",
            workspace.check_python_environment,
            config.repo_root,
            runner,
        )
    )
    result.checks.extend(_timed("hook_verification", hooks.check_hook_verification, config))
    result.checks.extend(_timed("repo_guardrails", hooks.check_repo_guardrails, config))
    result.checks.extend(_timed("worktree_hook_corruption", hooks.check_worktree_hook_corruption, config))
    result.checks.extend(_timed("agents", workspace.check_agents, config, runner))

    result.checks.extend(_timed("schema_checks", schema_checks.run_schema_checks, config))
    result.checks.extend(_timed("code_review", review.check_code_review, config))
    result.checks.extend(_timed("tech_lead_labels", tech_lead_checks.check_tech_lead_labels, config))
    result.checks.extend(_timed("tech_lead_finding_routes", tech_lead_checks.check_tech_lead_finding_routes, config))
    result.checks.extend(_timed("e2e_runner", e2e.check_e2e_runner, config))
    result.checks.extend(_timed("guardrails", guardrails.check_guardrails, config, runner))
    result.checks.extend(_timed("clock_sync", clock_sync.check_clock_sync, config, runner))

    return result
