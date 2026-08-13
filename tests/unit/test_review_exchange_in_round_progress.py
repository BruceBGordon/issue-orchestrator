"""In-round review-exchange progress surfacing (issue #6428).

During an in-round rework the Story timeline used to freeze on "Code review
started" for minutes while the reviewer had already responded and the coder
was reworking — an "information void". These tests pin the producer -> view
model boundary: while a review round is still open, the latest substate is
surfaced as a single transient ``in_round_progress`` row; once the round
closes, the completed Story view stays clean (no mechanic rows survive).
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.view_models.journey_projection import build_journey_step
from issue_orchestrator.view_models.lifecycle_semantics import JourneyStep
from tests.unit.review_timeline_scenario import ReviewTimelineScenario


def _open_rework_scenario(tmp_path: Path) -> ReviewTimelineScenario:
    return (
        ReviewTimelineScenario.create(tmp_path)
        .with_reviewer_round(round_index=1)
        .with_coder_round(round_index=1)
    )


def test_open_round_coder_reworking_surfaces_live_progress_row(tmp_path: Path) -> None:
    scenario = _open_rework_scenario(tmp_path)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
        scenario.review_role_prompted(round_index=1, role="reviewer"),
        scenario.review_role_feedback(
            round_index=1, role="reviewer", response_type="changes_requested"
        ),
        scenario.review_role_prompted(round_index=1, role="coder"),
    )

    # The frozen "Code review started" row is now followed by a live row.
    detail.assert_step_events(
        "review_exchange.round_started",
        "review_exchange.role_prompted",
    )
    progress = detail.step("review_exchange.role_prompted")
    assert progress["in_round_progress"] is True
    assert progress["narrative"] == "Coder running (round 1)"


def test_open_round_reviewer_requested_changes_surfaces_live_progress_row(
    tmp_path: Path,
) -> None:
    # Latest substate is the reviewer's changes_requested verdict — the coder
    # prompt has not been emitted yet.
    scenario = _open_rework_scenario(tmp_path)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
        scenario.review_role_prompted(round_index=1, role="reviewer"),
        scenario.review_role_feedback(
            round_index=1, role="reviewer", response_type="changes_requested"
        ),
    )

    detail.assert_step_events(
        "review_exchange.round_started",
        "review_exchange.role_feedback",
    )
    progress = detail.step("review_exchange.role_feedback")
    assert progress["in_round_progress"] is True
    assert progress["narrative"] == "Coder needs requested changes (round 1)"


def test_reviewer_opening_pass_does_not_duplicate_code_review_started(
    tmp_path: Path,
) -> None:
    # While the reviewer is still on its opening pass, "Code review started"
    # already represents the state — no redundant progress row is added.
    scenario = ReviewTimelineScenario.create(tmp_path).with_reviewer_round(round_index=1)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
        scenario.review_role_prompted(round_index=1, role="reviewer"),
    )

    detail.assert_step_events("review_exchange.round_started")
    assert detail.step("review_exchange.round_started").get("in_round_progress") in (
        None,
        False,
    )


def test_completed_round_stays_clean_no_progress_row(tmp_path: Path) -> None:
    # Once the round closes, the completed Story view collapses to a single
    # terminal row with no surviving mechanic / progress rows.
    scenario = _open_rework_scenario(tmp_path)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
        scenario.review_role_prompted(round_index=1, role="reviewer"),
        scenario.review_role_feedback(
            round_index=1, role="reviewer", response_type="changes_requested"
        ),
        scenario.review_role_prompted(round_index=1, role="coder"),
        scenario.review_role_feedback(round_index=1, role="coder", response_type="ok"),
        scenario.review_round_completed(round_index=1),
    )

    detail.assert_step_events("review_exchange.round_completed")
    terminal = detail.step("review_exchange.round_completed")
    assert terminal.get("in_round_progress") in (None, False)


def test_journey_step_carries_in_round_progress_as_a_typed_field() -> None:
    # The in-round progress decision is owned upstream by a typed projection
    # boundary: the transient row's marker must survive as a typed bool on the
    # ``JourneyStep`` model (not just as a loose dict key), so the UI renders a
    # live affordance distinct from completed steps. This pins that owner
    # boundary (issue #6428).
    live = build_journey_step(
        {
            "event": "review_exchange.role_prompted",
            "status": "completed",
            "narrative": "Coder running (round 1)",
            "timestamp": "2026-03-22T13:34:33Z",
            "in_round_progress": True,
        },
        today="2026-03-22",
    )
    assert isinstance(live, JourneyStep)
    assert live.in_round_progress is True

    ordinary = build_journey_step(
        {
            "event": "review.approved",
            "status": "completed",
            "narrative": "Reviewer approved",
            "timestamp": "2026-03-22T13:50:04Z",
        },
        today="2026-03-22",
    )
    assert ordinary.in_round_progress is False


def test_journey_step_carries_visible_timeline_evidence_state() -> None:
    step = build_journey_step(
        {
            "event": "session.failed",
            "status": "failed",
            "timestamp": "2026-03-22T13:50:04Z",
            "evidence": {
                "run_dir": "/runs/failed-42",
                "status": "pinned",
                "label": "Pinned",
                "available": True,
                "pinned": True,
                "archived": True,
                "expires_at": None,
                "help_text": "Retained until unpinned.",
                "unpin_expires_immediately": False,
            },
        },
        today="2026-03-22",
    )

    assert step.evidence is not None
    assert step.evidence.status == "pinned"
