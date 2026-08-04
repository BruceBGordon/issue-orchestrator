"""Finding promotion: case file -> gated runnable issue -> shipped fix (#6957).

Behavioral coverage of the lane's acceptance criteria: eligibility, dedup, the
per-target cap, permanent decline, the fix:human exclusion, restart resume of
the durable ledger, and loop closure.
"""

from unittest.mock import MagicMock, Mock

import pytest

from issue_orchestrator.control.actions import (
    PromoteTechLeadFindingAction,
    ReportPromotedFindingEvidenceAction,
    SettleTechLeadPromotionAction,
)
from issue_orchestrator.control.tech_lead_finding_promotion import (
    PromotionReadBudget,
    apply_promote_tech_lead_finding,
    apply_report_promoted_finding_evidence,
    apply_settle_tech_lead_promotion,
    classify_promotion_outcomes,
    gather_finding_promotion_facts,
    plan_finding_promotions,
    plan_promotion_updates,
    plan_promotion_settlements,
    promotion_issue_labels,
    resolve_promotion_route,
    select_promotion_updates,
    select_promotable_findings,
)
from issue_orchestrator.domain.tech_lead_findings import (
    PROMOTION_STATE_DECLINED,
    PROMOTION_STATE_SHIPPED,
    PatternEvidence,
    PromotionUpdate,
    PromotableFinding,
    PromotedFinding,
    promotion_issue_marker,
    promotion_issue_title,
)
from issue_orchestrator.domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import PromotionRouteTarget
from issue_orchestrator.ports.promotion_target import (
    InMemoryPromotionTargetHost,
    PromotedIssueOutcome,
)
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
    TechLeadPromotionConflictError,
)

UPSTREAM = "issue-orchestrator/issue-orchestrator"


def _route(**entries) -> dict[str, PromotionRouteTarget]:
    """Route table from the terse ``area="repo"`` / ``area=(repo, scope, agent)``."""
    table: dict[str, PromotionRouteTarget] = {}
    for area, value in entries.items():
        if isinstance(value, tuple):
            repo, scope_label, agent_label = value
            table[area] = PromotionRouteTarget(
                repo=repo, scope_label=scope_label, agent_label=agent_label
            )
        else:
            table[area] = PromotionRouteTarget(repo=value)
    return table


def _config(**findings_overrides) -> Config:
    config = Config()
    config.repo = "porchpin/porchpin"
    config.agents = {"agent:backend": Mock(), "agent:tech-lead": Mock()}
    config.tech_lead_follow_up_agent = "agent:backend"
    # The lane's single activation owner requires a tech lead agent: promotion
    # actuates tech-lead findings, so it is inert without one (#6957 R2 F9).
    config.tech_lead_review_agent = "agent:tech-lead"
    for key, value in findings_overrides.items():
        setattr(config.tech_lead.findings, key, value)
    return config


def _evidence(
    signature: str,
    *,
    observations: int = 2,
    fix_class: str = "code",
    area: str = "",
    case_file: int = 65,
    diagnosis: str = "",
) -> PatternEvidence:
    return PatternEvidence(
        signature=signature,
        case_file_issue_number=case_file,
        observation_count=observations,
        fix_class=fix_class,
        area=area,
        diagnosis=diagnosis,
    )


def _record_case_file(
    authority,
    *,
    signature: str,
    issue_number: int = 65,
    observations: int = 1,
    fix_class: str = "code",
    area: str = "",
) -> None:
    """Open a case file and accrue *observations* distinct observations."""
    authority.record_pattern(
        signature=signature,
        issue_number=issue_number,
        observation_id=f"{signature}:obs-1",
        fix_class=fix_class,
        area=area,
    )
    for index in range(2, observations + 1):
        authority.note_pattern_observation(
            signature=signature, observation_id=f"{signature}:obs-{index}"
        )


def _append_action(
    signature: str,
    observation_id: str,
    *,
    issue_number: int = 65,
    comment: str = "observed again",
    fix_class: str = "",
    area: str = "",
):
    from issue_orchestrator.control.actions import AppendPatternObservationAction
    from issue_orchestrator.domain.tech_lead_findings import PatternObservation

    return AppendPatternObservationAction(
        issue_number=issue_number,
        pattern_signature=signature,
        observation=PatternObservation(
            observation_id=observation_id, comment=comment
        ),
        fix_class=fix_class,
        area=area,
    )


def _promotion(
    signature: str,
    *,
    repo: str = UPSTREAM,
    issue: int = 500,
    state: str = "promoted",
    case_file: int = 65,
    reported: int = 0,
) -> PromotedFinding:
    return PromotedFinding(
        signature=signature,
        case_file_issue_number=case_file,
        target_repo=repo,
        target_issue_number=issue,
        state=state,  # type: ignore[arg-type]
        reported_observations=reported,
    )


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_signature_at_min_evidence_is_promotable(self):
        config = _config()
        selected = select_promotable_findings(
            config,
            evidence=[_evidence("anchor-close-buries-escalation")],
            promotions=[],
        )
        assert [item.evidence.signature for item in selected] == [
            "anchor-close-buries-escalation"
        ]
        assert selected[0].target_repo == "porchpin/porchpin"

    def test_below_min_evidence_is_not_promotable(self):
        config = _config()
        selected = select_promotable_findings(
            config, evidence=[_evidence("sig", observations=1)], promotions=[]
        )
        assert selected == ()

    def test_fix_human_finding_is_never_promoted(self):
        """A human-gated problem made runnable manufactures doomed rework."""
        config = _config()
        selected = select_promotable_findings(
            config,
            evidence=[
                _evidence("config-lock-misroute", fix_class="human", observations=9)
            ],
            promotions=[],
        )
        assert selected == ()

    def test_unclassified_finding_is_never_promoted(self):
        config = _config()
        selected = select_promotable_findings(
            config,
            evidence=[_evidence("unclassified", fix_class="", observations=9)],
            promotions=[],
        )
        assert selected == ()

    def test_promote_off_disables_the_lane(self):
        config = _config(promote="off")
        selected = select_promotable_findings(
            config, evidence=[_evidence("sig", observations=9)], promotions=[]
        )
        assert selected == ()

    def test_best_evidenced_signatures_are_selected_first(self):
        config = _config(max_open_promoted=1)
        selected = select_promotable_findings(
            config,
            evidence=[
                _evidence("thin", observations=2),
                _evidence("thick", observations=7),
            ],
            promotions=[],
        )
        assert [item.evidence.signature for item in selected] == ["thick"]


