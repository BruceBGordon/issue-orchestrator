"""Typed process-child resource snapshots for executor observation."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutorChildResourceSnapshot:
    """One cumulative process-child resource snapshot."""

    user_cpu_seconds: float
    system_cpu_seconds: float
    process_lifetime_children_max_rss_bytes: int
    input_blocks: int
    output_blocks: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("user_cpu_seconds", self.user_cpu_seconds),
            ("system_cpu_seconds", self.system_cpu_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"ExecutorChildResourceSnapshot.{field_name} must be finite "
                    "and non-negative"
                )
        for field_name, value in (
            (
                "process_lifetime_children_max_rss_bytes",
                self.process_lifetime_children_max_rss_bytes,
            ),
            ("input_blocks", self.input_blocks),
            ("output_blocks", self.output_blocks),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"ExecutorChildResourceSnapshot.{field_name} must be "
                    "non-negative"
                )
