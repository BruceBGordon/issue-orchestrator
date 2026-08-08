"""The operator retry/dismiss transition, at the owner (#6999 F5/A2).

The endpoint tests in ``tests/unit/test_control_api_issue_actions.py`` cover the
other side of the boundary — that the transport maps this command's typed
outcome and does nothing else. These cover the invariant itself, which is what
actually went wrong: local retry/queue state may only be settled once the GitHub
side of the transition committed, and a refusal must leave BOTH untouched.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import BlockOutcome
from issue_orchestrator.control.operator_issue_command_runner import (
    OperatorIssueCommandRunner,
)
from issue_orchestrator.control.operator_unblock import OperatorUnblocker
from issue_orchestrator.domain.models import (
    Issue,
    OrchestratorState,
    SessionHistoryEntry,
)
from issue_orchestrator.ports.operator_issue_commands import (
    OperatorCommandIntent,
    OperatorCommandStatus,
)

ISSUE = 903


class _RepositoryHost:
    """Ordinary label removals, with an optional refusal for named labels."""

    def __init__(self, live: dict[int, set[str]], *, refuse: frozenset[str] = frozenset()):
        self.live = live
        self.refuse = refuse

    def remove_label(self, issue_number: int, label: str) -> None:
        if label in self.refuse:
            raise RuntimeError(f"github refused to remove {label}")
        self.live.setdefault(issue_number, set()).discard(label)


class _Block:
    """A shared-block owner that either clears its label or refuses to."""

    def __init__(self, label: str, live: dict[int, set[str]], *, held=()):
        self.label = label
        self.live = live
        self.held = tuple(held)

    def owns(self, label: str) -> bool:
        return label == self.label

    def force_clear(self, target: int, reason: str) -> BlockOutcome:
        del reason
        if self.held:
            return BlockOutcome.HELD_BY_ANOTHER_CAUSE
        self.live.setdefault(target, set()).discard(self.label)
        return BlockOutcome.CLEARED

    def unsettleable_holders(self, issue_number: int):
        del issue_number
        return self.held


class _FreshLabels:
    def __init__(self, live: dict[int, set[str]]) -> None:
        self.live = live

    def read_issue_labels(self, issue_number: int) -> list[str]:
        return sorted(self.live.get(issue_number, set()))


class _Cause:
    """Stands in for a real ``NeedsHumanCause`` in the holder tuple."""

    def __init__(self, value: str) -> None:
        self.value = value


@pytest.fixture
def state() -> OrchestratorState:
    state = OrchestratorState()
    state.session_history = [
        SessionHistoryEntry(
            issue_number=ISSUE,
            title="Blocked issue",
            agent_type="agent:test",
            status="timed_out",
            runtime_minutes=95,
        )
    ]
    state.failed_this_cycle = {ISSUE}
    return state


def _runner(sample_config, state, live, *, block=None, refuse=frozenset(), store=None,
            host=None):
    from unittest.mock import MagicMock

    labels = LabelManager(sample_config)
    host = host if host is not None else _RepositoryHost(live, refuse=refuse)
    return labels, OperatorIssueCommandRunner(
        unblocker=OperatorUnblocker(
            repository_host=host,
            labels=labels,
            block=block or _Block(labels.needs_human, live),
        ),
        fresh_labels=_FreshLabels(live),
        config=sample_config,
        queue_cache_store=store if store is not None else MagicMock(),
        state=lambda: state,
        run_locked=lambda fn: fn(),
    )


class TestTheGitHubSideSettlesFirst:
    """Local state may only move after the label transition committed."""

    def test_a_refused_block_leaves_the_retry_gates_alone(
        self, sample_config, state
    ):
        """The concrete failure: gates cleared for a still-blocked issue.

        Clearing ``session_history``/``failed_this_cycle`` is what makes the
        planner eligible again, so doing it while GitHub still carries the
        block hands the planner an issue it will relaunch straight into.
        """
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        _labels, runner = _runner(
            sample_config,
            state,
            live,
            block=_Block(
                labels.needs_human, live, held=(_Cause("claim_quarantine"),)
            ),
        )

        outcome = runner.retry(ISSUE)

        assert outcome.status is OperatorCommandStatus.STILL_BLOCKED
        assert not outcome.committed
        assert outcome.held_by == ("claim_quarantine",)
        assert labels.needs_human in live[ISSUE]
        # Nothing after the refusal ran: the ordinary blocked label is still
        # there too, because stripping provenance out from under a refusal is
        # exactly what left blocks standing with nothing to explain them.
        assert labels.blocked in live[ISSUE]
        assert [entry.issue_number for entry in state.session_history] == [ISSUE]
        assert state.failed_this_cycle == {ISSUE}

    def test_a_refused_block_stops_dismiss_the_same_way(self, sample_config, state):
        """One rule, both commands - dismiss used to prune and report success."""
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        _labels, runner = _runner(
            sample_config,
            state,
            live,
            block=_Block(
                labels.needs_human, live, held=(_Cause("tech_lead_escalation"),)
            ),
        )

        outcome = runner.dismiss(ISSUE)

        assert outcome.status is OperatorCommandStatus.STILL_BLOCKED
        assert labels.needs_human in live[ISSUE]
        assert [entry.issue_number for entry in state.session_history] == [ISSUE]

    def test_an_unremovable_ordinary_label_also_keeps_the_gates(
        self, sample_config, state
    ):
        """Retry's partial failure: the block cleared, an ordinary label did not."""
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        _labels, runner = _runner(
            sample_config, state, live, refuse=frozenset({labels.blocked})
        )

        outcome = runner.retry(ISSUE)

        assert outcome.status is OperatorCommandStatus.INCOMPLETE
        assert not outcome.committed
        assert labels.blocked in outcome.failed
        assert labels.needs_human in outcome.removed
        assert [entry.issue_number for entry in state.session_history] == [ISSUE]
        assert state.failed_this_cycle == {ISSUE}

    def test_retry_settles_the_gates_once_every_label_came_off(
        self, sample_config, state
    ):
        """The committed path, so the assertions above are not vacuous."""
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        _labels, runner = _runner(sample_config, state, live)

        outcome = runner.retry(ISSUE)

        assert outcome.status is OperatorCommandStatus.COMMITTED
        assert outcome.committed
        assert live[ISSUE] == set()
        assert state.session_history == []
        assert state.failed_this_cycle == set()

    def test_dismiss_takes_the_issue_off_the_board_once_it_can(
        self, sample_config, state
    ):
        """...and the same for dismiss."""
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        _labels, runner = _runner(sample_config, state, live)

        outcome = runner.dismiss(ISSUE)

        assert outcome.committed
        assert labels.needs_human not in live[ISSUE]
        assert state.session_history == []

    def test_dismiss_stops_on_an_ordinary_label_it_cannot_remove(
        self, sample_config, state
    ):
        """Dismiss obeys the SAME invariant retry does (#6999 F5 round 7).

        This used to be excused as "a label that is already gone raises too".
        It is not true of the production adapter: ``remove_label`` treats a 404
        as idempotent success and retries transport faults itself, so a raise
        reaching here means GitHub still carries the label. Pruning the board
        over it leaves an issue blocked on GitHub, invisible locally, and an
        operator told it was dismissed - which is the entire defect this
        command exists to prevent, arriving through the other door.

        Every local store is asserted, because "left in place" has to mean all
        of them: the history gate, the retry gate, the cached copies the
        planner reads, and the snapshot persisted behind them.
        """
        from unittest.mock import MagicMock

        from issue_orchestrator.domain.models import Issue

        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        cached = Issue(number=ISSUE, title="Blocked issue", labels=["blocked"])
        state.cached_scope_issues = [cached]
        state.cached_queue_issues = [cached]
        store = MagicMock()
        _labels, runner = _runner(
            sample_config,
            state,
            live,
            refuse=frozenset({labels.blocked}),
            store=store,
        )

        outcome = runner.dismiss(ISSUE)

        assert outcome.status is OperatorCommandStatus.INCOMPLETE
        assert not outcome.committed
        assert labels.blocked in outcome.failed
        # The shared block DID come off, and that is reported honestly...
        assert labels.needs_human in outcome.removed
        # ...but nothing local moved, because GitHub and the board would then
        # disagree about an issue that is still gated.
        assert [entry.issue_number for entry in state.session_history] == [ISSUE]
        assert state.failed_this_cycle == {ISSUE}
        assert state.cached_scope_issues == [cached]
        assert state.cached_queue_issues == [cached]
        assert store.mock_calls == [], "no queue snapshot may be persisted"

    def test_retry_leaves_the_same_stores_alone_on_a_removal_failure(
        self, sample_config, state
    ):
        """The retry half of the same assertion, so the pair cannot drift."""
        from unittest.mock import MagicMock

        from issue_orchestrator.domain.models import Issue

        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        cached = Issue(number=ISSUE, title="Blocked issue", labels=["blocked"])
        state.cached_scope_issues = [cached]
        state.cached_queue_issues = [cached]
        store = MagicMock()
        _labels, runner = _runner(
            sample_config,
            state,
            live,
            refuse=frozenset({labels.blocked}),
            store=store,
        )

        outcome = runner.retry(ISSUE)

        assert outcome.status is OperatorCommandStatus.INCOMPLETE
        assert [entry.issue_number for entry in state.session_history] == [ISSUE]
        assert state.failed_this_cycle == {ISSUE}
        assert state.cached_scope_issues == [cached]
        assert store.mock_calls == []