class TestDedupAndDecline:
    def test_already_promoted_signature_is_not_refiled(self):
        config = _config()
        selected = select_promotable_findings(
            config,
            evidence=[_evidence("sig", observations=9)],
            promotions=[_promotion("sig")],
        )
        assert selected == ()

    def test_declined_signature_is_never_refiled(self):
        """Closing a gated promotion is a rejection; it must be permanent."""
        config = _config()
        selected = select_promotable_findings(
            config,
            evidence=[_evidence("sig", observations=99)],
            promotions=[_promotion("sig", state=PROMOTION_STATE_DECLINED)],
        )
        assert selected == ()

    def test_shipped_signature_is_not_refiled(self):
        config = _config()
        selected = select_promotable_findings(
            config,
            evidence=[_evidence("sig", observations=99)],
            promotions=[_promotion("sig", state=PROMOTION_STATE_SHIPPED)],
        )
        assert selected == ()

    def test_declined_promotion_does_not_consume_cap(self):
        """Terminal rows are not work in flight, so they must not block others."""
        config = _config(max_open_promoted=1)
        selected = select_promotable_findings(
            config,
            evidence=[_evidence("fresh")],
            promotions=[
                _promotion(
                    "old", repo="porchpin/porchpin", state=PROMOTION_STATE_DECLINED
                )
            ],
        )
        assert [item.evidence.signature for item in selected] == ["fresh"]


class TestStormBackpressure:
    def test_storm_of_eligible_signatures_files_at_most_the_cap(self):
        config = _config(max_open_promoted=3)
        evidence = [_evidence(f"sig-{index}", observations=5) for index in range(10)]

        selected = select_promotable_findings(config, evidence=evidence, promotions=[])

        assert len(selected) == 3

    def test_cap_counts_in_flight_promotions_per_target(self):
        config = _config(
            max_open_promoted=2,
            route=_route(upstream=UPSTREAM, default="self"),
        )
        evidence = [
            _evidence("up-1", area="upstream"),
            _evidence("up-2", area="upstream"),
            _evidence("local-1"),
        ]
        promotions = [_promotion("already", repo=UPSTREAM)]

        selected = select_promotable_findings(
            config, evidence=evidence, promotions=promotions
        )

        by_repo = [item.target_repo for item in selected]
        # One upstream slot left (2 - 1 in flight), self is untouched by it.
        assert by_repo.count(UPSTREAM) == 1
        assert by_repo.count("porchpin/porchpin") == 1

    def test_cap_folds_repository_case(self):
        config = _config(
            max_open_promoted=1,
            route=_route(upstream=UPSTREAM.upper(), default="self"),
        )

        selected = select_promotable_findings(
            config,
            evidence=[_evidence("new", area="upstream")],
            promotions=[_promotion("already", repo=UPSTREAM)],
        )

        assert selected == ()


class TestRouting:
    def test_area_routes_to_the_repo_that_owns_the_fix(self):
        config = _config(route=_route(**{"completion-pipeline": UPSTREAM, "default": "self"}))
        assert resolve_promotion_route(config, area="completion-pipeline").target_repo == UPSTREAM

    def test_unrouted_area_falls_back_to_the_default(self):
        config = _config(route=_route(**{"completion-pipeline": UPSTREAM, "default": "self"}))
        assert resolve_promotion_route(config, area="ui").target_repo == "porchpin/porchpin"

    def test_area_matching_is_case_insensitive(self):
        """The area rides an area:* GitHub label, and GitHub folds label names."""
        config = _config(route=_route(**{"Completion-Pipeline": UPSTREAM, "default": "self"}))
        assert resolve_promotion_route(config, area="completion-pipeline").target_repo == UPSTREAM

    def test_self_route_without_a_configured_repo_fails_loudly(self):
        config = _config()
        config.repo = None
        with pytest.raises(ValueError, match="no repository is configured"):
            resolve_promotion_route(config, area="")


