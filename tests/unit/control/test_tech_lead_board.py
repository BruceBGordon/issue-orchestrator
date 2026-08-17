"""Tests for the rung-1 tech_lead board projection + publisher (#6781, #7014).

The board is an orchestrator-authored projection of the tech_lead ledgers and
the gate labels the tick observed: a frozen view built from data the tick
already holds (zero GitHub calls) and a deterministic markdown renderer the
publisher throttles by content comparison.
"""

from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.control.tech_lead_board import (
    TECH_LEAD_BOARD_FILENAME,
    TechLeadBoardPublisher,
    tech_lead_board_path,
)
from issue_orchestrator.domain.models import TechLeadFacts
from issue_orchestrator.domain.tech_lead_session import (
    GatedTechLeadProposal,
    StoredTechLeadOp,
    TechLeadCaseFileSummary,
)
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.view_models.tech_lead_board import (
    TechLeadBoardCaseFile,
    TechLeadBoardProposal,
    TechLeadBoardProposalOp,
    TechLeadBoardView,
    _proposal_age_hours,
    build_tech_lead_board_view,
    render_tech_lead_board_md,
)

UTC = timezone.utc


def _op(target: int = 13, *, op_type: str = "reset_retry", created_at: str) -> StoredTechLeadOp:
    return StoredTechLeadOp(
        op_type=op_type,
        target_issue_number=target,
        rationale="r",
        source_run_id="run-1",
        source_session_name="issue-99",
        source_action_id="A2",
        created_at=created_at,
    )


def _gated(number: int, *, title: str = "", created_at: str = "") -> GatedTechLeadProposal:
    return GatedTechLeadProposal(
        issue_number=number,
        title=title or f"Proposed follow-up #{number}",
        created_at=created_at,
    )


def _summary(number: int, *, comments: int = 0, updated_at: str = "", area: str = "") -> TechLeadCaseFileSummary:
    return TechLeadCaseFileSummary(
        issue_number=number,
        title=f"Pattern case file: sig-{number}",
        comment_count=comments,
        updated_at=updated_at,
        area=area,
    )


# --- Age helper -----------------------------------------------------------


def test_proposal_age_hours_whole_hours() -> None:
    now = datetime(2026, 7, 11, 5, 30, tzinfo=UTC)
    assert _proposal_age_hours("2026-07-11T00:00:00+00:00", now) == 5


def test_proposal_age_hours_assumes_utc_for_naive_timestamp() -> None:
    now = datetime(2026, 7, 11, 3, 0, tzinfo=UTC)
    assert _proposal_age_hours("2026-07-11T00:00:00", now) == 3


def test_proposal_age_hours_unparseable_is_zero() -> None:
    assert _proposal_age_hours("not-a-date", datetime.now(UTC)) == 0


# --- View projection ------------------------------------------------------


def test_build_view_sorts_proposals_by_issue_number_with_ages() -> None:
    now = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
    ops = [
        (501, _op(14, created_at="2026-07-11T03:00:00+00:00")),
        (500, _op(13, created_at="2026-07-11T00:00:00+00:00")),
    ]

    view = build_tech_lead_board_view(
        ops=ops, gated_proposals=(), case_files=(), area_counts=(),
        last_health_review_at=0.0, now=now,
    )

    assert [p.proposal_issue_number for p in view.open_proposals] == [500, 501]
    assert view.open_proposals[0].age_hours == 5
    assert view.open_proposals[1].age_hours == 2
    assert view.open_proposals[0].op == TechLeadBoardProposalOp(
        op_type="reset_retry", target_issue_number=13
    )


