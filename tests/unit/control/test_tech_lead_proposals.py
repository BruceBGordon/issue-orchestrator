"""Tests for gated tech_lead proposal issues (#6778, amends ADR-0031 §2)."""

from unittest.mock import MagicMock, call

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    ActionResult,
    AddCommentAction,
    CreateTechLeadCaseFileIssueAction,
    CreateTechLeadProposalIssueAction,
    DiscardTerminalTechLeadProposalOpsAction,
    KillHungSessionAction,
    ResetRetryIssueAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.proposal_dedup_gate import (
    DuplicateTargetGrant,
    OpenIssueCorpus,
)
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_issue_creation import (
    apply_create_tech_lead_issue,
)
from issue_orchestrator.control.tech_lead_kill_session import (
    KillSessionRunOutcome,
    TechLeadKillSessionExecutor,
)
from issue_orchestrator.control.tech_lead_proposals import (
    apply_discard_terminal_tech_lead_proposal_ops,
    build_op_ledger,
    build_tech_lead_proposal_issue_action,
    finalize_tech_lead_op_execution,
    observe_gated_tech_lead_proposals,
    plan_approved_tech_lead_op_executions,
    reconcile_tech_lead_proposals,
)
from issue_orchestrator.control.tech_lead_reset_retry import (
    ResetRetryRunOutcome,
    TechLeadResetRetryExecutor,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_artifacts import ProposedTechLeadAction
from issue_orchestrator.domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    TECH_LEAD_OBSERVATION_LABEL,
    ApprovedTechLeadOp,
    StoredTechLeadOp,
    TechLeadCreationOrigin,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.tech_lead_findings import (
    PatternObservation,
    case_file_issue_marker,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore

EXPECTED = build_expected_for_mutation()


def _op(
    target: int = 13,
    *,
    op_type: str = "reset_retry",
    target_session_id: str = "",
    finding_ids: tuple[str, ...] = (),
) -> StoredTechLeadOp:
    return StoredTechLeadOp(
        op_type=op_type,
        target_issue_number=target,
        rationale="Worktree unrecoverable.",
        source_run_id="run-1",
        source_session_name="issue-99",
        source_action_id="A2",
        created_at="2026-07-11T00:00:00+00:00",
        target_session_id=target_session_id,
        target_terminal_id=(f"issue-{target}" if target_session_id else ""),
        target_session_type=("code" if target_session_id else ""),
        finding_ids=finding_ids,
    )


def _kill_op(target: int = 14, *, session_id: str = "RUN-14") -> StoredTechLeadOp:
    return _op(target, op_type="kill_hung_session", target_session_id=session_id)


def _proposed(
    act_type: str = "reset_retry", target: int = 13
) -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id="A2",
        action_type=act_type,
        target_number=target,
        body="Worktree unrecoverable.",
        finding_ids=("T1",),
    )


def _proposal_action(
    act_type: str = "reset_retry", target: int = 13
) -> CreateTechLeadProposalIssueAction:
    config = Config()
    config.tech_lead_review_agent = "tech-lead-agent"
    return build_tech_lead_proposal_issue_action(
        _proposed(act_type, target),
        config=config,
        anchor_issue_number=99,
        source_run_id="run-1",
        source_session_name="issue-99",
        expected=EXPECTED,
        now_iso="2026-07-11T00:00:00+00:00",
    )


def _issue(number: int, labels: list[str], title: str = "t") -> Issue:
    return Issue(number=number, title=title, labels=labels, repo="owner/repo")


def _host(created_number: int = 500) -> MagicMock:
    host = MagicMock()
    host.create_issue.return_value = {"number": created_number}
    # No orphaned remote case file unless a test says otherwise (#6957 F10).
    host.find_issue_by_marker.return_value = None
    host.list_milestones.return_value = []
    # The gate must be provisioned or the applier refuses to create (#6779 R3).
    host.list_labels.return_value = [
        {"name": PROPOSED_TECH_LEAD_LABEL},
        {"name": "tech-lead-agent"},
        {"name": TECH_LEAD_OBSERVATION_LABEL},
    ]
    return host


# --- Composition ----------------------------------------------------------


def test_proposal_action_carries_gate_label_and_scan_labels() -> None:
    action = _proposal_action()

    assert PROPOSED_TECH_LEAD_LABEL in action.labels
    # The tech lead agent label keeps the proposal inside the ONE anchor scan.
    assert "tech-lead-agent" in action.labels
    # R6: the proposal's findings are persisted onto the stored op.
    assert action.op == _op(finding_ids=("T1",))
    assert action.anchor_issue_number == 99


def test_proposal_action_requires_gate_label() -> None:
    with pytest.raises(ValueError, match="gate label"):
        CreateTechLeadProposalIssueAction(
            title="t",
            body="b",
            labels=("x",),
            op=_op(),
            origin=TechLeadCreationOrigin.derived_from_anchor(99),
            expected=build_expected_for_mutation(),
        )


def test_proposal_titles_never_match_anchor_heuristics() -> None:
    for act_type in ("reset_retry", "kill_hung_session"):
        title = _proposal_action(act_type).title
        assert "Batch Review" not in title
        assert "Tech Lead Review" not in title


def test_op_ledger_projects_rows_by_op_and_target() -> None:
    ledger = build_op_ledger(
        [(500, _op(13)), (501, _op(14, op_type="kill_hung_session"))]
    )

    assert ledger == {
        ("reset_retry", 13): 500,
        ("kill_hung_session", 14): 501,
    }


# --- Classification (the ONE anchor scan) ---------------------------------


def test_split_classifies_open_approved_and_anchors() -> None:
    gated = _issue(500, ["tech-lead-agent", PROPOSED_TECH_LEAD_LABEL])
    approved = _issue(501, ["tech-lead-agent"])
    anchor = _issue(
        7, ["tech-lead-agent"], title="Tech Lead Batch Review: 3 PRs pending"
    )
    ops = {500: _op(13), 501: _kill_op(14)}

    reconciled = reconcile_tech_lead_proposals([gated, approved, anchor], ops=ops)

    # Open proposal: excluded everywhere. Approved: op returned for planning.
    assert [i.number for i in reconciled.anchor_candidate_issues] == [7]
    assert reconciled.approved == (
        ApprovedTechLeadOp(proposal_issue_number=501, op=ops[501]),
    )
    # Every ledger row is accounted for by a live open issue: no candidates.
    assert reconciled.absent_op_issue_numbers == ()


def test_split_still_gated_yields_nothing_to_execute() -> None:
    gated = _issue(500, ["tech-lead-agent", PROPOSED_TECH_LEAD_LABEL])

    reconciled = reconcile_tech_lead_proposals([gated], ops={500: _op(13)})

    assert reconciled.anchor_candidate_issues == []
    assert reconciled.approved == ()
    assert reconciled.absent_op_issue_numbers == ()


def test_reconcile_treats_canonical_cased_gate_as_still_gated() -> None:
    """R15 (act-level gate): GitHub folds label names, so a repo whose canonical
    spelling is ``Proposed-Tech-Lead`` still gates. A case-variant gate must NOT be
    mistaken for operator approval — the op stays inert, never `approved=[500]`."""
    canonical = _issue(500, ["agent:tech-lead", "Proposed-Tech-Lead"])

    reconciled = reconcile_tech_lead_proposals([canonical], ops={500: _op(13)})

    # Case-insensitive: an open proposal, not an approved op and not an anchor.
    assert reconciled.approved == ()
    assert reconciled.anchor_candidate_issues == []
    # Still present in the open scan, so never a terminal-cleanup candidate.
    assert reconciled.absent_op_issue_numbers == ()


def test_reconcile_gate_case_variants_all_block_approval() -> None:
    """R15: every case spelling of the gate keeps the op inert (no divergence)."""
    for spelling in ("proposed-tech-lead", "Proposed-Tech-Lead", "PROPOSED-TECH-LEAD"):
        issue = _issue(500, ["agent:tech-lead", spelling])
        reconciled = reconcile_tech_lead_proposals([issue], ops={500: _op(13)})
        assert reconciled.approved == (), spelling


def test_split_without_ops_excludes_gate_labeled_issues() -> None:
    """A gate-labeled issue with no op row is inert — excluded from anchors,
    never executed."""
    gated = _issue(500, ["tech-lead-agent", PROPOSED_TECH_LEAD_LABEL])
    plain = _issue(7, ["tech-lead-agent"])

    reconciled = reconcile_tech_lead_proposals([gated, plain], ops={})

    assert [i.number for i in reconciled.anchor_candidate_issues] == [7]
    assert reconciled.approved == ()


def test_reconcile_flags_ledger_row_absent_from_scan_as_candidate_only() -> None:
    """R7: a ledger op whose proposal issue is not in the exhaustive open scan
    (manual close, a finalize that crashed before discard_op, OR a truncated
    scan) is surfaced only as a cleanup CANDIDATE — reconciliation is read-only
    and never proves terminality on absence alone."""
    # #500 is still open+gated; #501's proposal issue is gone from the scan.
    gated = _issue(500, ["tech-lead-agent", PROPOSED_TECH_LEAD_LABEL])
    ops = {500: _op(13), 501: _kill_op(14)}

    reconciled = reconcile_tech_lead_proposals([gated], ops=ops)

    assert reconciled.approved == ()
    assert reconciled.anchor_candidate_issues == []
    assert reconciled.absent_op_issue_numbers == (501,)


# --- Approval backlog: label truth (#7014) --------------------------------


def test_backlog_includes_a_gated_issue_with_no_ledger_row() -> None:
    """The #7014 defect: only act-level proposals leave a ledger row.

    Promoted findings and proposed follow-up issues carry the same gate with no
    ``tech_lead_proposal_ops`` row, so a backlog derived from the ledger reports
    nothing pending while they wait on the operator.
    """
    promoted = _issue(6922, ["agent:backend", PROPOSED_TECH_LEAD_LABEL], title="Fix seam")

    [pending] = observe_gated_tech_lead_proposals([promoted])

    assert (pending.issue_number, pending.title) == (6922, "Fix seam")


def test_backlog_excludes_issues_without_the_gate() -> None:
    ungated = _issue(7, ["agent:backend"])
    approved = _issue(8, ["agent:tech-lead"])  # gate removed = approved

    assert observe_gated_tech_lead_proposals([ungated, approved]) == ()


def test_backlog_folds_gate_label_case() -> None:
    """R15: GitHub folds label names, so a canonical spelling still gates."""
    canonical = Issue(
        number=500, title="t", labels=["agent:tech-lead", "Proposed-Tech-Lead"]
    )

    assert [p.issue_number for p in observe_gated_tech_lead_proposals([canonical])] == [500]


def test_backlog_excludes_a_closed_gated_issue() -> None:
    """A closed proposal was rejected or already handled: nobody is waiting."""
    closed = Issue(
        number=500, title="t", labels=[PROPOSED_TECH_LEAD_LABEL], state="closed"
    )

    assert observe_gated_tech_lead_proposals([closed]) == ()


def test_backlog_unions_observed_sets_and_orders_by_issue_number() -> None:
    """Callers pass whatever open-issue sets the tick already holds."""
    board = [_issue(7008, [PROPOSED_TECH_LEAD_LABEL]), _issue(6922, [PROPOSED_TECH_LEAD_LABEL])]
    scan = [_issue(500, [PROPOSED_TECH_LEAD_LABEL]), _issue(6922, [PROPOSED_TECH_LEAD_LABEL])]

    backlog = observe_gated_tech_lead_proposals(board, scan)

    # Deduplicated by issue number, ordered by it, deterministic.
    assert [p.issue_number for p in backlog] == [500, 6922, 7008]


def test_backlog_carries_the_filing_time_the_board_ages_it_by() -> None:
    filed = Issue(
        number=6922,
        title="t",
        labels=[PROPOSED_TECH_LEAD_LABEL],
        created_at="2026-07-28T00:00:00+00:00",
    )

    [pending] = observe_gated_tech_lead_proposals([filed])

    assert pending.created_at == "2026-07-28T00:00:00+00:00"


# --- Confirm-and-discard owner (#6779 R7/R10) -----------------------------


class _FakeTracker:
    """Targeted-read stand-in: maps issue number -> 'open'/'closed'/None."""

    def __init__(self, states: dict[int, str | None]) -> None:
        self._states = states
        self.reads: list[int] = []

    def get_issue_state(self, issue_number: int, repo=None) -> str | None:
        self.reads.append(issue_number)
        return self._states.get(issue_number)


def test_discard_owner_preserves_op_when_confirm_read_shows_open() -> None:
    """R7 (data-loss safety): a candidate absent from the scan but confirmed
    STILL OPEN is a pagination gap — its live op must be preserved."""
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=501, op=_kill_op(14))
    tracker = _FakeTracker({501: "open"})
    action = DiscardTerminalTechLeadProposalOpsAction(candidate_issue_numbers=(501,))

    result = apply_discard_terminal_tech_lead_proposal_ops(
        action, tracker=tracker, authority=ops
    )

    assert result.success
    assert tracker.reads == [501]  # a FRESH targeted read confirmed it
    assert ops.load_op(issue_number=501) is not None  # PRESERVED, not deleted
    assert result.details["discarded_op_count"] == 0
    assert result.details["preserved_op_count"] == 1


