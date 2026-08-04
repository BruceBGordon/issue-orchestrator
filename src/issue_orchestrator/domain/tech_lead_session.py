"""Tech Lead session flavor, assignment, and launch authority (ADR-0031).

Three tech_lead variants share one launch path: batch PR review (audit the
orchestrator-prepared PR manifest), failure investigation (diagnose one
failed issue), and the periodic health review (walk the board snapshot,
ADR-0031 §4). The :class:`TechLeadAssignment` written at launch tells the
*agent* which variant its session is (all variants run in ``issue-{N}``
terminals).

Trust boundary: the assignment file and the PR manifest live inside the
agent-writable worktree, so completion must never treat them as orchestrator
authority — an agent could rewrite them mid-session. The
:class:`TechLeadLaunchAuthority` record captures the same launch scope
(flavor, focus, manifest PR set, anchor) for persistence in an
orchestrator-owned store outside the worktree; completion reads THAT record
as authority and treats the worktree copies as the agent's reading material
only (tamper evidence when they diverge).
"""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Collection, cast

from .tech_lead_artifacts import ACT_LEVEL_TECH_LEAD_ACTIONS

TECH_LEAD_ASSIGNMENT_FILENAME = "tech-lead-assignment.json"

# Marker label carried by health-review anchor issues (ADR-0031 §4).
# Labels are crash-safe truth (ADR-0013): the marker is both how the launcher
# derives the HEALTH_REVIEW flavor and how the fact gatherer deduplicates an
# already-open health-review anchor. Single owner — the planner, launcher,
# fact gatherer, and startup recovery all import it from here.
HEALTH_REVIEW_MARKER_LABEL = "tech_lead:health-review"

# Gate label carried by gated tech_lead proposal issues (#6778, ADR-0031 §2
# amendment). Orchestrator-attached at creation; REMOVING it is per-instance
# operator approval. The scheduler's blocking-label classification excludes
# gate-labeled issues from pickup, and the agent-label allowlist rejects it
# as a protected workflow label. Raw (never prefixed), like the marker label:
# the tech_lead subsystem manages its labels without the orchestrator prefix.
PROPOSED_TECH_LEAD_LABEL = "proposed-tech-lead"


def is_proposed_tech_lead_gate(name: str) -> bool:
    """True iff *name* is the owned proposal gate, case-insensitively (#6779 R15).

    GitHub folds label names, so a repository whose canonical spelling is
    ``Proposed-Tech-Lead`` still carries the gate. This is the SINGLE owner of the
    gate comparison: reconciliation classification, scheduler blocking, and the
    apply-time consent re-check all fold case through here so they can never
    diverge — e.g. classify a canonical-cased gate as "approved" while blocking
    treats it as absent (or vice versa).
    """
    return name.casefold() == PROPOSED_TECH_LEAD_LABEL.casefold()


# Observation label carried by pattern case-file issues (#6781). Mirrors
# PROPOSED_TECH_LEAD_LABEL's treatment exactly: orchestrator-attached at
# creation, blocking-class (excluded from agent pickup by the scheduler),
# rejected as a protected workflow label when agent-proposed, and raw
# (never prefixed). Case files are durable flag_pattern evidence ledgers —
# never work items and never tech_lead anchors.
TECH_LEAD_OBSERVATION_LABEL = "tech-lead-observation"

# Area tag prefix on case-file issues (#6781 amendment): the optional
# ``area`` a flag_pattern proposal carries becomes an ``area:<name>`` label
# so evidence clusters are queryable across signatures from the anchor scan.
TECH_LEAD_AREA_LABEL_PREFIX = "area:"


def tech_lead_area_from_labels(labels: Collection[str]) -> str:
    """Return the first ``area:*`` label value, or ``""`` when absent.

    The case-file classifier, merge-history recorder, and board projection all
    need the same area/seam interpretation. Keep it here beside the label
    prefix so those paths cannot drift on casing or malformed empty values.
    """
    for label in labels:
        if label.casefold().startswith(TECH_LEAD_AREA_LABEL_PREFIX.casefold()):
            return label[len(TECH_LEAD_AREA_LABEL_PREFIX):]
    return ""


def is_tech_lead_observation_label(name: str) -> bool:
    """True iff *name* is the owned observation label, case-insensitively."""
    return name.casefold() == TECH_LEAD_OBSERVATION_LABEL.casefold()


