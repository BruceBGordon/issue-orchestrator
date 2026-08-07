"""Tech-lead reaction policy for blocked/failed issue problems (#6780).

This is the single policy owner for the reactive side of ADR-0031:

* failed/timed-out sessions remain immediate investigation candidates;
* an explicit block is investigated only when dependency policy cannot
  explain it as healthy waiting on a tracked open issue, and only when the
  blocked issue has downstream dependents;
* a time-bounded cohort at or above the configured storm threshold suppresses
  per-issue investigations and requests one unscheduled health review. Cohort
  membership is a strict subset of the problems an investigation covers: a
  problem too minor to investigate individually cannot make the board look
  stormy, because there would be nothing for the escalation to suppress.

The classifier is pure and deterministic. It consumes the immutable planner
snapshot plus an injected clock and returns facts for the planner to map onto
actions. The completion-side helper records problem facts at the same policy
boundary. Queue mutation and GitHub issue creation stay at their existing
owner boundaries.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from ..domain.dependency_gates import Gate, GateBlockReason
from ..domain.models import DiscoveredFailure, Session, SessionStatus
from ..ports.provider_resilience import ProviderErrorType
from .dependency_gate_snapshot import build_successor_index
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports import Issue
    from .dependency_evaluator import DependencyEvaluator
    from .label_manager import LabelManager
    from .planner_types import OrchestratorSnapshot


class ProblemDisposition(enum.Enum):
    """What this tick does about one observed problem.

    The three buckets are exhaustive and decided in ONE pass, so cohort
    membership and investigation queuing can never drift apart the way two
    independently-ordered filters can.
    """

    IGNORED = "ignored"
    """Not acted on: a block dependency policy explains, or a block with no
    downstream dependents. Carries no carrier and therefore joins no cohort."""

    PENDING = "pending"
    """Already queued as an investigation. Needs no new request, but its queue
    entry is a carrier, so it may join a cohort."""

    INVESTIGATE = "investigate"
    """Queue a new individual investigation for it."""


@dataclass(frozen=True, slots=True)
class TechLeadReaction:
    """One tick's reaction facts for the planner to map onto actions.

    ``investigations`` is the individual failure-investigation set to queue;
    ``storm_problems`` is the time-bounded cohort when the storm threshold is
    met (empty otherwise) — the escalation candidate.

    Every cohort member is backed by a carrier: membership requires a
    disposition of INVESTIGATE (queued by this tick's plan) or PENDING (already
    queued). A problem nothing individually investigates cannot join a cohort,
    because a cohort is a SUPPRESSION of individual investigations — collapsing
    a member that has no investigation to suppress would drop the problem
    entirely once the tick-scoped discovered-fact buffer is cleared.

    When a storm is present BOTH collections are populated: the classifier does
    NOT decide suppression by zeroing ``investigations``. Retiring them belongs
    solely to the anchor-intake owner, which alone knows the cohort was actually
    persisted — a classifier that dropped them up front would lose the problems
    on every path where the anchor never lands.
    """

    investigations: tuple[DiscoveredFailure, ...] = ()
    storm_problems: tuple[DiscoveredFailure, ...] = ()

    @property
    def storm_issue_numbers(self) -> frozenset[int]:
        return frozenset(problem.issue_number for problem in self.storm_problems)


def storm_possible(state: "OrchestratorState", config: "Config") -> bool:
    """Over-approximate whether ``state`` could escalate a storm this tick.

    Pure over state and config, so fact gathering can arm the open-anchor scan
    BEFORE making any GitHub call — the scan is what makes anchor dedup work on
    a storm-only tick, where the periodic interval is never due.

    Deliberately an over-approximation: it skips the window, dependency and
    dependents filters :meth:`TechLeadReactionPolicy.assess` applies. Arming a
    scan for a storm that then fails to materialise costs one extra scan on a
    tick that already has problems on the board; failing to arm one mints a
    duplicate anchor.
    """
    if not (config.tech_lead_review_on_failure and config.tech_lead_review_agent):
        return False
    threshold = config.tech_lead.health_review.storm_threshold
    if threshold <= 0:
        return False
    candidates = {failure.issue_number for failure in state.discovered_failures}
    candidates.update(
        item.issue_number
        for item in state.pending_tech_lead_reviews
        if item.failure is not None
    )
    return len(candidates) >= threshold


@dataclass(frozen=True, slots=True)
class _ClassifiedProblem:
    """One problem's disposition plus whether it falls inside the storm window."""

    problem: DiscoveredFailure
    disposition: ProblemDisposition
    in_storm_window: bool

    @property
    def cohort_eligible(self) -> bool:
        """True only for a windowed problem that has an investigation carrier.

        This is the single place the cohort/investigation relationship is
        decided, which is what makes ``storm_problems`` a subset of "problems
        an investigation covers" by construction.
        """
        return self.in_storm_window and self.disposition in (
            ProblemDisposition.INVESTIGATE,
            ProblemDisposition.PENDING,
        )