def test_discard_owner_discards_op_when_confirmed_closed() -> None:
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=501, op=_kill_op(14))
    tracker = _FakeTracker({501: "closed"})
    action = DiscardTerminalTechLeadProposalOpsAction(candidate_issue_numbers=(501,))

    result = apply_discard_terminal_tech_lead_proposal_ops(
        action, tracker=tracker, authority=ops
    )

    assert result.success
    assert ops.load_op(issue_number=501) is None  # confirmed terminal -> discarded
    assert result.details["discarded_op_count"] == 1


def test_discard_owner_discards_op_when_issue_deleted() -> None:
    """A deleted proposal issue reads as None (404) and is genuinely terminal."""
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=501, op=_kill_op(14))
    tracker = _FakeTracker({501: None})
    action = DiscardTerminalTechLeadProposalOpsAction(candidate_issue_numbers=(501,))

    result = apply_discard_terminal_tech_lead_proposal_ops(
        action, tracker=tracker, authority=ops
    )

    assert result.success
    assert ops.load_op(issue_number=501) is None


def test_discard_owner_never_deletes_live_op_on_truncated_scan() -> None:
    """R7: a later-page scan failure drops a still-open proposal from the
    exhaustive scan, so it arrives as a cleanup candidate alongside a genuinely
    closed one. The confirm read discards only the closed op and preserves the
    live one — a partial scan can never delete a live op."""
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=600, op=_op(20))  # genuinely closed
    ops.record_op(issue_number=601, op=_kill_op(21))  # live, dropped by truncation
    tracker = _FakeTracker({600: "closed", 601: "open"})
    action = DiscardTerminalTechLeadProposalOpsAction(
        candidate_issue_numbers=(600, 601)
    )

    result = apply_discard_terminal_tech_lead_proposal_ops(
        action, tracker=tracker, authority=ops
    )

    assert result.success
    assert ops.load_op(issue_number=600) is None  # confirmed closed -> discarded
    assert ops.load_op(issue_number=601) is not None  # live -> preserved
    assert result.details["discarded_op_count"] == 1
    assert result.details["preserved_op_count"] == 1


