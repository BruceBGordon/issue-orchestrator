"""Runtime setup step helpers for issue worktrees.

Each function here is one step of worktree runtime setup. The order in which
they run, and which of them run at all, is owned by
``_worktree_runtime_setup.WorktreeRuntimeSetup`` — not by callers.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from ...infra.runtime_artifacts import RUNTIME_IGNORE_FILE, load_runtime_ignore_patterns
from ._worktree_errors import WorktreeError
from ._worktree_git import _git_run

logger = logging.getLogger(__name__)

# Marker file name for worktree identity.
WORKTREE_ID_MARKER = ".issue-orchestrator/worktree-id"

# Claude Code settings to enforce completion command usage on exit.
# The Stop hook checks for a marker file that coding-done/reviewer-done creates.
CLAUDE_SETTINGS_FOR_AGENTS: dict[str, Any] = {
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "test -f .agent-done-marker || echo '⚠️  WARNING: Session ending without completion command! Run: coding-done completed/blocked/needs_human'",
                        "timeout": 5,
                    }
                ]
            }
        ]
    }
}

ALLOW_NO_VERIFY_DRY_RUN_PATH = Path(".issue-orchestrator") / "allow-no-verify-dry-run"
WORKTREE_LOCAL_EXCLUDE_PATHS: tuple[Path, ...] = (
    Path(".agent-done-marker"),
    Path(".venv"),
    Path(".claude/settings.json"),
    Path(".claude/scheduled_tasks.lock"),
    Path(WORKTREE_ID_MARKER),
    ALLOW_NO_VERIFY_DRY_RUN_PATH,
    Path(".issue-orchestrator/ai-gate-state.json"),
    Path(".issue-orchestrator/backups"),
    Path(".issue-orchestrator/diagnostics"),
    Path(".issue-orchestrator/dirty-rejection-count.json"),
    RUNTIME_IGNORE_FILE,
    Path(".issue-orchestrator/session-latest.json"),
    Path(".issue-orchestrator/sessions"),
    Path(".issue-orchestrator/timeline.sqlite"),
    Path(".issue-orchestrator/timeline.sqlite-shm"),
    Path(".issue-orchestrator/timeline.sqlite-wal"),
    Path(".issue-orchestrator/tool-homes"),
    Path(".issue-orchestrator/validation"),
)
WORKTREE_TRACKED_RUNTIME_PATHS: tuple[Path, ...] = (
    Path(".claude/settings.json"),
    Path(".issue-orchestrator/session-latest.json"),
)

__all__ = [
    "ALLOW_NO_VERIFY_DRY_RUN_PATH",
    "CLAUDE_SETTINGS_FOR_AGENTS",
    "WORKTREE_ID_MARKER",
    "WORKTREE_LOCAL_EXCLUDE_PATHS",
    "WORKTREE_TRACKED_RUNTIME_PATHS",
    "_configure_no_verify_dry_run",
    "_hide_runtime_artifacts_from_git_status",
    "_install_worktree_identity",
    "_link_repo_venv_into_worktree",
    "install_claude_settings",
    "sync_cli_tools",
]


def _configure_no_verify_dry_run(worktree_path: Path, allow: bool) -> None:
    """Enable or clear the ``--no-verify`` dry-run escape hatch for a worktree.

    The flag file is what the block-no-verify guardrail hook reads, so neither
    outcome of a dropped write is acceptable: a stale ``allow`` file leaves a
    hook bypass open for the whole session, and a missing one breaks the reuse
    push preflight. Fail the worktree setup instead of guessing.
    """
    flag_path = worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH
    try:
        if allow:
            flag_path.parent.mkdir(parents=True, exist_ok=True)
            flag_path.write_text("allow\n")
        elif flag_path.exists():
            flag_path.unlink()
    except OSError as exc:
        action = "set" if allow else "clear"
        raise WorktreeError(
            f"Failed to {action} no-verify dry-run flag at {flag_path}: {exc}"
        ) from exc


def _link_repo_venv_into_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Expose the repo venv inside a worktree so validation commands work there too."""
    source_venv = repo_root / ".venv"
    if not source_venv.exists():
        return

    target_venv = worktree_path / ".venv"
    if target_venv.is_symlink():
        try:
            if target_venv.resolve() == source_venv.resolve():
                return
        except OSError:
            pass
        target_venv.unlink()
    elif target_venv.exists():
        logger.warning(
            "Worktree already has a real .venv directory; leaving it in place: %s",
            target_venv,
        )
        return

    target_venv.symlink_to(source_venv, target_is_directory=True)
    logger.info(
        "Linked shared repo venv into worktree: %s -> %s", target_venv, source_venv
    )


