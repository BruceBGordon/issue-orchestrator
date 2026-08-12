"""Registry for runtime-managed artifact paths.

Two categories of paths that dirty-tree guardrails must ignore:

1. **Runtime metadata** — session logs, caches, UI state under
   ``.issue-orchestrator/`` and ``.claude/``. These are written by the
   orchestrator *and the agent's own session* during normal work and
   never represent user-authored source changes. Filtered in every
   dirty-tree surface regardless of tracked/untracked status.

2. **Orchestrator-planted source** — files the orchestrator copies into
   every worktree at creation time (see
   ``adapters/worktree/_worktree_runtime.sync_cli_tools``). In the
   orchestrator's own repo these files are tracked source (legitimate
   dev edits *must* count as dirty). In a *foreign* target repo the
   same files are untracked plantings that should never register as
   dirty — otherwise every ``coding-done`` call in a foreign worktree
   fails the guard, the agent tries to work around it by editing
   ``.git/info/exclude`` (Claude Code's sensitive-file gate blocks
   interactively), and sessions silently burn 90 minutes to the
   session-level timeout. Filtered **only when untracked** so developer
   modifications in the orchestrator repo still fire the guard.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

RUNTIME_IGNORE_FILE = Path(".issue-orchestrator/runtime-ignore")

RUNTIME_DIRTY_IGNORE_EXACT: frozenset[str] = frozenset(
    {
        ".issue-orchestrator/session-latest.json",
        ".issue-orchestrator/ai-gate-state.json",
        ".issue-orchestrator/timeline.sqlite",
        ".issue-orchestrator/timeline.sqlite-shm",
        ".issue-orchestrator/timeline.sqlite-wal",
        ".claude/scheduled_tasks.lock",
    }
)

RUNTIME_DIRTY_IGNORE_PREFIXES: tuple[str, ...] = (
    # Covers runtime artefacts git may surface from within the worktree:
    # sessions/, backups/, diagnostics/, plus anything ad-hoc the
    # orchestrator writes at the root (e.g. ``control-center.log``) that
    # hasn't (yet) been promoted into .gitignore. Broader than strictly
    # needed for subdirs already gitignored, but mirrors the historical
    # substring filter so every guard surface agrees on what counts as
    # runtime metadata — tracked ``config/*.yaml`` edits in the
    # orchestrator's own repo still slip past this filter, matching
    # long-standing behaviour.
    ".issue-orchestrator/",
    ".claude/",
)

# Paths ``sync_cli_tools`` plants into every worktree. Trailing slash is
# part of the prefix so the match doesn't accidentally swallow a future
# sibling like ``src/issue_orchestrator_tests/``.
ORCHESTRATOR_UNTRACKED_PLANTED_PREFIXES: tuple[str, ...] = (
    "src/issue_orchestrator/entrypoints/cli_tools/",
)

CLEANUP_SAFE_UNTRACKED_EXACT: frozenset[str] = frozenset(
    {
        *RUNTIME_DIRTY_IGNORE_EXACT,
        ".agent-done-marker",
        ".githooks/pre-push.log",
        ".mcp.json",
        str(RUNTIME_IGNORE_FILE),
        ".issue-orchestrator/allow-no-verify-dry-run",
        ".issue-orchestrator/completion.json",
        ".issue-orchestrator/dirty-rejection-count.json",
        ".issue-orchestrator/retry-prompt.md",
        ".issue-orchestrator/review-exchange-state.json",
        ".issue-orchestrator/review-exchange-turn-prompt.md",
        ".issue-orchestrator/review-report.md",
        ".issue-orchestrator/review-response.json",
        ".issue-orchestrator/reviewer-owned-head",
        ".issue-orchestrator/validation-errors.txt",
        ".issue-orchestrator/validation-state.json",
        ".issue-orchestrator/worktree-id",
    }
)

CLEANUP_SAFE_UNTRACKED_ROOTS: tuple[str, ...] = (
    ".issue-orchestrator/attempts",
    ".issue-orchestrator/backups",
    ".issue-orchestrator/diagnostics",
    ".issue-orchestrator/e2e-results",
    ".issue-orchestrator/persistent-pairs",
    ".issue-orchestrator/review-feedback",
    ".issue-orchestrator/sessions",
    ".issue-orchestrator/state",
    ".issue-orchestrator/tool-homes",
    ".issue-orchestrator/validation",
    ".venv",
)

CLEANUP_SAFE_UNTRACKED_PATTERNS: tuple[str, ...] = (
    ".issue-orchestrator/followups-*",
)

DEPENDENCY_OUTPUT_DIR_NAMES: frozenset[str] = frozenset({"node_modules"})


def _normalize_runtime_pattern(pattern: str) -> str:
    normalized = pattern.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def load_runtime_ignore_patterns(worktree: Path | None) -> tuple[str, ...]:
    """Load repo-local runtime artifact patterns.

    The conventional file is intentionally local to the target repository
    instead of a global orchestrator setting. Patterns are repo-relative.
    Blank lines and comments are ignored. Negations are not supported because
    this is an additive runtime-artifact list, not a full gitignore parser;
    negated lines are skipped with a warning.
    """
    if worktree is None:
        return ()
    ignore_file = Path(worktree) / RUNTIME_IGNORE_FILE
    try:
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()

    patterns: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            logger.warning(
                "Ignoring unsupported negated runtime-ignore pattern in %s: %s",
                ignore_file,
                stripped,
            )
            continue
        normalized = _normalize_runtime_pattern(stripped)
        if normalized:
            patterns.append(normalized)
    return tuple(dict.fromkeys(patterns))


def _matches_runtime_pattern(path: str, pattern: str) -> bool:
    """Match a repo-relative runtime-ignore pattern.

    This intentionally uses lightweight fnmatch semantics rather than full
    gitignore parsing. In particular, ``*`` may match ``/`` here while Git's
    exclude parser treats slash-separated path components more strictly. The
    runtime-ignore contract is additive and conservative: in-process guards
    may hide a broader runtime artifact set than plain Git status does.
    """
    normalized = path.replace("\\", "/")
    if pattern.endswith("/"):
        return normalized.startswith(pattern)
    if any(char in pattern for char in "*?["):
        return fnmatch(normalized, pattern)
    return normalized == pattern or normalized.startswith(f"{pattern}/")


def runtime_ignore_patterns(worktree: Path | None = None) -> tuple[str, ...]:
    """Return built-in plus repo-local runtime artifact patterns."""
    return (
        *RUNTIME_DIRTY_IGNORE_EXACT,
        *RUNTIME_DIRTY_IGNORE_PREFIXES,
        *load_runtime_ignore_patterns(worktree),
    )


def is_runtime_managed_dirty_path(path: str, worktree: Path | None = None) -> bool:
    """Return True when a dirty path is runtime-managed metadata.

    Applies regardless of tracked/untracked status — these paths are never
    source code in any repository, orchestrator or foreign.
    """
    normalized = path.replace("\\", "/")
    return any(
        _matches_runtime_pattern(normalized, pattern)
        for pattern in runtime_ignore_patterns(worktree)
    )


def filter_runtime_managed_dirty_paths(
    paths: list[str], worktree: Path | None = None
) -> list[str]:
    """Return dirty paths excluding runtime-managed metadata files."""
    return [path for path in paths if not is_runtime_managed_dirty_path(path, worktree)]


def is_orchestrator_untracked_planted(path: str) -> bool:
    """Return True for paths ``sync_cli_tools`` plants into worktrees.

    Caller MUST only consult this for paths git reports as untracked.
    Tracked/modified versions of the same paths in the orchestrator's
    own repo are legitimate dev edits that the dirty-tree guard must
    still report.

    Accepts both individual-file paths
    (``src/issue_orchestrator/entrypoints/cli_tools/coding_done.py``)
    and the directory-summary form git emits when ``status --porcelain``
    collapses an entirely-untracked subtree to its topmost directory
    (``src/``, ``src/issue_orchestrator/``).

    **Summary-form is defense-in-depth, not the hot path.** Every current
    caller avoids the collapse: ``check_dirty_files`` passes
    ``--untracked-files=all`` and ``GitWorkingCopy.list_dirty_files``
    enumerates with ``ls-files --others``. The summary branch exists so
    a future caller that forgets the flag and takes git's default
    (``--untracked-files=normal``) still gets the right answer. Do not
    remove it under the assumption that per-file input is always
    available.
    """
    normalized = path.replace("\\", "/")
    if not normalized:
        return False
    summary_form = normalized.endswith("/")
    for prefix in ORCHESTRATOR_UNTRACKED_PLANTED_PREFIXES:
        if normalized == prefix:
            return True
        if normalized.startswith(prefix):
            return True
        # Git's porcelain summary collapses untracked subtrees to their
        # topmost untracked directory. Accept the summary only when the
        # known planted prefix lies fully beneath it.
        if summary_form and prefix.startswith(normalized):
            return True
    return False


def filter_orchestrator_untracked_planted(paths: list[str]) -> list[str]:
    """Return only the untracked paths that are NOT orchestrator-planted.

    Input must already be scoped to git-untracked paths; the caller owns
    the tracked-vs-untracked classification (git status codes, separate
    ``ls-files --others`` invocation, etc.).
    """
    return [path for path in paths if not is_orchestrator_untracked_planted(path)]


def _path_matches_root(path: str, root: str) -> bool:
    normalized = _normalize_runtime_pattern(path)
    normalized_root = _normalize_runtime_pattern(root)
    return normalized == normalized_root or normalized.startswith(f"{normalized_root}/")


def _has_dependency_output_component(path: str) -> bool:
    parts = _normalize_runtime_pattern(path).split("/")
    return any(part in DEPENDENCY_OUTPUT_DIR_NAMES for part in parts)


def is_cleanup_safe_untracked_path(path: str, worktree: Path | None = None) -> bool:
    """Return True when an untracked path is owned runtime/dependency output.

    This is intentionally narrower than ``is_runtime_managed_dirty_path``. Dirty
    guards hide broad runtime roots, but forced worktree removal must only
    discard paths with explicit runtime/dependency ownership and path-boundary
    matches.
    """
    normalized = _normalize_runtime_pattern(path)
    if not normalized:
        return False
    if normalized in CLEANUP_SAFE_UNTRACKED_EXACT:
        return True
    if any(_path_matches_root(normalized, root) for root in CLEANUP_SAFE_UNTRACKED_ROOTS):
        return True
    if any(
        _matches_runtime_pattern(normalized, pattern)
        for pattern in CLEANUP_SAFE_UNTRACKED_PATTERNS
    ):
        return True
    if any(
        _matches_runtime_pattern(normalized, pattern)
        for pattern in load_runtime_ignore_patterns(worktree)
    ):
        return True
    if is_orchestrator_untracked_planted(normalized):
        return True
    return _has_dependency_output_component(normalized)


# --------------------------------------------------------------------------- #
# Forbidden-on-branch runtime artifacts (#6659)
#
# Distinct from the dirty-tree filters above. Those answer "is this dirty
# *working-tree* path runtime metadata I should ignore?". The guard below
# answers "is this path, present in a *committed* branch diff against base, a
# runtime artifact that must never have entered the branch?".
#
# The two are deliberately not the same set. The dirty filters broadly ignore
# *all* of ``.issue-orchestrator/`` and ``.claude/`` so foreign-repo plantings
# and live session writes don't fail the dirty guard. ``.claude/`` is excluded
# from the branch guard because this repo (and target repos) legitimately track
# hooks/settings/skills there. Under ``.issue-orchestrator/`` the repo tracks
# only a small allowlist of project-owned files; everything else is runtime
# output (review-exchange prompts, persistent-pair recordings, validation
# records, tool homes, sessions, …) and must not be committed onto an agent
# branch — it breaks the reviewer-worktree fast-forward checkout and bloats the
# review diff.
# --------------------------------------------------------------------------- #

# Project-owned files under ``.issue-orchestrator/`` that may legitimately be
# tracked on a branch. Allowlist, not denylist: any new runtime output path is
# rejected by default, so the guard fails safe as the runtime surface grows.
TRACKED_PROJECT_FILES_EXACT: frozenset[str] = frozenset(
    {
        ".issue-orchestrator/allow-no-verify-dry-run",
        str(RUNTIME_IGNORE_FILE),
    }
)

TRACKED_PROJECT_FILES_PREFIXES: tuple[str, ...] = (".issue-orchestrator/config/",)

# Branch content under these roots is guarded; anything here not allowlisted
# above is treated as a forbidden runtime artifact.
RUNTIME_ARTIFACT_BRANCH_ROOTS: tuple[str, ...] = (".issue-orchestrator/",)

_FORBIDDEN_ARTIFACT_PREVIEW = 8


def is_forbidden_branch_runtime_artifact(path: str) -> bool:
    """Return True when a branch path is a runtime artifact that must not be committed.

    Allowlist semantics: a path under a guarded root is forbidden unless it is an
    explicitly tracked project-owned file.
    """
    normalized = _normalize_runtime_pattern(path)
    if not normalized:
        return False
    if not any(normalized.startswith(root) for root in RUNTIME_ARTIFACT_BRANCH_ROOTS):
        return False
    if normalized in TRACKED_PROJECT_FILES_EXACT:
        return False
    if any(normalized.startswith(prefix) for prefix in TRACKED_PROJECT_FILES_PREFIXES):
        return False
    return True


def forbidden_branch_runtime_artifacts(paths: Iterable[str]) -> list[str]:
    """Return the deduped, sorted forbidden runtime artifacts among ``paths``."""
    found = {
        _normalize_runtime_pattern(path)
        for path in paths
        if is_forbidden_branch_runtime_artifact(path)
    }
    return sorted(found)


def build_forbidden_runtime_artifact_reason(paths: list[str]) -> str:
    """Build the operator-facing reason for a forbidden-artifact gate failure."""
    preview = ", ".join(paths[:_FORBIDDEN_ARTIFACT_PREVIEW])
    remaining = len(paths) - _FORBIDDEN_ARTIFACT_PREVIEW
    suffix = f" (+{remaining} more)" if remaining > 0 else ""
    return (
        "Branch contains issue-orchestrator runtime artifacts that must not be "
        "committed. These are session/review-exchange runtime outputs; leaving "
        "them on the branch breaks the reviewer-worktree fast-forward checkout "
        "and bloats the review diff. Remove them with `git rm --cached` and "
        "confirm they are gitignored before publishing. "
        f"Forbidden artifacts: {preview}{suffix}."
    )
