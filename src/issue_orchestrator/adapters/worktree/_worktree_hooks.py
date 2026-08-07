"""Git hook installation for worktrees."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ...infra.repo_guardrails import (
    LEGACY_MANAGED_PRE_PUSH_MARKER,
    MANAGED_PRE_PUSH_MARKER,
    quarantine_managed_hook_file,
)
from ...infra.hooks._python_path import shell_quote_issue_orchestrator_python
from ._worktree_git import _git_run

logger = logging.getLogger(__name__)

# Path to bundled hooks (in issue_orchestrator/hooks/, 3 levels up from this module)
HOOKS_DIR = Path(__file__).parent.parent.parent / "hooks"
PROJECT_COMMIT_MESSAGE_HOOKS = ("prepare-commit-msg", "applypatch-msg")

# Placeholder in the bundled pre-push template that we substitute with the
# orchestrator's interpreter path at install time. See the comment in
# ``hooks/pre-push`` for why baking the path beats env-var propagation.
ORCHESTRATOR_PYTHON_PLACEHOLDER = "@@ORCHESTRATOR_PYTHON@@"


def resolve_baked_python() -> str:
    """Return the shell-quoted interpreter literal for generated hooks.

    Prefers an operator override in ``ISSUE_ORCHESTRATOR_PYTHON`` when it
    points at an executable file, so tests and dev environments that
    configured a specific interpreter are honored even when the env var
    does not propagate to the eventual ``git push`` subprocess. Falls back
    to ``sys.executable`` — the interpreter running the orchestrator
    itself — which is always importable-safe by construction.
    """
    return shell_quote_issue_orchestrator_python()


def _render_orchestrator_pre_push(template_path: Path) -> str:
    """Read the bundled pre-push template and substitute the Python placeholder.

    Substituting at install time means the worktree hook works even if
    ``ISSUE_ORCHESTRATOR_PYTHON`` is missing from the orchestrator process's
    environment when ``git push`` runs (the original failure mode for
    target repos with no local ``.venv``).

    The path is rendered as a ``shlex.quote``'d shell literal so interpreter
    paths containing spaces, ``$``, backticks, quotes, or other metacharacters
    round-trip through the shell unchanged. The bundled template therefore
    places the placeholder *without* surrounding quotes; the quoting comes
    from ``shlex.quote``.
    """
    content = template_path.read_text()
    return content.replace(ORCHESTRATOR_PYTHON_PLACEHOLDER, resolve_baked_python())


def _install_orchestrator_pre_push(src: Path, dst: Path) -> None:
    """Install the orchestrator pre-push hook with placeholder substitution."""
    dst.write_text(_render_orchestrator_pre_push(src))
    dst.chmod(0o755)


def install_hooks(worktree_path: Path, pre_push_hook: Path | None = None) -> bool:
    """
    Install git hooks into a worktree.

    This function CHAINS hooks - if the project already has a pre-push hook,
    we preserve it and run it BEFORE the orchestrator's hook.

    Args:
        worktree_path: Path to the worktree
        pre_push_hook: Custom pre-push hook path (uses bundled if None)

    Returns:
        True when the orchestrator's pre-push guardrail ended up installed in
        the worktree hooks directory — either standalone as ``pre-push`` or as
        ``pre-push.orchestrator`` behind the chained wrapper. False when it did
        not: the worktree has no linked git dir, or the caller supplied a
        ``pre_push_hook`` path that does not exist. Callers that asked for
        enforced guardrails must treat False as a failure; the guardrail is the
        only thing stopping an agent from pushing past validation, so "asked
        for it" and "got it" are not the same fact.

    Note:
        Worktrees have a .git file (not directory) that points to the main repo.
        We need to find the actual hooks directory.

        If the project uses core.hooksPath (e.g., .githooks/), we override it
        for this worktree and copy the project hooks to the worktree's hooks dir.
    """
    worktree_path = Path(worktree_path)

    git_file = worktree_path / ".git"
    if not git_file.exists():
        return False

    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return False

    gitdir = Path(content.split(":", 1)[1].strip())
    hooks_dir = gitdir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Read from main repo config, not worktree config. Worktree config may have
    # our override from a previous install.
    main_git_dir = gitdir.parent.parent
    hooks_path_result = _git_run(
        main_git_dir,
        ["config", "--local", "--get", "core.hooksPath"],
        check=False,
    )
    custom_hooks_path = (
        hooks_path_result.stdout.strip()
        if hooks_path_result.returncode == 0
        else None
    )

    # Always set per-worktree hooksPath so hooks live with the worktree.
    # Explicit GIT_DIR prevents symlink resolution from writing to another worktree config.
    git_env = {
        **os.environ,
        "GIT_DIR": str(gitdir),
        "GIT_WORK_TREE": str(worktree_path),
    }
    _git_run(
        worktree_path,
        ["config", "extensions.worktreeConfig", "true"],
        check=False,
        env=git_env,
    )
    _git_run(
        worktree_path,
        ["config", "--worktree", "core.hooksPath", str(hooks_dir)],
        check=False,
        env=git_env,
    )
    _git_run(
        worktree_path,
        ["config", "--worktree", "core.worktree", str(worktree_path)],
        check=False,
        env=git_env,
    )
    _git_run(
        worktree_path,
        ["config", "--worktree", "core.bare", "false"],
        check=False,
        env=git_env,
    )
    logger.info(
        "Overriding core.hooksPath to %s for this worktree only (gitdir=%s)",
        hooks_dir,
        gitdir,
    )

    _install_project_commit_message_hooks(gitdir, custom_hooks_path, hooks_dir)

    project_hook = _resolve_project_pre_push_hook(gitdir, custom_hooks_path)
    dst_hook = hooks_dir / "pre-push"
    orchestrator_hook = pre_push_hook if pre_push_hook else HOOKS_DIR / "pre-push"

    # Self-heal: if a previous install left a corrupt pre-push.project (contains
    # the managed wrapper marker), quarantine it before writing new hooks.
    # Otherwise the chained wrapper would exec it and forkbomb the push.
    quarantine_managed_hook_file(hooks_dir / "pre-push.project")

    if project_hook is not None and project_hook.is_file():
        return _install_chained_hook(
            hooks_dir, dst_hook, project_hook, orchestrator_hook
        )
    if orchestrator_hook.exists():
        _install_orchestrator_pre_push(orchestrator_hook, dst_hook)
        logger.info("Installed orchestrator pre-push hook")
        return True

    logger.warning(
        "No orchestrator pre-push hook to install (missing source %s)",
        orchestrator_hook,
    )
    return False


def _project_hooks_base(gitdir: Path, custom_hooks_path: str | None) -> Path:
    main_git_dir = gitdir.parent.parent
    if custom_hooks_path:
        return main_git_dir.parent / custom_hooks_path
    return main_git_dir / "hooks"


def _resolve_project_hook(
    gitdir: Path,
    custom_hooks_path: str | None,
    hook_name: str,
) -> Path | None:
    candidate = _project_hooks_base(gitdir, custom_hooks_path) / hook_name
    if candidate.exists():
        return candidate
    return None


def _install_project_commit_message_hooks(
    gitdir: Path,
    custom_hooks_path: str | None,
    hooks_dir: Path,
) -> None:
    """Copy project commit-message hooks dropped by worktree hooksPath override."""
    for hook_name in PROJECT_COMMIT_MESSAGE_HOOKS:
        project_hook = _resolve_project_hook(gitdir, custom_hooks_path, hook_name)
        if project_hook is None or not project_hook.is_file():
            continue
        dst_hook = hooks_dir / hook_name
        shutil.copy2(project_hook, dst_hook)
        dst_hook.chmod(0o755)
        logger.info("Installed project %s hook in worktree", hook_name)


def _resolve_project_pre_push_hook(
    gitdir: Path, custom_hooks_path: str | None
) -> Path | None:
    """Locate the repo's real pre-push hook (the project's own logic).

    When the main repo has guardrails installed, its ``pre-push`` file is our managed
    wrapper and the repo's original hook lives at ``pre-push.project`` next to
    it. Returning the wrapper here would cause the worktree-level wrapper to
    chain to the repo wrapper, which then chains to its own sibling
    ``pre-push.project`` — which at the worktree level resolves back to a copy
    of the repo wrapper. That is the recursion we must not create.

    Returns ``None`` when there is no project hook to chain to (either the
    main-repo hook is the managed wrapper with no sibling original, or no
    pre-push exists at all). Callers must treat ``None`` as "install only the
    orchestrator hook".
    """
    candidate = _resolve_project_hook(gitdir, custom_hooks_path, "pre-push")
    if candidate is None:
        return None
    if _is_managed_pre_push(candidate):
        real_project_hook = candidate.parent / "pre-push.project"
        if real_project_hook.exists():
            logger.debug(
                "Repo pre-push is managed wrapper; using %s as project hook",
                real_project_hook,
            )
            return real_project_hook
        # Managed wrapper, no underlying project hook: nothing to chain.
        logger.debug(
            "Repo pre-push is managed wrapper with no sibling pre-push.project; "
            "skipping project-hook chain in worktree"
        )
        return None
    return candidate


def _is_managed_pre_push(path: Path) -> bool:
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    return (
        MANAGED_PRE_PUSH_MARKER in content
        or LEGACY_MANAGED_PRE_PUSH_MARKER in content
    )


def _install_chained_hook(
    hooks_dir: Path,
    dst_hook: Path,
    project_hook: Path,
    orchestrator_hook: Path,
) -> bool:
    """Install the project+orchestrator chain; report whether the guardrail landed.

    Returns False when the wrapper and the project hook are installed but the
    orchestrator hook source does not exist. The wrapper then runs only the
    project's own gate, which is exactly the silent guardrail loss the caller
    must be able to see.
    """
    # Fail fast: reaching here with a managed source hook means
    # _resolve_project_pre_push_hook's filter was bypassed (e.g. a caller
    # supplied an explicit project_hook). Silently skipping the copy would
    # change push semantics without a trace — the worktree would run the
    # orchestrator chain but never the repo's own lint/test gate. A loud
    # error from worktree creation is the right failure mode.
    if _is_managed_pre_push(project_hook):
        raise RuntimeError(
            "Refusing to install managed wrapper as pre-push.project: "
            f"{project_hook}. Resolve the main-repo hooks corruption first "
            "(see 'issue-orchestrator doctor')."
        )

    project_hook_copy = hooks_dir / "pre-push.project"
    shutil.copy2(project_hook, project_hook_copy)
    project_hook_copy.chmod(0o755)

    wrapper_content = _chained_hook_script()
    dst_hook.write_text(wrapper_content)
    dst_hook.chmod(0o755)

    if not orchestrator_hook.exists():
        logger.warning(
            "Installed chained pre-push wrapper without the orchestrator hook "
            "(missing source %s)",
            orchestrator_hook,
        )
        return False

    orch_hook_copy = hooks_dir / "pre-push.orchestrator"
    _install_orchestrator_pre_push(orchestrator_hook, orch_hook_copy)

    logger.info("Installed chained pre-push hooks (project + orchestrator)")
    return True


def _chained_hook_script() -> str:
    managed_marker = MANAGED_PRE_PUSH_MARKER
    legacy_managed_marker = LEGACY_MANAGED_PRE_PUSH_MARKER
    return f"""#!/bin/bash
