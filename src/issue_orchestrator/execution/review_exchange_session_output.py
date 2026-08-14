"""Filesystem helpers for review-exchange session artifacts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.review_exchange_manifest import ReviewExchangeSummaryManifestUpdate
from ..domain.review_exchange_run import ReviewExchangeRun, ReviewExchangeRunAssets
from ..domain.review_exchange_summary import ReviewExchangeSummaryV1
from ..infra.logging_config import get_repo_log_path
from ..ports.session_output import ReviewExchangeSummary, SessionRunAssets


def start_review_exchange_run(
    start_run: Callable[..., SessionRunAssets],
    worktree_path: Path,
    *,
    issue_number: int,
    parent_session_name: str,
    agent_label: str,
    retention_days: int = 7,
    retention_tier: str = "hot",
) -> ReviewExchangeRun:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    session_name = f"review-exchange-{issue_number}-{timestamp}"
    run = start_run(
        worktree_path,
        session_name,
        issue_number=issue_number,
        agent_label=agent_label,
        backend="persistent-pty",
        orchestrator_log=str(get_repo_log_path(worktree_path)),
        retention_days=retention_days,
        retention_tier=retention_tier,
    )
    assets = ReviewExchangeRunAssets.from_run_dir(run.run_dir)
    assets.exchange_dir.mkdir(parents=True, exist_ok=True)
    return ReviewExchangeRun(
        issue_number=issue_number,
        session_name=session_name,
        run_id=run.run_id,
        parent_session_name=parent_session_name,
        assets=assets,
    )


def store_review_exchange_summary(
    review_run: ReviewExchangeRun,
    summary: ReviewExchangeSummaryV1,
    *,
    write_json: Callable[[Path, dict[str, Any]], None],
    update_manifest: Callable[[Path, dict[str, str]], None],
    append_run_log_line: Callable[[Path, str], None],
) -> ReviewExchangeSummary:
    run_dir = review_run.assets.run_dir
    exchange_dir = review_run.assets.exchange_dir
    exchange_dir.mkdir(parents=True, exist_ok=True)
    summary_path = review_run.assets.summary_path
    write_json(summary_path, summary.to_payload())

    validation_record_path = None
    if review_run.assets.validation_record_path.exists():
        validation_record_path = review_run.assets.validation_record_path
    update_manifest(
        run_dir,
        ReviewExchangeSummaryManifestUpdate(
            exchange_dir=exchange_dir,
            summary_path=summary_path,
            validation_record_path=validation_record_path,
        ).to_manifest_fields(),
    )
    append_run_log_line(
        run_dir,
        f"review-exchange status={summary.status.value} reason={summary.reason.value}",
    )

    return ReviewExchangeSummary(
        summary=summary,
        run_assets=review_run.assets,
    )
