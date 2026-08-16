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

from typing import TYPE_CHECKING, Union

from pydantic import BaseModel, ConfigDict, Field

from ..domain.tech_lead_run_artifacts import TechLeadRunArtifactKind
from ..domain.tech_lead_run_record import TechLeadRunPhase, TechLeadRunSubjectKind
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .lifecycle_semantics import (
    OpenReviewArtifactCommand,
    OpenSessionRecordingCommand,
)

if TYPE_CHECKING:
    from ..domain.tech_lead_run_artifacts import TechLeadRunArtifacts
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
# owns the palette, and both themes get the same semantic buckets.
_PHASE_TONES: dict[TechLeadRunPhase, str] = {
    TechLeadRunPhase.RUNNING: "active",
    TechLeadRunPhase.COMPLETED: "good",
    TechLeadRunPhase.NEEDS_HUMAN: "warn",
    TechLeadRunPhase.FAILED: "bad",
    TechLeadRunPhase.WITHDRAWN: "muted",
}


# What a whole-repository run's subject is CALLED. A global run has no subject
# issue (#6858 F5), so without a word for the board and the PR manifest the panel
# would either show nothing or show the coordination anchor as if it were the
# subject — which is the confusion the record was fixed to stop.
_SUBJECT_LABELS: dict[TechLeadRunSubjectKind, str] = {
    TechLeadRunSubjectKind.BOARD: "Whole board",
    TechLeadRunSubjectKind.PR_MANIFEST: "PR manifest",
}

# Operator-facing name for each drill-down. Server-owned for the same reason
# phase words are: the browser must not invent a second vocabulary.
_ARTIFACT_LABELS: dict[TechLeadRunArtifactKind, str] = {
    TechLeadRunArtifactKind.SESSION_REPLAY: "Session replay",
    TechLeadRunArtifactKind.REPORT: "Report",
    TechLeadRunArtifactKind.DECISION: "Decision",
}

# Why a run offers no drill-down. Two different facts, so two sentences: one is
# "not yet", the other is "never", and an operator needs to tell them apart.
ARTIFACTS_PENDING_NOTE = "Artifacts are preserved when the run ends."
ARTIFACTS_ABSENT_NOTE = "No artifacts were preserved for this run."

# The commands a recorded run publishes. Deliberately the EXISTING lifecycle
# inspection commands rather than a tech-lead-shaped copy of them: the dashboard
# already has one dispatcher for "open this run's recording / artifact", and a
# parallel vocabulary would mean a second dispatcher and a second place for the
# artifact-read contract to drift (#6858 round 1 A1/F4).
TechLeadRunArtifactCommand = Union[
    OpenSessionRecordingCommand, OpenReviewArtifactCommand
]


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
    # What the run is ABOUT: "issue" / "board" / "pr_manifest".
    subject_kind: str = Field(serialization_alias="subjectKind")
    # The rendered subject — "#42 Flaky merge queue", "Whole board", "PR
    # manifest". One sentence per subject kind, owned here so the browser cannot
    # start calling a board review an investigation of its anchor.
    subject_label: str = Field(serialization_alias="subjectLabel")
    # 0 for every whole-repository run: its subject is not an issue (#6858 F5).
    subject_issue_number: int = Field(serialization_alias="subjectIssueNumber")
    subject_title: str = Field(serialization_alias="subjectTitle")
    # The bookkeeping issue the run was COORDINATED through (0 when none).
    # Published separately from the subject so an operator can still reach the
    # anchor without the panel claiming the run was about it.
    anchor_issue_number: int = Field(serialization_alias="anchorIssueNumber")
    detail: str
    findings: int
    proposals: int
    # Drill-down identity: what the session-replay surface needs to find this
    # run's transcript. Published rather than reconstructed in the browser so
    # the UI never has to know how run artifacts are addressed.
    run_id: str = Field(serialization_alias="runId")
    session_name: str = Field(serialization_alias="sessionName")
    # The inspections available for this run, in one fixed order. Empty when the
    # run is still going or preserved nothing.
    artifacts: tuple[TechLeadRunArtifactCommand, ...]
    # Why there are none ("" when there are some).
    artifacts_note: str = Field(serialization_alias="artifactsNote")


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
    commands = _artifact_commands(record)
    return TechLeadRunActivityEntry(
        run_key=record.run_key,
        flavor_label=_FLAVOR_LABELS[record.flavor],
        phase=record.phase.value,
        phase_label=_PHASE_LABELS[record.phase],
        tone=_PHASE_TONES[record.phase],
        started_at=record.started_at.isoformat(),
        ended_at=record.ended_at.isoformat() if record.ended_at else "",
        subject_kind=record.subject_kind.value,
        subject_label=_subject_label(record),
        subject_issue_number=record.subject_issue_number,
        subject_title=record.subject_title,
        anchor_issue_number=record.anchor_issue_number,
        detail=record.detail,
        findings=record.findings,
        proposals=record.proposals,
        run_id=record.run_id,
        session_name=record.session_name,
        artifacts=commands,
        artifacts_note=_artifacts_note(record, commands),
    )