def require_case_file_observation_label(labels: Collection[str]) -> None:
    """Enforce the case-file label invariant (#6781): the domain owns it.

    A pattern case-file issue MUST carry the orchestrator-owned observation
    label — without it the issue would be schedulable agent work rather than
    an evidence ledger. Co-located with ``TECH_LEAD_OBSERVATION_LABEL`` so the
    action layer delegates label semantics to the domain owner instead of
    re-deciding them at a control branch site.
    """
    if not any(is_tech_lead_observation_label(label) for label in labels):
        raise ValueError(
            "pattern case file must carry the"
            f" {TECH_LEAD_OBSERVATION_LABEL!r} observation label"
        )


_SCHEMA_VERSION = 1


class TechLeadSessionFlavor(str, Enum):
    """Which kind of tech_lead work a session was launched to do."""

    BATCH_REVIEW = "batch_review"
    FAILURE_INVESTIGATION = "failure_investigation"
    HEALTH_REVIEW = "health_review"


class TechLeadCreationKind(str, Enum):
    """Why a tech-lead-authored issue is being created.

    The two kinds have OPPOSITE reconciliation duties, which is the whole reason
    they are named rather than inferred:

    * ``AUTHORS_ANCHOR`` — the planner's batch tracking issue and the
      health-review anchor. These are the FIRST issue of their session; there is
      no prior issue whose pause label could gate them, so they legitimately
      have no reconciliation subject and no expectations.
    * ``DERIVED_FROM_ANCHOR`` — a follow-up issue, a gated proposal, or a
      pattern case file, all decided BY a tech-lead session working an anchor.
      Each must reconcile against that anchor before it writes: an anchor paused
      behind ``io:needs-reconcile`` must not spawn new issues.
    """

    AUTHORS_ANCHOR = "authors_anchor"
    DERIVED_FROM_ANCHOR = "derived_from_anchor"


@dataclass(frozen=True, slots=True)
class TechLeadCreationOrigin:
    """What a tech-lead issue creation was decided from (#6957 R2 review F6/A6).

    Replaces a pair of defaulted fields — ``anchor_issue_number: int = 0`` plus
    an inherited ``expected=None`` — whose simultaneous absence was being read as
    authority. Composition that dropped both produced a follow-up creation that
    looked exactly like legitimate anchor authoring and wrote UNGUARDED. Two
    defaults cannot express intent; this can.

    The valid states are exactly two, and ``__post_init__`` admits no others:

    * ``(AUTHORS_ANCHOR, no subject)`` — nothing to reconcile against;
    * ``(DERIVED_FROM_ANCHOR, positive subject)`` — reconcile against it.

    ``requires_expected_state`` carries the other half of the invariant to the
    command that owns it: a derived creation without an ``ExpectedState`` would
    cross the gate as a no-op, so the command rejects that combination too.
    """

    kind: TechLeadCreationKind
    anchor_issue_number: int = 0

    def __post_init__(self) -> None:
        if self.kind is TechLeadCreationKind.AUTHORS_ANCHOR:
            if self.anchor_issue_number:
                raise ValueError(
                    "a tech-lead creation that AUTHORS its anchor has no prior"
                    " issue to reconcile against; it must not name one, got"
                    f" #{self.anchor_issue_number}"
                )
            return
        if self.anchor_issue_number <= 0:
            raise ValueError(
                "a tech-lead creation derived from an anchor requires that"
                " anchor's positive issue number — it is the issue whose pause"
                f" label gates the creation, got {self.anchor_issue_number!r}"
            )

    @classmethod
    def authors_anchor(cls) -> "TechLeadCreationOrigin":
        """The batch/health-review anchor this session is being created FOR."""
        return cls(kind=TechLeadCreationKind.AUTHORS_ANCHOR)

    @classmethod
    def derived_from_anchor(cls, anchor_issue_number: int) -> "TechLeadCreationOrigin":
        """A creation decided BY a session working *anchor_issue_number*."""
        return cls(
            kind=TechLeadCreationKind.DERIVED_FROM_ANCHOR,
            anchor_issue_number=anchor_issue_number,
        )

    @property
    def authors_new_anchor(self) -> bool:
        return self.kind is TechLeadCreationKind.AUTHORS_ANCHOR

    @property
    def reconciliation_subject(self) -> int:
        """The managed-repo issue whose labels gate this creation (0 = none)."""
        return self.anchor_issue_number

    @property
    def requires_expected_state(self) -> bool:
        """True when the creation must carry expectations to check at the gate."""
        return not self.authors_new_anchor


