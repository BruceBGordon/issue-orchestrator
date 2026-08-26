"""Direct-subprocess backend against the shared lane contract."""

from __future__ import annotations

import pytest

from issue_orchestrator.adapters.direct_lane_executor import (
    DirectLaneExecutor,
    DirectLaneTerminationPolicy,
)
from issue_orchestrator.ports.lane_executor import LaneExecutor
from tests.unit.lane_executor_contract import LaneExecutorContract

pytestmark = pytest.mark.timeout(180)


class TestDirectLaneExecutorContract(LaneExecutorContract):
    def build_executor(self) -> LaneExecutor:
        return DirectLaneExecutor(DirectLaneTerminationPolicy(2.0))
