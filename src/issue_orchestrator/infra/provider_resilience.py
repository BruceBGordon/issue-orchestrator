"""Provider resilience status helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..ports.provider_resilience import ProviderErrorType


PROVIDER_STATUS_FILE = "provider-status.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProviderStatus:
    provider: str | None
    error_type: ProviderErrorType | None
    attempts: int
    succeeded: bool
    exit_code: int | None
    timed_out: bool
    last_error_summary: str | None
    last_attempt_at: str

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "error_type": self.error_type.value if self.error_type else None,
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "last_error_summary": self.last_error_summary,
            "last_attempt_at": self.last_attempt_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderStatus":
        try:
            last_attempt_at = data["last_attempt_at"]
        except KeyError as exc:
            raise ValueError(
                "provider status must include last_attempt_at"
            ) from exc
        if not isinstance(last_attempt_at, str):
            raise ValueError("provider status last_attempt_at must be a string")
        error_type = data.get("error_type")
        parsed_error_type = None
        if error_type:
            try:
                parsed_error_type = ProviderErrorType(str(error_type))
            except ValueError:
                parsed_error_type = None
        return cls(
            provider=data.get("provider"),
            error_type=parsed_error_type,
            attempts=int(data.get("attempts", 1)),
            succeeded=bool(data.get("succeeded", False)),
            exit_code=data.get("exit_code"),
            timed_out=bool(data.get("timed_out", False)),
            last_error_summary=data.get("last_error_summary"),
            last_attempt_at=last_attempt_at,
        )


def write_provider_status(run_dir: Path, status: ProviderStatus) -> Path:
    """Write provider status JSON to the session run directory."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / PROVIDER_STATUS_FILE
    path.write_text(json.dumps(status.to_dict(), indent=2, sort_keys=True))
    return path


def read_provider_status(run_dir: Path) -> ProviderStatus | None:
    """Read provider status JSON from the session run directory."""
    path = run_dir / PROVIDER_STATUS_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return ProviderStatus.from_dict(data)
