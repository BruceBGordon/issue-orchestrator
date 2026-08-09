"""A tech-lead run leaves a LOCAL record of itself (ADR-0033 / #6858).

The shared ledger (#6994) settles who OWNS a run; nothing settled what happened
to it. A run executed as a session on a bookkeeping anchor, and once that anchor
closed the only surviving trace of what the tech lead saw, decided and filed was
an issue on the *client's* board.

These tests drive the two real seams — the single launch authority and
completion finalization — and observe the record through the store, so "the run
is remembered" is a fact about durable state rather than an assertion about a
mock. The clock is injected; nothing sleeps.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.completion_types import ERROR_PREFIX_PUSH
from issue_orchestrator.control.tech_lead_run_activity import TechLeadRunActivity
from issue_orchestrator.domain.models import SessionStatus
from issue_orchestrator.domain.tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
)
from issue_orchestrator.domain.tech_lead_run import TechLeadRunScopeKind
from issue_orchestrator.domain.tech_lead_run_record import (
    TechLeadRunPhase,
    TechLeadRunRecord,
    TechLeadRunSubjectKind,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.domain.tech_lead_run_artifacts import (
    TechLeadRunArtifactKind,
)
from issue_orchestrator.infra.tech_lead_run_artifact_archive import (
    FileSystemTechLeadRunArtifactArchive,
)
from issue_orchestrator.infra.tech_lead_run_record_store import (
    SqliteTechLeadRunRecordStore,
)
from issue_orchestrator.ports.tech_lead_run_artifact_archive import (
    DiscardedTechLeadRunArtifacts,
)
from issue_orchestrator.ports.tech_lead_run_record_store import (
    InMemoryTechLeadRunRecordStore,
)

TECH_LEAD_AGENT = "agent:tech-lead"
STARTED = datetime(2026, 8, 9, 9, 0, 0)
ENDED = datetime(2026, 8, 9, 9, 30, 0)


class FakeIssue:
    def __init__(self, number: int) -> None:
        self.number = number
        self.title = f"Issue #{number}"
        self.agent_type = TECH_LEAD_AGENT


class FakeSession:
    """The session surface the activity owner reads."""

    def __init__(
        self,
        issue_number: int,
        flavor: TechLeadSessionFlavor | None,
        *,
        run_dir: Path,
    ) -> None:
        self.issue = FakeIssue(issue_number)
        self.terminal_id = f"tech-lead-{issue_number}"
        self.started_at = STARTED
        self.run_dir = run_dir
        self.run_assets = SimpleNamespace(
            run_id=f"run-{issue_number}",
            session_name=f"tech-lead-{issue_number}",
        )
        self.tech_lead_scope = (
            TechLeadLaunchScope(flavor=flavor) if flavor is not None else None
        )


def _activity(store, archive=None) -> TechLeadRunActivity:
    return TechLeadRunActivity(
        store,
        archive if archive is not None else DiscardedTechLeadRunArtifacts(),
        now=lambda: ENDED,
    )


def _session(tmp_path: Path, number: int, flavor) -> FakeSession:
    run_dir = tmp_path / f"run-{number}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return FakeSession(number, flavor, run_dir=run_dir)


# A valid decision artifact pair: one finding, one proposal, and a report that
# mentions both — the loader validates the PAIR, not just the JSON.
DECISION = {
    "schema_version": 1,
    "summary": "Two hotspots are past budget",
    "findings": [
        {
            "id": "T1",
            "title": "Provider outage stalled sessions",
            "classification": "infra",
            "evidence": ["log:orchestrator.log:1023"],
        }
    ],
    "proposed_actions": [
        {
            "id": "A1",
            "action_type": "post_comment",
            "target_number": 900,
            "body": "Diagnosis: two hotspots are past budget.",
            "finding_ids": ["T1"],
        }
    ],
}
REPORT = "The review recorded T1 and proposes A1."


def _write_decision(session: FakeSession) -> None:
    data_dir = Path(session.run_dir) / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / TECH_LEAD_DECISION_FILENAME).write_text(
        json.dumps(DECISION), encoding="utf-8"
    )
    (data_dir / TECH_LEAD_REPORT_FILENAME).write_text(REPORT, encoding="utf-8")


class TestARunIsRemembered:
    def test_a_started_health_review_appears_as_a_running_run(self, tmp_path):
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 900, TechLeadSessionFlavor.HEALTH_REVIEW)

        _activity(store).note_started(session)

        (record,) = store.recent(limit=10)
        assert record.run_key == "global:health_review"
        assert record.scope_kind is TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW
        assert record.phase is TechLeadRunPhase.RUNNING
        assert record.ended_at is None

    def test_the_record_references_its_subject_rather_than_owning_it(self, tmp_path):
        """A focused investigation names the issue it is ABOUT.

        The distinction matters on a client's board: the run is ours, the issue
        is theirs, so the record points at it and never claims to be it.
        """
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 42, TechLeadSessionFlavor.FAILURE_INVESTIGATION)

        _activity(store).note_started(session)

        (record,) = store.recent(limit=10)
        assert record.run_key == "issue:42"
        assert record.subject_issue_number == 42
        assert record.subject_title == "Issue #42"
        # For a focused investigation the anchor IS the subject.
        assert record.anchor_issue_number == 42

    @pytest.mark.parametrize(
        "flavor,subject_kind",
        [
            (TechLeadSessionFlavor.HEALTH_REVIEW, TechLeadRunSubjectKind.BOARD),
            (TechLeadSessionFlavor.BATCH_REVIEW, TechLeadRunSubjectKind.PR_MANIFEST),
        ],
    )
    def test_a_global_run_records_its_anchor_as_an_anchor_not_a_subject(
        self, tmp_path, flavor, subject_kind
    ):
        """Through the REAL owner seam, not a hand-built record (#6858 F5).

        A whole-repository run executes as a session on a bookkeeping anchor
        issue. Recording that anchor as the run's subject made every health and
        batch review read as an investigation OF its own bookkeeping — the exact
        coordination/visibility confusion ADR-0033 splits by owner.
        """
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 900, flavor)

        _activity(store).note_started(session)

        (record,) = store.recent(limit=10)
        assert record.subject_kind is subject_kind
        assert record.subject_issue_number == 0
        assert record.subject_title == ""
        assert record.anchor_issue_number == 900

    def test_a_session_with_no_launch_stamp_is_not_recorded(self, tmp_path):
        """A run that cannot name itself is noise, not evidence."""
        store = InMemoryTechLeadRunRecordStore()

        _activity(store).note_started(_session(tmp_path, 7, None))

        assert store.recent(limit=10) == ()

    def test_the_record_carries_the_session_run_identity_for_drill_down(
        self, tmp_path
    ):
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 900, TechLeadSessionFlavor.BATCH_REVIEW)

        _activity(store).note_started(session)

        (record,) = store.recent(limit=10)
        assert record.run_id == "run-900"
        assert record.session_name == "tech-lead-900"


class TestARunIsConcluded:
    def test_a_completed_run_records_what_its_decision_produced(self, tmp_path):
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 900, TechLeadSessionFlavor.HEALTH_REVIEW)
        _write_decision(session)
        activity = _activity(store)
        activity.note_started(session)

        activity.note_concluded(session, SessionStatus.COMPLETED)

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.COMPLETED
        assert record.ended_at == ENDED
        assert record.detail == "Two hotspots are past budget"
        assert (record.findings, record.proposals) == (1, 1)

    def test_a_rejected_decision_is_recorded_as_failed_not_completed(self, tmp_path):
        """The orchestrator threw the decision out, so the run produced nothing.

        Recording it as completed would contradict the actions the very same
        completion planned.
        """
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 900, TechLeadSessionFlavor.HEALTH_REVIEW)
        activity = _activity(store)
        activity.note_started(session)

        activity.note_concluded(
            session,
            SessionStatus.COMPLETED,
            processing_errors=["tech_lead decision contract violation"],
        )

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.FAILED

    @pytest.mark.parametrize(
        "status,phase",
        [
            (SessionStatus.NEEDS_HUMAN, TechLeadRunPhase.NEEDS_HUMAN),
            (SessionStatus.BLOCKED, TechLeadRunPhase.NEEDS_HUMAN),
            (SessionStatus.FAILED, TechLeadRunPhase.FAILED),
            (SessionStatus.TIMED_OUT, TechLeadRunPhase.FAILED),
        ],
    )
    def test_terminal_statuses_map_to_run_phases(self, tmp_path, status, phase):
        """An escalation is not a failure: burying it in the failure pile is how
        a correct tech-lead verdict stops being read."""
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 42, TechLeadSessionFlavor.FAILURE_INVESTIGATION)
        activity = _activity(store)
        activity.note_started(session)

        activity.note_concluded(session, status)

        (record,) = store.recent(limit=10)
        assert record.phase is phase

    def test_a_non_terminal_status_leaves_the_run_running(self, tmp_path):
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 42, TechLeadSessionFlavor.FAILURE_INVESTIGATION)
        activity = _activity(store)
        activity.note_started(session)

        activity.note_concluded(session, SessionStatus.NEEDS_VALIDATION_RETRY)

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.RUNNING

    def test_a_session_with_no_launch_stamp_is_never_concluded(self, tmp_path):
        """Completion finalization runs for EVERY session; only ours are runs,
        and the session's own launch stamp is what says so."""
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 42, None)

        _activity(store).note_concluded(session, SessionStatus.COMPLETED)

        assert store.recent(limit=10) == ()

    def test_a_renamed_tech_lead_agent_still_concludes_an_open_run(self, tmp_path):
        """The gate is the run's IMMUTABLE launch stamp, not the current agent
        configuration: a repository that renames its tech lead agent mid-run must
        not strand the open record at RUNNING forever."""
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 42, TechLeadSessionFlavor.FAILURE_INVESTIGATION)
        activity = _activity(store)
        activity.note_started(session)
        session.issue.agent_type = "agent:some-other-lead"

        activity.note_concluded(session, SessionStatus.COMPLETED)

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.COMPLETED

    def test_a_terminated_run_is_withdrawn_rather_than_left_running(self, tmp_path):
        """The one-shot timeout path runs no further tick, so a record left at
        RUNNING would claim forever that stopped work is still going."""
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 900, TechLeadSessionFlavor.HEALTH_REVIEW)
        activity = _activity(store)
        activity.note_started(session)

        activity.note_withdrawn(session)

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.WITHDRAWN
        assert record.ended_at == ENDED


