"""Doctor: promotion routes must be FILEABLE at startup (#6957).

A route target the token cannot file this route's promotions into has to fail
LOUDLY at startup. Discovering it at promotion time means losing the actuation
on the exact tick a pattern finally crossed its evidence threshold — and
"writable" was the wrong question: filing provisions any missing label before it
creates the issue, so the probe has to carry the label contract too (round-6
review F2/A1).
"""

from unittest.mock import patch

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.tech_lead_findings import PromotionFilingContract
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import PromotionRouteTarget
from issue_orchestrator.infra.doctor.checks.tech_lead import (
    check_tech_lead_finding_routes,
)
from issue_orchestrator.ports.promotion_target import InMemoryPromotionTargetHost

UPSTREAM = "issue-orchestrator/issue-orchestrator"


def _route(**entries) -> dict[str, PromotionRouteTarget]:
    return {area: PromotionRouteTarget(repo=repo) for area, repo in entries.items()}


def _config(**findings_overrides) -> Config:
    config = Config()
    config.repo = "porchpin/porchpin"
    config.agents = {"agent:backend": AgentConfig(prompt_path="prompts/backend.md")}
    config.tech_lead_review_agent = "agent:tech-lead"
    # Foreign routes that declare no agent_label inherit this one, so a
    # resolvable route table needs it configured.
    config.tech_lead_follow_up_agent = "agent:backend"
    for key, value in findings_overrides.items():
        setattr(config.tech_lead.findings, key, value)
    return config


def test_fileable_route_targets_pass():
    checks = check_tech_lead_finding_routes(
        _config(route=_route(cp=UPSTREAM, default="self")),
        target_host=InMemoryPromotionTargetHost(writable=True),
    )

    assert [check.status for check in checks] == ["ok"]
    assert UPSTREAM in checks[0].detail


def test_an_unfileable_route_target_is_an_error():
    checks = check_tech_lead_finding_routes(
        _config(route=_route(cp=UPSTREAM, default="self")),
        target_host=InMemoryPromotionTargetHost(
            writable=False, unwritable_reason="not writable by this token"
        ),
    )

    assert [check.status for check in checks] == ["error"]
    assert "not writable by this token" in checks[0].detail


def test_the_probe_carries_the_labels_filing_will_require():
    """The whole point of F2/A1: doctor proves the FILING contract."""
    host = InMemoryPromotionTargetHost(writable=True)

    check_tech_lead_finding_routes(
        _config(route=_route(cp=UPSTREAM, default="self")), target_host=host
    )

    assert [contract.repo for contract in host.filing_checks] == [UPSTREAM]
    contract = host.filing_checks[0]
    # The target's worker agent, its area tag, and the approval gate — exactly
    # what a promotion filed into that route carries.
    assert "agent:backend" in contract.labels
    assert "area:cp" in contract.labels
    assert "proposed-tech-lead" in contract.labels
    # An explicitly routed area is enumerable, so filing needs no label it
    # cannot name up front.
    assert not contract.provisions_unknown_labels


def test_a_foreign_catch_all_route_requires_label_provisioning():
    """`default` promotions carry an area tag nobody can enumerate at startup."""
    host = InMemoryPromotionTargetHost(writable=True)

    check_tech_lead_finding_routes(
        _config(route=_route(default=UPSTREAM)), target_host=host
    )

    assert [contract.provisions_unknown_labels for contract in host.filing_checks] == [
        True
    ]


def test_self_only_routes_need_no_cross_repo_read():
    class Exploding(InMemoryPromotionTargetHost):
        def check_filing_ready(self, contract: PromotionFilingContract) -> str | None:
            raise AssertionError("self-only routes must make no cross-repo read")

    checks = check_tech_lead_finding_routes(_config(), target_host=Exploding())

    assert [check.status for check in checks] == ["ok"]


@pytest.mark.parametrize(
    "overrides", ({"promote": "off"},)
)
def test_disabled_lane_reports_nothing(overrides):
    assert (
        check_tech_lead_finding_routes(
            _config(**overrides), target_host=InMemoryPromotionTargetHost()
        )
        == []
    )


def test_no_tech_lead_agent_reports_nothing():
    config = _config(route=_route(cp=UPSTREAM))
    config.tech_lead_review_agent = None

    assert (
        check_tech_lead_finding_routes(
            config, target_host=InMemoryPromotionTargetHost()
        )
        == []
    )


def test_an_unstartable_lane_reports_its_startup_problems():
    """An unroutable lane fails here too, not only in config validation."""
    config = _config(route=_route(cp=UPSTREAM))
    config.tech_lead_follow_up_agent = None

    checks = check_tech_lead_finding_routes(
        config, target_host=InMemoryPromotionTargetHost()
    )

    assert [check.status for check in checks] == ["error"]
    assert "tech_lead_follow_up_agent" in checks[0].detail


def test_each_distinct_target_is_probed_exactly_once():
    """Two areas routed to one repo cost ONE probe, not two (API discipline)."""
    probed: list[str] = []

    class Counting(InMemoryPromotionTargetHost):
        def check_filing_ready(self, contract: PromotionFilingContract) -> str | None:
            probed.append(contract.repo)
            return None

    checks = check_tech_lead_finding_routes(
        _config(
            route=_route(
                **{
                    "completion-pipeline": UPSTREAM,
                    "review-exchange": UPSTREAM,
                    "ui": "other/repo",
                    "default": "self",
                }
            )
        ),
        target_host=Counting(),
    )

    assert [check.status for check in checks] == ["ok"]
    assert sorted(probed) == sorted([UPSTREAM, "other/repo"])


def test_merged_contracts_keep_every_area_label():
    """One probe per repo must still require BOTH areas' labels."""
    host = InMemoryPromotionTargetHost(writable=True)

    check_tech_lead_finding_routes(
        _config(
            route=_route(
                **{"completion-pipeline": UPSTREAM, "ui": UPSTREAM, "default": "self"}
            )
        ),
        target_host=host,
    )

    assert len(host.filing_checks) == 1
    labels = host.filing_checks[0].labels
    assert "area:completion-pipeline" in labels and "area:ui" in labels


def test_route_host_construction_failure_is_an_error():
    config = _config(route=_route(cp=UPSTREAM))
    with patch(
        "issue_orchestrator.execution.providers.create_repository_host",
        side_effect=RuntimeError("auth unavailable"),
    ):
        checks = check_tech_lead_finding_routes(config)

    assert [check.status for check in checks] == ["error"]
    assert "Could not verify" in checks[0].detail


def test_unsupported_route_host_is_an_error():
    config = _config(route=_route(cp=UPSTREAM))
    with (
        patch(
            "issue_orchestrator.execution.providers.create_repository_host",
            return_value=object(),
        ),
        patch(
            "issue_orchestrator.execution.providers.create_promotion_target_host",
            return_value=None,
        ),
    ):
        checks = check_tech_lead_finding_routes(config)

    assert [check.status for check in checks] == ["error"]
    assert "must be proven" in checks[0].detail