def test_discard_owner_fails_loudly_without_tracker_or_store() -> None:
    action = DiscardTerminalTechLeadProposalOpsAction(candidate_issue_numbers=(1,))

    result = apply_discard_terminal_tech_lead_proposal_ops(
        action, tracker=None, authority=InMemoryTechLeadAuthorityStore()
    )

    assert not result.success


# --- Approval planning ----------------------------------------------------


def test_approved_reset_op_plans_reset_action_with_proposal_linkage() -> None:
    [action] = plan_approved_tech_lead_op_executions(
        (
            ApprovedTechLeadOp(
                proposal_issue_number=500, op=_op(13, finding_ids=("T1", "T2"))
            ),
        )
    )

    assert isinstance(action, ResetRetryIssueAction)
    assert action.issue_number == 13
    assert action.rationale == "Worktree unrecoverable."
    assert action.proposal_id == "A2"
    assert action.proposal_issue_number == 500
    assert action.anchor_issue_number == 500  # the surface the operator approved on
    # R6: the approved op's findings ride the action into TECH_LEAD_ACTION_EXECUTED.
    assert action.finding_ids == ("T1", "T2")
    assert action.expected is not None


def test_approved_kill_op_plans_kill_action() -> None:
    op = _kill_op(14, session_id="RUN-14")
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=501, op=op),)
    )

    assert isinstance(action, KillHungSessionAction)
    assert action.issue_number == 14
    assert action.proposal_issue_number == 501
    # R1: the approved generation binding rides the action to the executor.
    assert action.target_session_id == "RUN-14"


# --- Creation boundary (applier owner) ------------------------------------


