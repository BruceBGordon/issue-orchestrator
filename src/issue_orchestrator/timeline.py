"""Timeline domain model and helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .domain.event_taxonomy import (
    EventIntent,
    infer_event_intent,
    is_completion_event_name,
    is_e2e_event_name,
    is_issue_event_name,
    is_observation_event_name,
    is_review_event_name,
    is_review_oriented_event,
    is_rework_event_name,
    is_session_event_name,
    is_validation_event_name,
)
from .events.artifact_semantics import (
    CompletionPathSemantics,
    completion_path_semantics,
)
from .events.spec import spec_for
from .ports.timeline_store import TimelineRecord

TIMELINE_SCHEMA_VERSION = 4
MIN_SUPPORTED_TIMELINE_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class TimelineArtifact:
    artifact_type: str
    label: str
    value: str
    render_mode: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload: dict[str, str] = {
            "type": self.artifact_type,
            "label": self.label,
            "value": self.value,
        }
        if self.render_mode is not None:
            payload["render_mode"] = self.render_mode
        return payload


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    timestamp: str
    event: str
    issue_number: int
    phase: str
    step: str
    status: str
    level: str
    summary: str | None
    parent_key: str
    artifacts: list[TimelineArtifact]
    detail: str | None = None
    run_id: str | None = None
    run_dir: str | None = None
    agent: str | None = None
    task: str | None = None
    rework_cycle: int | None = None
    reviewer_agent: str | None = None
    added: list[str] | None = None
    removed: list[str] | None = None
    timeline_schema_version: int | None = None
    unsupported_schema: bool = False
    review_oriented: bool = False
    event_intent: str = EventIntent.SYSTEM.value
    logical_run: int | None = None
    logical_cycle: int | None = None
    logical_phase: str | None = None
    source_event: str | None = None
    views: list[str] | None = None
    narrative: str | None = None
    round_index: int | None = None
    rounds: int | None = None
    reviewer_response_type: str | None = None
    reviewer_response_text: str | None = None
    coder_response_type: str | None = None
    coder_response_text: str | None = None
    role: str | None = None
    response_type: str | None = None
    attempt_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event": self.event,
            "issue_number": self.issue_number,
            "phase": self.phase,
            "step": self.step,
            "status": self.status,
            "level": self.level,
            "summary": self.summary,
            "parent_key": self.parent_key,
            "detail": self.detail,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "unsupported_schema": self.unsupported_schema,
            "review_oriented": self.review_oriented,
            "event_intent": self.event_intent,
        }
        # Optional fields: include when set
        _optional: list[tuple[str, Any]] = [
            ("agent", self.agent),
            ("task", self.task),
            ("rework_cycle", self.rework_cycle),
            ("reviewer_agent", self.reviewer_agent),
            ("added", self.added),
            ("removed", self.removed),
            ("timeline_schema_version", self.timeline_schema_version),
            ("logical_run", self.logical_run),
            ("logical_cycle", self.logical_cycle),
            ("logical_phase", self.logical_phase),
            ("source_event", self.source_event),
            ("views", self.views),
            ("narrative", self.narrative),
            ("round_index", self.round_index),
            ("rounds", self.rounds),
            ("reviewer_response_type", self.reviewer_response_type),
            ("reviewer_response_text", self.reviewer_response_text),
            ("coder_response_type", self.coder_response_type),
            ("coder_response_text", self.coder_response_text),
            ("role", self.role),
            ("response_type", self.response_type),
            ("attempt_index", self.attempt_index),
        ]
        for key, val in _optional:
            if val is not None and val != "":
                d[key] = val
        return d


def project_timeline(
    records: list[TimelineRecord], *, issue_number: int
) -> list[TimelineEvent]:
    """Project a sequence of TimelineRecords into TimelineEvents.

    This is the single canonical projection — every consumer (web view
    models, replay tools, golden tests, the e2e timeline assembler)
    flows through this function.

    Contract:
      - Input order = output order. Each record produces exactly one
        `TimelineEvent`.
      - The projection is pure and deterministic given the inputs;
        no I/O, no global state.
      - Per-event display fields (phase / step / status / level) come
        from `events/spec.py` for catalogued public events, with a
        legacy fallback in this module for events outside
        `PublicEventName` (raw e2e runner events, debug-tier events).

    Use this function as the target for golden-timeline assertions.
    Goldens that pin a specific scenario's record stream to an expected
    ordered list of `TimelineEvent`s assert against the output of this
    function — making the projection function the single point at which
    "the timeline is right" is decided.
    """
    return [_record_to_event(issue_number, record) for record in records]


@dataclass(frozen=True)
class TimelineStream:
    """Higher-level view over timeline records for an issue."""

    issue_number: int
    events: list[TimelineEvent]

    @classmethod
    def from_records(
        cls, issue_number: int, records: list[TimelineRecord]
    ) -> "TimelineStream":
        return cls(
            issue_number=issue_number,
            events=project_timeline(records, issue_number=issue_number),
        )

    def group_by_phase(self) -> dict[str, list[TimelineEvent]]:
        grouped: dict[str, list[TimelineEvent]] = {}
        for event in self.events:
            grouped.setdefault(event.phase, []).append(event)
        return grouped

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_number": self.issue_number,
            "events": [event.to_dict() for event in self.events],
        }


def build_issue_timeline(
    issue_number: int, records: list[TimelineRecord]
) -> dict[str, Any]:
    return TimelineStream.from_records(issue_number, records).to_dict()


def validate_timeline_artifact_refs(data: Mapping[str, Any]) -> None:
    """Fail fast if an event payload contains malformed typed artifact refs."""
    _artifact_refs_from_data(data)


def _record_to_event(issue_number: int, record: TimelineRecord) -> TimelineEvent:
    data = record.data or {}
    event_name = record.event
    # Use the internal (source) event name for all derivation logic so that
    # fan-out renames (e.g. session.failed → agent.failed) don't break
    # phase/step/status/detail extraction.  Display uses event_name.
    canonical_name = record.source_event or event_name
    phase = _phase_for_event(canonical_name)
    step = _step_for_event(canonical_name)
    status = _status_for_event(canonical_name, data)
    level = _level_for_event(canonical_name)
    summary = _summary_from_data(data, event_name=canonical_name)
    detail = _detail_from_data(canonical_name, data, summary)
    parent_key = _parent_key(issue_number, data)
    run_id = _run_id_from_data(data)
    run_dir = _run_dir_from_data(data)
    artifacts = _artifacts_from_data(data, event_name=canonical_name)
    agent = data.get("agent") if isinstance(data.get("agent"), str) else None
    task = data.get("task") if isinstance(data.get("task"), str) else None
    rework_cycle = (
        data.get("rework_cycle") if isinstance(data.get("rework_cycle"), int) else None
    )
    reviewer_agent = (
        data.get("reviewer_agent")
        if isinstance(data.get("reviewer_agent"), str)
        else None
    )
    added = _string_list_or_none(data.get("added"))
    removed = _string_list_or_none(data.get("removed"))
    timeline_schema_version = _timeline_schema_version_from_data(data)
    logical_run = (
        data.get("logical_run") if isinstance(data.get("logical_run"), int) else None
    )
    logical_cycle = (
        data.get("logical_cycle")
        if isinstance(data.get("logical_cycle"), int)
        else None
    )
    logical_phase = (
        data.get("logical_phase")
        if isinstance(data.get("logical_phase"), str)
        else None
    )
    round_index = (
        data.get("round_index") if isinstance(data.get("round_index"), int) else None
    )
    rounds = data.get("rounds") if isinstance(data.get("rounds"), int) else None
    reviewer_response_type = (
        data.get("reviewer_response_type")
        if isinstance(data.get("reviewer_response_type"), str)
        else None
    )
    reviewer_response_text = (
        data.get("reviewer_response_text")
        if isinstance(data.get("reviewer_response_text"), str)
        else None
    )
    coder_response_type = (
        data.get("coder_response_type")
        if isinstance(data.get("coder_response_type"), str)
        else None
    )
    coder_response_text = (
        data.get("coder_response_text")
        if isinstance(data.get("coder_response_text"), str)
        else None
    )
    role = data.get("role") if isinstance(data.get("role"), str) else None
    # Per-role verdict carried by ``review_exchange.role_feedback`` events.
    # Distinct from the round-level ``reviewer_response_type`` /
    # ``coder_response_type`` recorded on ``round_completed``; needed so the
    # in-flight substate ("coder needs requested changes") survives to the
    # view model instead of being dropped (issue #6428).
    response_type = (
        data.get("response_type")
        if isinstance(data.get("response_type"), str)
        else None
    )
    attempt_index = (
        data.get("attempt_index")
        if isinstance(data.get("attempt_index"), int)
        else None
    )
    unsupported_schema = (
        timeline_schema_version is None
        or timeline_schema_version < MIN_SUPPORTED_TIMELINE_SCHEMA_VERSION
        or logical_run is None
        or logical_cycle is None
        or not logical_phase
    )
    review_oriented_raw = data.get("review_oriented")
    if isinstance(review_oriented_raw, bool):
        review_oriented = review_oriented_raw
    else:
        review_oriented = is_review_oriented_event(event_name=canonical_name, task=task)
    intent_raw = data.get("event_intent")
    if isinstance(intent_raw, str):
        try:
            event_intent = EventIntent(intent_raw).value
        except ValueError:
            event_intent = infer_event_intent(
                event_name=canonical_name, task=task
            ).value
    else:
        event_intent = infer_event_intent(event_name=canonical_name, task=task).value
    return TimelineEvent(
        event_id=record.event_id,
        timestamp=record.timestamp,
        event=event_name,
        issue_number=issue_number,
        phase=phase,
        step=step,
        status=status,
        level=level,
        summary=summary,
        detail=detail,
        parent_key=parent_key,
        run_id=run_id,
        run_dir=run_dir,
        artifacts=artifacts,
        agent=agent,
        task=task,
        rework_cycle=rework_cycle,
        reviewer_agent=reviewer_agent,
        added=added,
        removed=removed,
        timeline_schema_version=timeline_schema_version,
        unsupported_schema=unsupported_schema,
        review_oriented=review_oriented,
        event_intent=event_intent,
        logical_run=logical_run,
        logical_cycle=logical_cycle,
        logical_phase=logical_phase,
        source_event=record.source_event or None,
        views=data.get("views") if isinstance(data.get("views"), list) else None,
        narrative=data.get("narrative")
        if isinstance(data.get("narrative"), str)
        else None,
        round_index=round_index,
        rounds=rounds,
        reviewer_response_type=reviewer_response_type,
        reviewer_response_text=reviewer_response_text,
        coder_response_type=coder_response_type,
        coder_response_text=coder_response_text,
        role=role,
        response_type=response_type,
        attempt_index=attempt_index,
    )


def _string_list_or_none(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [item for item in value if isinstance(item, str)]
    return items if items else None


def _timeline_schema_version_from_data(data: dict[str, Any]) -> int | None:
    raw = data.get("timeline_schema_version")
    if isinstance(raw, int):
        return raw
    return None


def _phase_for_event(event_name: str) -> str:
    spec = spec_for(event_name)
    if spec is not None:
        return spec.phase
    # Fallback: events outside the EventName enum (e.g. raw `e2e.test_*`
    # strings emitted by the e2e runner). The spec covers every catalogued
    # event; this branch only fires for un-catalogued events.
    if is_e2e_event_name(event_name):
        return _e2e_phase(event_name)
    if is_validation_event_name(event_name) or event_name in {
        "session.validation_failed",
        "session.validation_retry_needed",
        "issue.pr_created",
        "review.queued",
        "publish.failed",
    }:
        return "orchestrator"
    if event_name in {"issue.completed"}:
        return "completed"
    if event_name in {"issue.blocked"}:
        return "blocked"
    if event_name in {"issue.needs_human"}:
        return "needs_human"
    if is_review_event_name(event_name):
        return "reviewing"
    if is_rework_event_name(event_name):
        return "rework"
    if (
        is_issue_event_name(event_name)
        or is_session_event_name(event_name)
        or is_completion_event_name(event_name)
        or is_observation_event_name(event_name)
    ):
        return "in_progress"
    return "system"


def _e2e_phase(event_name: str) -> str:
    if event_name in ("e2e.run_started", "e2e.tests_collected"):
        return "setup"
    if event_name.startswith("e2e.test_"):
        return "execution"
    if event_name == "e2e.retry_started":
        return "retry"
    return "teardown"


def _step_for_event(event_name: str) -> str:
    spec = spec_for(event_name)
    if spec is not None:
        return spec.step
    # Fallback for un-catalogued events.
    if is_e2e_event_name(event_name):
        return event_name.replace("e2e.", "")
    if is_session_event_name(event_name):
        return event_name.replace("session.", "")
    if is_issue_event_name(event_name):
        return event_name.replace("issue.", "")
    if is_review_event_name(event_name):
        if event_name.startswith("review_exchange."):
            return event_name.replace("review_exchange.", "")
        return event_name.replace("review.", "")
    if is_rework_event_name(event_name):
        return event_name.replace("rework.", "")
    if is_completion_event_name(event_name):
        return event_name.replace("completion.", "")
    if is_observation_event_name(event_name):
        return event_name.replace("observation.", "")
    return event_name


def _status_for_event(event_name: str, data: dict[str, Any] | None = None) -> str:
    # E2E test-runner events have data-dependent status (e.g.
    # `e2e.test_completed` checks `data['outcome']`). The catalogued
    # `e2e.*` events always return 'active' per the spec — but
    # un-catalogued ones (e2e.test_*, e2e.run_finished, etc.) need
    # `_e2e_status` for the data-driven outcome derivation.
    if is_e2e_event_name(event_name):
        spec = spec_for(event_name)
        if spec is not None:
            return spec.status
        return _e2e_status(event_name, data)
    spec = spec_for(event_name)
    if spec is not None:
        return spec.status
    # Fallback for un-catalogued non-e2e events.
    failure_events = {
        "session.failed",
        "session.timeout",
        "session.blocked",
        "session.validation_failed",
        "issue.blocked",
        "issue.dependency_blocked",
        "issue.needs_human",
        "issue.pr_rejected",
        "review.changes_requested",
        "review.escalated",
        "review.closed",
        "rework.escalating",
        "dependency.blocked",
        "session.validation_retry_needed",
        "publish.failed",
        "review_exchange.role_timeout",
    }
    success_events = {
        "session.completed",
        "issue.pr_created",
        "issue.completed",
        "issue.unblocked",
        "issue.released",
        "dependency.unblocked",
        "review.approved",
        "review.skipped",
        "review.rework_completed",
        "review.tech_lead_approved",
        "review.merged",
        "rework.skipped",
        "tech_lead.skipped",
        "cleanup.completed",
        "validation.completed",
    }
    pending_events = {
        "issue.claimed",
        "issue.started",
        "review.queued",
        "review.started",
        "review.launching",
        "review.rework_started",
        "review.tech_lead_started",
        "rework.started",
        "rework.launching",
        "tech_lead.issue_created",
        "tech_lead.launching",
        "validation.started",
        "provider.transient_error",
        "provider.outage_entered",
        "provider.retry_scheduled",
        "provider.retry_attempted",
        "provider.outage_exited",
    }
    if event_name in failure_events:
        return "failed"
    if event_name in success_events:
        return "completed"
    if event_name in pending_events:
        return "started"
    if event_name.endswith(".started") or event_name.endswith(".launching"):
        return "started"
    return "completed"


def _level_for_event(event_name: str) -> str:
    spec = spec_for(event_name)
    if spec is not None:
        return spec.level
    # Fallback for un-catalogued events.
    if is_e2e_event_name(event_name):
        return _e2e_level(event_name)
    if is_issue_event_name(event_name) or is_review_event_name(event_name):
        return "phase"
    return "detail"


def _e2e_status(event_name: str, data: dict[str, Any] | None = None) -> str:
    if event_name in ("e2e.run_error", "e2e.run_canceled"):
        return "error"
    if event_name == "e2e.test_completed" and data:
        outcome = data.get("outcome", "")
        if outcome in ("failed", "error"):
            return "error"
        if outcome == "skipped":
            return "skipped"
        return "completed"
    if event_name == "e2e.test_completed":
        return "completed"
    if event_name == "e2e.run_finished":
        return "completed"
    return "active"


def _e2e_level(event_name: str) -> str:
    if event_name == "e2e.run_error":
        return "error"
    if event_name == "e2e.run_canceled":
        return "warning"
    if "test_completed" in event_name:
        return "detail"
    return "info"


def _parent_key(issue_number: int, data: dict[str, Any]) -> str:
    if issue_number < 0:
        return f"e2e-run-{-issue_number}"
    if isinstance(data.get("session_id"), str):
        return f"session:{data['session_id']}"
    if isinstance(data.get("pr_number"), int):
        return f"review:{data['pr_number']}"
    return f"issue:{issue_number}"


def _summary_from_data(data: dict[str, Any], event_name: str = "") -> str | None:
    if is_e2e_event_name(event_name):
        return _e2e_summary(event_name, data)
    if event_name == "publish.failed":
        return _publish_failed_summary(data)
    if event_name == "review_exchange.completed":
        # The runner emits machine codes here (`reason="reviewer_ok"`,
        # `status="ok"`) for downstream policy decisions, not user
        # display. The narrative ("Review exchange completed (N
        # rounds)") carries the user-facing text; suppress summary so
        # raw machine codes don't render as the timeline summary line.
        return None
    if event_name == "review_exchange.role_timeout":
        # Same rationale: `data["reason"]` here is a machine code
        # (`"no_completion"`) for downstream retry policy. The
        # narrative carries the precise user-facing failure reason
        # from `failure_reason` when present.
        return None
    for key in ("reason", "summary", "error", "status", "outcome"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _e2e_summary(event_name: str, data: dict[str, Any]) -> str:
    if event_name == "e2e.run_started":
        branch = data.get("branch", "unknown")
        return f"E2E run started on {branch}"
    if event_name == "e2e.tests_collected":
        return f"Collected {data.get('total', '?')} tests"
    if event_name == "e2e.test_started":
        return data.get("nodeid", "")
    if event_name == "e2e.test_completed":
        outcome = data.get("outcome", "?")
        nodeid = data.get("nodeid", "")
        dur = data.get("duration_seconds")
        dur_str = f" ({dur:.1f}s)" if dur else ""
        return f"{nodeid}: {outcome}{dur_str}"
    if event_name == "e2e.retry_started":
        return f"Retrying {data.get('failed_count', '?')} failed tests"
    if event_name == "e2e.run_finished":
        return f"Run {data.get('status', '?')} in {data.get('duration_seconds', '?')}s"
    if event_name == "e2e.run_canceled":
        return "Run canceled"
    if event_name == "e2e.run_error":
        return f"Run error: {str(data.get('error', 'unknown'))[:100]}"
    return event_name


_MAX_SUMMARY = 200
_MAX_DETAIL = 200
_MAX_REVIEW_COMMENT_DETAIL = 4000


def _detail_from_data(  # noqa: C901, PLR0912 — event-type dispatch for detail extraction
    event_name: str,
    data: dict[str, Any],
    summary: str | None,
) -> str | None:
    """Extract contextual detail from enriched event data.

    The detail augments the narrative with "what happened and why"
    without duplicating the summary.  Returns None when no useful
    detail can be extracted.
    """
    parts: list[str] = []
    summary_str = summary or ""

    if event_name in ("session.blocked", "issue.blocked"):
        _add_if_new(parts, data.get("attempted"), summary_str)
        blocked_by = data.get("blocked_by")
        if isinstance(blocked_by, list) and blocked_by:
            issues = ", ".join(f"#{n}" for n in blocked_by)
            parts.append(f"Blocked by: {issues}")

    elif event_name in (
        "session.timeout",
        "session.failed",
        "session.invalid_completion_record",
    ):
        runtime = data.get("runtime_minutes")
        timeout = data.get("timeout_minutes")
        if runtime is not None and timeout is not None:
            parts.append(f"Ran {runtime:.0f} min (limit: {timeout} min)")
        elif runtime is not None:
            parts.append(f"Ran {runtime:.0f} min")
        parts.extend(
            _invalid_completion_detail_parts(
                data,
                summary_str,
                require_failure_kind=event_name != "session.invalid_completion_record",
            )
        )
        _add_if_new(parts, data.get("problems"), summary_str)

    elif event_name == "session.completed":
        _add_if_new(parts, data.get("implementation"), summary_str)
        _add_if_new(parts, data.get("problems"), summary_str)

    elif event_name == "session.validation_failed":
        _add_if_new(parts, data.get("validation_reason"), summary_str)

    elif event_name == "session.validation_retry_needed":
        _add_if_new(parts, data.get("validation_reason"), summary_str)
        source = data.get("validation_source")
        if isinstance(source, str) and source:
            parts.append(f"Source: {source}")

    elif event_name == "review.changes_requested":
        _add_if_new(parts, data.get("review_issues"), summary_str)
        risk = data.get("risk_level")
        if isinstance(risk, str) and risk:
            parts.append(f"Risk: {risk}")

    # `review.approved` carries its text in `data["summary"]` (see
    # `CompletionProcessor._emit_review_outcome`), which is already
    # picked up by `_summary_from_data`. The legacy `review_summary`
    # key was never populated on the local-exchange path; the branch
    # was dead and has been removed.

    elif event_name == "review.comment_added":
        _add_if_new(parts, data.get("comment_excerpt"), summary_str)

    elif event_name == "review_exchange.round_completed":
        round_index = data.get("round_index")
        if isinstance(round_index, int):
            parts.append(f"Round {round_index}")
        _add_if_new(parts, data.get("reviewer_response_text"), summary_str)
        coder_response_type = data.get("coder_response_type")
        if isinstance(coder_response_type, str) and coder_response_type:
            parts.append(f"Coder response: {coder_response_type}")

    elif event_name == "review.rework_started":
        round_index = data.get("round_index")
        if isinstance(round_index, int):
            parts.append(f"Round {round_index}")
        _add_if_new(parts, data.get("reviewer_response_text"), summary_str)

    elif event_name == "review.rework_completed":
        round_index = data.get("round_index")
        if isinstance(round_index, int):
            parts.append(f"Round {round_index}")
        coder_response_type = data.get("coder_response_type")
        if isinstance(coder_response_type, str) and coder_response_type:
            parts.append(f"Coder response: {coder_response_type}")
        _add_if_new(parts, data.get("coder_response_text"), summary_str)

    elif event_name == "review.escalated":
        rework = data.get("rework_cycle")
        limit = data.get("max_rework_cycles")
        if rework is not None and limit is not None:
            parts.append(f"Rework cycle {rework}/{limit}")

    elif event_name == "issue.needs_human":
        _add_if_new(parts, data.get("question"), summary_str)

    elif event_name == "publish.failed":
        branch = data.get("branch")
        if isinstance(branch, str) and branch:
            parts.append(f"Branch: {branch}")
        retryable = data.get("retryable")
        if isinstance(retryable, bool):
            parts.append(f"Retryable: {'yes' if retryable else 'no'}")

    if not parts:
        return None

    text = ". ".join(parts)
    max_detail = (
        _MAX_REVIEW_COMMENT_DETAIL
        if event_name == "review.comment_added"
        else _MAX_DETAIL
    )
    if len(text) > max_detail:
        text = text[: max_detail - 1] + "\u2026"
    return text


def _invalid_completion_detail_parts(
    data: dict[str, Any],
    summary: str,
    *,
    require_failure_kind: bool,
) -> list[str]:
    if require_failure_kind and data.get("failure_kind") != "invalid_completion_record":
        return []
    parts: list[str] = []
    failure = data.get("completion_load_failure")
    if isinstance(failure, str) and failure:
        parts.append(f"Failure: {failure}")
    _add_if_new(parts, data.get("completion_parse_error"), summary)
    completion_path = data.get("completion_path_absolute")
    if isinstance(completion_path, str) and completion_path:
        parts.append(f"Completion: {completion_path}")
    return parts


def _add_if_new(parts: list[str], value: Any, summary: str) -> None:
    """Append a string value to parts if it's not already in the summary."""
    if isinstance(value, str) and value and value not in summary:
        parts.append(value)