def _sorted_problems(
    items: "Iterable[_ClassifiedProblem]",
) -> tuple[DiscoveredFailure, ...]:
    return tuple(
        sorted(
            (item.problem for item in items), key=lambda problem: problem.issue_number
        )
    )


_REACTIVE_SESSION_STATUSES = frozenset(
    (SessionStatus.FAILED, SessionStatus.TIMED_OUT, SessionStatus.BLOCKED)
)


def record_completed_session_problem(
    *,
    status: SessionStatus,
    session: Session,
    tech_lead_agent: str | None,
    blocking_label: str,
    artifact_hints: Callable[[], tuple[str, ...]],
    record: Callable[[DiscoveredFailure], None],
    clock: Callable[[], float] = time.time,
    provider_error_type: ProviderErrorType | None = None,
) -> None:
    """Record one reactive worker-session fact at the state owner boundary.

    Completion coordinates mechanics; this reaction owner decides which
    terminal outcomes are problem facts. Artifact gathering stays lazy so a
    successful or non-worker session performs no filesystem work.

    Task kind does NOT filter here: a failed rework or review session is a
    problem on the board exactly as a failed coding session is, and rework
    agents reporting ``coding-done blocked`` are a reaction trigger this model
    exists to serve. Tech Lead's own sessions are excluded below — that check, not
    task kind, is what prevents tech_lead self-recursion.

    A typed AUTH verdict is excluded: an unauthenticated provider says nothing
    about the issue, and on 2026-08-04 one expired login minted four failure
    investigations that each spent their budget reading the wrong code. Only a
    human re-authenticating clears it, and the circuit owner has already paused
    the fleet — an investigation adds load, not information (#6999).
    """
    if provider_error_type is ProviderErrorType.AUTH:
        return
    if status not in _REACTIVE_SESSION_STATUSES:
        return
    if is_tech_lead_session(tech_lead_agent, session.issue.agent_type):
        return
    record(
        DiscoveredFailure(
            session.issue.number,
            session.issue.title,
            status.value,
            artifact_hints=artifact_hints(),
            observed_at=clock(),
            blocking_label=blocking_label,
            issue_body=session.issue.body or "",
            issue_milestone=session.issue.milestone,
        )
    )


