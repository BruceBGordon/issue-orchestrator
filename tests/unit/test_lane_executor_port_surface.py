"""Pin the LaneExecutor port surface against silent accretion.

Deep modules erode two ways. Leakage — backend vocabulary escaping
upward — is enforced by the semgrep condor-vocabulary guardrail.
Accretion — the interface silently widening one convenience field at a
time — is enforced here: growing or reshaping any pinned surface must
edit this file, making the widening a deliberate, reviewed decision.
(Precedent: the public-contract and settings-schema drift tests.)
"""

from __future__ import annotations

import dataclasses

from issue_orchestrator.domain import lane_execution
from issue_orchestrator.ports.lane_executor import LaneExecutor
from issue_orchestrator.ports.lane_runtime_history import LaneRuntimeHistory


def _field_names(datatype: type) -> tuple[str, ...]:
    return tuple(field.name for field in dataclasses.fields(datatype))


def test_lane_resources_surface_is_pinned() -> None:
    assert _field_names(lane_execution.LaneResources) == (
        "request_cpus",
        "exclusive",
        "priority",
        "request_memory_mb",
        # Deliberate widening (#7114): load-backoff eligibility is a
        # client-known correctness fact (live exchanges must never be
        # frozen mid-turn), not a tuning knob.
        "suspendable",
    )


def test_lane_command_surface_is_pinned() -> None:
    assert _field_names(lane_execution.LaneCommand) == (
        "work_key",
        "arguments",
        "working_directory",
        "deadline",
    )


def test_lane_outcome_surfaces_are_pinned() -> None:
    assert _field_names(lane_execution.LaneCompleted) == (
        "exit_code",
        "observed_runtime_seconds",
    )
    assert _field_names(lane_execution.LaneTimedOut) == ("elapsed_seconds",)


def test_executor_port_has_exactly_one_operation() -> None:
    operations = [
        name
        for name in vars(LaneExecutor)
        if not name.startswith("_") and callable(getattr(LaneExecutor, name))
    ]
    assert operations == ["run"]


def test_history_port_has_exactly_two_operations() -> None:
    operations = sorted(
        name
        for name in vars(LaneRuntimeHistory)
        if not name.startswith("_") and callable(getattr(LaneRuntimeHistory, name))
    )
    assert operations == ["learned_priority", "record_success"]
