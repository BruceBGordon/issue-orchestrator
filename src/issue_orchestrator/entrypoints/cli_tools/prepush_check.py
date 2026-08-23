"""Pre-push validation check.

This module provides a CLI command for checking validation cache
and running validation if needed, for use in pre-push hooks.

Usage:
    python -m issue_orchestrator.entrypoints.cli_tools.prepush_check

Exit codes:
    0 = validation passed (or no validation configured)
    1 = validation failed
    2 = validation error
"""

import logging
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ...control.validation import PublishGate
from ...execution import GitWorkingCopy, LocalCommandRunner
from ...infra.runtime_artifacts import filter_runtime_managed_dirty_paths
from ...infra.validation_timings import append_validation_timing, build_timing_envelope

logger = logging.getLogger(__name__)


def find_worktree_root() -> Path:
    """Find the worktree root by looking for .git."""
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    return cwd


DIRTY_CHECK_MODES = {"tracked", "unstaged", "all", "off"}
DIRTY_FILE_LIST_LIMIT = 20

# Why "commit", never "stash": the publish gate records its result against HEAD,
# and the git pre-push hook reuses that record only when it matches the commit
# being pushed. Stashing hides the change without moving HEAD, so the gate would
# certify a SHA that is about to be replaced -- and a stashed change never
# reaches the push at all.
DIRTY_GUARD_COMMIT_FIRST_HINT = (
    "Do not stash: the publish gate records its result against HEAD, which "
    "the pre-push hook reuses. Commit first so the recorded result matches "
    "the commit that gets pushed."
)


@dataclass(frozen=True)
class PrepushValidationOutcome:
    """Result of the validation phase within pre-push."""

    exit_code: int
    allowed: bool
    cache_hit: bool
    reason: str
    elapsed_seconds: float
    record_exit_code: int | None
    record_timed_out: bool | None


def load_validation_cmd(worktree: Path) -> tuple[Optional[str], int, str]:
    """Load publish validation configuration from the worktree's config file.

    Reads from .issue-orchestrator/config/ in the worktree.
    This ensures tests are deterministic - no env var leakage from parent processes.

    Args:
        worktree: Path to the worktree root

    Returns:
        Tuple of (command, timeout_seconds, dirty_check)
    """
    from ...infra.config import load_runtime_validation_config

    validation_config = load_runtime_validation_config(worktree)

    publish_config = validation_config.get("publish", {}) or {}
    cmd = publish_config.get("cmd")
    timeout = publish_config.get("timeout_seconds", 1800)
    dirty_check = publish_config.get("dirty_check", "tracked")
    if cmd:
        return cmd, timeout, dirty_check

    return None, 0, dirty_check


def _print_dirty_files(files: list[str]) -> None:
    """Print dirty file list, clipped for readability."""
    if not files:
        return
    print(f"Dirty files (showing up to {DIRTY_FILE_LIST_LIMIT}):")
    for path in files[:DIRTY_FILE_LIST_LIMIT]:
        print(f"  - {path}")
    if len(files) > DIRTY_FILE_LIST_LIMIT:
        print(f"  ... and {len(files) - DIRTY_FILE_LIST_LIMIT} more")


def _filter_guard_excluded_files(files: list[str], worktree: Path) -> list[str]:
    """Filter out orchestrator runtime metadata from dirty-tree guard checks."""
    return filter_runtime_managed_dirty_paths(files, worktree)


def _run_dirty_guard(worktree: Path, mode: str, verbose: bool) -> Optional[int]:
    """Return exit code if dirty guard should block, else None."""
    if mode not in DIRTY_CHECK_MODES:
        if verbose:
            print(
                "Invalid validation.publish.dirty_check value: "
                f"{mode!r} (expected tracked|unstaged|all|off)"
            )
        return 1
    if mode == "off":
        return None
    working_copy = GitWorkingCopy()
    raw_dirty_files = working_copy.list_dirty_files(worktree, mode)
    if raw_dirty_files is None:
        # Enumeration failed — fail closed instead of silently passing
        # the gate (which would happen if we collapsed None to []).
        if verbose:
            print(
                "Could not enumerate dirty files; failing closed. "
                f"(validation.publish.dirty_check={mode!r})"
            )
        return 1
    dirty_files = _filter_guard_excluded_files(raw_dirty_files, worktree)
    if dirty_files:
        if verbose:
            if mode == "all":
                print(
                    "Working tree is dirty (tracked or untracked files); "
                    "commit them before running this gate. "
                    "Override with validation.publish.dirty_check."
                )
            else:
                print(
                    "Tracked files are dirty; commit them before running this "
                    "gate. Ignored files are allowed. "
                    "Override with validation.publish.dirty_check."
                )
            print(DIRTY_GUARD_COMMIT_FIRST_HINT)
            _print_dirty_files(dirty_files)
        return 1
    return None