class TestPromotionIssueComposition:
    def test_gated_promotion_carries_the_gate_and_agent_labels(self):
        config = _config()
        labels = promotion_issue_labels(config, area="completion-pipeline")
        assert PROPOSED_TECH_LEAD_LABEL in labels
        assert "agent:backend" in labels
        assert "area:completion-pipeline" in labels

    def test_auto_promotion_is_ungated(self):
        config = _config(promote="auto")
        labels = promotion_issue_labels(config, area="")
        assert PROPOSED_TECH_LEAD_LABEL not in labels
        assert "agent:backend" in labels

    def test_gated_promotion_is_never_runnable_in_the_target_planner(self):
        """The gate must actually block pickup, not just be present: a promoted
        issue is inert until an operator removes exactly that one label."""
        from issue_orchestrator.control.label_manager import LabelManager

        config = _config()
        labels = LabelManager(config)
        gated = promotion_issue_labels(config, area="completion-pipeline")

        assert labels.is_blocking_any(list(gated))
        # Removing the gate — the operator's single action — leaves an ordinary,
        # schedulable issue carrying its agent and area labels.
        ungated = [name for name in gated if name != PROPOSED_TECH_LEAD_LABEL]
        assert not labels.is_blocking_any(ungated)
        assert "agent:backend" in ungated

    def test_auto_promotion_is_runnable_immediately(self):
        from issue_orchestrator.control.label_manager import LabelManager

        config = _config(promote="auto")

        assert not LabelManager(config).is_blocking_any(
            list(promotion_issue_labels(config, area=""))
        )

    def test_self_route_carries_the_managed_repos_scope_label(self):
        """#6957 review F2: the scope label is part of being RUNNABLE.

        Discovery queries ``filtering.label`` alongside the agent label, so a
        promotion that omits it is invisible to the scheduler — approval that
        actuates nothing.
        """
        config = _config()
        config.filtering.label = "io-scope"

        labels = promotion_issue_labels(config, area="completion-pipeline")

        assert "io-scope" in labels

    def test_self_route_with_no_scope_label_configured_adds_none(self):
        config = _config()
        config.filtering.label = ""

        assert promotion_issue_labels(config, area="") == (
            "agent:backend",
            PROPOSED_TECH_LEAD_LABEL,
        )

    def test_foreign_route_carries_the_targets_declared_contract(self):
        """A foreign repo's queue contract comes from its route entry, never
        from the source repo's own scope label — which means nothing there."""
        config = _config(
            route=_route(
                **{
                    "completion-pipeline": (
                        UPSTREAM,
                        "upstream-scope",
                        "agent:platform",
                    ),
                    "default": "self",
                }
            )
        )
        config.filtering.label = "io-scope"

        labels = promotion_issue_labels(config, area="completion-pipeline")

        assert "agent:platform" in labels
        assert "upstream-scope" in labels
        assert "io-scope" not in labels
        assert "agent:backend" not in labels

    def test_undeclared_foreign_route_uses_no_scope_and_the_source_agent(self):
        config = _config(route=_route(**{"cp": UPSTREAM, "default": "self"}))
        config.filtering.label = "io-scope"

        route = resolve_promotion_route(config, area="cp")

        assert route.target_repo == UPSTREAM
        assert route.scope_label == ""
        assert route.agent_label == "agent:backend"

    def test_a_route_without_an_agent_label_is_rejected(self):
        from issue_orchestrator.domain.tech_lead_findings import PromotionRoute

        with pytest.raises(ValueError, match="no worker agent label"):
            PromotionRoute(target_repo=UPSTREAM, agent_label="")

    def test_title_names_the_source_repo_and_signature(self):
        assert (
            promotion_issue_title(
                source_repo="porchpin/porchpin", signature="anchor-close"
            )
            == "[tech-lead:porchpin] anchor-close"
        )

    def test_planned_action_links_the_case_file_as_the_evidence_ledger(self):
        config = _config()
        [action] = plan_finding_promotions(
            config,
            promotable=(
                PromotableFinding(
                    evidence=_evidence("anchor-close", case_file=65),
                    target_repo=UPSTREAM,
                ),
            ),
        )
        assert isinstance(action, PromoteTechLeadFindingAction)
        assert action.target_repo == UPSTREAM
        assert action.case_file_issue_number == 65
        assert "porchpin/porchpin#65" in action.body
        assert PROPOSED_TECH_LEAD_LABEL in action.labels

    def test_planned_action_carries_the_original_diagnosis_and_suggested_fix(self):
        finding = PromotableFinding(
            evidence=_evidence(
                "anchor-close",
                diagnosis=(
                    "Mechanism: anchor closure drops a pending escalation.\n\n"
                    "Suggested fix: preserve the escalation until acknowledged."
                ),
            ),
            target_repo=UPSTREAM,
        )

        [action] = plan_finding_promotions(_config(), promotable=(finding,))

        assert "### Diagnosis and suggested fix" in action.body
        assert "anchor closure drops a pending escalation" in action.body
        assert "preserve the escalation until acknowledged" in action.body


# ---------------------------------------------------------------------------
# Filing (apply boundary) + at-most-once
# ---------------------------------------------------------------------------


def _promote_action(
    signature: str = "anchor-close", *, observations: int = 2
) -> PromoteTechLeadFindingAction:
    config = _config()
    [action] = plan_finding_promotions(
        config,
        promotable=(
            PromotableFinding(
                evidence=_evidence(signature, case_file=65, observations=observations),
                target_repo=UPSTREAM,
            ),
        ),
    )
    assert isinstance(action, PromoteTechLeadFindingAction)
    return action


def _rerouted(
    action: PromoteTechLeadFindingAction, repo: str
) -> PromoteTechLeadFindingAction:
    """The same planned action as it looked under a DIFFERENT configured route.

    Only the target repo moves: the signature, its area, its title and its
    marker are all route-independent, which is exactly why an operator editing
    ``tech_lead.findings.route`` reaches the mismatch without any area change.
    """
    from dataclasses import replace

    return replace(action, target_repo=repo)


