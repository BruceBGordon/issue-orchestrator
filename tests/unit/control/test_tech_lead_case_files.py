"""Tests for the pattern case-file policy owner (#6781).

Covers the durable flag_pattern ledger's composition helpers, the anchor-scan
partition that classifies case files into the board snapshot, and the area
bucketing that clusters evidence across signatures (#6781 amendment).
"""

import pytest

from issue_orchestrator.control.actions import CreateTechLeadCaseFileIssueAction
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_case_files import (
    CASE_FILE_TITLE_PREFIX,
    build_case_file_evidence_comment,
    build_case_file_issue_action,
    build_case_file_summary,
    build_pattern_ledger,
    build_pattern_observation,
    case_file_area_counts,
    split_tech_lead_case_file_issues,
)
from issue_orchestrator.domain.tech_lead_findings import (
    PatternEvidence,
    PatternObservation,
    case_file_issue_marker,
    pattern_observation_marker,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_artifacts import ProposedTechLeadAction, TechLeadFinding
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadCaseFileSummary,
)
from issue_orchestrator.infra.config import Config

EXPECTED = build_expected_for_mutation()


def _config() -> Config:
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.filtering.label = "io-scope"
    return config


def _proposed(
    *, signature: str = "db-timeout", area: str | None = "db", finding_ids=("T1",)
) -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id="A4",
        action_type="flag_pattern",
        body="Three sessions hit the same DB pool timeout.",
        pattern_signature=signature,
        area=area,
        finding_ids=finding_ids,
    )


def _findings() -> dict[str, TechLeadFinding]:
    return {
        "T1": TechLeadFinding(
            id="T1",
            title="Pool exhausted",
            classification="infra",
            evidence=("orchestrator log lines 10-20", "session issue-42"),
        )
    }


# --- Ledger projection ----------------------------------------------------


def test_build_pattern_ledger_projects_rows_to_signature_map() -> None:
    ledger = build_pattern_ledger(
        [
            PatternEvidence(
                signature="sig-a", case_file_issue_number=500, observation_count=1
            ),
            PatternEvidence(
                signature="sig-b", case_file_issue_number=501, observation_count=1
            ),
        ]
    )
    assert {sig: row.case_file_issue_number for sig, row in ledger.items()} == {
        "sig-a": 500,
        "sig-b": 501,
    }


def test_build_pattern_ledger_empty() -> None:
    assert build_pattern_ledger([]) == {}


# --- Case-file creation composition ---------------------------------------


def test_build_case_file_issue_action_first_observation() -> None:
    action = build_case_file_issue_action(
        _proposed(),
        config=_config(),
        anchor_issue_number=99,
        findings=_findings(),
        source_run_id="run-1",
        source_session_name="issue-99",
        observed_at="2026-07-11T00:00:00+00:00",
        expected=EXPECTED,
    )

    assert isinstance(action, CreateTechLeadCaseFileIssueAction)
    assert action.title == f"{CASE_FILE_TITLE_PREFIX}db-timeout"
    assert action.pattern_signature == "db-timeout"
    assert action.area == "db"
    assert action.diagnosis == _proposed().body
    # Labels keep it in the anchor scan, in scope, non-pickup, area-tagged.
    assert "agent:tech-lead" in action.labels
    assert "io-scope" in action.labels
    assert TECH_LEAD_OBSERVATION_LABEL in action.labels
    assert "area:db" in action.labels
    assert action.pr_count == 0
    assert action.expected is EXPECTED
    # Body documents the signature, anchor, provenance, and tamper boundary.
    assert "`db-timeout`" in action.body
    assert "#99" in action.body
    assert "run `run-1`" in action.body
    assert "issue-99" in action.body
    assert "2026-07-11T00:00:00+00:00" in action.body
    assert "Pool exhausted" in action.body
    assert "orchestrator log lines 10-20" in action.body
    assert TECH_LEAD_OBSERVATION_LABEL in action.body
    assert "editing this issue has no effect" in action.body
    # Reason names the signature and the tracking issue.
    assert "db-timeout" in action.reason
    assert "#6781" in action.reason