def test_apply_proposal_creation_records_op_and_links_anchor() -> None:
    host = _host(500)
    ops = InMemoryTechLeadAuthorityStore()
    action = _proposal_action()

    result = apply_create_tech_lead_issue(
        action,
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert result.success
    host.create_issue.assert_called_once()
    assert ops.load_op(issue_number=500) == action.op
    # The anchor digest entry is the issue link comment (replaces shadow).
    (anchor_number, comment), _ = host.add_comment.call_args
    assert anchor_number == 99
    assert "#500" in comment
    assert PROPOSED_TECH_LEAD_LABEL in comment


def test_apply_proposal_creation_fails_when_gate_not_provisioned() -> None:
    """R3: a fresh repo without the gate label must NOT get an orphan issue."""
    host = _host(500)
    host.list_labels.return_value = [{"name": "some-other-label"}]  # no gate
    ops = InMemoryTechLeadAuthorityStore()

    result = apply_create_tech_lead_issue(
        _proposal_action(),
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert not result.success
    assert PROPOSED_TECH_LEAD_LABEL in (result.error or "")
    host.create_issue.assert_not_called()  # no orphan
    assert ops.list_ops() == ()


def test_apply_proposal_creation_without_store_fails_loudly() -> None:
    host = _host()
    result = apply_create_tech_lead_issue(
        _proposal_action(),
        repository_host=host,
        events=MagicMock(),
        ops=None,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert not result.success
    assert "TechLeadAuthorityStore" in (result.error or "")


def _case_file_action(
    signature: str = "sig-x",
    *,
    area: str | None = None,
    additional_comments: tuple[str, ...] = (),
    fix_class: str = "",
    observation_suffix: str = "",
) -> CreateTechLeadCaseFileIssueAction:
    """A case-file creation action.

    ``observation_suffix`` makes a DIFFERENT decision's action for the same
    signature: same title and marker (both derived from the signature), but
    distinct observation identities — which is exactly the shape a later
    observation recovering an interrupted creation takes.
    """
    labels = ["tech-lead-agent", TECH_LEAD_OBSERVATION_LABEL]
    if area is not None:
        labels.append(f"area:{area}")
    prefix = f"{signature}:{observation_suffix}" if observation_suffix else signature
    observations = (
        PatternObservation(
            observation_id=f"{prefix}:obs-1", comment="first observation"
        ),
        *(
            PatternObservation(observation_id=f"{prefix}:obs-{index}", comment=comment)
            for index, comment in enumerate(additional_comments, start=2)
        ),
    )
    marker = case_file_issue_marker(signature)
    return CreateTechLeadCaseFileIssueAction(
        title=f"Pattern case file: {signature}",
        body=f"documentation only\n\n{marker}",
        labels=tuple(labels),
        pr_count=0,
        pattern_signature=signature,
        origin=TechLeadCreationOrigin.derived_from_anchor(42),
        expected=build_expected_for_mutation(),
        area=area,
        fix_class=fix_class,
        diagnosis="Pool exhaustion comes from a leaked connection.",
        idempotency_marker=marker,
        observations=observations,
    )


def test_apply_case_file_creation_records_pattern_ledger() -> None:
    """The applier's create-issue owner records the (signature -> issue) ledger
    row create-once when it creates a case file (#6781)."""
    host = _host(600)
    ops = InMemoryTechLeadAuthorityStore()

    result = apply_create_tech_lead_issue(
        _case_file_action("db-timeout"),
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert result.success
    host.create_issue.assert_called_once()
    assert ops.lookup_pattern(signature="db-timeout") == 600
    assert ops.list_pattern_evidence()[0].diagnosis == (
        "Pool exhaustion comes from a leaked connection."
    )
    # Case files do not record ops and post no anchor-link comment.
    assert ops.list_ops() == ()
    host.add_comment.assert_not_called()


def test_apply_case_file_missing_observation_label_creates_no_orphan() -> None:
    """A failed blocking-label write must stop before issue creation."""
    host = _host(600)
    host.list_labels.return_value = [{"name": "tech-lead-agent"}]
    host.create_label.side_effect = RuntimeError("label permission denied")
    ops = InMemoryTechLeadAuthorityStore()

    result = apply_create_tech_lead_issue(
        _case_file_action("db-timeout"),
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert not result.success
    assert TECH_LEAD_OBSERVATION_LABEL in (result.error or "")
    host.create_issue.assert_not_called()
    assert ops.lookup_pattern(signature="db-timeout") is None


def test_apply_case_file_missing_area_label_creates_no_orphan() -> None:
    """A failed dynamic area-label write must stop before issue creation."""
    host = _host(600)
    host.create_label.side_effect = RuntimeError("label permission denied")
    ops = InMemoryTechLeadAuthorityStore()

    result = apply_create_tech_lead_issue(
        _case_file_action("db-timeout", area="database"),
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert not result.success
    assert "area:database" in (result.error or "")
    host.create_issue.assert_not_called()
    assert ops.lookup_pattern(signature="db-timeout") is None


def test_apply_case_file_provisions_labels_before_blocking_area_tagged_issue() -> None:
    """Missing static/dynamic labels are guaranteed before issue creation."""
    host = _host(600)
    host.list_labels.return_value = [{"name": "tech-lead-agent"}]
    ops = InMemoryTechLeadAuthorityStore()
    action = _case_file_action("db-timeout", area="database")

    result = apply_create_tech_lead_issue(
        action,
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert result.success
    assert host.method_calls == [
        # Recovery first: "has a previous attempt already created this case
        # file?" must be answered before anything is provisioned or created,
        # or a crash-retry files a second one (#6957 R2 F10).
        # Best-effort here: with no creation intent, a miss just means "carry
        # on and create", so this does NOT pay for the exhaustive scan (F13).
        call.find_issue_by_marker(
            title=action.title,
            marker=action.idempotency_marker,
            authoritative=False,
        ),
        call.list_labels(),
        call.create_label(
            TECH_LEAD_OBSERVATION_LABEL,
            color="B60205",
            description="Pattern case file (tech_lead observation ledger)",
        ),
        call.create_label(
            "area:database",
            color="1D76DB",
            description="Tech Lead pattern area",
        ),
        call.create_issue(
            title=action.title,
            body=action.body,
            labels=["tech-lead-agent", TECH_LEAD_OBSERVATION_LABEL, "area:database"],
            milestone=None,
        ),
    ]
    assert ops.lookup_pattern(signature="db-timeout") == 600


def test_apply_case_file_creation_without_store_fails_loudly() -> None:
    host = _host()
    result = apply_create_tech_lead_issue(
        _case_file_action(),
        repository_host=host,
        events=MagicMock(),
        ops=None,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert not result.success
    assert "TechLeadAuthorityStore" in (result.error or "")


def test_apply_case_file_creation_posts_same_decision_observations() -> None:
    host = _host(600)
    ops = InMemoryTechLeadAuthorityStore()
    result = apply_create_tech_lead_issue(
        _case_file_action("db-timeout", additional_comments=("second observation",)),
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )
    assert result.success
    host.add_comment.assert_called_once_with(600, "second observation")


def test_apply_case_file_rechecks_ledger_and_comments_inflight_duplicate() -> None:
    host = _host(601)
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_pattern(
        signature="db-timeout", issue_number=600, observation_id="prior:obs"
    )
    result = apply_create_tech_lead_issue(
        _case_file_action("db-timeout", additional_comments=("follow-up",)),
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )
    assert result.success
    assert result.details["deduplicated"] is True
    host.create_issue.assert_not_called()
    assert host.add_comment.call_args_list == [
        call(600, "first observation"),
        call(600, "follow-up"),
    ]
    # Two distinct observations landed on the pre-existing case file.
    [evidence] = ops.list_pattern_evidence()
    assert evidence.observation_count == 3


# --- Replay after each partial write (#6957 review F1) ---------------------
#
# The lane's evidence count gates promotion, so every crash window between a
# GitHub write and the durable count has to be replay-safe: a retry may repeat
# a comment, but it must never count one observation twice.


def _apply_case_file(action, *, ops, host):
    return apply_create_tech_lead_issue(
        action,
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )


def test_replay_after_ledger_creation_does_not_recount_its_observations() -> None:
    """Crash right after ``record_pattern``: the retry reconciles onto the row.

    Before the fix this counted the body observation AND every additional
    comment all over again — a two-observation action reached count 4 after one
    retry and could cross ``min_evidence`` without distinct evidence.
    """
    action = _case_file_action("db-timeout", additional_comments=("second",))
    ops = InMemoryTechLeadAuthorityStore()

    first = _apply_case_file(action, ops=ops, host=_host(600))
    assert first.success
    [after_first] = ops.list_pattern_evidence()
    assert after_first.observation_count == 2

    # The process dies before the action is marked applied; the next tick
    # replays the SAME action.
    replay_host = _host(600)
    replay = _apply_case_file(action, ops=ops, host=replay_host)

    assert replay.success
    assert replay.details["deduplicated"] is True
    [after_replay] = ops.list_pattern_evidence()
    assert after_replay.observation_count == 2
    # Nothing is re-posted either: both identities are already recorded.
    replay_host.add_comment.assert_not_called()


def test_replay_after_one_additional_comment_counts_only_what_is_missing() -> None:
    """Crash between two additional comments: only the unposted one is added."""
    action = _case_file_action("db-timeout", additional_comments=("second", "third"))
    ops = InMemoryTechLeadAuthorityStore()
    host = _host(600)
    # Fail while posting the SECOND additional comment (the third observation
    # of the decision), after the first one and its count already landed.
    host.add_comment.side_effect = [None, RuntimeError("network died")]

    failed = _apply_case_file(action, ops=ops, host=host)

    assert not failed.success
    [partial] = ops.list_pattern_evidence()
    assert partial.observation_count == 2  # body + the one comment that landed

    replay_host = _host(600)
    replay = _apply_case_file(action, ops=ops, host=replay_host)

    assert replay.success
    [final] = ops.list_pattern_evidence()
    assert final.observation_count == 3
    # Exactly the observation that never landed is re-posted.
    assert replay_host.add_comment.call_args_list == [call(600, "third")]


def test_replay_after_a_lost_comment_repeats_it_rather_than_losing_evidence() -> None:
    """The comment is posted BEFORE its count, so a crash between them repeats
    the comment (cosmetic) instead of counting evidence nobody can read."""
    action = _case_file_action("db-timeout", additional_comments=("second",))
    ops = InMemoryTechLeadAuthorityStore()
    host = _host(600)
    host.add_comment.side_effect = RuntimeError("network died")

    assert not _apply_case_file(action, ops=ops, host=host).success
    [partial] = ops.list_pattern_evidence()
    assert partial.observation_count == 1

    replay_host = _host(600)
    assert _apply_case_file(action, ops=ops, host=replay_host).success

    [final] = ops.list_pattern_evidence()
    assert final.observation_count == 2
    assert replay_host.add_comment.call_args_list == [call(600, "second")]


def test_replaying_a_repeat_observation_append_never_double_counts() -> None:
    from issue_orchestrator.control.actions import AppendPatternObservationAction
    from issue_orchestrator.control.tech_lead_case_files import (
        apply_append_pattern_observation,
    )

    ops = InMemoryTechLeadAuthorityStore()
    ops.record_pattern(
        signature="db-timeout", issue_number=600, observation_id="r1:s:A1"
    )
    action = AppendPatternObservationAction(
        issue_number=600,
        pattern_signature="db-timeout",
        observation=PatternObservation(
            observation_id="r2:s:A1", comment="observed again"
        ),
    )
    host = MagicMock()

    assert apply_append_pattern_observation(
        action, repository_host=host, authority=ops
    ).success
    replay = apply_append_pattern_observation(
        action, repository_host=host, authority=ops
    )

    assert replay.success
    assert replay.details["deduplicated"] is True
    [evidence] = ops.list_pattern_evidence()
    assert evidence.observation_count == 2
    host.add_comment.assert_called_once_with(600, "observed again")


# --- Case-file creation crash window (#6957 review F10) --------------------
#
# The GitHub issue is created before its ledger row lands. A crash in between
# leaves an issue nothing knows about, and the retry is NOT guaranteed to be the
# same command: a case-file finalization failure is an ordinary ActionResult
# failure, so the next observation of that signature can be the one that
# recovers it. Everything below turns on that distinction.


def _crash_the_ledger_write(ops: InMemoryTechLeadAuthorityStore):
    """Make the next record_pattern die, as a process kill would."""
    original = ops.record_pattern

    def explode(**kwargs):
        ops.record_pattern = original  # type: ignore[method-assign]
        raise RuntimeError("sqlite went away")

    ops.record_pattern = explode  # type: ignore[method-assign]


def test_crash_after_remote_create_recovers_instead_of_filing_a_second() -> None:
    action = _case_file_action("db-timeout", additional_comments=("second",))
    ops = InMemoryTechLeadAuthorityStore()

    first_host = _host(600)
    _crash_the_ledger_write(ops)
    failed = _apply_case_file(action, ops=ops, host=first_host)

    assert not failed.success
    first_host.create_issue.assert_called_once()
    assert ops.lookup_pattern(signature="db-timeout") is None
    # The durable creation intent survives the crash - that is what makes the
    # recovery attributable rather than guessed.
    pending = ops.load_pending_case_file(signature="db-timeout")
    assert pending is not None
    assert pending.body_observation_id == "db-timeout:obs-1"

    retry_host = _host(999)  # a NEW number, to prove nothing was created
    retry_host.find_issue_by_marker.return_value = 600

    retry = _apply_case_file(action, ops=ops, host=retry_host)

    assert retry.success
    assert retry.details["deduplicated"] is True
    assert retry.details["recovered"] is True
    retry_host.create_issue.assert_not_called()
    assert ops.lookup_pattern(signature="db-timeout") == 600
    # The intent is retired once the ledger row lands.
    assert ops.load_pending_case_file(signature="db-timeout") is None


def test_recovered_case_file_records_the_right_count_and_classification() -> None:
    action = _case_file_action("db-timeout", additional_comments=("second",))
    ops = InMemoryTechLeadAuthorityStore()
    host = _host(600)
    _crash_the_ledger_write(ops)
    assert not _apply_case_file(action, ops=ops, host=host).success

    retry_host = _host(999)
    retry_host.find_issue_by_marker.return_value = 600
    assert _apply_case_file(action, ops=ops, host=retry_host).success

    [evidence] = ops.list_pattern_evidence()
    assert evidence.case_file_issue_number == 600
    # Body observation + the one additional observation, counted once each.
    assert evidence.observation_count == 2
    assert evidence.diagnosis == "Pool exhaustion comes from a leaked connection."
    # The body already carries observation 1 (the dead process wrote it), so
    # only the additional observation is posted as a comment.
    assert retry_host.add_comment.call_args_list == [call(600, "second")]


def test_a_later_action_recovering_keeps_the_original_body_authoritative() -> None:
    """#6957 round-3 review F10: the recovering action is NOT the creator.

    Action A (fix:human, observation A) creates #600 and its ledger write dies.
    A later action B (fix:code, observation B) is the one that recovers it.
    Attributing B's metadata to the body would silently reclassify a human-gated
    finding as promotable code work, lose observation A's identity, and claim
    evidence is visible when it is not.
    """
    first = _case_file_action("db-timeout", fix_class="human", area="database")
    ops = InMemoryTechLeadAuthorityStore()
    _crash_the_ledger_write(ops)
    assert not _apply_case_file(first, ops=ops, host=_host(600)).success

    # A DIFFERENT, later observation of the same signature, agreeing on
    # classification so it is a legitimate append rather than a conflict.
    later = _case_file_action(
        "db-timeout",
        fix_class="human",
        area="database",
        observation_suffix="later",
        additional_comments=(),
    )
    retry_host = _host(999)
    retry_host.find_issue_by_marker.return_value = 600

    recovered = _apply_case_file(later, ops=ops, host=retry_host)

    assert recovered.success and recovered.details["recovered"] is True
    retry_host.create_issue.assert_not_called()
    [evidence] = ops.list_pattern_evidence()
    assert evidence.case_file_issue_number == 600
    # The ORIGINAL action's observation is what the body records...
    assert ops.has_pattern_observation(
        signature="db-timeout", observation_id="db-timeout:obs-1"
    )
    # ...and the later observation is visibly appended AND counted, not
    # swallowed as "already recorded".
    assert ops.has_pattern_observation(
        signature="db-timeout", observation_id="db-timeout:later:obs-1"
    )
    assert evidence.observation_count == 2
    assert retry_host.add_comment.call_args_list == [call(600, "first observation")]


def test_a_recovering_action_cannot_reclassify_the_recovered_body() -> None:
    """The same scenario, with the conflict the immutability rule exists for.

    Nothing may be published before the classification is reconciled, so the
    later fix:code observation is rejected with no comment and no count.
    """
    first = _case_file_action("db-timeout", fix_class="human")
    ops = InMemoryTechLeadAuthorityStore()
    _crash_the_ledger_write(ops)
    assert not _apply_case_file(first, ops=ops, host=_host(600)).success

    later = _case_file_action(
        "db-timeout", fix_class="code", observation_suffix="later"
    )
    retry_host = _host(999)
    retry_host.find_issue_by_marker.return_value = 600

    result = _apply_case_file(later, ops=ops, host=retry_host)

    assert not result.success
    retry_host.create_issue.assert_not_called()
    retry_host.add_comment.assert_not_called()
    # The recovered row keeps the ORIGINAL classification, so the signature
    # stays excluded from promotion.
    [evidence] = ops.list_pattern_evidence()
    assert evidence.fix_class == "human"
    assert evidence.observation_count == 1
    assert not ops.has_pattern_observation(
        signature="db-timeout", observation_id="db-timeout:later:obs-1"
    )


def test_a_stale_intent_with_no_remote_issue_creates_fresh() -> None:
    """The create never happened, so the intent is discarded, not recovered."""
    action = _case_file_action("db-timeout")
    ops = InMemoryTechLeadAuthorityStore()
    failing_host = _host(600)
    failing_host.create_issue.side_effect = RuntimeError("GitHub rejected it")
    assert not _apply_case_file(action, ops=ops, host=failing_host).success
    assert ops.load_pending_case_file(signature="db-timeout") is not None

    retry_host = _host(700)  # no issue carries the marker
    retry = _apply_case_file(action, ops=ops, host=retry_host)

    assert retry.success
    retry_host.create_issue.assert_called_once()
    assert ops.lookup_pattern(signature="db-timeout") == 700
    assert ops.load_pending_case_file(signature="db-timeout") is None


def test_retiring_an_intent_demands_a_lookup_whose_no_is_proof() -> None:
    """#6957 round-5 review F13, case-file lane.

    The negative answer above is load-bearing: it RETIRES a durable creation
    intent and files a fresh case file. A bounded, title-scoped search reports
    "absent" for an issue that merely aged out of the recent window or was
    retitled, so answering this question with one creates a second case file for
    a signature that already has one. Only the authoritative lookup may be asked.
    """
    action = _case_file_action("db-timeout")
    ops = InMemoryTechLeadAuthorityStore()
    failing_host = _host(600)
    failing_host.create_issue.side_effect = RuntimeError("GitHub rejected it")
    assert not _apply_case_file(action, ops=ops, host=failing_host).success

    retry_host = _host(700)
    _apply_case_file(action, ops=ops, host=retry_host)

    assert retry_host.find_issue_by_marker.call_args.kwargs["authoritative"] is True


def test_an_orphan_with_no_creation_intent_stops_instead_of_guessing() -> None:
    """Durable-state loss, not a crash window: neither answer is safe."""
    host = _host(999)
    host.find_issue_by_marker.return_value = 600

    result = _apply_case_file(
        _case_file_action("db-timeout"),
        ops=InMemoryTechLeadAuthorityStore(),
        host=host,
    )

    assert not result.success
    assert "neither a ledger row nor a record of creating it" in (result.error or "")
    host.create_issue.assert_not_called()


def test_recovery_is_attempted_only_when_the_ledger_has_no_row() -> None:
    """A committed signature costs no GitHub call (API discipline)."""
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_pattern(
        signature="db-timeout", issue_number=600, observation_id="prior:obs"
    )
    host = _host(601)

    assert _apply_case_file(_case_file_action("db-timeout"), ops=ops, host=host).success

    host.find_issue_by_marker.assert_not_called()


def test_a_failed_recovery_lookup_never_files_a_duplicate() -> None:
    """ "Unknown" must not be mistaken for "no case file exists"."""
    host = _host(601)
    host.find_issue_by_marker.side_effect = RuntimeError("GitHub unreachable")

    result = _apply_case_file(
        _case_file_action("db-timeout"),
        ops=InMemoryTechLeadAuthorityStore(),
        host=host,
    )

    assert not result.success
    host.create_issue.assert_not_called()


def test_apply_plain_tech_lead_issue_records_no_op() -> None:
    from issue_orchestrator.control.actions import CreateTechLeadIssueAction

    host = _host(500)
    ops = InMemoryTechLeadAuthorityStore()

    result = apply_create_tech_lead_issue(
        CreateTechLeadIssueAction(
            title="t",
            body="b",
            labels=("x",),
            pr_count=2,
            origin=TechLeadCreationOrigin.authors_anchor(),
        ),
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    assert result.success
    assert ops.list_ops() == ()
    host.add_comment.assert_not_called()


def test_body_tamper_has_zero_effect_on_execution() -> None:
    """Tamper regression (#6778): execution consumes only the stored op.

    The proposal issue's body is edited after creation; the approved-op
    execution still resets the ORIGINAL target with the ORIGINAL rationale —
    nothing ever re-parses the body.
    """
    host = _host(500)
    ops = InMemoryTechLeadAuthorityStore()
    action = _proposal_action(target=13)
    apply_create_tech_lead_issue(
        action,
        repository_host=host,
        events=MagicMock(),
        ops=ops,
        add_comment=host.add_comment,
        emit_labels_changed=lambda *_: None,
    )

    # Attacker edits the issue body to point at another issue. The scan sees
    # the edited issue (gate removed = approved); the stored op is unchanged.
    tampered_issue = _issue(
        500,
        ["tech-lead-agent"],
        title="Tech Lead proposal: reset & retry issue #6666 from scratch",
    )
    approved_ops = reconcile_tech_lead_proposals(
        [tampered_issue], ops=dict(ops.list_ops())
    ).approved
    [planned] = plan_approved_tech_lead_op_executions(approved_ops)

    assert isinstance(planned, ResetRetryIssueAction)
    assert planned.issue_number == 13  # the recorded target, not #6666
    assert planned.rationale == "Worktree unrecoverable."


# --- Terminal handling (finalization) --------------------------------------


def _reset_action(proposal_issue: int = 500) -> ResetRetryIssueAction:
    return ResetRetryIssueAction(
        issue_number=13,
        rationale="r",
        proposal_id="A2",
        anchor_issue_number=proposal_issue,
        proposal_issue_number=proposal_issue,
        expected=EXPECTED,
    )


def test_finalize_success_comments_closes_and_discards() -> None:
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    action = _reset_action()
    result = ActionResult.ok(action, issue_number=13)

    out = finalize_tech_lead_op_execution(result, action, repository_host=host, ops=ops)

    assert out is result
    (number, comment), _ = host.add_comment.call_args
    assert number == 500 and "executed" in comment
    host.update_issue_state.assert_called_once_with(500, "closed")
    assert ops.load_op(issue_number=500) is None


def test_finalize_stale_comments_preconditions_no_longer_hold() -> None:
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    action = _reset_action()
    result = ActionResult.skip(
        action, "stale precondition: gone", mode="stale_downgrade"
    )

    out = finalize_tech_lead_op_execution(result, action, repository_host=host, ops=ops)

    assert out is result
    (number, comment), _ = host.add_comment.call_args
    assert number == 500 and "Preconditions no longer hold" in comment
    host.update_issue_state.assert_called_once_with(500, "closed")
    assert ops.load_op(issue_number=500) is None


def test_finalize_failure_keeps_op_for_retry() -> None:
    """A loud executor failure is NOT terminal: no comment, no close, op kept
    so the next tick retries."""
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    action = _reset_action()
    result = ActionResult.fail(action, "reset owner failed")

    out = finalize_tech_lead_op_execution(result, action, repository_host=host, ops=ops)

    assert out is result
    host.add_comment.assert_not_called()
    host.update_issue_state.assert_not_called()
    assert ops.load_op(issue_number=500) is not None


def test_finalize_passthrough_for_direct_execute_authority() -> None:
    """proposal_issue_number == 0 (direct execute tier): untouched."""
    host = MagicMock()
    action = ResetRetryIssueAction(
        issue_number=13, rationale="r", proposal_id="A2", anchor_issue_number=99
    )
    result = ActionResult.ok(action, issue_number=13)

    out = finalize_tech_lead_op_execution(
        result, action, repository_host=host, ops=InMemoryTechLeadAuthorityStore()
    )

    assert out is result
    host.add_comment.assert_not_called()


# --- Applier dispatch (both act-level ops) ---------------------------------


def _applier(host: MagicMock, ops: InMemoryTechLeadAuthorityStore) -> ActionApplier:
    applier = ActionApplier(
        labels=MagicMock(),
        sessions=MagicMock(),
        events=MagicMock(),
        repository_host=host,
    )
    applier.tech_lead_ops = ops
    # Apply-time consent re-check (#6779 R16): the owner freshly re-reads the
    # proposal issue immediately before the target mutation. By default model an
    # issue that STILL confirms approval (open, gate absent) so the op proceeds;
    # withdrawal tests override this side_effect.
    host.get_issue.side_effect = lambda n: _issue(n, ["tech-lead-agent"])
    return applier


def test_applier_reset_op_executes_once_and_finalizes() -> None:
    """Approved reset op through the applier: #6777 executor invoked once,
    outcome comment + close on the proposal, op discarded."""
    config = Config()
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    run_reset = MagicMock(return_value=ResetRetryRunOutcome(success=True))
    applier = _applier(host, ops)
    applier.tech_lead_reset_retry = TechLeadResetRetryExecutor(
        events=MagicMock(),
        label_manager=LabelManager(config),
        read_issue=lambda number: _issue(number, ["blocked-failed"]),
        has_active_issue_runtime=lambda _n: False,
        run_reset=run_reset,
    )
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=500, op=_op()),)
    )

    result = applier.apply(action)

    assert result.success
    run_reset.assert_called_once_with(13, ["blocked-failed"])
    host.update_issue_state.assert_called_once_with(500, "closed")
    assert ops.load_op(issue_number=500) is None


def test_applier_stale_reset_op_downgrades_with_zero_target_mutations() -> None:
    """Stale preconditions: downgrade comment + close on the PROPOSAL, no
    reset, no target mutations."""
    config = Config()
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    run_reset = MagicMock()
    applier = _applier(host, ops)
    applier.tech_lead_reset_retry = TechLeadResetRetryExecutor(
        events=MagicMock(),
        label_manager=LabelManager(config),
        # No blocking label left: the diagnosed failure already recovered.
        read_issue=lambda number: _issue(number, ["agent:test"]),
        has_active_issue_runtime=lambda _n: False,
        run_reset=run_reset,
    )
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=500, op=_op()),)
    )

    result = applier.apply(action)

    assert not result.success  # skipped
    run_reset.assert_not_called()
    (number, comment), _ = host.add_comment.call_args
    assert number == 500 and "Preconditions no longer hold" in comment
    host.update_issue_state.assert_called_once_with(500, "closed")
    assert ops.load_op(issue_number=500) is None


def test_applier_kill_op_invokes_termination_owner_under_stale_policy() -> None:
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    op = _kill_op(14, session_id="RUN-14")
    ops.record_op(issue_number=501, op=op)
    run_kill = MagicMock(return_value=KillSessionRunOutcome(success=True))
    applier = _applier(host, ops)
    applier.tech_lead_kill_session = TechLeadKillSessionExecutor(
        events=MagicMock(),
        run_kill=run_kill,
    )
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=501, op=op),)
    )

    result = applier.apply(action)

    assert result.success
    run_kill.assert_called_once()
    assert run_kill.call_args[0][0].issue_number == 14
    host.update_issue_state.assert_called_once_with(501, "closed")
    assert ops.load_op(issue_number=501) is None


