"""Hook verification checks for doctor."""

from collections.abc import Mapping
import logging
from pathlib import Path

from ..types import Check
from ...config import Config
from ...hooks.hooks import get_adapter, summarize_ai_gate_message
from ...ai_gate_state import AiGateState, load_ai_gate_state, save_ai_gate_state
from ...repo_guardrails import (
    MANAGED_PRE_PUSH_MARKER,
    inspect_repo_guardrails,
)

logger = logging.getLogger(__name__)


def _check_hook_installation(
    config: Config, unique_types: set, unsupported_types: set
) -> tuple[Check, bool]:
    """Check if hooks are installed. Returns (check, hooks_ok)."""
    missing_hooks = []
    unsupported = []
    for agent_type in unique_types:
        if agent_type in unsupported_types:
            unsupported.append(agent_type.value)
            continue
        adapter = get_adapter(agent_type)
        if not adapter.is_installed(config.repo_root):
            missing_hooks.append(agent_type.value)

    supported_count = len(unique_types) - len(unsupported)

    if not unique_types:
        return Check(
            name="AI Agent Hooks (Installation)",
            status="warning",
            detail="No agents configured",
        ), False

    # Unsupported agents block launch unless dangerous mode allows them
    if unsupported and not config.dangerous.allow_unsupported_agents:
        return Check(
            name="AI Agent Hooks (Installation)",
            status="error",
            detail=(
                "Unsupported AI agents: "
                f"{', '.join(sorted(unsupported))}. "
                "Use Claude Code or set dangerous.allow_unsupported_agents: true"
            ),
        ), False

    if missing_hooks:
        return Check(
            name="AI Agent Hooks (Installation)",
            status="error",
            detail=(
                "Hooks not installed for: "
                f"{', '.join(sorted(missing_hooks))}. "
                "Run 'issue-orchestrator setup-hooks'"
            ),
        ), False

    # Unsupported agents allowed — warn but let supported agents proceed to verification
    if unsupported:
        return Check(
            name="AI Agent Hooks (Installation)",
            status="warning",
            detail=(
                f"{supported_count} supported agent(s) installed; "
                f"unsupported (allowed): {', '.join(sorted(unsupported))}"
            ),
        ), True  # hooks_ok=True so supported agents still get verified

    return Check(
        name="AI Agent Hooks (Installation)",
        status="ok",
        detail=f"{len(unique_types)} AI agent type(s) installed",
    ), True


def _check_full_verification(
    config: Config, unique_types: set, unsupported_types: set, hooks_ok: bool
) -> Check:
    """Run full hook verification."""
    if not hooks_ok:
        return Check(
            name="AI Agent Hooks (Verification)",
            status="info",
            detail="Skipped because hooks are not installed or unsupported",
        )

    full_failures = []
    for agent_type in unique_types:
        if agent_type in unsupported_types:
            continue
        adapter = get_adapter(agent_type)
        result_obj = adapter.verify_hooks(config.repo_root)
        if not result_obj.success:
            full_failures.append(
                f"{agent_type.value}: {', '.join(result_obj.checks_failed[:3])}"
            )

    if full_failures:
        return Check(
            name="AI Agent Hooks (Verification)",
            status="error",
            detail="; ".join(full_failures),
        )

    return Check(
        name="AI Agent Hooks (Verification)",
        status="ok",
        detail="All checks passed",
    )


def _get_unsupported_types(unique_types: set) -> set:
    """Determine which agent types are unsupported by querying adapters.

    Rather than maintaining a hardcoded list, we ask the adapter system
    directly - if get_adapter returns an UnsupportedAdapter, it's unsupported.
    """
    from ...hooks.hooks import get_adapter, UnsupportedAdapter

    unsupported = set()
    for agent_type in unique_types:
        adapter = get_adapter(agent_type)
        if isinstance(adapter, UnsupportedAdapter):
            unsupported.add(agent_type)
    return unsupported


