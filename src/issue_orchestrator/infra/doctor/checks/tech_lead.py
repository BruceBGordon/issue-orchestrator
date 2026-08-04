"""Tech Lead workflow label + promotion-route checks for doctor.

When tech_lead is configured, act-level proposals are filed as GitHub issues
carrying the ``proposed-tech-lead`` gate (#6779 R3). A fresh install that never
provisioned that label would create ungated (schedulable) proposal issues, so
surface the missing gate here in addition to the applier's fail-before-create
guard.

The finding-promotion lane (#6957) adds a second precondition: every configured
``tech_lead.findings.route`` target must be a repo this token can actually file
issues in. That is verified here rather than at promotion time, because a
promotion fires on the tick a pattern finally crosses its evidence threshold —
discovering a misrouted target then means losing the actuation the lane exists
to provide.
"""

from typing import TYPE_CHECKING

from ..types import Check

if TYPE_CHECKING:
    from ....ports.promotion_target import PromotionTargetHost
    from ...config import Config


def check_tech_lead_labels(config: "Config | None" = None) -> list[Check]:
    if config is None or not config.tech_lead_review_agent or not config.repo:
        return []  # tech_lead/repo not configured -> nothing to verify

    from ....domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

    try:
        from ....execution.providers import create_repository_host

        host = create_repository_host(repo=config.repo, config=config)
        existing = {
            name.casefold()
            for entry in host.list_labels()
            if isinstance(entry, dict) and isinstance((name := entry.get("name")), str)
        }
    except Exception as exc:
        # Advisory only: a GitHub read failure must not fail doctor outright
        # (auth/connectivity are covered by their own checks).
        return [
            Check(
                name="Tech Lead Labels",
                status="warning",
                detail=f"Could not verify the '{PROPOSED_TECH_LEAD_LABEL}' gate label: {exc}",
            )
        ]

    gate_present = PROPOSED_TECH_LEAD_LABEL.casefold() in existing
    if gate_present:
        return [
            Check(
                name="Tech Lead Labels",
                status="ok",
                detail=f"Gate label '{PROPOSED_TECH_LEAD_LABEL}' provisioned",
            )
        ]
    return [
        Check(
            name="Tech Lead Labels",
            status="error",
            detail=(
                f"Gate label '{PROPOSED_TECH_LEAD_LABEL}' is missing — tech_lead"
                " proposals would be ungated. Run `issue-orchestrator init`."
            ),
        )
    ]


def check_tech_lead_finding_routes(
    config: "Config | None" = None,
    *,
    target_host: "PromotionTargetHost | None" = None,
) -> list[Check]:
    """Verify every finding-promotion route target is writable (#6957).

    Only NON-``self`` targets are probed: a ``self`` route lands in the managed
    repo, whose writability is already covered by the auth/repo checks. One read
    per distinct target, and none at all when the lane is off or every route is
    ``self`` — GitHub API discipline.

    ``target_host`` is injectable so this check is testable without a live
    GitHub; production leaves it None and the host is built from config.
    """
    if config is None or not config.tech_lead_review_agent or not config.repo:
        return []
    findings = config.tech_lead.findings
    if not findings.enabled:
        return []
    targets = findings.target_repos()
    if not targets:
        return [
            Check(
                name="Tech Lead Finding Routes",
                status="ok",
                detail="All promotion routes resolve to this repository",
            )
        ]
    if target_host is None:
        try:
            from ....execution.providers import (
                create_promotion_target_host,
                create_repository_host,
            )

            target_host = create_promotion_target_host(
                create_repository_host(repo=config.repo, config=config)
            )
        except Exception as exc:
            return [
                Check(
                    name="Tech Lead Finding Routes",
                    status="warning",
                    detail=f"Could not verify promotion route targets: {exc}",
                )
            ]
    if target_host is None:
        return [
            Check(
                name="Tech Lead Finding Routes",
                status="warning",
                detail=(
                    "Promotion routes cannot be verified for this repository host"
                ),
            )
        ]
    problems = [
        reason
        for repo in targets
        if (reason := target_host.check_writable(repo=repo)) is not None
    ]
    if problems:
        return [
            Check(
                name="Tech Lead Finding Routes",
                status="error",
                detail=(
                    "tech_lead.findings.route target(s) are not writable: "
                    + "; ".join(problems)
                ),
            )
        ]
    return [
        Check(
            name="Tech Lead Finding Routes",
            status="ok",
            detail=f"Promotion route target(s) writable: {', '.join(targets)}",
        )
    ]
