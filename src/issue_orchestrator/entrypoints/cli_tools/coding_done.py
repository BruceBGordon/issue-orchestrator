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
    ESCALATION_STATUSES,
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

#: Which statuses each stage applies to. ESCALATION_STATUSES is imported from
#: the remediation owner rather than restated, so the completion CLI and the
#: orchestrator cannot disagree about what an escalation is.
_STATUSES_REQUIRING_VALIDATION = frozenset({AgentStatus.COMPLETED})
_STATUSES_THAT_PUSH = ESCALATION_STATUSES | {AgentStatus.COMPLETED}


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

    print(f"\n{'=' * 60}")
    if phase == "post-validation":
        print("❌ WORKING TREE WAS DIRTIED BY VALIDATION — coding-done cannot complete")
    else:
        print("❌ WORKING TREE IS DIRTY — coding-done cannot complete")
    print(f"{'=' * 60}")
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
    print(f"{'=' * 60}")

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

            print(f"\n{'=' * 60}")
            print(
                f"⚠️  Auto-escalated to needs_human after {count} dirty-tree rejections."
            )
            print(
                "The orchestrator will route this to a human. Session "
                "will now exit cleanly rather than burn to the 90-minute "
                "timeout."
            )
            print(f"{'=' * 60}")

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
""",
    )

    parser.add_argument(
        "status",
        choices=["completed", "blocked", "needs_human"],
        help="Completion status",
    )

    # Completion fields
    parser.add_argument("--implementation", "-i", help="What was implemented")
    parser.add_argument("--problems", "-p", help="Problems encountered")

    # Blocked fields
    parser.add_argument("--reason", "-r", help="Why blocked")
    parser.add_argument("--attempted", "-a", help="What was attempted")
    parser.add_argument(
        "--blocked-by", "-b", type=int, nargs="+", help="Blocking issue numbers"
    )
    parser.add_argument(
        "--when-unblocked", "-w", help="Hint for when blocker is resolved"
    )

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
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be written"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed output"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="After writing completion, trigger orchestrator to resume processing.",
    )

    return parser


_DIRTY_TREE_HANDLERS: dict[
    DirtyTreeDisposition, Callable[[_DirtyTreeContext], None]
] = {
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


@dataclass(frozen=True)
class _CompletionRun:
    """The invariants of one ``coding-done`` invocation, resolved once.

    ``main`` used to thread these six values through 232 lines of inline
    stages, which is how the same dirty-tree decision came to be spelled out
    twice with different wording. Each stage now takes the run.
    """

    args: argparse.Namespace
    status: str
    issue_number: int | None
    record: Any
    worktree_root: Path
    managed: bool

    def dirty_context(self, dirty_files: list[str], phase: str) -> _DirtyTreeContext:
        """Bind this run's identity to a set of dirty files."""
        return _DirtyTreeContext(
            dirty_files=dirty_files,
            worktree_root=self.worktree_root,
            issue_number=self.issue_number,
            status=self.status,
            under_orchestrator=self.managed,
            phase=phase,
            record=self.record,
        )

    def log_outcome(self, message: str, *args: Any) -> None:
        if self.issue_number:
            logger.info(issue_log(self.issue_number, message), *args)


@dataclass(frozen=True)
class _ValidationStage:
    """What the agent-side validation stage produced, if it ran at all."""

    result: Any = None

    @property
    def passed(self) -> bool:
        return bool(self.result and self.result.passed)


def _print_captured_stream(
    raw_path: str | None, stream: str, note: str, max_lines: int
) -> None:
    """Echo a captured validation stream, tail-truncated.

    ``note`` annotates the opening marker only; the closing marker stays
    ``--- END <stream> ---`` because agents and tests match on that exact form.
    """
    if not raw_path:
        return
    path = Path(raw_path)
    if not path.exists():
        return
    content = path.read_text().strip()
    if not content:
        return
    header = f"{stream} {note}".strip()
    print(f"\n--- {header} ---")
    lines = content.split("\n")
    if len(lines) > max_lines:
        print(f"... ({len(lines) - max_lines} lines truncated)")
        lines = lines[-max_lines:]
    print("\n".join(lines))
    print(f"--- END {stream} ---")