# Chained pre-push hook: runs project hook first, then orchestrator hook
set -e

HOOKS_DIR="$(dirname "$0")"
AUDIT_LOG="$HOOKS_DIR/pre-push.log"
MANAGED_MARKER='{managed_marker}'
LEGACY_MANAGED_MARKER='{legacy_managed_marker}'

# Audit logging function
audit() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$AUDIT_LOG"
    echo "[orchestrator] $1"
}}

total_start=$(date +%s)
audit "Pre-push hook started (commit: $(git rev-parse --short HEAD))"

is_managed_wrapper() {{
    grep -qF "$MANAGED_MARKER" "$1" 2>/dev/null || grep -qF "$LEGACY_MANAGED_MARKER" "$1" 2>/dev/null
}}

# Run project's pre-push hook first (lint, tests, etc.)
#
# No env-var bypass: an ``ORCHESTRATOR_SKIP_PROJECT_HOOK`` escape hatch used
# to live here, but any process with shell access in the worktree could set
# it and neuter the project's lint/test gate. There are no remaining callers;
# removed for security issue #5987 (F5). To run without a project hook,
# arrange the worktree without a ``pre-push.project`` file.
if [ -x "$HOOKS_DIR/pre-push.project" ] && is_managed_wrapper "$HOOKS_DIR/pre-push.project"; then
    # Recursion guard: pre-push.project contains the managed wrapper marker,
    # meaning a prior install copied this wrapper (or the repo wrapper) into
    # it. Executing it would forkbomb the push.
    #
    # WORKTREE POLICY: hard-fail the push. A worktree is disposable — the
    # operator can reinstall hooks with `make worktree-setup` or by recreating
    # the worktree — so failing loudly here is safer than running only the
    # orchestrator chain and silently dropping the repo's lint/test gate.
    # (The main-repo wrapper takes a different stance — see
    # _render_repo_pre_push_hook in repo_guardrails.py.)
    audit "Refusing to exec managed wrapper as project hook (recursion guard): $HOOKS_DIR/pre-push.project"
    echo "pre-push: pre-push.project is the managed wrapper (corruption); refusing to recurse. Reinstall worktree hooks." >&2
    exit 1
