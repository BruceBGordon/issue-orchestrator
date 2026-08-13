"""Strict scalar conversion helpers shared by view-model projections."""

from __future__ import annotations

from typing import Any


def optional_string(value: Any) -> str | None:
    """Return a non-empty string for a present scalar value."""
    if value is None:
        return None
    text = str(value)
    return text or None