def _fail_on_validation(run: _CompletionRun, validation_result: Any) -> None:
    """Report a failed validation run and exit non-zero."""
    print(f"\n{'=' * 60}")
    print("❌ VALIDATION FAILED — coding-done cannot complete")
    print(f"{'=' * 60}")
    print(f"\nReason: {validation_result.reason}")

    record = validation_result.record
    if record:
        _print_captured_stream(record.stderr_path, "STDERR", "(what failed)", 50)
        _print_captured_stream(record.stdout_path, "STDOUT", "", 30)

    print(f"\n{'=' * 60}")
    print("TO FIX: Read the errors above, fix them, then run coding-done again.")
    print("If you CANNOT fix after 2-3 attempts, use:")
    print(
        '  coding-done blocked --reason "Validation failing: <error>" --attempted "..."'
    )
    print(f"{'=' * 60}")

    run.log_outcome("coding-done outcome: status=%s validation=FAILED", run.status)
    sys.exit(1)


def _enforce_pre_validation_dirty_policy(run: _CompletionRun) -> None:
    """Stage 2: resolve the working tree before anything else runs.

    A publishing status must have a clean tree. ``blocked`` and ``needs_human``
    are the escalation path *for* a dirty file the agent must not resolve on its
    own (rung 3 of the remediation ladder), so rejecting them would leave that
    agent with no legal move at all -- the pressure that gets someone else's
    uncommitted work deleted. ``domain.dirty_remediation`` owns that call, and
    every other enforcement point asks it the same question.
    """
    _apply_dirty_tree_policy(
        run.dirty_context(check_dirty_files(run.worktree_root), "pre-validation")
    )

    # Cleared tree: a prior rejection counter means the agent has demonstrated
    # recovery, so subsequent rejections start from scratch.
    if run.managed:
        reset_rejection_counter(run.worktree_root, get_session_id())


def _open_run_assets(run: _CompletionRun) -> Any:
    if not run.record.session_id:
        logger.error("[coding-done] Validation requires session_id but none found")
        sys.exit(1)
    if run.managed:
        return require_orchestrator_run_assets_for_session(
            run.worktree_root, run.record.session_id
        )
    return FileSystemSessionOutput().start_run(run.worktree_root, run.record.session_id)


def _run_agent_validation(run: _CompletionRun) -> _ValidationStage:
    """Stage 3: the agent's own fast feedback loop.

    Deeper publish validation runs later, through the orchestrator-controlled
    pre-push/pre-publish gate.
    """
    if run.status in ESCALATION_STATUSES:
        print(
            f"Note: Skipping validation for '{run.status}' status "
            "(agent is reporting a problem)"
        )
        return _ValidationStage()
    if run.status not in _STATUSES_REQUIRING_VALIDATION:
        return _ValidationStage()

    validation_cmd, _ = load_validation_cmd(run.worktree_root)
    if not validation_cmd:
        return _ValidationStage()

    assets = _open_run_assets(run)
    validation_result = run_validation(
        run.worktree_root,
        session_output_dir=assets.run_dir,
        verbose=run.args.verbose,
    )

    if validation_result:
        validation_record_path = record_validation_artifacts(
            run.worktree_root, assets.validation_artifacts, validation_result
        )
        if validation_record_path is not None:
            run.record.validation_record_path = str(validation_record_path)
        if not validation_result.passed:
            _fail_on_validation(run, validation_result)

    return _ValidationStage(result=validation_result)


def _enforce_post_validation_dirty_policy(
    run: _CompletionRun, validation: _ValidationStage
) -> None:
    """Stage 3b: re-check the tree, because validation can dirty it.

    Closes the temporal variance with the orchestrator's publish gate: the
    validation command can write to the tree (auto-formatters, generated
    artifacts, integration-test output that is not gitignored). Without this
    the agent completes "successfully" while the orchestrator's later check
    finds dirty files and silently rejects the push -- the rework loop seen on
    issue #359 in tixmeup.
    """
    if not validation.passed:
        return
    post_validation_dirty = check_dirty_files(run.worktree_root)
    if not post_validation_dirty:
        return
    _handle_dirty_files_rejection(
        run.dirty_context(post_validation_dirty, "post-validation")
    )