def _run_ai_gate_tests(
    unique_types: set,
    unsupported_types: set,
    repo_root,
    expandable: dict,
) -> tuple[dict[str, tuple[bool, str]], list[str]]:
    """Run AI gate tests for each supported agent type.

    Returns (results dict, failures list).
    """
    results: dict[str, tuple[bool, str]] = {}
    failures: list[str] = []

    for agent_type in unique_types:
        if agent_type in unsupported_types:
            continue

        agent_name = agent_type.value
        expandable["agents_tested"].append(agent_name)

        adapter = get_adapter(agent_type)
        try:
            if not adapter.supports_ai_gate():
                results[agent_name] = (True, "skipped (not supported)")
                expandable["results"][agent_name] = {
                    "success": True,
                    "message": "skipped (not supported)",
                }
                continue
            success, message = adapter.test_ai_gate(repo_root)

            results[agent_name] = (success, message)
            expandable["results"][agent_name] = {"success": success, "message": message}

            if not success:
                failures.append(f"{agent_name}: {summarize_ai_gate_message(message)}")
        except Exception as e:
            error_msg = f"Error: {e}"
            results[agent_name] = (False, error_msg)
            expandable["results"][agent_name] = {"success": False, "message": error_msg}
            failures.append(f"{agent_name}: {summarize_ai_gate_message(error_msg)}")
            logger.warning("AI gate test failed for %s: %s", agent_name, e)

    return results, failures


def _ai_gate_trigger_reason(state: AiGateState, required_types: set[str]) -> str:
    """Explain why the current provider set needs a fresh AI gate run."""
    if state.last_check is None:
        return "first run"
    if state.recorded_agent_types != tuple(sorted(required_types)):
        return "configured agent set changed"
    return "interval exceeded"


def _check_ai_gate_report(
    config: Config,
    unique_types: set,
    unsupported_types: set,
    hooks_ok: bool,
) -> Check | None:
    """Check if AI gate test is stale and run verification if needed.

    Returns a Check with expandable details showing what was tested and results,
    or None if AI gate tests are disabled.
    """
    interval_days = config.hooks.ai_gate.interval_days
    if interval_days <= 0:
        return None  # Disabled

    state = load_ai_gate_state(config.repo_root)
    expandable: dict = {
        "ran": False,
        "triggered_by": None,
        "agents_tested": [],
        "results": {},
        "last_check": state.last_check.isoformat() if state.last_check else None,
    }

    if not hooks_ok:
        return Check(
            name="AI Gate",
            status="info",
            detail="Skipped - hooks not installed",
            expandable=expandable,
        )

    required_types = {agent_type.value for agent_type in unique_types}
    trigger_reason = _ai_gate_trigger_reason(state, required_types)
    if not state.is_stale(interval_days, required_types):
        # Use cached results — but only trust cached *successes*.
        # Cached failures always re-run: a transient issue (environment,
        # timing) shouldn't block every subsequent startup until someone
        # manually deletes the state file.
        cached_failures = []
        for agent_type, result in state.last_results.items():
            expandable["results"][agent_type] = {
                "success": result.success,
                "message": result.message,
            }
            if not result.success:
                cached_failures.append(agent_type)
        from datetime import datetime, timezone

        days_ago = (
            (datetime.now(timezone.utc).date() - state.last_check.date()).days
            if state.last_check
            else 0
        )

        if not cached_failures:
            return Check(
                name="AI Gate",
                status="ok",
                detail=f"Passed (last check {days_ago}d ago)",
                expandable=expandable,
            )
        # Fall through to re-run — don't trust cached failures
        trigger_reason = f"cached failure retry ({', '.join(cached_failures)})"

    # Run AI gate tests — clear stale cached results before populating fresh ones
    expandable["results"] = {}
    expandable["ran"] = True
    expandable["triggered_by"] = trigger_reason

    results, failures = _run_ai_gate_tests(
        unique_types, unsupported_types, config.repo_root, expandable
    )

    state.mark_checked(results, required_types)
    save_ai_gate_state(config.repo_root, state)

    if not failures:
        return Check(
            name="AI Gate",
            status="ok",
            detail=f"Passed ({len(results)} agent(s) verified)",
            expandable=expandable,
        )

    if config.hooks.ai_gate.dangerous_allow_failure:
        return Check(
            name="AI Gate",
            status="warning",
            detail=f"Failed ({len(failures)} agent(s)) - allowed by config",
            expandable=expandable,
        )

    return Check(
        name="AI Gate",
        status="error",
        detail=f"Failed: {'; '.join(failures)}",
        expandable=expandable,
    )


