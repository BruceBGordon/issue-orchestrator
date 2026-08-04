"""One activation/readiness policy for the promotion lane (#6957 R2 F9/A4).

Configuration validation, doctor, and fact gathering each used to encode their
own reachability predicate. The result was a configuration that passed
validation AND doctor and then raised on a normal tick — or, the other way
round, doctor skipping its cross-repo probes while the runtime promoted anyway.

These tests pin the shared decision and then prove each of the three boundaries
consumes it, rather than re-deriving it.
"""

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.tech_lead_finding_promotion import (
    PromotionReadBudget,
    gather_finding_promotion_facts,
)
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.tech_lead_findings import PromotedFinding
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import PromotionRouteTarget
from issue_orchestrator.infra.doctor.checks.tech_lead import (
    check_tech_lead_finding_routes,
)
from issue_orchestrator.infra.validators.review import ReviewWorkflowValidator
from issue_orchestrator.infra.tech_lead_promotion_activation import (
    promotion_lane_readiness,
)
from issue_orchestrator.ports.promotion_target import InMemoryPromotionTargetHost
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
)

UPSTREAM = "issue-orchestrator/issue-orchestrator"


def _config(**findings_overrides) -> Config:
    config = Config()
    config.repo = "porchpin/porchpin"
    # Real AgentConfigs, not mocks: these tests run the review-workflow
    # validator, which reads agent fields.
    config.agents = {
        "agent:backend": AgentConfig(prompt_path="prompts/backend.md"),
        "agent:tech-lead": AgentConfig(prompt_path="prompts/tech-lead.md"),
    }
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_follow_up_agent = "agent:backend"
    for key, value in findings_overrides.items():
        setattr(config.tech_lead.findings, key, value)
    return config


def _authority_with_work() -> InMemoryTechLeadAuthorityStore:
    """Durable rows that WOULD produce promotion work on an active lane."""
    authority = InMemoryTechLeadAuthorityStore()
    authority.record_pattern(
        signature="ready-to-promote",
        issue_number=65,
        observation_id="r1:s:A1",
        fix_class="code",
    )
    authority.note_pattern_observation(
        signature="ready-to-promote", observation_id="r2:s:A1"
    )
    authority.record_promotion(
        promotion=PromotedFinding(
            signature="already-promoted",
            case_file_issue_number=66,
            target_repo=UPSTREAM,
            target_issue_number=500,
        )
    )
    return authority


class TestActivation:
    def test_a_fully_configured_lane_is_active_and_ready(self):
        readiness = promotion_lane_readiness(_config())

        assert readiness.active and readiness.ready
        assert readiness.inactive_reason == ""

    def test_promote_off_deactivates_the_lane(self):
        readiness = promotion_lane_readiness(_config(promote="off"))

        assert not readiness.active
        assert "off" in readiness.inactive_reason

    def test_no_tech_lead_agent_deactivates_the_lane(self):
        config = _config()
        config.tech_lead_review_agent = None

        readiness = promotion_lane_readiness(config)

        assert not readiness.active
        assert "tech lead agent" in readiness.inactive_reason
        # Deactivation is not a configuration ERROR — switching the agent off is
        # an operator decision, and the durable rows are kept.
        assert readiness.problems == ()

    def test_no_repo_deactivates_the_lane(self):
        config = _config()
        config.repo = None

        assert not promotion_lane_readiness(config).active

    def test_probe_targets_are_the_distinct_foreign_repos(self):
        config = _config(
            route={
                "a": PromotionRouteTarget(repo=UPSTREAM),
                "b": PromotionRouteTarget(repo=UPSTREAM),
                "default": PromotionRouteTarget(repo="self"),
            }
        )

        assert promotion_lane_readiness(config).probe_targets == (UPSTREAM,)


