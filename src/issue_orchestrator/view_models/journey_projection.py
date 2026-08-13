"""Drawer-facing journey overlay projection (issue #6310).

This module owns the dict-shape→typed-model transition for the issue detail
drawer. Inputs are timeline events plus ``IssueProjectionContext``; outputs are
typed ``JourneyRun`` / ``IssueCycle`` / ``JourneyStep`` /
``JourneyPhaseGroup`` / ``CycleArtifacts`` / ``CycleValidationBadge`` models.

Separation of concerns (per reviewer guidance on PR #6312):

* ``view_models.lifecycle_projection`` — events → typed coder / review /
  validation state.  No drawer presentation, no narrative copy, no phase
  bucketing.
* ``view_models.journey_projection`` (this file) — events + lifecycle
  cycles → drawer-facing journey overlay: narrative steps, phase groups,
  artifact references, run grouping, cycle validation badge.
* ``view_models.lifecycle_event_sets`` — canonical event classifier
  frozensets, imported by both projection modules.
* ``view_models.issue_detail`` — endpoint-facing adapter: filter events,
  build ``IssueProjectionContext``, call the journey projection, dump
  typed payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, cast

from ..domain.event_taxonomy import EventIntent, infer_event_intent
from ..domain.logical_run_projection import (
    LogicalRunProjector,
    group_events_by_logical_cycle,
)
from .lifecycle_event_sets import (
    CODING_TERMINAL_EVENTS,
    OUTCOME_EVENTS,
    VALIDATION_FAILED_EVENTS,
    VALIDATION_PASSED_EVENTS,
)
from .lifecycle_projection import project_cycle_stages
from .lifecycle_semantics import (
    CycleArtifacts,
    CycleReviewArtifact,
    CycleValidationBadge,
    IssueCycle,
    IssueProjectionContext,
    JourneyPhaseGroup,
    JourneyPhaseKey,
    JourneyRun,
    JourneyStep,
    OpenValidationDetailsCommand,
    OutcomeBadge,
)
from .projection_values import optional_string
from .timeline_evidence_view import parse_timeline_evidence

EventDict = Mapping[str, Any]


# ---------------------------------------------------------------------------
# Journey-overlay helpers — typed equivalents of the dict-based journey
# helpers that previously lived in ``view_models.issue_detail``.  Issue
# #6310 (PR 1) unifies the drawer payload through these typed helpers so
# the dict projection is no longer a parallel source of truth.
# ---------------------------------------------------------------------------

_logical_run_projector = LogicalRunProjector()

# Narrative templates for journey steps.  ``{_summary}`` is replaced with
# the event summary (with leading ": " when present).
_JOURNEY_NARRATIVE_MAP: dict[str, str] = {
    "session.started": "Code session started",
    "session.completed": "Agent completed{_summary}",
    "session.failed": "Session failed{_summary}",
    "session.invalid_completion_record": "Completion record rejected{_summary}",
    "session.timeout": "Session timed out",
    "session.blocked": "Agent blocked{_summary}",
    "session.validation_failed": "Validation failed{_summary}",
    "issue.pr_created": "PR created{_summary}",
    "issue.blocked": "Blocked{_summary}",
    "issue.needs_human": "Needs human input{_summary}",
    "issue.completed": "Issue completed",
    "issue.unblocked": "Unblocked",
    "review.started": "Code review started",
    "review.rework_started": "Coder addressing review feedback{_summary}",
    "review.rework_completed": "Coder finished review rework{_summary}",
    "review.queued": "Review queued",
    "review.approved": "Reviewer approved{_summary}",
    "review.changes_requested": "Reviewer requested changes{_summary}",
    "review.merged": "PR merged",
    "review.escalated": "Escalated to human review",
    "rework.started": "Rework session started",
    "rework.launching": "Rework session launching",
    "tech_lead.launching": "Tech Lead review launching",
    "validation.started": "Validation started",
    "validation.completed": "Validation passed",
    "session.validation_retry_needed": "Validation failed — retrying{_summary}",
    "publish.failed": "Publish failed{_summary}",
    "review.comment_added": "Review comment posted{_summary}",
}

_TRIVIAL_NARRATIVE_SUMMARIES = frozenset(
    {"completed", "started", "ok", "passed", "failed"}
)

_SESSION_START_NAMES = frozenset(
    {"session.started", "rework.started", "rework.launching"}
)

_DIRECT_OUTCOME_LABELS = {
    "session.completed": "Completed",
    "session.invalid_completion_record": "Failed",
    "review.changes_requested": "Changes Requested",
    "review.approved": "Approved",
    "review.escalated": "Escalated",
    "review.merged": "Merged",
    "issue.completed": "Completed",
}


def derive_cycle_validation_badge(
    events: Sequence[EventDict],
    *,
    issue_number: int,
) -> CycleValidationBadge:
    """Derive the per-cycle validation badge from a cycle's events.

    Single source of truth (issue #6310 AC-2): classifies events using the
    canonical ``VALIDATION_PASSED_EVENTS`` / ``VALIDATION_FAILED_EVENTS`` /
    ``CODING_TERMINAL_EVENTS`` sets and emits one of four states:

    * ``passed`` — validation succeeded; command opens the details dialog.
    * ``failed`` — validation failed; command opens the details dialog.
    * ``not_validated`` — coding finished but no validation event was
      recorded (anti-pattern marker; no command).
    * ``pending`` — coding has not finished; no badge rendered (no command).

    The command's ``run_dir`` is sourced from the validation event itself
    so the dialog can fetch JUnit cases / stdout / stderr.
    """
    cycle_has_terminal = False
    for evt in reversed(tuple(events)):
        name = str(evt.get("event") or evt.get("source_event") or "").lower()
        if name in VALIDATION_FAILED_EVENTS:
            return CycleValidationBadge(
                state="failed",
                command=OpenValidationDetailsCommand(
                    issue_number=issue_number,
                    run_dir=str(evt.get("run_dir") or ""),
                ),
            )
        if name in VALIDATION_PASSED_EVENTS:
            return CycleValidationBadge(
                state="passed",
                command=OpenValidationDetailsCommand(
                    issue_number=issue_number,
                    run_dir=str(evt.get("run_dir") or ""),
                ),
            )
        if not cycle_has_terminal and name in CODING_TERMINAL_EVENTS:
            cycle_has_terminal = True
    if cycle_has_terminal:
        return CycleValidationBadge(state="not_validated", command=None)
    return CycleValidationBadge(state="pending", command=None)


def format_agent_label(event: EventDict) -> str:
    """Extract a short agent label from an event (strips ``agent:`` prefix)."""
    raw = event.get("agent")
    if not isinstance(raw, str) or not raw:
        return ""
    return raw.removeprefix("agent:")


def collect_cycle_artifacts(events: Sequence[EventDict]) -> CycleArtifacts:
    """Collect artifact references (PR URL, log URL, review-feedback flag) for a cycle."""
    log_url: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    has_review_feedback = False
    run_dir: str | None = None
    review_report: CycleReviewArtifact | None = None
    review_decision: CycleReviewArtifact | None = None

    for evt in events:
        canonical = _journey_event_name(evt)
        if canonical == "issue.pr_created":
            pr_url, pr_number = _pr_url_and_number(evt) or (pr_url, pr_number)
        if canonical in ("review.changes_requested", "review.approved"):
            has_review_feedback = True
        event_run_dir = str(evt.get("run_dir") or "").strip()
        if event_run_dir:
            run_dir = event_run_dir
        report, decision = _review_artifacts_from_event(evt)
        review_report = report or review_report
        review_decision = decision or review_decision
        if report or decision:
            has_review_feedback = True
        log_url = _log_url_from_artifacts(evt) or log_url

    return CycleArtifacts(
        log_url=log_url,
        pr_url=pr_url,
        pr_number=pr_number,
        has_review_feedback=has_review_feedback,
        run_dir=run_dir,
        review_report=review_report,
        review_decision=review_decision,
    )


def _review_artifacts_from_event(
    event: EventDict,
) -> tuple[CycleReviewArtifact | None, CycleReviewArtifact | None]:
    run_dir = str(event.get("run_dir") or "").strip()
    if not run_dir:
        return None, None
    report: CycleReviewArtifact | None = None
    decision: CycleReviewArtifact | None = None
    for artifact in event.get("artifacts") or []:
        if not isinstance(artifact, Mapping):
            continue
        artifact_type = str(artifact.get("type") or "")
        if artifact_type not in {"review_report", "review_decision"}:
            continue
        value = str(artifact.get("value") or "")
        if not value:
            continue
        render_mode = "markdown" if artifact_type == "review_report" else "json"
        label = (
            "Review report" if artifact_type == "review_report" else "Decision JSON"
        )
        rendered = CycleReviewArtifact(
            artifact_type=cast(
                Literal["review_report", "review_decision"], artifact_type,
            ),
            label=str(artifact.get("label") or label),
            artifact_path=value,
            render_mode=cast(Literal["markdown", "json"], render_mode),
            run_dir=run_dir,
        )
        if artifact_type == "review_report":
            report = rendered
        else:
            decision = rendered
    return report, decision


def _pr_url_and_number(event: EventDict) -> tuple[str, int | None] | None:
    """Extract a PR URL + number from an ``issue.pr_created`` event."""
    for artifact in event.get("artifacts") or []:
        if not isinstance(artifact, Mapping):
            continue
        value = str(artifact.get("value") or "")
        if "/pull/" not in value:
            continue
        try:
            number = int(value.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            number = None
        return value, number
    return None


def _log_url_from_artifacts(event: EventDict) -> str | None:
    """Pick the last log/transcript artifact URL on an event, if any."""
    result: str | None = None
    for artifact in event.get("artifacts") or []:
        if not isinstance(artifact, Mapping):
            continue
        atype = str(artifact.get("type") or "")
        if atype not in ("log", "transcript"):
            continue
        value = str(artifact.get("value") or artifact.get("url") or "")
        if value:
            result = value
    return result


def derive_cycle_outcome(
    events: Sequence[EventDict],
    iteration: int,
    projection_context: IssueProjectionContext | None = None,
) -> OutcomeBadge:
    """Derive a cycle outcome badge from the last canonical OUTCOME_EVENTS entry.

    Returns a typed ``OutcomeBadge`` ``(label, tone)`` rather than a bare
    string.  Tone classification is the projection layer's responsibility
    (single owner per PR #6333 reviewer feedback) — the UI just reads the
    tone to pick its visual treatment.
    """
    last_outcome_event: EventDict | None = None
    for evt in reversed(tuple(events)):
        if _journey_event_name(evt) in OUTCOME_EVENTS:
            last_outcome_event = evt
            break

    if last_outcome_event is None:
        return outcome_badge("In progress")

    event_name = _journey_event_name(last_outcome_event)
    summary = str(last_outcome_event.get("summary") or "")
    label = _outcome_label(event_name, summary, projection_context)

    if iteration > 1:
        is_review_cycle = any(
            str(evt.get("event_intent") or "") == EventIntent.REVIEW.value
            for evt in events
        )
        if not is_review_cycle:
            label = f"Rework → {label}"
    return outcome_badge(label)


# Tone lookup tables.  These are the canonical labels emitted by the
# projection helpers above (``_outcome_label`` / ``_round_completed_outcome_label``
# / ``_session_outcome_label`` / ``_issue_blocked_outcome_label`` and the
# ``_DIRECT_OUTCOME_LABELS`` set), plus the ``Superseded`` mutation
# applied by ``LogicalRunProjector.build_runs`` and the
# ``In progress`` placeholder.
#
# Every label the projection knows how to emit has an entry below.  An
# unknown label (e.g. a raw summary pass-through from a third-party
# event) falls through to the ``neutral`` tone, never ``passed``.
# Silent green for "I don't know" is the bug the typed
# ``OutcomeBadge`` exists to prevent.
OutcomeTone = Literal["passed", "failed", "error", "in_progress", "neutral"]

_OUTCOME_TONE_EXACT: dict[str, OutcomeTone] = {
    "Completed": "passed",
    "Approved": "passed",
    "Merged": "passed",
    "Changes Requested": "failed",
    "Escalated": "failed",
    "Failed": "failed",
    "Blocked": "failed",
    "Timed out": "failed",
    "Needs human": "failed",
    "Needs human investigation": "failed",
    "Agent blocked": "failed",
    "Session failed": "failed",
    "Publishing failed": "failed",
    "Validation failed": "failed",
    "In progress": "in_progress",
    "Superseded": "neutral",
    "Rework": "in_progress",
}

# Prefixes the projection emits with a trailing ``: <summary>`` or
# ``— <summary>``.  Each maps to the same tone as its exact form.
#
# Order matters: ``outcome_badge`` walks this tuple in order and
# takes the first prefix match, so MORE-SPECIFIC prefixes
# (``Validation failed — retrying``) must precede LESS-SPECIFIC
# ones (``Validation failed —``).  The PR #6333 round-2 reviewer
# blocker was the absence of every entry below "Failed:" — labels
# emitted by ``blocked_explanation_for_event`` (the projection's
# own helper) were falling through to ``neutral``, rendering
# blocked/needs-human/publish-failed outcomes as grey instead of
# red.
_OUTCOME_TONE_PREFIXES: tuple[tuple[str, OutcomeTone], ...] = (
    ("Rework limit reached", "failed"),
    ("Timed out:", "failed"),
    ("Timed out —", "failed"),
    ("Timed out -", "failed"),
    ("Agent blocked:", "failed"),
    ("Agent reported blocked:", "failed"),
    ("Blocked:", "failed"),
    ("Needs human:", "failed"),
    ("Needs human input:", "failed"),
    ("Needs investigation:", "failed"),
    # ``Validation failed — retrying`` (from
    # ``session.validation_retry_needed``) is mid-flight, not
    # terminal — keep it ``in_progress``.  The retry-specific
    # prefix MUST come before the broader ``Validation failed —``
    # entry below.
    ("Validation failed — retrying", "in_progress"),
    ("Validation failed —", "failed"),
    ("Validation failed:", "failed"),
    ("Publishing failed —", "failed"),
    ("Publishing failed:", "failed"),
    ("Session failed —", "failed"),
    ("Session failed:", "failed"),
    ("Failed:", "failed"),
)


def outcome_badge(label: str) -> "OutcomeBadge":
    """Wrap a human-readable outcome label in a typed ``OutcomeBadge``.

    Single owner for tone classification (PR #6333 reviewer
    blocker).  The projection layer constructs every label; this
    function maps each canonical label to its visual ``tone``.
    Unknown labels are ``neutral`` — never silently ``passed``.

    Handles three label shapes:
      * Direct labels (``Completed``, ``Approved``, ``Changes Requested``)
        — exact match.
      * Prefixed labels (``Timed out: <summary>``, ``Blocked: <reason>``,
        ``Needs human: <reason>``, ``Agent blocked: <reason>``,
        ``Failed: <summary>``, ``Rework limit reached ...``) — prefix
        match.
      * Composed labels (``Rework → <inner>``) — recursive lookup on
        ``<inner>``, with a non-terminal ``rework`` parent treated as
        ``in_progress`` if the inner is unknown.
    """
    stripped = label.strip() if isinstance(label, str) else ""
    if not stripped:
        return OutcomeBadge(label=label or "", tone="neutral")

    # ``Rework → <inner>`` composition: tone is determined by the
    # inner label.  Unknown inner → in_progress (rework parent
    # implies still moving forward, not green).
    rework_arrow = "Rework → "
    if stripped.startswith(rework_arrow):
        inner = stripped[len(rework_arrow):].strip()
        inner_badge = outcome_badge(inner)
        tone = inner_badge.tone if inner_badge.tone != "neutral" else "in_progress"
        return OutcomeBadge(label=label, tone=tone)

    direct = _OUTCOME_TONE_EXACT.get(stripped)
    if direct is not None:
        return OutcomeBadge(label=label, tone=direct)

    for prefix, tone in _OUTCOME_TONE_PREFIXES:
        if stripped.startswith(prefix):
            return OutcomeBadge(label=label, tone=tone)

    # Lifecycle state strings (lowercase exact match) used by some
    # callers that bypass the human-label pipeline.
    lower = stripped.lower()
    if lower in {"passed", "completed"}:
        return OutcomeBadge(label=label, tone="passed")
    if lower in {"failed", "blocked"}:
        return OutcomeBadge(label=label, tone="failed")
    if lower == "errored":
        return OutcomeBadge(label=label, tone="error")
    if lower == "skipped":
        return OutcomeBadge(label=label, tone="neutral")

    # Unknown label — neutral, NOT passed.  Silent green for
    # unrecognized outcomes was the original bug.
    return OutcomeBadge(label=label, tone="neutral")


def build_journey_step(evt: EventDict, today: str) -> JourneyStep:
    """Build a typed journey step from one timeline event."""
    timestamp = str(evt.get("timestamp") or "")
    raw_actions = evt.get("actions")
    actions: tuple[dict[str, Any], ...] = ()
    if isinstance(raw_actions, list):
        actions = tuple(
            dict(action) for action in raw_actions if isinstance(action, Mapping)
        )
    return JourneyStep(
        timestamp=timestamp,
        time_label=format_time_label(timestamp, today),
        day=timestamp[:10] if timestamp else "",
        narrative=event_to_narrative(evt),
        status=str(evt.get("status") or ""),
        event=str(evt.get("event") or ""),
        detail=optional_string(evt.get("detail")),
        actions=actions,
        evidence=parse_timeline_evidence(evt.get("evidence")),
        in_round_progress=evt.get("in_round_progress") is True,
    )


def build_journey_phase_groups(
    raw_events: Sequence[EventDict],
    steps: Sequence[JourneyStep],
    iteration: int,
) -> tuple[JourneyPhaseGroup, ...]:
    """Group ``JourneyStep`` entries into user-facing phase buckets."""
    groups: list[JourneyPhaseGroup] = []
    current_key: JourneyPhaseKey | None = None
    bucket: list[JourneyStep] = []
    for evt, step in zip(raw_events, steps, strict=False):
        key = _phase_key_for_event(evt, iteration)
        if current_key != key:
            if current_key is not None:
                groups.append(
                    JourneyPhaseGroup(
                        key=current_key,
                        label=_phase_label_for_key(current_key),
                        steps=tuple(bucket),
                    )
                )
            current_key = key
            bucket = [step]
        else:
            bucket.append(step)
    if current_key is not None:
        groups.append(
            JourneyPhaseGroup(
                key=current_key,
                label=_phase_label_for_key(current_key),
                steps=tuple(bucket),
            )
        )
    return tuple(groups)


def event_to_narrative(event: EventDict) -> str:
    """Convert a timeline event to a single narrative sentence."""
    event_name = str(event.get("event") or "")
    summary = str(event.get("summary") or "")
    agent = format_agent_label(event)

    stored_narrative = event.get("narrative")
    if isinstance(stored_narrative, str) and stored_narrative:
        useful_summary = (
            summary
            if summary and summary.strip().lower() not in _TRIVIAL_NARRATIVE_SUMMARIES
            else ""
        )
        suffix = f": {useful_summary}" if useful_summary else ""
        text = f"{stored_narrative}{suffix}"
        if agent:
            text = f"{text} ({agent})"
        return text

    template = _JOURNEY_NARRATIVE_MAP.get(event_name)
    if template:
        suffix = f": {summary}" if summary else ""
        text = template.replace("{_summary}", suffix)
        if agent:
            text = f"{text} ({agent})"
        return text

    step = str(event.get("step") or event_name or "event")
    label = step.replace("_", " ").replace(".", " ").strip().capitalize()
    if summary:
        label = f"{label}: {summary}"
    if agent:
        label = f"{label} ({agent})"
    return label


def format_time_label(timestamp: str, today: str = "") -> str:
    """Format a timestamp to e.g. ``8:15:30 PM`` (today) or ``Feb 8, 8:15:30 PM``."""
    if not timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp)
        time_part = dt.strftime("%-I:%M:%S %p").lstrip("0")
        if today and timestamp[:10] == today:
            return time_part
        date_part = dt.strftime("%b %-d")
        return f"{date_part}, {time_part}"
    except (ValueError, TypeError):
        if "T" in timestamp and len(timestamp) >= 19:
            return timestamp[11:19]
        return timestamp


def format_date_time_label(timestamp: str) -> str:
    """Format a timestamp to e.g. ``Feb 9, 2:10:30 PM`` for cycle headers."""
    if not timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp)
        time_part = dt.strftime("%-I:%M:%S %p").lstrip("0")
        date_part = dt.strftime("%b %-d")
        return f"{date_part}, {time_part}"
    except (ValueError, TypeError):
        if "T" in timestamp and len(timestamp) >= 19:
            return timestamp[:19].replace("T", " ")
        return timestamp


def build_journey_runs(cycles: Sequence[IssueCycle]) -> tuple[JourneyRun, ...]:
    """Group typed ``IssueCycle`` rows into typed ``JourneyRun`` containers."""
    if not cycles:
        return ()

    cycle_dicts = [_cycle_to_run_proxy(cycle) for cycle in cycles]
    run_dicts = _logical_run_projector.build_runs(cycle_dicts)

    typed_runs: list[JourneyRun] = []
    cycles_by_key: dict[tuple[int, int], IssueCycle] = {
        (cycle.lifecycle or 0, cycle.iteration or 0): cycle for cycle in cycles
    }
    for run in run_dicts:
        run_cycle_dicts = run.get("cycles") or []
        run_typed_cycles: list[IssueCycle] = []
        reset_from_scratch = False
        for cd in run_cycle_dicts:
            if not isinstance(cd, dict):
                continue
            key = (int(cd.get("lifecycle") or 0), int(cd.get("iteration") or 0))
            typed = cycles_by_key.get(key)
            if typed is None:
                continue
            if typed.reset_from_scratch:
                reset_from_scratch = True
            # LogicalRunProjector.build_runs mutates older-run cycle
            # outcomes to "Superseded"; mirror that on the typed cycle to
            # keep wire parity with the legacy dict path.  The dict
            # carries a bare string label; wrap it back into a typed
            # OutcomeBadge before assigning.
            mutated_label = str(cd.get("outcome") or typed.outcome.label)
            if mutated_label != typed.outcome.label:
                typed = typed.model_copy(update={"outcome": outcome_badge(mutated_label)})
            run_typed_cycles.append(typed)
        run_number = int(run.get("run_number") or 0)
        outcome = outcome_badge(str(run.get("outcome") or ""))
        run_label = (
            f"Run {run_number} (scratch retry)"
            if reset_from_scratch
            else f"Run {run_number}"
        )
        run_session_ids = tuple(
            str(rid) for rid in (run.get("session_run_ids") or []) if rid
        )
        typed_runs.append(
            JourneyRun(
                run_number=run_number,
                run_label=run_label,
                outcome=outcome,
                run_key=str(run.get("run_key") or ""),
                run_id=(str(run["run_id"]) if run.get("run_id") else None),
                session_run_ids=run_session_ids,
                timestamp=str(run.get("timestamp") or ""),
                time_label=str(run.get("time_label") or ""),
                expanded=bool(run.get("expanded") or False),
                reset_from_scratch=reset_from_scratch,
                cycles=tuple(run_typed_cycles),
            )
        )

    if typed_runs:
        latest = typed_runs[-1]
        if not _run_contains_review_events_typed(latest):
            coerced_outcome = _coerce_non_review_latest_outcome(latest.outcome)
            coerced_cycles = tuple(
                cycle.model_copy(
                    update={"outcome": _coerce_non_review_latest_outcome(cycle.outcome)}
                )
                for cycle in latest.cycles
            )
            typed_runs[-1] = latest.model_copy(
                update={"outcome": coerced_outcome, "cycles": coerced_cycles}
            )

    return tuple(typed_runs)


def _cycle_to_run_proxy(cycle: IssueCycle) -> dict[str, Any]:
    """Build the minimal dict shape that ``LogicalRunProjector`` understands.

    The projector operates on string outcomes; flatten the typed
    ``OutcomeBadge`` to its label for the dict-layer pass.  Tone is
    re-derived on the way back out.
    """
    return {
        "lifecycle": cycle.lifecycle,
        "iteration": cycle.iteration,
        "cycle": cycle.cycle_number,
        "run_id": cycle.run_id,
        "session_run_ids": list(cycle.session_run_ids),
        "timestamp": cycle.timestamp,
        "time_label": cycle.time_label,
        "outcome": cycle.outcome.label,
        "expanded": cycle.expanded,
        "reset_from_scratch": cycle.reset_from_scratch,
        "steps": [
            {"event": step.event, "status": step.status}
            for step in cycle.steps
        ],
    }


def _run_contains_review_events_typed(run: JourneyRun) -> bool:
    for cycle in run.cycles:
        if any(
            step.event.startswith(("review.", "review_exchange."))
            for step in cycle.steps
        ):
            return True
    return False


def _coerce_non_review_latest_outcome(outcome: OutcomeBadge) -> OutcomeBadge:
    """Coerce a latest-run outcome that lacks review events away from
    success-terminal labels.

    Pre-PR-#6333 this operated on bare strings; now it preserves the
    typed-badge contract.  The label-level logic is unchanged.
    """
    label = outcome.label
    lower = label.strip().lower()
    if "approved" in lower or "completed" in lower or "awaiting merge" in lower:
        if lower.startswith("rework"):
            return outcome_badge("Rework → In progress")
        return outcome_badge("In progress")
    return outcome


def _phase_key_for_event(evt: EventDict, iteration: int) -> JourneyPhaseKey:
    logical_phase = str(evt.get("logical_phase") or "").strip().lower()
    if logical_phase in {"coding", "review", "rework", "orchestrator"}:
        return logical_phase  # type: ignore[return-value]

    intent_raw = evt.get("event_intent")
    intent = (
        intent_raw
        if isinstance(intent_raw, str) and intent_raw
        else infer_event_intent(
            event_name=_journey_event_name(evt),
            task=str(evt.get("task") or ""),
        ).value
    )

    if intent == EventIntent.ORCHESTRATOR.value:
        return "orchestrator"
    if intent == EventIntent.REVIEW.value:
        return "review"
    if intent == EventIntent.REWORK.value:
        return "rework"
    if intent == EventIntent.CODING.value:
        return "rework" if iteration > 1 else "coding"
    return "rework" if iteration > 1 else "coding"


def _phase_label_for_key(key: JourneyPhaseKey) -> str:
    if key == "review":
        return "Review"
    if key == "orchestrator":
        return "Orchestrator"
    if key == "rework":
        return "Rework"
    return "Coding"


def _outcome_label(
    event_name: str,
    summary: str,
    context: IssueProjectionContext | None,
) -> str:
    """Map a single event name + context to its outcome label text."""
    round_completed = _round_completed_outcome_label(event_name, summary)
    if round_completed is not None:
        return round_completed
    session_label = _session_outcome_label(event_name, summary)
    if session_label is not None:
        return session_label
    direct_label = _DIRECT_OUTCOME_LABELS.get(event_name)
    if direct_label is not None:
        return direct_label
    blocked_label = _issue_blocked_outcome_label(event_name, summary, context)
    if blocked_label is not None:
        return blocked_label
    return summary or event_name


def _round_completed_outcome_label(event_name: str, summary: str) -> str | None:
    if event_name != "review_exchange.round_completed" or not summary:
        return None
    summary_lower = summary.strip().lower()
    if "changes_requested" in summary_lower:
        return "Changes Requested"
    if "ok" in summary_lower:
        return "Approved"
    return None


def _session_outcome_label(event_name: str, summary: str) -> str | None:
    suffix = f": {summary}" if summary else ""
    if event_name == "session.failed":
        return f"Failed{suffix}"
    if event_name == "session.timeout":
        return f"Timed out{suffix}"
    if event_name == "session.blocked":
        reason = summary or "unknown"
        return f"Agent blocked: {reason}"
    return None


def _issue_blocked_outcome_label(
    event_name: str,
    summary: str,
    context: IssueProjectionContext | None,
) -> str | None:
    if event_name == "issue.blocked":
        if context and context.flow_stage == "blocked":
            return blocked_explanation_for_event(
                context=context,
                event_name=event_name,
                summary=summary,
            )
        reason = summary or "blocked"
        return f"Blocked: {reason}"
    if event_name == "issue.needs_human":
        reason = summary or "unknown"
        return f"Needs human: {reason}"
    return None


def blocked_explanation_for_event(  # noqa: C901 — dispatches across blocking conditions
    *,
    context: IssueProjectionContext,
    event_name: str,
    summary: str,
) -> str:
    """Produce a specific blocked explanation from labels + a single blocking event.

    Used by the projection layer to derive outcome labels.  The legacy
    ``view_models.issue_detail._blocked_explanation`` keeps the full
    status-explanation flow that needs ``IssueStoryContext`` fields not
    present on ``IssueProjectionContext`` (issue #6310 AC-3).
    """
    labels = context.labels

    if event_name == "review.escalated" or (
        context.current_rework_cycle > 0
        and context.current_rework_cycle >= context.max_rework_cycles
    ):
        return (
            f"Rework limit reached (cycle {context.current_rework_cycle}/"
            f"{context.max_rework_cycles}) — reviewer keeps requesting changes"
        )

    if event_name == "session.timeout":
        return "Timed out — agent didn't invoke completion command"

    if event_name == "session.validation_failed" or "validation-failed" in labels:
        reason = summary or "project tests did not pass"
        return f"Validation failed — {reason}"

    if any(label in labels for label in ("blocked-needs-human", "needs-human")):
        if event_name == "issue.needs_human" and summary:
            return f"Needs human input: {summary}"
        if summary:
            return f"Needs investigation: {summary}"
        return "Needs human investigation"

    if event_name in ("session.blocked", "issue.blocked"):
        reason = summary or "unknown reason"
        return f"Agent reported blocked: {reason}"

    if event_name == "session.failed":
        reason = summary or "session crashed"
        return f"Session failed — {reason}"

    if "publish-failed" in labels:
        return (
            f"Publishing failed: {summary}"
            if summary
            else "Publishing failed — could not push or create PR"
        )
    if "blocked-failed" in labels:
        return f"Failed: {summary}" if summary else "Session failed"

    if summary:
        return f"Blocked: {summary}"
    return "Blocked"


def build_journey_cycles_from_events(  # noqa: C901 — orchestration entry point
    events: Sequence[EventDict],
    today: str,
    projection_context: IssueProjectionContext,
    *,
    issue_number: int,
    review_required: bool = False,
) -> tuple[IssueCycle, ...]:
    """Build typed ``IssueCycle`` tuples with both lifecycle and journey fields.

    Unified entry point that replaces the dict-shaped journey-cycle path
    that previously lived in ``view_models.issue_detail``.  Each
    ``IssueCycle`` carries typed ``coder`` / ``review`` plus the full
    journey overlay (artifacts, steps, phase groups, validation badge —
    typed ``CycleValidationBadge`` derived from the same canonical event
    sets the lifecycle projection uses; issue #6310 AC-2).
    """
    if not _has_logical_semantics(events):
        return ()

    cycles: list[IssueCycle] = []
    grouped_events: list[dict[str, Any]] = [
        dict(event)
        for event in events
        if event.get("views") is not None
        or not _should_skip_journey_event(_journey_event_name(event))
    ]
    groups = group_events_by_logical_cycle(grouped_events)
    group_list = list(groups)
    for idx, group in enumerate(group_list, start=1):
        raw_events = list(group.events)
        # Public cycle-stage projection API — no reach into private
        # lifecycle internals (issue #6310 review feedback).
        coder, review = project_cycle_stages(
            issue_number=issue_number,
            cycle_number=idx,
            events=raw_events,
            review_required=review_required,
        )

        steps = tuple(build_journey_step(evt, today) for evt in raw_events)
        phase_groups = build_journey_phase_groups(raw_events, steps, group.logical_cycle)

        first_ts = str(raw_events[0].get("timestamp") or "") if raw_events else ""
        run_id = next(
            (str(evt.get("run_id")) for evt in raw_events if evt.get("run_id")),
            None,
        )
        session_run_ids = tuple(
            dict.fromkeys(
                str(evt.get("run_id"))
                for evt in raw_events
                if evt.get("run_id")
            )
        )
        session_starts = sum(
            1
            for evt in raw_events
            if _journey_event_name(evt) in _SESSION_START_NAMES
        )
        retry_count = max(0, session_starts - 1)
        reset_from_scratch = any(
            bool(evt.get("from_scratch") or evt.get("reset_from_scratch"))
            for evt in raw_events
        )
        agent_label = ""
        for evt in raw_events:
            label = format_agent_label(evt)
            if label:
                agent_label = label
                break
        reviewer_agent = ""
        for evt in raw_events:
            raw = evt.get("reviewer_agent")
            if isinstance(raw, str) and raw:
                reviewer_agent = raw.removeprefix("agent:")
                break

        outcome = derive_cycle_outcome(
            raw_events, group.logical_cycle, projection_context
        )
        artifacts = collect_cycle_artifacts(raw_events)
        cycle_label = (
            f"Cycle {group.logical_cycle} (scratch)"
            if reset_from_scratch
            else f"Cycle {group.logical_cycle}"
        )
        validation_badge = derive_cycle_validation_badge(
            raw_events, issue_number=issue_number
        )

        cycle = IssueCycle(
            cycle_number=idx,
            coder=coder,
            review=review,
            outcome=outcome,
            diagnostics=(),
            lifecycle=group.logical_run,
            iteration=group.logical_cycle,
            run_id=run_id,
            timestamp=first_ts,
            session_run_ids=session_run_ids,
            agent=agent_label,
            reviewer_agent=reviewer_agent,
            retry_count=retry_count,
            reset_from_scratch=reset_from_scratch,
            cycle_label=cycle_label,
            time_label=format_date_time_label(first_ts),
            expanded=False,
            artifacts=artifacts,
            steps=steps,
            phase_groups=phase_groups,
            validation=validation_badge,
        )
        cycles.append(cycle)

    if cycles:
        cycles[-1] = cycles[-1].model_copy(update={"expanded": True})

    # Annotate run-local cycle sequence (``cycle_in_run``).
    cycle_dicts = [_cycle_to_run_proxy(cycle) for cycle in cycles]
    annotated = _logical_run_projector.annotate_cycle_in_run(cycle_dicts)
    annotated_in_run: dict[tuple[int, int], int | None] = {}
    for ad in annotated:
        if not isinstance(ad, dict):
            continue
        key = (int(ad.get("lifecycle") or 0), int(ad.get("iteration") or 0))
        run_seq = ad.get("cycle_in_run")
        annotated_in_run[key] = run_seq if isinstance(run_seq, int) else None
    cycles = [
        cycle.model_copy(
            update={
                "cycle_in_run": annotated_in_run.get(
                    (cycle.lifecycle or 0, cycle.iteration or 0)
                )
            }
        )
        for cycle in cycles
    ]
    return tuple(cycles)


_JOURNEY_SKIP_PREFIXES = (
    "observation.",
    "cleanup.",
    "tick.",
    "orchestrator.",
    "apply.",
    "worktree.",
    "completion.",
    "stale.",
    "pr.",
    "claim.",
)


def _should_skip_journey_event(event_name: str) -> bool:
    """Return True for legacy events that should be filtered from journey steps."""
    if event_name in (
        "issue.labels_changed",
        "issue.claimed",
        "session.processing_completed",
        "session.no_completion_record",
        "session.no_output",
    ):
        return True
    return any(event_name.startswith(p) for p in _JOURNEY_SKIP_PREFIXES)


def _has_logical_semantics(events: Sequence[EventDict]) -> bool:
    if not events:
        return False
    return all(
        isinstance(e.get("logical_run"), int)
        and isinstance(e.get("logical_cycle"), int)
        and isinstance(e.get("logical_phase"), str)
        for e in events
    )


def _journey_event_name(event: EventDict) -> str:
    return str(event.get("source_event") or event.get("event") or "")


__all__ = [
    "blocked_explanation_for_event",
    "build_journey_cycles_from_events",
    "build_journey_phase_groups",
    "build_journey_runs",
    "build_journey_step",
    "collect_cycle_artifacts",
    "derive_cycle_outcome",
    "derive_cycle_validation_badge",
    "event_to_narrative",
    "format_agent_label",
    "format_date_time_label",
    "format_time_label",
]