def test_build_case_file_issue_action_unclassified_area() -> None:
    action = build_case_file_issue_action(
        _proposed(area=None),
        config=_config(),
        anchor_issue_number=99,
        findings=_findings(),
        source_run_id="run-1",
        source_session_name="issue-99",
        observed_at="2026-07-11T00:00:00+00:00",
        expected=EXPECTED,
    )

    assert action.area is None
    assert not any(label.startswith("area:") for label in action.labels)
    assert "| Area | unclassified |" in action.body


def test_build_case_file_evidence_comment_repeat_observation() -> None:
    comment = build_case_file_evidence_comment(
        _proposed(),
        anchor_issue_number=77,
        findings=_findings(),
        source_run_id="run-2",
        source_session_name="issue-77",
        observed_at="2026-07-11T01:00:00+00:00",
        observation_id="run-2:issue-77:A4",
    )

    assert comment.startswith("## 📌 Pattern observed again")
    assert "`db-timeout`" in comment
    assert "#77" in comment
    assert "run `run-2`" in comment
    assert "2026-07-11T01:00:00+00:00" in comment
    assert "Pool exhausted" in comment
    assert "orchestrator log lines 10-20" in comment


def test_evidence_block_omitted_when_no_findings_linked() -> None:
    comment = build_case_file_evidence_comment(
        _proposed(finding_ids=()),
        anchor_issue_number=77,
        findings=_findings(),
        source_run_id="run-2",
        source_session_name="issue-77",
        observed_at="2026-07-11T01:00:00+00:00",
        observation_id="run-2:issue-77:A4",
    )
    assert "### Evidence" not in comment


def test_observation_identity_is_stable_and_marks_its_comment() -> None:
    """The identity that makes the durable count create-once (#6957 F1).

    Two runs observing the SAME decision action produce different identities,
    while replaying one observation reproduces its identity exactly — and the
    identity is embedded in the comment so a crash-retry duplicate is
    recognizable as the same observation rather than fresh evidence.
    """
    kwargs = dict(
        anchor_issue_number=77,
        findings=_findings(),
        source_session_name="issue-77",
        observed_at="2026-07-11T01:00:00+00:00",
    )
    first = build_pattern_observation(_proposed(), source_run_id="run-2", **kwargs)
    replay = build_pattern_observation(_proposed(), source_run_id="run-2", **kwargs)
    other_run = build_pattern_observation(_proposed(), source_run_id="run-3", **kwargs)

    assert first.observation_id == replay.observation_id
    assert first.observation_id != other_run.observation_id
    assert pattern_observation_marker(first.observation_id) in first.comment
    assert pattern_observation_marker(first.observation_id) not in other_run.comment


def test_case_file_creation_carries_its_body_observation() -> None:
    action = build_case_file_issue_action(
        _proposed(),
        config=_config(),
        anchor_issue_number=99,
        findings=_findings(),
        source_run_id="run-1",
        source_session_name="issue-99",
        observed_at="2026-07-11T00:00:00+00:00",
        expected=EXPECTED,
    )

    assert action.body_observation.observation_id == "run-1:issue-99:A4"
    assert action.additional_observations == ()


# --- Case-file action self-validation -------------------------------------


MARKER = case_file_issue_marker("sig")


def _case_file_action(**overrides) -> CreateTechLeadCaseFileIssueAction:
    kwargs = dict(
        title="t",
        body=f"b\n\n{MARKER}",
        labels=("agent:tech-lead", TECH_LEAD_OBSERVATION_LABEL),
        pattern_signature="sig",
        idempotency_marker=MARKER,
        observations=(PatternObservation(observation_id="o1", comment="e"),),
    )
    kwargs.update(overrides)
    return CreateTechLeadCaseFileIssueAction(**kwargs)


def test_case_file_action_requires_observation_label() -> None:
    with pytest.raises(ValueError, match="observation label"):
        _case_file_action(labels=("agent:tech-lead",))


def test_case_file_action_requires_nonempty_signature() -> None:
    with pytest.raises(ValueError, match="pattern_signature"):
        _case_file_action(pattern_signature="   ")