def test_applier_kill_op_stale_when_session_already_gone() -> None:
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    op = _kill_op(14, session_id="RUN-14")
    ops.record_op(issue_number=501, op=op)
    run_kill = MagicMock(
        return_value=KillSessionRunOutcome(
            success=False,
            stale_reason="issue #14 has no active killable session",
        )
    )
    applier = _applier(host, ops)
    applier.tech_lead_kill_session = TechLeadKillSessionExecutor(
        events=MagicMock(),
        run_kill=run_kill,
    )
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=501, op=op),)
    )

    result = applier.apply(action)

    assert not result.success
    run_kill.assert_called_once()
    (number, comment), _ = host.add_comment.call_args
    assert number == 501 and "Preconditions no longer hold" in comment
    assert ops.load_op(issue_number=501) is None


def _reset_execution() -> ResetRetryIssueAction:
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=500, op=_op()),)
    )
    assert isinstance(action, ResetRetryIssueAction)
    return action


def _kill_execution() -> KillHungSessionAction:
    op = _kill_op(14, session_id="RUN-14")
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=501, op=op),)
    )
    assert isinstance(action, KillHungSessionAction)
    return action


def _wired_reset_applier(
    host: MagicMock, ops: InMemoryTechLeadAuthorityStore, run_reset: MagicMock
) -> ActionApplier:
    applier = _applier(host, ops)
    applier.tech_lead_reset_retry = TechLeadResetRetryExecutor(
        events=MagicMock(),
        label_manager=LabelManager(Config()),
        read_issue=lambda number: _issue(number, ["blocked-failed"]),
        has_active_issue_runtime=lambda _n: False,
        run_reset=run_reset,
    )
    return applier