def test_build_view_surfaces_gated_proposals_without_a_ledger_row() -> None:
    """The #7014 regression: label truth alone must reach the board.

    Promoted findings and proposed follow-up issues carry the approval gate
    with no ``tech_lead_proposal_ops`` row at all, so a projection built from
    the ledger renders an EMPTY backlog while they wait on the operator.
    """
    view = build_tech_lead_board_view(
        ops=(),  # the empty ledger that made the board print "None."
        gated_proposals=(
            _gated(6922, title="[P1-003] fix the thing", created_at="2026-07-11T00:00:00+00:00"),
        ),
        case_files=(), area_counts=(), last_health_review_at=0.0,
        now=datetime(2026, 7, 11, 5, 0, tzinfo=UTC),
    )

    [proposal] = view.open_proposals
    assert proposal.proposal_issue_number == 6922
    assert proposal.title == "[P1-003] fix the thing"
    assert proposal.age_hours == 5
    assert proposal.op is None  # nothing to execute; approval releases the issue


def test_build_view_merges_a_ledger_op_with_its_observed_issue() -> None:
    """One row, both halves: operation from the ledger, title from the scan."""
    view = build_tech_lead_board_view(
        ops=[(500, _op(13, created_at="2026-07-11T00:00:00+00:00"))],
        gated_proposals=(
            _gated(500, title="Tech Lead proposal: reset & retry issue #13"),
        ),
        case_files=(), area_counts=(), last_health_review_at=0.0,
        now=datetime(2026, 7, 11, 5, 0, tzinfo=UTC),
    )

    [proposal] = view.open_proposals
    assert proposal.title == "Tech Lead proposal: reset & retry issue #13"
    assert proposal.op == TechLeadBoardProposalOp(
        op_type="reset_retry", target_issue_number=13
    )
    # Age still comes from the ledger's own record of when the op was filed.
    assert proposal.age_hours == 5


def test_build_view_ranks_case_files_by_comment_cadence() -> None:
    now = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
    case_files = (
        _summary(700, comments=3, updated_at="2026-07-10T00:00:00+00:00"),
        _summary(701, comments=5, updated_at="2026-07-09T00:00:00+00:00"),
    )

    view = build_tech_lead_board_view(
        ops=(), gated_proposals=(), case_files=case_files, area_counts=(),
        last_health_review_at=0.0, now=now,
    )

    # The higher comment count (the severity signal) ranks first.
    assert [c.issue_number for c in view.case_files] == [701, 700]


def test_build_view_breaks_comment_ties_by_most_recent_update() -> None:
    view = build_tech_lead_board_view(
        ops=(), gated_proposals=(), case_files=(
            _summary(700, comments=3, updated_at="2026-07-09T00:00:00+00:00"),
            _summary(701, comments=3, updated_at="2026-07-10T00:00:00+00:00"),
        ), area_counts=(), last_health_review_at=0.0,
        now=datetime(2026, 7, 11, 5, 0, tzinfo=UTC),
    )
    assert [case.issue_number for case in view.case_files] == [701, 700]


def test_build_view_formats_last_health_review_from_epoch() -> None:
    now = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
    ts = datetime(2026, 7, 11, 0, 0, tzinfo=UTC).timestamp()

    view = build_tech_lead_board_view(
        ops=(), gated_proposals=(), case_files=(), area_counts=(),
        last_health_review_at=ts, now=now,
    )

    assert view.last_health_review == "2026-07-11T00:00:00+00:00"


def test_build_view_last_health_review_empty_when_never() -> None:
    view = build_tech_lead_board_view(
        ops=(), gated_proposals=(), case_files=(), area_counts=(),
        last_health_review_at=0.0, now=datetime.now(UTC),
    )
    assert view.last_health_review == ""


# --- Golden markdown render -----------------------------------------------


POPULATED_VIEW = TechLeadBoardView(
    open_proposals=(
        TechLeadBoardProposal(
            proposal_issue_number=500,
            age_hours=5,
            title="Tech Lead proposal: reset & retry issue #13",
            op=TechLeadBoardProposalOp(op_type="reset_retry", target_issue_number=13),
        ),
        TechLeadBoardProposal(
            proposal_issue_number=6922,
            age_hours=456,
            title="[P1-003] fix the thing",
        ),
    ),
    case_files=(
        TechLeadBoardCaseFile(
            issue_number=700,
            title="Pattern case file: db-timeout",
            comment_count=3,
            updated_at="2026-07-11T12:00:00+00:00",
            area="db",
        ),
    ),
    area_counts=(("db", 2), ("api", 1)),
    last_health_review="2026-07-11T00:00:00+00:00",
)

