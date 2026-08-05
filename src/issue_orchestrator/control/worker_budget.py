"""Worker-slot accounting — the single owner of "which active sessions count
against the worker budget (``max_concurrent_sessions``)".

Two seams must agree on this rule or they drift (cross-path rule drift):

* the planner's ``_launch_budgets`` computes remaining worker capacity, and
* the orchestrator's E2E start-gate asks "is a worker slot free?" before it
  lets a first-class E2E run claim one.

The rule: the tech lead draws from its own reserved additive budget
when ``tech_lead.max_concurrent`` is set, so its active sessions are NOT charged
to the worker budget; otherwise (the shared-budget default) every active
session counts. E2E, by contrast, is a WORKER workload — it is accounted here,
against ``max_concurrent_sessions``, never against the tech_lead reserved slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..infra.config import Config


@dataclass(frozen=True)
class TechLeadSlotAvailability:
    """How many slots the tech lead has THIS tick, and — when none — the true
    reason it was deferred.

    Produced by this module (the single slot-accounting owner) so the reason can
    never drift from the capacity math: the two are computed from the same
    inputs in one place, instead of being reconstructed downstream from loose
    primitive facts (#6892 review A2). ``reason`` is set iff ``available == 0``.
    """

    available: int
    reason: str | None

    def __post_init__(self) -> None:
        if (self.available > 0) == (self.reason is not None):
            raise ValueError(
                "TechLeadSlotAvailability: reason must be set iff available == 0"
                f" (available={self.available}, reason={self.reason!r})"
            )


def tech_lead_slot_availability(
    config: "Config",
    active_sessions: "Sequence[Session]",
    *,
    e2e_occupies_slot: bool,
    launched_this_tick: int,
    workflow_configured: bool,
) -> TechLeadSlotAvailability:
    """Tech-lead slot budget for this tick, with the true deferral reason.

    ``launched_this_tick`` is the count of capacity-consuming worker launches
    already planned this tick (LaunchSessionActions only — never provider-skip
    label actions), so a higher-priority launch consuming the last shared slot
    is distinguished from pre-existing saturation. ``active`` worker/tech-lead
    counts are derived HERE from ``active_sessions``, independent of any E2E
    charge, so E2E occupancy is never misattributed as worker saturation.
    """
    if not workflow_configured:
        return TechLeadSlotAvailability(0, "tech_lead_workflow_unavailable")

    reserved = config.tech_lead.max_concurrent
    if reserved is not None:
        active_tl = active_tech_lead_session_count(config, active_sessions)
        available = reserved - active_tl
        if available > 0:
            return TechLeadSlotAvailability(available, None)
        return TechLeadSlotAvailability(
            0, f"reserved_slot_occupied:max_concurrent={reserved},active_tech_lead={active_tl}"
        )

    # Shared worker budget: tech_lead competes for max_concurrent_sessions.
    max_sessions = config.max_concurrent_sessions
    active_worker = active_worker_session_count(config, active_sessions)
    available = max_sessions - active_worker - (1 if e2e_occupies_slot else 0) - launched_this_tick
    if available > 0:
        return TechLeadSlotAvailability(available, None)
    if active_worker >= max_sessions:
        return TechLeadSlotAvailability(
            0, f"worker_slot_occupied:active={active_worker},max={max_sessions}"
        )
    if e2e_occupies_slot:
        return TechLeadSlotAvailability(0, f"e2e_occupies_worker_slot:max={max_sessions}")
    if launched_this_tick > 0:
        return TechLeadSlotAvailability(
            0, f"higher_priority_launched_this_tick:launched={launched_this_tick},max={max_sessions}"
        )
    # Unreachable under valid planner invariants: available <= 0 with no worker,
    # no E2E, and nothing launched is impossible. Fail fast rather than emit a
    # false "no_worker_capacity:active=0" (the original F1 lie) (#6892 review A2).
    raise AssertionError(
        "tech_lead shared-slot deferral with no consumer"
        f" (active_worker={active_worker}, e2e={e2e_occupies_slot},"
        f" launched_this_tick={launched_this_tick}, max={max_sessions})"
    )


def active_tech_lead_session_count(
    config: "Config", active_sessions: "Sequence[Session]"
) -> int:
    """Number of active sessions launched under the configured tech lead agent.

    Tech Lead identity is the ADR-0031 owner rule (agent label == the configured
    ``tech_lead_review_agent``); both tech_lead variants launch as ``issue-{N}``
    sessions under that agent, so the agent label is what distinguishes them.
    """
    return sum(
        1
        for session in active_sessions
        if is_tech_lead_session(config.tech_lead_review_agent, session.agent_label)
    )


def active_worker_session_count(
    config: "Config", active_sessions: "Sequence[Session]"
) -> int:
    """Active sessions charged against ``max_concurrent_sessions``.

    Equals ``len(active_sessions)`` in the shared-budget default (unchanged);
    with a reserved tech_lead budget the tech-lead sessions are additive and
    excluded so they never steal worker slots.
    """
    if config.tech_lead.max_concurrent is None:
        return len(active_sessions)
    return len(active_sessions) - active_tech_lead_session_count(config, active_sessions)


def worker_slot_free(
    config: "Config", active_sessions: "Sequence[Session]"
) -> bool:
    """Whether at least one worker slot is unoccupied right now.

    Uses the SAME accounting as the planner so the E2E start-gate competes for
    the worker budget, not the tech-lead's reserved slot.
    """
    return (
        active_worker_session_count(config, active_sessions)
        < config.max_concurrent_sessions
    )