class TestTheHistorySurvivesRestart:
    def test_a_run_recorded_in_one_process_is_readable_in_the_next(self, tmp_path):
        """The whole point of a local record is that it outlives the session."""
        db = tmp_path / "runs.sqlite"
        session = _session(tmp_path, 900, TechLeadSessionFlavor.HEALTH_REVIEW)
        first = _activity(SqliteTechLeadRunRecordStore(db))
        first.note_started(session)
        first.note_concluded(session, SessionStatus.COMPLETED)

        reopened = SqliteTechLeadRunRecordStore(db)

        (record,) = reopened.recent(limit=10)
        assert record.run_key == "global:health_review"
        assert record.phase is TechLeadRunPhase.COMPLETED

    def test_a_second_conclusion_never_overwrites_the_first_verdict(self, tmp_path):
        """A publish retry re-enters completion for the same session run."""
        store = SqliteTechLeadRunRecordStore(tmp_path / "runs.sqlite")
        session = _session(tmp_path, 42, TechLeadSessionFlavor.FAILURE_INVESTIGATION)
        activity = _activity(store)
        activity.note_started(session)
        activity.note_concluded(session, SessionStatus.COMPLETED)

        activity.note_concluded(session, SessionStatus.FAILED)

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.COMPLETED

    def test_relaunching_the_same_run_does_not_duplicate_its_history_row(
        self, tmp_path
    ):
        store = SqliteTechLeadRunRecordStore(tmp_path / "runs.sqlite")
        session = _session(tmp_path, 900, TechLeadSessionFlavor.HEALTH_REVIEW)
        activity = _activity(store)

        activity.note_started(session)
        activity.note_started(session)

        assert len(store.recent(limit=10)) == 1

    def test_runs_come_back_most_recently_started_first(self, tmp_path):
        store = SqliteTechLeadRunRecordStore(tmp_path / "runs.sqlite")
        for offset, number in enumerate((10, 20, 30)):
            store.open_run(
                TechLeadRunRecord(
                    run_key=f"issue:{number}",
                    scope_kind=TechLeadRunScopeKind.ISSUE,
                    flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                    phase=TechLeadRunPhase.RUNNING,
                    started_at=datetime(2026, 8, 9, 9, offset, 0),
                    run_id=f"run-{number}",
                    session_name=f"tech-lead-{number}",
                    subject_issue_number=number,
                )
            )

        assert [r.subject_issue_number for r in store.recent(limit=10)] == [30, 20, 10]

    def test_the_limit_is_respected(self, tmp_path):
        store = SqliteTechLeadRunRecordStore(tmp_path / "runs.sqlite")
        for offset, number in enumerate((10, 20, 30)):
            store.open_run(
                TechLeadRunRecord(
                    run_key=f"issue:{number}",
                    scope_kind=TechLeadRunScopeKind.ISSUE,
                    flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                    phase=TechLeadRunPhase.RUNNING,
                    started_at=datetime(2026, 8, 9, 9, offset, 0),
                    run_id=f"run-{number}",
                    session_name=f"tech-lead-{number}",
                    subject_issue_number=number,
                )
            )

        assert len(store.recent(limit=2)) == 2


