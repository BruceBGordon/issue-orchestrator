"""Direct tests for completion action planning policy."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    CreateTechLeadCaseFileIssueAction,
    CreateTechLeadIssueAction,
    RemoveLabelAction,
    ResetRetryIssueAction,
    SurfaceTechLeadProposalAction,
)
from issue_orchestrator.control.completion_action_planner import (
    CompletionActionPlanner,
    critical_processing_errors,
)
from issue_orchestrator.control.completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUSH,
)
from issue_orchestrator.control.tech_lead_completion import (
    tech_lead_decision_processing_error,
)
from issue_orchestrator.control.label_manager import LabelManager
from tests.conftest import make_provider_availability
from issue_orchestrator.domain.board_snapshot import BoardFailure, BoardSnapshot
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_manifest import PRToReview, TechLeadManifest
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadAssignment,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from issue_orchestrator.infra.open_issue_corpus_store import (
    SqliteOpenIssueCorpusStore,
)
from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
from issue_orchestrator.ports.open_issue_corpus_store import (
    InMemoryOpenIssueCorpusStore,
    OpenIssueCorpusStore,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import RepositoryHost
from tests.unit.session_run_helpers import make_session_run_assets


def make_issue(
    number: int = 1,
    *,
    labels: list[str] | None = None,
) -> Issue:
    """Create an issue for planner tests."""
    return Issue(
        number=number,
        title=f"Test issue {number}",
        labels=labels or ["agent:test"],
        repo="owner/repo",
    )


def make_session(
    tmp_path: Path,
    *,
    issue: Issue | None = None,
    terminal_id: str = "issue-1",
) -> Session:
    """Create a session for planner tests."""
    issue = issue or make_issue()
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue.number)), task=TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(prompt_path=tmp_path / "prompt.md", timeout_minutes=45),
        terminal_id=terminal_id,
        worktree_path=tmp_path,
        branch_name=f"issue-{issue.number}",
        run_assets=make_session_run_assets(tmp_path, session_name=terminal_id),
    )


def make_planner(
    config: Config,
    *,
    issue_labels: list[str] | None = None,
    repository_host: RepositoryHost | None = None,
    open_issue_corpus_store: OpenIssueCorpusStore | None = None,
) -> CompletionActionPlanner:
    """Create a planner with a repository host that can answer label reads.

    Tech-Lead-configured tests rendezvous with ``record_authority`` through the
    SQLite adapter at ``config.repo_root`` (a tmp_path); everything else gets
    the in-memory port fake so no state files are written.
    """
    issue = SimpleNamespace(labels=issue_labels or [])
    repository_host = repository_host or cast(
        RepositoryHost, SimpleNamespace(get_issue=lambda _number: issue)
    )
    tech_lead_authority = (
        SqliteTechLeadAuthorityStore.for_repo(config.repo_root)
        if config.tech_lead_review_agent
        else InMemoryTechLeadAuthorityStore()
    )
    if open_issue_corpus_store is None:
        open_issue_corpus_store = InMemoryOpenIssueCorpusStore()
    corpus_repository_host = cast(
        RepositoryHost,
        SimpleNamespace(
            list_issues=lambda **_kwargs: [],
            list_issues_delta=lambda **_kwargs: ([], None),
        ),
    )
    open_issue_corpus = OpenIssueCorpusManager(
        corpus_repository_host,
        open_issue_corpus_store,
        is_enabled=lambda: bool(
            config.tech_lead_review_agent and config.tech_lead.dedup.enabled
        ),
    )
    # Completion consumes only refreshed local facts. Keep this fixture at the
    # same post-refresh boundary without making planning read GitHub.
    open_issue_corpus.sync()
    return CompletionActionPlanner(
        config, repository_host, LabelManager(config), tech_lead_authority,
        open_issue_corpus,
        lambda _n: None,  # no live target session in these unit fixtures (#6779 R1)
        make_provider_availability(config),
    )


def added_labels(actions: tuple[object, ...]) -> set[str]:
    """Return labels added by a planner result."""
    return {action.label for action in actions if isinstance(action, AddLabelAction)}


def removed_labels(actions: tuple[object, ...]) -> set[str]:
    """Return labels removed by a planner result."""
    return {action.label for action in actions if isinstance(action, RemoveLabelAction)}


def comments(actions: tuple[object, ...]) -> list[str]:
    """Return comments emitted by a planner result."""
    return [action.comment for action in actions if isinstance(action, AddCommentAction)]


def test_timeout_issue_session_marks_blocked_failed_and_releases_claim(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.TIMED_OUT,
    )

    assert "blocked-failed" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Timed Out" in comment for comment in comments(actions))


def test_failed_issue_session_without_retry_needs_human(tmp_path: Path) -> None:
    config = Config()
    config.retry.interrupted_sessions.enabled = False

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
    )

    assert "needs-human" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Needs Investigation" in comment for comment in comments(actions))


def test_blocked_issue_session_uses_reported_label_and_reason(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.BLOCKED,
        blocked_label="blocked-upstream",
        blocked_reason="Waiting on dependency",
    )

    assert "blocked-upstream" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Waiting on dependency" in comment for comment in comments(actions))


def test_completed_with_publish_error_tracks_publish_failure(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        processing_errors=[f"{ERROR_PREFIX_PUSH}: rejected"],
        diagnostic_path=".issue-orchestrator/diagnostics/publish.md",
    )

    assert {"publish-failed", "publish-fail-count-1"} <= added_labels(actions)
    assert {"in-progress", "needs-rework"} <= removed_labels(actions)
    assert any("Publishing Failed" in comment for comment in comments(actions))


def test_review_exchange_halt_puts_issue_on_hold(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        review_exchange_halted=True,
    )

    assert "blocked-failed" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Review Exchange Halted" in comment for comment in comments(actions))


def make_tech_lead_config(tmp_path: Path) -> Config:
    from unittest.mock import Mock

    config = Config()
    config.repo_root = tmp_path  # authority store lives in the repo state dir
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_reviewed_label = "tech-lead-reviewed"
    config.tech_lead_failed_label = "tech-lead-failed"
    # Destination worker a create_issue proposal routes to (#6779 R5/R9).
    config.agents = {"agent:web": Mock()}
    config.tech_lead_follow_up_agent = "agent:web"
    return config


def record_authority(
    config: Config, session: Session, authority: TechLeadLaunchAuthority
) -> None:
    """Persist the orchestrator-owned launch authority for a session run."""
    SqliteTechLeadAuthorityStore.for_repo(config.repo_root).record(
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
        authority=authority,
    )


def arm_batch_session(
    config: Config,
    session: Session,
    tmp_path: Path,
    *,
    with_manifest: bool = True,
) -> None:
    """Plant matching worktree copies AND the launch authority for a batch."""
    plant_tech_lead_assignment(
        session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
    )
    if with_manifest:
        plant_tech_lead_manifest(tmp_path, session)
    record_authority(
        config,
        session,
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            anchor_issue_number=session.issue.number,
            manifest_pr_numbers=(101, 102) if with_manifest else (),
        ),
    )


def arm_investigation_session(
    config: Config, session: Session, *, focus: int = 1
) -> None:
    """Plant matching worktree copies AND the launch authority for a focus run."""
    plant_tech_lead_assignment(
        session,
        TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=focus,
            focus_reason="Investigate: timed out",
        ),
    )
    record_authority(
        config,
        session,
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            anchor_issue_number=session.issue.number,
            focus_issue_number=focus,
        ),
    )


def arm_health_review_session(
    config: Config,
    session: Session,
    *,
    problem_issue_numbers: tuple[int, ...] = (),
    context_failure_numbers: tuple[int, ...] = (),
) -> None:
    """Plant the snapshot, assignment, and immutable health authority.

    ``problem_issue_numbers`` is the OWNED cohort (act-level authority);
    ``context_failure_numbers`` are unrelated failures that appear on the
    board but grant nothing (#6780). Cohort members also appear in
    ``recent_failures`` — that list is the board a reviewer reads, and it is
    a superset of the grant, which is exactly why authority is carried on its
    own surface.
    """
    plant_tech_lead_assignment(
        session, TechLeadAssignment(flavor=TechLeadSessionFlavor.HEALTH_REVIEW)
    )
    BoardSnapshot(
        generated_at="2026-07-14T12:00:00+00:00",
        orchestrator_paused=False,
        recent_failures=[
            BoardFailure(
                issue_number=number,
                issue_title=f"Problem {number}",
                failure_reason="failed",
                artifact_hints=[],
            )
            for number in (*problem_issue_numbers, *context_failure_numbers)
        ],
        problem_cohort=list(problem_issue_numbers),
    ).write(session.run_dir / "tech-lead-data" / "board-snapshot.json")
    record_authority(
        config,
        session,
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            anchor_issue_number=session.issue.number,
            problem_issue_numbers=problem_issue_numbers,
        ),
    )


def make_tech_lead_session(tmp_path: Path, *, terminal_id: str = "issue-1") -> Session:
    issue = make_issue(labels=["agent:tech-lead"])  # agent_type derives from labels
    return make_session(tmp_path, issue=issue, terminal_id=terminal_id)


def plant_tech_lead_assignment(session: Session, assignment: TechLeadAssignment) -> None:
    """Write the launch-time assignment into the session's tech-lead-data dir."""
    assignment_path = session.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
    assignment.write(assignment_path)
    run_manifest_path = session.run_dir / "manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text())
    run_manifest["tech_lead_assignment"] = str(assignment_path)
    run_manifest_path.write_text(json.dumps(run_manifest))


def plant_tech_lead_manifest(tmp_path: Path, session: Session) -> None:
    """Write a two-PR tech_lead manifest discoverable via the run manifest."""
    manifest = TechLeadManifest(
        prs=[
            PRToReview(number=101, title="PR 101", url="https://example/pr/101", branch="b1"),
            PRToReview(number=102, title="PR 102", url="https://example/pr/102", branch="b2"),
        ]
    )
    manifest_path = tmp_path / "tech-lead-manifest.json"
    manifest.write(manifest_path)
    run_manifest_path = session.run_dir / "manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text())
    run_manifest["tech_lead_manifest"] = str(manifest_path)
    run_manifest_path.write_text(json.dumps(run_manifest))


def plant_tech_lead_decision_pair(
    session: Session, *, comment_targets: tuple[int, ...] = (101,)
) -> None:
    """Write a valid decision + report pair into the session's tech-lead-data dir.

    ``comment_targets`` controls the post_comment proposals. Targets must fall
    inside the session's launch scope (manifest PRs + anchor for batch, the
    focus issue for investigations) and investigations must include the focus
    issue (#6761 F2 + re-review F2).
    """
    data_dir = session.run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    proposed_actions = [
        {
            "id": f"A{index}",
            "action_type": "post_comment",
            "target_number": target,
            "body": f"Diagnosis for #{target}: flaky CI.",
            "finding_ids": ["T1"],
        }
        for index, target in enumerate(comment_targets, start=1)
    ]
    (data_dir / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "One systemic pattern found.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "Flaky CI",
                        "classification": "infra",
                        "evidence": ["orchestrator log lines 10-20"],
                    }
                ],
                "proposed_actions": proposed_actions,
            }
        )
    )
    action_ids = ", ".join(action["id"] for action in proposed_actions)
    (data_dir / "tech-lead-report.md").write_text(
        f"# Report\n\nT1 leads to {action_ids or 'no actions'}.\n"
    )


