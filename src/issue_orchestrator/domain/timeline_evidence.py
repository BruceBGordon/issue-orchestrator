"""Timeline evidence retention value objects and policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class TimelineEvidenceStatus(str, Enum):
    """User-visible lifecycle of one exact session run's evidence."""

    ACTIVE = "active"
    RETAINED = "retained"
    PINNED = "pinned"
    EXPIRED = "expired"
    MISSING = "missing"


@dataclass(frozen=True)
class TimelineEvidenceIdentity:
    """Exact run identity accepted by the retention owner."""

    issue_number: int
    run_dir: Path


@dataclass(frozen=True)
class SetTimelineEvidencePinCommand:
    """Set the pin state for one exact issue/run pair."""

    identity: TimelineEvidenceIdentity
    pinned: bool


@dataclass(frozen=True)
class FinalizeTimelineEvidenceCommand:
    """Finalize retention for one exact run at its terminal boundary."""

    identity: TimelineEvidenceIdentity
    outcome: str
    ended_at: str | None = None


@dataclass(frozen=True)
class TimelineEvidenceState:
    """Typed retention state rendered by Timeline and returned by commands."""

    identity: TimelineEvidenceIdentity
    status: TimelineEvidenceStatus
    label: str
    available: bool
    pinned: bool
    archived: bool
    expires_at: str | None = None
    help_text: str = ""
    unpin_expires_immediately: bool = False


def parse_retention_timestamp(value: str | None) -> datetime | None:
    """Parse a manifest retention timestamp as an aware UTC datetime."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def retention_has_expired(
    *,
    expires_at: str | None,
    pinned: bool,
    now: datetime,
) -> bool:
    """Return whether evidence is logically expired at ``now``."""
    if pinned:
        return False
    expiry = parse_retention_timestamp(expires_at)
    return expiry is not None and expiry <= now.astimezone(timezone.utc)


def manifest_is_retention_protected(
    *,
    expires_at: str | None,
    pinned: bool,
    now: datetime,
) -> bool:
    """Protect pinned or not-yet-expired runs from count-based pruning."""
    if pinned:
        return True
    expiry = parse_retention_timestamp(expires_at)
    return expiry is not None and expiry > now.astimezone(timezone.utc)
