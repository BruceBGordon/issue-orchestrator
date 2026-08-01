"""Tech Lead artifact policy evaluated at reviewer approval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.tech_lead_session import TechLeadLaunchAuthority
from ..ports.review_exchange_approval_gate import ReviewExchangeApprovalGate
from .label_manager import LabelManager
from .tech_lead_completion import (
    load_validated_tech_lead_pair,
    resolve_tech_lead_launch_authority,
)
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


@dataclass(frozen=True)
class TechLeadDecisionApprovalGate:
    """Revalidate the current decision pair before reviewer approval is final."""

    run_dir: Path
    authority: TechLeadLaunchAuthority
    config: Config

    def rejection_reason(self) -> str | None:
        result = load_validated_tech_lead_pair(
            self.run_dir,
            self.authority,
            config=self.config,
            labels=LabelManager(self.config),
        )
        if result.ok:
            return None
        failure = result.failure.value if result.failure else "unknown"
        return (
            "Tech Lead decision artifact rejected "
            f"({failure}): {result.detail}."
        )


@dataclass(frozen=True)
class _RejectedTechLeadApprovalGate:
    reason: str

    def rejection_reason(self) -> str:
        return self.reason


def build_tech_lead_decision_approval_gate(
    config: Config | None,
    *,
    tech_lead_agent: str | None,
    agent_label: str | None,
    tech_lead_authority: TechLeadAuthorityStore,
    run_dir: Path,
    run_id: str,
    session_name: str,
) -> ReviewExchangeApprovalGate | None:
    """Build a fail-closed gate only for configured Tech Lead sessions."""
    if config is None or not is_tech_lead_session(tech_lead_agent, agent_label):
        return None
    authority, tamper = resolve_tech_lead_launch_authority(
        tech_lead_authority,
        run_dir=run_dir,
        run_id=run_id,
        session_name=session_name,
    )
    if authority is None:
        return _RejectedTechLeadApprovalGate(
            f"Tech Lead launch authority missing: {tamper}"
        )
    if tamper is not None:
        return _RejectedTechLeadApprovalGate(
            f"Tech Lead launch authority rejected: {tamper}"
        )
    return TechLeadDecisionApprovalGate(
        run_dir=run_dir,
        authority=authority,
        config=config,
    )