def test_case_file_action_requires_an_identified_observation() -> None:
    """An unidentified observation could not be counted create-once (#6957 F1)."""
    with pytest.raises(ValueError, match="identified observation"):
        _case_file_action(observations=())


def test_case_file_action_rejects_repeated_observation_identities() -> None:
    with pytest.raises(ValueError, match="distinct identities"):
        _case_file_action(
            observations=(
                PatternObservation(observation_id="o1", comment="e"),
                PatternObservation(observation_id="o1", comment="e2"),
            )
        )


def test_case_file_action_requires_a_recoverable_marker() -> None:
    """Without it an interrupted creation files a SECOND case file (#6957 F10)."""
    with pytest.raises(ValueError, match="idempotency_marker"):
        _case_file_action(idempotency_marker="")


def test_case_file_action_marker_must_appear_in_the_body() -> None:
    with pytest.raises(ValueError, match="must\\n? appear in the issue body|appear in the issue body"):
        _case_file_action(body="no marker here")


def test_composed_case_file_carries_its_recovery_marker() -> None:
    action = build_case_file_issue_action(
        _proposed(),
        config=_config(),
        anchor_issue_number=99,
        findings=_findings(),
        source_run_id="run-1",
        source_session_name="issue-99",
        observed_at="2026-07-11T00:00:00+00:00",
        expected=EXPECTED,
    )

    assert action.idempotency_marker == case_file_issue_marker("db-timeout")
    assert action.idempotency_marker in action.body


# --- Anchor-scan partition (classification) -------------------------------


def _issue(number: int, labels: list[str], title: str = "t") -> Issue:
    return Issue(number=number, title=title, labels=labels, repo="owner/repo")


def test_split_partitions_observation_labeled_issues() -> None:
    case_file = _issue(
        500, ["agent:tech-lead", TECH_LEAD_OBSERVATION_LABEL, "area:db"],
        title="Pattern case file: db-timeout",
    )
    anchor = _issue(7, ["agent:tech-lead"], title="Tech Lead Batch Review: 3 PRs pending")

    remaining, case_files = split_tech_lead_case_file_issues([case_file, anchor])

    assert [i.number for i in remaining] == [7]
    assert len(case_files) == 1
    assert isinstance(case_files[0], TechLeadCaseFileSummary)
    assert case_files[0].issue_number == 500
    assert case_files[0].area == "db"


def test_split_returns_empty_case_files_when_none_present() -> None:
    anchor = _issue(7, ["agent:tech-lead"])
    remaining, case_files = split_tech_lead_case_file_issues([anchor])
    assert [i.number for i in remaining] == [7]
    assert case_files == ()


def test_case_file_summary_reads_typed_scan_fields() -> None:
    summary = build_case_file_summary(
        Issue(
            number=500, title="Pattern case file: x",
            labels=[TECH_LEAD_OBSERVATION_LABEL], repo="owner/repo",
            comment_count=4, updated_at="2026-07-11T12:00:00+00:00",
        )
    )
    assert summary.comment_count == 4
    assert summary.updated_at == "2026-07-11T12:00:00+00:00"
    assert summary.area == ""  # no area:* tag


def test_case_file_classification_and_area_are_case_insensitive() -> None:
    issue = _issue(500, ["Tech-Lead-Observation", "Area:db"])
    remaining, case_files = split_tech_lead_case_file_issues([issue])
    assert remaining == []
    assert case_files[0].area == "db"


# --- Area bucketing (#6781 amendment) -------------------------------------


def _summary(area: str) -> TechLeadCaseFileSummary:
    return TechLeadCaseFileSummary(issue_number=1, title="t", area=area)


def test_case_file_area_counts_groups_and_defaults_unclassified() -> None:
    counts = case_file_area_counts(
        [_summary("db"), _summary("db"), _summary(""), _summary("api")]
    )
    # Sorted by count desc then area name; empty area groups as unclassified.
    assert counts == (("db", 2), ("api", 1), ("unclassified", 1))


def test_case_file_area_counts_empty() -> None:
    assert case_file_area_counts([]) == ()
