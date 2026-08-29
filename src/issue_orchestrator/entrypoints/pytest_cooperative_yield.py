# pyright: strict
"""Opt-in pytest plugin advertising cooperative yield points.

Enable per lane with ``-p issue_orchestrator.entrypoints.pytest_cooperative_yield``
on a lane declared ``suspendability: cooperative``. Between test items
the lane advertises that freezing is safe; while an item (setup, call,
teardown) runs it advertises unsafe. The submit description starts the
job at unsafe, so the window from job start through the first item is
covered without the plugin saying anything.

This module is a composition root: like ``lane_run``, it is the one
place this entrypoint names its scheduling adapter (guardrail
exemptions carry the same rationale).

Deliberate scope limits:

- Under pytest-xdist the plugin is INERT in every worker: workers are
  separate processes flipping one shared job attribute, so "between
  items" in one worker can be mid-item in eleven others. Cooperative
  adoption therefore targets serial lanes until a controller-side
  all-workers-idle aggregation exists (follow-up noted on #7124).
  Inert degrades to never-frozen — the fail-safe direction.
- Advertisement failure never fails the lane (see the port's
  documented exception to fail-fast); the signal goes inert loudly.
"""

from __future__ import annotations

import os
from typing import Generator

import pytest

from ..adapters.condor.chirp_yield_signal import resolve_lane_yield_signal
from ..ports.lane_yield_signal import LaneYieldSignal

_XDIST_WORKER_ENVIRONMENT_VARIABLE = "PYTEST_XDIST_WORKER"
_SIGNAL_KEY = pytest.StashKey["LaneYieldSignal | None"]()


def _build_signal() -> "LaneYieldSignal | None":
    """Resolution seam (tests monkeypatch this).

    None means "stay silent entirely" — the xdist-worker case, where
    even an unsafe advertisement would fight the other workers.
    """
    if _XDIST_WORKER_ENVIRONMENT_VARIABLE in os.environ:
        return None
    return resolve_lane_yield_signal()


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_SIGNAL_KEY] = _build_signal()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: "pytest.Item | None"
) -> Generator[None, None, None]:
    del nextitem
    signal = item.config.stash.get(_SIGNAL_KEY, None)
    if signal is None:
        yield
        return
    signal.advertise(False)
    try:
        yield
    finally:
        signal.advertise(True)


def pytest_unconfigure(config: pytest.Config) -> None:
    # The job is about to exit; leaving the ad at safe is harmless and
    # keeps a session that ends between items consistent with one that
    # ends here.
    signal = config.stash.get(_SIGNAL_KEY, None)
    if signal is not None:
        signal.advertise(True)
