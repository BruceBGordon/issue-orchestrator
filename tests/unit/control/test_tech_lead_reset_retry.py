"""Tests for the tech_lead reset_retry execution owner (#6764, ADR-0031 §2)."""

from dataclasses import replace
from unittest.mock import ANY, MagicMock, call

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    ActionResultType,
    AddLabelAction,
    ResetRetryIssueAction,
)
from issue_orchestrator.control.claim_gate import ClaimLostError
from issue_orchestrator.control.completion_handler import (
    CleanupDecision,
    CompletionHandler,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
from issue_orchestrator.control.reconciliation import (
    ExternalSnapshot,
    ReconciliationRequired,
)
from issue_orchestrator.control.session_completion import handle_session_completion
from issue_orchestrator.control.state_machine_manager import StateMachineManager
from issue_orchestrator.control.tech_lead_reset_retry import (
    STALE_DOWNGRADE_MODE,
    ResetRetryRunOutcome,
    TechLeadResetRetryExecutor,
    preserve_reset_retry_eligibility,
    reset_retry_stale_reason,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
    SessionStatus,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.state_machines.session_machine import (
    SessionState,
    SessionStateMachine,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.session_output import SessionOutput
from issue_orchestrator.ports.open_issue_corpus_store import (
    InMemoryOpenIssueCorpusStore,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from tests.conftest import make_provider_availability
from tests.unit.session_run_helpers import make_session_run_assets

BLOCKED_FAILED = "blocked-failed"


def make_action(**overrides) -> ResetRetryIssueAction:
    defaults = dict(
        issue_number=17,
        rationale="Worktree unrecoverable; scratch reset is the fix.",
        proposal_id="A2",
        finding_ids=("T1",),
        anchor_issue_number=17,
        reason="tech_lead decision action A2: reset and retry from scratch",
    )
    defaults.update(overrides)
    return ResetRetryIssueAction(**defaults)


def make_issue(**overrides) -> Issue:
    defaults = dict(
        number=17,
        title="Broken issue",
        labels=["agent:test", BLOCKED_FAILED],
        state="open",
        repo="owner/repo",
    )
    defaults.update(overrides)
    return Issue(**defaults)


def make_executor(
    *,
    issue: Issue | None = ...,
    active_session: bool = False,
    outcome: ResetRetryRunOutcome | None = None,
) -> tuple[TechLeadResetRetryExecutor, MagicMock, MagicMock]:
    """Executor with recording events + run_reset fakes."""
    events = MagicMock()
    run_reset = MagicMock(
        return_value=outcome
        if outcome is not None
        else ResetRetryRunOutcome(success=True, details={"queued_now": True})
    )
    resolved_issue = make_issue() if issue is ... else issue
    executor = TechLeadResetRetryExecutor(
        events=events,
        label_manager=LabelManager(Config()),
        read_issue=lambda _n: resolved_issue,
        has_active_issue_runtime=lambda _n: active_session,
        run_reset=run_reset,
    )
    return executor, events, run_reset


def published(events: MagicMock, name: EventName) -> list:
    return [
        call.args[0]
        for call in events.publish.call_args_list
        if call.args[0].name == name.value
    ]


class _RaisingApplier:
    """ActionApplier stand-in whose ``apply_all`` raises past the runtime-kill
    boundary, modelling the ``ReconciliationRequired`` / ``ClaimLostError`` races
    the real applier rethrows. Counts calls so a test can prove finalization
    publishes exactly once with no second GitHub write after the raise."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.apply_all_calls = 0

    def apply_all(self, _actions):
        self.apply_all_calls += 1
        raise self._error


class TestStaleReason:
    """Pure precondition policy."""

    def test_valid_when_open_blocked_and_idle(self):
        reason = reset_retry_stale_reason(
            issue=make_issue(),
            active_runtime=False,
            label_manager=LabelManager(Config()),
        )
        assert reason is None

    def test_unreadable_issue_is_stale(self):
        reason = reset_retry_stale_reason(
            issue=None,
            active_runtime=False,
            label_manager=LabelManager(Config()),
        )
        assert reason is not None and "could not be read" in reason

    def test_closed_issue_is_stale(self):
        reason = reset_retry_stale_reason(
            issue=make_issue(state="closed"),
            active_runtime=False,
            label_manager=LabelManager(Config()),
        )
        assert reason is not None and "closed" in reason

    def test_active_runtime_is_stale(self):
        reason = reset_retry_stale_reason(
            issue=make_issue(),
            active_runtime=True,
            label_manager=LabelManager(Config()),
        )
        assert reason is not None and "active runtime" in reason

    def test_no_blocking_label_is_stale(self):
        reason = reset_retry_stale_reason(
            issue=make_issue(labels=["agent:test"]),
            active_runtime=False,
            label_manager=LabelManager(Config()),
        )
        assert reason is not None and "blocking-class" in reason


class TestExecutorApply:
    def test_valid_preconditions_invoke_reset_owner(self):
        executor, events, run_reset = make_executor()

        result = executor.apply(make_action())

        assert result.result_type == ActionResultType.SUCCESS
        run_reset.assert_called_once_with(17, ["agent:test", BLOCKED_FAILED])
        [executed] = published(events, EventName.TECH_LEAD_ACTION_EXECUTED)
        assert executed.data["issue_number"] == 17  # anchor
        assert executed.data["target_number"] == 17
        assert executed.data["action_id"] == "A2"
        assert executed.data["proposal_type"] == "reset_retry"
        assert executed.data["boundary"] == {"queued_now": True}
        assert not published(events, EventName.TECH_LEAD_ACTION_PROPOSED)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"active_session": True},
            {"issue": None},
            {"issue": Issue(number=17, title="t", labels=["agent:test"], state="closed", repo="o/r")},
            {"issue": Issue(number=17, title="t", labels=["agent:test"], repo="o/r")},
        ],
        ids=["active-session", "unreadable", "closed", "no-blocking-label"],
    )
    def test_stale_precondition_downgrades_without_mutation(self, kwargs):
        executor, events, run_reset = make_executor(**kwargs)

        result = executor.apply(make_action())

        assert result.result_type == ActionResultType.SKIPPED
        assert result.details["mode"] == STALE_DOWNGRADE_MODE
        run_reset.assert_not_called()
        [surfaced] = published(events, EventName.TECH_LEAD_ACTION_PROPOSED)
        assert surfaced.data["mode"] == STALE_DOWNGRADE_MODE
        assert surfaced.data["stale_reason"]
        assert surfaced.data["action_id"] == "A2"
        assert surfaced.data["target_number"] == 17
        assert surfaced.data["body_preview"].startswith("Worktree unrecoverable")
        assert not published(events, EventName.TECH_LEAD_ACTION_EXECUTED)

    def test_downgrade_event_reports_on_anchor_issue(self):
        executor, events, _ = make_executor(active_session=True)

        executor.apply(make_action(issue_number=17, anchor_issue_number=99))

        [surfaced] = published(events, EventName.TECH_LEAD_ACTION_PROPOSED)
        assert surfaced.data["issue_number"] == 99
        assert surfaced.data["target_number"] == 17

    def test_owner_failure_fails_loudly(self):
        executor, events, run_reset = make_executor(
            outcome=ResetRetryRunOutcome(success=False, error="branch delete exploded")
        )

        result = executor.apply(make_action())

        assert result.result_type == ActionResultType.FAILURE
        assert "branch delete exploded" in (result.error or "")
        assert "A2" in (result.error or "")
        run_reset.assert_called_once()
        assert not published(events, EventName.TECH_LEAD_ACTION_EXECUTED)
        assert not published(events, EventName.TECH_LEAD_ACTION_PROPOSED)


class TestApplierDispatch:
    def _applier(self, executor=None) -> tuple[ActionApplier, MagicMock]:
        events = MagicMock()
        applier = ActionApplier(
            labels=MagicMock(),
            sessions=MagicMock(),
            events=events,
        )
        applier.tech_lead_reset_retry = executor
        return applier, events

    def test_dispatch_routes_to_executor(self):
        executor, _events, run_reset = make_executor()
        applier, _ = self._applier(executor)

        result = applier.apply(make_action())

        assert result.result_type == ActionResultType.SUCCESS
        run_reset.assert_called_once()

    def test_unwired_executor_fails_loudly(self):
        applier, _ = self._applier(executor=None)

        result = applier.apply(make_action())

        assert result.result_type == ActionResultType.FAILURE
        assert "no" in (result.error or "") and "wired" in (result.error or "")


class TestPreserveEligibility:
    def test_successful_reset_actions_are_recleared(self):
        executor, _events, _run_reset = make_executor()
        ok = executor.apply(make_action(issue_number=17))
        make_retryable = MagicMock()

        cleared = preserve_reset_retry_eligibility(
            [ok], make_retryable=make_retryable
        )

        assert cleared == [17]
        make_retryable.assert_called_once_with(17)

    def test_downgraded_and_foreign_results_are_ignored(self):
        stale_executor, _e, _r = make_executor(active_session=True)
        downgraded = stale_executor.apply(make_action())
        failed_executor, _e2, _r2 = make_executor(
            outcome=ResetRetryRunOutcome(success=False, error="boom")
        )
        failed = failed_executor.apply(make_action())
        make_retryable = MagicMock()

        cleared = preserve_reset_retry_eligibility(
            [downgraded, failed], make_retryable=make_retryable
        )

        assert cleared == []
        make_retryable.assert_not_called()


class TestCompletionPipelineEligibility:
    """The completion pipeline must not re-block an issue its own actions
    just reset: the history entry appended after apply_all would otherwise
    silently gate the relaunch (#6764)."""

    def _session(self, tmp_path) -> Session:
        issue = Issue(
            number=17,
            title="Broken issue",
            labels=["agent:tech-lead"],
            repo="owner/repo",
        )
        return Session(
            key=SessionKey(issue=FakeIssueKey("17"), task=TaskKind.CODE),
            issue=issue,
            agent_config=AgentConfig(
                prompt_path=tmp_path / "prompt.md", timeout_minutes=45
            ),
            terminal_id="issue-17",
            worktree_path=tmp_path / "worktree",
            branch_name="17-fix",
            run_assets=make_session_run_assets(
                tmp_path / "worktree", session_name="issue-17"
            ),
        )

    def _arrange(
        self,
        tmp_path,
        executor,
        *,
        seed_failed: bool = True,
        config=None,
        observer=None,
        repository_host=None,
        labels=None,
        leading_actions=(),
    ):
        """Arrange a COMPLETED tech_lead completion whose mandated action is a reset,
        returning ``(state, run)`` so callers can assert on pre-run state before
        invoking ``run()``. The agent always reports ``completed``; the injected
        ``executor`` decides whether the mandated reset commits. ``leading_actions``
        are success-only mutations planned BEFORE the reset in the same batch, to
        prove the reset gates them (#6779 R13)."""
        session = self._session(tmp_path)
        state = OrchestratorState()
        state.active_sessions = [session]
        if seed_failed:
            state.failed_this_cycle.add(17)

        completion_handler = MagicMock()
        completion_handler.process_completion.return_value = MagicMock(
            actions=[
                *leading_actions,
                make_action(issue_number=17, anchor_issue_number=17),
            ],
            history_entry=SessionHistoryEntry(
                issue_number=17,
                title="Broken issue",
                agent_type="agent:tech-lead",
                status="completed",
                runtime_minutes=3,
                pr_url=None,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        applier = ActionApplier(
            labels=labels if labels is not None else MagicMock(),
            sessions=MagicMock(),
            events=MagicMock(),
            repository_host=repository_host,
        )
        applier.tech_lead_reset_retry = executor

        if config is None:
            config = MagicMock()
            config.code_review_agent = None

        session_output = MagicMock(spec=SessionOutput)
        session_output.attach_claude_log.return_value = None

        def run() -> None:
            handle_session_completion(
                session=session,
                status=SessionStatus.COMPLETED,
                state=state,
                completion_handler=completion_handler,
                action_applier=applier,
                observer=observer if observer is not None else MagicMock(),
                worktree_manager=None,
                kill_session_fn=lambda _x: None,
                config=config,
                session_output=session_output,
                pending_work_claims=_test_claim_store(),
            )

        return state, run

    def _run_completion(self, tmp_path, executor) -> OrchestratorState:
        state, run = self._arrange(tmp_path, executor)
        run()
        return state

    def test_successful_reset_survives_the_history_append(self, tmp_path):
        executor, _events, run_reset = make_executor()

        state = self._run_completion(tmp_path, executor)

        run_reset.assert_called_once()
        assert all(e.issue_number != 17 for e in state.session_history), (
            "the completion's own history entry must not re-block the reset issue"
        )
        assert 17 not in state.failed_this_cycle

    def test_downgraded_reset_keeps_the_history_entry(self, tmp_path):
        executor, _events, run_reset = make_executor(active_session=True)

        state = self._run_completion(tmp_path, executor)

        run_reset.assert_not_called()
        assert any(e.issue_number == 17 for e in state.session_history), (
            "a downgraded proposal posts no mutations - history must be intact"
        )

    def test_failed_mandated_reset_routes_effective_failure_end_to_end(self, tmp_path):
        """A COMPLETED agent whose mandated reset FAILED at apply time is
        terminalized as FAILED and routed through EVERY post-apply consumer,
        with no success effect surviving (#6764 re-review F2).

        This begins with an EMPTY failure gate and asserts the completion path
        itself adds the issue — unlike the prior version, which pre-seeded
        ``failed_this_cycle`` and so passed even though the failed-reset path
        never routed anything."""
        executor, _events, run_reset = make_executor(
            outcome=ResetRetryRunOutcome(
                success=False, error="branch delete exploded"
            )
        )
        observer = MagicMock()
        repository_host = MagicMock()
        labels = MagicMock()
        labels.has_label.return_value = False  # let the needs-human add proceed

        state, run = self._arrange(
            tmp_path,
            executor,
            seed_failed=False,
            config=Config(),
            observer=observer,
            repository_host=repository_host,
            labels=labels,
        )
        # Begins WITHOUT the failure gate: the completion path must add it.
        assert 17 not in state.failed_this_cycle

        run()

        run_reset.assert_called_once()

        # Effective terminal status is FAILED across the whole post-apply phase.
        # Observer observed the failure, not the agent-reported success:
        observer.handle_completion.assert_called_once_with(ANY, SessionStatus.FAILED)

        # Failure discovery recorded the failure as a fact:
        [failure] = state.discovered_failures
        assert failure.issue_number == 17
        assert failure.failure_reason == "failed"

        # Retry gate was set BY the completion path (started empty above):
        assert 17 in state.failed_this_cycle

        # Immediate cleanup reason reflects the effective failure, not "completed":
        [cleanup] = state.immediate_cleanups
        assert cleanup.reason == "failed"

        # History terminalized as FAILED with the reset-owner error, never the
        # agent's "completed" intent:
        [entry] = state.session_history
        assert entry.issue_number == 17
        assert entry.status == "failed"
        assert "branch delete exploded" in (entry.status_reason or "")

        # Durable, crash-safe operator surface via the existing label/comment
        # action owners (needs-human label + explanatory comment on the issue):
        labels.add_label.assert_any_call(17, "needs-human")
        assert repository_host.add_comment.call_count == 1
        comment_number, comment_body = repository_host.add_comment.call_args.args
        assert comment_number == 17
        assert "Reset & Retry Did Not Complete" in comment_body
        assert "branch delete exploded" in comment_body

        # No success effect survived: the reset-success side effect (making the
        # issue retryable, which prunes its own history entry and clears the
        # failure gate) did NOT run, so the FAILED record and gate both stand.
        assert any(e.issue_number == 17 for e in state.session_history)

    def test_failing_mandated_reset_withholds_cobatched_success_effect(self, tmp_path):
        """#6779 R13 root cause — mandated authority must GATE the success-only
        effects it is co-batched with, not just the terminal consumers.

        A success-only label mutation planned BEFORE the failing mandated reset in
        the SAME action batch must NOT commit: the reset is the authority gate, so
        its siblings are withheld when it fails. Before this fix ``apply_all`` ran
        the whole list in order and committed the success mutation before the reset
        failure was ever detected."""
        executor, _events, run_reset = make_executor(
            outcome=ResetRetryRunOutcome(success=False, error="reset exploded")
        )
        observer = MagicMock()
        labels = MagicMock()
        labels.has_label.return_value = False  # would let any add proceed
        success_effect = AddLabelAction(
            issue_number=17, label="tech-lead-done", reason="success-only completion label"
        )

        state, run = self._arrange(
            tmp_path,
            executor,
            seed_failed=False,
            config=Config(),
            observer=observer,
            repository_host=MagicMock(),
            labels=labels,
            leading_actions=[success_effect],
        )

        run()

        run_reset.assert_called_once()
        # The co-batched success-only label was WITHHELD — the gate did not commit.
        # (needs-human IS added by the crash-safe operator surface; assert the
        # SUCCESS label specifically never applied.)
        assert call(17, "tech-lead-done") not in labels.add_label.call_args_list
        # The whole completion still terminalized FAILED, with no success recorded:
        observer.handle_completion.assert_called_once_with(ANY, SessionStatus.FAILED)
        assert 17 not in state.completed_today
        [entry] = state.session_history
        assert entry.status == "failed"


class _HandlerWithMandatedReset(CompletionHandler):
    """Real ``CompletionHandler`` that may also carry one decision-mandated reset
    action (standing in for the tech_lead decision that planned it), so the REAL
    deferred terminal-event path runs while the applier's injected executor
    decides whether the reset commits (#6764 re-review F3). With no mandated
    action it is a plain completion."""

    def __init__(
        self, *args, mandated_action: ResetRetryIssueAction | None = None, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self._mandated_action = mandated_action

    def process_completion(self, *args, **kwargs):  # type: ignore[override]
        result = super().process_completion(*args, **kwargs)
        if self._mandated_action is None:
            return result
        return replace(result, actions=result.actions + (self._mandated_action,))


class TestEffectiveTerminalOutcomeEvents:
    """Terminal event + completed_today + CLAIM_RELEASED all derive from the ONE
    effective outcome computed post-apply.

    Unlike the mocked-handler pipeline test above, these use the REAL
    ``CompletionHandler`` with a recording sink, so a false ``SESSION_COMPLETED``
    emitted BEFORE the mandated reset is applied would be visible (#6764
    re-review F3). The mandated reset is the completion's one act-level action;
    the injected executor decides whether it commits."""

    def _session(self, tmp_path) -> Session:
        issue = Issue(
            number=17, title="Broken issue", labels=["agent:tech-lead"], repo="owner/repo"
        )
        return Session(
            key=SessionKey(issue=FakeIssueKey("17"), task=TaskKind.CODE),
            issue=issue,
            agent_config=AgentConfig(
                prompt_path=tmp_path / "prompt.md", timeout_minutes=45
            ),
            terminal_id="issue-17",
            worktree_path=tmp_path / "worktree",
            branch_name="17-fix",
            lease_id="lease-17",
            run_assets=make_session_run_assets(
                tmp_path / "worktree", session_name="issue-17"
            ),
        )

    def _real_handler(
        self,
        events: InMemoryEventSink,
        *,
        mandated_action: ResetRetryIssueAction | None,
        session_machine: SessionStateMachine | None = None,
    ) -> CompletionHandler:
        repository_host = MagicMock()
        repository_host.get_prs_for_branch.return_value = []
        repository_host.get_pr.return_value = None
        repository_host.get_issue_labels_fresh.return_value = []
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None
        session_output.attach_claude_log.return_value = None
        session_output.get_log_path_for_run_dir.return_value = None
        config = Config()
        return _HandlerWithMandatedReset(
            config=config,
            events=events,
            repository_host=repository_host,
            get_issue_machine_fn=lambda _issue: None,
            get_session_machine_fn=lambda _terminal_id: session_machine,
            get_review_machine_fn=lambda _pr_number: None,
            session_output=session_output,
            tech_lead_authority=InMemoryTechLeadAuthorityStore(),
            open_issue_corpus=OpenIssueCorpusManager(
                repository_host,
                InMemoryOpenIssueCorpusStore(),
                is_enabled=lambda: config.tech_lead.dedup.enabled,
            ),
            active_session_run_id=lambda _issue_number: None,
            provider_availability=make_provider_availability(config),
            mandated_action=mandated_action,
        )

    def _run(
        self,
        tmp_path,
        *,
        events: InMemoryEventSink,
        executor=None,
        mandated_action: ResetRetryIssueAction | None,
        session_machine: SessionStateMachine | None = None,
        action_applier=None,
        state: OrchestratorState | None = None,
        claim_manager=None,
    ) -> OrchestratorState:
        session = self._session(tmp_path)
        if state is None:
            state = OrchestratorState()
        state.active_sessions = [session]
        if action_applier is None:
            action_applier = ActionApplier(
                labels=MagicMock(),
                sessions=MagicMock(),
                events=MagicMock(),
                repository_host=MagicMock(),
            )
            action_applier.tech_lead_reset_retry = executor
        session_output = MagicMock(spec=SessionOutput)
        session_output.attach_claude_log.return_value = None
        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=self._real_handler(
                events,
                mandated_action=mandated_action,
                session_machine=session_machine,
            ),
            action_applier=action_applier,
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _x: None,
            config=Config(),
            session_output=session_output,
            claim_manager=claim_manager if claim_manager is not None else MagicMock(),
            events=events,
            pending_work_claims=_test_claim_store(),
        )
        return state

    def test_failed_mandated_reset_publishes_only_session_failed(self, tmp_path):
        events = InMemoryEventSink()
        executor, _events, run_reset = make_executor(
            outcome=ResetRetryRunOutcome(success=False, error="branch delete exploded")
        )

        state = self._run(
            tmp_path,
            events=events,
            executor=executor,
            mandated_action=make_action(issue_number=17, anchor_issue_number=17),
        )

        run_reset.assert_called_once()
        # The false pre-apply success is never published; exactly one terminal
        # event fires and it is the effective FAILURE:
        assert events.get_events(EventName.SESSION_COMPLETED.value) == []
        failed = events.get_events(EventName.SESSION_FAILED.value)
        assert len(failed) == 1
        assert failed[0].data["issue_number"] == 17
        # A failed mandated reset leaves NO completed_today success entry:
        assert 17 not in state.completed_today
        # The CLAIM_RELEASED machine payload reports the effective failure:
        [claim] = events.get_events(EventName.CLAIM_RELEASED.value)
        assert claim.data["status"] == "failed"

    def test_committed_reset_publishes_only_session_completed(self, tmp_path):
        events = InMemoryEventSink()
        executor, _events, run_reset = make_executor()  # commits

        self._run(
            tmp_path,
            events=events,
            executor=executor,
            mandated_action=make_action(issue_number=17, anchor_issue_number=17),
        )

        run_reset.assert_called_once()
        # No-regression: a committed reset keeps the single SESSION_COMPLETED and
        # releases the claim as completed. (completed_today is intentionally
        # re-cleared here by preserve_reset_retry_eligibility, since a reset
        # issue is about to be retried — see the plain-completion case below for
        # the completed_today success gate.)
        assert len(events.get_events(EventName.SESSION_COMPLETED.value)) == 1
        assert events.get_events(EventName.SESSION_FAILED.value) == []
        [claim] = events.get_events(EventName.CLAIM_RELEASED.value)
        assert claim.data["status"] == "completed"

    def test_plain_completion_records_completed_today(self, tmp_path):
        """No-regression for the completed_today success gate: an ordinary
        committed completion (no mandated act-level action) still records the
        issue and emits the single SESSION_COMPLETED terminal event."""
        events = InMemoryEventSink()

        state = self._run(tmp_path, events=events, mandated_action=None)

        assert len(events.get_events(EventName.SESSION_COMPLETED.value)) == 1
        assert events.get_events(EventName.SESSION_FAILED.value) == []
        assert 17 in state.completed_today
        [claim] = events.get_events(EventName.CLAIM_RELEASED.value)
        assert claim.data["status"] == "completed"

    def test_failed_mandated_reset_ends_real_machine_failed(self, tmp_path):
        """A REAL running SessionStateMachine ends FAILED — not COMPLETED — when
        the agent reported COMPLETED but the mandated reset FAILED at apply.

        The prior helper wired ``get_session_machine_fn`` to ``None``, so the
        cached-machine transition never ran and the bug was invisible: the machine
        was left COMPLETED before the authoritative reset outcome existed. With a
        real machine wired through the handler, the deferred, effective-status-
        driven transition (#6777) is exercised end-to-end."""
        events = InMemoryEventSink()
        executor, _events, run_reset = make_executor(
            outcome=ResetRetryRunOutcome(success=False, error="branch delete exploded")
        )
        machine = SessionStateMachine(
            "issue-17", 17, initial_state=SessionState.RUNNING
        )

        state = self._run(
            tmp_path,
            events=events,
            executor=executor,
            mandated_action=make_action(issue_number=17, anchor_issue_number=17),
            session_machine=machine,
        )

        run_reset.assert_called_once()
        # The cached lifecycle machine is terminalized from the SAME effective
        # FAILED outcome as the event/history — never the agent's COMPLETED intent:
        assert machine.get_state() is SessionState.FAILED
        # The round-4 terminal-consumer guarantees still hold in lockstep:
        assert events.get_events(EventName.SESSION_COMPLETED.value) == []
        assert len(events.get_events(EventName.SESSION_FAILED.value)) == 1
        assert 17 not in state.completed_today
        [claim] = events.get_events(EventName.CLAIM_RELEASED.value)
        assert claim.data["status"] == "failed"

    def test_committed_reset_ends_real_machine_completed(self, tmp_path):
        """A committed mandated reset ends the REAL machine COMPLETED, matching the
        single SESSION_COMPLETED terminal event (no false split, #6777)."""
        events = InMemoryEventSink()
        executor, _events, run_reset = make_executor()  # commits
        machine = SessionStateMachine(
            "issue-17", 17, initial_state=SessionState.RUNNING
        )

        self._run(
            tmp_path,
            events=events,
            executor=executor,
            mandated_action=make_action(issue_number=17, anchor_issue_number=17),
            session_machine=machine,
        )

        run_reset.assert_called_once()
        assert machine.get_state() is SessionState.COMPLETED
        assert len(events.get_events(EventName.SESSION_COMPLETED.value)) == 1
        assert events.get_events(EventName.SESSION_FAILED.value) == []

    @pytest.mark.parametrize(
        "raised",
        [
            ReconciliationRequired(
                entity_type="issue",
                entity_id=17,
                expected=ExternalSnapshot.for_issue(17, set()),
                actual=ExternalSnapshot.for_issue(17, {"drifted"}),
                reason="labels changed under us",
            ),
            ClaimLostError(issue_number=17, operation="add_label"),
        ],
        ids=["reconciliation_required", "claim_lost"],
    )
    def test_apply_exception_finalizes_real_machine_failed_then_reraises(
        self, tmp_path, raised
    ):
        """An apply that RAISES past the runtime-kill boundary must still drive the
        REAL running machine to FAILED, commit every terminal consumer exactly
        once, and re-raise — never leave a RUNNING machine a later same-id launch
        could reuse (#6777).

        This is the gap the round-5 owner missed: ``finalize_terminal_outcome``
        only ran on the RETURN path from ``apply_all``. ``ReconciliationRequired``
        and ``ClaimLostError`` are expected production races that ``ActionApplier``
        rethrows, so before this fix they bypassed finalization and stranded a
        RUNNING cached machine with the runtime already dead."""
        events = InMemoryEventSink()
        machine = SessionStateMachine(
            "issue-17", 17, initial_state=SessionState.RUNNING
        )
        applier = _RaisingApplier(raised)
        claim_manager = MagicMock()
        state = OrchestratorState()

        with pytest.raises(type(raised)) as excinfo:
            self._run(
                tmp_path,
                events=events,
                mandated_action=make_action(issue_number=17, anchor_issue_number=17),
                session_machine=machine,
                action_applier=applier,
                state=state,
                claim_manager=claim_manager,
            )

        # The SAME error propagates (nothing swallowed / re-wrapped)...
        assert excinfo.value is raised
        # ...but ONLY after finalization: the cached machine ends terminal FAILED.
        assert machine.get_state() is SessionState.FAILED
        # Exactly one terminal event, the deliberate FAILURE — no double publication
        # and no false COMPLETED before the apply aborted:
        assert events.get_events(EventName.SESSION_COMPLETED.value) == []
        assert len(events.get_events(EventName.SESSION_FAILED.value)) == 1
        # Success gate untouched:
        assert 17 not in state.completed_today
        # Claim released, its machine payload reporting the effective failure:
        claim_manager.release_claim.assert_called_once_with(17, "lease-17")
        [claim] = events.get_events(EventName.CLAIM_RELEASED.value)
        assert claim.data["status"] == "failed"
        # Failure discovery + cleanup facts committed, consistent with the return path:
        assert [f.issue_number for f in state.discovered_failures] == [17]
        assert 17 in state.failed_this_cycle
        assert [c.reason for c in state.immediate_cleanups] == ["failed"]
        # History terminalized FAILED, never the agent's "completed" intent:
        [entry] = state.session_history
        assert entry.status == "failed"
        # The applier was invoked exactly once: no second GitHub write (operator
        # surface) after the raise, so finalization publishes once and only once.
        assert applier.apply_all_calls == 1
        # A later launch under the same terminal id CANNOT reuse a RUNNING machine:
        # the machine is terminal, so StateMachineManager replaces it on next get.
        manager = StateMachineManager(Config())
        manager.session_machines["issue-17"] = machine
        assert manager.get_session_machine("issue-17", 17) is not machine


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
