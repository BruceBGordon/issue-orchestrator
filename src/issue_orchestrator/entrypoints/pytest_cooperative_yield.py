# pyright: strict
"""Opt-in pytest plugin driving the acknowledged yield lifecycle.

Enable per lane with ``-p issue_orchestrator.entrypoints.pytest_cooperative_yield``
on a lane declared ``suspendability: cooperative``. The lifecycle
(A2/A3, #7134 review — the state machine itself is owned by
``execution/lane_yield.py``):

- ``pytest_configure`` ALWAYS forces an acknowledged False before any
  item, so a predecessor process in the same job that crashed while
  safe is caught here — as a hard, run-fatal error — instead of this
  process running exposed.
- Around every item: acknowledged ``lower()`` before (a hard
  :class:`LaneYieldError` fails the item visibly), best-effort
  ``raise_safe()`` after.
- ``pytest_unconfigure`` lowers and LEAVES False: the rest state
  between processes is unfreezable by design. The idle-job freeze
  window between suites is deliberately forfeited — a parting True
  would turn every crash boundary into a stale-safe hazard, and
  idle-capacity decisions belong to stage admission, not a dying
  process's last word.

This module is a composition root: like ``lane_run``, it is the one
place this entrypoint names its scheduling adapter (guardrail
exemptions carry the same rationale).

Under pytest-xdist the plugin composes the inert owner in every
worker: workers are separate processes flipping one shared job
attribute, so "between items" in one worker can be mid-item in eleven
others. Cooperative adoption targets serial lanes until a
controller-side all-workers-idle aggregation exists (noted on #7124).
Inert means the submit-time False stands — never-frozen, fail-safe.
"""

from __future__ import annotations

import os
from typing import Generator, Union

import pytest

from ..adapters.condor.chirp_yield_signal import resolve_lane_yield_transport
from ..execution.lane_yield import AcknowledgedLaneYield, InertLaneYield

_XDIST_WORKER_ENVIRONMENT_VARIABLE = "PYTEST_XDIST_WORKER"
_LaneYield = Union[AcknowledgedLaneYield, InertLaneYield]
_YIELD_KEY = pytest.StashKey[_LaneYield]()


def _compose_lane_yield() -> _LaneYield:
    if _XDIST_WORKER_ENVIRONMENT_VARIABLE in os.environ:
        return InertLaneYield()
    transport = resolve_lane_yield_transport()
    if transport is None:
        return InertLaneYield()
    return AcknowledgedLaneYield(transport)


def pytest_configure(config: pytest.Config) -> None:
    lane_yield = _compose_lane_yield()
    # The opening acknowledged False: raises LaneYieldError (fatal to
    # the run, visible in the lane output) when a predecessor's stale
    # True cannot be lowered.
    lane_yield.lower()
    config.stash[_YIELD_KEY] = lane_yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: "pytest.Item | None"
) -> Generator[None, None, None]:
    del nextitem
    lane_yield = item.config.stash.get(_YIELD_KEY, None)
    if lane_yield is None:
        yield
        return
    lane_yield.lower()
    try:
        yield
    finally:
        lane_yield.raise_safe()


def pytest_unconfigure(config: pytest.Config) -> None:
    lane_yield = config.stash.get(_YIELD_KEY, None)
    if lane_yield is not None:
        # Leave the rest state unfreezable; a hard failure here is a
        # loud INTERNALERROR rather than a silent stale-safe handoff.
        lane_yield.lower()
