# pyright: strict
"""How much CPU one lane asks for, and why — the single policy owner.

The declared ``request_cpus`` in ``.issue-orchestrator/lanes.yaml`` is a
hand-measured constant. Measurement automates itself once lanes report
their own busy-cores figure, but the declaration does not stop being
useful: it becomes the SEED (what to ask for before anything is known)
and the CEILING (what evidence may never exceed).

The asymmetry is deliberate and is the whole safety property of this
module:

- **Learned evidence may only LOWER the request.** A lane that measures
  under its declaration is giving back capacity, and giving back
  capacity can only improve pool throughput.
- **Learned evidence may NEVER RAISE it.** A lane suddenly "measuring"
  sixteen cores is far more likely to be a broken measurement (a
  runtime clock too coarse to divide by, a contended host, a lane that
  swallowed another lane's children) than a lane that genuinely got
  eight times hungrier. Granting that request would let one bad number
  drain the pool. The divergence is recorded instead, so a real change
  in demand shows up as evidence for a human to act on by editing the
  declaration — a deliberate act, not an automatic one.

Nothing here is scheduler-flavored: it is arithmetic over a declared
integer and a measured float, and every backend that honors CPU
requests consumes the result.

**Known limitation — the one-way ratchet.** With only a downward path,
a measurement taken while the lane was starved is indistinguishable
from a lane that genuinely wants less, and it lowers the request
permanently: a lane that needed eight cores but got four on a busy
host learns "four", and once four is what it asks for, four is all it
will ever measure. Nothing here can climb back. Three things bound the
damage today — the floor of one core, the rolling window (a lane that
does measure higher again re-converges up to its declared ceiling),
and full visibility in the dispatch journal, where `declared_cpus`,
`request_cpus`, and `observed_busy_cores` sit side by side per run.
The real fix is an upward path with an evidence bar, deliberately left
to a later increment rather than smuggled in as a fudge factor: the
safe direction had to ship first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# A lane always gets at least one core: a floor of zero is not a
# smaller request, it is an unschedulable one.
_MINIMUM_REQUEST_CPUS = 1


@dataclass(frozen=True, slots=True)
class LaneCpuRequest:
    """The CPU request one lane run submits with, and the evidence for it.

    All three numbers are kept, not just the winner: a dispatch record
    holding only the submitted request cannot tell "no history yet"
    from "history agrees with the declaration", and cannot show a
    capped divergence at all.
    """

    declared_cpus: int
    """The lanes.yaml declaration: seed when nothing is known, ceiling always."""

    learned_busy_cores: float | None
    """Rolling median of measured busy cores; None when nothing is known."""

    request_cpus: int
    """What actually crosses the port. Never above ``declared_cpus``."""

    def __post_init__(self) -> None:
        if type(self.declared_cpus) is not int or self.declared_cpus < 1:
            raise ValueError(
                "LaneCpuRequest.declared_cpus must be a positive integer"
            )
        if self.learned_busy_cores is not None and (
            type(self.learned_busy_cores) is not float
            or not math.isfinite(self.learned_busy_cores)
            or self.learned_busy_cores < 0
        ):
            raise ValueError(
                "LaneCpuRequest.learned_busy_cores must be None or a finite, "
                "non-negative float"
            )
        if type(self.request_cpus) is not int or self.request_cpus < 1:
            raise ValueError(
                "LaneCpuRequest.request_cpus must be a positive integer"
            )
        if self.request_cpus > self.declared_cpus:
            raise ValueError(
                "LaneCpuRequest.request_cpus may never exceed declared_cpus: "
                f"{self.request_cpus} > {self.declared_cpus}"
            )

    @classmethod
    def resolve(
        cls, declared_cpus: int, learned_busy_cores: float | None
    ) -> LaneCpuRequest:
        """Decide this run's request from the declaration and the evidence.

        Empty evidence submits the declaration unchanged — the naive
        run is byte-for-byte the pre-learning behavior. Evidence is
        rounded UP (a lane measuring 0.85 busy cores still needs a
        whole core to run on), floored at one, and capped at the
        declaration.
        """
        if learned_busy_cores is None:
            return cls(declared_cpus, None, declared_cpus)
        wanted = max(_MINIMUM_REQUEST_CPUS, math.ceil(learned_busy_cores))
        return cls(declared_cpus, learned_busy_cores, min(declared_cpus, wanted))

    @property
    def is_capped(self) -> bool:
        """Evidence asked for more than the declaration allows.

        True marks the suspicious direction: something measured above
        the hand-set ceiling and was refused. Visible in the dispatch
        record so a sustained divergence can be investigated rather
        than silently granted.
        """
        if self.learned_busy_cores is None:
            return False
        return math.ceil(self.learned_busy_cores) > self.declared_cpus