def _tech_lead_labels(actions: tuple[object, ...]) -> list[AddLabelAction]:
    return [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "tech-lead-reviewed"
    ]


def test_completed_tech_lead_session_labels_manifest_prs_and_plans_decision(
    tmp_path: Path,
) -> None:
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    plant_tech_lead_decision_pair(session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert {action.issue_number for action in _tech_lead_labels(actions)} == {101, 102}
    assert "in-progress" in removed_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction) and action.number == 101
    ]
    assert len(decision_comments) == 1
    assert decision_comments[0].comment.startswith("Diagnosis for #101: flaky CI.")
    assert "ADR-0031" in decision_comments[0].comment


def test_completed_tech_lead_session_missing_pair_fails_labels_and_surfaces_rejection(
    tmp_path: Path,
) -> None:
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    # No decision artifact pair written: contract violation, no grace path.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    failed_actions = [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "tech-lead-failed"
    ]
    assert {action.issue_number for action in failed_actions} == {101, 102}
    assert "tech-lead-reviewed" not in added_labels(actions)
    rejections = [
        action for action in actions if isinstance(action, SurfaceTechLeadProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert rejections[0].proposal_type == "decision"
    assert rejections[0].issue_number == session.issue.number
    assert "tech-lead-decision.json" in rejections[0].body_preview


def test_tech_lead_manifest_in_sibling_run_dir_is_ignored(tmp_path: Path) -> None:
    """Completion reads only ``session.run_dir`` (typed run contract).

    The pre-#6769 code scanned every run dir under the worktree's sessions
    root and could pick up a stale prior run's manifest. A manifest planted
    in a sibling run dir must now be invisible: no labels on its PRs.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    plant_tech_lead_assignment(
        session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
    )
    plant_tech_lead_decision_pair(session)
    stale_run_dir = session.run_dir.parent / "20250101T000000000000Z__issue-1"
    stale_run_dir.mkdir(parents=True)
    stale_manifest = TechLeadManifest(
        prs=[
            PRToReview(
                number=999, title="Stale PR", url="https://example/pr/999", branch="s"
            )
        ]
    )
    stale_manifest_path = tmp_path / "stale-tech-lead-manifest.json"
    stale_manifest.write(stale_manifest_path)
    (stale_run_dir / "manifest.json").write_text(
        json.dumps({"tech_lead_manifest": str(stale_manifest_path)})
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    tech_lead_label_targets = {
        action.issue_number
        for action in actions
        if isinstance(action, AddLabelAction)
        and action.label in ("tech-lead-reviewed", "tech-lead-failed")
    }
    assert tech_lead_label_targets == set()


def test_completed_tech_lead_investigation_session_plans_decision_without_labels(
    tmp_path: Path,
) -> None:
    """Failure investigations plan decision actions but never manifest labels.

    Flavor comes from the launch-time assignment (#6768 B4) — both tech_lead
    variants share the issue-N terminal, so the manifest planted here must
    still not be labeled.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(1,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert "tech-lead-reviewed" not in added_labels(actions)
    assert "tech-lead-failed" not in added_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction) and action.number == 1
        and "Diagnosis" in action.comment
    ]
    assert len(decision_comments) == 1


def test_completed_non_tech_lead_session_is_unaffected(tmp_path: Path) -> None:
    config = make_tech_lead_config(tmp_path)
    session = make_session(tmp_path)  # agent:test, not the tech lead agent

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, SurfaceTechLeadProposalAction) for a in actions)
    assert added_labels(actions) == set()
    assert "in-progress" in removed_labels(actions)


