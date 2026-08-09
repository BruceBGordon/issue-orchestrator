"""The LOCAL record of a tech-lead run — ADR-0033's visibility half (#6858).

:mod:`.tech_lead_run` names what a run IS (scope, request, admission verdict)
and the shared ledger coordinates who owns it. Neither remembers that a run
happened. That gap is the "invisible / black box" defect ADR-0033 names: a run
executes as a session on a bookkeeping anchor issue, and once the anchor closes
the only surviving trace of what the tech lead saw, decided, and filed is a
GitHub issue on the *client's* board.

ADR-0033 splits those two jobs by owner: coordination is shared (GitHub), the
run record is LOCAL to the engine that won the run. This module is the local
side's vocabulary — one record per run, referencing its subject rather than
being it, and pointing at the artifacts the run already produces (the session
run directory and its terminal recording, the same capture session-replay
reads) instead of copying them.

Why a record rather than "just read the session": a session is a *physical*
attempt at a run. Sessions are dropped from live state when they end, their
worktrees are disposable scratch (#6823), and a run can outlive several ticks
of queueing before one ever starts. The run is the durable thing an operator
asks about ("what did the last health review conclude?"), so it gets its own
identity, its own lifecycle, and its own home.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Optional

from .tech_lead_run import TechLeadRunScopeKind, scope_kind_of_run_key
from .tech_lead_run_artifacts import TechLeadRunArtifacts
from .tech_lead_session import TechLeadSessionFlavor


class TechLeadRunSubjectKind(str, Enum):
    """What a run is ABOUT — never what it was coordinated through (#6858 F5).

    The canonical scopes already say this: both global flavors report
    ``subject_issue_number is None`` because their subject is the board or the
    accumulated PR manifest. Recording ``session.issue`` for every flavor made a
    health review look like an investigation of its own bookkeeping anchor,
    which is precisely the coordination/visibility confusion ADR-0033 splits.
    So the subject is derived from the scope, and the anchor is recorded
    separately, as an anchor.
    """

    ISSUE = "issue"
    BOARD = "board"
    PR_MANIFEST = "pr_manifest"


# One scope → one subject. A table rather than a branch chain, so a new scope
# cannot be classified differently by two surfaces.
_SUBJECT_KIND_BY_SCOPE: dict[TechLeadRunScopeKind, TechLeadRunSubjectKind] = {
    TechLeadRunScopeKind.ISSUE: TechLeadRunSubjectKind.ISSUE,
    TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW: TechLeadRunSubjectKind.BOARD,
    TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW: TechLeadRunSubjectKind.PR_MANIFEST,
}


class TechLeadRunPhase(str, Enum):
    """Where one recorded run got to.

    Deliberately NOT ``SessionStatus``: that enum answers "what happened to a
    terminal process", and it carries states (``needs_validation_retry``,
    ``validation_failed``) that describe a coding agent's publish pipeline and
    mean nothing for an audit that files proposals. Projecting the session's
    terminal status onto these four is a mapping the recorder owns once, so the
    activity surface never has to explain a coding-shaped word to an operator
    reading about a health review.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    # The run reached a verdict, and the verdict is "a person has to look".
    # Kept distinct from FAILED because it is the tech lead working correctly:
    # folding the two would bury every genuine escalation in the failure pile,
    # which is the one place an operator stops reading.
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"
    # The run ended without a verdict of its own: the engine stopped it, a peer
    # took the identity, or its subject stopped being worth investigating.
    # Distinct from FAILED because nothing went wrong with the *work* — an
    # operator reading "failed" would go looking for a defect that is not there.
    WITHDRAWN = "withdrawn"

    @property
    def is_terminal(self) -> bool:
        """True once the run can no longer change."""
        return self is not TechLeadRunPhase.RUNNING