class TestPublishFailuresAreNotConclusions:
    def test_a_publish_failure_leaves_the_run_open_for_its_retry(self, tmp_path):
        """The retry re-enters completion for this same session run, so
        concluding now would publish a verdict the retry overturns — the same
        rule the launch-authority retention owner applies beside this call."""
        store = InMemoryTechLeadRunRecordStore()
        session = _session(tmp_path, 900, TechLeadSessionFlavor.HEALTH_REVIEW)
        activity = _activity(store)
        activity.note_started(session)

        activity.note_concluded(
            session,
            SessionStatus.FAILED,
            processing_errors=[f"{ERROR_PREFIX_PUSH}: remote rejected"],
        )

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.RUNNING


class TestTheRunsEvidenceOutlivesItsWorktree:
    """#6858 F4: the promise of local visibility is the ARTIFACTS, not a summary.

    A failure investigation writes its evidence map, decision, report and
    terminal recording inside a DISPOSABLE scratch worktree, and completion
    always removes that worktree. Before the archive, a record kept counts and a
    sentence while the evidence they described was deleted minutes later.
    """

    def _run_dir_with_artifacts(self, tmp_path) -> "FakeSession":
        session = _session(tmp_path, 42, TechLeadSessionFlavor.FAILURE_INVESTIGATION)
        _write_decision(session)
        (Path(session.run_dir) / "terminal-recording.jsonl").write_text(
            '{"event_type": "output", "data_b64": "aGk="}\n', encoding="utf-8"
        )
        (Path(session.run_dir) / "manifest.json").write_text("{}", encoding="utf-8")
        return session

    def test_a_concluded_run_keeps_its_artifacts_after_the_worktree_is_gone(
        self, tmp_path
    ):
        store = InMemoryTechLeadRunRecordStore()
        archive = FileSystemTechLeadRunArtifactArchive(tmp_path / "archive")
        session = self._run_dir_with_artifacts(tmp_path)
        activity = _activity(store, archive)
        activity.note_started(session)

        activity.note_concluded(session, SessionStatus.COMPLETED)
        # The scratch worktree is removed by the cleanup this same completion
        # plans. Everything the operator was promised must survive it.
        shutil.rmtree(session.run_dir)

        (record,) = store.recent(limit=10)
        assert record.artifacts is not None
        assert set(record.artifacts.kinds) == {
            TechLeadRunArtifactKind.SESSION_REPLAY,
            TechLeadRunArtifactKind.REPORT,
            TechLeadRunArtifactKind.DECISION,
        }
        for kind in record.artifacts.kinds:
            assert record.artifacts.path_for(kind).is_file()

    def test_the_preserved_location_is_outside_the_runs_own_directory(self, tmp_path):
        """Otherwise "preserved" means "deleted with everything else"."""
        store = InMemoryTechLeadRunRecordStore()
        archive = FileSystemTechLeadRunArtifactArchive(tmp_path / "archive")
        session = self._run_dir_with_artifacts(tmp_path)
        activity = _activity(store, archive)
        activity.note_started(session)

        activity.note_concluded(session, SessionStatus.COMPLETED)

        (record,) = store.recent(limit=10)
        assert record.artifacts is not None
        with pytest.raises(ValueError):
            record.artifacts.location.relative_to(Path(session.run_dir))

    def test_a_run_that_wrote_nothing_advertises_no_drill_down(self, tmp_path):
        """Truthful emptiness beats a button that 404s."""
        store = InMemoryTechLeadRunRecordStore()
        archive = FileSystemTechLeadRunArtifactArchive(tmp_path / "archive")
        session = _session(tmp_path, 900, TechLeadSessionFlavor.HEALTH_REVIEW)
        activity = _activity(store, archive)
        activity.note_started(session)

        activity.note_concluded(session, SessionStatus.FAILED)

        (record,) = store.recent(limit=10)
        assert record.artifacts is None

    def test_an_unwritable_archive_does_not_fail_the_conclusion(self, tmp_path):
        """A lost receipt must never lose the run: the port forbids raising."""
        store = InMemoryTechLeadRunRecordStore()
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        archive = FileSystemTechLeadRunArtifactArchive(blocked / "archive")
        session = self._run_dir_with_artifacts(tmp_path)
        activity = _activity(store, archive)
        activity.note_started(session)

        activity.note_concluded(session, SessionStatus.COMPLETED)

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.COMPLETED
        assert record.artifacts is None

    def test_a_withdrawn_runs_evidence_is_preserved_too(self, tmp_path):
        """A run stopped mid-audit is exactly the one an operator wants to read."""
        store = InMemoryTechLeadRunRecordStore()
        archive = FileSystemTechLeadRunArtifactArchive(tmp_path / "archive")
        session = self._run_dir_with_artifacts(tmp_path)
        activity = _activity(store, archive)
        activity.note_started(session)

        activity.note_withdrawn(session)

        (record,) = store.recent(limit=10)
        assert record.phase is TechLeadRunPhase.WITHDRAWN
        assert record.artifacts is not None

    def test_the_locator_survives_a_restart(self, tmp_path):
        db = tmp_path / "runs.sqlite"
        archive = FileSystemTechLeadRunArtifactArchive(tmp_path / "archive")
        session = self._run_dir_with_artifacts(tmp_path)
        first = _activity(SqliteTechLeadRunRecordStore(db), archive)
        first.note_started(session)
        first.note_concluded(session, SessionStatus.COMPLETED)

        (record,) = SqliteTechLeadRunRecordStore(db).recent(limit=10)

        assert record.artifacts is not None
        assert record.artifacts.has(TechLeadRunArtifactKind.REPORT)