def _run_validation_gate(
    worktree: Path,
    cmd: str,
    timeout: int,
    verbose: bool,
) -> PrepushValidationOutcome:
    """Run the publish gate and return the pre-push validation outcome."""
    if verbose:
        print(f"Validation configured: {cmd}")

    gate = PublishGate(
        worktree,
        command_runner=LocalCommandRunner(),
        working_copy=GitWorkingCopy(),
        command=cmd,
        timeout_seconds=timeout,
    )
    start = time.monotonic()
    result = gate.check(session_output_dir=_prepush_output_dir(worktree))
    duration = time.monotonic() - start
    logger.info(
        "Pre-push validation completed in %.2fs: allowed=%s cache_hit=%s",
        duration,
        result.allowed,
        result.cache_hit,
    )

    if result.allowed:
        cache_note = " (cached)" if result.cache_hit else ""
        if verbose:
            print(f"Validation passed{cache_note}: {result.reason} (%.2fs)" % duration)
        return PrepushValidationOutcome(
            exit_code=0,
            allowed=True,
            cache_hit=result.cache_hit,
            reason=result.reason,
            elapsed_seconds=duration,
            record_exit_code=result.record.exit_code if result.record else None,
            record_timed_out=result.record.timed_out if result.record else None,
        )

    if verbose:
        print(f"Validation failed: {result.reason} (%.2fs)" % duration)
        if result.record and result.record.stderr_path:
            stderr_path = worktree / result.record.stderr_path
            if stderr_path.exists():
                print("\nValidation stderr:")
                print(stderr_path.read_text()[:1000])
    return PrepushValidationOutcome(
        exit_code=1,
        allowed=False,
        cache_hit=result.cache_hit,
        reason=result.reason,
        elapsed_seconds=duration,
        record_exit_code=result.record.exit_code if result.record else None,
        record_timed_out=result.record.timed_out if result.record else None,
    )


def _record_prepush_summary(
    worktree: Path,
    *,
    wall_started_at: datetime,
    monotonic_started_at: float,
    head_sha: str | None,
    cmd: str | None,
    timeout: int,
    dirty_check: str,
    dirty_only: bool,
    dirty_elapsed_seconds: float | None,
    dirty_exit_code: int | None,
    validation_outcome: PrepushValidationOutcome | None,
    final_exit_code: int | None,
    phase: str,
    error_type: str | None,
) -> None:
    """Append an outer pre-push summary timing record."""
    payload: dict[str, object] = {
        "kind": "prepush_gate_summary",
        "head_sha": head_sha,
        "command": cmd,
        "timeout_seconds": timeout,
        "dirty_check": dirty_check,
        "dirty_only": dirty_only,
        "dirty_elapsed_seconds": (
            round(dirty_elapsed_seconds, 3)
            if dirty_elapsed_seconds is not None
            else None
        ),
        "dirty_exit_code": dirty_exit_code,
        "validation_elapsed_seconds": (
            round(validation_outcome.elapsed_seconds, 3) if validation_outcome else None
        ),
        "validation_cache_hit": validation_outcome.cache_hit
        if validation_outcome
        else None,
        "validation_allowed": validation_outcome.allowed
        if validation_outcome
        else None,
        "validation_reason": validation_outcome.reason if validation_outcome else None,
        "validation_record_exit_code": validation_outcome.record_exit_code
        if validation_outcome
        else None,
        "validation_record_timed_out": validation_outcome.record_timed_out
        if validation_outcome
        else None,
        "final_exit_code": final_exit_code,
        "phase": phase,
        "error_type": error_type,
    }
    payload.update(
        build_timing_envelope(
            wall_started_at=wall_started_at,
            monotonic_started_at=monotonic_started_at,
        )
    )
    append_validation_timing(worktree, payload)


