import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from issue_orchestrator.events import EventName
from issue_orchestrator.entrypoints.web import _timeline_event_actions


from issue_orchestrator.ports.working_copy import PushResult

from .conftest import StubWorkingCopy
from .scenario_dsl import scenario, script


class FailingPushWorkingCopy(StubWorkingCopy):
    def push(
        self,
        worktree,
        remote: str = "origin",
        force_with_lease: bool = True,
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult:
        return PushResult(success=False, branch=self.branch, remote=remote, message="simulated push failure")


class DirtyTrackedWorkingCopy(StubWorkingCopy):
    """Reports tracked-file changes — tracked-mode dirty check should block.

    Returns a non-empty ``list_dirty_files`` so the policy's
    ``blocking_files`` filter does not collapse to "runtime-only,
    pass". Without that, the bool short-circuit alone wouldn't fail
    the gate.
    """

    def has_tracked_changes(self, worktree, include_staged: bool = True) -> bool:
        return True

    def list_dirty_files(self, worktree, mode: str) -> list[str] | None:
        return ["src/some_module.py"]


class DirtyEnumerationFailsWorkingCopy(StubWorkingCopy):
    """``has_*_changes`` returns True but ``list_dirty_files`` returns
    ``None`` (e.g., git error). The orchestrator must fail closed
    rather than collapse to "blocking_files=[] -> pass" and silently
    let a possibly-dirty worktree through (#6159)."""

    def has_tracked_changes(self, worktree, include_staged: bool = True) -> bool:
        return True

    def list_dirty_files(self, worktree, mode: str) -> list[str] | None:
        return None


class DirtyUntrackedWorkingCopy(StubWorkingCopy):
    """Reports any uncommitted change (tracked or untracked) — all-mode blocks."""

    def has_uncommitted_changes(self, worktree) -> bool:
        return True

    def has_tracked_changes(self, worktree, include_staged: bool = True) -> bool:
        # Untracked-only case: tracked side is clean, but ``has_uncommitted_changes``
        # still trips because of the untracked file.
        return False

    def list_dirty_files(self, worktree, mode: str) -> list[str] | None:
        # Untracked file the orchestrator's runtime-managed filter
        # won't strip (not under ``.issue-orchestrator/`` etc.).
        return ["scratchpad.txt"]


class LeaseRenewerOnce:
    def __init__(self) -> None:
        self._used = False

    def check_renewals(self, sessions):
        if not sessions or self._used:
            return []
        self._used = True
        return list(sessions)


def _assert_run_dir_has_core_artifacts(run_dir: Path) -> None:
    """Fail-fast checks for run-scoped artifacts used by UI diagnostics/actions."""
    assert run_dir.exists(), f"run_dir missing: {run_dir}"

    manifest_path = run_dir / "manifest.json"
    assert manifest_path.exists(), f"manifest missing under run_dir: {manifest_path}"
    manifest = json.loads(manifest_path.read_text())

    # Ensure manifest identity is populated and points back to this run directory.
    assert manifest.get("run_dir") == str(run_dir)
    assert manifest.get("session_name"), f"session_name missing in {manifest_path}"
    assert manifest.get("run_id"), f"run_id missing in {manifest_path}"

    # Agent output must be available via at least one canonical log path.
    log_candidates = (
        run_dir / "terminal-recording.jsonl",
        run_dir / "ui-session.log",
        run_dir / "session.log",
        run_dir / "provider-runner" / "stdout.log",
        run_dir / "claude-session.jsonl",
    )
    usable = [p for p in log_candidates if p.exists() and p.stat().st_size > 0]
    assert usable, (
        "no usable run-scoped agent log candidates; "
        f"run_dir={run_dir} candidates={', '.join(str(p) for p in log_candidates)}"
    )


_skip_if_no_agent_cli = pytest.mark.skipif(
    shutil.which("claude") is None and shutil.which("codex") is None,
    reason="Agent CLI not available (claude/codex); skipping agent-backed scenario assertions",
)


def test_local_loop_happy_path_creates_non_draft_pr(scenario_repo: Path):
    scenario("happy_path_local_loop", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_validation_status("passed") \
        .expect_validation_artifacts(True) \
        .expect_pr(created=True, draft=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .expect_timeline_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .expect_timeline_event(EventName.ISSUE_PR_CREATED) \
        .run()


@pytest.mark.simulated_review_outcome(
    reviewer_responses=[
        {"response_type": "changes_requested", "response_text": "round 1", "getting_closer": True},
        {"response_type": "changes_requested", "response_text": "round 2", "getting_closer": True},
        {"response_type": "ok", "response_text": "round 3 ok", "getting_closer": True},
    ],
)
def test_local_loop_two_rounds_of_review(scenario_repo: Path):
    scenario("two_rounds", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_two_rounds.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_pr(created=True) \
        .expect_review_exchange_rounds(3) \
        .run()


@pytest.mark.simulated_review_outcome(
    reviewer_responses=[
        {"response_type": "changes_requested", "response_text": "disagree", "getting_closer": True},
        {"response_type": "ok", "response_text": "ok", "getting_closer": True},
    ],
)
def test_review_exchange_disagree_then_ok(scenario_repo: Path):
    scenario("disagree_then_ok", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_disagree_then_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_review_exchange_reason("reviewer_ok") \
        .run()


def test_review_exchange_noise_output_parses(scenario_repo: Path):
    scenario("noise_output", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_noise_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_review_exchange_round_response(reviewer_response_type="ok") \
        .run()


def test_validation_failure_queues_retry(scenario_repo: Path):
    scenario("validation_retry_queue", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=1) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .wait_for_event(EventName.SESSION_VALIDATION_RETRY_NEEDED) \
        .expect_validation_status("retry") \
        .expect_validation_artifacts(False) \
        .expect_pending_validation_retries(0) \
        .expect_active_validation_retry(retry_count=1) \
        .expect_timeline_event(EventName.SESSION_VALIDATION_RETRY_NEEDED) \
        .run()


def test_validation_retry_succeeds_after_retry(scenario_repo: Path):
    def _disable_grace_period(config) -> None:
        config.session_grace_period_seconds = 0
        config.session_log_activity_seconds = 0

    scenario("validation_retry_succeeds", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail_once.sh"), max_retries=1) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .configure(_disable_grace_period) \
        .wait_for_event(EventName.SESSION_VALIDATION_PASSED) \
        .wait_for(lambda orch: True, max_ticks=12) \
        .expect_event(EventName.SESSION_VALIDATION_RETRY_NEEDED) \
        .expect_validation_status("passed") \
        .expect_validation_artifacts(True) \
        .run()


def test_validation_retry_failure_exhausts_final_attempt(scenario_repo: Path):
    def _disable_grace_period(config) -> None:
        config.session_grace_period_seconds = 0
        config.session_log_activity_seconds = 0

    scenario("validation_retry_exhausts_final_attempt", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=1) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .configure(_disable_grace_period) \
        .wait_for_event(EventName.SESSION_VALIDATION_RETRY_NEEDED) \
        .wait_for_event(EventName.SESSION_VALIDATION_FAILED) \
        .wait_for(lambda orch: not orch.state.active_sessions, max_ticks=12) \
        .expect_validation_status("failed") \
        .expect_pending_validation_retries(0) \
        .expect_session_history_status({"validation_failed"}) \
        .run()


def test_draft_pr_queues_review_without_exchange(scenario_repo: Path):
    scenario("draft_pr_queues_review", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .expect_pr(created=True, draft=True) \
        .expect_event(EventName.REVIEW_QUEUED) \
        .expect_no_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .run()


def test_review_disabled_skips_queue(scenario_repo: Path):
    def _disable_review(config) -> None:
        config.review_enabled = False
        config.code_review_agent = None

    scenario("review_disabled", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .configure(_disable_review) \
        .expect_pr(created=True, draft=True) \
        .expect_no_event(EventName.REVIEW_QUEUED) \
        .run()


def test_skip_review_agent_suppresses_queue(scenario_repo: Path):
    def _skip_review(config) -> None:
        config.agents["agent:coder"].skip_review = True

    scenario("skip_review", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .configure(_skip_review) \
        .expect_pr(created=True, draft=True) \
        .expect_no_event(EventName.REVIEW_QUEUED) \
        .run()


def test_processing_failure_push_error_marks_blocked_failed(scenario_repo: Path):
    scenario("push_failure_blocked_failed", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .use_working_copy(FailingPushWorkingCopy()) \
        .expect_issue_label("publish-failed") \
        .expect_issue_comment_contains("Publishing Failed") \
        .run()


def test_session_crash_marks_for_conservative_auto_retry(scenario_repo: Path):
    def _disable_grace_period(config) -> None:
        config.session_grace_period_seconds = 0
        config.session_log_activity_seconds = 0

    scenario("session_crash_needs_human", scenario_repo) \
        .coder(script("coder_no_completion.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .configure(_disable_grace_period) \
        .wait_for_event(EventName.SESSION_FAILED) \
        .expect_issue_comment_contains("Session Interrupted") \
        .run()


def test_grace_period_keeps_session_running(scenario_repo: Path):
    def _configure_grace_period(config) -> None:
        config.session_grace_period_seconds = 300
        config.session_log_activity_seconds = 0

    ctx = scenario("grace_period_running", scenario_repo) \
        .coder(script("coder_no_completion.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .configure(_configure_grace_period) \
        .wait_for(lambda orch: len(orch.state.active_sessions) == 1, max_ticks=1) \
        .run()

    assert len(ctx.orch.state.active_sessions) == 1


def test_claim_loss_marks_blocked_and_comment(scenario_repo: Path):
    def _configure_grace_period(config) -> None:
        config.session_grace_period_seconds = 300
        config.session_log_activity_seconds = 0

    scenario("claim_loss_blocked", scenario_repo) \
        .coder(script("coder_no_completion.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .configure(_configure_grace_period) \
        .use_lease_renewer(LeaseRenewerOnce()) \
        .wait_for(
            lambda orch: "blocked:claim-lost" in orch.deps.repository_host.get_issue_labels(1),
            max_ticks=3,
        ) \
        .expect_issue_label("blocked:claim-lost") \
        .expect_issue_comment_contains("Work Cancelled") \
        .run()

def test_review_exchange_cache_skips_agent_run(scenario_repo: Path):
    ctx1 = scenario("cache_skips_first", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .run()
    assert ctx1 is not None

    ctx2 = scenario("cache_skips_second", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_no_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .run()
    assert ctx2 is not None


def test_review_exchange_cache_requires_validation(scenario_repo: Path):
    ctx1 = scenario("cache_validation_first", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok_with_validation.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=True) \
        .expect_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .expect_validation_status("passed") \
        .run()
    assert ctx1 is not None

    ctx2 = scenario("cache_validation_second", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok_with_validation.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=True) \
        .expect_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .run()
    assert ctx2 is not None


def test_review_exchange_cache_invalid_validation_reruns(scenario_repo: Path):
    ctx1 = scenario("cache_invalid_first", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .run()
    assert ctx1 is not None

    ctx2 = scenario("cache_invalid_second", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok_with_validation.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=True) \
        .expect_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .run()
    assert ctx2 is not None


def test_validation_failure_exhausts_retries(scenario_repo: Path):
    scenario("validation_exhausted", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=0) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_validation_status("failed") \
        .expect_validation_artifacts(False) \
        .expect_pending_validation_retries(0) \
        .expect_session_history_status({"validation_failed"}) \
        .expect_timeline_event(EventName.SESSION_VALIDATION_FAILED) \
        .run()


def test_validation_failure_updates_run_manifest(scenario_repo: Path):
    scenario("validation_manifest", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=0) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_validation_status("failed") \
        .expect_run_manifest(
            require_keys=["ended_at"],
            expected_fields={
                "outcome": "completed",
                "validation_passed": False,
                "validation_status": "failed",
            },
            session_name_prefix="coding-",
        ) \
        .run()


def test_validation_timeout_marks_failed(scenario_repo: Path):
    def _short_timeout(config) -> None:
        config.validation.quick.timeout_seconds = 1

    scenario("validation_timeout", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_timeout.sh"), max_retries=0) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .configure(_short_timeout) \
        .expect_validation_status("failed") \
        .expect_validation_artifacts(False, timed_out=True) \
        .run()


def test_review_exchange_auto_uses_local_loop_when_mcp_unsupported(scenario_repo: Path):
    def _configure_auto_mode(config) -> None:
        config.review_exchange_mode = "auto"
        config.agents["agent:coder"].ai_system = "unsupported"
        config.agents["agent:reviewer"].ai_system = "unsupported"

    scenario("auto_mode_local_loop", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .configure(_configure_auto_mode) \
        .expect_pr(created=True, draft=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .run()


def test_reconciliation_no_drift_allows_progress(scenario_repo: Path):
    matching_labels = {"simulated-scenario", "agent:coder"}
    scenario("reconciliation_ok", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .issue(labels=sorted(matching_labels)) \
        .reconciliation(enabled=True, fresh_labels={1: matching_labels}) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .expect_no_event(EventName.RECONCILIATION_REQUIRED) \
        .run()


def test_review_queue_approved_flow_updates_pr_labels(scenario_repo: Path):
    scenario("review_approved_flow", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_approved.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for(
            lambda orch: (
                orch.deps.repository_host.get_pr(100) is not None
                and "code-reviewed" in orch.deps.repository_host.get_pr(100).labels
            ),
            max_ticks=12,
        ) \
        .expect_pr(created=True, draft=True) \
        .expect_pr_label("code-reviewed") \
        .expect_pr_lacks_label("needs-code-review") \
        .expect_timeline_event(EventName.REVIEW_APPROVED) \
        .expect_latest_timeline_event(
            EventName.SESSION_COMPLETED,
            predicate=lambda e: e.task == "code",
        ) \
        .run()


def test_review_session_does_not_emit_coding_events(scenario_repo: Path):
    """Review approval emits review.approved, NOT session.completed with task=review."""
    ctx = scenario("review_no_coding_events", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_approved.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for(
            lambda orch: (
                orch.deps.repository_host.get_pr(100) is not None
                and "code-reviewed" in orch.deps.repository_host.get_pr(100).labels
            ),
            max_ticks=12,
        ) \
        .expect_timeline_event(EventName.ISSUE_PR_CREATED) \
        .expect_timeline_event(EventName.REVIEW_APPROVED) \
        .expect_latest_timeline_event(
            EventName.SESSION_COMPLETED,
            predicate=lambda e: e.task == "code",
        ) \
        .run()
    # Verify no SESSION_COMPLETED event has task="review"
    review_completed = [
        e for e in ctx.timeline_since_baseline()
        if e.event == EventName.SESSION_COMPLETED.value and e.task == "review"
    ]
    assert not review_completed, "Review session should not emit SESSION_COMPLETED"


def test_review_changes_requested_queues_rework(scenario_repo: Path):
    scenario("review_changes_requested", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_changes_requested.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for(
            lambda orch: (
                orch.deps.repository_host.get_pr(100) is not None
                and "needs-rework" in orch.deps.repository_host.get_pr(100).labels
            ),
            max_ticks=12,
        ) \
        .expect_pr(created=True, draft=True) \
        .expect_pr_label("needs-rework") \
        .expect_review_feedback_written() \
        .run()


def test_review_rework_then_approved(scenario_repo: Path):
    scenario("review_rework_then_approved", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_changes_then_approve.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for(
            lambda orch: (
                orch.deps.repository_host.get_pr(100) is not None
                and "code-reviewed" in orch.deps.repository_host.get_pr(100).labels
            ),
            max_ticks=16,
        ) \
        .expect_pr(created=True, draft=True) \
        .expect_pr_label("code-reviewed") \
        .expect_pr_lacks_label("needs-rework") \
        .expect_review_feedback_written() \
        .expect_timeline_event(EventName.REVIEW_CHANGES_REQUESTED) \
        .run()


def test_completion_outcome_blocked_sets_label_and_event(scenario_repo: Path):
    scenario("completion_blocked", scenario_repo) \
        .coder(script("coder_blocked.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_issue_label("blocked") \
        .expect_latest_event(
            EventName.ISSUE_BLOCKED,
            predicate=lambda data: data.get("issue_number") == 1,
        ) \
        .expect_latest_timeline_event(
            EventName.ISSUE_BLOCKED,
            predicate=lambda event: event.issue_number == 1,
        ) \
        .run()


def test_completion_outcome_needs_human_sets_label_and_event(scenario_repo: Path):
    scenario("completion_needs_human", scenario_repo) \
        .coder(script("coder_needs_human.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_issue_label("needs-human") \
        .expect_latest_event(
            EventName.ISSUE_NEEDS_HUMAN,
            predicate=lambda data: data.get("issue_number") == 1,
        ) \
        .expect_latest_timeline_event(
            EventName.ISSUE_NEEDS_HUMAN,
            predicate=lambda event: event.issue_number == 1,
        ) \
        .run()


def test_reconciliation_drift_pauses_issue(scenario_repo: Path):
    pause_label = "io:needs-reconcile"
    scenario("reconciliation_drift", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .issue(labels=["simulated-scenario", "agent:coder", "in-progress"]) \
        .reconciliation(enabled=True, fresh_labels={1: {pause_label}}) \
        .expect_issue_label(pause_label) \
        .expect_latest_event(
            EventName.RECONCILIATION_REQUIRED,
            predicate=lambda data: data.get("issue_number") == 1 and pause_label in set(data.get("actual_labels", [])),
        ) \
        .expect_latest_event(
            EventName.ISSUE_PAUSED_RECONCILE,
            predicate=lambda data: data.get("issue_number") == 1 and data.get("pause_label") == pause_label,
        ) \
        .run()


def test_sqlite_backups_created_for_existing_dbs(scenario_repo: Path):
    state_dir = scenario_repo / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_paths = [
        state_dir / "goal_pilot.sqlite",
        state_dir / "session_registry.sqlite",
    ]
    for db_path in db_paths:
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
            conn.commit()

    def _enable_sqlite_backups(config) -> None:
        config.sqlite_backup.enabled = True
        config.sqlite_backup.retention_daily = 1
        config.sqlite_backup.retention_weekly = 0
        config.sqlite_backup.cadence_hours = 0

    scenario("sqlite_backups", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .configure(_enable_sqlite_backups) \
        .run()

    backup_root = scenario_repo / ".issue-orchestrator" / "backups" / "sqlite"
    backups = list(backup_root.rglob("*.db"))
    assert backups, "Expected sqlite backup files to be created"


def test_restart_recovery_uses_labels_not_memory(scenario_repo: Path):
    ctx = scenario("restart_recovery", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .run()

    restarted = ctx.restart()

    for _ in range(3):
        restarted.orch.tick()

    issue = restarted.repo_host.get_issue(ctx.issue_number)
    assert issue is not None
    assert "pr-pending" in issue.labels
    assert restarted.orch.state.active_sessions == []
    assert restarted.orch.state.pending_reviews == []


@pytest.mark.simulated_review_outcome(
    reviewer_responses=[
        {"response_type": "changes_requested", "response_text": "stuck", "getting_closer": False},
    ],
    status="stopped",
    reason="reviewer_reports_no_progress",
)
def test_review_exchange_stops_on_no_progress(scenario_repo: Path):
    scenario("no_progress", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_no_progress.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_no_progress=1) \
        .expect_review_exchange_reason("reviewer_reports_no_progress") \
        .run()


@pytest.mark.simulated_review_outcome(
    reviewer_responses=[
        {"response_type": "changes_requested", "response_text": "n", "getting_closer": True},
    ],
)
def test_review_exchange_max_rounds_exceeded(scenario_repo: Path):
    scenario("max_rounds", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_never_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=2, max_no_progress=0) \
        .expect_review_exchange_reason("max_rounds_exceeded") \
        .expect_review_exchange_rounds(2) \
        .run()


@pytest.mark.simulated_review_outcome(
    # require_validation=True with no validation-record.json triggers
    # the runner's auto-flip from ok to changes_requested with
    # getting_closer=False; the no-progress streak then trips after
    # max_no_progress=1 with reason "reviewer_reports_no_progress".
    reviewer_responses=[
        {"response_type": "ok", "response_text": "looks good", "getting_closer": True},
    ],
    status="stopped",
    reason="reviewer_reports_no_progress",
)
def test_review_exchange_requires_validation_blocks_ok(scenario_repo: Path):
    scenario("require_validation_blocks_ok", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=True, max_no_progress=1) \
        .expect_review_exchange_reason("reviewer_reports_no_progress") \
        .run()


@pytest.mark.simulated_review_outcome(
    # Negative seeded-record: the validation record exists but has
    # ``passed: false``. The runner's ``_validation_passed`` parses the
    # JSON and only accepts ``true``; this scenario pins that the
    # require_validation guard flips reviewer-ok even when the record
    # is present-but-failed (file existence alone is not enough).
    reviewer_responses=[
        {"response_type": "ok", "response_text": "looks good", "getting_closer": True},
    ],
    write_validation_record_failed=True,
    status="stopped",
    reason="reviewer_reports_no_progress",
)
def test_review_exchange_requires_validation_blocks_ok_with_failed_record(scenario_repo: Path):
    scenario("require_validation_blocks_ok_failed_record", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=True, max_no_progress=1) \
        .expect_review_exchange_reason("reviewer_reports_no_progress") \
        .run()


@pytest.mark.simulated_review_outcome(
    reviewer_responses=[
        {"response_type": "ok", "response_text": "ok", "getting_closer": True},
    ],
    write_validation_record_passed=True,
)
def test_review_exchange_requires_validation_allows_ok(scenario_repo: Path):
    """When the validation record is present alongside the reviewer-ok
    response, the runner accepts the ok and finishes with reason
    reviewer_ok. The scenario harness writes a passing
    validation-record.json before the exchange enters via the
    validation step, so the stub here just emits a plain ok."""
    scenario("require_validation_allows_ok", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok_with_validation.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=True) \
        .expect_review_exchange_reason("reviewer_ok") \
        .run()


@pytest.mark.simulated_review_outcome(
    # Reviewer dies / produces invalid output: the runner's
    # ``_build_outcome_for_role_timeout`` path emits status="error",
    # reason="reviewer_no_completion". The round event still records
    # the synthesized "error"-typed reviewer response.
    reviewer_responses=[
        {"response_type": "error", "response_text": "invalid_json", "getting_closer": False},
    ],
    status="error",
    reason="reviewer_no_completion",
)
def test_reviewer_invalid_json_emits_error(scenario_repo: Path):
    scenario("reviewer_invalid_json", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_invalid_json.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=1) \
        .expect_review_exchange_round_response(reviewer_response_type="error") \
        .expect_review_exchange_terminal_state(status="error", reason="reviewer_no_completion") \
        .run()


@pytest.mark.simulated_review_outcome(
    reviewer_responses=[
        {"response_type": "error", "response_text": "exit_nonzero", "getting_closer": False},
    ],
    status="error",
    reason="reviewer_no_completion",
)
def test_reviewer_exit_nonzero_emits_error(scenario_repo: Path):
    scenario("reviewer_exit_nonzero", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_exit_nonzero.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=1) \
        .expect_review_exchange_round_response(reviewer_response_type="error") \
        .expect_review_exchange_terminal_state(status="error", reason="reviewer_no_completion") \
        .run()


@pytest.mark.simulated_review_outcome(
    # Coder protocol guardrail exhausts retries: runner's
    # ``_build_outcome_for_coder_protocol_error`` emits status="error",
    # reason="coder_protocol_error". The round event records the
    # protocol_error coder response.
    reviewer_responses=[
        {"response_type": "changes_requested", "response_text": "n", "getting_closer": True},
    ],
    coder_response_type="protocol_error",
    status="error",
    reason="coder_protocol_error",
)
def test_coder_invalid_json_emits_error(scenario_repo: Path):
    scenario("coder_invalid_json", scenario_repo) \
        .coder(script("coder_invalid_json.sh")) \
        .reviewer(script("reviewer_never_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=1) \
        .expect_review_exchange_round_response(coder_response_type="protocol_error") \
        .expect_review_exchange_terminal_state(status="error", reason="coder_protocol_error") \
        .run()


@pytest.mark.simulated_review_outcome(
    reviewer_responses=[
        {"response_type": "changes_requested", "response_text": "n", "getting_closer": True},
    ],
    coder_response_type="protocol_error",
    status="error",
    reason="coder_protocol_error",
)
def test_coder_exit_nonzero_emits_error(scenario_repo: Path):
    scenario("coder_exit_nonzero", scenario_repo) \
        .coder(script("coder_exit_nonzero.sh")) \
        .reviewer(script("reviewer_never_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=1) \
        .expect_review_exchange_round_response(coder_response_type="protocol_error") \
        .expect_review_exchange_terminal_state(status="error", reason="coder_protocol_error") \
        .run()


def test_review_session_no_completion_marks_interrupted_retry(scenario_repo: Path):
    def _disable_grace_period(config) -> None:
        config.session_grace_period_seconds = 0
        config.session_log_activity_seconds = 0

    scenario("review_no_completion", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_no_completion.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .configure(_disable_grace_period) \
        .wait_for_event(EventName.SESSION_FAILED) \
        .expect_issue_comment_contains("Review Session Interrupted") \
        .run()


@_skip_if_no_agent_cli
def test_agent_sessions_emit_core_run_artifacts(scenario_repo: Path):
    """Agent-backed coding/review scenarios must produce debuggable run artifacts."""
    ctx = scenario("agent_run_artifacts", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_approved.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for_event(EventName.REVIEW_APPROVED) \
        .run()

    run_dirs = sorted({
        Path(str(event.run_dir))
        for event in ctx.timeline_since_baseline()
        if event.run_dir
    })
    assert run_dirs, "expected timeline events with run_dir for agent sessions"
    for run_dir in run_dirs:
        _assert_run_dir_has_core_artifacts(run_dir)


@_skip_if_no_agent_cli
def test_review_changes_requested_writes_feedback_artifact(scenario_repo: Path):
    """Changes-requested review runs must persist reviewer-feedback.json in run_dir."""
    ctx = scenario("review_feedback_artifact", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_changes_requested.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for_event(EventName.REVIEW_CHANGES_REQUESTED) \
        .run()

    review_run_dirs = [
        Path(str(event.run_dir))
        for event in ctx.timeline_since_baseline()
        if event.event == EventName.REVIEW_STARTED.value and event.run_dir
    ]
    assert review_run_dirs, "expected at least one review.started event with run_dir"

    feedback_paths = [run_dir / "reviewer-feedback.json" for run_dir in review_run_dirs]
    existing_feedback = [path for path in feedback_paths if path.exists() and path.stat().st_size > 0]
    assert existing_feedback, (
        "reviewer feedback artifact missing/empty; "
        f"candidates={', '.join(str(path) for path in feedback_paths)}"
    )


def test_local_loop_run_artifacts_and_actions_are_run_scoped(scenario_repo: Path):
    """via-local-loop should emit run-scoped actions with usable artifacts."""
    ctx = scenario("local_loop_run_scoped_actions", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .wait_for_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .run()

    timeline_events = [event.to_dict() for event in ctx.timeline_reader.read(1).events]
    agent_run_events = {
        EventName.SESSION_STARTED.value,
        EventName.REVIEW_STARTED.value,
        EventName.REWORK_STARTED.value,
    }
    run_dirs = sorted({
        Path(str(event.get("run_dir")))
        for event in timeline_events
        if event.get("run_dir") and str(event.get("event") or "") in agent_run_events
    })
    assert run_dirs, "expected at least one run_dir in timeline events"

    for run_dir in run_dirs:
        _assert_run_dir_has_core_artifacts(run_dir)

    for event in timeline_events:
        event_name = str(event.get("event") or "")
        if event_name not in agent_run_events:
            continue
        actions = _timeline_event_actions(event, 1)
        action_types = {action["type"] for action in actions}
        # Post-cutover the canonical run-scoped review artifact is the
        # phase-scoped terminal recording (``open_agent_log`` with
        # round_index + session_role). The legacy ``open_review_transcript``
        # was tied to the spawn-per-phase ``transcript.log`` writer that
        # the cutover deleted; the persistent runner does not write a
        # text transcript.
        assert "open_agent_log" in action_types
        assert "open_session_diagnostics" in action_types


# ---------------------------------------------------------------------------
# Coverage gaps — dirty-tree + validation behavior
#
# Scenarios added during the post-cutover coverage sweep to pin runtime
# behavior that had only unit-level coverage (or none). Each test
# exercises the orchestrator's dirty-policy / validation paths through
# the simulated harness so a regression at the orchestration level
# surfaces here, not just in the unit suite.
# ---------------------------------------------------------------------------


def _enable_dirty_check(mode: str):
    def _configure(config) -> None:
        config.validation.publish.dirty_check = mode
    return _configure


def test_dirty_tree_tracked_blocks_publish(scenario_repo: Path):
    """Tracked-file changes left dirty by the agent must block publish.

    The orchestrator's completion-record validator checks
    ``has_tracked_changes`` against the working copy before running
    publish actions. When it reports dirty, ``check_dirty_policy``
    fails with ``DIRTY_POLICY`` and ``_handle_gate_failure`` records
    the failure on the run manifest. Without this scenario, a
    regression in the dirty-check call could silently allow a publish
    on a dirty worktree — only caught at the prepush hook level
    (which simulated_scenarios doesn't drive).
    """
    scenario("dirty_tree_tracked", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .configure(_enable_dirty_check("tracked")) \
        .use_working_copy(DirtyTrackedWorkingCopy()) \
        .expect_validation_status("failed") \
        .expect_session_history_status({"validation_failed"}) \
        .run()


def test_dirty_tree_all_mode_blocks_untracked(scenario_repo: Path):
    """``validation.publish.dirty_check: all`` blocks publish even when only
    untracked files are dirty. The check uses
    ``has_uncommitted_changes`` (which covers untracked) instead of
    the tracked-only path, so a regression that confused the two
    modes would surface here.
    """
    scenario("dirty_tree_all_untracked", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .configure(_enable_dirty_check("all")) \
        .use_working_copy(DirtyUntrackedWorkingCopy()) \
        .expect_validation_status("failed") \
        .expect_session_history_status({"validation_failed"}) \
        .run()


def test_validation_failed_event_carries_run_context(scenario_repo: Path):
    """``SESSION_VALIDATION_FAILED`` must carry the run-scoped fields the
    UI/timeline relies on. Without these, the timeline can't render a
    failure row that links back to the right diagnostic artifacts
    (``open_session_diagnostics`` resolves the run from
    ``run_dir`` + ``session_name``; ``artifacts`` exposes the
    validation-record path).
    """
    scenario("validation_failed_event_payload", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=0) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_latest_event(
            EventName.SESSION_VALIDATION_FAILED,
            predicate=lambda data: bool(
                data.get("run_dir")
                and data.get("session_name")
                and data.get("issue_number")
                and "validation_cmd" in data
                and "artifacts" in data
            ),
        ) \
        .run()


def test_validation_failure_surfaces_as_timeline_failure_row(scenario_repo: Path):
    """A failed validation must surface as a failure-status timeline row.

    The timeline projection maps ``SESSION_VALIDATION_FAILED`` to a
    ``failed`` status (not ``completed``). This test pins that mapping
    by reading the timeline events and verifying the row's status
    field is ``failed``. Without it, a regression in
    ``timeline._status_for_event`` could hide validation failures
    from the dashboard's failure-row treatment.
    """
    ctx = scenario("validation_timeline_failure_row", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=0) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_timeline_event(EventName.SESSION_VALIDATION_FAILED) \
        .run()
    timeline_events = list(ctx.timeline_since_baseline())
    # Timeline rows can carry the upstream event name in either
    # ``event`` or ``source_event`` depending on whether the row was
    # collapsed/derived; check both, matching ``expect_timeline_event``.
    failed_rows = [
        evt for evt in timeline_events
        if (evt.source_event or evt.event) == EventName.SESSION_VALIDATION_FAILED.value
    ]
    assert failed_rows, "expected SESSION_VALIDATION_FAILED in timeline"
    for evt in failed_rows:
        assert evt.status == "failed", (
            f"expected failed status, got {evt.status!r} for SESSION_VALIDATION_FAILED"
        )


def test_validation_cache_invalidates_on_record_corruption(scenario_repo: Path):
    """A corrupt ``validation/<sha>.json`` must trigger a fresh
    validation run, not a silent cache hit. ``ValidationRecordStore.read``
    returns ``None`` on ``JSONDecodeError``/``KeyError``/``TypeError``,
    which is the contract that lets the publish gate distinguish
    "no record yet" from "record but unreadable" — both miss, so
    validation re-runs.
    """
    # Stub head SHA is "deadbeef" (StubWorkingCopy.get_head_sha).
    # Seed the corrupt cache before the orchestrator boots; the worktree
    # path is the conventional ``<repo>/.issue-orchestrator/worktrees/sim-wt-1``.
    validation_dir = (
        scenario_repo
        / ".issue-orchestrator" / "worktrees" / "sim-wt-1"
        / ".issue-orchestrator" / "validation"
    )
    validation_dir.mkdir(parents=True, exist_ok=True)
    (validation_dir / "deadbeef.json").write_text(
        "{ this is not valid JSON",
        encoding="utf-8",
    )

    scenario("validation_cache_corrupted", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_validation_status("passed") \
        .expect_validation_artifacts(True) \
        .run()


def test_dirty_enumeration_failure_fails_closed(scenario_repo: Path):
    """When ``list_dirty_files`` reports it can't enumerate
    (returns ``None`` on a git error), the dirty-policy guard must
    fail closed — not collapse to "no blocking files, pass" and let
    a possibly-dirty worktree through. Regression for the bug fixed
    in PR #6159 at the prepush hook level; this scenario pins the
    same fail-closed behavior at the completion-record validator
    level used inside the orchestration pipeline.
    """
    scenario("dirty_enum_fails_closed", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .configure(_enable_dirty_check("tracked")) \
        .use_working_copy(DirtyEnumerationFailsWorkingCopy()) \
        .expect_validation_status("failed") \
        .expect_session_history_status({"validation_failed"}) \
        .run()


def test_validation_diagnostics_persisted_on_failure(scenario_repo: Path):
    """A failed validation run must leave diagnostic artifacts that the
    UI's ``open_session_diagnostics`` action can resolve: a
    ``validation/<sha>.json`` record with stdout_path / stderr_path
    fields, and the underlying log files. Without these, a failed-
    validation timeline row links to nothing and the operator can't
    see what broke.
    """
    ctx = scenario("validation_diag_persisted", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=0) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_validation_status("failed") \
        .run()

    # Find the worktree the orchestrator used so we can inspect its
    # ``.issue-orchestrator/validation/`` directory.
    worktree_root = scenario_repo / ".issue-orchestrator" / "worktrees"
    sim_worktrees = [p for p in worktree_root.iterdir() if p.is_dir()] if worktree_root.exists() else []
    assert sim_worktrees, "expected a simulated worktree to exist"
    validation_dir = sim_worktrees[0] / ".issue-orchestrator" / "validation"
    record_files = list(validation_dir.glob("*.json")) if validation_dir.exists() else []
    assert record_files, (
        f"expected validation/<sha>.json record under {validation_dir}; "
        "the failed-validation diagnostics action depends on it."
    )
    record = json.loads(record_files[0].read_text())
    assert record.get("passed") is False
    stdout_rel = record.get("stdout_path")
    stderr_rel = record.get("stderr_path")
    assert stdout_rel and stderr_rel, (
        "validation record missing stdout_path/stderr_path; "
        "diagnostic action cannot link to the actual log files"
    )
    stdout_abs = sim_worktrees[0] / stdout_rel
    stderr_abs = sim_worktrees[0] / stderr_rel
    assert stdout_abs.exists(), f"stdout log missing at {stdout_abs}"
    assert stderr_abs.exists(), f"stderr log missing at {stderr_abs}"
    # Sanity: events emitted with the right context were captured.
    assert any(
        evt.name == EventName.SESSION_VALIDATION_FAILED
        for evt in ctx.events_since_baseline()
    ), "expected SESSION_VALIDATION_FAILED to be emitted"