def _close_actions(actions: tuple[object, ...]) -> list[CloseIssueAction]:
    return [a for a in actions if isinstance(a, CloseIssueAction)]


def _tech_lead_failed_labels(actions: tuple[object, ...]) -> list[AddLabelAction]:
    return [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "tech-lead-failed"
    ]


def test_failed_batch_labels_prs_failed_and_closes_tracking_issue(
    tmp_path: Path,
) -> None:
    """A FAILED batch reaches the tech-lead-failed contract (#6768 r5).

    Manifest PRs carry the operator-visible tech-lead-failed label and the
    tracking issue closes (after the generic needs-human diagnosis and the PR
    labels) so restart recovery cannot requeue it with an empty manifest.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.FAILED,
    )

    assert {a.issue_number for a in _tech_lead_failed_labels(actions)} == {101, 102}
    (close,) = _close_actions(actions)
    assert close.issue_number == session.issue.number
    # Composes with (not replaces) the generic failure diagnosis...
    assert "needs-human" in added_labels(actions)
    # ...and the terminal close comes after every label action.
    assert actions.index(close) == len(actions) - 1


def test_timed_out_batch_labels_prs_failed_and_closes_tracking_issue(
    tmp_path: Path,
) -> None:
    """A TIMED_OUT batch gets the same terminal lifecycle as a failed one."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.TIMED_OUT,
    )

    assert {a.issue_number for a in _tech_lead_failed_labels(actions)} == {101, 102}
    (close,) = _close_actions(actions)
    assert close.issue_number == session.issue.number
    # Composes with the generic timeout diagnosis; close is last.
    assert "blocked-failed" in added_labels(actions)
    assert actions.index(close) == len(actions) - 1


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_failure_investigation_failure_paths_preserve_source_issue(
    tmp_path: Path, status: SessionStatus
) -> None:
    """Failed/timed-out investigations never touch manifest PRs or close their
    anchor — it IS the original failed work issue (#6768 r5 controls)."""
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    plant_tech_lead_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(session, status)

    assert _close_actions(actions) == []
    assert _tech_lead_failed_labels(actions) == []
    assert _tech_lead_labels(actions) == []


def test_failure_investigation_tech_lead_session_never_labels_manifest_prs(
    tmp_path: Path,
) -> None:
    """A focused investigation must not label PRs even when a manifest exists (#6768 B4)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    plant_tech_lead_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _tech_lead_labels(actions) == []
    assert "in-progress" in removed_labels(actions)


def test_completed_health_review_plans_decision_and_closes_anchor(
    tmp_path: Path,
) -> None:
    """Health review + valid pair: decision actions, close the anchor, no labels.

    The anchor issue is a walk-the-floor log entry (ADR-0031 §4) — a landed
    review closes it, and manifest labels never apply (there is no manifest).
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    # Even stray planted manifest noise must not be labeled for this flavor.
    plant_tech_lead_manifest(tmp_path, session)
    plant_tech_lead_decision_pair(session, comment_targets=(session.issue.number,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert "tech-lead-reviewed" not in added_labels(actions)
    assert "tech-lead-failed" not in added_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction)
        and action.number == session.issue.number
        and "Diagnosis" in action.comment
    ]
    assert len(decision_comments) == 1
    (close,) = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert close.issue_number == session.issue.number
    assert "Health review completed" in close.reason
    # Terminal ordering: a mid-apply crash leaves the anchor open.
    assert actions.index(close) == len(actions) - 1


