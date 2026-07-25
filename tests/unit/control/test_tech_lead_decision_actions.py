"""Tests for tech_lead decision -> orchestrator action mapping (ADR-0031)."""

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    CreateTechLeadCaseFileIssueAction,
    CreateTechLeadIssueAction,
    CreateTechLeadProposalIssueAction,
    ResetRetryIssueAction,
    SurfaceTechLeadProposalAction,
    TechLeadMilestoneIntent,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.proposal_dedup import OpenIssueRef
from issue_orchestrator.control.proposal_dedup_gate import (
    DuplicateTargetGrant,
    OpenIssueCorpus,
)
from issue_orchestrator.control.tech_lead_decision_actions import (
    plan_tech_lead_decision_actions,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_artifacts import (
    ProposedTechLeadAction,
    TechLeadDecision,
    TechLeadFinding,
)
from issue_orchestrator.domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    TECH_LEAD_OBSERVATION_LABEL,
)
from issue_orchestrator.infra.config import Config


EXPECTED = build_expected_for_mutation()
NEEDS_HUMAN = "needs-human"
SOURCE_RUN = {
    "source_run_id": "run-1",
    "source_session_name": "issue-99",
    "observed_at": "2026-07-11T00:00:00+00:00",
}


def _decision(*actions: ProposedTechLeadAction) -> TechLeadDecision:
    finding_ids = {ref for action in actions for ref in action.finding_ids}
    findings = tuple(
        TechLeadFinding(
            id=fid,
            title=f"Finding {fid}",
            classification="infra",
            evidence=("orchestrator log lines 10-20",),
        )
        for fid in sorted(finding_ids)
    )
    return TechLeadDecision(
        summary="summary",
        findings=findings,
        proposed_actions=tuple(actions),
    )


def _config(**authority_overrides: str) -> Config:
    from unittest.mock import Mock

    config = Config()
    # A worker agent must exist: decision-driven create_issue routes the new
    # issue to the typed, validated follow-up worker (#6779 R5/R9).
    config.agents = {"agent:web": Mock()}
    config.tech_lead_follow_up_agent = "agent:web"
    for key, value in authority_overrides.items():
        setattr(config.tech_lead.authority, key, value)
    return config


def _anchor(number: int = 99, **overrides) -> Issue:
    fields = {
        "number": number,
        "title": f"Anchor issue {number}",
        "labels": ["agent:tech-lead"],
        "repo": "owner/repo",
    }
    fields.update(overrides)
    return Issue(**fields)


def _plan(
    decision: TechLeadDecision,
    config: Config | None = None,
    anchor: Issue | None = None,
    op_ledger: dict[tuple[str, int], int] | None = None,
    active_session_run_id=lambda _n: None,
    pattern_ledger: dict[str, int] | None = None,
    dedup_corpus: OpenIssueCorpus | None = None,
    dedup_grant: DuplicateTargetGrant | None = None,
):
    config = config or _config()
    return plan_tech_lead_decision_actions(
        decision,
        config,
        LabelManager(config),
        anchor_issue=anchor or _anchor(),
        expected=EXPECTED,
        op_ledger=op_ledger or {},
        active_session_run_id=active_session_run_id,
        pattern_ledger=pattern_ledger or {},
        dedup_corpus=dedup_corpus or OpenIssueCorpus.disabled(),
        dedup_grant=dedup_grant or DuplicateTargetGrant.none(),
        **SOURCE_RUN,
    )


def _shadow_digests(actions) -> list[AddCommentAction]:
    return [
        action
        for action in actions
        if isinstance(action, AddCommentAction)
        and "shadow mode" in action.comment
    ]


def test_post_comment_execute_maps_to_add_comment_with_provenance() -> None:
    action = ProposedTechLeadAction(
        id="A1",
        action_type="post_comment",
        target_number=42,
        target_is_pr=True,
        body="Diagnosis: flaky CI.",
        finding_ids=("T1", "T2"),
    )

    [planned] = _plan(_decision(action))

    assert isinstance(planned, AddCommentAction)
    assert planned.number == 42
    assert planned.is_pr is True
    assert planned.comment.startswith("Diagnosis: flaky CI.")
    assert planned.comment.endswith(
        "\n\n---\n*Proposed by tech_lead session (action A1;"
        " findings: T1, T2) — ADR-0031.*"
    )
    assert "tech_lead" in planned.reason and "A1" in planned.reason
    assert planned.expected is EXPECTED


def test_create_issue_execute_maps_to_create_tech_lead_issue() -> None:
    action = ProposedTechLeadAction(
        id="A2",
        action_type="create_issue",
        title="Fix flaky CI runner",
        body="The runner disconnects mid-build.",
        labels=("bug",),
    )

    [planned] = _plan(_decision(action))

    assert isinstance(planned, CreateTechLeadIssueAction)
    assert planned.title == "Fix flaky CI runner"
    assert planned.body.startswith("The runner disconnects mid-build.")
    assert "(action A2; findings: none)" in planned.body
    assert "bug" in planned.labels
    assert planned.pr_count == 0
    assert planned.milestone == TechLeadMilestoneIntent()
    assert "tech_lead" in planned.reason and "A2" in planned.reason
    assert planned.expected is EXPECTED