def _wired_kill_applier(
    host: MagicMock, ops: InMemoryTechLeadAuthorityStore, run_kill: MagicMock
) -> ActionApplier:
    applier = _applier(host, ops)
    applier.tech_lead_kill_session = TechLeadKillSessionExecutor(
        events=MagicMock(),
        run_kill=run_kill,
    )
    return applier


def test_applier_direct_kill_executes_without_proposal_consent_round_trip() -> None:
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    run_kill = MagicMock(return_value=KillSessionRunOutcome(success=True))
    applier = _wired_kill_applier(host, ops, run_kill)
    action = KillHungSessionAction(
        issue_number=14,
        rationale="Session is corroborated as hung.",
        proposal_id="A8",
        anchor_issue_number=99,
        target_session_id="RUN-14",
        target_terminal_id="issue-14",
        target_session_type="code",
        expected=EXPECTED,
    )

    result = applier.apply(action)

    assert result.success
    run_kill.assert_called_once()
    host.update_issue_state.assert_not_called()
    assert ops.list_ops() == ()


def test_applier_reset_op_preserved_inert_when_gate_readded_before_apply() -> None:
    """R16: remove-gate -> plan -> RE-ADD-gate -> apply. The fact scan planned
    the reset while the gate was absent; the operator re-added it before apply.
    The fresh consent re-read sees the gate back, so the op is PRESERVED inert —
    the reset never runs and the proposal is NOT closed."""
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    run_reset = MagicMock(return_value=ResetRetryRunOutcome(success=True))
    applier = _wired_reset_applier(host, ops, run_reset)
    # The operator re-added the gate between plan and apply.
    host.get_issue.side_effect = lambda n: _issue(
        n, ["tech-lead-agent", PROPOSED_TECH_LEAD_LABEL]
    )

    result = applier.apply(_reset_execution())

    assert not result.success  # withheld, not executed
    run_reset.assert_not_called()  # target never mutated
    host.update_issue_state.assert_not_called()  # proposal NOT closed
    assert ops.load_op(issue_number=500) is not None  # op preserved for next tick