def test_health_review_missing_pair_surfaces_rejection_and_keeps_anchor_open(
    tmp_path: Path,
) -> None:
    """Missing/invalid pair: rejection surfaced, anchor NOT closed (visibility)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    # No decision artifact pair written.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    rejections = [
        action for action in actions if isinstance(action, SurfaceTechLeadProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert not any(isinstance(a, CloseIssueAction) for a in actions)


def test_health_review_decision_targeting_anchor_passes_scope_validation(
    tmp_path: Path,
) -> None:
    """The anchor issue is the ONE allowed target for health post_comment."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(session.issue.number,))

    error = tech_lead_decision_processing_error(
        config,
        tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )

    assert error is None


def test_health_review_decision_targeting_other_issue_is_rejected(
    tmp_path: Path,
) -> None:
    """A health decision may not address arbitrary issues (#6761 rr F2 scope).

    Board-wide findings belong in scope-free create_issue/flag_pattern
    proposals; a post_comment outside the anchor is a contract violation on
    both completion seams (processing outcome AND planned effects).
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(999,))

    error = tech_lead_decision_processing_error(
        config,
        tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )
    assert error is not None
    assert "outside this session's launch scope" in error

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )
    rejections = [
        action for action in actions if isinstance(action, SurfaceTechLeadProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    # And the out-of-scope comment is never planned.
    assert not any(
        isinstance(a, AddCommentAction) and a.number == 999 for a in actions
    )


def _plant_flag_pattern_decision(
    session: Session, *, signature: str = "db-timeout", area: str = "db"
) -> None:
    """Plant a health-review decision whose only action is a flag_pattern
    (scope-free board-wide finding, #6781)."""
    data_dir = session.run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Recurring cross-job pattern found.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "DB pool exhausted",
                        "classification": "infra",
                        "evidence": ["orchestrator log lines 10-20"],
                    }
                ],
                "proposed_actions": [
                    {
                        "id": "A1",
                        "action_type": "flag_pattern",
                        "body": "Three sessions hit the same DB pool timeout.",
                        "pattern_signature": signature,
                        "area": area,
                        "finding_ids": ["T1"],
                    }
                ],
            }
        )
    )
    (data_dir / "tech-lead-report.md").write_text("# Report\n\nT1 leads to A1.\n")


def test_health_review_flag_pattern_is_scope_free_and_opens_case_file(
    tmp_path: Path,
) -> None:
    """A health decision's flag_pattern carries no target: it passes scope
    validation and plans a durable case file for a first-seen signature
    (#6781 acceptance)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    _plant_flag_pattern_decision(session)

    # Scope-free: no out-of-scope error even though it is not the anchor.
    error = tech_lead_decision_processing_error(
        config,
        tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )
    assert error is None

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    [case_file] = [
        a for a in actions if isinstance(a, CreateTechLeadCaseFileIssueAction)
    ]
    assert case_file.pattern_signature == "db-timeout"
    assert case_file.area == "db"
    assert TECH_LEAD_OBSERVATION_LABEL in case_file.labels


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_failed_health_review_closes_anchor_without_labels(
    tmp_path: Path, status: SessionStatus
) -> None:
    """FAILED/TIMED_OUT health sessions close the anchor (no manifest labels).

    An open dead anchor would be requeued at restart AND dedupe the next
    interval's trigger; closing it lets a fresh review fire on schedule.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    plant_tech_lead_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(session, status)

    (close,) = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert close.issue_number == session.issue.number
    assert "Health review session failed" in close.reason
    assert _tech_lead_failed_labels(actions) == []
    assert _tech_lead_labels(actions) == []
    assert actions.index(close) == len(actions) - 1


def test_tech_lead_session_without_launch_authority_is_rejected(
    tmp_path: Path, caplog
) -> None:
    """No orchestrator launch-authority record => never trust worktree copies.

    The old fail-safe (skip effects, PRs re-enter the next batch) let a
    missing assignment reach the success path (#6761 re-review F1); now the
    rejection is surfaced and no labels are planned.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    plant_tech_lead_manifest(tmp_path, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _tech_lead_labels(actions) == []
    rejections = [
        a for a in actions
        if isinstance(a, SurfaceTechLeadProposalAction) and a.mode == "rejected"
    ]
    assert len(rejections) == 1
    assert "launch-authority" in rejections[0].body_preview
    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    assert "Launch authority rejected" in caplog.text


def test_tech_lead_artifacts_in_sibling_run_dir_are_ignored(
    tmp_path: Path, caplog
) -> None:
    """Stale artifacts from a previous run must not leak into this completion.

    Reads go exclusively through the session's typed run_dir (#6768 B6): a
    sibling run carrying a batch assignment and a full manifest produces no
    labels and no decision actions for the current session.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    sibling = session.run_dir.parent / "issue-1__coding-0"
    (sibling / "tech-lead-data").mkdir(parents=True)
    TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW).write(
        sibling / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
    )
    manifest = TechLeadManifest(
        prs=[PRToReview(number=301, title="Stale", url="https://example/pr/301", branch="s1")]
    )
    manifest_path = sibling / "tech-lead-manifest.json"
    manifest.write(manifest_path)
    (sibling / "manifest.json").write_text(
        json.dumps({"tech_lead_manifest": str(manifest_path)})
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, AddLabelAction) and a.label.startswith("tech-lead-") for a in actions)
    assert "Launch authority rejected" in caplog.text


def test_successful_batch_completion_closes_tracking_issue(tmp_path: Path) -> None:
    """Batch success gives the tracking issue a crash-safe terminal state.

    Open+agent-labeled tracking issues are what startup recovery requeues and
    what _find_existing_tech_lead_issue treats as the active batch (#6768 round
    4). Close is ordered after the PR labels so a mid-apply crash leaves the
    batch open and re-auditable. Success requires the valid decision pair.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    plant_tech_lead_decision_pair(session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    closes = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert [c.issue_number for c in closes] == [session.issue.number]
    label_indexes = [
        i for i, a in enumerate(actions)
        if isinstance(a, AddLabelAction) and a.label == "tech-lead-reviewed"
    ]
    assert label_indexes and actions.index(closes[0]) > max(label_indexes)


def test_batch_completion_with_rejected_pair_does_not_close_tracking_issue(
    tmp_path: Path,
) -> None:
    """A contract violation leaves the batch anchor open for re-audit."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    # No decision pair: rejection path.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, CloseIssueAction) for a in actions)


