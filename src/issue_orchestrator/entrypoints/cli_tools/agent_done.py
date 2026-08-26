"""Shared core for agent completion commands (coding-done, reviewer-done).

This module contains shared utilities used by both coding-done and reviewer-done.
It is NOT a CLI entry point — use coding-done or reviewer-done instead.

Architecture principle: The agent reports intent; the orchestrator decides and executes.
"""

import os
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Optional, cast

from ...infra.env import get_env

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ...domain.models import (
        CompletionRecord,
        ProposedFollowUpIssue,
    )

try:
    from ...domain.models import (
        CompletionRecord as RuntimeCompletionRecord,
        CompletionOutcome as RuntimeCompletionOutcome,
        ProposedFollowUpIssue as RuntimeProposedFollowUpIssue,
        RequestedAction as RuntimeRequestedAction,
        COMPLETION_RECORD_PATH,
    )
except ImportError:
    from ._runtime_models import (
        CompletionRecord as RuntimeCompletionRecord,
        CompletionOutcome as RuntimeCompletionOutcome,
        ProposedFollowUpIssue as RuntimeProposedFollowUpIssue,
        RequestedAction as RuntimeRequestedAction,
        COMPLETION_RECORD_PATH,
    )
if not TYPE_CHECKING:
    CompletionRecord = RuntimeCompletionRecord
    CompletionOutcome = RuntimeCompletionOutcome
    ProposedFollowUpIssue = RuntimeProposedFollowUpIssue
    RequestedAction = RuntimeRequestedAction

RUNTIME_COMPLETION_RECORD: Any = RuntimeCompletionRecord
RUNTIME_COMPLETION_OUTCOME: Any = RuntimeCompletionOutcome
RUNTIME_PROPOSED_FOLLOW_UP_ISSUE: Any = RuntimeProposedFollowUpIssue
RUNTIME_REQUESTED_ACTION: Any = RuntimeRequestedAction
from ...control.validation import AgentGate, AgentGateResult
from ...domain.artifact_contracts import ValidationFailed, ValidationPassed
from ...domain.session_run import ValidationArtifactPaths
from ...execution.run_evidence import RunEvidenceRecorder
from ...execution.session_output_adapter import FileSystemSessionOutput
from .agent_callback import (
    api_request_headers as _api_request_headers,
    resolve_control_api_port as _resolve_control_api_port,
)


class AgentStatus:
    """Allowed completion statuses from CLI. Maps to CompletionOutcome."""
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


# Required fields per status - agents MUST provide these
REQUIRED_FIELDS = {
    AgentStatus.COMPLETED: ["implementation", "problems"],
    AgentStatus.BLOCKED: ["reason", "attempted"],
    AgentStatus.NEEDS_HUMAN: ["question"],
    AgentStatus.APPROVED: ["summary", "risk"],
    AgentStatus.CHANGES_REQUESTED: ["issues", "risk"],
}

# Map CLI status to CompletionOutcome
STATUS_TO_OUTCOME = {
    AgentStatus.COMPLETED: RUNTIME_COMPLETION_OUTCOME.COMPLETED,
    AgentStatus.BLOCKED: RUNTIME_COMPLETION_OUTCOME.BLOCKED,
    AgentStatus.NEEDS_HUMAN: RUNTIME_COMPLETION_OUTCOME.NEEDS_HUMAN,
    AgentStatus.APPROVED: RUNTIME_COMPLETION_OUTCOME.REVIEW_APPROVED,
    AgentStatus.CHANGES_REQUESTED: RUNTIME_COMPLETION_OUTCOME.REVIEW_CHANGES_REQUESTED,
}

