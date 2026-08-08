"""Per-batch label churn counters (#6999 F2 round 2 extraction).

A value object, not part of the IO boundary that happens to increment it. It
knows nothing about actions, ports or reconciliation - only how many label adds
and removes a batch attempted, applied, skipped as no-ops, and failed, and how
to render that as one observability payload.

Split out of :mod:`.action_applier` because the applier is a long-standing
line-budget hotspot and this is the piece with no coupling to it at all: the
counters are read by exactly one caller and could be handed to any of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LabelMutationStatField = Literal[
    "label_add_attempted",
    "label_add_applied",
    "label_add_noop",
    "label_remove_attempted",
    "label_remove_applied",
    "label_remove_noop",
    "label_mutation_failed",
]


@dataclass
class LabelMutationStats:
    """Per-batch label mutation counters for churn observability."""

    label_add_attempted: int = 0
    label_add_applied: int = 0
    label_add_noop: int = 0
    label_remove_attempted: int = 0
    label_remove_applied: int = 0
    label_remove_noop: int = 0
    label_mutation_failed: int = 0

    @property
    def attempted(self) -> int:
        return self.label_add_attempted + self.label_remove_attempted

    @property
    def applied(self) -> int:
        return self.label_add_applied + self.label_remove_applied

    @property
    def noop(self) -> int:
        return self.label_add_noop + self.label_remove_noop

    def increment(self, field_name: LabelMutationStatField) -> None:
        """Bump one counter by name, keeping the field set closed to typos."""
        setattr(self, field_name, getattr(self, field_name) + 1)

    def to_payload(self) -> dict[str, int]:
        return {
            "label_add_attempted": self.label_add_attempted,
            "label_add_applied": self.label_add_applied,
            "label_add_noop": self.label_add_noop,
            "label_remove_attempted": self.label_remove_attempted,
            "label_remove_applied": self.label_remove_applied,
            "label_remove_noop": self.label_remove_noop,
            "label_mutation_attempted": self.attempted,
            "label_mutation_applied": self.applied,
            "label_mutation_noop": self.noop,
            "label_mutation_failed": self.label_mutation_failed,
        }


__all__ = ["LabelMutationStatField", "LabelMutationStats"]