@dataclass(frozen=True, slots=True)
class TechLeadRunRecord:
    """One tech-lead run as the winning engine remembers it.

    ``run_key`` is the logical run identity the shared ledger coordinates on, so
    a record and the claim that authorised it can always be lined up. It is not
    the primary key here though: a withdrawn health review and the one that runs
    an hour later share ``global:health_review``. The identity of a *recorded*
    run is its session run — ``(run_id, session_name)`` — which is also exactly
    what the replay surface needs to find its artifacts.

    ``subject_issue_number`` is a REFERENCE, never ownership, and it belongs to
    the run's SCOPE rather than to the session that executed it: a focused
    investigation references the issue under investigation, and a whole-board
    review references no issue at all, because its subject is the board. The
    bookkeeping anchor a global run was coordinated THROUGH is recorded
    separately as ``anchor_issue_number`` — naming it as an anchor is what stops
    the shared coordination half masquerading as the local subject (#6858 F5),
    and it is what lets the anchor become optional later without the record
    losing its subject.
    """

    run_key: str
    scope_kind: TechLeadRunScopeKind
    flavor: TechLeadSessionFlavor
    phase: TechLeadRunPhase
    started_at: datetime
    # Drill-down identity. Both halves are required: the replay surface is keyed
    # by the pair, and a record that could not be drilled into would be a claim
    # about a run with no way to check it.
    run_id: str
    session_name: str
    # The issue this run is ABOUT: the focus issue of an investigation, and 0
    # for every whole-repository run, whose subject is not an issue.
    subject_issue_number: int = 0
    subject_title: str = ""
    # The GitHub object the run was COORDINATED through (0 when none). For a
    # focused investigation this is the same issue as the subject; for a global
    # run it is the bookkeeping anchor, which is not its subject.
    anchor_issue_number: int = 0
    ended_at: Optional[datetime] = None
    # Operator-facing sentence for the phase. Free prose, deliberately: it is
    # never branched on, only rendered.
    detail: str = ""
    # What the run produced, as counted from its own decision artifact.
    findings: int = 0
    proposals: int = 0
    # Where the run's PRESERVED artifacts are, once it has ended. ``None`` while
    # a run is still going, and also for a run whose artifacts could not be
    # preserved — the record then truthfully offers no drill-down rather than a
    # button pointing into a deleted scratch worktree (#6858 F4).
    artifacts: Optional[TechLeadRunArtifacts] = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.session_name:
            raise ValueError(
                "A tech-lead run record needs its session run identity"
                f" (run_id={self.run_id!r}, session_name={self.session_name!r}):"
                " without it the record cannot be drilled into, which is the"
                " only reason it is kept."
            )
        if scope_kind_of_run_key(self.run_key) is not self.scope_kind:
            raise ValueError(
                f"run key {self.run_key!r} does not name a"
                f" {self.scope_kind.value} run"
            )
        if self.subject_issue_number < 0 or self.anchor_issue_number < 0:
            raise ValueError(
                "issue references are positive numbers or 0, never negative;"
                f" got subject={self.subject_issue_number}"
                f" anchor={self.anchor_issue_number}"
            )
        # The scope decides what a subject may be. Without this a global run can
        # still be stored as if the anchor were its subject, which is the exact
        # confusion the anchor field exists to remove (#6858 F5).
        if self.subject_kind is TechLeadRunSubjectKind.ISSUE:
            if self.subject_issue_number <= 0:
                raise ValueError(
                    "a focused tech-lead investigation references the issue it"
                    " investigates; got no subject issue number"
                )
        elif self.subject_issue_number or self.subject_title:
            raise ValueError(
                f"a {self.subject_kind.value} run has no subject issue — its"
                " coordination anchor belongs in anchor_issue_number"
            )
        if self.phase.is_terminal and self.ended_at is None:
            raise ValueError(
                f"a {self.phase.value} tech-lead run must record when it ended"
            )
        if not self.phase.is_terminal and self.ended_at is not None:
            raise ValueError("a running tech-lead run has not ended yet")

    @property
    def subject_kind(self) -> TechLeadRunSubjectKind:
        """What this run is about, as its scope declares it.

        Derived rather than stored so a record can never carry a subject kind
        that disagrees with its own scope.
        """
        return _SUBJECT_KIND_BY_SCOPE[self.scope_kind]

    def concluded(
        self,
        *,
        phase: TechLeadRunPhase,
        ended_at: datetime,
        detail: str = "",
        findings: int = 0,
        proposals: int = 0,
        artifacts: Optional[TechLeadRunArtifacts] = None,
    ) -> "TechLeadRunRecord":
        """This run, as it looks once it has stopped.

        Rejects a non-terminal phase rather than storing "it ended, and it is
        still running": the store's conclusion path writes whatever this
        returns, so the invariant belongs on the transition, not on each caller.
        """
        if not phase.is_terminal:
            raise ValueError(
                f"{phase.value} is not a conclusion; a run concludes as"
                " completed, failed, or withdrawn"
            )
        return replace(
            self,
            phase=phase,
            ended_at=ended_at,
            detail=detail,
            findings=findings,
            proposals=proposals,
            artifacts=artifacts,
        )

    @property
    def duration_seconds(self) -> float:
        """How long the run took, or 0.0 while it is still going."""
        if self.ended_at is None:
            return 0.0
        return max(0.0, (self.ended_at - self.started_at).total_seconds())
