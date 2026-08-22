"""Shared timeline shaping helpers for entrypoint HTTP surfaces.

These helpers define the timeline semantics that both control and web surfaces
render. The extraction is intentionally behavior-preserving: entrypoints remain
composition roots while the event-shaping rules live in one owner module.

This module is also the canonical import surface for operational scripts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..domain.event_taxonomy import (
    EventIntent,
    is_review_oriented_event,
    is_rework_event_name,
    is_review_event_name,
    is_session_event_name,
)
from ..execution.manifest_accessor import ArtifactNotFoundError
from ..execution.timeline_action_capabilities import (
    AvailableRunArtifacts,
    TimelineRunArtifacts,
    available_run_artifacts,
    classify_timeline_run_artifacts,
    require_external_timeline_url,
    require_existing_timeline_artifact,
    review_feedback_event_name,
    timeline_local_artifact_kind,
    timeline_url_artifact_kind,
)
from ..execution.timeline_artifact_expectations import event_requires_run_dir
from ..timeline import MIN_SUPPORTED_TIMELINE_SCHEMA_VERSION, TIMELINE_SCHEMA_VERSION
from ..view_models.timestamp_values import DETAIL_VALUE_KINDS_KEY, timeline_detail_value_kinds

logger = logging.getLogger(__name__)

_NOISY_TIMELINE_EVENTS = frozenset({"issue.labels_changed"})
_REVIEW_ARTIFACT_TYPES = frozenset({"review_report", "review_decision"})
_TIMELINE_START_EVENTS = frozenset({"session.started", "review.started", "rework.started"})
_TIMELINE_FAILURE_EVENTS = frozenset({
    "agent.invalid_completion_record",
    "issue.blocked",
    "issue.needs_human",
    "issue.pr_rejected",
    "session.blocked",
    "session.failed",
    "session.invalid_completion_record",
    "session.timeout",
    "session.validation_failed",
    "review.changes_requested",
    "review.escalated",
    "review_exchange.role_timeout",
})
_VALIDATION_DETAIL_EVENTS = frozenset({
    "validation.failed",
    "session.validation_failed",
    "validation.passed",
    "session.validation_passed",
})
_ORCHESTRATOR_ONLY_EVENTS = frozenset({
    "validation.passed",
    "validation.failed",
    "validation.retry",
    "validation.started",
    "validation.completed",
    "agent.completed",
    "pr.created",
    "issue.completed",
    "issue.blocked",
    "issue.needs_human",
    "issue.unblocked",
    "session.processing_completed",
    "completion.lookup",
})
def _load_test_result_backfill(
    e2e_db_path: Optional[Path], run_id: int,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read ``e2e_test_results`` for ``run_id`` and index longrepr/outcome by nodeid."""
    longrepr_by_nodeid: dict[str, str] = {}
    outcome_by_nodeid: dict[str, str] = {}
    if e2e_db_path is None or not e2e_db_path.exists():
        return longrepr_by_nodeid, outcome_by_nodeid
    try:
        from ..infra.e2e_db import E2EDB

        details = E2EDB(e2e_db_path).run_details(run_id)
    except Exception:
        logger.debug(
            "Could not load e2e_test_results for run %d", run_id, exc_info=True,
        )
        return longrepr_by_nodeid, outcome_by_nodeid
    if not details:
        return longrepr_by_nodeid, outcome_by_nodeid
    for result in details.get("results") or []:
        node = result.get("nodeid")
        if not isinstance(node, str):
            continue
        lr = result.get("longrepr")
        if isinstance(lr, str) and lr:
            longrepr_by_nodeid[node] = lr
        oc = result.get("outcome")
        if isinstance(oc, str) and oc:
            outcome_by_nodeid[node] = oc
    return longrepr_by_nodeid, outcome_by_nodeid