def check_hook_verification(config: Config) -> list[Check]:
    from ...hooks.hooks import detect_agents_from_config

    checks: list[Check] = []

    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    # Ask the adapter system what's unsupported (no hardcoded list)
    unsupported_types = _get_unsupported_types(unique_types)

    # Check hook installation
    install_check, hooks_ok = _check_hook_installation(
        config, unique_types, unsupported_types
    )
    checks.append(install_check)

    # Run full verification (gated on installation success)
    full_check = _check_full_verification(
        config, unique_types, unsupported_types, hooks_ok
    )
    checks.append(full_check)

    # Run AI gate tests (verification with state persistence)
    ai_gate_check = _check_ai_gate_report(
        config, unique_types, unsupported_types, hooks_ok
    )
    if ai_gate_check:
        checks.append(ai_gate_check)

    return checks


def check_repo_guardrails(config: Config) -> list[Check]:
    """Check repo-local pre-push guardrail state."""
    if not config.validation.publish.cmd:
        return [
            Check(
                name="Repo Guardrails",
                status="info",
                detail="Skipped - validation.publish.cmd not configured",
            )
        ]

    status = inspect_repo_guardrails(config.repo_root, config=config)

    if not status.pre_push_exists and not status.verify_exists:
        return [
            Check(
                name="Repo Guardrails",
                status="warning",
                detail="Not installed. Run 'issue-orchestrator setup-guardrails'.",
            )
        ]

    problems = _repo_guardrail_problems(config, status)
    managed_agents = _managed_agent_names(status)

    if problems:
        return [
            Check(
                name="Repo Guardrails",
                status="error",
                detail="; ".join(problems)
                + ". Run 'issue-orchestrator setup-guardrails'.",
            )
        ]

    hooks_path = status.hooks_path_config or ".git/hooks"
    detail = f"{hooks_path}/pre-push -> scripts/verify-pr.sh"
    if status.helper_exists:
        detail += " with repo-local hook helper"
    if managed_agents:
        detail += f"; managed AI hooks: {', '.join(managed_agents)}"
    return [
        Check(
            name="Repo Guardrails",
            status="ok",
            detail=detail,
        )
    ]


def _requires_repo_local_hook_helper(agent_hooks: Mapping[str, object]) -> bool:
    """Return True when configured agent hooks depend on the repo-local helper."""
    return any(agent_name != "codex" for agent_name in agent_hooks)


def _repo_guardrail_problems(config: Config, status) -> list[str]:
    problems = _repo_pre_push_problems(config, status)
    problems.extend(_repo_helper_problems(status))
    problems.extend(_managed_agent_hook_problems(config, status))
    return problems


def _repo_pre_push_problems(config: Config, guardrails) -> list[str]:
    problems: list[str] = []
    if not guardrails.pre_push_exists:
        problems.append("pre-push hook missing")
    elif not guardrails.pre_push_executable:
        problems.append("pre-push hook is not executable")
    elif not guardrails.pre_push_calls_verify:
        problems.append("pre-push hook does not call scripts/verify-pr.sh")

    if not guardrails.verify_exists:
        problems.append("scripts/verify-pr.sh missing")
    elif not guardrails.verify_executable:
        problems.append("scripts/verify-pr.sh is not executable")
    elif config.config_path is not None:
        config_root = (config.repo_root / ".issue-orchestrator" / "config").resolve()
        try:
            expected_selection = (
                config.config_path.resolve().relative_to(config_root).as_posix()
            )
        except ValueError:
            expected_selection = None
        if guardrails.verify_selected_config != expected_selection:
            problems.append("scripts/verify-pr.sh configuration selection drifted")
    return problems


def _repo_helper_problems(status) -> list[str]:
    if not _requires_repo_local_hook_helper(status.agent_hooks):
        return []
    if not status.helper_exists:
        return ["scripts/agent-hooks/block_no_verify.py missing"]
    if not status.helper_executable:
        return ["scripts/agent-hooks/block_no_verify.py is not executable"]
    if not status.helper_managed:
        return ["scripts/agent-hooks/block_no_verify.py drifted"]
    return []