def test_successful_batch_without_manifest_still_closes(tmp_path: Path) -> None:
    """An empty batch (no PRs matched) must not anchor future batches forever."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path, with_manifest=False)
    plant_tech_lead_decision_pair(session, comment_targets=())

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert any(isinstance(a, CloseIssueAction) for a in actions)


def test_failure_investigation_completion_preserves_source_issue(
    tmp_path: Path,
) -> None:
    """An investigation's anchor IS the failed work issue - never close it."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(1,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    assert "tech-lead-reviewed" not in added_labels(actions)
    assert "tech-lead-failed" not in added_labels(actions)


def _rejections(actions: tuple[object, ...]) -> list[SurfaceTechLeadProposalAction]:
    return [
        action for action in actions
        if isinstance(action, SurfaceTechLeadProposalAction) and action.mode == "rejected"
    ]


class TestFailureInvestigationDiagnosisRequired:
    """A failure investigation must publish its diagnosis to the originating
    issue via a post_comment proposal (#6761 finding 2)."""

    def test_empty_proposed_actions_is_contract_violation(self, tmp_path: Path) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        plant_tech_lead_decision_pair(session, comment_targets=())

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "originating issue #1" in rejection.body_preview

    def test_wrong_target_comment_is_contract_violation(self, tmp_path: Path) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        plant_tech_lead_decision_pair(session, comment_targets=(42,))

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview

    def test_correct_target_comment_passes(self, tmp_path: Path) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        plant_tech_lead_decision_pair(session, comment_targets=(1,))

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        diagnosis = [
            action for action in actions
            if isinstance(action, AddCommentAction) and action.number == 1
        ]
        assert diagnosis and "Diagnosis" in diagnosis[0].comment


def _plant_decision_with_actions(session: Session, proposed: list[dict]) -> None:
    """Write a decision pair with explicit proposed actions (T1 evidence set)."""
    data_dir = session.run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Findings and proposals.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "Flaky CI",
                        "classification": "infra",
                        "evidence": ["orchestrator log lines 10-20"],
                    }
                ],
                "proposed_actions": proposed,
            }
        )
    )
    ids = ", ".join(action["id"] for action in proposed)
    (data_dir / "tech-lead-report.md").write_text(
        f"# Report\n\nT1 leads to {ids or 'no actions'}.\n"
    )


class TestDecisionTargetScope:
    """Every targeted proposal must stay inside the immutable launch scope
    (#6761 re-review finding 2) — validated against the authority record,
    never the worktree copies."""

    def _batch(self, tmp_path: Path) -> tuple[Config, Session]:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        return config, session

    def test_in_scope_targets_pass(self, tmp_path: Path) -> None:
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 102,  # manifest PR
                    "body": "Diagnosis.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "escalate_to_human",
                    "target_number": 1,  # anchor tracking issue
                    "body": "Needs a human.",
                },
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        assert {a.issue_number for a in _tech_lead_labels(actions)} == {101, 102}

    def test_out_of_scope_comment_rejected(self, tmp_path: Path) -> None:
        """A batch comment to a non-manifest PR is a confused-deputy attempt."""
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 999,
                    "body": "Out of scope.",
                }
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "#999" in rejection.body_preview
        assert "outside this session's launch scope" in rejection.body_preview
        assert "tech-lead-reviewed" not in added_labels(actions)
        assert not any(isinstance(a, CloseIssueAction) for a in actions)

    def test_out_of_scope_escalation_rejected(self, tmp_path: Path) -> None:
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "escalate_to_human",
                    "target_number": 555,
                    "body": "Escalate elsewhere.",
                }
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview
        # The escalation must not have been planned (no needs-human label).
        assert "needs-human" not in added_labels(actions)

    @pytest.mark.parametrize("act_type", ["reset_retry", "kill_hung_session"])
    def test_out_of_scope_act_level_rejected(
        self, tmp_path: Path, act_type: str
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis.",
                },
                {
                    "id": "A2",
                    "action_type": act_type,
                    "target_number": 777,  # not the focus issue
                    "body": "Rationale.",
                },
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview

    def test_batch_reset_retry_targeting_manifest_pr_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """A batch reset_retry aimed at a manifest PR is a confused deputy.

        The PR number is inside the general launch scope (comments/escalations
        may target manifest PRs), but the issue reset owner would treat it as
        an ``issue_number`` and reset the wrong entity. Act-level targets are
        held to the stricter issue-only scope, which is EMPTY for a batch
        review, so the proposal is rejected — never planned or executed
        (#6764 re-review F1).
        """
        config, session = self._batch(tmp_path)
        # Even with execute authority the confused deputy must be blocked.
        config.tech_lead.authority.reset_retry = "execute"
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 101,  # a manifest PR, NOT a work issue
                    "body": "Scratch reset it.",
                    "finding_ids": ["T1"],
                },
            ],
        )

        # Authoritative processing seam rejects the whole completion.
        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )
        assert error is not None
        assert "#101" in error
        assert "outside this session's launch scope" in error

        # Planning surfaces the rejection and plans NO reset / NO success
        # terminalization (no close, no tech-lead-reviewed labels).
        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )
        [rejection] = _rejections(actions)
        assert "#101" in rejection.body_preview
        assert not any(isinstance(a, ResetRetryIssueAction) for a in actions)
        assert not any(isinstance(a, CloseIssueAction) for a in actions)
        assert "tech-lead-reviewed" not in added_labels(actions)

    def test_health_reset_retry_accepts_snapshot_problem_cohort(
        self, tmp_path: Path
    ) -> None:
        """A health review CAN propose reset_retry for a cohort member.

        Asserted symmetrically with its negative twin: the acceptance criterion
        is that the proposal WORKS, so this checks the typed
        ``ResetRetryIssueAction`` is actually planned for the member — not
        merely that the completion was not rejected.
        """
        config = make_tech_lead_config(tmp_path)
        config.tech_lead.authority.reset_retry = "execute"
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config, session, problem_issue_numbers=(41, 42, 43)
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 42,
                    "body": "Reset the stuck cohort member.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is None

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        [reset] = [a for a in actions if isinstance(a, ResetRetryIssueAction)]
        assert reset.issue_number == 42
        assert reset.proposal_id == "A1"

    def test_health_reset_retry_rejects_issue_outside_snapshot_cohort(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config, session, problem_issue_numbers=(41, 42, 43)
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 99,
                    "body": "Out-of-cohort reset attempt.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is not None
        assert "immutable problem cohort" in error
        assert "(#41, #42, #43)" in error

    def test_context_only_failures_do_not_widen_health_act_level_scope(
        self, tmp_path: Path
    ) -> None:
        """Unrelated board failures are context, not authority (#6780).

        #99 is on the board because its own investigation is pending. The
        health review can SEE it, but reset_retry'ing it is out of scope —
        before this fix the launch authority absorbed every board failure and
        this completion was accepted.
        """
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config,
            session,
            problem_issue_numbers=(41, 42, 43),
            context_failure_numbers=(99,),
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 99,
                    "body": "Reset an issue this review does not own.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is not None
        assert "immutable problem cohort" in error
        assert "(#41, #42, #43)" in error

    def test_context_only_failures_still_pass_the_tamper_check(
        self, tmp_path: Path
    ) -> None:
        """The tamper check reads the cohort surface, not the failure list.

        A snapshot whose ``recent_failures`` legitimately exceed the grant
        must NOT read as tampering (#6780) — otherwise every storm
        review launched alongside an unrelated pending investigation would
        fail its own completion.
        """
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config,
            session,
            problem_issue_numbers=(41, 42, 43),
            context_failure_numbers=(99,),
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 42,
                    "body": "Reset a cohort member.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is None

    def test_health_snapshot_problem_set_tampering_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """Rewriting the snapshot's COHORT surface is still caught."""
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config, session, problem_issue_numbers=(41, 42, 43)
        )
        BoardSnapshot(
            generated_at="2026-07-14T12:01:00+00:00",
            orchestrator_paused=False,
            recent_failures=[
                BoardFailure(99, "Injected problem", "failed", [])
            ],
            problem_cohort=[99],
        ).write(session.run_dir / "tech-lead-data" / "board-snapshot.json")

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is not None
        assert error.startswith("tech_lead_authority: scope_tampered")
        assert "problem set [99]" in error

    def test_duplicate_reset_retry_target_rejects_completion_without_effects(
        self, tmp_path: Path
    ) -> None:
        """One focus issue cannot carry contradictory act-level commands."""
        config = make_tech_lead_config(tmp_path)
        config.tech_lead.authority.reset_retry = "execute"
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis for the originating issue.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "reset_retry",
                    "target_number": 1,
                    "body": "Reset the corrupted worktree.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A3",
                    "action_type": "reset_retry",
                    "target_number": 1,
                    "body": "Retry the same issue from scratch.",
                    "finding_ids": ["T1"],
                },
            ],
        )

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )
        assert error is not None
        assert "multiple act-level proposed actions target #1: A2, A3" in error

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "multiple act-level proposed actions" in rejection.body_preview
        prohibited_effects = (
            ResetRetryIssueAction,
            AddLabelAction,
            AddCommentAction,
            CloseIssueAction,
        )
        assert not any(isinstance(action, prohibited_effects) for action in actions)
        assert removed_labels(actions) == {"in-progress"}


