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
    OperatorCommandStatus,
    OperatorIssueCommandRunner,
)
from issue_orchestrator.control.operator_unblock import OperatorUnblocker
from issue_orchestrator.domain.models import OrchestratorState, SessionHistoryEntry

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


def _runner(sample_config, state, live, *, block=None, refuse=frozenset()):
    from unittest.mock import MagicMock

    labels = LabelManager(sample_config)
    host = _RepositoryHost(live, refuse=refuse)
    return labels, OperatorIssueCommandRunner(
        unblocker=OperatorUnblocker(
            repository_host=host,
            labels=labels,
            block=block or _Block(labels.needs_human, live),
        ),
        fresh_labels=_FreshLabels(live),
        config=sample_config,
        queue_cache_store=MagicMock(),
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

    def test_dismiss_tolerates_an_ordinary_label_it_cannot_remove(
        self, sample_config, state
    ):
        """A label that is already gone raises too.

        That is not a reason to refuse an operator asking for the issue to go
        away - only the shared block may stop this command.
        """
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        _labels, runner = _runner(
            sample_config, state, live, refuse=frozenset({labels.blocked})
        )

        outcome = runner.dismiss(ISSUE)

        assert outcome.committed
        assert state.session_history == []


class TestTheOutcomeSaysWhatHappened:
    """One typed result the transport maps, rather than a code it interprets."""

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

        body = runner.retry(ISSUE).payload()

        assert body["success"] is False
        assert body["failed_labels"] == [labels.needs_human]
        assert body["held_by"] == ["claim_quarantine"]
        assert "claim_quarantine still requires it" in body["error"]
        assert "was not retried" in body["error"]

    def test_a_refusal_that_is_a_plain_write_failure_says_so_instead(
        self, sample_config, state
    ):
        """No holder to name: the block simply did not come off."""
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.needs_human}}

        class _FailingBlock(_Block):
            def force_clear(self, target, reason):
                del target, reason
                return BlockOutcome.FAILED

        _labels, runner = _runner(
            sample_config, state, live, block=_FailingBlock(labels.needs_human, live)
        )

        body = runner.dismiss(ISSUE).payload()

        assert body["success"] is False
        assert body["held_by"] == []
        assert "could not be cleared" in body["error"]
        assert "was not dismissed" in body["error"]

    def test_the_committed_payload_tells_the_operator_what_came_off(
        self, sample_config, state
    ):
        labels = LabelManager(sample_config)
        live = {ISSUE: {labels.blocked, labels.needs_human}}
        _labels, runner = _runner(sample_config, state, live)

        body = runner.retry(ISSUE).payload()

        assert body["success"] is True
        assert "retry" in body["message"].lower()
        assert labels.needs_human in body["removed_labels"]