POPULATED_GOLDEN = """\
# Tech Lead Board

Orchestrator-authored projection of the tech_lead ledgers and the observed \
approval gates (ADR-0031 / #6781, #7014).

Last health review: 2026-07-11T00:00:00+00:00

## Open proposals

2 awaiting operator approval — remove the `proposed-tech-lead` label from a \
proposal to approve it.

| Proposal | Operation | Target | Age | Title |
|---|---|---|---|---|
| #500 | `reset_retry` | #13 | 5h | Tech Lead proposal: reset & retry issue #13 |
| #6922 | — | — | 19d | [P1-003] fix the thing |

`—` operation: gated issue with no act-level operation recorded (a proposed \
follow-up or promoted finding); approving it releases the issue for scheduling.

## Open pattern case files

| Case file | Title | Comments | Updated | Area |
|---|---|---|---|---|
| #700 | Pattern case file: db-timeout | 3 | 2026-07-11T12:00:00+00:00 | db |

## Case files by area

- db: 2
- api: 1
"""

EMPTY_GOLDEN = """\
# Tech Lead Board

Orchestrator-authored projection of the tech_lead ledgers and the observed \
approval gates (ADR-0031 / #6781, #7014).

Last health review: never

## Open proposals

None.

## Open pattern case files

None.

## Case files by area

None.
"""


def test_render_populated_board_is_golden() -> None:
    assert render_tech_lead_board_md(POPULATED_VIEW) == POPULATED_GOLDEN


def test_render_empty_board_is_golden() -> None:
    empty = TechLeadBoardView(
        open_proposals=(), case_files=(), area_counts=(), last_health_review=""
    )
    assert render_tech_lead_board_md(empty) == EMPTY_GOLDEN


def test_render_omits_the_ledgerless_note_when_every_proposal_has_an_op() -> None:
    """The footnote explains em-dashed rows; it must not appear without them."""
    view = TechLeadBoardView(
        open_proposals=(
            TechLeadBoardProposal(
                proposal_issue_number=500,
                age_hours=5,
                op=TechLeadBoardProposalOp(
                    op_type="reset_retry", target_issue_number=13
                ),
            ),
        ),
        case_files=(), area_counts=(), last_health_review="",
    )

    rendered = render_tech_lead_board_md(view)

    assert "no act-level operation recorded" not in rendered
    # A ledger row the tick never observed on GitHub still renders a full row.
    assert "| #500 | `reset_retry` | #13 | 5h | — |" in rendered


def test_render_is_deterministic() -> None:
    assert render_tech_lead_board_md(POPULATED_VIEW) == render_tech_lead_board_md(POPULATED_VIEW)


def test_render_escapes_table_breaking_issue_text() -> None:
    view = TechLeadBoardView(
        open_proposals=(
            TechLeadBoardProposal(
                proposal_issue_number=6922, age_hours=1, title="Fix | the\nthing",
            ),
        ),
        case_files=(TechLeadBoardCaseFile(
            issue_number=700, title="Pattern | case\nfile", comment_count=1,
            updated_at="", area="db|storage",
        ),),
        area_counts=(("db|storage", 1),), last_health_review="",
    )
    rendered = render_tech_lead_board_md(view)
    assert "Fix \\| the thing" in rendered
    assert "Pattern \\| case file" in rendered
    assert "db\\|storage" in rendered


# --- Publisher -------------------------------------------------------------


def _publisher(tmp_path: Path, authority=None) -> TechLeadBoardPublisher:
    return TechLeadBoardPublisher(
        board_path=tech_lead_board_path(tmp_path),
        authority=authority if authority is not None else InMemoryTechLeadAuthorityStore(),
        clock=lambda: datetime(2026, 7, 11, 5, 0, tzinfo=UTC),
    )


