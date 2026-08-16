from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.actions import (
    AddLabelAction,
    RemoveLabelAction,
    SupersedePullRequestAction,
)
from issue_orchestrator.control.completion_types import ProcessingResult
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
)
from issue_orchestrator.control.publish_recovery import PublishRecoveryService
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.execution.json_publish_retry_locator_store import (
    JsonPublishRetryLocatorStore,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.background_job import CompletedJob
from issue_orchestrator.ports.fresh_issue_reader import FreshIssueReadError
from issue_orchestrator.ports.pull_request_tracker import PRInfo

BRANCH = "4057-scratch-1"
PR_URL = "https://github.com/owner/repo/pull/5453"


def _config(tmp_path: Path) -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.repo_root = tmp_path / "repo"
    return config


@dataclass
class _Repo:
    issue: Issue
    labels: list[str]
    prs: list[PRInfo] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    superseded: list[int] = field(default_factory=list)

    def get_issue(self, issue_number: int) -> Issue | None:
        return self.issue if self.issue.number == issue_number else None

    def read_issue_labels(self, issue_number: int) -> list[str]:
        if self.issue.number != issue_number:
            return []
        return list(self.labels)

    def add_label(self, issue_number: int, label: str) -> None:
        if self.issue.number != issue_number:
            return
        if label not in self.labels:
            self.labels.append(label)
        self.added.append(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        if self.issue.number != issue_number:
            return
        self.labels = [current for current in self.labels if current != label]
        self.removed.append(label)

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        if self.issue.number != issue_number:
            return []
        if state == "all":
            return list(self.prs)
        return [pr for pr in self.prs if pr.state == state]


@dataclass
class _CompletionProcessor:
    result: ProcessingResult
    calls: list[dict] = field(default_factory=list)

    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        *,
        run_assets: SessionRunAssets,
        pr_number: int | None = None,
        completion_path: str | None = None,
        agent_label: str | None = None,
    ) -> ProcessingResult:
        record_path = worktree / (completion_path or "completion.json")
        self.calls.append({
            "worktree": worktree,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "completion_path": completion_path,
            "agent_label": agent_label,
            # Captured at call time so tests can prove the completion record was
            # available to the processor when the republish actually ran.
            "completion_present": record_path.exists(),
        })
        return self.result


class _RecordingRunner:
    """A BackgroundJobRunner that captures submitted work and runs it on demand.

    Keeps the republish off the request thread (``retry_publish`` only submits;
    the work executes when the test calls ``run_all``), then reports completions
    through ``drain_completed`` exactly like the production thread runner.
    """

    def __init__(self) -> None:
        self._pending: dict[str, Callable[[], None]] = {}
        self._running: set[str] = set()
        self._done: list[CompletedJob] = []

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        if job_id in self._running:
            return False
        self._running.add(job_id)
        self._pending[job_id] = fn
        return True

    def is_running(self, job_id: str) -> bool:
        return job_id in self._running

    def running_ids(self) -> set[str]:
        return set(self._running)

    def run_all(self) -> None:
        for job_id, fn in list(self._pending.items()):
            error: BaseException | None = None
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 — mirror thread runner
                error = exc
            self._done.append(CompletedJob(job_id=job_id, error=error))
            self._pending.pop(job_id, None)
            self._running.discard(job_id)

    def drain_completed(self) -> list[CompletedJob]:
        done = self._done
        self._done = []
        return done


class _DrainDuringSubmitRunner:
    """Runs the job inside ``submit`` and lets a hook fire before it returns.

    This deterministically reproduces the fast-completion race: the worker
    finishes and the tick drains it *before* ``submit()`` returns to the owner.
    A correct owner records its in-flight context before the worker can start,
    so the mid-submit drain still reconciles the result instead of dropping it.
    """

    def __init__(self) -> None:
        self._done: list[CompletedJob] = []
        self.on_submit: Callable[[], None] | None = None

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        error: BaseException | None = None
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 — mirror thread runner
            error = exc
        self._done.append(CompletedJob(job_id=job_id, error=error))
        if self.on_submit is not None:
            self.on_submit()
        return True

    def is_running(self, job_id: str) -> bool:
        return False

    def drain_completed(self) -> list[CompletedJob]:
        done = self._done
        self._done = []
        return done


@dataclass
class _ActionApplier:
    repo: _Repo
    # When set, applying a SupersedePullRequestAction raises this instead of
    # returning an outcome — mirrors ActionApplier re-raising claim/state races.
    raise_on_supersede: BaseException | None = None
    # When set, applying any label action targeting this label returns a failure
    # outcome (which _apply_label_actions turns into a RuntimeError) — models a
    # GitHub label mutation failure during finalize cleanup.
    fail_on_label: str | None = None

    def apply(
        self,
        action: AddLabelAction | RemoveLabelAction | SupersedePullRequestAction,
    ) -> SimpleNamespace:
        if isinstance(action, (AddLabelAction, RemoveLabelAction)) and action.label == self.fail_on_label:
            return SimpleNamespace(success=False, error=f"label mutation failed: {action.label}")
        if isinstance(action, AddLabelAction):
            self.repo.add_label(action.issue_number, action.label)
        elif isinstance(action, SupersedePullRequestAction):
            if self.raise_on_supersede is not None:
                raise self.raise_on_supersede
            self.repo.superseded.append(action.pr_number)
            self.repo.prs = [
                pr for pr in self.repo.prs if pr.number != action.pr_number
            ]
        else:
            self.repo.remove_label(action.issue_number, action.label)
        return SimpleNamespace(success=True, error=None)


def _service(
    tmp_path: Path,
    repo: _Repo,
    lm: LabelManager,
    *,
    result: ProcessingResult | None = None,
    runner: Any = None,
    action_applier: _ActionApplier | None = None,
    code_review_agent_configured: bool = False,
    tech_lead_authority: InMemoryTechLeadAuthorityStore | None = None,
) -> tuple[PublishRecoveryService, JsonPublishRetryLocatorStore, Any]:
    store = JsonPublishRetryLocatorStore(tmp_path / "publish_retry_locators.json")
    runner = runner or _RecordingRunner()
    processor = _CompletionProcessor(
        result or ProcessingResult(success=True, message="ok", pr_url=PR_URL)
    )
    service = PublishRecoveryService(
        repository_host=repo,
        completion_processor=processor,
        locator_store=store,
        runner=runner,
        label_manager=lm,
        fresh_issue_reader=repo,
        action_applier=action_applier or _ActionApplier(repo),
        code_review_agent_configured=code_review_agent_configured,
        tech_lead_authority=tech_lead_authority or InMemoryTechLeadAuthorityStore(),
    )
    return service, store, runner


def _issue(lm: LabelManager, *, publish_failed: bool = True) -> Issue:
    labels = ["agent:web"]
    if publish_failed:
        labels += [lm.publish_failed, lm.publish_fail_count_label(2)]
    return Issue(number=4057, title="UI: Surface provider status", labels=labels)


def _record_failure(
    service: PublishRecoveryService,
    make_session,
    tmp_path: Path,
    *,
    agent_config: Any = None,
    review_exchange_completed: bool = False,
    review_exchange_halted: bool = False,
) -> Session:
    """Persist retry locators the way the live completion path does."""
    session = make_session(
        issue_number=4057,
        issue_title="UI: Surface provider status",
        branch_name=BRANCH,
        agent_config=agent_config,
    )
    completion_file = session.worktree_path / session.completion_path
    completion_file.parent.mkdir(parents=True, exist_ok=True)
    completion_file.write_text("{}")
    service.record_publish_failure(
        session,
        ["push_branch: Push failed: remote rejected"],
        review_exchange_completed=review_exchange_completed,
        review_exchange_halted=review_exchange_halted,
    )
    return session


# ---------------------------------------------------------------------------
# Command surface A: publish-fail -> persisted retry locators
# ---------------------------------------------------------------------------


def test_record_publish_failure_persists_retry_locators(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, _ = _service(tmp_path, repo, lm)

    session = _record_failure(service, make_session, tmp_path)

    locators = store.get(4057)
    assert locators is not None
    assert locators.branch_name == BRANCH
    assert locators.worktree_path == str(session.worktree_path)
    assert locators.completion_path == session.completion_path


def test_publish_blocked_failure_persists_retry_locators(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, _ = _service(tmp_path, repo, lm)

    session = make_session(
        issue_number=4057,
        issue_title="UI: Surface provider status",
        branch_name=BRANCH,
    )
    service.record_publish_failure(
        session,
        ["publish_blocked: Could not determine current branch"],
    )

    locators = store.get(4057)
    assert locators is not None
    assert locators.branch_name == BRANCH
    assert locators.worktree_path == str(session.worktree_path)


def test_record_publish_failure_ignores_non_publish_errors(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, _ = _service(tmp_path, repo, lm)

    session = make_session(issue_number=4057, branch_name=BRANCH)
    service.record_publish_failure(session, ["validation_failed: tests failed"])

    assert store.get(4057) is None


# ---------------------------------------------------------------------------
# Command surface B: retry request -> runner submission + drain reconciliation
# ---------------------------------------------------------------------------


def test_retry_publish_submits_off_thread_without_running_inline(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, _, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    result = service.retry_publish(4057, state)

    assert result.status == "submitted"
    # Submission-scoped job id: republish:<issue>:<token>.
    assert result.job_id is not None
    assert result.job_id.startswith("republish:4057:")
    # The heavy publish work runs on the runner, not on the request thread.
    assert runner.is_running(result.job_id)


def test_retry_publish_reconciles_success_on_drain(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    assert lm.publish_failed in repo.removed
    assert lm.publish_fail_count_label(2) in repo.removed
    assert lm.pr_pending in repo.added
    assert store.get(4057) is None  # locators cleared on success
    assert state.session_history[-1].status == "completed"
    assert state.session_history[-1].pr_url == PR_URL


def test_retry_publish_success_queues_code_review_when_configured(
    make_session, tmp_path
) -> None:
    """A retry that produces a real PR must not bypass the configured review gate.

    With a code review agent configured and no completed local review exchange,
    reconciliation routes the PR through the same review-discovery fact the live
    completion path uses (planner then owns pr-pending + the review queue),
    instead of silently finalizing straight to awaiting-merge.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(
        tmp_path, repo, lm, code_review_agent_configured=True
    )
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    # The review-discovery fact was produced, carrying the real PR + branch.
    assert len(state.discovered_reviews) == 1
    review = state.discovered_reviews[0]
    assert review.issue_number == 4057
    assert review.pr_number == 5453
    assert review.pr_url == PR_URL
    assert review.branch_name == BRANCH
    # Publish-failed state is still cleared and the issue is no longer retryable.
    assert lm.publish_failed in repo.removed
    assert store.get(4057) is None
    # pr-pending is now owned by the planner (via the discovered review), not
    # finalized here — the retry path must not double-own that policy.
    assert lm.pr_pending not in repo.added


def test_retry_publish_success_skips_review_when_exchange_completed(
    make_session, tmp_path
) -> None:
    """An approved local review exchange must not enqueue another review.

    ``review_exchange_completed=true`` means the PR was already reviewed to
    approval in-band, so reconciliation finalizes to awaiting-merge directly
    without producing a review-discovery fact.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(
        tmp_path,
        repo,
        lm,
        result=ProcessingResult(
            success=True, message="ok", pr_url=PR_URL, review_exchange_completed=True
        ),
        code_review_agent_configured=True,
    )
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    assert state.discovered_reviews == []
    assert lm.pr_pending in repo.added
    assert lm.publish_failed in repo.removed
    assert store.get(4057) is None


def test_retry_publish_success_skips_review_when_agent_opted_out(
    make_session, sample_agent_config, tmp_path
) -> None:
    """A ``skip_review`` coding agent's PR must not be queued for review.

    The intent is persisted in the locators at failure time and honored by the
    same policy the live path uses, so retry does not re-introduce review for an
    agent that explicitly opted out.
    """
    import dataclasses

    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(
        tmp_path, repo, lm, code_review_agent_configured=True
    )
    _record_failure(
        service,
        make_session,
        tmp_path,
        agent_config=dataclasses.replace(sample_agent_config, skip_review=True),
    )
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    assert state.discovered_reviews == []
    assert lm.pr_pending in repo.added
    assert store.get(4057) is None


def test_retry_publish_failure_leaves_issue_retryable(make_session, tmp_path) -> None:
    """A failed republish must not permanently lock out a second retry."""
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(
        tmp_path, repo, lm,
        result=ProcessingResult(success=False, message="push failed again"),
    )
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    # While pending, a second retry is rejected by the owner in-flight guard.
    assert service.retry_publish(4057, state).status == "rejected"

    runner.run_all()
    service.drain_completed_retries(state)

    # publish-failed + locators survive a failed republish, so retry is available again.
    assert store.get(4057) is not None
    assert not runner.running_ids()
    assert service.retry_publish(4057, state).status == "submitted"


# ---------------------------------------------------------------------------
# Recovery of an already-created PR
# ---------------------------------------------------------------------------


def test_retry_publish_recovers_existing_pr_and_clears_state(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    issue = _issue(lm)
    repo = _Repo(
        issue=issue,
        labels=list(issue.labels),
        prs=[
            PRInfo(
                number=5453,
                title="#4057: UI: Surface provider status",
                url=PR_URL,
                branch=BRANCH,
                body="",
                state="open",
                labels=[],
            )
        ],
    )
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState(
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title=issue.title,
                agent_type="agent:web",
                status="failed",
                runtime_minutes=5,
                status_reason="Push or PR creation failed",
                completed_at=datetime.now(),
            )
        ]
    )

    result = service.retry_publish(4057, state)

    assert result.status == "recovered_existing_pr"
    assert not runner.running_ids()  # no republish submitted
    assert store.get(4057) is None
    assert lm.publish_failed in repo.removed
    assert lm.pr_pending in repo.added
    assert not any(
        entry.issue_number == 4057 and entry.status in {"blocked", "failed"}
        for entry in state.session_history
    )
    assert state.session_history[-1].status == "completed"
    assert state.session_history[-1].pr_url == PR_URL


def test_recover_existing_pr_honors_original_review_exchange_completed(
    make_session, tmp_path
) -> None:
    """An existing-PR recovery must not requeue a PR whose review already completed.

    When the original completion finished a local review exchange and then
    publish failed leaving an open PR, recovery honors that persisted state and
    finalizes directly to pr-pending instead of contradicting the shared policy.
    """
    lm = LabelManager(_config(tmp_path))
    issue = _issue(lm)
    repo = _Repo(
        issue=issue,
        labels=list(issue.labels),
        prs=[
            PRInfo(
                number=5453,
                title="#4057: UI: Surface provider status",
                url=PR_URL,
                branch=BRANCH,
                body="",
                state="open",
                labels=[],
            )
        ],
    )
    service, store, _ = _service(
        tmp_path, repo, lm, code_review_agent_configured=True
    )
    _record_failure(service, make_session, tmp_path, review_exchange_completed=True)
    state = OrchestratorState()

    result = service.retry_publish(4057, state)

    assert result.status == "recovered_existing_pr"
    # Already reviewed to approval in-band: no new review discovery, and the PR
    # finalizes straight to awaiting-merge.
    assert state.discovered_reviews == []
    assert lm.pr_pending in repo.added
    assert store.get(4057) is None


def test_finalize_failure_leaves_no_partial_review_or_history(
    make_session, tmp_path
) -> None:
    """A cleanup-label failure must not leave split-brain review/history state.

    If the external publish-failed label removal fails mid-finalize, the retry
    must not have already queued review-discovery, recorded completed history,
    or cleared its locators — the issue stays fully retryable.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    applier = _ActionApplier(repo, fail_on_label=lm.publish_failed)
    service, store, runner = _service(
        tmp_path, repo, lm, action_applier=applier, code_review_agent_configured=True
    )
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    # The label cleanup raises during drain; the tick surfaces it rather than
    # silently finalizing.
    with pytest.raises(RuntimeError):
        service.drain_completed_retries(state)

    assert state.discovered_reviews == []
    assert not any(entry.issue_number == 4057 for entry in state.session_history)
    assert 4057 not in state.completed_today
    # Locators survive: the issue is still retryable, not half-finalized.
    assert store.get(4057) is not None


def test_retry_publish_rejected_without_locators(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, _, _ = _service(tmp_path, repo, lm)

    result = service.retry_publish(4057, OrchestratorState())

    assert result.status == "rejected"
    assert "locators" in result.message.lower()


# ---------------------------------------------------------------------------
# Termination: abandon in-flight retry on reset (F1/A1 regression)
# ---------------------------------------------------------------------------


def test_abandon_issue_supersedes_late_retry_and_skips_reconcile(
    make_session, tmp_path
) -> None:
    """Reset while a republish is in flight must not repopulate the attempt.

    Start a retry, abandon the issue (as reset does), then drain a *successful*
    late retry result. It must supersede the PR the late worker created and
    must NOT add pr-pending, remove publish-failed, append completed history,
    or leave an unsuperseded stale PR.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"

    # Operator resets the issue before the republish thread drains.
    service.abandon_issue(4057)
    assert store.get(4057) is None  # durable locators dropped by abandon

    # The abandoned worker still finishes and lands a PR past reset's scan.
    repo.prs = [
        PRInfo(
            number=5453,
            title="#4057: UI: Surface provider status",
            url=PR_URL,
            branch=BRANCH,
            body="",
            state="open",
            labels=[],
        )
    ]
    runner.run_all()
    service.drain_completed_retries(state)

    # Late PR is superseded, not adopted as the issue's live attempt.
    assert repo.superseded == [5453]
    assert lm.pr_pending not in repo.added
    assert lm.publish_failed not in repo.removed
    assert not any(entry.issue_number == 4057 for entry in state.session_history)
    assert 4057 not in state.completed_today
    assert store.get(4057) is None


def test_abandon_issue_without_inflight_does_not_block_a_later_retry(
    make_session, tmp_path
) -> None:
    """Abandon with no in-flight work only clears locators, leaves no tombstone.

    A stale tombstone would silently drop the next legitimate retry, so prove a
    fresh retry after abandon still reconciles normally.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    service.abandon_issue(4057)
    assert store.get(4057) is None

    # A fresh attempt fails publish again and is retried.
    _record_failure(service, make_session, tmp_path)
    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    # Reconciled as a normal success — the earlier abandon left no tombstone.
    assert lm.publish_failed in repo.removed
    assert lm.pr_pending in repo.added
    assert store.get(4057) is None
    assert state.session_history[-1].status == "completed"
    assert repo.superseded == []


# ---------------------------------------------------------------------------
# Live failure-to-retry shape: durable record survives processor cleanup (F1/A1)
# ---------------------------------------------------------------------------


def _service_with_processor(tmp_path, repo, lm):
    """Build a service and hand back the completion processor for inspection."""
    store = JsonPublishRetryLocatorStore(tmp_path / "publish_retry_locators.json")
    runner = _RecordingRunner()
    processor = _CompletionProcessor(
        ProcessingResult(success=True, message="ok", pr_url=PR_URL)
    )
    service = PublishRecoveryService(
        repository_host=repo,
        completion_processor=processor,
        locator_store=store,
        runner=runner,
        label_manager=lm,
        fresh_issue_reader=repo,
        action_applier=_ActionApplier(repo),
        code_review_agent_configured=False,
        tech_lead_authority=InMemoryTechLeadAuthorityStore(),
    )
    return service, store, runner, processor


def test_retry_after_live_cleanup_restores_durable_completion_record(
    make_session, tmp_path
) -> None:
    """The live path deletes the agent completion file after preserving a copy.

    Recording locators from that production shape (original gone, run-scoped
    durable copy present) must still let retry_publish submit, and the worker
    must restore the durable record so the processor reads a real input.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, _, runner, processor = _service_with_processor(tmp_path, repo, lm)

    session = make_session(
        issue_number=4057,
        issue_title="UI: Surface provider status",
        branch_name=BRANCH,
    )
    # Production shape: CompletionProcessor preserved a run-scoped copy, then
    # unlinked the agent's original completion file.
    durable = session.run_assets.completion_record_copy.path
    durable.parent.mkdir(parents=True, exist_ok=True)
    durable.write_text('{"outcome": "completed"}')
    original = session.worktree_path / session.completion_path
    assert not original.exists()

    service.record_publish_failure(session, ["push_branch: Push failed: remote rejected"])
    state = OrchestratorState()

    # Not rejected for a missing completion record — the durable copy counts.
    assert service.retry_publish(4057, state).status == "submitted"

    runner.run_all()

    # The worker restored the durable record so the processor had a valid input.
    assert original.exists()
    assert processor.calls[-1]["completion_present"] is True


def test_retry_rejected_when_no_completion_record_anywhere(make_session, tmp_path) -> None:
    """With neither the original nor the durable copy, retry is honestly rejected."""
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, _, _ = _service(tmp_path, repo, lm)

    session = make_session(
        issue_number=4057,
        issue_title="UI: Surface provider status",
        branch_name=BRANCH,
    )
    # No completion record written at all (original absent, durable absent).
    service.record_publish_failure(session, ["push_branch: Push failed: remote rejected"])

    result = service.retry_publish(4057, OrchestratorState())

    assert result.status == "rejected"
    assert "completion record" in result.message.lower()


# ---------------------------------------------------------------------------
# Abandoned-retry supersede survives claim/reconciliation races (F2)
# ---------------------------------------------------------------------------


def _drain_late_abandoned_retry(service, runner, state, repo) -> None:
    """Submit a retry, abandon it, land a late PR, then drain the tombstoned job."""
    assert service.retry_publish(4057, state).status == "submitted"
    service.abandon_issue(4057)
    repo.prs = [
        PRInfo(
            number=5453,
            title="#4057: UI: Surface provider status",
            url=PR_URL,
            branch=BRANCH,
            body="",
            state="open",
            labels=[],
        )
    ]
    runner.run_all()
    service.drain_completed_retries(state)


def test_abandoned_retry_supersede_survives_claim_lost(make_session, tmp_path) -> None:
    """A ClaimLostError from the supersede must not abort the drain/tick."""
    from issue_orchestrator.control.claim_gate import ClaimLostError

    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    applier = _ActionApplier(
        repo, raise_on_supersede=ClaimLostError(4057, "supersede pr")
    )
    service, _, runner = _service(tmp_path, repo, lm, action_applier=applier)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    # Must not raise — the stranded PR is left open, tick survives.
    _drain_late_abandoned_retry(service, runner, state, repo)

    assert repo.superseded == []
    assert any(pr.number == 5453 for pr in repo.prs)


def test_abandoned_retry_supersede_survives_reconciliation_required(
    make_session, tmp_path
) -> None:
    """A ReconciliationRequired from the supersede must not abort the drain/tick."""
    from issue_orchestrator.control.reconciliation import (
        ExternalSnapshot,
        ReconciliationRequired,
    )

    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    applier = _ActionApplier(
        repo,
        raise_on_supersede=ReconciliationRequired(
            entity_type="pr",
            entity_id=5453,
            expected=ExternalSnapshot.for_issue(5453, set()),
            actual=ExternalSnapshot.for_issue(5453, {"drift"}),
        ),
    )
    service, _, runner = _service(tmp_path, repo, lm, action_applier=applier)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    _drain_late_abandoned_retry(service, runner, state, repo)

    assert repo.superseded == []
    assert any(pr.number == 5453 for pr in repo.prs)


# ---------------------------------------------------------------------------
# Owner in-flight state independent of the worker's alive bit (F1)
# ---------------------------------------------------------------------------


def test_retry_completion_drained_during_submit_is_reconciled(make_session, tmp_path) -> None:
    """A completion that drains before submit() returns must not be lost.

    The owner records its in-flight context before the worker can start, so a
    mid-submit drain reconciles the result instead of dropping it as an
    unknown job_id.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    runner = _DrainDuringSubmitRunner()
    service, store, _ = _service(tmp_path, repo, lm, runner=runner)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()
    # The runner drains the just-completed job while still inside submit().
    runner.on_submit = lambda: service.drain_completed_retries(state)

    assert service.retry_publish(4057, state).status == "submitted"

    # The mid-submit drain reconciled the success exactly once.
    assert lm.publish_failed in repo.removed
    assert lm.pr_pending in repo.added
    assert store.get(4057) is None
    completed = [e for e in state.session_history if e.status == "completed"]
    assert [e.issue_number for e in completed] == [4057]


def test_completed_but_undrained_retry_rejects_duplicate_submit(make_session, tmp_path) -> None:
    """A finished-but-not-yet-drained retry still blocks a second submission.

    Duplicate detection is owner state, not the worker's alive bit: after the
    job completes but before the tick drains it, a second Retry Publish click
    must be rejected and reconcile exactly once on drain.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()  # job completes; CompletedJob queued but NOT yet drained
    assert not runner.running_ids()  # worker alive bit is off

    # Second click while completed-but-undrained: rejected, no new job queued.
    assert service.retry_publish(4057, state).status == "rejected"

    service.drain_completed_retries(state)

    # The single original submission reconciled exactly once.
    assert lm.pr_pending in repo.added
    completed = [e for e in state.session_history if e.status == "completed"]
    assert [e.issue_number for e in completed] == [4057]
    assert store.get(4057) is None


# ---------------------------------------------------------------------------
# Live completion ordering: locators persisted before publish-failed labels (F1)
# ---------------------------------------------------------------------------


def test_locators_persisted_before_publish_failed_labels_applied(make_session, tmp_path) -> None:
    """handle_session_completion must record retry locators before applying the
    publish-failed labels, so a crash between the two can't leave GitHub marked
    publish-failed with no locators (Retry Publish permanently unavailable).

    It must ALSO finalize the terminal outcome even when the apply raises past the
    runtime-kill boundary (#6777): the raised apply is captured, the completion is
    terminalized FAILED, and only THEN is the error re-raised. Before that fix the
    apply raise skipped ``finalize_terminal_outcome`` entirely, so this asserts it
    now runs with the effective FAILED status on the raising-apply path."""
    from issue_orchestrator.control.completion_handler import (
        CleanupDecision,
        SessionStatus,
    )
    from issue_orchestrator.control.session_completion import handle_session_completion

    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, _ = _service(tmp_path, repo, lm)

    session = make_session(
        issue_number=4057,
        issue_title="UI: Surface provider status",
        branch_name=BRANCH,
    )

    completion_handler = MagicMock()
    # A publish failure makes process_completion report FAILED for history; the
    # full result lets the post-apply consumer chain run on the raising path.
    completion_handler.process_completion.return_value = SimpleNamespace(
        actions=[
            AddLabelAction(issue_number=4057, label=lm.publish_failed, reason="publish failed"),
        ],
        history_status=SessionStatus.FAILED,
        history_entry=SessionHistoryEntry(
            issue_number=4057,
            title="UI: Surface provider status",
            agent_type="agent:coder",
            status="failed",
            runtime_minutes=1,
            pr_url=None,
        ),
        pr_url=None,
        pr_number=None,
        cleanup=CleanupDecision.immediate(),
        should_queue_review=False,
    )
    # Applying the publish-failed label crashes (simulates a mid-apply failure).
    action_applier = MagicMock()
    action_applier.apply_all.side_effect = RuntimeError("boom applying publish-failed")
    session_output = MagicMock()
    session_output.attach_claude_log.return_value = None

    with pytest.raises(RuntimeError):
        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=OrchestratorState(),
            completion_handler=completion_handler,
            action_applier=action_applier,
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _terminal_id: None,
            config=MagicMock(),
            session_output=session_output,
            processing_errors=["push_branch: Push failed: remote rejected"],
            publish_recovery=service,
            pending_work_claims=_test_claim_store(),
        )

    # Even though the label application crashed, the durable locators were
    # already persisted, so Retry Publish survives.
    assert store.get(4057) is not None
    # Terminal finalization ran on the raising-apply path — exactly once, with the
    # effective FAILED status — instead of being skipped (#6777).
    completion_handler.finalize_terminal_outcome.assert_called_once()
    finalize_args = completion_handler.finalize_terminal_outcome.call_args.args
    assert finalize_args[1] == SessionStatus.FAILED
    # The mandated-reset operator surface is NOT invoked here: no reset action was
    # applied, so the raising path publishes the terminal once with no extra write.
    assert action_applier.apply_all.call_count == 1


# ---------------------------------------------------------------------------
# Submission-scoped drain: old abandoned completion can't evict a newer retry (F2)
# ---------------------------------------------------------------------------


def test_draining_abandoned_submission_does_not_drop_newer_retry(make_session, tmp_path) -> None:
    """Abandon retry A, finish A undrained, submit retry B, then drain A.

    B must remain pending and later reconcile normally — draining the old
    tombstoned submission must not evict B's pending slot or ignore B's
    completion as an unknown job.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    state = OrchestratorState()

    # Retry A, then abandon it (as reset does). A's worker still finishes.
    _record_failure(service, make_session, tmp_path)
    assert service.retry_publish(4057, state).status == "submitted"
    service.abandon_issue(4057)
    runner.run_all()  # A completes; CompletedJob queued but NOT yet drained

    # A fresh attempt fails publish again and submits retry B for the same issue.
    _record_failure(service, make_session, tmp_path)
    assert service.retry_publish(4057, state).status == "submitted"

    # Drain A (the abandoned submission). B must survive.
    service.drain_completed_retries(state)

    # A's tombstoned completion was superseded, not reconciled: no labels flipped.
    assert lm.pr_pending not in repo.added
    assert lm.publish_failed not in repo.removed
    # B is still pending — a duplicate retry is rejected.
    assert service.retry_publish(4057, state).status == "rejected"

    # B now completes and drains: it reconciles normally.
    runner.run_all()
    service.drain_completed_retries(state)

    assert lm.publish_failed in repo.removed
    assert lm.pr_pending in repo.added
    assert store.get(4057) is None
    completed = [e for e in state.session_history if e.status == "completed"]
    assert [e.issue_number for e in completed] == [4057]


# ---------------------------------------------------------------------------
# Non-terminal republish result must not be finalized as publish success (F1/A1)
# ---------------------------------------------------------------------------


def test_deferred_review_exchange_does_not_finalize_publish(make_session, tmp_path) -> None:
    """A republish that defers to a background review exchange is NOT terminal.

    The live completion path keeps such a completion RUNNING; retry-publish must
    likewise leave the issue retryable — it must not remove publish-failed, add
    pr-pending / completed history, or clear the durable locators.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(
        tmp_path, repo, lm,
        result=ProcessingResult.for_review_exchange_deferred(),
    )
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    # Nothing finalized: recovery state is preserved so the issue stays retryable.
    assert lm.publish_failed not in repo.removed
    assert lm.pr_pending not in repo.added
    assert not any(entry.issue_number == 4057 for entry in state.session_history)
    assert store.get(4057) is not None
    # And a follow-up retry is available once the exchange settles.
    assert service.retry_publish(4057, state).status == "submitted"


# ---------------------------------------------------------------------------
# Tech Lead launch-authority retention at retry terminals (#6769 F3)
# ---------------------------------------------------------------------------


def _arm_authority_for_locators(authority_store, locators):
    from issue_orchestrator.domain.tech_lead_session import (
        TechLeadLaunchAuthority,
        TechLeadSessionFlavor,
    )

    authority_store.record(
        run_id=locators.run_assets.run_id,
        session_name=locators.run_assets.session_name,
        authority=TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            anchor_issue_number=4057,
        ),
    )
    authority_store.record_storm_cohort(
        anchor_issue_number=4057,
        cohort=(DiscoveredFailure(41, "Problem 41", "failed"),),
    )


def test_retry_success_discards_tech_lead_authority_row(make_session, tmp_path) -> None:
    """A publish-retryable failure keeps the run's authority row alive (the
    retry re-validates it); the retry's success drain is the run's true
    terminal, so the row is discarded there with the locators (#6769 F3)."""
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    authority_store = InMemoryTechLeadAuthorityStore()
    service, store, runner = _service(
        tmp_path, repo, lm, tech_lead_authority=authority_store
    )
    _record_failure(service, make_session, tmp_path)
    locators = store.get(4057)
    assert locators is not None
    _arm_authority_for_locators(authority_store, locators)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    assert store.get(4057) is None
    assert (
        authority_store.load(
            run_id=locators.run_assets.run_id,
            session_name=locators.run_assets.session_name,
        )
        is None
    )
    assert authority_store.load_storm_cohort(anchor_issue_number=4057) is None


def test_abandon_issue_discards_tech_lead_authority_row(make_session, tmp_path) -> None:
    """Abandonment (reset/teardown) ends the retryable run: locators AND the
    tech_lead authority row go together (#6769 F3)."""
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    authority_store = InMemoryTechLeadAuthorityStore()
    service, store, _runner = _service(
        tmp_path, repo, lm, tech_lead_authority=authority_store
    )
    _record_failure(service, make_session, tmp_path)
    locators = store.get(4057)
    assert locators is not None
    _arm_authority_for_locators(authority_store, locators)

    service.abandon_issue(4057)

    assert store.get(4057) is None
    assert (
        authority_store.load(
            run_id=locators.run_assets.run_id,
            session_name=locators.run_assets.session_name,
        )
        is None
    )
    assert authority_store.load_storm_cohort(anchor_issue_number=4057) is None


# ---------------------------------------------------------------------------
# Command surface G: an unreadable issue is UNKNOWN, never "no blocking labels"
# ---------------------------------------------------------------------------
#
# The fresh reader now raises instead of fabricating `[]` (#6957 round-2 F4), so
# this service has three new outcomes. All three share one rule: an unreadable
# issue must leave the retry EXACTLY as retryable as it was — never submitted on
# a guess, never half-finalized, and never able to abort the rest of a drain.


@dataclass
class _UnreadableRepo(_Repo):
    """A repo whose fresh label read fails for a named set of issues."""

    unreadable: set[int] = field(default_factory=set)
    label_reads: list[int] = field(default_factory=list)

    def read_issue_labels(self, issue_number: int) -> list[str]:
        self.label_reads.append(issue_number)
        if issue_number in self.unreadable:
            raise FreshIssueReadError(
                f"could not read fresh labels for issue #{issue_number}: rate limited"
            )
        return super().read_issue_labels(issue_number)


def test_retry_admission_rejects_when_the_issue_cannot_be_read(
    make_session, tmp_path
) -> None:
    """Unreadable is not "not blocked": admission must not submit on a guess."""
    lm = LabelManager(_config(tmp_path))
    repo = _UnreadableRepo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    repo.unreadable = {4057}
    state = OrchestratorState()

    result = service.retry_publish(4057, state)

    assert result.status == "rejected"
    assert "labels" in result.message
    # Nothing was submitted, and the issue stays exactly as retryable as before:
    # durable locators intact, labels untouched.
    assert runner.running_ids() == set()
    assert store.get(4057) is not None
    assert repo.added == [] and repo.removed == []
    # ...and the same request succeeds once GitHub answers again.
    repo.unreadable = set()
    assert service.retry_publish(4057, state).status == "submitted"


def test_can_retry_publish_reports_false_rather_than_raising(
    make_session, tmp_path
) -> None:
    """This one renders the issue-detail page; it must not surface an exception."""
    lm = LabelManager(_config(tmp_path))
    repo = _UnreadableRepo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, _store, _runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    repo.unreadable = {4057}

    assert service.can_retry_publish(4057, OrchestratorState()) is False


def test_existing_pr_recovery_stays_retryable_when_the_read_fails(
    make_session, tmp_path
) -> None:
    """Recovery finalizes off CURRENT labels; an unreadable issue changes nothing."""
    lm = LabelManager(_config(tmp_path))
    issue = _issue(lm)
    repo = _UnreadableRepo(
        issue=issue,
        labels=list(issue.labels),
        prs=[
            PRInfo(
                number=5453,
                title="#4057: UI: Surface provider status",
                url=PR_URL,
                branch=BRANCH,
                body="",
                state="open",
                labels=[],
            )
        ],
    )
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()
    # Admission reads labels first, then finalization reads them again. Only the
    # SECOND read fails, so the request reaches the recovery path it must not
    # half-apply.
    calls = {"n": 0}
    original = repo.read_issue_labels

    def _fail_on_finalize(issue_number: int) -> list[str]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise FreshIssueReadError("rate limited")
        return original(issue_number)

    repo.read_issue_labels = _fail_on_finalize  # type: ignore[method-assign]

    result = service.retry_publish(4057, state)

    assert result.status == "rejected"
    assert "retryable" in result.message
    # No partial state change: publish-failed labels intact, locators kept, no
    # history entry, no review queued, nothing submitted to the runner.
    assert repo.added == [] and repo.removed == []
    assert lm.publish_failed in repo.labels
    assert store.get(4057) is not None
    assert state.session_history == []
    assert state.discovered_reviews == []
    assert runner.running_ids() == set()


@dataclass
class _TwoIssueRepo:
    """Two independently-labelled issues, so ONE drain can hold two jobs.

    ``_Repo`` is single-issue by construction, which cannot express the thing
    that matters here: an unreadable issue must not strand the OTHER completed
    jobs sharing its drain.
    """

    issues: dict[int, Issue]
    labels: dict[int, list[str]]
    unreadable: set[int] = field(default_factory=set)
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)

    def get_issue(self, issue_number: int) -> Issue | None:
        return self.issues.get(issue_number)

    def read_issue_labels(self, issue_number: int) -> list[str]:
        if issue_number in self.unreadable:
            raise FreshIssueReadError(
                f"could not read fresh labels for issue #{issue_number}: rate limited"
            )
        return list(self.labels.get(issue_number, []))

    def add_label(self, issue_number: int, label: str) -> None:
        labels = self.labels.setdefault(issue_number, [])
        if label not in labels:
            labels.append(label)
        self.added.append((issue_number, label))

    def remove_label(self, issue_number: int, label: str) -> None:
        self.labels[issue_number] = [
            current for current in self.labels.get(issue_number, []) if current != label
        ]
        self.removed.append((issue_number, label))

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        return []


def test_a_finalize_read_failure_does_not_abort_the_rest_of_the_drain(
    make_session, tmp_path
) -> None:
    """The drain runs on the TICK thread, so one unreadable issue must not
    strand every other completed job sharing that drain."""
    lm = LabelManager(_config(tmp_path))
    unreadable_issue = _issue(lm)  # #4057
    healthy_issue = Issue(
        number=4058,
        title="Second retry",
        labels=["agent:web", lm.publish_failed, lm.publish_fail_count_label(2)],
    )
    repo = _TwoIssueRepo(
        issues={4057: unreadable_issue, 4058: healthy_issue},
        labels={
            4057: list(unreadable_issue.labels),
            4058: list(healthy_issue.labels),
        },
    )
    service, store, runner = _service(tmp_path, repo, lm)  # type: ignore[arg-type]
    state = OrchestratorState()
    for number, title in ((4057, "UI: Surface provider status"), (4058, "Second retry")):
        session = make_session(
            issue_number=number, issue_title=title, branch_name=f"{number}-scratch-1"
        )
        completion_file = session.worktree_path / session.completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text("{}")
        service.record_publish_failure(
            session, ["push_branch: Push failed: remote rejected"]
        )
        assert service.retry_publish(number, state).status == "submitted"
    runner.run_all()
    # #4057 becomes unreadable AFTER both republishes completed, so both jobs
    # are waiting in the same drain and the failing one is drained first.
    repo.unreadable = {4057}

    service.drain_completed_retries(state)

    # The unreadable issue is exactly as retryable as before: locators kept,
    # publish-failed intact, nothing finalized.
    assert store.get(4057) is not None
    assert lm.publish_failed in repo.labels[4057]
    assert not [entry for entry in repo.removed if entry[0] == 4057]
    # ...and the healthy issue in the SAME drain still finalized.
    assert store.get(4058) is None
    assert (4058, lm.publish_failed) in repo.removed
    assert [entry.issue_number for entry in state.session_history] == [4058]


def _test_claim_store(tmp_path=None):
    """The orchestrator-owned claim store completion now requires (#6999 F9)."""
    import tempfile
    from pathlib import Path as _Path

    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    return SqlitePendingWorkClaimStore.for_repo(
        _Path(tmp_path) if tmp_path is not None else _Path(tempfile.mkdtemp())
    )