class TestFiling:
    def test_filing_records_the_ledger_row_and_returns_the_issue(self):
        target = InMemoryPromotionTargetHost()
        authority = InMemoryTechLeadAuthorityStore()

        result = apply_promote_tech_lead_finding(
            _promote_action(),
            target=target,
            authority=authority,
            now_iso="2026-08-04T00:00:00+00:00",
        )

        assert result.success
        assert len(target.filed) == 1
        row = authority.load_promotion(signature="anchor-close")
        assert row is not None
        assert row.target_repo == UPSTREAM
        assert row.target_issue_number == result.details["issue_number"]
        assert row.is_open
        # The issue body already carries the observations present at filing;
        # the next tick must not misreport them as later evidence.
        assert row.reported_observations == 2

    def test_a_failed_filing_leaves_the_ledger_untouched(self):
        """A phantom row would block the signature forever; retry must be clean."""
        target = InMemoryPromotionTargetHost()
        target.file_error = RuntimeError("403 no write access")
        authority = InMemoryTechLeadAuthorityStore()

        result = apply_promote_tech_lead_finding(
            _promote_action(), target=target, authority=authority, now_iso="t"
        )

        assert not result.success
        assert authority.load_promotion(signature="anchor-close") is None

    def test_a_stale_plan_never_files_a_second_issue(self):
        target = InMemoryPromotionTargetHost()
        authority = InMemoryTechLeadAuthorityStore()
        action = _promote_action()
        apply_promote_tech_lead_finding(
            action, target=target, authority=authority, now_iso="t"
        )

        again = apply_promote_tech_lead_finding(
            action, target=target, authority=authority, now_iso="t"
        )

        assert again.success
        assert again.details["deduplicated"] is True
        assert len(target.filed) == 1

    def test_restart_after_remote_create_before_ledger_write_recovers_one_issue(self):
        """The exact cross-system crash window must not duplicate the filing."""

        class FailFirstLedgerWrite(InMemoryTechLeadAuthorityStore):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next_record = True

            def record_promotion(self, *, promotion):
                if self.fail_next_record:
                    self.fail_next_record = False
                    raise RuntimeError("process died before the ledger commit")
                super().record_promotion(promotion=promotion)

        target = InMemoryPromotionTargetHost()
        authority = FailFirstLedgerWrite()
        action = _promote_action()

        crashed = apply_promote_tech_lead_finding(
            action,
            target=target,
            authority=authority,
            now_iso="t1",
        )
        assert not crashed.success
        assert len(target.filed) == 1
        assert authority.load_promotion(signature=action.signature) is None
        # The durable creation intent survives, and it is what the retry
        # finalizes from (#6957 round-3 review F11).
        assert authority.load_pending_promotion(signature=action.signature) is not None

        recovered = apply_promote_tech_lead_finding(
            action,
            target=target,
            authority=authority,
            now_iso="t2",
        )

        assert recovered.success
        assert len(target.filed) == 1
        assert recovered.details["issue_number"] == 1001
        assert (
            authority.load_promotion(signature=action.signature).target_issue_number
            == 1001
        )
        # The intent is retired once the ledger row lands.
        assert authority.load_pending_promotion(signature=action.signature) is None

    def test_recovery_keeps_the_watermark_the_filed_body_documents(self):
        """#6957 round-3 review F11: evidence must not be suppressed.

        A count-2 promotion is filed, its ledger write fails, the case file then
        reaches count 3, and a count-3 action recovers the SAME remote issue.
        Seeding reported_observations from that later action recorded evidence
        the target was never told, so select_promotion_updates never emitted the
        count-3 comment — permanently.
        """

        class FailFirstLedgerWrite(InMemoryTechLeadAuthorityStore):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next_record = True

            def record_promotion(self, *, promotion):
                if self.fail_next_record:
                    self.fail_next_record = False
                    raise RuntimeError("process died before the ledger commit")
                super().record_promotion(promotion=promotion)

        target = InMemoryPromotionTargetHost()
        authority = FailFirstLedgerWrite()

        # 1. Filed at count 2; the ledger write dies.
        crashed = apply_promote_tech_lead_finding(
            _promote_action(observations=2),
            target=target,
            authority=authority,
            now_iso="t1",
        )
        assert not crashed.success
        assert len(target.filed) == 1
        assert (
            authority.load_pending_promotion(
                signature="anchor-close"
            ).body_observations
            == 2
        )

        # 2. The case file accrues a third observation, so the next tick plans a
        #    count-3 filing action, which recovers the same remote issue.
        recovered = apply_promote_tech_lead_finding(
            _promote_action(observations=3),
            target=target,
            authority=authority,
            now_iso="t2",
        )

        assert recovered.success
        assert len(target.filed) == 1  # no second issue
        assert recovered.details["recovered"] is True
        promotion = authority.load_promotion(signature="anchor-close")
        # The watermark is the count the FILED BODY documents, not the action's.
        assert promotion.reported_observations == 2

        # 3. ...so the count-3 evidence comment is still planned, and only the
        #    successful target comment advances the watermark.
        [update] = select_promotion_updates(
            evidence=[_evidence("anchor-close", observations=3)],
            promotions=[promotion],
        )
        assert update.observation_count == 3
        [report] = plan_promotion_updates(_config(), updates=(update,))
        assert apply_report_promoted_finding_evidence(
            report, target=target, authority=authority
        ).success
        assert len(target.comments) == 1
        assert (
            authority.load_promotion(signature="anchor-close").reported_observations
            == 3
        )

    def test_a_fresh_filing_seeds_the_watermark_from_its_own_body(self):
        target = InMemoryPromotionTargetHost()
        authority = InMemoryTechLeadAuthorityStore()

        result = apply_promote_tech_lead_finding(
            _promote_action(observations=4),
            target=target,
            authority=authority,
            now_iso="t",
        )

        assert result.success and result.details["recovered"] is False
        assert (
            authority.load_promotion(signature="anchor-close").reported_observations
            == 4
        )

class TestRouteChangedWhileFilingWasInFlight:
    """#6957 round-4 review F12: a re-routed signature must still settle.

    ``tech_lead.findings.route`` is ordinary user-editable YAML and the pending
    intent is deliberately durable across restarts, so an operator re-pointing
    an area between ticks reaches this WITHOUT the signature's area changing.
    Refusing to act stranded the signature forever: every later tick rebuilt the
    same action from the new route, hit the same mismatch, and failed again.
    """

    OTHER = "someone/else"

    @staticmethod
    def _crashed_filing(target, *, repo, observations=2):
        """Leave a durable intent for a filing whose ledger write died."""

        class FailFirstLedgerWrite(InMemoryTechLeadAuthorityStore):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next_record = True

            def record_promotion(self, *, promotion):
                if self.fail_next_record:
                    self.fail_next_record = False
                    raise RuntimeError("process died before the ledger commit")
                super().record_promotion(promotion=promotion)

        authority = FailFirstLedgerWrite()
        action = _promote_action(observations=observations)
        crashed = apply_promote_tech_lead_finding(
            _rerouted(action, repo),
            target=target,
            authority=authority,
            now_iso="t1",
        )
        assert not crashed.success
        pending = authority.load_pending_promotion(signature=action.signature)
        assert pending is not None and pending.target_repo == repo
        return authority

    def test_the_old_target_stays_authoritative_when_its_issue_exists(self):
        """One signature promotes exactly once: the filed issue IS the promotion."""
        target = InMemoryPromotionTargetHost()
        authority = self._crashed_filing(target, repo=self.OTHER)
        assert len(target.filed) == 1

        # The operator re-points the area; the next tick plans against the new
        # route while the old route's issue already exists.
        result = apply_promote_tech_lead_finding(
            _promote_action(observations=3),
            target=target,
            authority=authority,
            now_iso="t2",
        )

        assert result.success
        assert len(target.filed) == 1  # no second issue, in either repo
        assert result.details["recovered"] is True
        assert result.details["target_repo"] == self.OTHER
        promotion = authority.load_promotion(signature="anchor-close")
        assert promotion.target_repo == self.OTHER
        # The intent's watermark still rules, so later evidence is not lost.
        assert promotion.reported_observations == 2
        assert authority.load_pending_promotion(signature="anchor-close") is None

    def test_a_proven_absent_old_issue_lets_the_new_route_take_over(self):
        """That create never happened, so nothing is orphaned."""
        target = InMemoryPromotionTargetHost()
        target.file_error = RuntimeError("the old route rejected it")
        authority = InMemoryTechLeadAuthorityStore()
        assert not apply_promote_tech_lead_finding(
            _rerouted(_promote_action(), self.OTHER),
            target=target,
            authority=authority,
            now_iso="t1",
        ).success
        assert target.filed == []
        assert authority.load_pending_promotion(signature="anchor-close") is not None

        target.file_error = None
        result = apply_promote_tech_lead_finding(
            _promote_action(),
            target=target,
            authority=authority,
            now_iso="t2",
        )

        assert result.success
        # Filed exactly once, in the CURRENT route.
        assert [repo for repo, *_ in target.filed] == [UPSTREAM]
        assert result.details["recovered"] is False
        assert result.details["target_repo"] == UPSTREAM
        assert (
            authority.load_promotion(signature="anchor-close").target_repo == UPSTREAM
        )
        # ...and the stale intent is retired, not left to strand the next tick.
        assert authority.load_pending_promotion(signature="anchor-close") is None

    def test_an_unreadable_old_target_creates_nothing_and_retries_later(self):
        """"Unknown" is never "absent": that is what files a second issue."""
        target = InMemoryPromotionTargetHost()
        authority = self._crashed_filing(target, repo=self.OTHER)
        target.find_error = RuntimeError("the old repo is unreachable")

        result = apply_promote_tech_lead_finding(
            _promote_action(),
            target=target,
            authority=authority,
            now_iso="t2",
        )

        assert not result.success
        assert len(target.filed) == 1  # nothing new was created anywhere
        assert authority.load_promotion(signature="anchor-close") is None
        # The intent survives, so a later tick can still settle it.
        assert authority.load_pending_promotion(signature="anchor-close") is not None

        target.find_error = None
        recovered = apply_promote_tech_lead_finding(
            _promote_action(),
            target=target,
            authority=authority,
            now_iso="t3",
        )

        assert recovered.success
        assert len(target.filed) == 1
        assert recovered.details["target_repo"] == self.OTHER