class TestTheOutcomeSaysWhatHappened:
    """FACTS, not a response body (#6999 F6 round 7).

    The command reports what the transition did; how that is phrased and shaped
    belongs to whoever is talking to the operator. So these assert the typed
    fields, and the route tests assert the mapping - neither reaches into the
    other's business.
    """

    def test_a_refusal_names_the_label_and_who_is_holding_it(
        self, sample_config, state
    ):
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.needs_human}}
        _labels, runner = _runner(
            sample_config,
            state,
            live,
            block=_Block(
                labels.needs_human, live, held=(_Cause("claim_quarantine"),)
            ),
        )

        outcome = runner.retry(ISSUE)

        assert outcome.intent is OperatorCommandIntent.RETRY
        assert outcome.status is OperatorCommandStatus.STILL_BLOCKED
        assert outcome.issue_number == ISSUE
        assert outcome.blocked == labels.needs_human
        assert outcome.held_by == ("claim_quarantine",)

    def test_a_refusal_that_is_a_plain_write_failure_names_no_holder(
        self, sample_config, state
    ):
        """Nothing is holding it; the block simply did not come off.

        A distinct fact from the refusal above, and the transport says
        something different about each - so the outcome has to keep them apart.
        """
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.needs_human}}

        class _FailingBlock(_Block):
            def force_clear(self, target, reason):
                del target, reason
                return BlockOutcome.FAILED

        _labels, runner = _runner(
            sample_config, state, live, block=_FailingBlock(labels.needs_human, live)
        )

        outcome = runner.dismiss(ISSUE)

        assert outcome.intent is OperatorCommandIntent.DISMISS
        assert outcome.status is OperatorCommandStatus.STILL_BLOCKED
        assert outcome.blocked == labels.needs_human
        assert outcome.held_by == ()

    def test_a_committed_outcome_reports_what_came_off(self, sample_config, state):
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        _labels, runner = _runner(sample_config, state, live)

        outcome = runner.retry(ISSUE)

        assert outcome.status is OperatorCommandStatus.COMMITTED
        assert outcome.blocked is None
        assert outcome.failed == ()
        assert labels.needs_human in outcome.removed

    def test_the_outcome_carries_no_response_shape_at_all(
        self, sample_config, state
    ):
        """The dependency direction, pinned.

        A command that grows a ``payload()`` again has taken transport policy
        back into the core, and the port would once more be describing one
        concrete implementation rather than a contract.
        """
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.needs_human}}
        _labels, runner = _runner(sample_config, state, live)

        outcome = runner.retry(ISSUE)

        assert not hasattr(outcome, "payload")
        assert not hasattr(outcome, "message")