class TestReadinessProblems:
    def test_a_self_route_without_a_follow_up_agent_is_a_startup_error(self):
        """The failure this replaces was a raise on a normal tick (F9)."""
        config = _config()
        config.tech_lead_follow_up_agent = None

        readiness = promotion_lane_readiness(config)

        assert readiness.active and not readiness.ready
        assert any("tech_lead_follow_up_agent" in p for p in readiness.problems)
        # ...and the SAME strings are what startup validation reports, because
        # the validator consumes this owner rather than its own predicate.
        assert set(readiness.problems) <= set(
            ReviewWorkflowValidator().validate(config)
        )

    def test_a_foreign_shorthand_route_also_needs_the_follow_up_agent(self):
        config = _config(
            route={"default": PromotionRouteTarget(repo=UPSTREAM)}
        )
        config.tech_lead_follow_up_agent = None

        assert any(
            "tech_lead_follow_up_agent" in p
            for p in promotion_lane_readiness(config).problems
        )

    def test_a_fully_declared_foreign_route_needs_nothing_from_this_repo(self):
        config = _config(
            route={
                "default": PromotionRouteTarget(
                    repo=UPSTREAM,
                    scope_label="upstream-scope",
                    agent_label="agent:platform",
                )
            }
        )
        config.tech_lead_follow_up_agent = None

        assert promotion_lane_readiness(config).ready

    def test_an_inactive_lane_reports_no_problems(self):
        """Switching the lane off is always a way out of a misconfiguration."""
        config = _config(promote="off")
        config.tech_lead_follow_up_agent = None

        assert promotion_lane_readiness(config).problems == ()
        assert not any(
            "tech_lead.findings" in error
            for error in ReviewWorkflowValidator().validate(config)
        )


class TestBoundariesConsumeTheSameDecision:
    """Validation, doctor, and fact gathering must agree — that was F9."""

    @staticmethod
    def _gather(config, authority, target):
        return gather_finding_promotion_facts(
            config,
            authority=authority,
            target=target,
            read_budget=PromotionReadBudget(),
        )

    def test_no_tech_lead_agent_makes_the_lane_a_zero_read_no_op(self):
        """Durable promotable AND open rows exist; the lane must still not run."""
        config = _config()
        config.tech_lead_review_agent = None
        authority = _authority_with_work()

        class Exploding(InMemoryPromotionTargetHost):
            def read_outcome(self, *, repo: str, issue_number: int):
                raise AssertionError("an inactive lane must make no cross-repo read")

        promotable, updates, settled = self._gather(config, authority, Exploding())

        assert (promotable, updates, settled) == ((), (), ())

    def test_no_tech_lead_agent_makes_doctor_skip_its_probes_too(self):
        config = _config(route={"cp": PromotionRouteTarget(repo=UPSTREAM)})
        config.tech_lead_review_agent = None

        class Exploding(InMemoryPromotionTargetHost):
            def check_writable(self, *, repo: str) -> str | None:
                raise AssertionError("an inactive lane must make no cross-repo read")

        assert check_tech_lead_finding_routes(config, target_host=Exploding()) == []

    def test_an_active_lane_is_probed_by_doctor_and_runs_at_tick_time(self):
        config = _config(route={"cp": PromotionRouteTarget(repo=UPSTREAM)})
        authority = _authority_with_work()

        checks = check_tech_lead_finding_routes(
            config, target_host=InMemoryPromotionTargetHost(writable=True)
        )
        promotable, _, _ = self._gather(
            config, authority, InMemoryPromotionTargetHost()
        )

        assert [check.status for check in checks] == ["ok"]
        assert [item.evidence.signature for item in promotable] == [
            "ready-to-promote"
        ]

    def test_an_unready_lane_refuses_to_plan_instead_of_raising(self, caplog):
        """Validation rejects this config; if it runs anyway, no-op loudly."""
        config = _config()
        config.tech_lead_follow_up_agent = None
        authority = _authority_with_work()

        with caplog.at_level("ERROR"):
            facts = self._gather(config, authority, InMemoryPromotionTargetHost())

        assert facts == ((), (), ())
        assert "not ready" in caplog.text

    @pytest.mark.parametrize("agent", (None, "agent:tech-lead"))
    def test_the_fact_gatherer_never_re_derives_the_predicate(self, agent):
        """The tick seam must agree with promotion_lane_readiness, always."""
        config = _config()
        config.tech_lead_review_agent = agent
        config.tech_lead_review_threshold = 0
        authority = _authority_with_work()
        repository_host = MagicMock()
        repository_host.list_issues.return_value = []
        repository_host.get_prs_with_label.return_value = []
        gatherer = FactGatherer(
            config=config,
            repository_host=repository_host,
            tech_lead_authority=authority,
            promotion_target=InMemoryPromotionTargetHost(),
        )

        from issue_orchestrator.domain.models import OrchestratorState

        facts = gatherer.gather_tech_lead_facts(OrchestratorState())

        expected_active = promotion_lane_readiness(config).ready
        has_promotion_facts = facts is not None and bool(facts.promotable_findings)
        assert has_promotion_facts is expected_active
