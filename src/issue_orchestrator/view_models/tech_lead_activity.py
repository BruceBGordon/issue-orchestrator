"""Projection of the local tech-lead run history onto the dashboard (#6858).

ADR-0033's second surface. :mod:`.tech_lead_run_actions` projects what the
operator can DO right now; this projects what the tech lead has already done —
the "tech-lead activity" view the ADR names, and the thing that makes a run a
visible object rather than a session on a bookkeeping issue nobody reads.

Presentation only. Nothing here decides anything: the phases come from the
recorded run, and every string this emits is rendered verbatim so the surface
is legible without a colour key (the same rule the run-action affordances
follow). The subject is shown as a REFERENCE — "#123", a link out — never as
the run's own identity, because on a client's board that issue belongs to them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from ..domain.tech_lead_run_record import TechLeadRunPhase
from ..domain.tech_lead_session import TechLeadSessionFlavor

if TYPE_CHECKING:
    from ..domain.tech_lead_run_record import TechLeadRunRecord
    from ..ports.tech_lead_run_record_store import TechLeadRunHistoryReader


# How many runs the panel shows. Small on purpose: this is a "what has the tech
# lead been doing?" glance, not an audit log — the full history stays queryable
# in the local store.
ACTIVITY_LIMIT = 20

# Human names for the three run flavors. Here rather than in the browser so the
# vocabulary has one owner: a flavor renamed in the domain cannot leave the UI
# quietly showing the old word.
_FLAVOR_LABELS: dict[TechLeadSessionFlavor, str] = {
    TechLeadSessionFlavor.HEALTH_REVIEW: "Health review",
    TechLeadSessionFlavor.BATCH_REVIEW: "Batch review",
    TechLeadSessionFlavor.FAILURE_INVESTIGATION: "Failure investigation",
}

# Colour-independent phase words, per the project's rule that status must never
# be signalled by tint alone.
_PHASE_LABELS: dict[TechLeadRunPhase, str] = {
    TechLeadRunPhase.RUNNING: "Running",
    TechLeadRunPhase.COMPLETED: "Completed",
    TechLeadRunPhase.NEEDS_HUMAN: "Needs human",
    TechLeadRunPhase.FAILED: "Failed",
    TechLeadRunPhase.WITHDRAWN: "Withdrawn",
}

# The tone each phase renders with. A CLASS name, not a colour: the stylesheet
# owns the palette, and both themes get the same four semantic buckets.
_PHASE_TONES: dict[TechLeadRunPhase, str] = {
    TechLeadRunPhase.RUNNING: "active",
    TechLeadRunPhase.COMPLETED: "good",
    TechLeadRunPhase.NEEDS_HUMAN: "warn",
    TechLeadRunPhase.FAILED: "bad",
    TechLeadRunPhase.WITHDRAWN: "muted",
}


class TechLeadRunActivityEntry(BaseModel):
    """One recorded run, as the activity panel renders it."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    run_key: str = Field(serialization_alias="runKey")
    # "Health review" / "Batch review" / "Failure investigation".
    flavor_label: str = Field(serialization_alias="flavorLabel")
    phase: str
    phase_label: str = Field(serialization_alias="phaseLabel")
    # Semantic bucket for styling. Never the only signal — ``phase_label`` is
    # always rendered too.
    tone: str
    started_at: str = Field(serialization_alias="startedAt")
    # "" while the run is still going.
    ended_at: str = Field(serialization_alias="endedAt")
    # 0 when the run subject is the whole board and no anchor was involved.
    subject_issue_number: int = Field(serialization_alias="subjectIssueNumber")
    subject_title: str = Field(serialization_alias="subjectTitle")
    detail: str
    findings: int
    proposals: int
    # Drill-down identity: what the session-replay surface needs to find this
    # run's transcript. Published rather than reconstructed in the browser so
    # the UI never has to know how run artifacts are addressed.
    run_id: str = Field(serialization_alias="runId")
    session_name: str = Field(serialization_alias="sessionName")


class TechLeadActivityView(BaseModel):
    """The dashboard's tech-lead activity panel."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    entries: tuple[TechLeadRunActivityEntry, ...]
    # The sentence shown when there are none. Published rather than hardcoded in
    # the browser: "nothing has run yet" and "this engine has no history" read
    # the same to the UI but not to an operator.
    empty_message: str = Field(serialization_alias="emptyMessage")

    @classmethod
    def empty(cls) -> "TechLeadActivityView":
        return cls(entries=(), empty_message=EMPTY_MESSAGE)


EMPTY_MESSAGE = "No tech-lead runs recorded yet."


def read_tech_lead_activity(
    history: "TechLeadRunHistoryReader", *, limit: int = ACTIVITY_LIMIT
) -> TechLeadActivityView:
    """Project the local run history onto the activity panel."""
    entries = tuple(_entry(record) for record in history.recent(limit=limit))
    return TechLeadActivityView(entries=entries, empty_message=EMPTY_MESSAGE)


def _entry(record: "TechLeadRunRecord") -> TechLeadRunActivityEntry:
    return TechLeadRunActivityEntry(
        run_key=record.run_key,
        flavor_label=_FLAVOR_LABELS[record.flavor],
        phase=record.phase.value,
        phase_label=_PHASE_LABELS[record.phase],
        tone=_PHASE_TONES[record.phase],
        started_at=record.started_at.isoformat(),
        ended_at=record.ended_at.isoformat() if record.ended_at else "",
        subject_issue_number=record.subject_issue_number,
        subject_title=record.subject_title,
        detail=record.detail,
        findings=record.findings,
        proposals=record.proposals,
        run_id=record.run_id,
        session_name=record.session_name,
    )


__all__ = [
    "ACTIVITY_LIMIT",
    "EMPTY_MESSAGE",
    "TechLeadActivityView",
    "TechLeadRunActivityEntry",
    "read_tech_lead_activity",
]