def _publish_failed_summary(data: dict[str, Any]) -> str | None:
    raw_error = data.get("error")
    if not isinstance(raw_error, str) or not raw_error.strip():
        return "Publish failed"
    error = " ".join(raw_error.split())
    stage = data.get("stage")
    stage_label = {
        "push_branch": "Push",
        "create_pr": "PR creation",
    }.get(stage if isinstance(stage, str) else "")
    if stage_label:
        return _truncate_summary(f"{stage_label} failed: {error}")
    return _truncate_summary(error)


def _truncate_summary(text: str) -> str:
    if len(text) <= _MAX_SUMMARY:
        return text
    return text[: _MAX_SUMMARY - 1].rstrip() + "\u2026"


def _artifacts_from_data(
    data: dict[str, Any], *, event_name: str
) -> list[TimelineArtifact]:
    artifacts: list[TimelineArtifact] = []
    seen: dict[tuple[str, str], TimelineArtifact] = {}

    for artifact_type, label, value, render_mode in _explicit_artifacts_from_data(data):
        _append_artifact(artifacts, seen, artifact_type, label, value, render_mode)
    for artifact_type, label, value, render_mode in _artifact_refs_from_data(data):
        _append_artifact(artifacts, seen, artifact_type, label, value, render_mode)
    pr_url = data.get("pr_url")
    if isinstance(pr_url, str) and pr_url:
        _append_artifact(artifacts, seen, "pull_request", "PR", pr_url)
    comment_url = data.get("comment_url")
    if isinstance(comment_url, str) and comment_url:
        _append_artifact(
            artifacts, seen, "review_comment", "Review Comment", comment_url
        )
    completion_path = data.get("completion_path_absolute")
    if (
        isinstance(completion_path, str)
        and completion_path
        and completion_path_semantics(event_name, data)
        in {
            CompletionPathSemantics.OPTIONAL_EXISTING_ARTIFACT,
            CompletionPathSemantics.REQUIRED_EXISTING_ARTIFACT,
        }
    ):
        _append_artifact(
            artifacts, seen, "completion_record", "Completion", completion_path
        )
    worktree_path = data.get("worktree_path")
    if isinstance(worktree_path, str) and worktree_path:
        _append_artifact(artifacts, seen, "worktree", "Worktree", worktree_path)
    validation_path = data.get("validation_record_path")
    if isinstance(validation_path, str) and validation_path:
        _append_artifact(artifacts, seen, "validation", "Validation", validation_path)
    run_dir = _run_dir_from_data(data)
    if isinstance(run_dir, str) and run_dir:
        _append_artifact(artifacts, seen, "run_dir", "Run Dir", run_dir)
    return artifacts


