"""Tests for the tech_lead session assignment domain type (ADR-0031)."""

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadAssignment,
    TechLeadCreationKind,
    TechLeadCreationOrigin,
    TechLeadSessionFlavor,
    require_case_file_observation_label,
)


class TestCaseFileObservationLabelInvariant:
    """The domain owns the pattern case-file label invariant (#6781)."""

    def test_accepts_labels_carrying_the_observation_label(self) -> None:
        require_case_file_observation_label(
            ("agent:tech-lead", TECH_LEAD_OBSERVATION_LABEL, "area:db")
        )  # does not raise

    def test_rejects_labels_missing_the_observation_label(self) -> None:
        with pytest.raises(ValueError, match="observation label"):
            require_case_file_observation_label(("agent:tech-lead", "area:db"))


class TestTechLeadAssignmentRoundTrip:
    def test_batch_review_round_trips_through_file(self, tmp_path: Path) -> None:
        assignment = TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
        path = tmp_path / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME

        assignment.write(path)

        assert TechLeadAssignment.read(path) == assignment

    def test_failure_investigation_round_trips_focus_fields(
        self, tmp_path: Path
    ) -> None:
        assignment = TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=4321,
            focus_reason="Investigate: session timed out",
        )
        path = tmp_path / TECH_LEAD_ASSIGNMENT_FILENAME

        assignment.write(path)
        loaded = TechLeadAssignment.read(path)

        assert loaded == assignment
        assert loaded.focus_issue_number == 4321
        assert loaded.focus_reason == "Investigate: session timed out"

    def test_health_review_round_trips_through_file(self, tmp_path: Path) -> None:
        """Health reviews carry no focus fields — like batch (ADR-0031 §4)."""
        assignment = TechLeadAssignment(flavor=TechLeadSessionFlavor.HEALTH_REVIEW)
        path = tmp_path / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME

        assignment.write(path)
        loaded = TechLeadAssignment.read(path)

        assert loaded == assignment
        assert loaded.focus_issue_number is None
        assert loaded.focus_reason == ""

    def test_write_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / TECH_LEAD_ASSIGNMENT_FILENAME

        TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW).write(path)

        assert path.exists()

    def test_serialized_form_is_stable(self) -> None:
        assignment = TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=7,
            focus_reason="broken",
        )

        assert assignment.to_dict() == {
            "schema_version": 1,
            "flavor": "failure_investigation",
            "focus_issue_number": 7,
            "focus_reason": "broken",
        }


class TestTechLeadAssignmentValidation:
    def test_unknown_flavor_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="Unknown tech_lead assignment flavor"):
            TechLeadAssignment.from_dict(
                {"schema_version": 1, "flavor": "board_walkthrough"}
            )

    def test_missing_flavor_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="Unknown tech_lead assignment flavor"):
            TechLeadAssignment.from_dict({"schema_version": 1})

    def test_bad_schema_version_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            TechLeadAssignment.from_dict(
                {"schema_version": 99, "flavor": "batch_review"}
            )

    def test_non_int_schema_version_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            TechLeadAssignment.from_dict(
                {"schema_version": "1", "flavor": "batch_review"}
            )

    def test_failure_flavor_requires_focus_issue_number(self) -> None:
        with pytest.raises(ValueError, match="focus_issue_number"):
            TechLeadAssignment(flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)

    def test_failure_flavor_requires_focus_issue_number_from_dict(self) -> None:
        with pytest.raises(ValueError, match="focus_issue_number"):
            TechLeadAssignment.from_dict(
                {"schema_version": 1, "flavor": "failure_investigation"}
            )

    def test_non_int_focus_issue_number_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="focus_issue_number"):
            TechLeadAssignment.from_dict(
                {
                    "schema_version": 1,
                    "flavor": "failure_investigation",
                    "focus_issue_number": "42",
                }
            )

    def test_non_string_focus_reason_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="focus_reason"):
            TechLeadAssignment.from_dict(
                {
                    "schema_version": 1,
                    "flavor": "failure_investigation",
                    "focus_issue_number": 42,
                    "focus_reason": 3,
                }
            )

    def test_malformed_json_raises_from_read(self, tmp_path: Path) -> None:
        path = tmp_path / TECH_LEAD_ASSIGNMENT_FILENAME
        path.write_text("{not json")

        with pytest.raises(json.JSONDecodeError):
            TechLeadAssignment.read(path)


class TestCreationOriginHasExactlyTwoValidStates:
    """#6957 round-2 review F6/A6: authority is stated, never inferred.

    A tech-lead issue creation either AUTHORS its session's anchor or was
    DECIDED by a session already working one. The second must reconcile against
    that anchor before it writes. Encoding the difference as a defaulted
    ``anchor_issue_number: int = 0`` meant composition that dropped the value
    produced a follow-up indistinguishable from legitimate anchor authoring —
    and it wrote unguarded. These pin the states the type admits.
    """

    def test_anchor_authoring_has_no_subject_and_expects_nothing(self) -> None:
        origin = TechLeadCreationOrigin.authors_anchor()

        assert origin.kind is TechLeadCreationKind.AUTHORS_ANCHOR
        assert origin.authors_new_anchor
        assert origin.reconciliation_subject == 0
        assert not origin.requires_expected_state

    def test_a_derived_creation_names_its_anchor_and_expects_state(self) -> None:
        origin = TechLeadCreationOrigin.derived_from_anchor(77)

        assert origin.kind is TechLeadCreationKind.DERIVED_FROM_ANCHOR
        assert not origin.authors_new_anchor
        assert origin.reconciliation_subject == 77
        assert origin.requires_expected_state

    @pytest.mark.parametrize("dropped", (0, -1))
    def test_a_derived_creation_without_its_anchor_is_rejected(self, dropped) -> None:
        """The dropped-subject case that used to read as anchor authoring."""
        with pytest.raises(ValueError, match="requires that anchor"):
            TechLeadCreationOrigin(
                kind=TechLeadCreationKind.DERIVED_FROM_ANCHOR,
                anchor_issue_number=dropped,
            )

    def test_anchor_authoring_may_not_claim_a_subject(self) -> None:
        """The mirror: naming an anchor means the creation did not author it."""
        with pytest.raises(ValueError, match="must not name one"):
            TechLeadCreationOrigin(
                kind=TechLeadCreationKind.AUTHORS_ANCHOR, anchor_issue_number=77
            )