def test_board_path_lives_in_state_dir(tmp_path: Path) -> None:
    assert tech_lead_board_path(tmp_path) == state_dir(tmp_path) / TECH_LEAD_BOARD_FILENAME


def test_publish_retains_case_files_and_writes_board(tmp_path: Path) -> None:
    facts = TechLeadFacts(
        open_case_files=(
            _summary(700, comments=3, updated_at="2026-07-11T12:00:00+00:00", area="db"),
        ),
        case_files_scanned=True,
    )
    publisher = _publisher(tmp_path)
    publisher.publish(facts, last_health_review_at=0.0)
    assert publisher.case_files() == facts.open_case_files
    board = tech_lead_board_path(tmp_path)
    assert board.exists()
    content = board.read_text()
    assert content.startswith("# Tech Lead Board")
    assert "#700" in content
    assert "Pattern case file: sig-700" in content


def test_publish_shows_gated_proposals_with_an_empty_op_ledger(tmp_path: Path) -> None:
    """#7014: an empty ledger must never print "Open proposals: None."

    The reported failure verbatim: ``tech_lead_proposal_ops`` held zero rows
    while twenty gate-labeled issues waited on the operator, so the board — the
    only surface that shows the approval backlog — said there was nothing to
    approve.
    """
    publisher = _publisher(tmp_path)  # InMemory authority: zero ledger rows

    publisher.publish(
        TechLeadFacts(
            gated_proposals=(
                _gated(6922, title="[P1-003] oldest gated proposal"),
                _gated(7008, title="[P2-001] newest gated proposal"),
            )
        ),
        last_health_review_at=0.0,
    )

    content = tech_lead_board_path(tmp_path).read_text()
    assert "## Open proposals\n\nNone." not in content
    assert "2 awaiting operator approval" in content
    assert "#6922" in content
    assert "[P1-003] oldest gated proposal" in content
    assert "#7008" in content


def test_publish_drops_a_proposal_the_operator_approved(tmp_path: Path) -> None:
    """The backlog is THIS tick's observation, never a retained projection.

    Retaining it would keep advertising an approval the operator already gave
    (the gate label is gone), which is the mirror image of the #7014 defect.
    """
    publisher = _publisher(tmp_path)
    publisher.publish(
        TechLeadFacts(gated_proposals=(_gated(6922),)), last_health_review_at=0.0
    )
    assert "#6922" in tech_lead_board_path(tmp_path).read_text()

    publisher.publish(TechLeadFacts(gated_proposals=()), last_health_review_at=0.0)

    content = tech_lead_board_path(tmp_path).read_text()
    assert "#6922" not in content
    assert "## Open proposals\n\nNone." in content


def test_publisher_reads_shipped_fixes_from_durable_authority(tmp_path: Path) -> None:
    authority = InMemoryTechLeadAuthorityStore()
    authority.record_shipped_fix(
        issue_number=600,
        title="Repair DB seam",
        pr_url="https://github.com/o/r/pull/700",
        area="db",
    )
    publisher = _publisher(tmp_path, authority)

    [fix] = publisher.shipped_fixes(10)

    assert (fix.issue_number, fix.area) == (600, "db")


def test_publish_throttles_unchanged_content(tmp_path: Path) -> None:
    """Identical facts render identically -> no second write (#6781)."""
    facts = TechLeadFacts(
        open_case_files=(_summary(700, comments=1),), case_files_scanned=True
    )
    publisher = _publisher(tmp_path)
    publisher.publish(facts, last_health_review_at=0.0)
    board = tech_lead_board_path(tmp_path)
    assert board.exists()
    board.unlink()  # remove the artifact

    publisher.publish(facts, last_health_review_at=0.0)

    assert not board.exists()  # never rewritten