def _explicit_artifacts_from_data(
    data: Mapping[str, Any],
) -> list[tuple[str, str, str, str | None]]:
    raw = data.get("artifacts")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("timeline event artifacts must be a list")
    return [_explicit_artifact_fields(artifact) for artifact in raw]


def _explicit_artifact_fields(artifact: Any) -> tuple[str, str, str, str | None]:
    if not isinstance(artifact, Mapping):
        raise ValueError("timeline event artifact entries must be objects")
    return (
        _required_artifact_text(artifact, "type"),
        _required_artifact_text(artifact, "label"),
        _required_artifact_text(artifact, "value"),
        _optional_artifact_text(artifact, "render_mode"),
    )


def _artifact_refs_from_data(
    data: Mapping[str, Any],
) -> list[tuple[str, str, str, str | None]]:
    raw = data.get("artifact_refs")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("timeline event artifact_refs must be a list")
    return [_artifact_ref_fields(artifact) for artifact in raw]


def _artifact_ref_fields(artifact: Any) -> tuple[str, str, str, str | None]:
    if not isinstance(artifact, Mapping):
        raise ValueError("timeline event artifact_refs entries must be objects")
    return (
        _required_artifact_ref_text(artifact, "kind"),
        _required_artifact_ref_text(artifact, "label"),
        _required_artifact_ref_text(artifact, "path"),
        _optional_artifact_text(artifact, "render_mode"),
    )