def sync_cli_tools(worktree_path: Path) -> list[Path]:
    """
    Sync CLI tools from the orchestrator package to worktree.

    This ensures the worktree has the latest orchestrator tools (especially
    coding-done/reviewer-done) regardless of when the worktree was created or what branch
    it's on.

    Uses package-relative paths so this works even when the target repo is
    a foreign (non-orchestrator) repository.

    Args:
        worktree_path: Path to the worktree
    """
    package_root = Path(__file__).resolve().parents[2]
    src_cli_tools = package_root / "entrypoints" / "cli_tools"
    dst_cli_tools = (
        worktree_path / "src" / "issue_orchestrator" / "entrypoints" / "cli_tools"
    )

    if not src_cli_tools.exists():
        logger.debug(
            "No cli_tools in orchestrator package at %s, skipping sync", src_cli_tools
        )
        return []

    dst_cli_tools.mkdir(parents=True, exist_ok=True)

    synced_paths: list[Path] = []
    for src_file in src_cli_tools.glob("*.py"):
        dst_file = dst_cli_tools / src_file.name
        try:
            shutil.copy2(src_file, dst_file)
            synced_paths.append(dst_file.relative_to(worktree_path))
            logger.debug("Synced cli tool: %s -> %s", src_file.name, dst_file)
        except OSError as e:
            logger.warning("Failed to sync cli tool %s: %s", src_file.name, e)

    logger.info("Synced cli_tools from orchestrator package to worktree")
    return synced_paths


def _read_worktree_identity(marker_path: Path) -> str | None:
    """Return the persisted worktree identity, or None if it must be created.

    An unreadable or empty marker is treated as absent: for path-reuse
    detection it carries exactly as much information as a missing file. It is
    logged rather than swallowed so a filesystem problem is visible.
    """
    if not marker_path.exists():
        return None
    try:
        existing_id = marker_path.read_text().strip()
    except OSError as exc:
        logger.warning(
            "Unreadable worktree identity marker, regenerating: path=%s error=%s",
            marker_path,
            exc,
        )
        return None
    if not existing_id:
        logger.warning("Empty worktree identity marker, regenerating: %s", marker_path)
        return None
    return existing_id


def _install_worktree_identity(worktree_path: Path) -> str:
    """
    Install a unique identity marker in the worktree.

    This identity is used to detect path reuse - if a worktree is deleted
    and recreated at the same path, it gets a new identity. Jobs store
    the worktree_id and can detect when their worktree has been replaced.

    The identity is only created once - subsequent calls are idempotent.

    Args:
        worktree_path: Path to the worktree

    Returns:
        The worktree identity (existing or newly created)

    Raises:
        WorktreeError: If a new identity cannot be persisted. Returning an
            unpersisted id would hand jobs a value no later run can match,
            silently disabling path-reuse detection.
    """
    marker_path = worktree_path / WORKTREE_ID_MARKER

    existing_id = _read_worktree_identity(marker_path)
    if existing_id:
        logger.debug("Worktree identity exists: %s", existing_id)
        return existing_id

    worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(worktree_id)
    except OSError as exc:
        raise WorktreeError(
            f"Failed to install worktree identity at {marker_path}: {exc}"
        ) from exc
    logger.info("Installed worktree identity: %s", worktree_id)
    return worktree_id


def _worktree_git_dir(worktree_path: Path) -> Path | None:
    git_file = worktree_path / ".git"
    if not git_file.exists():
        return None
    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return None
    return Path(content.split(":", 1)[1].strip())