def _subject_label(record: "TechLeadRunRecord") -> str:
    """What this run is about, in words.

    A focused investigation names its issue as a REFERENCE ("#42 …"); a global
    run names the board or the manifest, because that is its subject and its
    anchor is only how it was coordinated.
    """
    if record.subject_kind is TechLeadRunSubjectKind.ISSUE:
        title = f" {record.subject_title}" if record.subject_title else ""
        return f"#{record.subject_issue_number}{title}"
    return _SUBJECT_LABELS[record.subject_kind]


def _artifact_commands(
    record: "TechLeadRunRecord",
) -> tuple[TechLeadRunArtifactCommand, ...]:
    """The inspections this run's PRESERVED artifacts support.

    Driven entirely by what the archive actually filed: a run whose decision was
    never written offers no Decision button, so the panel cannot present an
    action that 404s.
    """
    artifacts: "TechLeadRunArtifacts | None" = record.artifacts
    if artifacts is None:
        return ()
    issue_number = record.subject_issue_number or record.anchor_issue_number
    run_dir = str(artifacts.location)
    return tuple(
        _command_for(kind, issue_number=issue_number, run_dir=run_dir)
        for kind in artifacts.kinds
    )


def _command_for(
    kind: TechLeadRunArtifactKind, *, issue_number: int, run_dir: str
) -> TechLeadRunArtifactCommand:
    """The typed inspection command for one preserved artifact kind.

    ``artifact_path`` is run-RELATIVE and comes from the domain's kind→member
    table, so the run-scoped artifact reader resolves and contains it against the
    preserved directory rather than trusting a path the browser assembled.
    """
    if kind is TechLeadRunArtifactKind.REPORT:
        return OpenReviewArtifactCommand(
            label=_ARTIFACT_LABELS[kind],
            issue_number=issue_number,
            run_dir=run_dir,
            artifact_path=str(kind.member),
            artifact_type="tech_lead_report",
            render_mode="markdown",
        )
    if kind is TechLeadRunArtifactKind.DECISION:
        return OpenReviewArtifactCommand(
            label=_ARTIFACT_LABELS[kind],
            issue_number=issue_number,
            run_dir=run_dir,
            artifact_path=str(kind.member),
            artifact_type="tech_lead_decision",
            render_mode="json",
        )
    return OpenSessionRecordingCommand(
        label=_ARTIFACT_LABELS[kind],
        issue_number=issue_number,
        run_dir=run_dir,
    )


def _artifacts_note(
    record: "TechLeadRunRecord",
    commands: tuple[TechLeadRunArtifactCommand, ...],
) -> str:
    if commands:
        return ""
    return (
        ARTIFACTS_ABSENT_NOTE if record.phase.is_terminal else ARTIFACTS_PENDING_NOTE
    )


__all__ = [
    "ACTIVITY_LIMIT",
    "ARTIFACTS_ABSENT_NOTE",
    "ARTIFACTS_PENDING_NOTE",
    "EMPTY_MESSAGE",
    "TechLeadActivityView",
    "TechLeadRunActivityEntry",
    "TechLeadRunArtifactCommand",
    "read_tech_lead_activity",
]
