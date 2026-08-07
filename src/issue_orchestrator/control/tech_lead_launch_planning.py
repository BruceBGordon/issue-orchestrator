"""When a QUEUED tech-lead run may actually launch (#6994).

Admission (:mod:`.tech_lead_run_admission`) answers "should this run exist?".
This module answers the different question a TICK asks: of the runs that already
exist, which may start right now, and which of them should no longer exist at
all. The two are separated because a run can be admitted once and then wait many
ticks — behind the global barrier, behind capacity, behind an open provider
circuit — and the board moves underneath it in that window. Admitting a run is
never a standing licence to launch it.

Two rules live here, both consulted by
:func:`..control.reactive_tech_lead_planning.plan_tech_lead_launch_queue`:

* :func:`plan_tech_lead_launch_gate` — scope exclusivity. A global run is
  exclusive of every other tech-lead run, and a QUEUED one is a barrier.
* :func:`plan_tech_lead_launch_revalidation` — subject eligibility, re-asked
  against this tick's live evidence, so a run whose subject was closed or
  unblocked while it waited is withdrawn rather than launched.

They are free functions, not coordinator methods, so the planner can consult the
rules without constructing an admission coordinator — while there is still only
one implementation of each.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from ..domain.models import PendingTechLeadReview
from ..domain.tech_lead_run import (
    BARRIER_GLOBAL_AWAITING_DRAIN,
    BARRIER_GLOBAL_RUN_ACTIVE,
    BARRIER_GLOBAL_RUN_QUEUED,
    REASON_ISSUE_CLOSED,
    REASON_NO_LONGER_BLOCKED,
)
from .tech_lead_run_admission import (
    active_tech_lead_sessions,
    has_active_global_run,
    is_global_pending,
)

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..infra.config import Config
    from ..ports import Issue


@dataclass(frozen=True, slots=True)
class TechLeadLaunchGate:
    """Which queued runs the scope matrix allows to launch this tick.

    ``held`` is never silently empty-handed: whenever anything is withheld,
    ``barrier_reason`` says which rule withheld it, so the launch log and the
    dashboard can explain a queued-but-idle run instead of showing a stall.
    """

    launchable: tuple[PendingTechLeadReview, ...]
    held: tuple[PendingTechLeadReview, ...]
    barrier_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if bool(self.held) != bool(self.barrier_reason):
            raise ValueError(
                "TechLeadLaunchGate: barrier_reason must be set iff runs are held"
                f" (held={len(self.held)}, reason={self.barrier_reason!r})"
            )


def plan_tech_lead_launch_gate(
    config: "Config",
    pending: "Sequence[PendingTechLeadReview]",
    active_sessions: "Sequence[Session]",
) -> TechLeadLaunchGate:
    """The scope-exclusivity gate over a tick's queued tech-lead runs.

    The three rules, in the order they apply:

    1. A queued global run is a BARRIER. Nothing else launches while it is
       queued, and the global run itself waits until every active tech-lead
       session has drained — that is what makes it exclusive rather than merely
       first in line.
    2. An ACTIVE global run holds everything back until it completes.
    3. Otherwise every queued targeted run is launchable; the numeric budget
       (``worker_budget.tech_lead_slot_availability``) slices it downstream,
       which is exactly why no capacity arithmetic happens here.

    A free function so the planner can consult the rule without constructing an
    admission coordinator — there is still only ONE implementation of it, which
    :meth:`TechLeadRunCoordinator.launch_gate` also delegates to.
    """
    items = tuple(pending)
    if not items:
        return TechLeadLaunchGate((), ())

    global_queued = tuple(item for item in items if is_global_pending(item))
    if global_queued:
        if active_tech_lead_sessions(config, active_sessions):
            return TechLeadLaunchGate((), items, BARRIER_GLOBAL_AWAITING_DRAIN)
        first = global_queued[0]
        held = tuple(item for item in items if item is not first)
        return TechLeadLaunchGate(
            (first,), held, BARRIER_GLOBAL_RUN_QUEUED if held else None
        )
    if has_active_global_run(config, active_sessions):
        return TechLeadLaunchGate((), items, BARRIER_GLOBAL_RUN_ACTIVE)
    return TechLeadLaunchGate(items, ())


# ----------------------------------------------------------------------
# Subject eligibility — one rule, applied at request time AND before launch
# ----------------------------------------------------------------------


def issue_run_eligibility(
    issue: "Issue", blocking_label: str
) -> Optional[tuple[str, str]]:
    """Is this issue still worth a tech-lead investigation? None when yes.

    The rule: the issue must be OPEN and must still carry a blocking label.
    Returned as a ``(reason_code, detail)`` pair so both callers report the same
    machine-readable refusal.

    It is deliberately module-level, because it is asked TWICE about the same
    logical run: once by :meth:`TechLeadRunCoordinator.admit` when the request
    arrives, and again by :func:`plan_tech_lead_launch_revalidation` immediately
    before the queued run would launch. A run can sit queued for many ticks
    behind the global barrier, and in that window its subject can be closed or
    unblocked by a human — so admitting a run is never a standing licence to
    launch it. ``blocking_label`` is the label the caller already resolved:
    classification happens ONCE, so the verdict and the evidence-map context can
    never disagree about which label blocked it.
    """
    lifecycle = (getattr(issue, "state", "") or "").casefold()
    if lifecycle and lifecycle != "open":
        return (
            REASON_ISSUE_CLOSED,
            f"Issue #{issue.number} is closed; nothing to investigate.",
        )
    if not blocking_label:
        return (
            REASON_NO_LONGER_BLOCKED,
            f"Issue #{issue.number} is no longer blocked; nothing to investigate.",
        )
    return None


@dataclass(frozen=True, slots=True)
class TechLeadRunWithdrawal:
    """A queued run whose subject stopped being worth investigating."""

    item: PendingTechLeadReview
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class TechLeadRevalidation:
    """Which queued runs survived launch-time revalidation, and which did not."""

    still_eligible: tuple[PendingTechLeadReview, ...]
    withdrawn: tuple[TechLeadRunWithdrawal, ...]


def plan_tech_lead_launch_revalidation(
    pending: "Sequence[PendingTechLeadReview]",
    board: "Sequence[Issue]",
    is_blocking_any: "Callable[[Sequence[str]], bool]",
    subjects: "Sequence[Issue]" = (),
) -> TechLeadRevalidation:
    """Re-check every queued INVESTIGATION against this tick's live evidence.

    Two evidence sources, in order:

    * ``board`` — the issues the tick already fetched, so a subject still on the
      board costs no extra GitHub call no matter how long its run waits behind
      the global barrier;
    * ``subjects`` — the AUTHORITATIVE lifecycle reads the fact gatherer makes
      for queued subjects the board did not carry
      (:meth:`FactGatherer.gather_tech_lead_subject_facts`). Without them the
      closed-while-queued rule was unreachable in production: the board fetch
      asks GitHub only for OPEN issues, so a subject closed while queued came
      back ABSENT rather than ``state="closed"`` (#6994 round 1 F4).

    Only POSITIVE evidence withdraws a run. The board is filtered — by agent
    label, milestone, and ``filtering.exclude_labels``, which ``tech_lead
    .inherit_labels`` deliberately re-admits for tech-lead work — so a subject
    that is absent from BOTH sources proves nothing and its run is kept.
    Withdrawing on absence would silently cancel legitimate investigations of
    every issue the board filter happens not to carry, and would turn a
    transient GitHub read failure into a cancelled run.

    Global runs are never subject to this: a health-review anchor is not a
    blocked work item, and blocked-label eligibility says nothing about whether
    the board is still worth auditing.
    """
    by_number: dict[int, "Issue"] = {issue.number: issue for issue in subjects}
    by_number.update({issue.number: issue for issue in board})
    eligible: list[PendingTechLeadReview] = []
    withdrawn: list[TechLeadRunWithdrawal] = []
    for item in pending:
        issue = (
            None if is_global_pending(item) else by_number.get(item.issue_number)
        )
        if issue is None:
            eligible.append(item)
            continue
        blocking = next(
            (name for name in issue.labels if is_blocking_any([name])), ""
        )
        verdict = issue_run_eligibility(issue, blocking)
        if verdict is None:
            eligible.append(item)
        else:
            withdrawn.append(TechLeadRunWithdrawal(item, verdict[0], verdict[1]))
    return TechLeadRevalidation(tuple(eligible), tuple(withdrawn))