def test_applier_kill_op_preserved_inert_when_gate_readded_before_apply() -> None:
    """R16 (kill path): the same withdraw-before-apply consent gate protects the
    kill execution path, not just reset."""
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    op = _kill_op(14, session_id="RUN-14")
    ops.record_op(issue_number=501, op=op)
    run_kill = MagicMock(return_value=KillSessionRunOutcome(success=True))
    applier = _wired_kill_applier(host, ops, run_kill)
    host.get_issue.side_effect = lambda n: _issue(
        n, ["tech-lead-agent", PROPOSED_TECH_LEAD_LABEL]
    )

    result = applier.apply(_kill_execution())

    assert not result.success
    run_kill.assert_not_called()
    host.update_issue_state.assert_not_called()
    assert ops.load_op(issue_number=501) is not None


def test_applier_gate_readded_case_variant_still_withholds() -> None:
    """R16 x R15: a case-variant gate re-added before apply still withdraws
    consent (the apply-time gate shares the case-insensitive predicate)."""
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    run_reset = MagicMock(return_value=ResetRetryRunOutcome(success=True))
    applier = _wired_reset_applier(host, ops, run_reset)
    host.get_issue.side_effect = lambda n: _issue(
        n, ["tech-lead-agent", "Proposed-Tech-Lead"]
    )

    result = applier.apply(_reset_execution())

    assert not result.success
    run_reset.assert_not_called()
    assert ops.load_op(issue_number=500) is not None


def test_applier_reset_op_executes_when_gate_still_absent_at_apply() -> None:
    """R16 (no regression): remove-gate -> plan -> apply with the gate STILL
    absent. The fresh consent re-read confirms approval, so the reset runs once
    and the proposal is finalized/closed — the gate has not withdrawn it."""
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    run_reset = MagicMock(return_value=ResetRetryRunOutcome(success=True))
    applier = _wired_reset_applier(host, ops, run_reset)
    host.get_issue.side_effect = lambda n: _issue(n, ["tech-lead-agent"])  # gate absent

    result = applier.apply(_reset_execution())

    assert result.success
    run_reset.assert_called_once_with(13, ["blocked-failed"])
    host.update_issue_state.assert_called_once_with(500, "closed")
    assert ops.load_op(issue_number=500) is None


def test_applier_closed_proposal_preserves_op_without_executing() -> None:
    """R16: a proposal CLOSED before apply is not approval — consent read shows
    closed, so the op is preserved inert (never executed)."""
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    run_reset = MagicMock(return_value=ResetRetryRunOutcome(success=True))
    applier = _wired_reset_applier(host, ops, run_reset)
    host.get_issue.side_effect = lambda n: Issue(
        number=n,
        title="t",
        labels=["tech-lead-agent"],
        state="closed",
        repo="owner/repo",
    )

    result = applier.apply(_reset_execution())

    assert not result.success
    run_reset.assert_not_called()
    host.update_issue_state.assert_not_called()
    assert ops.load_op(issue_number=500) is not None


def test_applier_read_error_at_apply_withholds_execution_fail_safe() -> None:
    """R16 (fail-safe): a consent read that RAISES must not execute. It cannot
    confirm approval, so the op is preserved inert rather than acted on."""
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=500, op=_op())
    run_reset = MagicMock(return_value=ResetRetryRunOutcome(success=True))
    applier = _wired_reset_applier(host, ops, run_reset)
    host.get_issue.side_effect = RuntimeError("GitHub unreachable")

    result = applier.apply(_reset_execution())

    assert not result.success
    run_reset.assert_not_called()
    host.update_issue_state.assert_not_called()
    assert ops.load_op(issue_number=500) is not None


def test_applier_unwired_executors_fail_loudly() -> None:
    applier = _applier(MagicMock(), InMemoryTechLeadAuthorityStore())
    reset = _reset_action()
    kill = KillHungSessionAction(
        issue_number=14, proposal_id="A2", proposal_issue_number=501
    )

    assert not applier.apply(reset).success
    assert not applier.apply(kill).success


# --- End-to-end: propose -> gated issue -> approval -> execute once --------