def _worktree_git_common_dir(worktree_path: Path) -> Path | None:
    git_dir = _worktree_git_dir(worktree_path)
    if git_dir is None:
        return
    commondir_file = git_dir / "commondir"
    if not commondir_file.exists():
        return git_dir
    common_dir = Path(commondir_file.read_text().strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


def _append_exclude_entries(exclude_path: Path, paths: list[Path]) -> None:
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    existing_text = ""
    if exclude_path.exists():
        existing_text = exclude_path.read_text()
        existing_lines = existing_text.splitlines()
    existing = {line.strip() for line in existing_lines if line.strip()}
    missing = [
        str(path).replace("\\", "/")
        for path in paths
        if str(path).replace("\\", "/") not in existing
    ]
    if not missing:
        return
    suffix = "\n" if existing_lines and not existing_text.endswith("\n") else ""
    with exclude_path.open("a", encoding="utf-8") as handle:
        if suffix:
            handle.write(suffix)
        for entry in missing:
            handle.write(f"{entry}\n")


def _write_worktree_exclude_entries(worktree_path: Path, paths: list[Path]) -> None:
    git_dir = _worktree_git_dir(worktree_path)
    if git_dir is None:
        return
    common_dir = _worktree_git_common_dir(worktree_path)
    exclude_paths = [git_dir / "info" / "exclude"]
    if common_dir is not None and common_dir != git_dir:
        exclude_paths.append(common_dir / "info" / "exclude")
    for exclude_path in exclude_paths:
        _append_exclude_entries(exclude_path, paths)


def _worktree_git_exclude_paths(
    worktree_path: Path, synced_cli_tool_paths: list[Path]
) -> list[Path]:
    """Return untracked paths that should be hidden from plain git status.

    This covers both runtime-only metadata and the synced CLI helper files we
    plant into foreign worktrees so first-run agents don't misread a clean
    session as a dirty repo before they make any user-facing change.
    """
    # Path normalisation intentionally widens trailing-slash patterns from
    # directory-only to file-or-directory when writing Git excludes. The
    # runtime-ignore file is an additive hide list, so broader exclusion is
    # safer than leaving agent-visible runtime artifacts in plain git status.
    repo_local_runtime_paths = [
        Path(pattern) for pattern in load_runtime_ignore_patterns(worktree_path)
    ]
    return [
        *WORKTREE_LOCAL_EXCLUDE_PATHS,
        *repo_local_runtime_paths,
        *synced_cli_tool_paths,
    ]


def _hide_runtime_artifacts_from_git_status(
    worktree_path: Path,
    synced_cli_tool_paths: list[Path],
) -> None:
    tracked_paths = [*WORKTREE_TRACKED_RUNTIME_PATHS, *synced_cli_tool_paths]
    for path in tracked_paths:
        normalized = str(path).replace("\\", "/")
        tracked = _git_run(
            worktree_path,
            ["ls-files", "--error-unmatch", normalized],
            check=False,
        )
        if tracked.returncode != 0:
            continue
        _git_run(
            worktree_path,
            ["update-index", "--skip-worktree", "--", normalized],
            check=False,
        )
    _write_worktree_exclude_entries(
        worktree_path,
        _worktree_git_exclude_paths(worktree_path, synced_cli_tool_paths),
    )


def _read_mergeable_claude_settings(settings_file: Path) -> dict[str, Any] | None:
    """Return existing settings safe to merge into, or None to write a fresh file.

    Missing, unreadable, non-JSON, or wrong-shaped settings all resolve to
    "replace": the Stop hook is a completion guardrail, so a broken file must
    never leave a worktree without it. Replacement discards operator content,
    so it is logged at WARNING rather than swallowed.
    """
    if not settings_file.exists():
        return None

    try:
        existing = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Replacing unreadable Claude settings: path=%s error=%s", settings_file, exc
        )
        return None

    if not isinstance(existing, dict):
        logger.warning("Replacing non-object Claude settings: %s", settings_file)
        return None

    hooks = existing.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        logger.warning(
            "Replacing Claude settings with non-object 'hooks': %s", settings_file
        )
        return None
    if hooks is not None and not isinstance(hooks.get("Stop", []), list):
        logger.warning(
            "Replacing Claude settings with non-list 'hooks.Stop': %s", settings_file
        )
        return None

    return existing


def _merge_agent_stop_hook(existing: dict[str, Any] | None) -> dict[str, Any]:
    """Return settings that contain the agent Stop hook exactly once."""
    if existing is None:
        return copy.deepcopy(CLAUDE_SETTINGS_FOR_AGENTS)

    merged = copy.deepcopy(existing)
    hooks = merged.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])
    our_hook = CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"][0]
    if our_hook not in stop_hooks:
        stop_hooks.append(copy.deepcopy(our_hook))
    return merged


def install_claude_settings(worktree_path: Path) -> None:
    """
    Install Claude Code settings to enforce completion command usage on exit.

    Creates .claude/settings.json in the worktree with a Stop hook
    that checks if a completion command was called before allowing exit.

    Args:
        worktree_path: Path to the worktree

    Raises:
        WorktreeError: If the settings file cannot be written. The Stop hook is
            the only reminder an agent gets to run a completion command, so a
            worktree without it is not a runnable session.
    """
    worktree_path = Path(worktree_path)
    claude_dir = worktree_path / ".claude"
    settings_file = claude_dir / "settings.json"

    settings = _merge_agent_stop_hook(_read_mergeable_claude_settings(settings_file))
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings, indent=2))
    except OSError as exc:
        raise WorktreeError(
            f"Failed to install Claude settings at {settings_file}: {exc}"
        ) from exc

    logger.debug("Installed Claude settings at %s", settings_file)