class TestResetRetryExecutionPipeline:
    """Execute-authority reset_retry proposals flow from the decision
    artifact through planning into the reset owner (#6764 first slice)."""

    def _armed_investigation(
        self, tmp_path: Path, *, authority_mode: str
    ) -> tuple[Config, Session]:
        config = make_tech_lead_config(tmp_path)
        config.tech_lead.authority.reset_retry = authority_mode
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "reset_retry",
                    "target_number": 1,  # the focus issue
                    "body": "Scratch reset: worktree unrecoverable.",
                    "finding_ids": ["T1"],
                },
            ],
        )
        return config, session

    def test_execute_authority_plans_typed_reset_action(self, tmp_path: Path) -> None:
        config, session = self._armed_investigation(tmp_path, authority_mode="execute")

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        [reset] = [a for a in actions if isinstance(a, ResetRetryIssueAction)]
        assert reset.issue_number == 1
        assert reset.anchor_issue_number == 1
        assert reset.proposal_id == "A2"
        assert reset.finding_ids == ("T1",)
        # No shadow surface for the executed proposal.
        surfaced = [
            a for a in actions
            if isinstance(a, SurfaceTechLeadProposalAction)
            and a.proposal_type == "reset_retry"
        ]
        assert surfaced == []

    def test_propose_authority_plans_gated_proposal_issue(self, tmp_path: Path) -> None:
        """Propose-authority reset_retry is a gated proposal issue carrying
        the stored op (#6778): never a shadow record or a direct execution."""
        from issue_orchestrator.control.actions import (
            CreateTechLeadProposalIssueAction,
        )
        from issue_orchestrator.domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

        config, session = self._armed_investigation(tmp_path, authority_mode="propose")

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert not any(type(a) is ResetRetryIssueAction for a in actions)
        assert not any(
            isinstance(a, SurfaceTechLeadProposalAction)
            and a.proposal_type == "reset_retry"
            for a in actions
        )
        [proposal] = [
            a for a in actions if isinstance(a, CreateTechLeadProposalIssueAction)
        ]
        assert proposal.op.op_type == "reset_retry"
        assert proposal.op.target_issue_number == 1
        assert proposal.op.source_action_id == "A2"
        assert proposal.op.source_run_id == session.run_assets.run_id
        assert proposal.op.source_session_name == session.run_assets.session_name
        assert PROPOSED_TECH_LEAD_LABEL in proposal.labels

    def test_full_pipeline_invokes_reset_owner(self, tmp_path: Path) -> None:
        """Completed investigation + execute authority -> the reset owner is
        invoked through planner -> applier with the target's fresh labels."""
        from unittest.mock import MagicMock

        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.tech_lead_reset_retry import (
            ResetRetryRunOutcome,
            TechLeadResetRetryExecutor,
        )
        from issue_orchestrator.domain.models import Issue as DomainIssue

        config, session = self._armed_investigation(tmp_path, authority_mode="execute")
        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        run_reset = MagicMock(
            return_value=ResetRetryRunOutcome(success=True, details={"queued_now": True})
        )
        executor = TechLeadResetRetryExecutor(
            events=MagicMock(),
            label_manager=LabelManager(config),
            read_issue=lambda number: DomainIssue(
                number=number,
                title="Focus issue",
                labels=["agent:test", "blocked-failed"],
                repo="owner/repo",
            ),
            has_active_issue_runtime=lambda _number: False,
            run_reset=run_reset,
        )
        applier = ActionApplier(
            labels=MagicMock(),
            sessions=MagicMock(),
            events=MagicMock(),
            repository_host=MagicMock(),
        )
        applier.tech_lead_reset_retry = executor

        results = applier.apply_all(list(actions))

        run_reset.assert_called_once_with(1, ["agent:test", "blocked-failed"])
        assert all(r.result_type is not None for r in results)


