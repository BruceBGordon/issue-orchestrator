"""Coding agent completion CLI.

Used by coding and rework agents to signal completion. Enforces:
- Dirty-file check (working tree must be clean)
- Validation gate (tests/linting if configured)
- Preflight push check

Review agents use reviewer-done instead.
"""

import argparse
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_done import (
    AgentStatus,
    FileSystemSessionOutput,
    RUNTIME_COMPLETION_OUTCOME,
    RUNTIME_COMPLETION_RECORD,
    STATUS_TO_ACTIONS,
    build_completion_record,
    find_worktree_root,
    get_issue_number,
    get_session_id,
    load_validation_cmd,
    run_preflight_push_check,
    run_validation,
    validate_fields,
    write_completion_record,
    write_error_completion,
    write_marker_file,
    record_validation_artifacts,
)
from .dirty_retry_budget import (
    build_completion_record_for_escalation,
    build_escalation_payload,
    is_budget_exhausted,
    record_rejection,
    reset_rejection_counter,
)
from .orchestrator_resume import trigger_orchestrator_resume
from .orchestrator_run_assets import require_orchestrator_run_assets_for_session
from ...domain.dirty_remediation import (
    DirtyTreeDisposition,
    dirty_tree_disposition,
    rejection_hint_lines,
)
from ...infra.env import get_env
from ...infra.logging_config import issue_log
from ...infra.runtime_artifacts import (
    is_orchestrator_untracked_planted,
    is_runtime_managed_dirty_path,
)

import logging

logger = logging.getLogger(__name__)

CODING_STATUSES = [
    AgentStatus.COMPLETED,
    AgentStatus.BLOCKED,
    AgentStatus.NEEDS_HUMAN,
]


def _is_managed_session() -> bool:
    return bool(get_env("SESSION_ID") or os.environ.get("ORCHESTRATOR_SESSION_ID"))