class TestFilingRecoveryIsReportedHonestly:
    """``recovered`` describes the REMOTE outcome, not watermark drift (A5)."""

    def test_the_same_action_recovering_reports_recovered(self):
        target = InMemoryPromotionTargetHost()
        authority = InMemoryTechLeadAuthorityStore()
        action = _promote_action(observations=2)
        assert apply_promote_tech_lead_finding(
            action, target=target, authority=authority, now_iso="t1"
        ).success
        # Same action, same watermark: the old inference said "not recovered".
        authority.discard_pending_promotion(signature=action.signature)

        target_only = InMemoryTechLeadAuthorityStore()
        again = apply_promote_tech_lead_finding(
            action, target=target, authority=target_only, now_iso="t2"
        )

        assert again.success
        assert again.details["recovered"] is True

    def test_a_fresh_create_reports_not_recovered_even_after_a_stale_intent(self):
        target = InMemoryPromotionTargetHost()
        target.file_error = RuntimeError("transient")
        authority = InMemoryTechLeadAuthorityStore()
        assert not apply_promote_tech_lead_finding(
            _promote_action(observations=2),
            target=target,
            authority=authority,
            now_iso="t1",
        ).success

        # An older intent exists, but this call really does create the first
        # remote issue — the old inference would have said "recovered".
        target.file_error = None
        result = apply_promote_tech_lead_finding(
            _promote_action(observations=3),
            target=target,
            authority=authority,
            now_iso="t2",
        )

        assert result.success
        assert result.details["recovered"] is False


class TestPromotionMarker:
    def test_promotion_action_carries_stable_marker_in_remote_body(self):
        action = _promote_action("signature-with--html-risk")

        expected = promotion_issue_marker(
            source_repo="porchpin/porchpin",
            signature="signature-with--html-risk",
        )
        assert action.idempotency_marker == expected
        assert expected in action.body
        assert expected == promotion_issue_marker(
            source_repo="PORCHPIN/PORCHPIN",
            signature="signature-with--html-risk",
        )

    def test_unwired_promotion_target_fails_loudly(self):
        result = apply_promote_tech_lead_finding(
            _promote_action(),
            target=None,
            authority=InMemoryTechLeadAuthorityStore(),
            now_iso="t",
        )
        assert not result.success


class TestLedgerRestartResume:
    def test_ledger_survives_restart_and_still_blocks_refiling(self, tmp_path):
        """The promotion ledger is durable: a restart must not re-file work."""
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )

        db = tmp_path / "authority.sqlite"
        store = SqliteTechLeadAuthorityStore(db)
        store.record_pattern(
            signature="anchor-close",
            issue_number=65,
            observation_id="run-1:sess:a1",
            fix_class="code",
            area="cp",
        )
        store.note_pattern_observation(
            signature="anchor-close", observation_id="run-2:sess:a1"
        )
        store.record_promotion(promotion=_promotion("anchor-close"))

        reopened = SqliteTechLeadAuthorityStore(db)
        evidence = reopened.list_pattern_evidence()
        assert [
            (row.signature, row.observation_count, row.fix_class) for row in evidence
        ] == [("anchor-close", 2, "code")]
        assert (
            select_promotable_findings(
                _config(), evidence=evidence, promotions=reopened.list_promotions()
            )
            == ()
        )

    def test_a_second_promotion_for_one_signature_is_a_conflict(self, tmp_path):
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )

        store = SqliteTechLeadAuthorityStore(tmp_path / "authority.sqlite")
        store.record_promotion(promotion=_promotion("sig", issue=1))
        store.record_promotion(promotion=_promotion("sig", issue=1))  # idempotent

        with pytest.raises(TechLeadPromotionConflictError):
            store.record_promotion(promotion=_promotion("sig", issue=2))

    def test_observation_count_is_orchestrator_owned(self, tmp_path):
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )

        store = SqliteTechLeadAuthorityStore(tmp_path / "authority.sqlite")
        store.record_pattern(
            signature="sig", issue_number=7, observation_id="run-1:sess:a1"
        )
        store.note_pattern_observation(
            signature="sig", observation_id="run-2:sess:a1", fix_class="code"
        )
        store.note_pattern_observation(signature="sig", observation_id="run-3:sess:a1")

        [row] = store.list_pattern_evidence()
        assert row.observation_count == 3
        # A later observation can classify what the first one left unclassified.
        assert row.fix_class == "code"

    def test_reported_observation_watermark_survives_restart(self, tmp_path):
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )

        db = tmp_path / "authority.sqlite"
        store = SqliteTechLeadAuthorityStore(db)
        store.record_promotion(promotion=_promotion("sig", reported=2))
        store.note_promotion_reported(signature="sig", observations=4)

        reopened = SqliteTechLeadAuthorityStore(db)

        assert reopened.load_promotion(signature="sig").reported_observations == 4