class TestARecoveredRetrySettlesTheCachedCopy:
    """The second attempt, after a partial failure (#6999 F7 round 8).

    Partial failure only became reachable in round 7, and it brought a second
    attempt with it. That attempt sees a DIFFERENT GitHub state from the first,
    so patching the cached copy with just its own removals is not the same as
    reconciling it - and the difference is a label the planner still gates on.
    """

    def _cached(self, labels, *extra):
        return Issue(
            number=ISSUE,
            title="Timed out issue",
            labels=("agent:web", labels.blocked, labels.blocked_failed, *extra),
        )

    def test_a_recovered_retry_leaves_no_stale_blocking_label_behind(
        self, sample_config, state
    ):
        """Remove one label, fail the other, recover - and the cache settles.

        Subtracting only the second attempt's removals from the first
        attempt's cache leaves ``blocked`` on the cached copy: gone from
        GitHub, still gating the planner, while the operator is told the issue
        was queued for retry.
        """
        from unittest.mock import MagicMock

        from issue_orchestrator.control.scheduler import Scheduler

        labels = LabelManager(sample_config)
        live = {ISSUE: {"agent:web", labels.blocked, labels.blocked_failed}}
        cached = self._cached(labels)
        state.cached_scope_issues = [cached]
        state.cached_queue_issues = []
        store = MagicMock()
        host = _RepositoryHost(live, refuse=frozenset({labels.blocked_failed}))
        _labels, runner = _runner(
            sample_config, state, live, store=store, host=host
        )

        # Attempt 1: one label comes off, GitHub refuses the other.
        first = runner.retry(ISSUE)

        assert first.status is OperatorCommandStatus.INCOMPLETE
        assert labels.blocked in first.removed
        assert labels.blocked_failed in first.failed
        assert live[ISSUE] == {"agent:web", labels.blocked_failed}
        assert state.cached_scope_issues == [cached], "nothing settled yet"

        # Attempt 2: GitHub recovers. The fresh read no longer mentions the
        # label attempt 1 already removed, so this attempt removes only one.
        host.refuse = frozenset()
        second = runner.retry(ISSUE)

        assert second.status is OperatorCommandStatus.COMMITTED
        assert second.removed == (labels.blocked_failed,)

        settled = next(
            issue for issue in state.cached_scope_issues if issue.number == ISSUE
        )
        assert labels.blocked not in settled.labels, (
            "removed from GitHub by the FIRST attempt, so it must not survive "
            "in the cache the planner reads"
        )
        assert labels.blocked_failed not in settled.labels
        assert "agent:web" in settled.labels, "non-gating labels are preserved"

        # The queue copy is the one the planner actually pulls from.
        queued = next(
            issue for issue in state.cached_queue_issues if issue.number == ISSUE
        )
        assert labels.blocked not in queued.labels

        # ...and so is the snapshot a warm restart would come back to.
        persisted = next(
            issue
            for issue in store.save_snapshot.call_args.args[0]
            if issue.number == ISSUE
        )
        assert labels.blocked not in persisted.labels

        # Finally the real arbiter: the scheduler must now let it through.
        decision = Scheduler(sample_config, label_manager=labels).evaluate_issues(
            [settled], check_dependencies=False
        )[0]
        assert decision.available, decision.reason

    def test_a_single_successful_retry_still_settles_the_same_way(
        self, sample_config, state
    ):
        """The one-attempt path, so the reconciliation is not just two-attempt.

        ``observed`` is authoritative for EVERY label, which also means a cache
        carrying a label GitHub no longer has is corrected rather than trusted.
        """
        from unittest.mock import MagicMock

        labels = LabelManager(sample_config)
        live = {ISSUE: {"agent:web", labels.blocked}}
        # The cache is stale: it still carries a label GitHub has already lost.
        state.cached_scope_issues = [self._cached(labels)]
        state.cached_queue_issues = []
        store = MagicMock()
        _labels, runner = _runner(sample_config, state, live, store=store)

        outcome = runner.retry(ISSUE)

        assert outcome.status is OperatorCommandStatus.COMMITTED
        settled = next(
            issue for issue in state.cached_scope_issues if issue.number == ISSUE
        )
        assert set(settled.labels) == {"agent:web"}
