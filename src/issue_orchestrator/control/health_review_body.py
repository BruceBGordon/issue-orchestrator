"""What a health-review anchor issue SAYS to the tech lead (ADR-0031 §4).

Split out of ``health_review_trigger``, which decides WHEN a health review
fires, recovers pending anchors, and persists storm cohorts — a policy module
that had no business also holding two blocks of operator/agent prose. The same
separation ``tech_lead_gate_notes`` already has from the decision planner.

Both bodies are the session's briefing, not authority: what the tech lead is
actually allowed to do comes from ADR-0031's graduated authority and the launch
scope, never from this text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ..domain.models import DiscoveredFailure

PERIODIC_HEALTH_REVIEW_BODY = """## Periodic Health Review (ADR-0031 §4)

Walk the floor: review the orchestrator board holistically instead of
auditing a PR batch. Your session receives a board snapshot
(`tech-lead-data/board-snapshot.json`) with active sessions, pending/blocked
queues, recent failures, timeline extracts, and an orchestrator log tail.

Look for hung or aging sessions, queue pile-ups, repeated failures, and
cross-job patterns. Report findings and propose actions through the tech_lead
decision artifact; the orchestrator closes this issue when the review lands.
"""


def problem_storm_body(problems: Sequence["DiscoveredFailure"]) -> str:
    """Briefing for a storm review, naming the exact cohort that triggered it.

    The cohort is enumerated in the body because the session is authorized over
    THAT set of issues; a session told only "there was a storm" would have to
    re-derive its own scope from the broader board snapshot (#6780).
    """
    cohort = "\n".join(
        f"- #{problem.issue_number}: {problem.issue_title} "
        f"(`{problem.failure_reason}`)"
        for problem in problems
    )
    return f"""## Immediate Problem-Storm Health Review (ADR-0031)

The orchestrator observed {len(problems)} blocked/failed problem issues inside
the configured settle window and escalated them as one cohort instead of
launching per-issue investigations:

{cohort}

Walk the floor using `tech-lead-data/board-snapshot.json`. Diagnose shared root
causes and propose group remediation through the tech_lead decision artifact.
Each act-level proposal remains individually gated and re-validated.
"""
