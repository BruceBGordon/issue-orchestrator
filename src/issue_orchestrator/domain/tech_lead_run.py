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

The scopes are deliberately asymmetric, because the work is:

* :class:`GlobalHealthReviewScope` and :class:`GlobalBatchReviewScope` — the
  whole board (one walks the issue board, the other the accumulated PR
  manifest). Each is exclusive of every other tech-lead run, and once queued it
  is a barrier later work waits behind.
* :class:`IssueInvestigationScope` — one focus issue. Different issues may run
  concurrently, bounded only by the numeric tech-lead capacity that
  ``worker_budget.tech_lead_slot_availability`` owns.

The two GLOBAL scopes are separate identities rather than one "global" bucket
(#6994 round 1 F2). They audit different evidence and produce different verdicts,
so a health review must not swallow a batch-review request as a duplicate: they
COALESCE within a flavor and SERIALIZE across flavors. Collapsing them would make
one operator's request silently disappear into the other's run.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional, Union

from .tech_lead_session import TechLeadSessionFlavor

if TYPE_CHECKING:
    from .models import DiscoveredFailure


class TechLeadRunScopeKind(str, Enum):
    """The shapes a tech-lead run can have.

    ``is_global`` is asked here rather than by comparing against a list of
    global members at each call site, so adding a third whole-board flavor
    cannot leave one call site treating it as issue-scoped.
    """

    GLOBAL_HEALTH_REVIEW = "global_health_review"
    GLOBAL_BATCH_REVIEW = "global_batch_review"
    ISSUE = "issue"

    @property
    def is_global(self) -> bool:
        """True for every whole-repository scope."""
        return self is not TechLeadRunScopeKind.ISSUE


@dataclass(frozen=True, slots=True)
class GlobalHealthReviewScope:
    """Whole-board health review — exclusive of every other tech-lead run."""

    kind: TechLeadRunScopeKind = TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW

    @property
    def run_key(self) -> str:
        """Stable logical identity: repeated requests coalesce onto this key."""
        return "global:health_review"

    @property
    def flavor(self) -> TechLeadSessionFlavor:
        """The session variant this scope launches as."""
        return TechLeadSessionFlavor.HEALTH_REVIEW

    @property
    def subject_issue_number(self) -> Optional[int]:
        """A global run's subject is the board, not an issue."""
        return None


@dataclass(frozen=True, slots=True)
class GlobalBatchReviewScope:
    """Whole-repository audit of the accumulated PR manifest.

    Global for the same reason a health review is — it reasons about the
    repository as a whole, not one work item — but a DISTINCT identity, so a
    queued health review never absorbs a batch request (or the reverse).
    """

    kind: TechLeadRunScopeKind = TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW

    @property
    def run_key(self) -> str:
        return "global:batch_review"

    @property
    def flavor(self) -> TechLeadSessionFlavor:
        return TechLeadSessionFlavor.BATCH_REVIEW

    @property
    def subject_issue_number(self) -> Optional[int]:
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
    def flavor(self) -> TechLeadSessionFlavor:
        return TechLeadSessionFlavor.FAILURE_INVESTIGATION

    @property
    def subject_issue_number(self) -> Optional[int]:
        return self.issue_number


TechLeadRunScope = Union[
    GlobalHealthReviewScope, GlobalBatchReviewScope, IssueInvestigationScope
]

# The scope each session flavor runs at. One map, so a queued item, a restored
# session, and a fresh request can never be classified differently.
_SCOPE_BY_FLAVOR: dict[TechLeadSessionFlavor, TechLeadRunScopeKind] = {
    TechLeadSessionFlavor.HEALTH_REVIEW: TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
    TechLeadSessionFlavor.BATCH_REVIEW: TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW,
    TechLeadSessionFlavor.FAILURE_INVESTIGATION: TechLeadRunScopeKind.ISSUE,
}


def scope_kind_of_flavor(flavor: TechLeadSessionFlavor) -> TechLeadRunScopeKind:
    """The run scope a session flavor executes at.

    Fails loudly on an unmapped flavor rather than defaulting to global: a new
    variant silently inheriting whole-board exclusivity would deadlock every
    targeted run behind it.
    """
    try:
        return _SCOPE_BY_FLAVOR[flavor]
    except KeyError:  # pragma: no cover - guarded by the enum
        raise ValueError(
            f"No tech-lead run scope is declared for flavor {flavor!r}"
        ) from None


def scope_kind_of_run_key(run_key: str) -> TechLeadRunScopeKind:
    """The scope a run key names. Fails loudly on anything it does not name.

    Run keys travel through the shared coordination ledger, where a key and its
    declared scope arriving from a peer must be checked against each other — a
    ``global:health_review`` row declaring itself issue-scoped would be given
    the wrong exclusivity. This is also the one place ``release``-style callers,
    which hold a key and no scope value, recover the kind, so a key can never be
    classified two different ways (#6994 round 2 F7).
    """
    if run_key.startswith(_ISSUE_KEY_PREFIX):
        subject = run_key[len(_ISSUE_KEY_PREFIX) :]
        if subject.isdigit() and int(subject) > 0:
            return TechLeadRunScopeKind.ISSUE
        raise ValueError(f"run key {run_key!r} names no positive issue number")
    kind = _GLOBAL_KIND_BY_RUN_KEY.get(run_key)
    if kind is None:
        raise ValueError(f"run key {run_key!r} names no known tech-lead run scope")
    return kind


_ISSUE_KEY_PREFIX = "issue:"
_GLOBAL_KIND_BY_RUN_KEY: dict[str, TechLeadRunScopeKind] = {
    GlobalHealthReviewScope().run_key: TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
    GlobalBatchReviewScope().run_key: TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW,
}


def global_scope_for_flavor(flavor: TechLeadSessionFlavor) -> TechLeadRunScope:
    """The global scope value for a whole-repository flavor."""
    kind = scope_kind_of_flavor(flavor)
    if kind is TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW:
        return GlobalHealthReviewScope()
    if kind is TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW:
        return GlobalBatchReviewScope()
    raise ValueError(f"{flavor!r} is issue-scoped, not a whole-repository run")


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
        if self.failure is not None and self.scope.kind.is_global:
            raise ValueError(
                "A whole-repository review has no single triggering failure;"
                " pass the problem cohort through the anchor lifecycle instead."
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
    # The Repository Engine is not running at all, so nothing exists to admit
    # the request. Distinct from PAUSED (engine up, planning halted) and from
    # NOT_CONFIGURED (engine up, no tech lead agent): the three need three
    # different operator remedies, so they are three typed outcomes rather than
    # one untyped transport error (#6994 round 1 F5).
    NOT_RUNNING = "not_running"
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
REASON_ENGINE_NOT_RUNNING = "engine_not_running"
REASON_NO_TECH_LEAD_AGENT = "no_tech_lead_agent"
REASON_ISSUE_NOT_FOUND = "issue_not_found"
REASON_ISSUE_CLOSED = "issue_closed"
REASON_NO_LONGER_BLOCKED = "no_longer_blocked"
REASON_CLAIMED_BY_PEER = "claimed_by_peer"
REASON_ANCHOR_UNAVAILABLE = "anchor_unavailable"
# The whole-repository anchor this queued run points at has been CLOSED — the
# usual cause being that a peer engine completed the very same recovered run.
# The run is withdrawn: a closed anchor is a finished logical run, and starting
# a second session on it would duplicate the review (#6994 round 2 F9).
REASON_ANCHOR_CLOSED = "anchor_closed"
# The anchor could not be read at launch time. A global run fails CLOSED here
# rather than launching on ignorance: the cost of waiting a tick is one tick,
# whereas launching a duplicate whole-repository review is a duplicate audit.
REASON_ANCHOR_UNREADABLE = "anchor_unreadable"
# The shared run-claim store could not be reached, so ownership of the logical
# run could not be established. Admission fails CLOSED on this rather than
# guessing, because guessing is what creates the duplicate run.
REASON_RUN_CLAIM_UNAVAILABLE = "run_claim_unavailable"
# The requested whole-repository run has no anchor and none can be authored on
# demand (batch reviews are created by the PR-manifest threshold owner).
REASON_NO_ON_DEMAND_ANCHOR = "no_on_demand_anchor"

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

    @classmethod
    def engine_not_running(cls, scope: TechLeadRunScope, trigger: TechLeadRunTrigger
                           ) -> "TechLeadRunAdmission":
        """The verdict when no Repository Engine exists to admit the request.

        Built HERE rather than in the web handler so the "engine is down"
        answer is the same typed admission every other refusal is, and the
        route keeps exactly one response shape (#6994 round 1 F5).
        """
        return cls(
            outcome=TechLeadRunOutcome.NOT_RUNNING,
            scope_kind=scope.kind,
            run_key=scope.run_key,
            reason=REASON_ENGINE_NOT_RUNNING,
            detail="The Repository Engine is not running; start it and retry.",
            trigger=trigger,
            issue_number=scope.subject_issue_number,
        )
