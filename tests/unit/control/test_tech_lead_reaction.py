"""Tech-lead reaction classification tests (#6780)."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.tech_lead_reaction import (
    TechLeadReactionPolicy,
    record_completed_session_problem,
    storm_possible,
)
from issue_orchestrator.domain.models import (
    AgentConfig,
    DiscoveredFailure,
    Issue,
    OrchestratorState,
    PendingTechLeadReview,
    Session,
    SessionStatus,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot
from tests.unit.session_run_helpers import make_session_run_assets


class _IssueChecker:
    def __init__(self, states: dict[int, str]) -> None:
        self._states = states

    def get_dependency_issue_snapshot(
        self, issue_number: int, repo: str | None = None
    ) -> DependencyIssueSnapshot | None:
        del repo
        state = self._states.get(issue_number)
        if state is None:
            return None
        return DependencyIssueSnapshot(state=state, milestone="M1")


def _config(
    *,
    threshold: int = 3,
    window_minutes: int = 5,
    enabled: bool | None = None,
) -> Config:
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead.enabled = enabled
    config.tech_lead_review_on_failure = True
    config.tech_lead.health_review.storm_threshold = threshold
    config.tech_lead.health_review.storm_window_minutes = window_minutes
    return config


def _issue(number: int, *, body: str = "") -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        body=body,
        labels=["agent:test"],
        state="open",
        milestone="M1",
    )


def _blocked(
    number: int = 42,
    *,
    observed_at: float = 1_000.0,
    label: str = "",
    issue_body: str = "",
) -> DiscoveredFailure:
    return DiscoveredFailure(
        issue_number=number,
        issue_title=f"Issue {number}",
        failure_reason="blocked",
        observed_at=observed_at,
        blocking_label=label,
        issue_body=issue_body,
        issue_milestone="M1",
    )


def _failed(number: int, *, observed_at: float = 1_000.0) -> DiscoveredFailure:
    return DiscoveredFailure(
        issue_number=number,
        issue_title=f"Issue {number}",
        failure_reason="failed",
        observed_at=observed_at,
    )


def _policy(
    config: Config,
    *,
    dependency_states: dict[int, str] | None = None,
    now: float = 1_100.0,
) -> TechLeadReactionPolicy:
    evaluator = (
        DependencyEvaluator(
            issue_checker=_IssueChecker(dependency_states),
            events=Mock(),
        )
        if dependency_states is not None
        else None
    )
    return TechLeadReactionPolicy(
        config=config,
        labels=LabelManager(config),
        dependency_evaluator=evaluator,
        clock=lambda: now,
    )


def _snapshot(
    *,
    issues: tuple[Issue, ...] = (),
    problems: tuple[DiscoveredFailure, ...] = (),
    pending: tuple[PendingTechLeadReview, ...] = (),
) -> OrchestratorSnapshot:
    return OrchestratorSnapshot(
        issues=issues,
        active_sessions=(),
        pending_reviews=(),
        pending_reworks=(),
        discovered_failures=problems,
        pending_tech_lead=pending,
        paused=False,
    )


def test_unexplained_block_with_dependents_triggers_investigation() -> None:
    problem = _blocked()
    reaction = _policy(_config()).assess(
        _snapshot(
            issues=(
                _issue(42),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(problem,),
        )
    )

    assert reaction.investigations == (problem,)
    assert reaction.storm_problems == ()


def test_master_switch_prevents_failure_investigation() -> None:
    problem = _blocked()
    reaction = _policy(_config(enabled=False)).assess(
        _snapshot(
            issues=(
                _issue(42),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(problem,),
        )
    )

    assert reaction.investigations == ()
    assert reaction.storm_problems == ()


def test_dependency_satisfied_but_still_blocked_is_unexplained() -> None:
    problem = _blocked()
    reaction = _policy(_config(), dependency_states={7: "closed"}).assess(
        _snapshot(
            issues=(
                _issue(42, body="Depends-on: #7"),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(problem,),
        )
    )

    assert reaction.investigations == (problem,)


def test_open_tracked_dependency_explains_plain_block() -> None:
    reaction = _policy(_config(), dependency_states={7: "open"}).assess(
        _snapshot(
            issues=(
                _issue(42, body="Depends-on: #7"),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(_blocked(),),
        )
    )

    assert reaction.investigations == ()
    assert reaction.storm_problems == ()


def test_embedded_completion_dependency_facts_explain_block_when_issue_filtered() -> None:
    """The completed source issue need not remain in the available-work queue."""
    reaction = _policy(_config(), dependency_states={7: "open"}).assess(
        _snapshot(
            issues=(_issue(43, body="Depends-on: #42"),),
            problems=(_blocked(issue_body="Depends-on: #7"),),
        )
    )

    assert reaction.investigations == ()
    assert reaction.storm_problems == ()


def test_blocked_failed_label_is_unexplained_even_with_open_dependency() -> None:
    config = _config()
    problem = _blocked(label=LabelManager(config).blocked_failed.upper())
    reaction = _policy(config, dependency_states={7: "open"}).assess(
        _snapshot(
            issues=(
                _issue(42, body="Depends-on: #7"),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(problem,),
        )
    )

    assert reaction.investigations == (problem,)


def test_unexplained_block_without_dependents_does_not_spawn_investigation() -> None:
    reaction = _policy(_config()).assess(
        _snapshot(issues=(_issue(42),), problems=(_blocked(),))
    )

    assert reaction.investigations == ()
    # It earns no investigation, so it earns no cohort seat either. Counting it
    # would put a problem in a cohort that has nothing to suppress.
    assert reaction.storm_problems == ()


def test_leaf_blocks_never_form_a_storm() -> None:
    """A cohort must never contain a problem no investigation covers.

    Blocked-with-no-dependents issues are deliberately not investigated. If
    they still counted toward the threshold, a board of nothing but leaf blocks
    would escalate — and the escalation would collapse investigations that were
    never queued, dropping every member at the end-of-tick clear. Since they
    also never reach the pending queue, they cannot coalesce across ticks
    either, so such a cohort is unreachable by design rather than by luck.
    """
    reaction = _policy(_config(threshold=3)).assess(
        _snapshot(
            issues=(_issue(41), _issue(42), _issue(43)),
            problems=(
                _blocked(41),
                _blocked(42),
                _blocked(43),
            ),
        )
    )

    assert reaction.investigations == ()
    assert reaction.storm_problems == ()


def test_storm_cohort_is_covered_by_investigations() -> None:
    """Mixed board: only the problems an investigation covers form the cohort.

    Three real failures plus two leaf blocks must escalate as a cohort of
    exactly the three — the invariant every downstream collapse relies on.
    """
    reaction = _policy(_config(threshold=3)).assess(
        _snapshot(
            issues=(_issue(41), _issue(42), _issue(3), _issue(6), _issue(9)),
            problems=(
                _failed(3),
                _failed(6),
                _failed(9),
                _blocked(41),
                _blocked(42),
            ),
        )
    )

    assert tuple(p.issue_number for p in reaction.storm_problems) == (3, 6, 9)
    assert tuple(p.issue_number for p in reaction.investigations) == (3, 6, 9)
    assert reaction.storm_issue_numbers <= {
        p.issue_number for p in reaction.investigations
    }


def test_storm_threshold_zero_disables_escalation() -> None:
    """The documented ``storm_threshold: 0`` disable path.

    The ``threshold > 0`` half of the guard is load-bearing: weakened to
    ``>= 0``, every board would satisfy ``len(cohort) >= 0`` and escalate.
    """
    reaction = _policy(_config(threshold=0)).assess(
        _snapshot(
            issues=(_issue(3), _issue(6), _issue(9)),
            problems=(_failed(3), _failed(6), _failed(9)),
        )
    )

    assert reaction.storm_problems == ()
    assert tuple(p.issue_number for p in reaction.investigations) == (3, 6, 9)


def test_problem_storm_reports_cohort_and_individual_fallback() -> None:
    """A storm reports BOTH the cohort and the individual fallback the planner
    queues when the cohort cannot be escalated (#6780). Suppressing
    the individual investigations is the planner's decision — bound to actual
    cohort persistence — not the classifier's."""
    problems = (_failed(9), _failed(3), _blocked(6))
    reaction = _policy(_config(threshold=3)).assess(
        _snapshot(
            issues=(
                _issue(6),
                _issue(7, body="Depends-on: #6"),
            ),
            problems=problems,
        )
    )

    assert tuple(p.issue_number for p in reaction.storm_problems) == (3, 6, 9)
    assert tuple(p.issue_number for p in reaction.investigations) == (3, 6, 9)


def test_recent_queued_problems_join_new_discovery_across_ticks() -> None:
    queued = tuple(
        PendingTechLeadReview(
            issue_number=number,
            title=f"Investigate {number}",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=_failed(number, observed_at=1_000.0),
        )
        for number in (1, 2)
    )

    reaction = _policy(_config(threshold=3), now=1_100.0).assess(
        _snapshot(problems=(_failed(3, observed_at=1_090.0),), pending=queued)
    )

    # Already-queued members (1, 2) are not re-queued; only the newly
    # discovered #3 is in the individual fallback the planner would queue if
    # the cohort could not be escalated.
    assert tuple(p.issue_number for p in reaction.investigations) == (3,)
    assert tuple(p.issue_number for p in reaction.storm_problems) == (1, 2, 3)


def test_expired_problem_does_not_count_toward_storm() -> None:
    old = _failed(1, observed_at=700.0)
    recent = (_failed(2), _failed(3))
    reaction = _policy(_config(threshold=3), now=1_100.0).assess(
        _snapshot(problems=(old, *recent))
    )

    assert reaction.storm_problems == ()
    assert reaction.investigations == (old, *recent)


def test_legacy_pending_timestamp_cannot_make_storm_unbounded() -> None:
    legacy = _failed(1, observed_at=0.0)
    pending = PendingTechLeadReview(
        issue_number=1,
        title="Investigate 1",
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        failure=legacy,
    )
    reaction = _policy(_config(threshold=3)).assess(
        _snapshot(
            problems=(_failed(2), _failed(3)),
            pending=(pending,),
        )
    )

    assert reaction.storm_problems == ()
    assert tuple(p.issue_number for p in reaction.investigations) == (2, 3)


# ---------------------------------------------------------------------------
# Completion-side recording: which terminal outcomes become problem facts
# ---------------------------------------------------------------------------


def _worker_session(
    task: TaskKind, tmp_path: Path, *, agent_label: str = "agent:backend"
):
    return Session(
        key=SessionKey(issue=FakeIssueKey("42"), task=task),
        issue=Issue(
            number=42,
            title="Issue 42",
            # Issue.agent_type is derived from the agent:* label.
            labels=[agent_label],
            repo="test/repo",
            body="Some body",
            milestone="M1",
        ),
        agent_config=AgentConfig(prompt_path=tmp_path / "p.md", timeout_minutes=45),
        terminal_id="issue-42",
        worktree_path=tmp_path,
        branch_name="42-branch",
        run_assets=make_session_run_assets(tmp_path, session_name="issue-42"),
    )


def _record(session, status: SessionStatus) -> list[DiscoveredFailure]:
    recorded: list[DiscoveredFailure] = []
    record_completed_session_problem(
        status=status,
        session=session,
        tech_lead_agent="agent:tech-lead",
        blocking_label="blocked",
        artifact_hints=lambda: ("hint",),
        record=recorded.append,
        clock=lambda: 1_000.0,
    )
    return recorded


@pytest.mark.parametrize(
    "task",
    [TaskKind.CODE, TaskKind.REWORK, TaskKind.REVIEW, TaskKind.RETROSPECTIVE_REVIEW],
)
@pytest.mark.parametrize(
    "status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT, SessionStatus.BLOCKED]
)
def test_every_worker_task_kind_records_its_problem(task, status, tmp_path) -> None:
    """Task kind does not filter what counts as a problem.

    A failed rework session is a problem on the board exactly as a failed
    coding session is, and a rework agent reporting ``coding-done blocked`` is
    a trigger this reaction model exists to serve. Narrowing to CODE silently
    drops both, and also removes them from the health review's board context —
    while ``failed_this_cycle`` still counts them, so retry-suppression and
    tech-lead-reaction would disagree about what a failure is.
    """
    recorded = _record(_worker_session(task, tmp_path), status)

    assert [problem.issue_number for problem in recorded] == [42]
    assert recorded[0].failure_reason == status.value


def test_tech_lead_sessions_never_record_their_own_problems(tmp_path) -> None:
    """Self-recursion is prevented by the tech-lead-agent check, not task kind."""
    session = _worker_session(TaskKind.CODE, tmp_path, agent_label="agent:tech-lead")

    assert _record(session, SessionStatus.FAILED) == []


def test_successful_sessions_record_nothing(tmp_path) -> None:
    session = _worker_session(TaskKind.CODE, tmp_path)

    assert _record(session, SessionStatus.COMPLETED) == []


# ---------------------------------------------------------------------------
# storm_possible: the pure predicate that arms the anchor scan
# ---------------------------------------------------------------------------


def _state_with_problems(count: int) -> OrchestratorState:
    state = OrchestratorState()
    for number in range(41, 41 + count):
        state.record_discovered_failure(
            DiscoveredFailure(number, f"Problem {number}", "failed")
        )
    return state


def test_storm_possible_is_true_at_the_threshold() -> None:
    assert storm_possible(_state_with_problems(3), _config(threshold=3)) is True


def test_storm_possible_is_false_below_the_threshold() -> None:
    assert storm_possible(_state_with_problems(2), _config(threshold=3)) is False


def test_storm_possible_is_false_when_escalation_is_disabled() -> None:
    assert storm_possible(_state_with_problems(9), _config(threshold=0)) is False


def test_storm_possible_is_false_when_master_switch_is_disabled() -> None:
    assert storm_possible(_state_with_problems(9), _config(enabled=False)) is False


def test_storm_possible_counts_pending_investigations() -> None:
    """Pending investigations coalesce with fresh discoveries across ticks, so
    the predicate must see them or a coalescing storm arms no scan."""
    state = _state_with_problems(1)
    for number in (51, 52):
        state.pending_tech_lead_reviews.append(
            PendingTechLeadReview(
                issue_number=number,
                title=f"Investigate: {number}",
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                failure=DiscoveredFailure(number, f"Problem {number}", "failed"),
            )
        )

    assert storm_possible(state, _config(threshold=3)) is True


def test_storm_possible_ignores_problems_when_reaction_is_off() -> None:
    config = _config(threshold=3)
    config.tech_lead_review_on_failure = False

    assert storm_possible(_state_with_problems(5), config) is False