def _promote_e2e_test_event_fields(
    raw_events: list[dict[str, Any]],
    records: list[Any],
    *,
    run_id: int,
    e2e_db_path: Optional[Path],
) -> None:
    """Promote ``nodeid``/``longrepr``/``outcome`` from data_json onto events."""
    longrepr_by_nodeid, outcome_by_nodeid = _load_test_result_backfill(
        e2e_db_path, run_id,
    )
    for evt, rec in zip(raw_events, records):
        data = rec.data if isinstance(rec.data, dict) else {}
        nodeid = data.get("nodeid")
        if isinstance(nodeid, str) and nodeid:
            evt["nodeid"] = nodeid
        if evt.get("event") != "e2e.test_completed":
            continue
        longrepr = data.get("longrepr")
        if not (isinstance(longrepr, str) and longrepr) and isinstance(nodeid, str):
            longrepr = longrepr_by_nodeid.get(nodeid)
        if isinstance(longrepr, str) and longrepr:
            evt["longrepr"] = longrepr
        outcome = data.get("outcome")
        if not (isinstance(outcome, str) and outcome) and isinstance(nodeid, str):
            outcome = outcome_by_nodeid.get(nodeid)
        if isinstance(outcome, str) and outcome:
            evt["outcome"] = outcome


def _build_phase_toc(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the phase table of contents from the first appearance of each phase."""
    toc: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        phase = str(event.get("phase") or "system")
        if phase in seen:
            continue
        seen.add(phase)
        toc.append({
            "phase": phase,
            "label": _format_phase_name(phase),
        })
    return toc


def _build_timeline_cycles(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group timeline events into stable code/review cycles for both HTTP surfaces."""
    cycles: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    cycle_number = 0
    cycle_phases = {"in_progress", "reviewing", "rework", "tech_lead", "orchestrator"}
    cycle_start_events = {"session.started", "rework.started", "review.started", "tech_lead.launching"}

    for event in events:
        event_name = str(event.get("event") or "")
        phase = str(event.get("phase") or "system")
        if phase not in cycle_phases:
            continue

        if current is None and event_name not in cycle_start_events:
            continue

        if current is not None and event_name == "session.started":
            cycles.append(current)
            current = None

        if current is None:
            cycle_number += 1
            current = {
                "cycle": cycle_number,
                "start": event.get("timestamp"),
                "end": event.get("timestamp"),
                "status": event.get("status") or "started",
                "phases": [phase],
                "events": [event],
                "summary": event.get("summary") or "",
            }
            continue
        current["end"] = event.get("timestamp")
        current["status"] = event.get("status") or current["status"]
        if phase not in current["phases"]:
            current["phases"].append(phase)
        current["events"].append(event)
        if phase == "reviewing" and str(event.get("status")) in {"completed", "failed"}:
            cycles.append(current)
            current = None

    if current is not None:
        cycles.append(current)
    return cycles


def _retain_semantic_timeline_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep only events with required logical semantics for correctness-first rendering."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for event in events:
        timeline_schema_version = event.get("timeline_schema_version")
        if (
            isinstance(timeline_schema_version, int)
            and timeline_schema_version == TIMELINE_SCHEMA_VERSION
            and isinstance(event.get("logical_run"), int)
            and isinstance(event.get("logical_cycle"), int)
            and isinstance(event.get("logical_phase"), str)
            and bool(str(event.get("logical_phase") or "").strip())
        ):
            kept.append(event)
        else:
            dropped += 1
    return kept, dropped


def _filter_timeline_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop high-volume low-signal events from timeline payloads.

    This stays centralized so the control API, web UI, and supporting scripts
    all suppress the same noise instead of re-implementing their own filters.
    """
    filtered: list[dict[str, Any]] = []
    for event in events:
        event_name = str(event.get("event"))
        if event_name in _NOISY_TIMELINE_EVENTS and not _is_high_signal_timeline_event(event):
            continue
        filtered.append(event)
    return filtered


def _is_high_signal_timeline_event(event: dict[str, Any]) -> bool:
    """Return True for otherwise-noisy events that affect lifecycle semantics."""
    event_name = str(event.get("event"))
    if event_name != "issue.labels_changed":
        return False
    removed = event.get("removed")
    if not isinstance(removed, list):
        return False
    return any(
        isinstance(label, str) and label.split(":", 1)[0] == "pr-pending"
        for label in removed
    )


def _decorate_timeline_events(events: list[dict[str, Any]], issue_number: int) -> list[dict[str, Any]]:
    decorated: list[dict[str, Any]] = []
    for event in events:
        event_with_actions = dict(event)
        if detail_value_kinds := timeline_detail_value_kinds(event):
            event_with_actions[DETAIL_VALUE_KINDS_KEY] = detail_value_kinds
        event_with_actions["actions"] = _timeline_event_actions(event, issue_number)
        decorated.append(event_with_actions)
    return decorated


def _timeline_event_recommended_actions(
    *,
    event: dict[str, Any],
    event_name: str,
    issue_number: int,
    run_artifacts: TimelineRunArtifacts,
    agent_log_label: str = "View Session Recording",
    add_action: Callable[[dict[str, Any], str], None],
) -> None:
    """Add event-specific suggested actions."""
    available_run = available_run_artifacts(run_artifacts)
    if available_run is not None:
        review_report_action = _review_artifact_action_for_event(
            event=event,
            issue_number=issue_number,
            artifact_type="review_report",
        )
        if review_report_action is not None:
            action, dedupe = review_report_action
            add_action(action, dedupe)
        transcript_action = _review_transcript_action_for_event(
            event=event,
            event_name=event_name,
            issue_number=issue_number,
            run_artifacts=available_run,
        )
        if transcript_action is not None:
            action, dedupe = transcript_action
            add_action(action, dedupe)
        if event_name in _TIMELINE_START_EVENTS:
            session_action = _preferred_run_scoped_session_action(
                event=event,
                event_name=event_name,
                issue_number=issue_number,
                agent_log_label=agent_log_label,
                run_artifacts=available_run,
            )
            if session_action is not None:
                action, dedupe = session_action
                add_action(action, dedupe)
            add_action(
                {"type": "view_claude_log", "label": "View Claude Session Log", "issue_number": issue_number},
                f"claude:{issue_number}",
            )
        if event_name in _VALIDATION_DETAIL_EVENTS:
            add_action(
                {
                    "type": "open_validation_failure",
                    "label": "Validation Details",
                    "issue_number": issue_number,
                },
                f"validation-details:{issue_number}",
            )

    feedback_action = _review_feedback_action_for_event(
        event=event,
        event_name=event_name,
        issue_number=issue_number,
    )
    if feedback_action is not None:
        action, dedupe = feedback_action
        add_action(action, dedupe)
    if available_run is not None and event_name.startswith("validation."):
        add_action(
            {
                "type": "open_orchestrator_log",
                "label": "Open Orchestrator Log for This Issue ↗",
                "issue_number": issue_number,
            },
            f"orchestrator:{issue_number}",
        )
    if available_run is not None and event_name in _TIMELINE_FAILURE_EVENTS:
        add_action(
            {
                "type": "open_session_diagnostics",
                "label": "Diagnostics…",
                "issue_number": issue_number,
            },
            f"diagnostics:{issue_number}",
        )


def _review_feedback_action_for_event(
    *,
    event: dict[str, Any],
    event_name: str,
    issue_number: int,
) -> tuple[dict[str, Any], str] | None:
    """Return a feedback action only for timeline rows with event-specific review content."""
    feedback_event = review_feedback_event_name(
        event_name,
        reviewer_response_text=event.get("reviewer_response_text"),
    )
    if feedback_event is None:
        return None
    timestamp = str(event.get("timestamp") or "").strip()
    round_index = event.get("round_index")
    base_action: dict[str, Any] = {
        "type": "open_review_feedback",
        "label": "View Review Feedback",
        "issue_number": issue_number,
        "feedback_event": feedback_event.value,
    }
    if timestamp:
        base_action["event_timestamp"] = timestamp
    if isinstance(round_index, int):
        base_action["round_index"] = round_index

    identity = timestamp or round_index or "event"
    return (
        base_action,
        f"review-feedback:{issue_number}:{feedback_event.value}:{identity}",
    )


def _review_artifact_action_for_event(
    *,
    event: dict[str, Any],
    issue_number: int,
    artifact_type: str,
) -> tuple[dict[str, Any], str] | None:
    for artifact in event.get("artifacts", []):
        if not isinstance(artifact, dict) or artifact.get("type") != artifact_type:
            continue
        value = str(artifact.get("value") or "")
        if not value:
            continue
        label = "Review report" if artifact_type == "review_report" else "Decision JSON"
        action = {
            "type": "open_review_artifact",
            "label": label,
            "issue_number": issue_number,
            "artifact_type": artifact_type,
            "artifact_path": value,
            "render_mode": str(artifact.get("render_mode") or ""),
        }
        if artifact_type == "review_report":
            action["primary"] = True
        return action, f"review-artifact:{artifact_type}:{value}"
    return None


def _timeline_event_artifact_actions(
    *,
    event: dict[str, Any],
    issue_number: int,
    run_artifacts: TimelineRunArtifacts,
    add_action: Callable[[dict[str, Any], str], None],
) -> None:
    """Add actions derived from timeline event artifacts and run directory."""
    available_run = available_run_artifacts(run_artifacts)
    for artifact in event.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("type") or "")
        label = str(artifact.get("label") or artifact_type or "Artifact")
        value = str(artifact.get("value") or "")
        if not value:
            continue
        url_artifact_kind = timeline_url_artifact_kind(artifact_type)
        if url_artifact_kind is not None:
            url = require_external_timeline_url(
                value=value,
                artifact_kind=url_artifact_kind,
                issue_number=issue_number,
            )
            add_action(
                {"type": "open_url", "label": f"Open {label} ↗", "url": url},
                url,
            )
            continue
        if available_run is None:
            continue
        if artifact_type in _REVIEW_ARTIFACT_TYPES:
            action = {
                "type": "open_review_artifact",
                "label": "Review report" if artifact_type == "review_report" else "Decision JSON",
                "issue_number": issue_number,
                "artifact_type": artifact_type,
                "artifact_path": value,
                "render_mode": str(artifact.get("render_mode") or ""),
            }
            if artifact_type == "review_report":
                action["primary"] = True
            add_action(action, f"review-artifact:{artifact_type}:{value}")
            continue
        local_artifact_kind = timeline_local_artifact_kind(artifact_type)
        if local_artifact_kind is not None:
            artifact_path = require_existing_timeline_artifact(
                run_artifacts=available_run,
                artifact_path=Path(value),
                artifact_kind=local_artifact_kind,
                issue_number=issue_number,
            )
            add_action(
                {"type": "open_path", "label": f"Open {label}", "path": str(artifact_path)},
                str(artifact_path),
            )

    if available_run is not None:
        run_dir = str(available_run.run_dir)
        add_action(
            {"type": "open_path", "label": "Open Run Dir", "path": run_dir},
            run_dir,
        )


def _timeline_event_default_actions(
    *,
    event: dict[str, Any],
    event_name: str,
    issue_number: int,
    run_artifacts: TimelineRunArtifacts,
    agent_log_label: str = "View Session Recording",
    include_run_scoped: bool = True,
    add_action: Callable[[dict[str, Any], str], None],
) -> None:
    """Add default diagnostics and log actions shown for every timeline event."""
    available_run = available_run_artifacts(run_artifacts)
    if available_run is None:
        return
    if include_run_scoped:
        session_action = _preferred_run_scoped_session_action(
            event=event,
            event_name=event_name,
            issue_number=issue_number,
            agent_log_label=agent_log_label,
            run_artifacts=available_run,
        )
        if session_action is not None:
            action, dedupe = session_action
            add_action(action, dedupe)
        add_action(
            {"type": "view_claude_log", "label": "View Claude Session Log", "issue_number": issue_number},
            f"claude:{issue_number}",
        )
    add_action(
        {
            "type": "open_orchestrator_log",
            "label": "Open Orchestrator Log for This Issue ↗",
            "issue_number": issue_number,
        },
        f"orchestrator:{issue_number}",
    )
    add_action(
        {
            "type": "open_session_diagnostics",
            "label": "Diagnostics…",
            "issue_number": issue_number,
        },
        f"diagnostics:{issue_number}",
    )


def _agent_log_is_usable(log_path: Path) -> bool:
    """Return True when run-scoped agent log should be exposed in timeline actions."""
    return log_path.exists()


def _validate_timeline_event_requirements(
    *,
    issue_number: int,
    event_name: str,
    timeline_schema_version: int,
) -> None:
    if timeline_schema_version < MIN_SUPPORTED_TIMELINE_SCHEMA_VERSION:
        raise RuntimeError(
            "timeline event has unsupported schema version: "
            f"issue={issue_number} event={event_name} version={timeline_schema_version} "
            f"min_supported={MIN_SUPPORTED_TIMELINE_SCHEMA_VERSION}"
        )


def _validated_run_scoped_artifact(
    *,
    action: dict[str, Any],
    issue_number: int,
    event_name: str,
    run_artifacts: AvailableRunArtifacts,
    run_scoped_validated: set[tuple[str, int | None, str | None]],
) -> str | None:
    action_type = str(action.get("type") or "")
    round_index = _positive_int(action.get("round_index"))
    session_role_raw = action.get("session_role")
    session_role = str(session_role_raw).strip() if isinstance(session_role_raw, str) else None
    validation_scope = str(action.get("artifact_path") or "") if action_type == "open_review_artifact" else session_role
    validation_key = (action_type, round_index, validation_scope)
    if validation_key in run_scoped_validated:
        return None
    event_run_dir = str(run_artifacts.run_dir)
    accessor = run_artifacts.recorded_run.artifacts
    if action_type == "open_agent_log":
        if round_index is not None and session_role:
            artifact = accessor.get_review_exchange_phase_terminal_recording(
                round_index=round_index,
                role=session_role,
                allow_empty=True,
            )
        else:
            artifact = accessor.get_agent_log(allow_empty=True)
        if not _agent_log_is_usable(artifact.path):
            raise RuntimeError(
                f"run-scoped agent log is empty/unusable: issue={issue_number} event={event_name} run_dir={event_run_dir}"
            )
    elif action_type == "open_review_transcript":
        artifact = accessor.get_review_exchange_transcript(allow_empty=True)
    elif action_type == "open_review_artifact":
        artifact_path = str(action.get("artifact_path") or "")
        artifact_type = str(action.get("artifact_type") or "")
        artifact = accessor.get_review_artifact(
            artifact_path=artifact_path,
            artifact_type=artifact_type,
        )
    elif action_type == "view_claude_log":
        artifact = accessor.get_claude_log()
    else:
        raise RuntimeError(
            f"unsupported run-scoped action type: issue={issue_number} event={event_name} action={action_type}"
        )
    if not artifact.path.exists():
        raise RuntimeError(
            f"resolved artifact path missing: issue={issue_number} event={event_name} action={action_type} run_dir={event_run_dir}"
        )
    run_scoped_validated.add(validation_key)
    return None


def _timeline_event_actions(event: dict[str, Any], issue_number: int) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    event_name = str(event.get("event") or "")
    agent_log_label = _agent_log_label_for_event(event)
    timeline_schema_version_raw = event.get("timeline_schema_version")
    timeline_schema_version = timeline_schema_version_raw if isinstance(timeline_schema_version_raw, int) else 0
    _validate_timeline_event_requirements(
        issue_number=issue_number,
        event_name=event_name,
        timeline_schema_version=timeline_schema_version,
    )
    run_artifacts = classify_timeline_run_artifacts(
        raw_run_dir=event.get("run_dir"),
        issue_number=issue_number,
        event_name=event_name,
    )
    available_run = available_run_artifacts(run_artifacts)
    run_directory_action_types = {
        "open_agent_log",
        "open_review_artifact",
        "open_review_transcript",
        "open_validation_failure",
        "view_claude_log",
    }
    manifest_artifact_action_types = run_directory_action_types - {
        "open_validation_failure"
    }
    run_scoped_validated: set[tuple[str, int | None, str | None]] = set()

    def _add_action(action: dict[str, Any], dedupe_value: str) -> None:
        action_type = str(action.get("type") or "")
        run_for_action: AvailableRunArtifacts | None = None
        if action_type in run_directory_action_types:
            if available_run is None:
                raise RuntimeError(
                    f"timeline run-scoped action lacks available artifacts: "
                    f"issue={issue_number} event={event_name} action={action_type}"
                )
            run_for_action = available_run
        if action_type in manifest_artifact_action_types:
            if run_for_action is None:
                raise AssertionError(
                    f"manifest action {action_type!r} lacks its run capability"
                )
            try:
                _validated_run_scoped_artifact(
                    action=action,
                    issue_number=issue_number,
                    event_name=event_name,
                    run_artifacts=run_for_action,
                    run_scoped_validated=run_scoped_validated,
                )
            except ArtifactNotFoundError as exc:
                if action_type == "view_claude_log" and str(exc).strip() in {
                    "manifest missing claude_log_path",
                    "manifest missing claude log candidates",
                }:
                    return
                raise
        if available_run is not None and action_type in {
            *run_directory_action_types,
            "open_orchestrator_log",
            "open_session_diagnostics",
        }:
            action = {**action, "run_dir": str(available_run.run_dir)}
        key = (action_type, dedupe_value)
        if key in seen:
            return
        seen.add(key)
        actions.append(action)

    _timeline_event_recommended_actions(
        event=event,
        event_name=event_name,
        issue_number=issue_number,
        run_artifacts=run_artifacts,
        agent_log_label=agent_log_label,
        add_action=_add_action,
    )
    _timeline_event_artifact_actions(
        event=event,
        issue_number=issue_number,
        run_artifacts=run_artifacts,
        add_action=_add_action,
    )
    is_agent_event = _is_agent_scoped_event(event, event_name)
    _timeline_event_default_actions(
        event=event,
        event_name=event_name,
        issue_number=issue_number,
        run_artifacts=run_artifacts,
        agent_log_label=agent_log_label,
        include_run_scoped=available_run is not None and is_agent_event,
        add_action=_add_action,
    )
    return actions


def _is_agent_scoped_event(event: dict[str, Any], event_name: str) -> bool:
    """Return True when an event represents agent work."""
    if event_name in _ORCHESTRATOR_ONLY_EVENTS:
        return False
    intent = str(event.get("event_intent") or "")
    if intent in {"coding", "review", "rework"}:
        return True
    return (
        is_review_event_name(event_name)
        or is_rework_event_name(event_name)
        or event_name.startswith("agent.")
        or event_name.startswith("review_exchange.")
    )


def _event_supports_review_transcript(event: dict[str, Any], event_name: str) -> bool:
    """Return True when a review event has structured exchange transcript context."""
    return _is_review_oriented_timeline_event(event, event_name)


def _is_review_oriented_timeline_event(event: dict[str, Any], event_name: str) -> bool:
    """Return True when timeline presentation should treat an event as review work."""
    task = str(event.get("task") or "").strip().lower()
    intent = str(event.get("event_intent") or "")
    return (
        intent == EventIntent.REVIEW.value
        or bool(event.get("review_oriented"))
        or event_name.startswith("review_exchange.")
        or is_review_oriented_event(event_name=event_name, task=task)
    )


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _review_transcript_context_for_event(event: dict[str, Any], event_name: str) -> dict[str, Any]:
    """Return transcript filtering context for a timeline event."""
    round_index = _positive_int(event.get("round_index"))
    rounds = _positive_int(event.get("rounds"))
    if event_name in {"review_exchange.round_started", "review_exchange.round_completed"} and round_index:
        return {"round_index": round_index, "transcript_role": "reviewer"}
    if event_name in {
        "review_exchange.role_prompted",
        "review_exchange.role_feedback",
        "review_exchange.role_timeout",
    } and round_index:
        role = str(event.get("role") or "").strip()
        if role in {"coder", "reviewer"}:
            return {"round_index": round_index, "transcript_role": role}
    if event_name in {"review.rework_started", "review.rework_completed"} and round_index:
        return {"round_index": round_index, "transcript_role": "coder"}
    if event_name in {"review.approved", "review.changes_requested"}:
        final_round = round_index or rounds
        if final_round:
            return {"round_index": final_round, "transcript_role": "reviewer"}
    return {}


def _agent_log_context_for_event(event: dict[str, Any], event_name: str) -> dict[str, Any]:
    """Return phase-specific session-recording context for a timeline event."""
    round_index = _positive_int(event.get("round_index"))
    rounds = _positive_int(event.get("rounds"))
    if event_name in {"review_exchange.round_started", "review_exchange.round_completed"} and round_index:
        return {"round_index": round_index, "session_role": "reviewer"}
    if event_name in {
        "review_exchange.role_prompted",
        "review_exchange.role_feedback",
        "review_exchange.role_timeout",
    } and round_index:
        role = str(event.get("role") or "").strip()
        if role in {"coder", "reviewer"}:
            return {"round_index": round_index, "session_role": role}
    if event_name in {"review.rework_started", "review.rework_completed"} and round_index:
        return {"round_index": round_index, "session_role": "coder"}
    if event_name in {"review.approved", "review.changes_requested"}:
        final_round = round_index or rounds
        if final_round:
            return {"round_index": final_round, "session_role": "reviewer"}
    return {}


def _review_transcript_action_for_event(
    *,
    event: dict[str, Any],
    event_name: str,
    issue_number: int,
    run_artifacts: AvailableRunArtifacts,
) -> tuple[dict[str, Any], str] | None:
    """Expose the structured exchange transcript as secondary review context."""
    if not _event_supports_review_transcript(event, event_name):
        return None
    accessor = run_artifacts.recorded_run.artifacts
    try:
        accessor.get_review_exchange_transcript(allow_empty=True)
    except (ArtifactNotFoundError, FileNotFoundError):
        return None
    action: dict[str, Any] = {
        "type": "open_review_transcript",
        "label": "View Review Transcript",
        "issue_number": issue_number,
    }
    action.update(_review_transcript_context_for_event(event, event_name))
    dedupe_parts = ["review-transcript", str(issue_number), event_name]
    round_index = action.get("round_index")
    transcript_role = action.get("transcript_role")
    if isinstance(round_index, int):
        dedupe_parts.append(str(round_index))
    if isinstance(transcript_role, str) and transcript_role:
        dedupe_parts.append(transcript_role)
    return action, ":".join(dedupe_parts)


def _preferred_run_scoped_session_action(
    *,
    event: dict[str, Any],
    event_name: str,
    issue_number: int,
    agent_log_label: str,
    run_artifacts: AvailableRunArtifacts,
) -> tuple[dict[str, Any], str] | None:
    """Resolve the truthful primary run-scoped session artifact for an event."""
    run_dir = str(run_artifacts.run_dir)
    action: dict[str, Any] = {
        "type": "open_agent_log",
        "label": agent_log_label,
        "issue_number": issue_number,
    }
    context = _agent_log_context_for_event(event, event_name)
    if not context and _is_review_exchange_aggregate_event(event, event_name):
        return None
    if context:
        accessor = run_artifacts.recorded_run.artifacts
        try:
            accessor.get_review_exchange_phase_terminal_recording(
                round_index=int(context["round_index"]),
                role=str(context["session_role"]),
                allow_empty=True,
            )
        except ArtifactNotFoundError as exc:
            if event_requires_run_dir(event_name):
                role = str(context["session_role"])
                round_index = int(context["round_index"])
                raise RuntimeError(
                    "required Timeline phase recording is missing: "
                    f"issue={issue_number} event={event_name} "
                    f"run_dir={run_dir} round_index={round_index} role={role}; {exc}"
                ) from exc
            return None
        action.update(context)
    dedupe_parts = ["agent", str(issue_number)]
    round_index = action.get("round_index")
    session_role = action.get("session_role")
    if isinstance(round_index, int):
        dedupe_parts.append(str(round_index))
    if isinstance(session_role, str) and session_role:
        dedupe_parts.append(session_role)
    return action, ":".join(dedupe_parts)


def _is_review_exchange_aggregate_event(event: dict[str, Any], event_name: str) -> bool:
    """Return True for review-exchange rows that are not role sessions."""
    exchange_mode = str(event.get("review_exchange_mode") or "").strip()
    return event_name == "review_exchange.completed" or (
        event_name == "review.started"
        and exchange_mode in {"via-local-loop", "via-mcp"}
    )


def _timeline_event_requires_run_dir(event: dict[str, Any]) -> bool:
    """Return True when a timeline event is expected to be tied to a run directory."""
    event_name = str(event.get("event") or "")
    return event_requires_run_dir(event_name)


def _agent_log_label_for_event(event: dict[str, Any]) -> str:
    """Describe which session log the user will see for this event."""
    event_name = str(event.get("event") or "")
    task = str(event.get("task") or "").strip().lower()
    intent = str(event.get("event_intent") or "")
    if _is_review_oriented_timeline_event(event, event_name):
        return "View Reviewer Session Recording"
    if intent == EventIntent.REWORK.value or is_rework_event_name(event_name) or task == "rework":
        return "View Rework Session Recording"
    if intent == EventIntent.CODING.value or is_session_event_name(event_name) or task in {"code", "coding"}:
        return "View Coding Session Recording"
    return "View Session Recording"


def _format_phase_name(phase_name: str) -> str:
    """Format phase name for display (e.g., 'coding-1' -> 'Coding 1')."""
    if not phase_name:
        return "Unknown"
    parts = phase_name.split("-")
    if len(parts) == 2:
        name, num = parts
        return f"{name.title()} {num}"
    return phase_name.replace("-", " ").title()


def _phase_status_icon(status: str) -> str:
    """Return status icon for a phase."""
    icons = {
        "completed": "✓",
        "in_progress": "●",
        "validation_failed": "✗",
        "blocked": "✗",
        "timeout": "✗",
        "timed_out": "✗",
        "unknown": "○",
    }
    return icons.get(status, "○")


__all__ = [
    "_build_phase_toc",
    "_build_timeline_cycles",
    "_decorate_timeline_events",
    "_filter_timeline_events",
    "_format_phase_name",
    "_is_agent_scoped_event",
    "_phase_status_icon",
    "_positive_int",
    "_promote_e2e_test_event_fields",
    "_retain_semantic_timeline_events",
    "_timeline_event_default_actions",
    "_timeline_event_actions",
    "_timeline_event_recommended_actions",
    "_timeline_event_requires_run_dir",
]