# ---------------------------------------------------------------------------
# Later evidence on the one promoted issue
# ---------------------------------------------------------------------------


class TestPromotedEvidenceReporting:
    def test_new_evidence_selects_one_update(self):
        updates = select_promotion_updates(
            evidence=[_evidence("sig", observations=4)],
            promotions=[_promotion("sig", reported=2)],
        )

        assert updates == (
            PromotionUpdate(
                promotion=_promotion("sig", reported=2), observation_count=4
            ),
        )
        assert updates[0].new_observations == 2

    def test_already_reported_evidence_selects_no_update(self):
        assert (
            select_promotion_updates(
                evidence=[_evidence("sig", observations=4)],
                promotions=[_promotion("sig", reported=4)],
            )
            == ()
        )

    @pytest.mark.parametrize(
        "state", [PROMOTION_STATE_DECLINED, PROMOTION_STATE_SHIPPED]
    )
    def test_terminal_promotion_is_not_revived_by_later_evidence(self, state):
        assert (
            select_promotion_updates(
                evidence=[_evidence("sig", observations=9)],
                promotions=[_promotion("sig", state=state, reported=2)],
            )
            == ()
        )

    def test_target_settling_this_tick_is_not_also_updated(self):
        assert (
            select_promotion_updates(
                evidence=[_evidence("sig", observations=4)],
                promotions=[_promotion("sig", reported=2)],
                settling_signatures=frozenset({"sig"}),
            )
            == ()
        )

    def test_reporting_comments_then_advances_the_watermark(self):
        authority = InMemoryTechLeadAuthorityStore()
        authority.record_promotion(promotion=_promotion("sig", reported=2))
        target = InMemoryPromotionTargetHost()
        [action] = plan_promotion_updates(
            _config(),
            updates=(
                PromotionUpdate(
                    promotion=_promotion("sig", reported=2), observation_count=4
                ),
            ),
        )

        result = apply_report_promoted_finding_evidence(
            action, target=target, authority=authority
        )

        assert result.success
        assert target.comments == [
            (UPSTREAM, 500, action.comment),
        ]
        assert "now 4 observations" in action.comment
        assert authority.load_promotion(signature="sig").reported_observations == 4

    def test_stale_retry_does_not_repeat_the_comment(self):
        authority = InMemoryTechLeadAuthorityStore()
        authority.record_promotion(promotion=_promotion("sig", reported=4))
        target = InMemoryPromotionTargetHost()
        action = ReportPromotedFindingEvidenceAction(
            signature="sig",
            case_file_issue_number=65,
            target_repo=UPSTREAM,
            target_issue_number=500,
            comment="later evidence",
            observation_count=4,
        )

        result = apply_report_promoted_finding_evidence(
            action, target=target, authority=authority
        )

        assert result.success
        assert result.details["deduplicated"] is True
        assert target.comments == []

    def test_failed_comment_does_not_advance_the_watermark(self):
        class FailingTarget(InMemoryPromotionTargetHost):
            def add_comment(self, *, repo, issue_number, body):
                raise RuntimeError("target unavailable")

        authority = InMemoryTechLeadAuthorityStore()
        authority.record_promotion(promotion=_promotion("sig", reported=2))
        [action] = plan_promotion_updates(
            _config(),
            updates=(
                PromotionUpdate(
                    promotion=_promotion("sig", reported=2), observation_count=3
                ),
            ),
        )

        result = apply_report_promoted_finding_evidence(
            action, target=FailingTarget(), authority=authority
        )

        assert not result.success
        assert authority.load_promotion(signature="sig").reported_observations == 2


# ---------------------------------------------------------------------------
# Loop closure
# ---------------------------------------------------------------------------


class TestLoopClosure:
    def test_open_promotion_produces_no_settlement_fact(self):
        target = InMemoryPromotionTargetHost()
        assert classify_promotion_outcomes([_promotion("sig")], target=target) == ()

    def test_closed_with_a_merged_pr_is_shipped(self):
        target = InMemoryPromotionTargetHost()
        target.outcomes[(UPSTREAM, 500)] = PromotedIssueOutcome(
            state="closed", merged_pr_url="https://github.com/x/y/pull/6956"
        )

        [settled] = classify_promotion_outcomes([_promotion("sig")], target=target)

        assert settled.shipped is True
        assert settled.merged_pr_url.endswith("/6956")

    def test_closed_without_a_merged_pr_is_declined(self):
        target = InMemoryPromotionTargetHost()
        target.outcomes[(UPSTREAM, 500)] = PromotedIssueOutcome(state="closed")

        [settled] = classify_promotion_outcomes([_promotion("sig")], target=target)

        assert settled.shipped is False

    def test_unreachable_target_leaves_the_promotion_in_flight(self):
        """A temporarily unreachable repo must never read as a decline."""

        class Unreachable(InMemoryPromotionTargetHost):
            def read_outcome(self, *, repo, issue_number):
                raise RuntimeError("connection reset")

        assert (
            classify_promotion_outcomes([_promotion("sig")], target=Unreachable()) == ()
        )

    def test_unknown_outcome_leaves_the_promotion_in_flight(self):
        target = InMemoryPromotionTargetHost()
        target.outcomes[(UPSTREAM, 500)] = None
        assert classify_promotion_outcomes([_promotion("sig")], target=target) == ()

    def test_terminal_promotions_are_not_re_read(self):
        target = InMemoryPromotionTargetHost()
        settled = classify_promotion_outcomes(
            [_promotion("sig", state=PROMOTION_STATE_SHIPPED)], target=target
        )
        assert settled == ()