# Map CLI status to requested actions
STATUS_TO_ACTIONS = {
    AgentStatus.COMPLETED: [
        RUNTIME_REQUESTED_ACTION.PUSH_BRANCH,
        RUNTIME_REQUESTED_ACTION.CREATE_PR,
        RUNTIME_REQUESTED_ACTION.POST_COMMENT,
    ],
    AgentStatus.BLOCKED: [
        RUNTIME_REQUESTED_ACTION.PUSH_BRANCH,
        RUNTIME_REQUESTED_ACTION.ADD_BLOCKED_LABEL,
        RUNTIME_REQUESTED_ACTION.POST_COMMENT,
    ],
    AgentStatus.NEEDS_HUMAN: [
        RUNTIME_REQUESTED_ACTION.PUSH_BRANCH,
        RUNTIME_REQUESTED_ACTION.ADD_NEEDS_HUMAN_LABEL,
        RUNTIME_REQUESTED_ACTION.POST_COMMENT,
    ],
    AgentStatus.APPROVED: [
        RUNTIME_REQUESTED_ACTION.ADD_CODE_REVIEWED_LABEL,
        RUNTIME_REQUESTED_ACTION.REMOVE_NEEDS_REWORK_LABEL,
        RUNTIME_REQUESTED_ACTION.REMOVE_CODE_REVIEW_LABEL,
        RUNTIME_REQUESTED_ACTION.POST_COMMENT,
    ],
    AgentStatus.CHANGES_REQUESTED: [
        RUNTIME_REQUESTED_ACTION.ADD_NEEDS_REWORK_LABEL,
        RUNTIME_REQUESTED_ACTION.REMOVE_CODE_REVIEW_LABEL,
        RUNTIME_REQUESTED_ACTION.POST_COMMENT,
    ],
}


def extract_pr_verification_status(pr_body: str) -> tuple[bool, str | None]:
    """Extract verification status from PR body.

    Looks for the completion-command verification marker that proves the PR
    was created through the proper coding-done/reviewer-done workflow.

    Args:
        pr_body: The PR description body text

    Returns:
        Tuple of (has_marker, token) where:
        - has_marker: True if verification marker is present
        - token: The verification token if present, None otherwise
    """
    import re

    # Look for verification marker pattern: <!-- agent-done-verified: <token> -->
    pattern = r'<!--\s*agent-done-verified:\s*([a-zA-Z0-9-]+)\s*-->'
    match = re.search(pattern, pr_body)

    if match:
        return (True, match.group(1))
    return (False, None)


def _copy_validation_log(
    *,
    worktree_root: Path,
    run_dir: Path,
    source_path: str | None,
    destination_name: str,
    manifest_key: str,
    session_output: FileSystemSessionOutput,
) -> None:
    """Copy one validation log artifact into the session run directory."""
    if not source_path:
        return
    src = worktree_root / source_path
    if not src.exists():
        return
    dest = run_dir / destination_name
    try:
        dest.write_text(src.read_text(errors="ignore"))
        session_output.update_manifest(run_dir, {manifest_key: str(dest)})
    except OSError:
        logger.debug("Failed to write validation log %s for %s", destination_name, run_dir)


def record_validation_artifacts(
    worktree_root: Path,
    validation_artifacts: ValidationArtifactPaths,
    validation_result: AgentGateResult,
) -> Path | None:
    """Attach validation artifacts to the session output for diagnostics."""
    run_dir = validation_artifacts.run_dir
    if not run_dir.is_dir():
        raise ValueError(f"validation run directory does not exist: {run_dir}")
    record = validation_result.record
    record_path = validation_result.record_path
    if not record_path and not record:
        return None

    session_output = FileSystemSessionOutput()
    run_record_path = validation_artifacts.record_path
    resolved_record_path = (
        run_record_path
        if run_record_path.exists()
        else Path(record_path) if record_path else None
    )
    # Outcome (status + reason) is written via the typed API so the
    # three legacy fields stay consistent. The record path is a
    # separate concern and goes through the unchecked merge.
    if validation_result.passed:
        outcome = ValidationPassed()
    else:
        outcome = ValidationFailed(
            reason=validation_result.reason or "validation failed"
        )
    session_output.update_validation_outcome(run_dir, outcome)
    if resolved_record_path is not None:
        # Path-pointer field is independent of the validation outcome
        # itself; it goes through the unchecked merge.
        session_output.update_manifest(
            run_dir,
            {"validation_record_path": str(resolved_record_path)},
        )

    if not record:
        return resolved_record_path

    _copy_validation_log(
        worktree_root=worktree_root,
        run_dir=run_dir,
        source_path=record.stdout_path,
        destination_name="validation-stdout.log",
        manifest_key="validation_stdout",
        session_output=session_output,
    )
    _copy_validation_log(
        worktree_root=worktree_root,
        run_dir=run_dir,
        source_path=record.stderr_path,
        destination_name="validation-stderr.log",
        manifest_key="validation_stderr",
        session_output=session_output,
    )
    RunEvidenceRecorder(session_output).record_validation_evidence(
        run_dir=run_dir,
        worktree=worktree_root,
        record=record,
        record_path=resolved_record_path,
        junit_xml_paths=_runtime_validation_junit_xml_paths(worktree_root),
    )
    return resolved_record_path