class TestLaunchScopeTamperResistance:
    """The agent-writable worktree copies carry zero authority (#6761
    re-review finding 1)."""

    def test_flavor_flip_after_launch_fails_session(self, tmp_path: Path) -> None:
        """The reviewer's repro: an investigation flipping its worktree
        assignment to batch_review must fail, never close the originating
        issue."""
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        # Agent tampering: rewrite the worktree copy to the batch flavor.
        plant_tech_lead_assignment(
            session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
        )
        plant_tech_lead_decision_pair(session, comment_targets=())

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )
        assert error is not None and error.startswith(
            "tech_lead_authority: scope_tampered"
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED, processing_errors=[error]
        )

        assert not any(isinstance(a, CloseIssueAction) for a in actions)
        assert "tech-lead-reviewed" not in added_labels(actions)
        assert "blocked-failed" in added_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number

    def test_deleted_assignment_fails_session(self, tmp_path: Path) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        assignment_path = (
            session.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        )
        assignment_path.unlink()

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is not None and error.startswith(
            "tech_lead_authority: scope_tampered"
        )
        assert "missing" in error

    def test_tampered_manifest_prs_never_get_labels(self, tmp_path: Path) -> None:
        """Substituted PR numbers in the worktree manifest must not be
        labeled — labels come from the authority set, and the divergence
        itself fails the session."""
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        # Agent tampering: substitute the manifest PR set.
        tampered = TechLeadManifest(
            prs=[PRToReview(number=999, title="Sub", url="https://x/999", branch="s")]
        )
        manifest_path = tmp_path / "tech-lead-manifest.json"
        tampered.write(manifest_path)

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )
        assert error is not None and error.startswith(
            "tech_lead_authority: scope_tampered"
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED, processing_errors=[error]
        )

        assert not any(
            isinstance(a, AddLabelAction) and a.issue_number == 999 for a in actions
        )
        # The authority set still records the failure on the REAL PRs.
        assert {a.issue_number for a in _tech_lead_failed_labels(actions)} == {101, 102}

    def test_missing_authority_is_critical_in_processing_path(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        plant_tech_lead_assignment(
            session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
        )

        error = tech_lead_decision_processing_error(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is not None and error.startswith(
            "tech_lead_authority: missing_authority"
        )


def test_protected_agent_label_on_create_issue_rejects_decision(
    tmp_path: Path,
) -> None:
    """Untrusted agent labels may not touch workflow truth (#6761 finding 4)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    _plant_decision_with_actions(
        session,
        [
            {
                "id": "A1",
                "action_type": "create_issue",
                "title": "Follow-up",
                "body": "Fix it.",
                "labels": ["in-progress"],
                "finding_ids": ["T1"],
            }
        ],
    )

    actions = make_planner(config).generate_completion_actions(
        session, SessionStatus.COMPLETED
    )

    [rejection] = _rejections(actions)
    assert "protected" in rejection.body_preview
    assert "in-progress" in rejection.body_preview
    assert not any(
        isinstance(a, AddLabelAction) and a.label == "tech-lead-reviewed" for a in actions
    )


class TestTechLeadDecisionFailureTransition:
    """A rejected pair rides the critical-error seam: FAILED history plus the
    blocked/failed labeling path for the session's own issue (#6761 finding 3)."""

    ERROR = "tech_lead_decision: contract_violation: finding T1 has no evidence"

    def test_error_prefix_is_critical(self) -> None:
        critical, downgraded = critical_processing_errors([self.ERROR])
        assert critical == [self.ERROR]
        assert downgraded == []

    def test_authority_error_prefix_is_critical(self) -> None:
        error = "tech_lead_authority: scope_tampered: assignment flipped"
        critical, downgraded = critical_processing_errors([error])
        assert critical == [error]
        assert downgraded == []

    def test_batch_flavor_fails_manifest_and_blocks_own_issue(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.ERROR],
        )

        failed = [
            a for a in actions
            if isinstance(a, AddLabelAction) and a.label == "tech-lead-failed"
        ]
        assert {a.issue_number for a in failed} == {101, 102}
        assert "blocked-failed" in added_labels(actions)
        assert "publish-failed" not in added_labels(actions)
        assert "in-progress" in removed_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number
        assert "finding T1 has no evidence" in rejection.body_preview
        assert any(
            "Tech Lead completion rejected" in comment for comment in comments(actions)
        )
        assert not any(isinstance(a, CloseIssueAction) for a in actions)

    def test_investigation_flavor_blocks_own_issue_without_manifest_labels(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        plant_tech_lead_manifest(tmp_path, session)  # planted noise: must stay unread

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.ERROR],
        )

        assert "tech-lead-failed" not in added_labels(actions)
        assert "blocked-failed" in added_labels(actions)
        assert "in-progress" in removed_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number
        assert any(
            "Tech Lead completion rejected" in comment for comment in comments(actions)
        )


def test_interrupted_retry_adds_guard_and_keeps_retry_loop_bounded(tmp_path: Path) -> None:
    config = Config()
    config.retry.interrupted_sessions.enabled = True
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
    )

    assert config.retry.interrupted_sessions.coding_guard_label in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Interrupted" in comment for comment in comments(actions))


def test_create_pr_error_is_downgraded_when_pr_exists(caplog) -> None:
    critical, downgraded = critical_processing_errors(
        [f"{ERROR_PREFIX_CREATE_PR}: 422 already exists"],
        pr_url="https://github.com/owner/repo/pull/5",
        issue_number=5,
        log_downgraded=True,
        context="test",
    )

    assert critical == []
    assert downgraded == [f"{ERROR_PREFIX_CREATE_PR}: 422 already exists"]
    assert "Ignoring non-blocking create_pr processing errors" in caplog.text