class TestSettlement:
    def _settle(self, *, shipped: bool, merged_pr_url: str = ""):
        target = InMemoryPromotionTargetHost()
        if shipped:
            target.outcomes[(UPSTREAM, 500)] = PromotedIssueOutcome(
                state="closed", merged_pr_url=merged_pr_url
            )
        else:
            target.outcomes[(UPSTREAM, 500)] = PromotedIssueOutcome(state="closed")
        authority = InMemoryTechLeadAuthorityStore()
        authority.record_promotion(promotion=_promotion("anchor-close"))
        facts = classify_promotion_outcomes(authority.list_promotions(), target=target)
        [action] = plan_promotion_settlements(facts)
        assert isinstance(action, SettleTechLeadPromotionAction)
        repository_host = Mock()
        result = apply_settle_tech_lead_promotion(
            action, repository_host=repository_host, authority=authority
        )
        return result, repository_host, authority

    def test_shipped_records_the_fix_and_closes_the_case_file(self):
        result, repository_host, authority = self._settle(
            shipped=True, merged_pr_url="https://github.com/x/y/pull/6956"
        )

        assert result.success
        # tech_lead_shipped_fixes finally gets a row.
        [fix] = authority.list_recent_shipped_fixes(limit=5)
        assert fix.pr_url.endswith("/6956")
        repository_host.update_issue_state.assert_called_once_with(65, "closed")
        comment = repository_host.add_comment.call_args[0][1]
        assert f"{UPSTREAM}#500" in comment
        assert authority.load_promotion(signature="anchor-close").state == (
            PROMOTION_STATE_SHIPPED
        )

    def test_declined_leaves_the_case_file_open_and_blocks_refiling(self):
        result, repository_host, authority = self._settle(shipped=False)

        assert result.success
        repository_host.update_issue_state.assert_not_called()
        assert authority.load_promotion(signature="anchor-close").state == (
            PROMOTION_STATE_DECLINED
        )
        assert authority.list_recent_shipped_fixes(limit=5) == ()
        assert (
            select_promotable_findings(
                _config(),
                evidence=[_evidence("anchor-close", observations=99)],
                promotions=authority.list_promotions(),
            )
            == ()
        )

    def test_shipped_settlement_requires_the_merged_pr_evidence(self):
        with pytest.raises(ValueError, match="merged"):
            SettleTechLeadPromotionAction(
                signature="sig",
                case_file_issue_number=65,
                target_repo=UPSTREAM,
                target_issue_number=500,
                shipped=True,
            )


# ---------------------------------------------------------------------------
# Fact gathering + planning integration
# ---------------------------------------------------------------------------


def _fact_gatherer(config: Config, authority, target=None):

    from issue_orchestrator.control.fact_gatherer import FactGatherer

    repository_host = MagicMock()
    repository_host.list_issues.return_value = []
    repository_host.get_prs_with_label.return_value = []
    return FactGatherer(
        config=config,
        repository_host=repository_host,
        tech_lead_authority=authority,
        promotion_target=target,
    )


def _state():
    from issue_orchestrator.domain.models import OrchestratorState

    return OrchestratorState()


class TestFactGatheringAndPlanning:
    def test_promotion_arms_independently_of_the_batch_and_health_triggers(self):
        """A promotable finding must reach the planner on a tick where nothing
        else about tech_lead is armed — otherwise the lane only ever fires when
        some unrelated trigger happens to be due."""
        config = _config()
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_threshold = 0  # batch disabled
        authority = InMemoryTechLeadAuthorityStore()
        _record_case_file(
            authority, signature="anchor-close", issue_number=65, observations=2
        )

        facts = _fact_gatherer(config, authority).gather_tech_lead_facts(_state())

        assert facts is not None
        assert [item.evidence.signature for item in facts.promotable_findings] == [
            "anchor-close"
        ]

    def test_disabled_lane_gathers_no_promotion_facts_and_makes_no_reads(self):
        config = _config(promote="off")
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_threshold = 0
        authority = InMemoryTechLeadAuthorityStore()
        _record_case_file(authority, signature="sig", issue_number=65, observations=9)
        target = InMemoryPromotionTargetHost()
        authority.record_promotion(promotion=_promotion("other"))

        facts = _fact_gatherer(config, authority, target).gather_tech_lead_facts(
            _state()
        )

        # Nothing armed: no facts at all, and no cross-repo read was attempted.
        assert facts is None

    def test_gathered_facts_become_filing_update_and_settlement_actions(self):
        from issue_orchestrator.control.tech_lead_finding_promotion import (
            plan_finding_promotion_actions,
        )
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.domain.tech_lead_findings import SettledPromotion

        actions = plan_finding_promotion_actions(
            _config(),
            TechLeadFacts(
                promotable_findings=(
                    PromotableFinding(
                        evidence=_evidence("anchor-close"), target_repo=UPSTREAM
                    ),
                ),
                promotion_updates=(
                    PromotionUpdate(
                        promotion=_promotion("repeat", reported=2),
                        observation_count=3,
                    ),
                ),
                settled_promotions=(
                    SettledPromotion(
                        promotion=_promotion("older"),
                        shipped=True,
                        merged_pr_url="https://github.com/x/y/pull/1",
                    ),
                ),
            ),
        )

        assert [type(action) for action in actions] == [
            PromoteTechLeadFindingAction,
            ReportPromotedFindingEvidenceAction,
            SettleTechLeadPromotionAction,
        ]

    def test_later_evidence_arms_facts_without_other_tech_lead_triggers(self):
        config = _config()
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_threshold = 0
        authority = InMemoryTechLeadAuthorityStore()
        _record_case_file(authority, signature="sig", issue_number=65, observations=3)
        authority.record_promotion(promotion=_promotion("sig", reported=2))

        facts = _fact_gatherer(
            config, authority, InMemoryPromotionTargetHost()
        ).gather_tech_lead_facts(_state())

        assert facts is not None
        assert facts.promotion_updates == (
            PromotionUpdate(
                promotion=_promotion("sig", reported=2), observation_count=3
            ),
        )

    def test_planning_is_a_no_op_without_tech_lead_facts(self):
        from issue_orchestrator.control.tech_lead_finding_promotion import (
            plan_finding_promotion_actions,
        )

        assert plan_finding_promotion_actions(_config(), None) == []