def _runtime_validation_junit_xml_paths(worktree: Path) -> tuple[str, ...]:
    from ...infra.config import load_runtime_validation_config

    validation_config = load_runtime_validation_config(worktree)
    return tuple(validation_config.get("junit_xml_paths", ()) or ())


def die(message: str) -> NoReturn:
    """Print error and exit with failure."""
    print(f"ERROR: {message}", file=sys.stderr)
    print("\nUse --help for usage information.", file=sys.stderr)
    sys.exit(1)


def get_session_id() -> str:
    """Get session ID from environment or generate one.

    The orchestrator sets ISSUE_ORCHESTRATOR_SESSION_ID when launching agents.
    Legacy ORCHESTRATOR_SESSION_ID is also accepted for compatibility.
    If not set, we generate a timestamp-based ID for standalone usage.
    """
    session_id = get_env("SESSION_ID") or os.environ.get("ORCHESTRATOR_SESSION_ID")
    if session_id:
        return session_id
    # Fallback for standalone usage
    return f"standalone-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def get_issue_number() -> Optional[int]:
    """Get issue number from environment.

    The orchestrator sets ISSUE_ORCHESTRATOR_ISSUE_NUMBER when launching agents.
    Legacy ORCHESTRATOR_ISSUE_NUMBER is also accepted for compatibility.
    Returns None if not set (standalone usage).
    """
    issue_str = get_env("ISSUE_NUMBER") or os.environ.get("ORCHESTRATOR_ISSUE_NUMBER")
    if issue_str:
        try:
            return int(issue_str)
        except ValueError:
            return None
    return None