@dataclass(frozen=True)
class TechLeadAssignment:
    """Launch-time record of a tech_lead session's assignment.

    ``focus_issue_number``/``focus_reason`` name the single issue a
    failure-investigation session must diagnose; batch and health reviews
    carry neither (their scope is the PR manifest / the board snapshot).
    """

    flavor: TechLeadSessionFlavor
    focus_issue_number: int | None = None
    focus_reason: str = ""
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported tech_lead assignment schema_version: {self.schema_version!r}"
            )
        if (
            self.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
            and self.focus_issue_number is None
        ):
            raise ValueError(
                "TechLeadAssignment with flavor=failure_investigation requires "
                "focus_issue_number"
            )

    def to_dict(self) -> dict[str, object]:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "flavor": self.flavor.value,
            "focus_issue_number": self.focus_issue_number,
            "focus_reason": self.focus_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TechLeadAssignment":
        """Parse from dict; malformed content fails loudly with ValueError."""
        raw_flavor = data.get("flavor")
        try:
            flavor = TechLeadSessionFlavor(raw_flavor)
        except ValueError:
            raise ValueError(
                f"Unknown tech_lead assignment flavor: {raw_flavor!r}"
            ) from None
        raw_schema = data.get("schema_version")
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise ValueError(
                f"tech_lead assignment schema_version must be an int, got {raw_schema!r}"
            )
        focus_issue_number = data.get("focus_issue_number")
        if focus_issue_number is not None and (
            isinstance(focus_issue_number, bool)
            or not isinstance(focus_issue_number, int)
        ):
            raise ValueError(
                "tech_lead assignment focus_issue_number must be an int or null, "
                f"got {focus_issue_number!r}"
            )
        focus_reason = data.get("focus_reason", "")
        if not isinstance(focus_reason, str):
            raise ValueError(
                f"tech_lead assignment focus_reason must be a string, got {focus_reason!r}"
            )
        return cls(
            flavor=flavor,
            focus_issue_number=focus_issue_number,
            focus_reason=focus_reason,
            schema_version=raw_schema,
        )

    def write(self, path: Path) -> None:
        """Write assignment to file, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> "TechLeadAssignment":
        """Read assignment from file; malformed content raises ValueError."""
        return cls.from_dict(json.loads(path.read_text()))


@dataclass(frozen=True, slots=True)
class TechLeadLaunchScope:
    """What the PRODUCER boundary grants one tech_lead session run (#6780).

    The queued item knows which variant it is and — for a problem storm —
    exactly which issues the review owns. This value object carries that
    grant across the launch command boundary (queue -> routing -> launcher ->
    ``prepare_tech_lead_session_data``) so the authority record is built from the
    OWNED cohort rather than inferred downstream.

    It exists because the board snapshot is the wrong place to infer authority
    from: that surface merges the live failure buffer, every pending failure
    investigation, and every pending health review's cohort, so deriving
    ``problem_issue_numbers`` from it silently widened a review's act-level
    scope to unrelated issues that merely happened to be failing at launch
    — and handed a PERIODIC review act-level scope it should never
    have.

    Issue numbers, not ``DiscoveredFailure`` objects: a scope conveys
    AUTHORITY (which issues may be acted on). The failure detail those issues
    carry is board CONTEXT, and travels in the snapshot.
    """

    flavor: TechLeadSessionFlavor
    problem_issue_numbers: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.problem_issue_numbers
            and self.flavor is not TechLeadSessionFlavor.HEALTH_REVIEW
        ):
            raise ValueError(
                "TechLeadLaunchScope problem_issue_numbers are valid only for a "
                "health review; other flavors derive scope from their focus "
                "issue or PR manifest"
            )
        if any(
            isinstance(number, bool) or number <= 0
            for number in self.problem_issue_numbers
        ):
            raise ValueError(
                "TechLeadLaunchScope problem_issue_numbers must contain positive ints"
            )
        if self.problem_issue_numbers != tuple(
            sorted(set(self.problem_issue_numbers))
        ):
            raise ValueError(
                "TechLeadLaunchScope problem_issue_numbers must be sorted and unique"
            )


@dataclass(frozen=True)
class TechLeadLaunchAuthority:
    """Orchestrator-owned launch scope for one tech_lead session run.

    Persisted OUTSIDE the agent-writable worktree at launch time and read
    back at completion as the sole authority for the session's flavor, focus
    issue, manifest PR set, and anchor issue. Completion effects (labels,
    close, decision-target scope) key off this record; the worktree copies
    exist only for the agent to read.
    """

    flavor: TechLeadSessionFlavor
    anchor_issue_number: int
    focus_issue_number: int | None = None
    manifest_pr_numbers: tuple[int, ...] = ()
    # The health review's OWNED problem cohort (#6780), recorded from the
    # producer's ``TechLeadLaunchScope`` grant (or the durable cohort ledger for
    # an anchor launched outside the pending queue) — never inferred from the
    # board snapshot, whose failure list is deliberately broader context.
    # Immutable act-level authority, not agent-provided scope.
    problem_issue_numbers: tuple[int, ...] = ()
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported tech_lead authority schema_version: {self.schema_version!r}"
            )
        if (
            self.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
            and self.focus_issue_number is None
        ):
            raise ValueError(
                "TechLeadLaunchAuthority with flavor=failure_investigation requires "
                "focus_issue_number"
            )
        if self.flavor is TechLeadSessionFlavor.HEALTH_REVIEW and (
            self.focus_issue_number is not None or self.manifest_pr_numbers
        ):
            raise ValueError(
                "TechLeadLaunchAuthority with flavor=health_review carries no "
                "focus issue or manifest PRs; its general scope is the anchor "
                "and its act-level scope is the problem cohort it was launched "
                "owning"
            )
        if (
            self.flavor is not TechLeadSessionFlavor.HEALTH_REVIEW
            and self.problem_issue_numbers
        ):
            raise ValueError(
                "TechLeadLaunchAuthority problem_issue_numbers are valid only "
                "for a health review"
            )
        if any(
            isinstance(number, bool) or number <= 0
            for number in self.problem_issue_numbers
        ):
            raise ValueError(
                "TechLeadLaunchAuthority problem_issue_numbers must contain "
                "positive ints"
            )
        if self.problem_issue_numbers != tuple(sorted(set(self.problem_issue_numbers))):
            raise ValueError(
                "TechLeadLaunchAuthority problem_issue_numbers must be sorted "
                "and unique"
            )

    def allowed_targets(self) -> frozenset[int]:
        """Issue/PR numbers a decision from this session may target.

        Failure investigations may only address their focus issue; health
        reviews may only address their anchor issue (the report's home,
        ADR-0031 §4); batch reviews may address the audited manifest PRs
        plus the anchor tracking issue. ``create_issue``/``flag_pattern``
        proposals carry no target and are scope-free by construction.
        """
        if self.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
            assert self.focus_issue_number is not None  # __post_init__
            return frozenset((self.focus_issue_number,))
        if self.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
            return frozenset((self.anchor_issue_number,))
        return frozenset((*self.manifest_pr_numbers, self.anchor_issue_number))

    def allowed_act_level_targets(self) -> frozenset[int]:
        """Issue numbers an ACT-LEVEL proposal (reset_retry/kill_hung_session)
        may target — a STRICTER scope than :meth:`allowed_targets`.

        Act-level intents mutate a work ISSUE's runtime (scratch reset, session
        kill). The issue reset owner is handed this number as an
        ``issue_number``, so a batch manifest PR number — or a tech_lead
        bookkeeping anchor — passed here is a confused deputy: it resets the
        wrong entity (#6764 re-review F1). Only a failure investigation owns a
        work issue in scope: its focus issue. A health review additionally owns
        the problem cohort it was LAUNCHED owning — the storm the anchor was
        created for, carried here from the launch grant (#6780) — enabling
        group diagnosis with individually gated and execution-time re-validated
        resets. That cohort is empty for a periodic review, which therefore
        owns no act-level target at all. Batch reviews own no resettable work
        issue because manifest entries are PRs and their anchor is bookkeeping.
        """
        if self.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
            assert self.focus_issue_number is not None  # __post_init__
            return frozenset((self.focus_issue_number,))
        if self.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
            return frozenset(self.problem_issue_numbers)
        return frozenset()

    def matches_assignment(self, assignment: TechLeadAssignment) -> bool:
        """True when the agent-visible assignment copy mirrors this authority."""
        return (
            assignment.flavor is self.flavor
            and assignment.focus_issue_number == self.focus_issue_number
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "flavor": self.flavor.value,
            "anchor_issue_number": self.anchor_issue_number,
            "focus_issue_number": self.focus_issue_number,
            "manifest_pr_numbers": list(self.manifest_pr_numbers),
            "problem_issue_numbers": list(self.problem_issue_numbers),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TechLeadLaunchAuthority":
        """Parse from dict; malformed content fails loudly with ValueError."""
        raw_flavor = data.get("flavor")
        try:
            flavor = TechLeadSessionFlavor(raw_flavor)
        except ValueError:
            raise ValueError(
                f"Unknown tech_lead authority flavor: {raw_flavor!r}"
            ) from None
        raw_schema = data.get("schema_version")
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise ValueError(
                f"tech_lead authority schema_version must be an int, got {raw_schema!r}"
            )
        anchor = data.get("anchor_issue_number")
        if isinstance(anchor, bool) or not isinstance(anchor, int):
            raise ValueError(
                f"tech_lead authority anchor_issue_number must be an int, got {anchor!r}"
            )
        focus = data.get("focus_issue_number")
        if focus is not None and (isinstance(focus, bool) or not isinstance(focus, int)):
            raise ValueError(
                "tech_lead authority focus_issue_number must be an int or null, "
                f"got {focus!r}"
            )
        raw_prs = data.get("manifest_pr_numbers", [])
        if not isinstance(raw_prs, list) or any(
            isinstance(pr, bool) or not isinstance(pr, int) for pr in raw_prs
        ):
            raise ValueError(
                f"tech_lead authority manifest_pr_numbers must be a list of ints, got {raw_prs!r}"
            )
        raw_problems = data.get("problem_issue_numbers", [])
        if not isinstance(raw_problems, list) or any(
            isinstance(number, bool) or not isinstance(number, int)
            for number in raw_problems
        ):
            raise ValueError(
                "tech_lead authority problem_issue_numbers must be a list of ints, "
                f"got {raw_problems!r}"
            )
        return cls(
            flavor=flavor,
            anchor_issue_number=anchor,
            focus_issue_number=focus,
            manifest_pr_numbers=tuple(raw_prs),
            problem_issue_numbers=tuple(raw_problems),
            schema_version=raw_schema,
        )


@dataclass(frozen=True)
class StoredTechLeadOp:
    """Orchestrator-recorded executable payload of a gated tech_lead proposal.

    Recorded create-once in the orchestrator-owned authority store when a
    gated proposal issue is created (#6778): the GitHub issue body is human
    documentation ONLY and is never re-parsed as a command. What the approver
    read and delabeled is exactly what runs — execution consumes THIS record.
    """

    op_type: str  # one of ACT_LEVEL_TECH_LEAD_ACTIONS
    target_issue_number: int
    rationale: str
    source_run_id: str
    source_session_name: str
    source_action_id: str  # the decision artifact action id (A<n>)
    created_at: str  # ISO-8601 UTC timestamp
    # The target issue's ACTIVE session run id captured at proposal time
    # (#6779 R1). ``kill_hung_session`` consents to terminating exactly THAT
    # generation: the kill executor refuses to act unless the target issue's
    # live session still carries this run id, so a replacement session that
    # started before approval is never killed. Empty for ``reset_retry`` —
    # that op is stale-checked by labels/no-active-session, never bound to a
    # specific generation (a non-empty value there is a bug).
    target_session_id: str = ""
    # The decision findings the approver saw for this op (#6779 R6): forwarded
    # into ``TECH_LEAD_ACTION_EXECUTED`` so execution correlates to those findings.
    finding_ids: tuple[str, ...] = ()
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported tech_lead op schema_version: {self.schema_version!r}"
            )
        if self.op_type not in ACT_LEVEL_TECH_LEAD_ACTIONS:
            raise ValueError(
                f"StoredTechLeadOp op_type must be one of"
                f" {sorted(ACT_LEVEL_TECH_LEAD_ACTIONS)}, got {self.op_type!r}"
            )
        # Runtime re-checks: from_dict feeds this dataclass persisted JSON,
        # so the declared annotations carry no runtime guarantee here.
        target = cast(object, self.target_issue_number)
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise ValueError(
                "StoredTechLeadOp target_issue_number must be a positive int,"
                f" got {target!r}"
            )
        for field_name in (
            "source_run_id",
            "source_session_name",
            "source_action_id",
            "created_at",
        ):
            value: object = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"StoredTechLeadOp {field_name} must be a non-empty string,"
                    f" got {value!r}"
                )
        rationale = cast(object, self.rationale)
        if not isinstance(rationale, str):
            raise ValueError(
                f"StoredTechLeadOp rationale must be a string, got {rationale!r}"
            )
        session_id = cast(object, self.target_session_id)
        if not isinstance(session_id, str):
            raise ValueError(
                "StoredTechLeadOp target_session_id must be a string,"
                f" got {session_id!r}"
            )
        if self.op_type == "reset_retry" and session_id.strip():
            raise ValueError(
                "StoredTechLeadOp target_session_id must be empty for reset_retry;"
                " that op is never bound to a specific session generation,"
                f" got {session_id!r}"
            )
        findings = cast(object, self.finding_ids)
        if not isinstance(findings, tuple) or any(
            not isinstance(item, str) for item in findings
        ):
            raise ValueError(
                "StoredTechLeadOp finding_ids must be a tuple of strings,"
                f" got {findings!r}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "op_type": self.op_type,
            "target_issue_number": self.target_issue_number,
            "rationale": self.rationale,
            "source_run_id": self.source_run_id,
            "source_session_name": self.source_session_name,
            "source_action_id": self.source_action_id,
            "created_at": self.created_at,
            "target_session_id": self.target_session_id,
            "finding_ids": list(self.finding_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StoredTechLeadOp":
        """Parse from dict; malformed content fails loudly with ValueError.

        The store is orchestrator-owned, so corruption is a bug, never agent
        input to fail-safe around (mirrors TechLeadLaunchAuthority.from_dict).
        """
        raw_schema = data.get("schema_version")
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise ValueError(
                f"tech_lead op schema_version must be an int, got {raw_schema!r}"
            )
        raw_findings = data.get("finding_ids", [])
        if not isinstance(raw_findings, list):
            raise ValueError(
                f"tech_lead op finding_ids must be a list, got {raw_findings!r}"
            )
        return cls(
            op_type=str(data.get("op_type")),
            target_issue_number=data.get("target_issue_number"),  # type: ignore[arg-type]
            rationale=str(data.get("rationale", "")),
            source_run_id=str(data.get("source_run_id", "")),
            source_session_name=str(data.get("source_session_name", "")),
            source_action_id=str(data.get("source_action_id", "")),
            created_at=str(data.get("created_at", "")),
            target_session_id=str(data.get("target_session_id", "")),
            finding_ids=tuple(str(item) for item in raw_findings),
            schema_version=raw_schema,
        )


@dataclass(frozen=True)
class ApprovedTechLeadOp:
    """A stored op whose proposal issue no longer carries the gate label.

    Classified by the fact gatherer from the SAME open-issue scan that finds
    tech_lead anchors (#6778): an open issue with a stored op but without
    ``PROPOSED_TECH_LEAD_LABEL`` was approved by the operator. The planner turns
    each into the op's execution action; the applier re-validates
    preconditions and finalizes the proposal issue.
    """

    proposal_issue_number: int
    op: StoredTechLeadOp


@dataclass(frozen=True)
class TechLeadCaseFileSummary:
    """One open pattern case-file issue, as seen by the anchor scan (#6781).

    Classified by the fact gatherer from the SAME open-issue scan that finds
    tech_lead anchors: an open issue carrying ``TECH_LEAD_OBSERVATION_LABEL``.
    Comment cadence is the severity signal, so the summary carries the
    comment count and last-update time alongside the ``area:*`` tag; health
    reviews mine these from the board snapshot instead of the current tick.
    """

    issue_number: int
    title: str
    comment_count: int = 0
    updated_at: str = ""  # ISO timestamp; "" when the scan source lacks it
    area: str = ""  # the area:* tag's value; "" when unclassified

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_number": self.issue_number,
            "title": self.title,
            "comment_count": self.comment_count,
            "updated_at": self.updated_at,
            "area": self.area,
        }


@dataclass(frozen=True)
class TechLeadShippedFixSummary:
    """One area-tagged fix observed at the canonical PR-merge boundary.

    Persisted in the orchestrator-owned tech_lead ledger so health reviews can
    recognize fixed-then-recurred seams across process restarts instead of
    relying on the bounded, process-local session history projection.
    """

    issue_number: int
    title: str
    pr_url: str
    area: str
    merged_at: str
