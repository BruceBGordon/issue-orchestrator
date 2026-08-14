"""Typed run ownership contracts for review exchange artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_guards import require_absolute_path, require_path_under
from .review_exchange_turn_artifacts import review_exchange_dir


@dataclass(frozen=True, slots=True)
class ReviewExchangeRunAssets:
    """Canonical artifact locations for one review-exchange run."""

    run_dir: Path
    exchange_dir: Path
    summary_path: Path
    validation_record_path: Path

    def __post_init__(self) -> None:
        _require_absolute(self.run_dir, "run_dir")
        _require_absolute(self.exchange_dir, "exchange_dir")
        _require_absolute(self.summary_path, "summary_path")
        _require_absolute(self.validation_record_path, "validation_record_path")
        expected_exchange_dir = review_exchange_dir(self.run_dir)
        if self.exchange_dir.resolve() != expected_exchange_dir.resolve():
            raise ValueError(
                "review exchange assets must use the canonical exchange_dir"
            )
        _require_under(self.summary_path, self.exchange_dir, "summary_path")
        _require_under(
            self.validation_record_path,
            self.run_dir,
            "validation_record_path",
        )

    @classmethod
    def from_run_dir(cls, run_dir: Path) -> "ReviewExchangeRunAssets":
        exchange_dir = review_exchange_dir(run_dir)
        return cls(
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            summary_path=exchange_dir / "summary.json",
            validation_record_path=run_dir / "validation-record.json",
        )

    @classmethod
    def from_exchange_dir(cls, exchange_dir: Path) -> "ReviewExchangeRunAssets":
        return cls.from_run_dir(exchange_dir.parent)


@dataclass(frozen=True, slots=True)
class ReviewExchangeRun:
    """A concrete review-exchange session run allocated by the run owner."""

    issue_number: int
    session_name: str
    run_id: str
    parent_session_name: str
    assets: ReviewExchangeRunAssets

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("review exchange run requires a positive issue_number")
        if not self.session_name:
            raise ValueError("review exchange run requires session_name")
        if not self.run_id:
            raise ValueError("review exchange run requires run_id")
        if not self.parent_session_name:
            raise ValueError("review exchange run requires parent_session_name")


def _require_absolute(path: object, field_name: str) -> None:
    require_absolute_path(path, field_name)


def _require_under(path: Path, root: Path, field_name: str) -> None:
    require_path_under(path, root, field_name)
