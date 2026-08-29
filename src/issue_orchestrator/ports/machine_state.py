# pyright: strict
"""Port for the machine-state envelope stamped on forensic records.

Every durable record this repository writes about how long something
took — a validation-timing row, a lane-dispatch row — answers "how
long?" without answering "under what?". A contention-inflated sample is
then indistinguishable from a real regression, and the covariate that
would separate them survives only in whoever's terminal happened to be
open at that moment (#7127, evidence 2026-08-28: two overlapping gates
produced 86s/81s samples with nothing in the record naming the overlap,
and a 40-minute "wait for quiet" was spent on a machine that was 80.6%
CPU-idle because load average on macOS counts parked threads).

``MachineState`` is that covariate: one reading of host contention,
carried by the record instead of reconstructed afterwards.

Two rules make it safe to stamp on everything:

1. **It is a measurement, not a decision.** Every measured field is
   optional because an unavailable reading is recorded as unavailable
   and never invented. Nothing may branch control flow on it.
2. **It may never change the outcome it observes.** ``sample()`` must
   not raise. The repository is fail-fast by default; this is the
   deliberate exception, owned here and stated once: an observability
   probe that can fail a gate manufactures exactly the false failures
   this forensics work exists to remove.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class MachineState:
    """One moment's reading of how contended the host was.

    ``cpu_idle_source`` always names where the idle figure came from —
    the probe when it answered, the reason when it did not — so a
    silently degrading probe is visible in the same record an analyst is
    already reading. ``probe_error`` is set only when the reading failed
    outright; the measured fields are then absent rather than zeroed,
    because an invented zero is worse than a missing number.

    ``physical_cores`` is the same count the validation pool's capacity
    dial scales (``hw.ncpu`` / ``nproc``), so an envelope's core count
    and a pool's admitted width are comparable numbers.
    """

    sampled_at: datetime
    loadavg_1m: float | None
    loadavg_5m: float | None
    loadavg_15m: float | None
    cpu_idle_percent: float | None
    cpu_idle_source: str
    physical_cores: int | None
    probe_error: str | None

    def __post_init__(self) -> None:
        if type(self.sampled_at) is not datetime or self.sampled_at.tzinfo is None:
            raise ValueError(
                "MachineState.sampled_at must be a timezone-aware datetime"
            )
        if type(self.cpu_idle_source) is not str or not self.cpu_idle_source:
            raise ValueError(
                "MachineState.cpu_idle_source must be a non-empty string"
            )
        if self.probe_error is not None and (
            type(self.probe_error) is not str or not self.probe_error
        ):
            raise ValueError(
                "MachineState.probe_error must be None or a non-empty string"
            )
        self._validate_loads()
        self._validate_cpu_idle_percent()
        self._validate_physical_cores()
        # Coherence: a reading that did not fail must actually carry the
        # cheap syscall facts. Otherwise "no error and no numbers" would
        # be an unreportable third state.
        if self.probe_error is None and (
            self.loadavg_1m is None or self.physical_cores is None
        ):
            raise ValueError(
                "MachineState without probe_error must carry load average "
                "and core count"
            )

    def _validate_loads(self) -> None:
        for field_name, value in (
            ("loadavg_1m", self.loadavg_1m),
            ("loadavg_5m", self.loadavg_5m),
            ("loadavg_15m", self.loadavg_15m),
        ):
            if value is None:
                continue
            if type(value) is not float or not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"MachineState.{field_name} must be finite and non-negative"
                )

    def _validate_cpu_idle_percent(self) -> None:
        value = self.cpu_idle_percent
        if value is None:
            return
        if (
            type(value) is not float
            or not math.isfinite(value)
            or not 0.0 <= value <= 100.0
        ):
            raise ValueError(
                "MachineState.cpu_idle_percent must be a percentage in [0, 100]"
            )

    def _validate_physical_cores(self) -> None:
        value = self.physical_cores
        if value is None:
            return
        if type(value) is not int or value < 1:
            raise ValueError(
                "MachineState.physical_cores must be a positive integer"
            )


@runtime_checkable
class MachineStateSampler(Protocol):
    """Read the host's current contention.

    Implementations MUST NOT raise: see rule 2 in the module docstring.
    A probe that cannot answer returns a ``MachineState`` whose
    ``probe_error``/``cpu_idle_source`` say so.
    """

    def sample(self) -> MachineState:
        """Return one reading of host contention; never raise."""
        ...