def _required_artifact_text(artifact: Mapping[str, Any], field: str) -> str:
    return _required_timeline_artifact_text(
        artifact,
        field,
        contract_name="artifacts",
        required_fields="type, label, and value",
    )


def _required_artifact_ref_text(artifact: Mapping[str, Any], field: str) -> str:
    return _required_timeline_artifact_text(
        artifact,
        field,
        contract_name="artifact_refs",
        required_fields="kind, label, and path",
    )


def _required_timeline_artifact_text(
    artifact: Mapping[str, Any],
    field: str,
    *,
    contract_name: str,
    required_fields: str,
) -> str:
    value = artifact.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"timeline event {contract_name} require non-empty {required_fields}"
        )
    return value


def _optional_artifact_text(artifact: Mapping[str, Any], field: str) -> str | None:
    value = artifact.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(
            "timeline event artifacts require string render_mode when present"
        )
    return value


def _append_artifact(
    artifacts: list[TimelineArtifact],
    seen: dict[tuple[str, str], TimelineArtifact],
    artifact_type: str,
    label: str,
    value: str,
    render_mode: str | None = None,
) -> None:
    key = (artifact_type, value)
    existing = seen.get(key)
    if existing is not None:
        raise ValueError(
            "timeline event artifacts contain duplicate artifact identity: "
            f"type={artifact_type!r} value={value!r}"
        )
    artifact = TimelineArtifact(artifact_type, label, value, render_mode)
    seen[key] = artifact
    artifacts.append(artifact)


def _run_id_from_data(data: dict[str, Any]) -> str | None:
    run_id = data.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    return None


def _run_dir_from_data(data: dict[str, Any]) -> str | None:
    run_dir = data.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        return run_dir
    return None