def validate_fields(status: str, args: argparse.Namespace) -> None:
    """Validate all required fields are present for the status."""
    required = REQUIRED_FIELDS[status]
    missing = []

    for field in required:
        value = getattr(args, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(f"--{field.replace('_', '-')}")

    if missing:
        die(f"Status '{status}' requires: {', '.join(missing)}")


def format_comment_body(status: str, args: argparse.Namespace) -> str:  # noqa: C901 - distinct markdown templates for each status type
    """Format the comment body based on status and args."""
    if status == AgentStatus.COMPLETED:
        return f"""## Implementation

{args.implementation}

## Problems Encountered

{args.problems}"""

    elif status == AgentStatus.BLOCKED:
        blocked_by = ""
        if args.blocked_by:
            refs = ", ".join(f"#{n}" for n in args.blocked_by)
            blocked_by = f"\n**Blocked by:** {refs}"

        when_unblocked = ""
        if args.when_unblocked:
            when_unblocked = f"\n\n**When unblocked:** {args.when_unblocked}"

        return f"""## Blocked

**Reason:** {args.reason}{blocked_by}
**Attempted:** {args.attempted}{when_unblocked}"""

    elif status == AgentStatus.NEEDS_HUMAN:
        parts = [f"## Needs Human Input\n\n**Question:** {args.question}"]
        if args.context:
            parts.append(f"**Context:** {args.context}")
        if args.options:
            parts.append("**Options:**")
            for i, opt in enumerate(args.options, 1):
                parts.append(f"{i}. {opt}")
        if args.default:
            parts.append(f"**Default if no response:** {args.default}")
        return "\n".join(parts)

    elif status == AgentStatus.APPROVED:
        risk_emoji = {"low": "G", "medium": "Y", "high": "R"}[args.risk]
        checks_str = ", ".join(f"`{c}`" for c in (args.checks or []))
        return f"""## Code Review Approved

{args.summary}

---
<!-- VERDICT_START -->
**Verdict:** `approve`
**Risk:** {risk_emoji} `{args.risk}`
{f"**Checks passed:** {checks_str}" if checks_str else ""}
<!-- VERDICT_END -->"""

    else:  # CHANGES_REQUESTED
        risk_emoji = {"low": "G", "medium": "Y", "high": "R"}[args.risk]
        checks_str = ", ".join(f"`{c}`" for c in (args.checks_needed or []))
        return f"""## Changes Requested

{args.issues}

---
<!-- VERDICT_START -->
**Verdict:** `request_changes`
**Risk:** {risk_emoji} `{args.risk}`
{f"**Checks needed:** {checks_str}" if checks_str else ""}
<!-- VERDICT_END -->

*The work agent will be re-queued to address these issues.*"""


def _read_follow_up_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Failed to read follow-up issue file {path}: {exc}") from exc


def _parse_follow_up_json(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _parse_follow_up_jsonl(raw: str, path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL entry in follow-up issue file {path}: {exc}") from exc
        entries.append(item)
    return entries


def _coerce_follow_up_entries(parsed: Any, raw: str, path: Path) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    if parsed is not None:
        raise ValueError("Follow-up issue file must contain a JSON array or JSONL objects")
    return _parse_follow_up_jsonl(raw, path)


def load_follow_up_issues(path_value: str | None) -> list[ProposedFollowUpIssue] | None:
    """Load ancillary follow-up issue proposals from JSON or JSONL."""
    if not path_value:
        return None
    path = Path(path_value)
    raw = _read_follow_up_file(path)
    parsed = _parse_follow_up_json(raw)
    entries = _coerce_follow_up_entries(parsed, raw, path)
    return cast(
        list[ProposedFollowUpIssue],
        [RUNTIME_PROPOSED_FOLLOW_UP_ISSUE.from_dict(item) for item in entries],
    )


def build_completion_record(status: str, args: argparse.Namespace) -> CompletionRecord:
    """Build a CompletionRecord from CLI arguments."""
    runtime_outcome = STATUS_TO_OUTCOME[status]
    runtime_actions = STATUS_TO_ACTIONS[status]

    # Format summary
    if status == AgentStatus.COMPLETED:
        summary = f"Completed: {args.implementation[:100]}..."
    elif status == AgentStatus.BLOCKED:
        summary = f"Blocked: {args.reason[:100]}..."
    elif status == AgentStatus.NEEDS_HUMAN:
        summary = f"Needs human: {args.question[:100]}..."
    elif status == AgentStatus.APPROVED:
        summary = f"Approved: {args.summary[:100]}..."
    else:
        summary = f"Changes requested: {args.issues[:100]}..."

    # Build comment body
    comment_body = format_comment_body(status, args)
    follow_up_issues = load_follow_up_issues(getattr(args, "follow_up_file", None)) if status == AgentStatus.COMPLETED else None

    return cast(
        CompletionRecord,
        RUNTIME_COMPLETION_RECORD(
        session_id=get_session_id(),
        timestamp=datetime.now().isoformat(),
        outcome=runtime_outcome,
        summary=summary,
        requested_actions=runtime_actions,
        # Completion fields
        implementation=args.implementation if status == AgentStatus.COMPLETED else None,
        problems=args.problems if status == AgentStatus.COMPLETED else None,
        # Blocked fields
        blocked_reason=args.reason if status == AgentStatus.BLOCKED else None,
        blocked_by=args.blocked_by if status == AgentStatus.BLOCKED else None,
        attempted=args.attempted if status == AgentStatus.BLOCKED else None,
        when_unblocked=args.when_unblocked if status == AgentStatus.BLOCKED else None,
        # Needs human fields
        question=args.question if status == AgentStatus.NEEDS_HUMAN else None,
        context=args.context if status == AgentStatus.NEEDS_HUMAN else None,
        options=args.options if status == AgentStatus.NEEDS_HUMAN else None,
        default_action=args.default if status == AgentStatus.NEEDS_HUMAN else None,
        # Review fields
        review_summary=args.summary if status == AgentStatus.APPROVED else None,
        review_issues=args.issues if status == AgentStatus.CHANGES_REQUESTED else None,
        risk_level=args.risk if status in (AgentStatus.APPROVED, AgentStatus.CHANGES_REQUESTED) else None,
        checks_passed=args.checks if status == AgentStatus.APPROVED else None,
        checks_needed=args.checks_needed if status == AgentStatus.CHANGES_REQUESTED else None,
        # Comment to post
        comment_body=comment_body,
        # PR labels
        pr_labels=getattr(args, 'pr_labels', None),
        follow_up_issues=follow_up_issues,
        ),
    )


def write_completion_record(record: CompletionRecord) -> Path:
    """Write the completion record to JSON file.

    Returns the path to the written file.
    """
    # Reuse find_worktree_root() so the WORKTREE guard is applied here too
    worktree_root = find_worktree_root()

    # Orchestrator tells agent where to write via env var
    # This ensures each session type writes to a distinct file
    base_path = get_env("COMPLETION_PATH") or COMPLETION_RECORD_PATH
    output_path = worktree_root / base_path

    # Create all necessary parent directories
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # If file exists (e.g., second review after rework), add numeric suffix
    if output_path.exists():
        stem = output_path.stem  # e.g., "completion-agent_review"
        suffix = output_path.suffix  # e.g., ".json"
        parent = output_path.parent
        counter = 2
        while output_path.exists():
            output_path = parent / f"{stem}-{counter}{suffix}"
            counter += 1

    with open(output_path, "w") as f:
        json.dump(record.to_dict(), f, indent=2)

    # Emit event for debugging - shows where completion was written
    from ...infra.emit import emit_event
    emit_event("completion.written", {
        "outcome": record.outcome.value,
        "path": str(output_path.resolve()),
        "env_path": base_path,
    })

    return output_path


def write_marker_file(status: str) -> None:
    """Write marker file indicating a completion command was called.

    This is checked by the Stop hook to detect sessions that exit without
    calling coding-done/reviewer-done.
    """
    marker_file = Path(".agent-done-marker")
    marker_file.write_text(
        f"agent-done {status} called at {datetime.now().isoformat()}\n"
    )


class WorktreeMismatchError(SystemExit):
    """Raised when CWD does not match the expected worktree."""

    def __init__(self, cwd_root: Path, expected: Path) -> None:
        msg = (
            f"WORKTREE MISMATCH: You are in '{cwd_root}' but the orchestrator "
            f"expected '{expected}'.  cd back to the correct worktree and retry."
        )
        super().__init__(msg)


def find_worktree_root() -> Path:
    """Find the worktree root, guarding against cross-worktree confusion.

    If ``ISSUE_ORCHESTRATOR_WORKTREE`` is set (i.e. the session was launched
    by the orchestrator), the discovered git root **must** match.  This
    prevents agents that ``cd`` into stale worktrees from silently writing
    completion records in the wrong place.
    """
    cwd = Path.cwd()
    cwd_root = cwd
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            cwd_root = path
            break

    expected_raw = get_env("WORKTREE")
    if expected_raw:
        expected = Path(expected_raw).resolve()
        if cwd_root.resolve() != expected:
            raise WorktreeMismatchError(cwd_root, expected)

    return cwd_root


def load_validation_cmd(worktree: Path) -> tuple[Optional[str], int]:
    """Load quick validation configuration from the worktree's config file.

    Reads from .issue-orchestrator/config/ in the worktree.
    This ensures tests are deterministic - no env var leakage from parent processes.

    Args:
        worktree: Path to the worktree root

    Returns:
        Tuple of (command, timeout_seconds) or (None, 0) if not configured
    """
    from ...infra.config import load_runtime_validation_config

    try:
        validation_config = load_runtime_validation_config(worktree)
    except FileNotFoundError as exc:
        die(str(exc))

    quick_config = validation_config.get("quick", {}) or {}
    cmd = quick_config.get("cmd")
    if cmd:
        return cmd, quick_config.get("timeout_seconds", 300)

    return None, 0


def run_validation(
    worktree: Path,
    session_output_dir: Path,
    verbose: bool = False,
) -> Optional[AgentGateResult]:
    """Run validation if configured.

    Args:
        worktree: Path to the worktree root
        session_output_dir: Directory to write validation output
        verbose: Whether to print validation output

    Returns:
        AgentGateResult if validation was run, None if not configured
    """
    cmd, timeout = load_validation_cmd(worktree)
    if not cmd:
        return None

    if verbose:
        print(f"Running quick validation: {cmd}")

    from ...entrypoints.bootstrap_executor import build_validation_command_runner
    from ...execution import GitWorkingCopy

    gate = AgentGate(
        worktree,
        command_runner=build_validation_command_runner(),
        working_copy=GitWorkingCopy(),
        command=cmd,
        timeout_seconds=timeout,
    )
    result = gate.run(session_output_dir=session_output_dir)

    if verbose:
        if result.passed:
            print(f"✓ Validation passed: {result.reason}")
        else:
            print(f"✗ Validation failed: {result.reason}")

    return result


def run_preflight_push_check(worktree: Path, verbose: bool = False) -> tuple[bool, str | None, str | None]:
    """Check if git push would succeed by calling the orchestrator API.

    The agent environment has git credentials scrubbed, so we can't do this
    check directly. Instead, we call the orchestrator's API which has credentials.

    Args:
        worktree: Path to the worktree root
        verbose: Whether to print status messages

    Returns:
        Tuple of (would_succeed, error_message, fix_hint)
    """
    import urllib.request
    import urllib.error

    # Resolved by the shared agent-callback owner, which treats the
    # ``0`` auto-assign sentinel as unset (#6913).
    port = _resolve_control_api_port()
    if not port:
        # No port configured - skip preflight check
        # This happens when running coding-done/reviewer-done outside orchestrator context
        if verbose:
            print("Note: ISSUE_ORCHESTRATOR_API_PORT not set, skipping push preflight check")
        return True, None, None

    url = f"http://localhost:{port}/api/preflight-push"
    payload = json.dumps({"worktree": str(worktree)}).encode("utf-8")

    if verbose:
        print(f"Checking if push would succeed...")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers=_api_request_headers().to_mutable_mapping(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
            return (
                result.get("would_succeed", False),
                result.get("error"),
                result.get("fix_hint"),
            )
    except urllib.error.URLError as e:
        # Can't reach orchestrator - skip check (maybe running standalone)
        if verbose:
            print(f"Note: Could not reach orchestrator API: {e}")
        return True, None, None
    except Exception as e:
        if verbose:
            print(f"Note: Preflight check failed: {e}")
        return True, None, None


def write_error_completion(error_msg: str, status: str) -> Optional[Path]:
    """Write an error completion record when a completion command itself fails.

    This ensures the orchestrator knows something went wrong even if
    coding-done/reviewer-done crashes after the AI agent called it.
    """
    try:
        worktree_root = find_worktree_root()
        base_path = get_env("COMPLETION_PATH") or COMPLETION_RECORD_PATH
        output_path = worktree_root / base_path

        error_record = {
            "outcome": status,  # Preserve what the agent intended
            "agent_done_error": error_msg,
            "session_id": get_session_id(),
            "timestamp": datetime.now().isoformat(),
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(error_record, indent=2))
        return output_path
    except Exception:
        # If we can't even write the error record, there's nothing more we can do
        return None
