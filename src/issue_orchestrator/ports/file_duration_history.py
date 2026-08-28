# pyright: strict
"""Port for the per-file duration learning loop that balances suite slices.

The slicer used to balance on constants mined by hand and baked into
its source: accurate the day they were measured, decaying every day
after, with no signal when they went wrong. This port closes the loop —
the lanes that run the tests are the same thing that learns what they
cost.

Backend-neutral by construction: the durations are observed inside the
pytest process that runs a slice, which is the same process in every
execution backend (a scheduler backend re-invokes the identical direct
recipe inside its job). No scheduling vocabulary crosses this port.
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable


@runtime_checkable
class FileDurationHistory(Protocol):
    """Learn what each test file costs; answer with balancing weights.

    The contract of the loop:

    - **Only successes teach.** A failed run's durations are the
      failure's, not the suite's — an aborted run stops early and would
      teach every unreached file that it is free.
    - **Only wholly-run files teach.** A caller that ran part of a file
      (a node-level selection) must leave that file out: a partial
      measurement recorded as a whole-file weight makes a fat file look
      thin, and the next run would fatten it again — an oscillation.
    - **Absence is not an error.** The weights omit files never seen;
      the caller supplies its own naive default for them, so an empty
      store is the naive first run by design.
    - **One gate, one set of weights.** The slices of a single gate ask
      at different moments — a scheduler admits them minutes apart, and
      a slice that already finished has already taught the store. Two
      slicers balancing on different weights do not produce a
      partition: a file can be claimed twice, or by nobody. So weights
      are read *pinned to an epoch*, and every reader of one epoch is
      answered with the snapshot the first of them pinned.
    - Weights are a *balancing* hint (rolling median seconds), never a
      promised duration, and staleness may only cost speed: the
      partition's coverage never depends on what the store holds.
    """

    def record_success(self, durations: Mapping[str, float]) -> None:
        """Persist one successful run's per-file durations in seconds."""
        ...

    def pinned_weights(self, epoch: str) -> Mapping[str, float]:
        """The weights for one epoch, pinned on the first ask.

        Every later ask for the same epoch is answered identically, no
        matter what has been recorded since.
        """
        ...