def check_dirty_files(worktree_root: Path | None = None) -> list[str]:
    """Return dirty porcelain lines the agent is responsible for.

    Filters two categories:

    - Runtime metadata under ``.issue-orchestrator/`` and ``.claude/`` —
      always ignored, never source.
    - Orchestrator-planted sync targets under
      ``src/issue_orchestrator/entrypoints/cli_tools/`` — ignored **only
      when untracked**. A tracked modification in the orchestrator's own
      repo remains a legitimate developer edit and still counts as dirty.

    Uses ``--untracked-files=all`` so git lists each untracked file
    individually rather than summarising a subtree to its topmost
    untracked directory (``?? src/``). The summary form silently broke
    the prior prefix filter — ``src/`` doesn't match
    ``src/issue_orchestrator/entrypoints/cli_tools/``.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []  # Can't determine — don't block
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []  # Can't determine — don't block

    dirty: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4 or not line.strip():
            continue
        # Porcelain reserves columns 0-1 for the two-char XY status code
        # and col 2 for a space separator; line[3:] is the path. The only
        # status class that affects filtering here is ``??`` (untracked) —
        # planted-path filtering is gated on it explicitly below. Every
        # other code (``M ``, `` M``, ``A ``, ``R ``, ``C ``, ``U ``, …)
        # represents a real tracked change and is reported as dirty with
        # no rename-target parsing applied. Rename lines carry their
        # ``old -> new`` form verbatim into the output; callers display
        # but do not re-parse them.
        status_code = line[:2]
        path = line[3:]
        is_untracked = status_code == "??"
        if is_runtime_managed_dirty_path(path, worktree_root):
            continue
        if is_untracked and is_orchestrator_untracked_planted(path):
            continue
        dirty.append(line.strip())
    return dirty


#: How many dirty paths any operator-facing listing shows before eliding.
_DIRTY_PREVIEW_LIMIT = 15


@dataclass(frozen=True)
class _DirtyTreeContext:
    """Everything either dirty-tree outcome needs, gathered once.

    Both outcomes previously took the same five keyword arguments threaded
    through by hand, which is what let the two paths drift into disagreeing
    about what an agent should do with a file it did not create.
    """

    dirty_files: list[str]
    worktree_root: Path
    issue_number: int | None
    status: str
    under_orchestrator: bool
    phase: str
    record: Any = None


def _preserve_dirty_files_on_escalation(ctx: _DirtyTreeContext) -> None:
    """Accept an escalation on a dirty tree, leaving every file untouched.

    Nothing here writes to the working tree. The point is that the files
    survive: the agent reached rung 3 of the remediation ladder, where the
    correct move is to preserve what it cannot classify and hand it to a human.
    The list is folded into the record's comment body so whoever picks the
    escalation up sees exactly what was left behind, instead of discovering an
    unexplained dirty worktree later.
    """
    dirty_files = ctx.dirty_files
    preview = ", ".join(dirty_files[:_DIRTY_PREVIEW_LIMIT])
    remaining = len(dirty_files) - _DIRTY_PREVIEW_LIMIT
    if remaining > 0:
        preview = f"{preview} (+{remaining} more)"
    notice = (
        f"Working tree left dirty and preserved ({len(dirty_files)} file(s)): "
        f"{preview}. The agent escalated instead of resolving these; nothing "
        "was deleted, reverted, or ignored."
    )
    print(f"\n{notice}")
    record = ctx.record
    record.comment_body = (
        f"{record.comment_body}\n\n{notice}" if record.comment_body else notice
    )
    if ctx.issue_number:
        logger.info(
            issue_log(
                ctx.issue_number,
                "coding-done accepted escalation on dirty tree: "
                "status=%s dirty_files=%d",
            ),
            ctx.status,
            len(dirty_files),
        )


def _handle_dirty_files_rejection(ctx: _DirtyTreeContext) -> None:
    """Print actionable error, record rejection, escalate-or-exit non-zero.

    Used by both the pre-validation and post-validation dirty checks. The
    post-validation check exists to close the temporal variance with the
    orchestrator's publish gate: ``validate.sh`` can write to the tree
    (auto-formatters, generated artifacts) between the agent's pre-check
    and the orchestrator's later check, and without this the agent
    completes "successfully" while the orchestrator silently rejects the
    push and starts a rework loop.
    """
    dirty_files = ctx.dirty_files
    worktree_root = ctx.worktree_root
    issue_number = ctx.issue_number
    status = ctx.status
    under_orchestrator = ctx.under_orchestrator
    phase = ctx.phase

    print(f"\n{'='*60}")
    if phase == "post-validation":
        print("❌ WORKING TREE WAS DIRTIED BY VALIDATION — coding-done cannot complete")
    else:
        print("❌ WORKING TREE IS DIRTY — coding-done cannot complete")
    print(f"{'='*60}")
    print(f"\nUncommitted files ({len(dirty_files)}):")
    for entry in dirty_files[:_DIRTY_PREVIEW_LIMIT]:
        print(f"  {entry}")
    if len(dirty_files) > _DIRTY_PREVIEW_LIMIT:
        print(f"  ... and {len(dirty_files) - _DIRTY_PREVIEW_LIMIT} more")
    print()
    for line in rejection_hint_lines(phase):
        print(line)
    if phase != "post-validation":
        print("If you modified contracts or schemas, regenerate artifacts first.")
    print("Then run coding-done again.")
    print(f"{'='*60}")

    if issue_number:
        logger.info(
            issue_log(
                issue_number,
                "coding-done outcome: status=%s phase=%s dirty_files=%d",
            ),
            status,
            phase,
            len(dirty_files),
        )

    if under_orchestrator:
        session_id = get_session_id()
        count = record_rejection(worktree_root, session_id)
        if is_budget_exhausted(count):
            payload = build_escalation_payload(
                session_id=session_id,
                dirty_files=dirty_files,
                count=count,
            )
            escalation_record = build_completion_record_for_escalation(
                payload,
                completion_record_cls=RUNTIME_COMPLETION_RECORD,
                completion_outcome_cls=RUNTIME_COMPLETION_OUTCOME,
                status_to_actions=STATUS_TO_ACTIONS,
                needs_human_status=AgentStatus.NEEDS_HUMAN,
            )
            write_completion_record(escalation_record)
            write_marker_file("needs_human")
            reset_rejection_counter(worktree_root, session_id)

            print(f"\n{'='*60}")
            print(
                f"⚠️  Auto-escalated to needs_human after {count} "
                f"dirty-tree rejections."
            )
            print(
                "The orchestrator will route this to a human. Session "
                "will now exit cleanly rather than burn to the 90-minute "
                "timeout."
            )
            print(f"{'='*60}")

            trigger_orchestrator_resume(verbose=False)
            sys.exit(0)

    sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for coding-done."""
    parser = argparse.ArgumentParser(
        prog="coding-done",
        description="Complete coding/rework agent work with structured status reporting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  Completed successfully:
    coding-done completed --implementation "Added user auth" --problems "None"

  Completed with ancillary follow-up proposals:
    First write the ancillary proposals to a JSON or JSONL file, then pass
    --follow-up-file <existing-path> to the completed command above.

  Completed with resume (debug session):
    coding-done completed --implementation "Fixed the bug" --problems "None" --resume

  Blocked:
    coding-done blocked --reason "Need API credentials" --attempted "Checked env vars"

  Need human input:
    coding-done needs_human --question "Should we use OAuth or API keys?"

STATUSES:
  completed    - Work done, PR ready (requires: --implementation, --problems)
  blocked      - Cannot proceed (requires: --reason, --attempted)
  needs_human  - Need decision (requires: --question)
"""
    )

    parser.add_argument(
        "status",
        choices=["completed", "blocked", "needs_human"],
        help="Completion status"
    )

    # Completion fields
    parser.add_argument("--implementation", "-i", help="What was implemented")
    parser.add_argument("--problems", "-p", help="Problems encountered")

    # Blocked fields
    parser.add_argument("--reason", "-r", help="Why blocked")
    parser.add_argument("--attempted", "-a", help="What was attempted")
    parser.add_argument("--blocked-by", "-b", type=int, nargs="+", help="Blocking issue numbers")
    parser.add_argument("--when-unblocked", "-w", help="Hint for when blocker is resolved")

    # Needs human fields
    parser.add_argument("--question", "-q", help="Question for human")
    parser.add_argument("--context", "-c", help="Context for the question")
    parser.add_argument("--options", "-o", nargs="+", help="Available options")
    parser.add_argument("--default", help="Default action if no response")

    # PR options
    parser.add_argument("--pr-labels", nargs="+", help="Extra labels to add to the PR")
    parser.add_argument(
        "--follow-up-file",
        help=(
            "Path to JSON or JSONL file describing ancillary follow-up issues. "
            "Use this for unrelated fixes discovered while completing the assigned issue."
        ),
    )

    # Meta options
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="After writing completion, trigger orchestrator to resume processing.",
    )

    return parser


_DIRTY_TREE_HANDLERS: dict[DirtyTreeDisposition, Callable[[_DirtyTreeContext], None]] = {
    DirtyTreeDisposition.REJECT: _handle_dirty_files_rejection,
    DirtyTreeDisposition.PRESERVE_AND_ESCALATE: _preserve_dirty_files_on_escalation,
}


def _apply_dirty_tree_policy(ctx: _DirtyTreeContext) -> None:
    """Resolve a dirty tree the way this status is entitled to.

    The choice itself belongs to ``domain.dirty_remediation``, beside the
    ladder whose last rung depends on it; this dispatches on that answer so
    the entrypoint holds no copy of the policy.
    """
    if not ctx.dirty_files:
        return
    _DIRTY_TREE_HANDLERS[dirty_tree_disposition(ctx.status)](ctx)


def main() -> None:  # noqa: C901, PLR0912
    """Main entry point for coding-done."""
    parser = build_parser()
    args = parser.parse_args()
    status = args.status
    issue_number = get_issue_number()

    if issue_number:
        logger.info(issue_log(issue_number, "coding-done starting: status=%s"), status)
    else:
        logger.info("[coding-done] Starting (standalone): status=%s", status)

    # 1. Validate required fields
    validate_fields(status, args)

    # Build completion record
    record = build_completion_record(status, args)

    if args.dry_run:
        print("--- DRY RUN: Would write this completion record ---")
        print(json.dumps(record.to_dict(), indent=2))
        print("--- END ---")
        return

    worktree_root = find_worktree_root()

    # The retry budget (#5949) only applies under orchestrator-managed
    # sessions. Standalone dev invocations have per-call session ids
    # (``standalone-<timestamp>``) so the counter never reaches the
    # escalation threshold anyway, but gating explicitly avoids surprising
    # a developer whose workflow is to deliberately rerun ``coding-done``
    # against a dirty tree during testing.
    #
    # Two env vars, two eras: ``ISSUE_ORCHESTRATOR_SESSION_ID``
    # (via ``get_env("SESSION_ID")`` — the ``get_env`` helper adds the
    # ``ISSUE_ORCHESTRATOR_`` prefix) is the current contract;
    # ``ORCHESTRATOR_SESSION_ID`` is the legacy form still accepted for
    # compatibility. Short-circuit OR means the current form wins when
    # both are set — the hypothetical "both set but disagree" case
    # favours the current contract, which is the behaviour the agent
    # prompts emit.
    managed = _is_managed_session()

    # 2. Dirty check. A publishing status must have a clean tree. ``blocked``
    #    and ``needs_human`` are the escalation path *for* a dirty file the
    #    agent must not resolve on its own (rung 3 of the remediation ladder),
    #    so rejecting them here would leave that agent with no legal move: it
    #    cannot commit the file, must not delete it, and could not report it
    #    either. That dead end is exactly the pressure that gets someone's
    #    uncommitted work deleted. The orchestrator agrees these are safe to
    #    accept -- ``validate_worktree_state`` applies the dirty policy only
    #    when the record requests PUSH_BRANCH, which neither status does.
    dirty_files = check_dirty_files(worktree_root)
    _apply_dirty_tree_policy(
        _DirtyTreeContext(
            dirty_files=dirty_files,
            worktree_root=worktree_root,
            issue_number=issue_number,
            status=status,
            under_orchestrator=managed,
            phase="pre-validation",
            record=record,
        )
    )

    # Dirty check passed — if a prior rejection left a non-zero counter
    # the agent has demonstrated recovery, so clear it. Subsequent
    # rejections start from scratch rather than continuing the streak.
    if managed:
        reset_rejection_counter(worktree_root, get_session_id())

    # 3. Run quick validation if configured. This is the immediate feedback
    #    path for coding agents; deeper publish validation runs later through
    #    the orchestrator-controlled pre-push/pre-publish gate.
    validation_result = None
    statuses_requiring_validation = {AgentStatus.COMPLETED}
    assets = None
    if status in statuses_requiring_validation:
        validation_cmd, _ = load_validation_cmd(worktree_root)
        if validation_cmd:
            if not record.session_id:
                logger.error("[coding-done] Validation requires session_id but none found")
                sys.exit(1)
            if managed:
                assets = require_orchestrator_run_assets_for_session(
                    worktree_root,
                    record.session_id,
                )
            else:
                assets = FileSystemSessionOutput().start_run(
                    worktree_root,
                    record.session_id,
                )
            validation_result = run_validation(
                worktree_root,
                session_output_dir=assets.run_dir,
                verbose=args.verbose,
            )
    elif status in {AgentStatus.BLOCKED, AgentStatus.NEEDS_HUMAN}:
        print(f"Note: Skipping validation for '{status}' status (agent is reporting a problem)")

    if validation_result and assets is not None:
        validation_record_path = record_validation_artifacts(
            worktree_root,
            assets.validation_artifacts,
            validation_result,
        )
        if validation_record_path is not None:
            record.validation_record_path = str(validation_record_path)

    if validation_result and not validation_result.passed:
        print(f"\n{'='*60}")
        print("❌ VALIDATION FAILED — coding-done cannot complete")
        print(f"{'='*60}")
        print(f"\nReason: {validation_result.reason}")

        if validation_result.record and validation_result.record.stderr_path:
            stderr_path = Path(validation_result.record.stderr_path)
            if stderr_path.exists():
                stderr_content = stderr_path.read_text()
                if stderr_content.strip():
                    print(f"\n--- STDERR (what failed) ---")
                    lines = stderr_content.strip().split('\n')
                    if len(lines) > 50:
                        print(f"... ({len(lines) - 50} lines truncated)")
                        lines = lines[-50:]
                    print('\n'.join(lines))
                    print("--- END STDERR ---")

        if validation_result.record and validation_result.record.stdout_path:
            stdout_path = Path(validation_result.record.stdout_path)
            if stdout_path.exists():
                stdout_content = stdout_path.read_text()
                if stdout_content.strip():
                    print(f"\n--- STDOUT ---")
                    lines = stdout_content.strip().split('\n')
                    if len(lines) > 30:
                        print(f"... ({len(lines) - 30} lines truncated)")
                        lines = lines[-30:]
                    print('\n'.join(lines))
                    print("--- END STDOUT ---")

        print(f"\n{'='*60}")
        print("TO FIX: Read the errors above, fix them, then run coding-done again.")
        print("If you CANNOT fix after 2-3 attempts, use:")
        print('  coding-done blocked --reason "Validation failing: <error>" --attempted "..."')
        print(f"{'='*60}")

        if issue_number:
            logger.info(issue_log(issue_number, "coding-done outcome: status=%s validation=FAILED"), status)
        sys.exit(1)

    # 3b. Re-check dirty tree AFTER validation. Closes the temporal
    #     variance with the orchestrator's publish gate: validate.sh can
    #     write to the tree (auto-formatters, generated artifacts,
    #     integration-test outputs that aren't gitignored). Without this
    #     re-check the agent completed "successfully" while the
    #     orchestrator's later check found dirty files and silently
    #     rejected the push, producing the rework loop seen on issue
    #     #359 in tixmeup.
    if validation_result and validation_result.passed:
        post_validation_dirty = check_dirty_files(worktree_root)
        if post_validation_dirty:
            _handle_dirty_files_rejection(
                _DirtyTreeContext(
                    dirty_files=post_validation_dirty,
                    worktree_root=worktree_root,
                    issue_number=issue_number,
                    status=status,
                    under_orchestrator=managed,
                    phase="post-validation",
                    record=record,
                )
            )

    # 4. Run preflight push check
    #    Skip under orchestrator — the orchestrator handles pushing via its own
    #    adapters with credentials.  Running a dry-run push here triggers the
    #    pre-push hook inside the session timeout, which can fail on flaky tests
    #    and leave the agent unable to complete at all.
    statuses_that_push = {AgentStatus.COMPLETED, AgentStatus.BLOCKED, AgentStatus.NEEDS_HUMAN}
    if status in statuses_that_push and not managed:
        would_succeed, error, fix_hint = run_preflight_push_check(worktree_root, verbose=args.verbose)
        if not would_succeed:
            print(f"\n{'='*60}")
            print("❌ PUSH WOULD FAIL — coding-done cannot complete")
            print(f"{'='*60}")
            print(f"\nError: {error}")
            if fix_hint:
                print(f"\nTo fix: {fix_hint}")
            print(f"\n{'='*60}")
            print("Fix the issue above, then run coding-done again.")
            print(f"{'='*60}")

            if issue_number:
                logger.info(issue_log(issue_number, "coding-done outcome: status=%s push_preflight=FAILED"), status)
            sys.exit(1)
    elif status in statuses_that_push and managed:
        if args.verbose:
            print("Skipping push preflight (orchestrator handles pushing)")

    # 5. Write marker + completion record
    write_marker_file(status)
    output_path = write_completion_record(record)
    output_path_abs = output_path.resolve()

    print(f"Completion record written to: {output_path_abs}")
    print(f"Status: {status}")
    print(f"Session: {record.session_id}")
    if validation_result:
        print(f"Validation: {'passed' if validation_result.passed else 'failed'}")

    # Handle --resume flag
    if args.resume:
        print("\nTriggering orchestrator resume...")
        resume_success, resume_error = trigger_orchestrator_resume(verbose=args.verbose)
        if resume_success:
            print("Orchestrator resume triggered successfully.")
        else:
            print(f"\n{resume_error}")
    else:
        print("\nThe orchestrator will process this record and perform the necessary actions.")

    if issue_number:
        logger.info(
            issue_log(issue_number, "coding-done outcome: status=%s validation=%s resume=%s"),
            status,
            "passed" if validation_result and validation_result.passed else "skipped",
            "triggered" if args.resume else "not_requested",
        )


def safe_main() -> None:
    """Wrapper that catches unexpected errors and writes error completion."""
    status = "unknown"
    issue_number = get_issue_number()

    try:
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            status = sys.argv[1]
        main()
    except SystemExit:
        raise
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

        if issue_number:
            logger.error(issue_log(issue_number, "coding-done crashed: %s"), str(e))

        print(f"\n{'='*60}", file=sys.stderr)
        print("❌ CODING-DONE INTERNAL ERROR", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print(f"\nError: {e}", file=sys.stderr)
        print(f"\n{traceback.format_exc()}", file=sys.stderr)

        error_path = write_error_completion(error_msg, status)
        if error_path:
            print(f"\nError completion written to: {error_path}", file=sys.stderr)

        sys.exit(1)


if __name__ == "__main__":
    safe_main()