class TestProductionOpenIssueDedupCorpus:
    """Completion reads the SQL-backed corpus; planning performs no GitHub read."""

    @staticmethod
    def _plant_duplicate_pair(session: Session) -> None:
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis for the focus issue.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "create_issue",
                    "title": "Stabilize flaky CI runner",
                    "body": "Runner disconnects mid-build.",
                    "duplicate_of": 1,
                    "finding_ids": ["T1"],
                },
            ],
        )

    def test_execute_routes_verified_open_issue_duplicate_to_comment(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.domain.open_issue_corpus import (
            build_open_issue_fingerprint,
        )

        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_duplicate_pair(session)
        store = SqliteOpenIssueCorpusStore.for_repo(tmp_path)
        store.replace_all(
            (
                build_open_issue_fingerprint(
                    1, "Stabilize flaky CI runner", "Runner disconnects mid-build."
                ),
            ),
            watermark="2026-07-23T12:00:00Z",
        )
        repository_host = MagicMock(spec=RepositoryHost)

        actions = make_planner(
            config,
            repository_host=repository_host,
            open_issue_corpus_store=store,
        ).generate_completion_actions(session, SessionStatus.COMPLETED)

        repository_host.list_issues.assert_not_called()
        repository_host.list_issues_delta.assert_not_called()
        assert not any(
            isinstance(action, CreateTechLeadIssueAction) for action in actions
        )
        assert any(
            isinstance(action, AddCommentAction)
            and action.number == 1
            and "deduplicated follow-up" in action.comment
            for action in actions
        )

    def test_propose_gates_verified_open_issue_duplicate(
        self, tmp_path: Path
    ) -> None:
        from issue_orchestrator.domain.open_issue_corpus import (
            build_open_issue_fingerprint,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            PROPOSED_TECH_LEAD_LABEL,
        )

        config = make_tech_lead_config(tmp_path)
        config.tech_lead.authority.create_issue = "propose"
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_duplicate_pair(session)
        store = SqliteOpenIssueCorpusStore.for_repo(tmp_path)
        store.replace_all(
            (
                build_open_issue_fingerprint(
                    1, "Stabilize flaky CI runner", "Runner disconnects mid-build."
                ),
            ),
            watermark="2026-07-23T12:00:00Z",
        )

        actions = make_planner(
            config,
            open_issue_corpus_store=store,
        ).generate_completion_actions(session, SessionStatus.COMPLETED)

        [gated] = [
            action for action in actions if isinstance(action, CreateTechLeadIssueAction)
        ]
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert "DUPLICATE of #1" in gated.body
        assert not any(
            isinstance(action, AddCommentAction)
            and "deduplicated follow-up" in action.comment
            for action in actions
        )


class TestMilestoneResolutionBoundary:
    """The completion seam plans milestone INTENT only (#6769 finding 4).

    Under ``create_issue: propose`` (shadow) the decision must complete with
    ZERO GitHub reads — the reviewer reproduced a ``list_milestones`` lookup
    failure failing the completion for an issue that would never be created.
    Under execute, the name still travels as intent; the applier resolves it.
    """

    def _plant_pair_with_create_issue(self, session: Session) -> None:
        data_dir = session.run_dir / "tech-lead-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "tech-lead-decision.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "summary": "One systemic pattern found.",
                    "findings": [
                        {
                            "id": "T1",
                            "title": "Flaky CI",
                            "classification": "infra",
                            "evidence": ["orchestrator log lines 10-20"],
                        }
                    ],
                    "proposed_actions": [
                        {
                            "id": "A1",
                            "action_type": "post_comment",
                            "target_number": 1,
                            "body": "Diagnosis for #1: flaky CI.",
                            "finding_ids": ["T1"],
                        },
                        {
                            "id": "A2",
                            "action_type": "create_issue",
                            "title": "Stabilize CI runner",
                            "body": "Runner disconnects mid-build.",
                            "labels": ["bug"],
                            "finding_ids": ["T1"],
                        },
                    ],
                }
            )
        )
        (data_dir / "tech-lead-report.md").write_text(
            "# Report\n\nFinding T1: flaky CI.\n\nProposals: A1, A2.\n"
        )

    def _completed_actions(self, config: Config, session: Session, host) -> tuple:
        return make_planner(
            config, repository_host=cast(RepositoryHost, host)
        ).generate_completion_actions(session, SessionStatus.COMPLETED)

    def test_gated_create_issue_with_explicit_milestone_makes_zero_reads(
        self, tmp_path: Path
    ) -> None:
        """Propose-authority create_issue is a GATED creation now (#6778):
        it still plans milestone INTENT with zero GitHub reads, and the
        planned issue carries the proposed-tech-lead gate label."""
        from unittest.mock import MagicMock

        from issue_orchestrator.domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

        config = make_tech_lead_config(tmp_path)
        config.tech_lead.milestone_strategy.explicit = "M5"
        config.tech_lead.authority.create_issue = "propose"
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_pair_with_create_issue(session)
        host = MagicMock()

        actions = self._completed_actions(config, session, host)

        host.list_milestones.assert_not_called()
        assert not any(
            isinstance(action, SurfaceTechLeadProposalAction)
            and action.proposal_type == "create_issue"
            for action in actions
        )
        [create] = [
            action for action in actions if isinstance(action, CreateTechLeadIssueAction)
        ]
        assert PROPOSED_TECH_LEAD_LABEL in create.labels

    def test_execute_create_issue_plans_name_intent_without_reads(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.control.actions import TechLeadMilestoneIntent

        config = make_tech_lead_config(tmp_path)
        config.tech_lead.milestone_strategy.explicit = "M5"
        assert config.tech_lead.authority.mode_for("create_issue") == "execute"
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_pair_with_create_issue(session)
        host = MagicMock()

        actions = self._completed_actions(config, session, host)

        host.list_milestones.assert_not_called()
        [create] = [
            action for action in actions if isinstance(action, CreateTechLeadIssueAction)
        ]
        assert create.milestone == TechLeadMilestoneIntent(explicit_name="M5")