def _enforce_preflight_push(run: _CompletionRun) -> None:
    """Stage 4: prove the push would land, for unmanaged runs only.

    Under the orchestrator this is skipped: it pushes through its own adapters
    with credentials, and a dry-run push here would trigger the pre-push hook
    inside the session timeout, which can fail on flaky tests and leave the
    agent unable to complete at all.
    """
    if run.status not in _STATUSES_THAT_PUSH:
        return
    if run.managed:
        if run.args.verbose:
            print("Skipping push preflight (orchestrator handles pushing)")
        return

    would_succeed, error, fix_hint = run_preflight_push_check(
        run.worktree_root, verbose=run.args.verbose
    )
    if would_succeed:
        return

    print(f"\n{'=' * 60}")
    print("❌ PUSH WOULD FAIL — coding-done cannot complete")
    print(f"{'=' * 60}")
    print(f"\nError: {error}")
    if fix_hint:
        print(f"\nTo fix: {fix_hint}")
    print(f"\n{'=' * 60}")
    print("Fix the issue above, then run coding-done again.")
    print(f"{'=' * 60}")

    run.log_outcome("coding-done outcome: status=%s push_preflight=FAILED", run.status)
    sys.exit(1)


def _finalize(run: _CompletionRun, validation: _ValidationStage) -> None:
    """Stage 5: write the marker and the record, then hand back to the operator."""
    write_marker_file(run.status)
    output_path = write_completion_record(run.record)

    print(f"Completion record written to: {output_path.resolve()}")
    print(f"Status: {run.status}")
    print(f"Session: {run.record.session_id}")
    if validation.result:
        print(f"Validation: {'passed' if validation.passed else 'failed'}")

    if run.args.resume:
        print("\nTriggering orchestrator resume...")
        resume_success, resume_error = trigger_orchestrator_resume(
            verbose=run.args.verbose
        )
        print(
            "Orchestrator resume triggered successfully."
            if resume_success
            else f"\n{resume_error}"
        )
    else:
        print(
            "\nThe orchestrator will process this record and perform the "
            "necessary actions."
        )

    run.log_outcome(
        "coding-done outcome: status=%s validation=%s resume=%s",
        run.status,
        "passed" if validation.passed else "skipped",
        "triggered" if run.args.resume else "not_requested",
    )


def main() -> None:
    """Run the completion stages in order; any stage may exit non-zero."""
    args = build_parser().parse_args()
    status = args.status
    issue_number = get_issue_number()

    if issue_number:
        logger.info(issue_log(issue_number, "coding-done starting: status=%s"), status)
    else:
        logger.info("[coding-done] Starting (standalone): status=%s", status)

    validate_fields(status, args)
    record = build_completion_record(status, args)

    if args.dry_run:
        print("--- DRY RUN: Would write this completion record ---")
        print(json.dumps(record.to_dict(), indent=2))
        print("--- END ---")
        return

    run = _CompletionRun(
        args=args,
        status=status,
        issue_number=issue_number,
        record=record,
        worktree_root=find_worktree_root(),
        # The retry budget (#5949) applies only under orchestrator-managed
        # sessions; standalone dev invocations get per-call session ids so the
        # counter never reaches the escalation threshold anyway.
        managed=_is_managed_session(),
    )

    _enforce_pre_validation_dirty_policy(run)
    validation = _run_agent_validation(run)
    _enforce_post_validation_dirty_policy(run, validation)
    _enforce_preflight_push(run)
    _finalize(run, validation)


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

        print(f"\n{'=' * 60}", file=sys.stderr)
        print("❌ CODING-DONE INTERNAL ERROR", file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)
        print(f"\nError: {e}", file=sys.stderr)
        print(f"\n{traceback.format_exc()}", file=sys.stderr)

        error_path = write_error_completion(error_msg, status)
        if error_path:
            print(f"\nError completion written to: {error_path}", file=sys.stderr)

        sys.exit(1)


if __name__ == "__main__":
    safe_main()