class TestPromotedIssueIsDiscoverableByTheScheduler:
    """Producer -> scheduler proof for #6957 review F2.

    ``LabelManager.is_blocking_any`` only proves the gate blocks; it cannot
    prove the issue is DISCOVERABLE, because discovery queries the scope label
    too. These tests run the composed labels through the real fetch path.
    """

    @staticmethod
    def _discover(config, labels: tuple[str, ...]) -> list[int]:
        from issue_orchestrator.control.fact_gatherer import FactGatherer
        from issue_orchestrator.control.github_workflow import GitHubWorkflow
        from issue_orchestrator.domain.models import Issue
        from issue_orchestrator.events import EventContext

        promoted = Issue(
            number=501, title="promoted", labels=list(labels), repo=config.repo
        )

        class LabelFilteringHost(MagicMock):
            def list_issues(self, *, labels, **_kwargs):
                # GitHub's label filter is an AND across every requested label.
                requested = {name.casefold() for name in labels}
                carried = {name.casefold() for name in promoted.labels}
                return [promoted] if requested <= carried else []

        host = LabelFilteringHost()
        workflow = GitHubWorkflow(
            config=config,
            events=MagicMock(),
            repository_host=host,
            fact_gatherer=FactGatherer(config=config, repository_host=host),
            pr_scanner=MagicMock(),
            label_sync=None,
            event_context=EventContext(run_id="r", tick_id=0),
        )
        return [issue.number for issue in workflow.fetch_all_issues(None)]

    def test_gate_removal_alone_makes_a_gated_self_route_discoverable(self):
        config = _config()
        config.filtering.label = "io-scope"
        gated = promotion_issue_labels(config, area="completion-pipeline")

        # Gated: present in the repo but blocked, so the scheduler must not run
        # it even though discovery can see it.
        from issue_orchestrator.control.label_manager import LabelManager

        assert LabelManager(config).is_blocking_any(list(gated))

        ungated = tuple(
            name for name in gated if name != PROPOSED_TECH_LEAD_LABEL
        )
        assert not LabelManager(config).is_blocking_any(list(ungated))
        # Removing the gate is the operator's WHOLE approval: nothing else is
        # missing for the scheduler's own discovery query to return it.
        assert self._discover(config, ungated) == [501]

    def test_auto_self_route_is_discoverable_immediately(self):
        config = _config(promote="auto")
        config.filtering.label = "io-scope"

        labels = promotion_issue_labels(config, area="")

        assert self._discover(config, labels) == [501]

    def test_a_promotion_without_the_scope_label_is_invisible(self):
        """The regression itself: the pre-fix label set is never discovered."""
        config = _config(promote="auto")
        config.filtering.label = "io-scope"

        assert self._discover(config, ("agent:backend", "area:cp")) == []


class TestLoopClosureReadBudget:
    """#6957 review F5: the configured cap must bound the reads, not just the
    work in flight. The durable ledger outlives the setting."""

    @staticmethod
    def _counting_target():
        class Counting(InMemoryPromotionTargetHost):
            def __init__(self):
                super().__init__()
                self.reads: list[int] = []

            def read_outcome(self, *, repo: str, issue_number: int):
                self.reads.append(issue_number)
                return super().read_outcome(repo=repo, issue_number=issue_number)

        return Counting()

    def _authority_with(self, open_promotions: int, *, repo: str = UPSTREAM):
        authority = InMemoryTechLeadAuthorityStore()
        for index in range(open_promotions):
            authority.record_promotion(
                promotion=_promotion(f"sig-{index:02d}", repo=repo, issue=500 + index)
            )
        return authority

    def test_reads_are_capped_per_tick_when_the_cap_is_lowered(self):
        """A cohort filed under max_open_promoted=5, then the cap drops to 2."""
        config = _config(max_open_promoted=2)
        authority = self._authority_with(5)
        target = self._counting_target()

        gather_finding_promotion_facts(
            config,
            authority=authority,
            target=target,
            read_budget=PromotionReadBudget(),
        )

        assert len(target.reads) == 2

    def test_every_durable_promotion_is_eventually_polled(self):
        """Rotation, not a fixed prefix: rows beyond the window cannot starve."""
        config = _config(max_open_promoted=2)
        authority = self._authority_with(5)
        target = self._counting_target()
        budget = PromotionReadBudget()

        for _ in range(3):  # ceil(5 / 2) ticks covers the whole ledger
            gather_finding_promotion_facts(
                config, authority=authority, target=target, read_budget=budget
            )

        assert sorted(set(target.reads)) == [500, 501, 502, 503, 504]
        assert len(target.reads) == 6  # still 2 per tick, never 5

    def test_the_budget_is_per_target_repository(self):
        config = _config(
            max_open_promoted=1,
            route=_route(upstream=UPSTREAM, default="self"),
        )
        authority = InMemoryTechLeadAuthorityStore()
        authority.record_promotion(promotion=_promotion("a", repo=UPSTREAM, issue=1))
        authority.record_promotion(promotion=_promotion("b", repo=UPSTREAM, issue=2))
        authority.record_promotion(
            promotion=_promotion("c", repo="porchpin/porchpin", issue=3)
        )
        target = self._counting_target()

        gather_finding_promotion_facts(
            config,
            authority=authority,
            target=target,
            read_budget=PromotionReadBudget(),
        )

        # One read for each of the two targets, not one read overall.
        assert len(target.reads) == 2
        assert 3 in target.reads

    def test_terminal_promotions_are_never_read(self):
        config = _config(max_open_promoted=5)
        authority = InMemoryTechLeadAuthorityStore()
        authority.record_promotion(
            promotion=_promotion("shipped", issue=1, state=PROMOTION_STATE_SHIPPED)
        )
        authority.record_promotion(
            promotion=_promotion("declined", issue=2, state=PROMOTION_STATE_DECLINED)
        )
        target = self._counting_target()

        gather_finding_promotion_facts(
            config,
            authority=authority,
            target=target,
            read_budget=PromotionReadBudget(),
        )

        assert target.reads == []


class TestObservationCountBoundary:
    """The durable count promotion reads is written by the case-file owner."""

    def test_repeat_observation_comments_and_increments(self):
        from issue_orchestrator.control.tech_lead_case_files import (
            apply_append_pattern_observation,
        )

        authority = InMemoryTechLeadAuthorityStore()
        _record_case_file(authority, signature="sig", issue_number=65, fix_class="")
        repository_host = Mock()

        result = apply_append_pattern_observation(
            _append_action("sig", "obs-2", comment="observed again", fix_class="code"),
            repository_host=repository_host,
            authority=authority,
        )

        assert result.success
        repository_host.add_comment.assert_called_once_with(65, "observed again")
        [row] = authority.list_pattern_evidence()
        assert row.observation_count == 2
        assert row.fix_class == "code"

    def test_observation_for_an_unknown_signature_fails_loudly(self):
        from issue_orchestrator.control.tech_lead_case_files import (
            apply_append_pattern_observation,
        )

        result = apply_append_pattern_observation(
            _append_action("never-recorded", "obs-1"),
            repository_host=Mock(),
            authority=InMemoryTechLeadAuthorityStore(),
        )

        assert not result.success