def _managed_agent_hook_problems(config: Config, status) -> list[str]:
    problems: list[str] = []
    for agent_name, agent_status in sorted(status.agent_hooks.items()):
        if not agent_status.installed:
            problems.append(f"{agent_name} hook wiring missing")
            continue
        problems.extend(_managed_agent_file_problems(config, agent_name, agent_status))
    return problems


def _managed_agent_file_problems(
    config: Config, agent_name: str, agent_status
) -> list[str]:
    problems: list[str] = []
    for file_status in agent_status.managed_files:
        relative = file_status.path.relative_to(config.repo_root)
        if not file_status.exists:
            problems.append(f"{agent_name} managed hook file missing: {relative}")
            continue
        if not file_status.executable and relative.suffix in {".sh", ".py"}:
            problems.append(
                f"{agent_name} managed hook file is not executable: {relative}"
            )
        if file_status.matches_template is False:
            problems.append(f"{agent_name} managed hook file drifted: {relative}")
    return problems


def _managed_agent_names(status) -> list[str]:
    return [
        agent_name
        for agent_name, agent_status in sorted(status.agent_hooks.items())
        if agent_status.installed
    ]


def check_worktree_hook_corruption(config: Config) -> list[Check]:
    """Detect ``pre-push.project`` files that contain the managed wrapper marker.

    A ``pre-push.project`` whose content is our managed wrapper is corrupt: the
    parent ``pre-push`` wrapper executes it by path, which recurses into the
    same file and forkbombs the push. The runtime recursion guards now block
    execution, but the doctor surface still flags the broken file so an
    operator repairs (or reinstalls) the hooks.
    """
    corrupt: list[Path] = []
    for candidate in _iter_project_hook_candidates(config.repo_root):
        try:
            content = candidate.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if MANAGED_PRE_PUSH_MARKER in content:
            corrupt.append(candidate)

    if not corrupt:
        return [
            Check(
                name="Pre-push Hook Corruption",
                status="ok",
                detail="No recursive pre-push.project files detected",
            )
        ]

    rel_paths = ", ".join(_display_path(config.repo_root, p) for p in corrupt)
    return [
        Check(
            name="Pre-push Hook Corruption",
            status="error",
            detail=(
                f"Corrupt pre-push.project detected ({len(corrupt)}): {rel_paths}. "
                "Contains the managed wrapper marker; executing it would recurse. "
                "Run 'issue-orchestrator setup-guardrails' or delete/rename the files."
            ),
        )
    ]


def _iter_project_hook_candidates(repo_root: Path) -> list[Path]:
    """Enumerate pre-push.project files across main-repo and worktree hooks dirs.

    Cost is O(worktrees): one directory read plus one stat per worktree. Fine
    for doctor's current call frequency (startup + manual/UI-triggered); if
    doctor ever moves to a hot-path polling cadence this is the hot-spot to
    cache.
    """
    candidates: list[Path] = []
    gitdir = _resolve_gitdir(repo_root)
    if gitdir is None:
        return candidates

    main_hook_dirs = [gitdir / "hooks"]
    configured = repo_root / ".githooks"
    if configured.is_dir():
        main_hook_dirs.append(configured)

    for hooks_dir in main_hook_dirs:
        project_hook = hooks_dir / "pre-push.project"
        if project_hook.is_file():
            candidates.append(project_hook)

    worktrees_root = gitdir / "worktrees"
    if worktrees_root.is_dir():
        for worktree_entry in worktrees_root.iterdir():
            project_hook = worktree_entry / "hooks" / "pre-push.project"
            if project_hook.is_file():
                candidates.append(project_hook)
    return candidates


def _resolve_gitdir(repo_root: Path) -> Path | None:
    git_path = repo_root / ".git"
    if git_path.is_dir():
        return git_path
    if git_path.is_file():
        try:
            content = git_path.read_text().strip()
        except OSError:
            return None
        if content.startswith("gitdir:"):
            gitdir = Path(content.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (repo_root / gitdir).resolve()
            return gitdir
    return None


def _display_path(repo_root: Path, candidate: Path) -> str:
    try:
        return str(candidate.relative_to(repo_root))
    except ValueError:
        return str(candidate)
