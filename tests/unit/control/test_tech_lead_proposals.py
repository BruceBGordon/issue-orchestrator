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
from issue_orchestrator.control.tech_lead_issue_creation import apply_create_tech_lead_issue
from issue_orchestrator.control.tech_lead_kill_session import (
    KillSessionRunOutcome,
    TechLeadKillSessionExecutor,
)
from issue_orchestrator.control.tech_lead_proposals import (
    apply_discard_terminal_tech_lead_proposal_ops,
    build_op_ledger,
    build_tech_lead_proposal_issue_action,
    finalize_tech_lead_op_execution,
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
)
from issue_orchestrator.infra.config import Config
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
        finding_ids=finding_ids,
    )


def _kill_op(target: int = 14, *, session_id: str = "RUN-14") -> StoredTechLeadOp:
    return _op(target, op_type="kill_hung_session", target_session_id=session_id)


def _proposed(act_type: str = "reset_retry", target: int = 13) -> ProposedTechLeadAction:
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
            title="t", body="b", labels=("x",), op=_op(), anchor_issue_number=99
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
    anchor = _issue(7, ["tech-lead-agent"], title="Tech Lead Batch Review: 3 PRs pending")
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
    ops.record_op(issue_number=600, op=_op(20))     # genuinely closed
    ops.record_op(issue_number=601, op=_kill_op(21))  # live, dropped by truncation
    tracker = _FakeTracker({600: "closed", 601: "open"})
    action = DiscardTerminalTechLeadProposalOpsAction(
        candidate_issue_numbers=(600, 601)
    )

    result = apply_discard_terminal_tech_lead_proposal_ops(
        action, tracker=tracker, authority=ops
    )

    assert result.success
    assert ops.load_op(issue_number=600) is None      # confirmed closed -> discarded
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
        (ApprovedTechLeadOp(proposal_issue_number=500, op=_op(13, finding_ids=("T1", "T2"))),)
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
) -> CreateTechLeadCaseFileIssueAction:
    labels = ["tech-lead-agent", TECH_LEAD_OBSERVATION_LABEL]
    if area is not None:
        labels.append(f"area:{area}")
    return CreateTechLeadCaseFileIssueAction(
        title=f"Pattern case file: {signature}",
        body="documentation only",
        labels=tuple(labels),
        pr_count=0,
        pattern_signature=signature,
        diagnosis="Pool exhaustion comes from a leaked connection.",
        dedup_comment="first observation",
        additional_observation_comments=additional_comments,
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
    ops.record_pattern(signature="db-timeout", issue_number=600)
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
        call(600, "first observation"), call(600, "follow-up")
    ]


def test_apply_plain_tech_lead_issue_records_no_op() -> None:
    from issue_orchestrator.control.actions import CreateTechLeadIssueAction

    host = _host(500)
    ops = InMemoryTechLeadAuthorityStore()

    result = apply_create_tech_lead_issue(
        CreateTechLeadIssueAction(title="t", body="b", labels=("x",), pr_count=2),
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
    tampered_issue = _issue(500, ["tech-lead-agent"], title="Tech Lead proposal: reset & retry issue #6666 from scratch")
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

    out = finalize_tech_lead_op_execution(
        result, action, repository_host=host, ops=ops
    )

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

    out = finalize_tech_lead_op_execution(
        result, action, repository_host=host, ops=ops
    )

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

    out = finalize_tech_lead_op_execution(
        result, action, repository_host=host, ops=ops
    )

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
        active_session_run_id=lambda n: "RUN-14" if n == 14 else None,
        run_kill=run_kill,
    )
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=501, op=op),)
    )

    result = applier.apply(action)

    assert result.success
    run_kill.assert_called_once()
    assert run_kill.call_args[0][0] == 14
    host.update_issue_state.assert_called_once_with(501, "closed")
    assert ops.load_op(issue_number=501) is None


def test_applier_kill_op_stale_when_session_already_gone() -> None:
    host = MagicMock()
    ops = InMemoryTechLeadAuthorityStore()
    op = _kill_op(14, session_id="RUN-14")
    ops.record_op(issue_number=501, op=op)
    run_kill = MagicMock()
    applier = _applier(host, ops)
    applier.tech_lead_kill_session = TechLeadKillSessionExecutor(
        events=MagicMock(),
        active_session_run_id=lambda _n: None,
        run_kill=run_kill,
    )
    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=501, op=op),)
    )

    result = applier.apply(action)

    assert not result.success
    run_kill.assert_not_called()
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
        active_session_run_id=lambda n: "RUN-14" if n == 14 else None,
        run_kill=run_kill,
    )
    return applier


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
    host.get_issue.side_effect = lambda n: _issue(n, ["tech-lead-agent", PROPOSED_TECH_LEAD_LABEL])

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
    host.get_issue.side_effect = lambda n: _issue(n, ["tech-lead-agent", PROPOSED_TECH_LEAD_LABEL])

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
    host.get_issue.side_effect = lambda n: _issue(n, ["tech-lead-agent", "Proposed-Tech-Lead"])

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
        number=n, title="t", labels=["tech-lead-agent"], state="closed", repo="owner/repo"
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
        active_session_run_id=lambda _n: None,
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
        active_session_run_id=lambda _n: None,
        dedup_corpus=OpenIssueCorpus.disabled(),
        dedup_grant=DuplicateTargetGrant.none(),
    )
    [dedup_comment] = [a for a in replanned if isinstance(a, AddCommentAction)]
    assert dedup_comment.number == 500
    assert not any(
        isinstance(a, CreateTechLeadProposalIssueAction) for a in replanned
    )

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
