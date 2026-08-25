# pyright: strict
"""Neutral strict base for private JSON wire records."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictWireRecord(BaseModel):
    """Reject unknown fields, coercion, and mutation at process boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