def _prepush_output_dir(worktree: Path) -> Path:
    """Return a stable worktree-local diagnostics directory for hook validation output."""
    head_sha = GitWorkingCopy().get_head_sha(worktree) or "unknown-sha"
    base_dir = worktree / ".issue-orchestrator" / "diagnostics" / "prepush"
    _prune_prepush_output_dirs(base_dir, keep_names={head_sha}, max_keep=5)
    return base_dir / head_sha


def _prune_prepush_output_dirs(
    base_dir: Path,
    *,
    keep_names: set[str],
    max_keep: int,
) -> None:
    """Bound pre-push diagnostics retention to the current SHA plus recent siblings."""
    if not base_dir.exists():
        return
    children = sorted(
        base_dir.iterdir(),
        key=lambda child: child.stat().st_mtime,
        reverse=True,
    )
    keep_budget = max(max_keep - len(keep_names), 0)
    kept_recent = 0
    for child in children:
        if child.name in keep_names:
            continue
        if kept_recent < keep_budget:
            kept_recent += 1
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def run_prepush_check(verbose: bool = False, dirty_only: bool = False) -> int:
    """Run pre-push validation check.

    This function:
    1. Finds the worktree root
    2. Loads validation config
    3. Checks cache for existing valid result
    4. Runs validation if needed
    5. Returns exit code based on result

    Args:
        verbose: Whether to print status messages
        dirty_only: If True, enforce only dirty-tree policy and skip validation command.

    Returns:
        Exit code (0 = passed, 1 = failed, 2 = error)
    """
    worktree = find_worktree_root()
    wall_started_at = datetime.now(timezone.utc)
    monotonic_started_at = time.monotonic()
    cmd: str | None = None
    timeout = 0
    dirty_check = "tracked"
    dirty_elapsed_seconds: float | None = None
    dirty_exit_code: int | None = None
    validation_outcome: PrepushValidationOutcome | None = None
    final_exit_code: int | None = None
    phase = "started"
    error_type: str | None = None
    head_sha: str | None = None

    try:
        cmd, timeout, dirty_check = load_validation_cmd(worktree)
        head_sha = GitWorkingCopy().get_head_sha(worktree)

        dirty_started_at = time.monotonic()
        dirty_result = _run_dirty_guard(worktree, dirty_check, verbose)
        dirty_elapsed_seconds = time.monotonic() - dirty_started_at
        dirty_exit_code = dirty_result
        if dirty_result is not None:
            phase = "dirty_guard_failed"
            final_exit_code = dirty_result
            return dirty_result

        if dirty_only:
            if verbose:
                print("Dirty-tree check passed")
            phase = "dirty_only_passed"
            final_exit_code = 0
            return 0

        if not cmd:
            if verbose:
                print("No publish validation configured - allowing push")
            phase = "validation_unconfigured"
            final_exit_code = 0
            return 0

        validation_outcome = _run_validation_gate(worktree, cmd, timeout, verbose)
        phase = "validation_gate"
        final_exit_code = validation_outcome.exit_code
        return validation_outcome.exit_code
    except Exception as exc:
        phase = "error"
        error_type = type(exc).__name__
        raise
    finally:
        _record_prepush_summary(
            worktree,
            wall_started_at=wall_started_at,
            monotonic_started_at=monotonic_started_at,
            head_sha=head_sha,
            cmd=cmd,
            timeout=timeout,
            dirty_check=dirty_check,
            dirty_only=dirty_only,
            dirty_elapsed_seconds=dirty_elapsed_seconds,
            dirty_exit_code=dirty_exit_code,
            validation_outcome=validation_outcome,
            final_exit_code=final_exit_code,
            phase=phase,
            error_type=error_type,
        )


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run pre-push validation check",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress all output",
    )
    parser.add_argument(
        "--dirty-only",
        action="store_true",
        help="Run only dirty-tree guard (skip validation command)",
    )

    args = parser.parse_args()

    if args.quiet:
        logging.disable(logging.CRITICAL)

    try:
        exit_code = run_prepush_check(verbose=args.verbose, dirty_only=args.dirty_only)
        sys.exit(exit_code)
    except Exception as e:
        if not args.quiet:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