elif [ -x "$HOOKS_DIR/pre-push.project" ]; then
    audit "Running project pre-push hook..."
    project_start=$(date +%s)
    if "$HOOKS_DIR/pre-push.project" "$@"; then
        project_end=$(date +%s)
        project_duration=$((project_end - project_start))
        audit "Project hook PASSED (duration=${{project_duration}}s)"
    else
        project_exit=$?
        project_end=$(date +%s)
        project_duration=$((project_end - project_start))
        audit "Project hook FAILED (exit ${{project_exit}} duration=${{project_duration}}s)"
        exit 1
    fi
else
    audit "No project hook found"
fi

# Then run orchestrator's trailer validation
if [ -x "$HOOKS_DIR/pre-push.orchestrator" ]; then
    audit "Running orchestrator pre-push hook..."
    orch_start=$(date +%s)
    if "$HOOKS_DIR/pre-push.orchestrator" "$@"; then
        orch_end=$(date +%s)
        orch_duration=$((orch_end - orch_start))
        audit "Orchestrator hook PASSED (duration=${{orch_duration}}s)"
    else
        orch_exit=$?
        orch_end=$(date +%s)
        orch_duration=$((orch_end - orch_start))
        audit "Orchestrator hook FAILED (exit ${{orch_exit}} duration=${{orch_duration}}s)"
        exit 1
    fi
else
    audit "No orchestrator hook found"
fi

total_end=$(date +%s)
total_duration=$((total_end - total_start))
audit "Pre-push hook completed successfully (total_duration=${{total_duration}}s)"
"""
