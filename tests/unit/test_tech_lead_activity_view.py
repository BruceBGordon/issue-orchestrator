"""The dashboard's tech-lead activity projection (ADR-0033 / #6858).

The run record is only half of "local visibility": an operator has to be able
to SEE it. These tests pin the producer→payload half of the command surface —
that a recorded run reaches the dashboard payload with a colour-independent
phase, a subject shown as a reference, and the run identity a drill-down needs.
The payload→rendered-output half lives in ``tests/js/tech_lead_activity.test.js``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from issue_orchestrator.contracts.public import (
    DashboardDataContract,
    TechLeadActivityContract,
)
from issue_orchestrator.domain.tech_lead_run import TechLeadRunScopeKind
from issue_orchestrator.domain.tech_lead_run_artifacts import (
    TechLeadRunArtifactKind,
    TechLeadRunArtifacts,
)
from issue_orchestrator.domain.tech_lead_run_record import (
    TechLeadRunPhase,
    TechLeadRunRecord,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.ports.tech_lead_run_record_store import (
    NO_TECH_LEAD_RUN_HISTORY,
    InMemoryTechLeadRunRecordStore,
)
from issue_orchestrator.view_models.tech_lead_activity import (
    ARTIFACTS_ABSENT_NOTE,
    ARTIFACTS_PENDING_NOTE,
    EMPTY_MESSAGE,
    read_tech_lead_activity,
)

STARTED = datetime(2026, 8, 9, 9, 0, 0)
ENDED = datetime(2026, 8, 9, 9, 30, 0)


def _record(**overrides) -> TechLeadRunRecord:
    fields = {
        "run_key": "global:health_review",
        "scope_kind": TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
        "flavor": TechLeadSessionFlavor.HEALTH_REVIEW,
        "phase": TechLeadRunPhase.RUNNING,
        "started_at": STARTED,
        "run_id": "run-900",
        "session_name": "tech-lead-900",
        "anchor_issue_number": 900,
    }
    fields.update(overrides)
    return TechLeadRunRecord(**fields)


def _view(*records):
    store = InMemoryTechLeadRunRecordStore()
    for record in records:
        store.open_run(record)
    return read_tech_lead_activity(store)


def test_an_engine_with_no_history_publishes_its_own_empty_sentence():
    """Published, not hardcoded in the browser: the UI must not have to invent
    words for a state it did not observe."""
    view = read_tech_lead_activity(NO_TECH_LEAD_RUN_HISTORY)

    assert view.entries == ()
    assert view.empty_message == EMPTY_MESSAGE


def test_a_recorded_run_reaches_the_payload_with_a_readable_flavor():
    view = _view(_record())

    (entry,) = view.entries
    assert entry.flavor_label == "Health review"
    assert entry.run_key == "global:health_review"


@pytest.mark.parametrize(
    "phase,label,tone",
    [
        (TechLeadRunPhase.RUNNING, "Running", "active"),
        (TechLeadRunPhase.COMPLETED, "Completed", "good"),
        (TechLeadRunPhase.NEEDS_HUMAN, "Needs human", "warn"),
        (TechLeadRunPhase.FAILED, "Failed", "bad"),
        (TechLeadRunPhase.WITHDRAWN, "Withdrawn", "muted"),
    ],
)
def test_every_phase_carries_text_as_well_as_a_tone(phase, label, tone):
    """Colour is never the only status signal — the label is always populated."""
    terminal = {"ended_at": ENDED} if phase.is_terminal else {}
    view = _view(_record(phase=phase, **terminal))

    (entry,) = view.entries
    assert (entry.phase, entry.phase_label, entry.tone) == (phase.value, label, tone)
    assert entry.phase_label


def test_a_whole_board_run_advertises_the_board_as_its_subject():
    """A health review's subject is the board, and the anchor it was coordinated
    through is published separately AS an anchor (#6858 F5). Claiming the anchor
    is the subject points the operator at the wrong thing."""
    view = _view(_record())

    (entry,) = view.entries
    assert entry.subject_kind == "board"
    assert entry.subject_label == "Whole board"
    assert entry.subject_issue_number == 0
    assert entry.anchor_issue_number == 900


def test_a_batch_review_advertises_the_pr_manifest_as_its_subject():
    view = _view(
        _record(
            run_key="global:batch_review",
            scope_kind=TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW,
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        )
    )

    (entry,) = view.entries
    assert (entry.subject_kind, entry.subject_label) == ("pr_manifest", "PR manifest")
    assert entry.subject_issue_number == 0


def test_a_focused_investigation_references_its_subject_issue():
    view = _view(
        _record(
            run_key="issue:42",
            scope_kind=TechLeadRunScopeKind.ISSUE,
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            run_id="run-42",
            session_name="tech-lead-42",
            subject_issue_number=42,
            subject_title="Flaky merge queue",
            anchor_issue_number=42,
        )
    )

    (entry,) = view.entries
    assert entry.flavor_label == "Failure investigation"
    assert entry.subject_kind == "issue"
    assert entry.subject_label == "#42 Flaky merge queue"
    assert entry.subject_issue_number == 42
    assert entry.subject_title == "Flaky merge queue"


def test_a_running_run_publishes_an_empty_end_time_rather_than_a_guess():
    view = _view(_record())

    (entry,) = view.entries
    assert entry.started_at == STARTED.isoformat()
    assert entry.ended_at == ""


def test_the_payload_carries_the_drill_down_identity():
    """The UI must never have to reconstruct how run artifacts are addressed."""
    view = _view(_record())

    (entry,) = view.entries
    assert (entry.run_id, entry.session_name) == ("run-900", "tech-lead-900")


def test_the_serialized_payload_satisfies_the_public_contract():
    """Producer→payload: the dashboard blob the browser reads is contracted."""
    view = _view(
        _record(
            phase=TechLeadRunPhase.COMPLETED,
            ended_at=ENDED,
            detail="Two hotspots are past budget",
            findings=2,
            proposals=1,
        )
    )

    payload = view.model_dump(mode="json", by_alias=True)
    contract = TechLeadActivityContract.model_validate(payload)

    assert contract.entries[0].phaseLabel == "Completed"
    assert contract.entries[0].findings == 2
    assert contract.entries[0].proposals == 1
    assert contract.entries[0].detail == "Two hotspots are past budget"


def test_the_dashboard_contract_requires_the_activity_payload():
    """A dropped producer value must fail loudly rather than read as "the tech
    lead has never run"."""
    with pytest.raises(ValueError):
        DashboardDataContract.model_validate(
            {
                "startupComplete": True,
                "paused": False,
                "e2eRunning": False,
                "queueRefreshSeconds": 60,
                "repo": "o/r",
                "repoRoot": "/tmp/r",
                "githubOwner": "o",
                "githubRepo": "r",
                "agents": [],
                "validationConfigured": True,
                "providerCircuit": {
                    "any_open": False,
                    "open_count": 0,
                    "summary_text": "",
                },
                "techLeadRuns": {
                    "configured": True,
                    "paused": False,
                    "globalStatus": "idle",
                    "globalStatusLabel": "",
                    "healthReviewStatus": "idle",
                    "healthReviewStatusLabel": "",
                    "globalBarrierActive": False,
                },
            }
        )


def _preserved(tmp_path, *kinds) -> TechLeadRunArtifacts:
    location = tmp_path / "archive" / "run-900__tech-lead-900"
    location.mkdir(parents=True, exist_ok=True)
    return TechLeadRunArtifacts(location=location, kinds=kinds)


def _concluded(tmp_path, *kinds):
    return _record(
        phase=TechLeadRunPhase.COMPLETED,
        ended_at=ENDED,
        artifacts=_preserved(tmp_path, *kinds),
    )


class TestTheDrillDownIsAPublishedCommand:
    """#6858 F4/A1: local visibility means the ARTIFACTS are reachable.

    The panel publishes the dashboard's EXISTING typed lifecycle commands, built
    from the PRESERVED location the archive owner recorded. The browser therefore
    never reconstructs a path, and never derives one from runId/sessionName —
    which it could not do anyway, since the run's own directory was deleted with
    its worktree.
    """

    def test_a_preserved_replay_publishes_the_session_recording_command(
        self, tmp_path
    ):
        view = _view(
            _concluded(tmp_path, TechLeadRunArtifactKind.SESSION_REPLAY)
        )

        (entry,) = view.entries
        (command,) = entry.artifacts
        assert command.kind == "open_session_recording"
        assert command.label == "Session replay"
        # Keyed by the run's anchor, and pointed at the PRESERVED directory.
        assert command.issue_number == 900
        assert command.run_dir.endswith("run-900__tech-lead-900")

    def test_a_preserved_pair_publishes_report_and_decision_commands(self, tmp_path):
        view = _view(
            _concluded(
                tmp_path,
                TechLeadRunArtifactKind.REPORT,
                TechLeadRunArtifactKind.DECISION,
            )
        )

        (entry,) = view.entries
        report, decision = entry.artifacts
        assert (report.kind, report.label) == ("open_review_artifact", "Report")
        assert report.artifact_type == "tech_lead_report"
        assert report.artifact_path == "tech-lead-data/tech-lead-report.md"
        assert report.render_mode == "markdown"
        assert decision.artifact_type == "tech_lead_decision"
        assert decision.artifact_path == "tech-lead-data/tech-lead-decision.json"
        assert decision.render_mode == "json"

    def test_only_preserved_kinds_are_offered(self, tmp_path):
        """A run that died before writing a decision must not offer a button
        that 404s."""
        view = _view(_concluded(tmp_path, TechLeadRunArtifactKind.SESSION_REPLAY))

        (entry,) = view.entries
        assert [command.kind for command in entry.artifacts] == [
            "open_session_recording"
        ]
        assert entry.artifacts_note == ""

    def test_a_running_run_says_artifacts_are_not_preserved_yet(self):
        view = _view(_record())

        (entry,) = view.entries
        assert entry.artifacts == ()
        assert entry.artifacts_note == ARTIFACTS_PENDING_NOTE

    def test_a_finished_run_with_nothing_preserved_says_so(self):
        """"Not yet" and "never" are different facts, so they are two sentences."""
        view = _view(_record(phase=TechLeadRunPhase.FAILED, ended_at=ENDED))

        (entry,) = view.entries
        assert entry.artifacts == ()
        assert entry.artifacts_note == ARTIFACTS_ABSENT_NOTE

    def test_the_serialized_commands_satisfy_the_public_contract(self, tmp_path):
        """Producer→payload for the drill-down itself, not just the summary."""
        view = _view(
            _concluded(
                tmp_path,
                TechLeadRunArtifactKind.SESSION_REPLAY,
                TechLeadRunArtifactKind.REPORT,
            )
        )

        payload = view.model_dump(mode="json", by_alias=True)
        contract = TechLeadActivityContract.model_validate(payload)

        commands = contract.entries[0].artifacts
        assert [command.kind for command in commands] == [
            "open_session_recording",
            "open_review_artifact",
        ]
        assert commands[1].artifact_type == "tech_lead_report"
        assert all(command.run_dir for command in commands)
