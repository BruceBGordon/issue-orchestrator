"""Approval-boundary coverage for tech-lead decision artifacts."""

import json
from pathlib import Path

from issue_orchestrator.control.tech_lead_approval_gate import (
    TechLeadDecisionApprovalGate,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config


def _write_pair(run_dir: Path, *, title: str) -> None:
    data_dir = run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Health review complete.",
                "findings": [
                    {
                        "id": "T1",
                        "title": title,
                        "classification": "systemic",
                        "evidence": ["board-snapshot.json"],
                    }
                ],
                "proposed_actions": [],
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "tech-lead-report.md").write_text(
        "# Tech Lead Report\n\nT1 is documented here.\n",
        encoding="utf-8",
    )


def test_gate_rechecks_current_pair_and_accepts_repair(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_pair(run_dir, title="x" * 301)
    gate = TechLeadDecisionApprovalGate(
        run_dir=run_dir,
        authority=TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            anchor_issue_number=34,
        ),
        config=Config(),
    )

    rejection = gate.rejection_reason()

    assert rejection is not None
    assert "contract_violation" in rejection
    assert "title exceeds 300 characters (301)" in rejection

    _write_pair(run_dir, title="Concise health-review finding")

    assert gate.rejection_reason() is None
