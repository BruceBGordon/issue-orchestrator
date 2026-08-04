"""Doctor: finding-promotion route targets must be writable at startup (#6957).

A route target the token cannot file issues in has to fail LOUDLY at startup.
Discovering it at promotion time means losing the actuation on the exact tick a
pattern finally crossed its evidence threshold.
"""

from unittest.mock import patch

import pytest

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
    config.tech_lead_review_agent = "agent:tech-lead"
    for key, value in findings_overrides.items():
        setattr(config.tech_lead.findings, key, value)
    return config


def test_writable_route_targets_pass():
    checks = check_tech_lead_finding_routes(
        _config(route=_route(cp=UPSTREAM, default="self")),
        target_host=InMemoryPromotionTargetHost(writable=True),
    )

    assert [check.status for check in checks] == ["ok"]
    assert UPSTREAM in checks[0].detail


def test_unwritable_route_target_is_an_error():
    checks = check_tech_lead_finding_routes(
        _config(route=_route(cp=UPSTREAM, default="self")),
        target_host=InMemoryPromotionTargetHost(
            writable=False, unwritable_reason="not writable by this token"
        ),
    )

    assert [check.status for check in checks] == ["error"]
    assert "not writable by this token" in checks[0].detail


def test_self_only_routes_need_no_cross_repo_read():
    class Exploding(InMemoryPromotionTargetHost):
        def check_writable(self, *, repo: str) -> str | None:
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


def test_each_distinct_target_is_probed_exactly_once():
    """Two areas routed to one repo cost ONE read, not two (API discipline)."""
    probed: list[str] = []

    class Counting(InMemoryPromotionTargetHost):
        def check_writable(self, *, repo: str) -> str | None:
            probed.append(repo)
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