class TestCreateIssueDedup:
    """#6878: the planner translates each ProposalDedupGate outcome into actions.
    The gate's decision logic is covered in test_proposal_dedup_gate.py; here we
    assert the ACTION each outcome produces at the planner boundary."""

    _MATCH = OpenIssueRef(
        1234, "Stabilize CI runner disconnects", "runner drops mid build"
    )

    def _issue(self, **overrides) -> ProposedTechLeadAction:
        base = dict(
            id="A1",
            action_type="create_issue",
            title="Stabilize CI runner disconnects",
            body="The runner disconnects mid-build.",
        )
        base.update(overrides)
        return ProposedTechLeadAction(**base)

    def _ready(self, *issues: OpenIssueRef) -> OpenIssueCorpus:
        return OpenIssueCorpus.ready(issues or (self._MATCH,))

    _GRANT = DuplicateTargetGrant.of({1234})

    # --- CommentExisting: verified + granted + execute/execute authority ---

    def test_confirmed_duplicate_comments_with_title_and_body(self) -> None:
        planned = _plan(
            _decision(self._issue(duplicate_of=1234)),
            dedup_corpus=self._ready(),
            dedup_grant=self._GRANT,
        )
        assert not any(isinstance(a, CreateTechLeadIssueAction) for a in planned)
        [comment] = [a for a in planned if isinstance(a, AddCommentAction)]
        assert comment.number == 1234 and comment.is_pr is False
        # B5: BOTH the proposal title and body are routed onto the existing issue.
        assert "Stabilize CI runner disconnects" in comment.comment
        assert "The runner disconnects mid-build." in comment.comment
        assert "#1234" in comment.reason

    # --- GateSuspectedDuplicate: authority-respecting, evidence-carrying ---

    def test_confirmed_duplicate_gated_under_propose_authority(self) -> None:
        [planned] = _plan(
            _decision(self._issue(duplicate_of=1234)),
            _config(create_issue="propose"),
            dedup_corpus=self._ready(),
            dedup_grant=self._GRANT,
        )
        # No immediate comment under propose (B2); a gated create carries evidence.
        assert isinstance(planned, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in planned.labels
        assert "#1234" in planned.body

    def test_confirmed_duplicate_gated_when_post_comment_is_propose(self) -> None:
        # create_issue=execute but post_comment=propose -> still no immediate write.
        [planned] = _plan(
            _decision(self._issue(duplicate_of=1234)),
            _config(post_comment="propose"),
            dedup_corpus=self._ready(),
            dedup_grant=self._GRANT,
        )
        assert isinstance(planned, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in planned.labels

    def test_verified_but_out_of_grant_citation_is_gated_not_commented(self) -> None:
        # A known open issue outside the comment grant: gated (writes nothing).
        [gated] = _plan(
            _decision(self._issue(duplicate_of=1234)),
            dedup_corpus=self._ready(),
            dedup_grant=DuplicateTargetGrant.none(),
        )
        assert isinstance(gated, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert "#1234" in gated.body

    def test_lexical_backstop_gates_regardless_of_grant(self) -> None:
        # B2: the backstop gates even with no redirect grant (batch/failure).
        [gated] = _plan(
            _decision(self._issue()),  # no duplicate_of
            dedup_corpus=self._ready(),
            dedup_grant=DuplicateTargetGrant.none(),
        )
        assert isinstance(gated, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert "#1234" in gated.body and "score" in gated.body.lower()

    def test_similarity_threshold_controls_lexical_backstop(self) -> None:
        strict = _config()
        strict.tech_lead.dedup.similarity_threshold = 0.99
        [created] = _plan(
            _decision(self._issue()),
            strict,
            dedup_corpus=self._ready(),
        )
        assert isinstance(created, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL not in created.labels

        permissive = _config()
        permissive.tech_lead.dedup.similarity_threshold = 0.98
        [gated] = _plan(
            _decision(self._issue()),
            permissive,
            dedup_corpus=self._ready(),
        )
        assert isinstance(gated, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert "#1234" in gated.body

    def test_missing_citation_is_gated_never_comments_it(self) -> None:
        planned = _plan(
            _decision(self._issue(duplicate_of=999)),  # not in the corpus
            dedup_corpus=self._ready(),
            dedup_grant=self._GRANT,
        )
        [gated] = planned
        assert isinstance(gated, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert not any(
            isinstance(a, AddCommentAction) and a.number == 999 for a in planned
        )

    # --- Corpus state: DISABLED files normally, UNAVAILABLE fails closed ---

    def test_disabled_corpus_without_citation_files_normally(self) -> None:
        # Configured-off posture: a proposal with no citation files normally.
        [created] = _plan(
            _decision(self._issue()), dedup_corpus=OpenIssueCorpus.disabled()
        )
        assert isinstance(created, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL not in created.labels

    def test_disabled_corpus_with_citation_is_gated_not_filed(self) -> None:
        # The agent's dedup intent is preserved (gated with the candidate), never
        # discarded into a novel issue.
        planned = _plan(
            _decision(self._issue(duplicate_of=1234)),
            dedup_corpus=OpenIssueCorpus.disabled(),
        )
        [gated] = planned
        assert isinstance(gated, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert "#1234" in gated.body
        assert not any(isinstance(a, AddCommentAction) for a in planned)

    def test_unavailable_corpus_without_citation_fails_closed(self) -> None:
        # A fact-production failure must NEVER file unchecked — gate it.
        planned = _plan(
            _decision(self._issue()), dedup_corpus=OpenIssueCorpus.unavailable()
        )
        [gated] = planned
        assert isinstance(gated, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert not any(isinstance(a, AddCommentAction) for a in planned)

    def test_unavailable_corpus_with_citation_gates_with_candidate(self) -> None:
        planned = _plan(
            _decision(self._issue(duplicate_of=1234)),
            dedup_corpus=OpenIssueCorpus.unavailable(),
        )
        [gated] = planned
        assert isinstance(gated, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert "#1234" in gated.body

    def test_novel_proposal_files_normally_under_execute(self) -> None:
        [created] = _plan(
            _decision(self._issue()),
            dedup_corpus=self._ready(OpenIssueRef(42, "Unrelated widget gadget", "")),
            dedup_grant=self._GRANT,
        )
        assert isinstance(created, CreateTechLeadIssueAction)
        assert PROPOSED_TECH_LEAD_LABEL not in created.labels

    # --- Intra-decision dedup (#6883 review): siblings within ONE decision ---

    def test_identical_sibling_creates_only_the_first_is_filed(self) -> None:
        # Two identical create_issue proposals in one decision, novel vs the
        # backlog (empty READY corpus), under execute: the persisted-corpus gate
        # classifies each as FileNew, so without intra-decision dedup BOTH would be
        # filed. The first files directly; the identical sibling is GATED.
        a = self._issue(id="A1")
        b = self._issue(id="A2")  # identical title/body
        planned = _plan(
            _decision(a, b),
            dedup_corpus=OpenIssueCorpus.ready(()),  # novel vs backlog
            dedup_grant=DuplicateTargetGrant.none(),
        )
        creates = [x for x in planned if isinstance(x, CreateTechLeadIssueAction)]
        ungated = [c for c in creates if PROPOSED_TECH_LEAD_LABEL not in c.labels]
        gated = [c for c in creates if PROPOSED_TECH_LEAD_LABEL in c.labels]
        assert len(ungated) == 1  # only the FIRST is filed directly under execute
        assert len(gated) == 1  # the identical sibling is gated, not spam-filed
        assert "intra-decision duplicate" in gated[0].body
        assert "A1" in gated[0].body  # names the sibling it duplicates

    def test_distinct_sibling_creates_both_file(self) -> None:
        a = self._issue(id="A1", title="Stabilize CI runner disconnects", body="x")
        b = self._issue(id="A2", title="Add retry cap to publish gate", body="y")
        planned = _plan(
            _decision(a, b),
            dedup_corpus=OpenIssueCorpus.ready(()),
            dedup_grant=DuplicateTargetGrant.none(),
        )
        creates = [x for x in planned if isinstance(x, CreateTechLeadIssueAction)]
        assert len(creates) == 2
        assert all(PROPOSED_TECH_LEAD_LABEL not in c.labels for c in creates)

    def test_cited_sibling_is_gated_not_a_second_ungated_create(self) -> None:
        # #6883 review case 1: a CommentExisting proposal must still register as a
        # sibling. A1 cites #1234 (routed onto it as a comment); A2 is identical
        # but uncited, and #1234's stored text is lexically distant so A2 does NOT
        # trip the corpus backstop -> it would classify FileNew. Without covering
        # CommentExisting in the batch ledger, A2 files an UNGATED create. It must
        # instead be gated as an intra-decision duplicate of A1.
        distant = self._ready(OpenIssueRef(1234, "Migrate billing webhooks", "v2"))
        planned = _plan(
            _decision(self._issue(id="A1", duplicate_of=1234), self._issue(id="A2")),
            dedup_corpus=distant,
            dedup_grant=self._GRANT,
        )
        comments = [a for a in planned if isinstance(a, AddCommentAction)]
        assert len(comments) == 1 and comments[0].number == 1234  # only A1 routes
        creates = [x for x in planned if isinstance(x, CreateTechLeadIssueAction)]
        assert [PROPOSED_TECH_LEAD_LABEL in c.labels for c in creates] == [True]
        assert not any(  # the invariant the reviewer proved was broken
            PROPOSED_TECH_LEAD_LABEL not in c.labels for c in creates
        )
        assert "intra-decision duplicate" in creates[0].body
        assert "A1" in creates[0].body

    def test_two_identical_citations_emit_one_comment_not_two(self) -> None:
        # #6883 review case 2: two identical proposals both citing #1234 must not
        # emit two AddCommentActions to the same issue. A1 comments; A2 is gated,
        # and its gate note COMPOSES the sibling reason with the cited candidate so
        # #1234 is not lost.
        planned = _plan(
            _decision(
                self._issue(id="A1", duplicate_of=1234),
                self._issue(id="A2", duplicate_of=1234),
            ),
            dedup_corpus=self._ready(),
            dedup_grant=self._GRANT,
        )
        comments = [a for a in planned if isinstance(a, AddCommentAction)]
        assert len(comments) == 1 and comments[0].number == 1234  # never two
        [gated] = [x for x in planned if isinstance(x, CreateTechLeadIssueAction)]
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert "intra-decision duplicate" in gated.body and "A1" in gated.body
        assert "#1234" in gated.body  # composed: the cited candidate survives

    def test_sibling_that_is_also_a_corpus_match_keeps_its_candidate_and_score(
        self,
    ) -> None:
        # #6883 review finding 2: batch gating must COMPOSE with, not REPLACE, the
        # typed gate's evidence. Two identical uncited proposals both lexically
        # match #1234 -> GateSuspectedDuplicate with a score. The first gated body
        # carries #1234 + score; the sibling must retain BOTH plus the batch note.
        planned = _plan(
            _decision(self._issue(id="A1"), self._issue(id="A2")),
            dedup_corpus=self._ready(),  # _MATCH #1234 is lexically similar
            dedup_grant=DuplicateTargetGrant.none(),
        )
        creates = [x for x in planned if isinstance(x, CreateTechLeadIssueAction)]
        assert len(creates) == 2 and all(
            PROPOSED_TECH_LEAD_LABEL in c.labels for c in creates
        )
        [sibling] = [c for c in creates if "intra-decision duplicate" in c.body]
        [first] = [c for c in creates if "intra-decision duplicate" not in c.body]
        # The sibling loses NOTHING the standalone gate captured...
        assert "#1234" in sibling.body and "score" in sibling.body.lower()
        assert "A1" in sibling.body  # ...and still names the sibling it duplicates
        # ...while the first still carries the standalone candidate + score.
        assert "#1234" in first.body and "score" in first.body.lower()

    def test_outcome_gate_note_fails_fast_never_falls_open(self) -> None:
        # #6883 review: the outcome→note mapper must not silently degrade to the
        # ungated path (return None under execute) for anything but FileNew. A
        # value outside DedupOutcome — a bad caller, or a future variant added
        # without extending the mapper — must FAIL FAST, never fail open.
        from issue_orchestrator.control.tech_lead_gate_notes import outcome_gate_note

        with pytest.raises(AssertionError):
            outcome_gate_note(object(), execute=True)  # type: ignore[arg-type]


class TestDecisionIssuePolicy:
    """Decision-created issues route through the tech_lead: config owner (F4)."""

    def _issue_action(self, labels: tuple[str, ...] = ("bug",)) -> ProposedTechLeadAction:
        return ProposedTechLeadAction(
            id="A1",
            action_type="create_issue",
            title="Stabilize CI runner",
            body="Runner disconnects.",
            labels=labels,
        )

    def test_configured_labels_priority_and_scope_applied(self) -> None:
        config = _config()
        config.tech_lead.explicit_labels = ["needs-batch-review"]
        config.tech_lead.inherit_labels = ["team:backend", "not-on-anchor"]
        config.tech_lead.priority = "P2"
        config.filtering.label = "io-scope"
        anchor = _anchor(labels=["agent:tech-lead", "team:backend"])

        [planned] = _plan(_decision(self._issue_action()), config, anchor)

        assert isinstance(planned, CreateTechLeadIssueAction)
        assert planned.title.startswith("[P2-000] ")
        # The orchestrator-owned destination worker (#6779 R5) is appended so
        # the created issue is schedulable by normal discovery.
        assert planned.labels == (
            "io-scope",
            "needs-batch-review",
            "team:backend",
            "bug",
            "agent:web",
        )

    def test_milestone_strategy_inherits_anchor_milestone(self) -> None:
        anchor = _anchor(milestone="M2", milestone_number=7)

        [planned] = _plan(_decision(self._issue_action()), _config(), anchor)

        assert isinstance(planned, CreateTechLeadIssueAction)
        assert planned.milestone == TechLeadMilestoneIntent(inherited_number=7)

    def test_explicit_milestone_strategy_plans_name_intent(self) -> None:
        """Decision-created issues carry the explicit strategy as a NAME;
        resolution happens once, in the create-issue applier (#6769 F4)."""
        config = _config()
        config.tech_lead.milestone_strategy.explicit = "M5"

        [planned] = _plan(_decision(self._issue_action()), config)

        assert isinstance(planned, CreateTechLeadIssueAction)
        assert planned.milestone == TechLeadMilestoneIntent(explicit_name="M5")

    def test_root_cause_issue_carries_area_seam_label(self) -> None:
        action = ProposedTechLeadAction(
            id="A1", action_type="create_issue", title="Review DB seam",
            body="Repeated patching has not held.", labels=("design-review",), area="db",
        )
        [planned] = _plan(_decision(action))
        assert isinstance(planned, CreateTechLeadIssueAction)
        assert "area:db" in planned.labels

    def test_protected_agent_labels_fail_loudly_at_planning(self) -> None:
        """Validation upstream must have rejected these; planning never
        silently forwards or filters a protected label."""
        with pytest.raises(ValueError, match="protected labels"):
            _plan(_decision(self._issue_action(labels=("in-progress",))))


def test_escalate_to_human_maps_to_routing_surface_only() -> None:
    """Escalation = needs-human label + comment; never EscalateToHumanAction.

    EscalateToHumanAction's applier terminates the target issue's runtime,
    which would give the always-execute escalation floor the same effect as
    the shadow-only kill_hung_session intent (#6764 authority hole).
    """
    action = ProposedTechLeadAction(
        id="A3",
        action_type="escalate_to_human",
        target_number=55,
        body="Session keeps looping.\nDetails follow.",
        finding_ids=("T1",),
    )

    [label, comment] = _plan(_decision(action))

    assert isinstance(label, AddLabelAction)
    assert label.issue_number == 55
    assert label.label == NEEDS_HUMAN
    assert label.expected is EXPECTED
    assert isinstance(comment, AddCommentAction)
    assert comment.number == 55
    assert comment.is_pr is False
    assert comment.comment.startswith("## ⚠️ Tech Lead escalation")
    assert "Session keeps looping." in comment.comment
    assert "(action A3; findings: T1)" in comment.comment
    assert comment.expected is EXPECTED


def test_escalate_to_human_executes_even_in_full_propose_config() -> None:
    """escalate_to_human is the non-configurable floor: always executed."""
    config = _config(
        post_comment="propose", create_issue="propose", flag_pattern="propose"
    )
    action = ProposedTechLeadAction(
        id="A1",
        action_type="escalate_to_human",
        target_number=7,
        body="Needs a human.",
    )

    [label, comment] = _plan(_decision(action), config)

    assert isinstance(label, AddLabelAction)
    assert isinstance(comment, AddCommentAction)


def test_propose_authority_surfaces_shadow_proposal() -> None:
    config = _config(post_comment="propose")
    action = ProposedTechLeadAction(
        id="A1",
        action_type="post_comment",
        target_number=42,
        body="x" * 900,
        finding_ids=("T1",),
    )

    planned = _plan(_decision(action), config)

    [surfaced] = [a for a in planned if isinstance(a, SurfaceTechLeadProposalAction)]
    assert surfaced.mode == "shadow"
    assert surfaced.issue_number == 99  # anchor issue, not the target
    assert surfaced.action_id == "A1"
    assert surfaced.proposal_type == "post_comment"
    assert surfaced.target_number == 42
    assert surfaced.finding_ids == ("T1",)
    assert len(surfaced.body_preview) == 500  # capped at construction


def test_shadow_proposals_plan_durable_digest_comment() -> None:
    """Shadow records must reach the operator surface durably, not only as a
    trace event (#6761 finding 6)."""
    config = _config(post_comment="propose")
    action = ProposedTechLeadAction(
        id="A1",
        action_type="post_comment",
        target_number=42,
        body="Diagnosis for #42.",
        finding_ids=("T1",),
    )

    planned = _plan(_decision(action), config)

    [digest] = _shadow_digests(planned)
    assert digest.number == 99  # the anchor issue
    assert digest.is_pr is False
    assert "would-have-done" in digest.comment
    assert "A1" in digest.comment
    assert "post_comment" in digest.comment
    assert "#42" in digest.comment
    assert "Diagnosis for #42." in digest.comment
    assert "T1" in digest.comment
    assert digest.expected is EXPECTED
    # The digest complements the event-producing surface action.
    assert any(isinstance(a, SurfaceTechLeadProposalAction) for a in planned)


def test_digest_names_only_shadow_tier_knobs() -> None:
    """Act-level proposals never reach the shadow digest anymore (#6778):
    they become gated issues. The digest names only the flip-able knobs of
    the immediate/report tier proposals that stayed shadow."""
    config = _config(post_comment="propose")
    shadowed = ProposedTechLeadAction(
        id="A1", action_type="post_comment", target_number=42, body="c"
    )
    act_level = ProposedTechLeadAction(
        id="A2", action_type="reset_retry", target_number=42, body="r"
    )

    planned = _plan(_decision(shadowed, act_level), config)

    [digest] = _shadow_digests(planned)
    assert "`tech_lead.authority.post_comment`" in digest.comment
    assert "Flip" in digest.comment
    # The act-level proposal is a gated issue, not a digest entry.
    assert "reset_retry" not in digest.comment
    assert any(isinstance(a, CreateTechLeadProposalIssueAction) for a in planned)


def test_act_level_only_decision_plans_no_digest() -> None:
    """Gated proposals replace shadow digests for act-level intents (#6778)."""
    action = ProposedTechLeadAction(
        id="A1", action_type="kill_hung_session", target_number=13, body="r"
    )

    planned = _plan(_decision(action))

    assert _shadow_digests(planned) == []
    assert not any(isinstance(a, SurfaceTechLeadProposalAction) for a in planned)


def test_execute_only_decision_plans_no_digest_comment() -> None:
    action = ProposedTechLeadAction(
        id="A1", action_type="post_comment", target_number=1, body="c"
    )

    planned = _plan(_decision(action))

    assert _shadow_digests(planned) == []


def test_flag_pattern_execute_surfaces_as_pattern_and_opens_case_file() -> None:
    """Execute flag_pattern surfaces the pattern event AND opens the durable
    case file for a first-seen signature (#6781)."""
    action = ProposedTechLeadAction(
        id="A4",
        action_type="flag_pattern",
        body="Three sessions hit the same 422.",
        pattern_signature="github-422-batch",
        area="github-api",
    )

    surfaced, case_file = _plan(_decision(action))

    assert isinstance(surfaced, SurfaceTechLeadProposalAction)
    assert surfaced.mode == "pattern"
    assert surfaced.proposal_type == "flag_pattern"
    assert surfaced.target_number == 0

    assert isinstance(case_file, CreateTechLeadCaseFileIssueAction)
    assert case_file.pattern_signature == "github-422-batch"
    assert case_file.area == "github-api"
    assert TECH_LEAD_OBSERVATION_LABEL in case_file.labels
    assert "area:github-api" in case_file.labels
    assert case_file.expected is EXPECTED


def test_flag_pattern_execute_known_signature_comments_evidence() -> None:
    """A repeat observation of a recorded signature appends evidence to the
    existing case file instead of filing a second issue (#6781)."""
    action = ProposedTechLeadAction(
        id="A4",
        action_type="flag_pattern",
        body="Seen again in two more sessions.",
        pattern_signature="github-422-batch",
        finding_ids=("T1",),
    )

    surfaced, comment = _plan(
        _decision(action), pattern_ledger={"github-422-batch": 777}
    )

    assert isinstance(surfaced, SurfaceTechLeadProposalAction)
    assert surfaced.mode == "pattern"
    assert isinstance(comment, AddCommentAction)
    assert comment.number == 777
    assert comment.is_pr is False
    assert "observed again" in comment.reason
    assert not any(
        isinstance(a, CreateTechLeadCaseFileIssueAction) for a in (surfaced, comment)
    )


def test_two_same_signature_observations_open_one_case_file() -> None:
    """Two flag_pattern proposals with the SAME signature in ONE decision open
    exactly one case file and preserve the second as an evidence comment."""
    first = ProposedTechLeadAction(
        id="A1", action_type="flag_pattern", body="obs1", pattern_signature="sig-x"
    )
    second = ProposedTechLeadAction(
        id="A2", action_type="flag_pattern", body="obs2", pattern_signature="sig-x"
    )

    planned = _plan(_decision(first, second))

    creations = [
        a for a in planned if isinstance(a, CreateTechLeadCaseFileIssueAction)
    ]
    assert len(creations) == 1
    assert creations[0].pattern_signature == "sig-x"
    assert len(creations[0].additional_observation_comments) == 1
    assert "obs2" in creations[0].additional_observation_comments[0]


def test_different_signatures_open_distinct_case_files() -> None:
    first = ProposedTechLeadAction(
        id="A1", action_type="flag_pattern", body="obs1", pattern_signature="sig-a"
    )
    second = ProposedTechLeadAction(
        id="A2", action_type="flag_pattern", body="obs2", pattern_signature="sig-b"
    )

    planned = _plan(_decision(first, second))

    creations = [
        a for a in planned if isinstance(a, CreateTechLeadCaseFileIssueAction)
    ]
    assert {c.pattern_signature for c in creations} == {"sig-a", "sig-b"}


def test_case_file_ledger_for_other_signature_does_not_dedup() -> None:
    action = ProposedTechLeadAction(
        id="A4", action_type="flag_pattern", body="obs", pattern_signature="sig-new"
    )

    _surface, case_file = _plan(
        _decision(action), pattern_ledger={"sig-other": 321}
    )

    assert isinstance(case_file, CreateTechLeadCaseFileIssueAction)
    assert case_file.pattern_signature == "sig-new"


def test_flag_pattern_propose_surfaces_as_shadow_and_opens_no_case_file() -> None:
    """tech_lead.authority.flag_pattern must not be dead config (#6761 F5); under
    propose it stays a shadow record with NO durable case file (#6781)."""
    config = _config(flag_pattern="propose")
    action = ProposedTechLeadAction(
        id="A4",
        action_type="flag_pattern",
        body="Three sessions hit the same 422.",
        pattern_signature="github-422-batch",
    )

    planned = _plan(_decision(action), config)

    [surfaced] = [a for a in planned if isinstance(a, SurfaceTechLeadProposalAction)]
    assert surfaced.mode == "shadow"
    assert surfaced.proposal_type == "flag_pattern"
    assert len(_shadow_digests(planned)) == 1
    assert not any(
        isinstance(a, CreateTechLeadCaseFileIssueAction) for a in planned
    )


@pytest.mark.parametrize("act_type", ["reset_retry", "kill_hung_session"])
def test_act_level_under_propose_plans_gated_proposal_issue(act_type: str) -> None:
    """Propose-authority act-level intents become gated issues carrying the
    op payload (#6778): never shadow records, never direct executions."""
    action = ProposedTechLeadAction(
        id="A5",
        action_type=act_type,
        target_number=13,
        body="Rationale.",
        finding_ids=("T1",),
    )

    [planned] = _plan(_decision(action))

    assert isinstance(planned, CreateTechLeadProposalIssueAction)
    assert PROPOSED_TECH_LEAD_LABEL in planned.labels
    assert planned.anchor_issue_number == 99
    assert planned.expected is EXPECTED
    # The stored op is the executable payload; the body is documentation.
    assert planned.op.op_type == act_type
    assert planned.op.target_issue_number == 13
    assert planned.op.rationale == "Rationale."
    assert planned.op.source_action_id == "A5"
    assert planned.op.source_run_id == "run-1"
    assert planned.op.source_session_name == "issue-99"
    # Human documentation names the op, target, and the approval gesture.
    assert f"`{act_type}`" in planned.body
    assert "#13" in planned.body
    assert PROPOSED_TECH_LEAD_LABEL in planned.body
    assert "Batch Review" not in planned.title
    assert "Tech Lead Review" not in planned.title


@pytest.mark.parametrize("act_type", ["reset_retry", "kill_hung_session"])
def test_duplicate_open_proposal_comments_instead_of_second_issue(
    act_type: str,
) -> None:
    """One open proposal per (op, target) (#6778): a re-proposal plans a
    comment on the existing proposal issue, never a second issue."""
    action = ProposedTechLeadAction(
        id="A5", action_type=act_type, target_number=13, body="Again."
    )

    [planned] = _plan(
        _decision(action), op_ledger={(act_type, 13): 321}
    )

    assert isinstance(planned, AddCommentAction)
    assert planned.number == 321
    assert planned.is_pr is False
    assert PROPOSED_TECH_LEAD_LABEL in planned.comment
    assert "Again." in planned.comment
    assert not isinstance(planned, CreateTechLeadProposalIssueAction)


def test_duplicate_within_one_decision_plans_single_proposal_issue() -> None:
    first = ProposedTechLeadAction(
        id="A1", action_type="reset_retry", target_number=13, body="r1"
    )
    second = ProposedTechLeadAction(
        id="A2", action_type="reset_retry", target_number=13, body="r2"
    )

    planned = _plan(_decision(first, second))

    creations = [a for a in planned if isinstance(a, CreateTechLeadProposalIssueAction)]
    assert len(creations) == 1
    assert creations[0].op.source_action_id == "A1"


def test_ledger_for_other_target_does_not_dedup() -> None:
    action = ProposedTechLeadAction(
        id="A5", action_type="reset_retry", target_number=13, body="r"
    )

    [planned] = _plan(
        _decision(action), op_ledger={("reset_retry", 14): 321}
    )

    assert isinstance(planned, CreateTechLeadProposalIssueAction)


def test_create_issue_propose_creates_gated_issue() -> None:
    """Propose-authority create_issue CREATES the issue WITH the gate label
    (#6778) instead of a shadow record."""
    config = _config(create_issue="propose")
    action = ProposedTechLeadAction(
        id="A2",
        action_type="create_issue",
        title="Fix flaky CI runner",
        body="The runner disconnects mid-build.",
        labels=("ci",),
    )

    [planned] = _plan(_decision(action), config)

    assert isinstance(planned, CreateTechLeadIssueAction)
    assert not isinstance(planned, CreateTechLeadProposalIssueAction)
    assert PROPOSED_TECH_LEAD_LABEL in planned.labels
    assert "ci" in planned.labels
    assert not any(isinstance(a, SurfaceTechLeadProposalAction) for a in [planned])


def test_reset_retry_execute_plans_typed_reset_action() -> None:
    """Execute authority maps reset_retry to the typed executor action (#6764)."""
    config = _config(reset_retry="execute")
    action = ProposedTechLeadAction(
        id="A7",
        action_type="reset_retry",
        target_number=13,
        body="Worktree is unrecoverable; start from scratch.",
        finding_ids=("T1",),
    )

    [planned] = _plan(_decision(action), config)

    assert isinstance(planned, ResetRetryIssueAction)
    assert planned.issue_number == 13
    assert planned.anchor_issue_number == 99  # the anchor issue
    assert planned.proposal_id == "A7"
    assert planned.rationale.startswith("Worktree is unrecoverable")
    assert planned.finding_ids == ("T1",)
    assert "A7" in planned.reason
    assert planned.expected is EXPECTED
    # Execute-mode means no shadow surface and no digest for this proposal.
    assert not any(isinstance(a, SurfaceTechLeadProposalAction) for a in [planned])


def test_kill_hung_session_stays_gated_even_if_execute_sneaks_past_startup() -> None:
    """The planner never trusts config validation for kill_hung_session:
    it is a GATED PROPOSAL ISSUE even under 'execute' (its direct tier is
    not wired; startup rejects the mode, #6778)."""
    config = _config(kill_hung_session="execute")
    action = ProposedTechLeadAction(
        id="A8",
        action_type="kill_hung_session",
        target_number=13,
        body="Session looks hung.",
    )

    [planned] = _plan(_decision(action), config)

    assert isinstance(planned, CreateTechLeadProposalIssueAction)
    assert planned.op.op_type == "kill_hung_session"
    assert not isinstance(planned, ResetRetryIssueAction)


def test_mixed_decision_preserves_order_and_authority() -> None:
    config = _config(create_issue="propose")
    comment = ProposedTechLeadAction(
        id="A1", action_type="post_comment", target_number=1, body="c"
    )
    issue = ProposedTechLeadAction(
        id="A2", action_type="create_issue", title="t", body="b"
    )
    pattern = ProposedTechLeadAction(
        id="A3", action_type="flag_pattern", body="p", pattern_signature="sig-mix"
    )

    planned = _plan(_decision(comment, issue, pattern), config)

    assert isinstance(planned[0], AddCommentAction)
    # create_issue under propose is a gated creation now (#6778), not shadow.
    assert isinstance(planned[1], CreateTechLeadIssueAction)
    assert PROPOSED_TECH_LEAD_LABEL in planned[1].labels
    # flag_pattern under execute surfaces the event AND opens the case file.
    assert isinstance(planned[2], SurfaceTechLeadProposalAction)
    assert planned[2].mode == "pattern"
    assert isinstance(planned[3], CreateTechLeadCaseFileIssueAction)
    assert planned[3].pattern_signature == "sig-mix"
    # No shadow proposals in this decision -> no digest.
    assert _shadow_digests(planned) == []


def test_authority_mode_for_unknown_action_raises() -> None:
    from issue_orchestrator.infra.config_models import TechLeadAuthorityConfig

    with pytest.raises(ValueError, match="unknown tech_lead action type"):
        TechLeadAuthorityConfig().mode_for("merge_pr")


def test_authority_mode_for_escalate_is_always_execute() -> None:
    from issue_orchestrator.infra.config_models import TechLeadAuthorityConfig

    authority = TechLeadAuthorityConfig()
    assert authority.mode_for("escalate_to_human") == "execute"


class TestCreateIssueExpediteProducer:
    """Expedite intent (#6870) rides the create_issue action, gate-aware."""

    def _expedite_action(self, expedite: bool = True) -> ProposedTechLeadAction:
        return ProposedTechLeadAction(
            id="A1",
            action_type="create_issue",
            title="Fix the corrupting merge race",
            body="It corrupts state; needs working now.",
            expedite=expedite,
        )

    def test_execute_authority_carries_expedite_and_stays_ungated(self) -> None:
        # Default authority: create_issue = execute.
        [planned] = _plan(_decision(self._expedite_action()))
        assert isinstance(planned, CreateTechLeadIssueAction)
        assert planned.expedite is True
        # Execute authority creates an UNGATED issue: no proposed-tech-lead gate,
        # so the applier expedites it immediately.
        assert PROPOSED_TECH_LEAD_LABEL not in planned.labels

    def test_propose_authority_carries_expedite_but_is_gated(self) -> None:
        config = _config(create_issue="propose")
        [planned] = _plan(_decision(self._expedite_action()), config)
        assert isinstance(planned, CreateTechLeadIssueAction)
        assert planned.expedite is True
        # Propose authority gates the issue: expedite must wait for un-gating.
        assert PROPOSED_TECH_LEAD_LABEL in planned.labels

    def test_expedite_defaults_false_on_the_action(self) -> None:
        [planned] = _plan(_decision(self._expedite_action(expedite=False)))
        assert isinstance(planned, CreateTechLeadIssueAction)
        assert planned.expedite is False
