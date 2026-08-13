"""Typed Timeline evidence payload and projection helper."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class TimelineEvidenceView(BaseModel):
    """Visible retention state for one exact run in the issue Timeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_dir: str
    status: Literal["active", "retained", "pinned", "expired", "missing"]
    label: str
    available: bool
    pinned: bool
    archived: bool
    expires_at: str | None = None
    help_text: str
    unpin_expires_immediately: bool


def parse_timeline_evidence(value: Any) -> TimelineEvidenceView | None:
    """Validate an optional evidence mapping at the projection boundary."""
    if not isinstance(value, Mapping):
        return None
    return TimelineEvidenceView.model_validate(value)