def test_end_to_end_gated_reset_proposal_executes_once() -> None:
    from issue_orchestrator.control.tech_lead_decision_actions import (
        plan_tech_lead_decision_actions,
    )
    from issue_orchestrator.domain.tech_lead_artifacts import (
        TechLeadDecision,
        TechLeadFinding,
    )

    config = Config()
    config.tech_lead_review_agent = "tech-lead-agent"
    labels = LabelManager(config)
    ops = InMemoryTechLeadAuthorityStore()
    host = _host(500)
    anchor = _issue(99, ["tech-lead-agent"], title="anchor")
    decision = TechLeadDecision(
        summary="s",
        findings=(
            TechLeadFinding(
                id="T1", title="f", classification="infra", evidence=("log",)
            ),
        ),
        proposed_actions=(_proposed("reset_retry", 13),),
    )

    # 1. Completion planning under propose authority -> gated issue action.
    planned = plan_tech_lead_decision_actions(
        decision,
        config,
        labels,
        anchor_issue=anchor,
        expected=EXPECTED,
        op_ledger=build_op_ledger(ops.list_ops()),
        pattern_ledger={},
        source_run_id="run-1",
        source_session_name="issue-99",
        observed_at="2026-07-11T00:00:00+00:00",
        observed_session_generation=lambda _n: None,
        dedup_corpus=OpenIssueCorpus.disabled(),
        dedup_grant=DuplicateTargetGrant.none(),
    )
    [creation] = [
        a for a in planned if isinstance(a, CreateTechLeadProposalIssueAction)
    ]

    # 2. Apply: proposal issue created + stored op recorded.
    applier = _applier(host, ops)
    assert applier.apply(creation).success
    assert ops.load_op(issue_number=500) is not None

    # 2b. A re-proposal now dedups onto the open proposal issue.
    replanned = plan_tech_lead_decision_actions(
        decision,
        config,
        labels,
        anchor_issue=anchor,
        expected=EXPECTED,
        op_ledger=build_op_ledger(ops.list_ops()),
        pattern_ledger={},
        source_run_id="run-2",
        source_session_name="issue-99",
        observed_at="2026-07-11T01:00:00+00:00",
        observed_session_generation=lambda _n: None,
        dedup_corpus=OpenIssueCorpus.disabled(),
        dedup_grant=DuplicateTargetGrant.none(),
    )
    [dedup_comment] = [a for a in replanned if isinstance(a, AddCommentAction)]
    assert dedup_comment.number == 500
    assert not any(isinstance(a, CreateTechLeadProposalIssueAction) for a in replanned)

    # 3. Simulate operator approval: the scan shows #500 without the gate.
    approved_issue = _issue(500, ["tech-lead-agent"])
    approved_ops = reconcile_tech_lead_proposals(
        [approved_issue], ops=dict(ops.list_ops())
    ).approved
    [execution] = plan_approved_tech_lead_op_executions(approved_ops)

    # 4. Execute once: reset owner invoked, proposal finalized, op discarded.
    run_reset = MagicMock(return_value=ResetRetryRunOutcome(success=True))
    applier.tech_lead_reset_retry = TechLeadResetRetryExecutor(
        events=MagicMock(),
        label_manager=labels,
        read_issue=lambda number: _issue(number, ["blocked-failed"]),
        has_active_issue_runtime=lambda _n: False,
        run_reset=run_reset,
    )
    assert applier.apply(execution).success
    run_reset.assert_called_once()
    host.update_issue_state.assert_called_once_with(500, "closed")
    assert ops.load_op(issue_number=500) is None

    # 5. The next scan finds no op row -> nothing further to execute.
    leftover = reconcile_tech_lead_proposals(
        [approved_issue], ops=dict(ops.list_ops())
    ).approved
    assert plan_approved_tech_lead_op_executions(leftover) == []


class TestALaterObservationCanRetireAnEarlierOne:
    """F3: "last observation wins" must include observing that it is done.

    The observer filtered on `_awaits_approval` BEFORE resolving duplicate
    issue numbers, so a later observation without the gate was discarded
    rather than superseding the earlier gated one. Connecting a second source
    made that reachable: an issue approved between the worker fetch and the
    anchor scan stayed advertised as pending despite fresher evidence in the
    very same tick.
    """

    @staticmethod
    def _issue(number: int, *, labels: list[str], state: str = "open"):
        from issue_orchestrator.domain.models import Issue

        return Issue(number=number, title="Proposal", labels=labels, state=state)

    def test_a_later_ungated_observation_removes_the_proposal(self) -> None:
        from issue_orchestrator.control.tech_lead_proposals import (
            observe_gated_tech_lead_proposals,
        )

        gated = self._issue(9000, labels=["agent:tech-lead", "proposed-tech-lead"])
        approved = self._issue(9000, labels=["agent:tech-lead"])

        assert observe_gated_tech_lead_proposals([gated], [approved]) == ()

    def test_a_later_closed_observation_removes_the_proposal(self) -> None:
        from issue_orchestrator.control.tech_lead_proposals import (
            observe_gated_tech_lead_proposals,
        )

        gated = self._issue(9000, labels=["agent:tech-lead", "proposed-tech-lead"])
        closed = self._issue(
            9000, labels=["agent:tech-lead", "proposed-tech-lead"], state="closed"
        )

        assert observe_gated_tech_lead_proposals([gated], [closed]) == ()

    def test_the_earlier_observation_still_wins_when_nothing_supersedes_it(
        self,
    ) -> None:
        """The fix must not make a proposal vanish for being absent elsewhere.

        Absence from a later set is not an observation of that issue; only an
        actual later observation of the SAME issue may retire it.
        """
        from issue_orchestrator.control.tech_lead_proposals import (
            observe_gated_tech_lead_proposals,
        )

        gated = self._issue(9000, labels=["agent:tech-lead", "proposed-tech-lead"])
        unrelated = self._issue(9001, labels=["agent:tech-lead"])

        observed = observe_gated_tech_lead_proposals([gated], [unrelated])

        assert [p.issue_number for p in observed] == [9000]


class TestTheAuthoritativeQueryDecidesMembership:
    """F4: absence in the complete set retires an entry a partial set held.

    Appending an exhaustive gate-filtered query to a union does not make it
    authoritative. Its verdict on a retired proposal is ABSENCE, and absence
    supersedes nothing in a union — so a proposal approved between the worker
    fetch and the gate query stayed advertised on the strength of the older,
    narrower observation.
    """

    class _Host:
        def __init__(self, approval):
            self._approval = approval

        def list_issues(self, **_kwargs):
            return list(self._approval)

    @staticmethod
    def _issue(number: int, *, gated: bool = True):
        from issue_orchestrator.domain.models import Issue

        labels = ["agent:backend"] + (["proposed-tech-lead"] if gated else [])
        return Issue(number=number, title="Proposal", labels=labels)

    @staticmethod
    def _config():
        from types import SimpleNamespace

        return SimpleNamespace(filtering=SimpleNamespace(label=""))

    def test_an_empty_authoritative_result_retires_a_partial_entry(self) -> None:
        from issue_orchestrator.control.tech_lead_approval_scope import (
            observe_approval_backlog,
        )

        observed = observe_approval_backlog(
            self._Host([]), self._config(), [self._issue(9000)]
        )

        assert observed == (), (
            "the complete observation says #9000 is not pending, but the "
            "older worker fetch kept it on the board"
        )

    def test_an_authoritative_result_for_a_different_issue_also_retires_it(
        self,
    ) -> None:
        from issue_orchestrator.control.tech_lead_approval_scope import (
            observe_approval_backlog,
        )

        observed = observe_approval_backlog(
            self._Host([self._issue(9001)]),
            self._config(),
            [self._issue(9000)],
        )

        assert [p.issue_number for p in observed] == [9001]
