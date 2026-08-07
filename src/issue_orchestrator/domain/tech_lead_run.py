"""The vocabulary of a tech-lead RUN: its scope, its request, its verdict (#6994).

A tech-lead run is requested by many callers — the reactive failure model, the
periodic/storm health-review trigger, the stuck sweep, the one-shot CLI, and the
repository dashboard — and every one of them has to name the same things: what
SCOPE the work has, who asked, and what the answer was.

Those names are domain vocabulary, not policy, so they live here rather than
beside the coordinator that applies them
(:mod:`..control.tech_lead_run_admission`). Keeping them apart is what lets an
entrypoint, a view model, or a test speak about runs without importing the
admission machinery — and stops the policy owner from being the de-facto home
for every type the feature touches.

The two scopes are deliberately asymmetric, because the work is:

* :class:`GlobalHealthReviewScope` — the whole board. Exclusive of every other
  tech-lead run, and once queued it is a barrier later work waits behind.
* :class:`IssueInvestigationScope` — one focus issue. Different issues may run
  concurrently, bounded only by the numeric tech-lead capacity that
  ``worker_budget.tech_lead_slot_availability`` owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from .models import DiscoveredFailure


class TechLeadRunScopeKind(str, Enum):
    """The two shapes a tech-lead run can have."""

    GLOBAL_HEALTH_REVIEW = "global_health_review"
    ISSUE = "issue"


@dataclass(frozen=True, slots=True)
class GlobalHealthReviewScope:
    """Whole-board health review — exclusive of every other tech-lead run."""

    kind: TechLeadRunScopeKind = TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW

    @property
    def run_key(self) -> str:
        """Stable logical identity: repeated requests coalesce onto this key."""
        return "global:health_review"

    @property
    def subject_issue_number(self) -> Optional[int]:
        """A global run's subject is the board, not an issue."""
        return None


@dataclass(frozen=True, slots=True)
class IssueInvestigationScope:
    """Focused failure investigation of one issue."""

    issue_number: int
    kind: TechLeadRunScopeKind = TechLeadRunScopeKind.ISSUE

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError(
                f"IssueInvestigationScope needs a positive issue number, got"
                f" {self.issue_number}"
            )

    @property
    def run_key(self) -> str:
        return f"issue:{self.issue_number}"

    @property
    def subject_issue_number(self) -> Optional[int]:
        return self.issue_number


TechLeadRunScope = Union[GlobalHealthReviewScope, IssueInvestigationScope]


class TechLeadRunTrigger(str, Enum):
    """Who asked. Recorded for observability; it never changes the policy.

    Trigger-conditional admission is exactly the cross-path rule drift this
    owner exists to remove — a dashboard request and a reactive one are judged
    by the same matrix.
    """

    DASHBOARD = "dashboard"
    CLI = "cli"
    AUTOMATIC_FAILURE = "automatic_failure"
    PERIODIC_HEALTH_REVIEW = "periodic_health_review"
    STUCK_SWEEP = "stuck_sweep"


@dataclass(frozen=True, slots=True)
class TechLeadRunRequest:
    """One request for tech-lead work, from any trigger path.

    ``failure`` is the typed triggering context an automatic path already holds.
    It is also what makes an otherwise-unblocked issue investigation-worthy: the
    eligibility rule is "open AND (carries a blocking label OR arrives with
    failure context)", one rule for every trigger rather than a manual/automatic
    split. ``title`` overrides the fetched issue title when a caller already
    knows it.
    """

    scope: TechLeadRunScope
    trigger: TechLeadRunTrigger
    failure: Optional[DiscoveredFailure] = None
    title: str = ""

    def __post_init__(self) -> None:
        if self.failure is not None and not self.title:
            raise ValueError(
                "A tech-lead run request that supplies failure context must also"
                " supply the subject title: skipping the GitHub re-read is only"
                " safe when the caller already holds what the read would give."
            )
        if self.failure is not None and isinstance(
            self.scope, GlobalHealthReviewScope
        ):
            raise ValueError(
                "A global health review has no single triggering failure; pass"
                " the problem cohort through the anchor lifecycle instead."
            )


class TechLeadRunOutcome(str, Enum):
    """The discriminated result of an admission decision.

    Every value is truthful about what the operator's click actually achieved,
    so the UI never has to infer "did that work?" from an HTTP status alone.
    """

    QUEUED = "queued"
    ALREADY_QUEUED = "already_queued"
    ALREADY_RUNNING = "already_running"
    PAUSED = "paused"
    NOT_CONFIGURED = "not_configured"
    NOT_ELIGIBLE = "not_eligible"
    CLAIM_CONFLICT = "claim_conflict"
    FAILED = "failed"

    @property
    def admitted(self) -> bool:
        """True only when THIS request created the queued run."""
        return self is TechLeadRunOutcome.QUEUED

    @property
    def has_run(self) -> bool:
        """True when a run for this scope exists — newly queued or already."""
        return self in (
            TechLeadRunOutcome.QUEUED,
            TechLeadRunOutcome.ALREADY_QUEUED,
            TechLeadRunOutcome.ALREADY_RUNNING,
        )


# Machine-readable reason codes. Kept as constants (not free prose) because the
# UI and the API contract both branch on them.
REASON_ADMITTED = "admitted"
REASON_DUPLICATE_REQUEST = "duplicate_request"
REASON_RUN_ACTIVE = "run_active"
REASON_ORCHESTRATOR_PAUSED = "orchestrator_paused"
REASON_NO_TECH_LEAD_AGENT = "no_tech_lead_agent"
REASON_ISSUE_NOT_FOUND = "issue_not_found"
REASON_ISSUE_CLOSED = "issue_closed"
REASON_NO_LONGER_BLOCKED = "no_longer_blocked"
REASON_CLAIMED_BY_PEER = "claimed_by_peer"
REASON_ANCHOR_UNAVAILABLE = "anchor_unavailable"

# Why a queued run is not launching this tick (launch_gate).
BARRIER_GLOBAL_RUN_ACTIVE = "global_run_active"
BARRIER_GLOBAL_RUN_QUEUED = "global_run_queued"
BARRIER_GLOBAL_AWAITING_DRAIN = "global_run_awaiting_drain"


@dataclass(frozen=True, slots=True)
class TechLeadRunAdmission:
    """What happened to one :class:`TechLeadRunRequest`.

    ``run_key`` is the logical run identity — the same value for every request
    that coalesces onto one run — so a caller can correlate repeated clicks,
    automatic triggers, and the eventual session without inventing its own key.
    """

    outcome: TechLeadRunOutcome
    scope_kind: TechLeadRunScopeKind
    run_key: str
    reason: str
    detail: str
    trigger: TechLeadRunTrigger
    issue_number: Optional[int] = None
    # True when the admitted/existing run cannot start until a global run
    # completes. Only meaningful alongside an outcome in ``has_run``.
    behind_global_barrier: bool = False