class TechLeadReactionPolicy:
    """Classify tech-lead-worthy problems without mutating queues or state."""

    def __init__(
        self,
        *,
        config: "Config",
        labels: "LabelManager",
        dependency_evaluator: "DependencyEvaluator | None",
        clock: Callable[[], float],
    ) -> None:
        self._config = config
        self._labels = labels
        self._dependency_evaluator = dependency_evaluator
        self._clock = clock

    def assess(self, snapshot: "OrchestratorSnapshot") -> TechLeadReaction:
        """Return the individual or storm reaction for ``snapshot``.

        Pending failure investigations participate in storm detection so
        adjacent ticks can coalesce before launch. They are not returned as
        new investigation requests; the pending-queue owner already holds
        them.
        """
        if (
            not self._config.tech_lead_review_on_failure
            or not self._config.tech_lead_review_agent
        ):
            return TechLeadReaction()

        classified = self._classify(snapshot)
        investigations = _sorted_problems(
            item
            for item in classified
            if item.disposition is ProblemDisposition.INVESTIGATE
        )
        cohort = _sorted_problems(item for item in classified if item.cohort_eligible)

        threshold = self._config.tech_lead.health_review.storm_threshold
        if threshold > 0 and len(cohort) >= threshold:
            # Report BOTH the cohort and the individual fallback. Suppressing
            # the individual investigations is the planner's decision, made at
            # the boundary that also knows whether the cohort was durably
            # persisted to a health-review anchor; a suppression not backed by
            # a persisted cohort would lose the problems at the end-of-tick
            # discovered-fact clear.
            return TechLeadReaction(
                investigations=investigations, storm_problems=cohort
            )
        return TechLeadReaction(investigations=investigations)

    def _classify(
        self, snapshot: "OrchestratorSnapshot"
    ) -> tuple["_ClassifiedProblem", ...]:
        """Bucket every observed problem exactly once."""
        now = self._clock()
        pending = {
            item.issue_number: item.failure
            for item in snapshot.pending_tech_lead
            if item.failure is not None
        }
        discovered = {
            problem.issue_number: problem
            for problem in snapshot.discovered_failures
        }
        problems = {**pending, **discovered}
        issue_by_number = {issue.number: issue for issue in snapshot.issues}
        successors = build_successor_index(snapshot.issues)
        already_queued = {item.issue_number for item in snapshot.pending_tech_lead}

        return tuple(
            _ClassifiedProblem(
                problem=problem,
                disposition=self._disposition(
                    problem,
                    issue=issue_by_number.get(problem.issue_number),
                    successors=successors,
                    already_queued=already_queued,
                ),
                # A zero timestamp is a compatibility value on legacy queue
                # entries. A problem discovered in THIS snapshot is current; an
                # already-pending legacy entry has no trustworthy observation
                # time and therefore cannot count toward a time-bounded storm.
                in_storm_window=self._inside_storm_window(
                    problem, now, observed_now=problem.issue_number in discovered
                ),
            )
            for problem in problems.values()
        )

    def _disposition(
        self,
        problem: DiscoveredFailure,
        *,
        issue: "Issue | None",
        successors: Mapping[int, object],
        already_queued: set[int],
    ) -> ProblemDisposition:
        is_block = problem.failure_reason == "blocked"
        if is_block and self._is_explained_block(problem, issue):
            return ProblemDisposition.IGNORED
        if problem.issue_number in already_queued:
            return ProblemDisposition.PENDING
        if is_block and not successors.get(problem.issue_number):
            # Nothing downstream is waiting, so this earns no investigation —
            # and therefore no cohort seat either. Were it counted toward a
            # storm, the escalation would collapse a problem that has no
            # investigation to collapse INTO, losing it at the end-of-tick
            # clear and making a leaf-only board escalate forever without ever
            # investigating anything.
            return ProblemDisposition.IGNORED
        return ProblemDisposition.INVESTIGATE

    def _is_explained_block(
        self, problem: DiscoveredFailure, issue: "Issue | None"
    ) -> bool:
        """True only for a plain block waiting on a tracked open dependency."""
        if (
            problem.blocking_label.casefold()
            == self._labels.blocked_failed.casefold()
        ):
            return False
        if self._dependency_evaluator is None:
            return False
        body = issue.body if issue is not None else problem.issue_body
        if not body:
            return False
        report = self._dependency_evaluator.evaluate_all_gates(
            problem.issue_number,
            body,
            issue.milestone if issue is not None else problem.issue_milestone,
        )
        return GateBlockReason.DEPENDENCY_OPEN in report.reason_codes(Gate.WORK)

    def _inside_storm_window(
        self,
        problem: DiscoveredFailure,
        now: float,
        *,
        observed_now: bool,
    ) -> bool:
        observed_at = problem.observed_at
        if observed_at <= 0:
            return observed_now
        window_seconds = (
            self._config.tech_lead.health_review.storm_window_minutes * 60
        )
        return observed_at <= now and now - observed_at <= window_seconds
