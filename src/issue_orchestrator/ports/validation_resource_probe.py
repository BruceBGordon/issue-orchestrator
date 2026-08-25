"""Port for one bounded validation host-resource observation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..infra.validation_timings import ValidationResourceSample


@runtime_checkable
class ValidationResourceProbe(Protocol):
    """Collect one resource sample without exceeding its owned probe bounds."""

    def collect(self) -> ValidationResourceSample:
        """Return one complete or explicitly partial host sample."""
        ...
