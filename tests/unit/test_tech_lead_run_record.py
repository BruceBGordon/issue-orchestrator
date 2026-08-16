"""Invariants of the local tech-lead run record (ADR-0033 / #6858).

The record's whole value is that it can be trusted after the run is gone, so
the shapes it refuses matter as much as the ones it accepts: a record with no
drill-down identity cannot be checked, and a record whose key and scope
disagree would be given the wrong exclusivity if anything ever read it back.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from issue_orchestrator.domain.tech_lead_run import TechLeadRunScopeKind
from issue_orchestrator.domain.tech_lead_run_record import (
    TechLeadRunPhase,
    TechLeadRunRecord,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

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
    }
    fields.update(overrides)
    return TechLeadRunRecord(**fields)


class TestItRefusesRecordsThatCannotBeTrusted:
    @pytest.mark.parametrize("field", ["run_id", "session_name"])
    def test_a_record_with_no_drill_down_identity_is_refused(self, field):
        with pytest.raises(ValueError, match="session run identity"):
            _record(**{field: ""})

    def test_a_run_key_that_contradicts_its_scope_is_refused(self):
        with pytest.raises(ValueError, match="does not name"):
            _record(scope_kind=TechLeadRunScopeKind.ISSUE)

    def test_an_unknown_run_key_is_refused(self):
        with pytest.raises(ValueError, match="no known tech-lead run scope"):
            _record(run_key="global:vibes")

    def test_a_terminal_run_must_say_when_it_ended(self):
        with pytest.raises(ValueError, match="must record when it ended"):
            _record(phase=TechLeadRunPhase.COMPLETED)

    def test_a_running_run_cannot_claim_an_end_time(self):
        with pytest.raises(ValueError, match="has not ended yet"):
            _record(ended_at=ENDED)


class TestConclusion:
    def test_concluding_records_the_verdict_and_what_it_produced(self):
        concluded = _record().concluded(
            phase=TechLeadRunPhase.COMPLETED,
            ended_at=ENDED,
            detail="Two hotspots are past budget",
            findings=2,
            proposals=1,
        )

        assert concluded.phase is TechLeadRunPhase.COMPLETED
        assert concluded.ended_at == ENDED
        assert (concluded.findings, concluded.proposals) == (2, 1)
        assert concluded.duration_seconds == 1800.0

    def test_concluding_as_running_is_refused(self):
        """The store writes whatever this returns, so "it ended, and it is still
        running" must be impossible to construct."""
        with pytest.raises(ValueError, match="not a conclusion"):
            _record().concluded(phase=TechLeadRunPhase.RUNNING, ended_at=ENDED)

    def test_a_running_run_reports_no_duration_rather_than_a_negative_one(self):
        assert _record().duration_seconds == 0.0


class TestPhaseVocabulary:
    def test_only_running_is_non_terminal(self):
        terminal = {
            phase for phase in TechLeadRunPhase if phase.is_terminal
        }
        assert TechLeadRunPhase.RUNNING not in terminal
        assert terminal == set(TechLeadRunPhase) - {TechLeadRunPhase.RUNNING}

    def test_an_escalation_is_not_a_failure(self):
        """Needs-human is the tech lead working correctly; folding it into the
        failure pile is how a correct verdict stops being read."""
        assert TechLeadRunPhase.NEEDS_HUMAN is not TechLeadRunPhase.FAILED
