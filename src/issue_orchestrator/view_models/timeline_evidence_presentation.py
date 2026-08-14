"""Presentation policy for exact-run Timeline evidence."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from ..domain.timeline_evidence import (
    TimelineEvidenceIdentity,
    TimelineEvidenceState,
    TimelineEvidenceStatus,
)
from ..domain.run_manifest import RunManifest
from ..ports.timeline_evidence import TimelineEvidence

logger = logging.getLogger(__name__)


class TimelineEvidencePublicState(TypedDict):
    """Stable serialized evidence state shared with Timeline UI contracts."""

    run_dir: str
    status: str
    label: str
    available: bool
    pinned: bool
    archived: bool
    expires_at: str | None
    help_text: str
    unpin_expires_immediately: bool


class TimelineEvidencePinAction(TypedDict):
    """Exact-run pin command rendered in a Timeline action menu."""

    type: Literal["set_timeline_evidence_pin"]
    label: str
    issue_number: int
    run_dir: str
    pinned: bool
    confirm_message: str


class TimelineEvidenceEventFields(TypedDict, total=False):
    """Evidence fields added to a decorated Timeline event."""

    evidence: TimelineEvidencePublicState


@dataclass(frozen=True)
class TimelineEventBatch:
    """Typed boundary around the legacy Timeline event mapping sequence."""

    events: list[dict[str, Any]]


@dataclass(frozen=True)
class TimelineEvidencePresentation:
    """Evidence fields plus the action policy for one Timeline row."""

    state: TimelineEvidenceState | None = None

    @property
    def event_fields(self) -> TimelineEvidenceEventFields:
        return (
            {"evidence": timeline_evidence_public_state(self.state)}
            if self.state
            else {}
        )

    def actions(
        self,
        run_actions: Callable[[], Sequence[Mapping[str, Any]]],
        *,
        include_pin: bool = True,
    ) -> list[Mapping[str, Any]]:
        if self.state is None:
            return list(run_actions())
        actions = list(run_actions()) if self.state.available else []
        if self.state.archived and not _archived_orchestrator_log_available(
            self.state
        ):
            actions = [
                action
                for action in actions
                if action.get("type") != "open_orchestrator_log"
            ]
        if include_pin and self.state.status in {
            TimelineEvidenceStatus.RETAINED,
            TimelineEvidenceStatus.PINNED,
        }:
            actions.append(self._pin_action())
        return actions

    def _pin_action(self) -> TimelineEvidencePinAction:
        if self.state is None:
            raise RuntimeError("Pin actions require Timeline evidence state")
        return {
            "type": "set_timeline_evidence_pin",
            "label": (
                "Unpin Timeline Evidence"
                if self.state.pinned
                else "Pin Timeline Evidence"
            ),
            "issue_number": self.state.identity.issue_number,
            "run_dir": str(self.state.identity.run_dir),
            "pinned": not self.state.pinned,
            "confirm_message": (
                "This retention window has already elapsed. "
                "Unpinning will remove this run's Timeline evidence now."
                if self.state.unpin_expires_immediately
                else ""
            ),
        }


def _archived_orchestrator_log_available(state: TimelineEvidenceState) -> bool:
    """Return whether an archived run owns a readable local log tail."""
    run_dir = state.identity.run_dir
    try:
        tail_value = RunManifest.load(run_dir).orchestrator_tail
        if not tail_value:
            return False
        tail_path = Path(tail_value).resolve(strict=True)
        tail_path.relative_to(run_dir.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return tail_path.is_file()


def present_timeline_evidence(
    event: Mapping[str, Any],
    issue_number: int,
    owner: TimelineEvidence,
) -> TimelineEvidencePresentation:
    """Resolve optional evidence state without breaking ordinary row actions."""
    run_dir = event.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir.strip():
        return TimelineEvidencePresentation()
    try:
        state = owner.describe(TimelineEvidenceIdentity(issue_number, Path(run_dir)))
    except Exception as exc:
        logger.warning(
            "Timeline evidence decoration failed (issue=%s run_dir=%s): %s",
            issue_number,
            run_dir,
            exc,
        )
        return TimelineEvidencePresentation(
            state=TimelineEvidenceState(
                identity=TimelineEvidenceIdentity(issue_number, Path(run_dir)),
                status=TimelineEvidenceStatus.MISSING,
                label="Evidence unavailable",
                available=False,
                pinned=False,
                archived=False,
                help_text="The retained evidence state could not be read.",
            )
        )
    return TimelineEvidencePresentation(
        state=state if isinstance(state, TimelineEvidenceState) else None
    )


def timeline_evidence_public_state(
    state: TimelineEvidenceState,
) -> TimelineEvidencePublicState:
    """Serialize domain retention state at the UI presentation boundary."""
    return {
        "run_dir": str(state.identity.run_dir),
        "status": state.status.value,
        "label": state.label,
        "available": state.available,
        "pinned": state.pinned,
        "archived": state.archived,
        "expires_at": state.expires_at,
        "help_text": state.help_text,
        "unpin_expires_immediately": state.unpin_expires_immediately,
    }


def attach_timeline_evidence(
    batch: TimelineEventBatch,
    issue_number: int,
    owner: TimelineEvidence,
) -> TimelineEventBatch:
    """Attach one visible retention state per run after action decoration."""
    presentations: dict[str, TimelineEvidencePresentation] = {}
    last_event_by_run: dict[str, int] = {}
    for index, event in enumerate(batch.events):
        run_dir = event.get("run_dir")
        if not isinstance(run_dir, str) or not run_dir.strip():
            continue
        last_event_by_run[run_dir] = index
        if run_dir not in presentations:
            presentations[run_dir] = present_timeline_evidence(
                event, issue_number, owner
            )

    decorated: list[dict[str, Any]] = []
    for index, event in enumerate(batch.events):
        event_with_evidence = dict(event)
        run_dir_value = event.get("run_dir")
        run_dir = run_dir_value if isinstance(run_dir_value, str) else ""
        presentation = presentations.get(run_dir) or TimelineEvidencePresentation()
        is_latest_run_event = bool(run_dir) and last_event_by_run.get(run_dir) == index
        if is_latest_run_event:
            event_with_evidence.update(presentation.event_fields)
        event_with_evidence["actions"] = presentation.actions(
            lambda: _existing_actions(event),
            include_pin=is_latest_run_event,
        )
        decorated.append(event_with_evidence)
    return TimelineEventBatch(decorated)


def scope_timeline_actions_to_repository(
    batch: TimelineEventBatch,
    repo_root: Path,
) -> TimelineEventBatch:
    """Carry Control Center repository scope on every Timeline command."""
    scoped_events: list[dict[str, Any]] = []
    for event in batch.events:
        scoped_event = dict(event)
        scoped_actions: list[Mapping[str, Any]] = []
        for action in _existing_actions(event):
            scoped_action = dict(action)
            scoped_action["repo_root"] = str(repo_root)
            scoped_actions.append(scoped_action)
        scoped_event["actions"] = scoped_actions
        scoped_events.append(scoped_event)
    return TimelineEventBatch(scoped_events)


def _existing_actions(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    actions = event.get("actions")
    if not isinstance(actions, list):
        return []
    return [action for action in actions if isinstance(action, Mapping)]