def test_publish_rewrites_when_content_changes(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(
        TechLeadFacts(open_case_files=(_summary(700, comments=1),), case_files_scanned=True),
        last_health_review_at=0.0,
    )
    publisher.publish(
        TechLeadFacts(open_case_files=(_summary(700, comments=2),), case_files_scanned=True),
        last_health_review_at=0.0,
    )

    content = tech_lead_board_path(tmp_path).read_text()
    assert "| 2 |" in content  # the newer comment count landed


def test_publish_swallows_render_failure(tmp_path: Path) -> None:
    """A projection write must never fail the planning tick (#6781)."""
    def boom() -> datetime:
        raise RuntimeError("clock exploded")

    publisher = TechLeadBoardPublisher(
        board_path=tech_lead_board_path(tmp_path),
        authority=InMemoryTechLeadAuthorityStore(),
        clock=boom,
    )
    facts = TechLeadFacts(open_case_files=(_summary(700),), case_files_scanned=True)
    publisher.publish(facts, last_health_review_at=0.0)  # must not raise
    assert publisher.case_files() == facts.open_case_files
    assert not tech_lead_board_path(tmp_path).exists()


def test_publish_swallows_write_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    publisher = TechLeadBoardPublisher(
        board_path=blocker / "tech-lead-board.md",
        authority=InMemoryTechLeadAuthorityStore(),
        clock=lambda: datetime(2026, 7, 11, 5, 0, tzinfo=UTC),
    )

    publisher.publish(
        TechLeadFacts(open_case_files=(_summary(700),), case_files_scanned=True),
        last_health_review_at=0.0,
    )
    # No exception propagated.


# --- Retain-vs-clear across scanned / not-scanned ticks (#6781 R2) ---------


def test_publish_retains_prior_case_files_when_scan_did_not_run(tmp_path: Path) -> None:
    """A frugal tick (no anchor scan) must NOT wipe the durable projection.

    Regression for #6781 R2: a health-review-armed but not-due tick gathers
    facts with ``case_files_scanned=False`` and ``open_case_files=()``. That
    empty tuple means "not observed this tick", not "observed empty" — the
    publisher must retain the last scanned projection so the board snapshot
    keeps seeing accumulating case-file evidence between scans.
    """
    publisher = _publisher(tmp_path)
    scanned = TechLeadFacts(
        open_case_files=(
            _summary(700, comments=3, updated_at="2026-07-11T12:00:00+00:00", area="db"),
        ),
        case_files_scanned=True,
    )
    publisher.publish(scanned, last_health_review_at=0.0)
    assert publisher.case_files() == scanned.open_case_files

    # A subsequent no-scan tick: empty case files, scanned flag off.
    not_scanned = TechLeadFacts(open_case_files=(), case_files_scanned=False)
    publisher.publish(not_scanned, last_health_review_at=0.0)

    # The injected reader (what the board snapshot builder consumes) still
    # holds the prior case file...
    assert publisher.case_files() == scanned.open_case_files
    # ...and the rendered board still surfaces it rather than "None".
    content = tech_lead_board_path(tmp_path).read_text()
    assert "#700" in content
    assert "Pattern case file: sig-700" in content


def test_publish_clears_case_files_when_scan_observed_none(tmp_path: Path) -> None:
    """A real scan that observed no open case files DOES clear the projection.

    The counterpart to the retain-on-no-scan case: when the anchor scan runs
    (``case_files_scanned=True``) and finds nothing, the empty tuple is a
    genuine observation, so stale case files are removed from both the reader
    and the board.
    """
    publisher = _publisher(tmp_path)
    publisher.publish(
        TechLeadFacts(
            open_case_files=(_summary(700, comments=3, area="db"),),
            case_files_scanned=True,
        ),
        last_health_review_at=0.0,
    )
    assert publisher.case_files()  # sanity: something to clear

    # A real scan observing an empty ledger.
    publisher.publish(
        TechLeadFacts(open_case_files=(), case_files_scanned=True),
        last_health_review_at=0.0,
    )

    assert publisher.case_files() == ()
    content = tech_lead_board_path(tmp_path).read_text()
    assert "#700" not in content
    # The case-file section collapses to the empty marker.
    assert "## Open pattern case files\n\nNone." in content
