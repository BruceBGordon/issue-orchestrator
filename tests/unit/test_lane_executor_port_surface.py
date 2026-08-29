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
from issue_orchestrator.ports import lane_dispatch_journal
from issue_orchestrator.ports.lane_executor import LaneExecutor
from issue_orchestrator.ports.lane_policy_check import LanePolicyCheck
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
        # frozen mid-turn), not a tuning knob. Deliberately reshaped
        # (#7124) from bool to the three-valued LaneSuspendability so
        # cooperative lanes — freezable only at self-advertised safe
        # points — are expressible without weakening either old value.
        "suspendability",
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
        # Deliberate widening (dispatch observability): the scheduling
        # wait the runtime excludes, reported so dispatch quality is
        # visible per lane without pool archaeology. Backend-agnostic:
        # the direct backend reports 0.0.
        "queue_wait_seconds",
    )
    assert _field_names(lane_execution.LaneTimedOut) == ("elapsed_seconds",)


def test_executor_port_has_exactly_one_operation() -> None:
    operations = [
        name
        for name in vars(LaneExecutor)
        if not name.startswith("_") and callable(getattr(LaneExecutor, name))
    ]
    assert operations == ["run"]


def test_dispatch_journal_surface_is_pinned() -> None:
    """New port (A1, #7122 review): one operation, one typed record —
    dispatch observability without the CLI owning storage transport."""
    operations = [
        name
        for name in vars(lane_dispatch_journal.LaneDispatchJournal)
        if not name.startswith("_")
        and callable(getattr(lane_dispatch_journal.LaneDispatchJournal, name))
    ]
    assert operations == ["record"]
    assert _field_names(lane_dispatch_journal.LaneDispatchRecord) == (
        "work_key",
        "backend",
        "priority",
        "queue_wait_seconds",
        "observed_runtime_seconds",
        "exit_code",
    )


def test_policy_check_port_surface_is_pinned() -> None:
    """New port (#7129): one read-only operation, one typed report.

    Kept deliberately narrow — a self-check that could also *repair*
    would give the gate a way to mutate the backend it is judging."""
    operations = [
        name
        for name in vars(LanePolicyCheck)
        if not name.startswith("_") and callable(getattr(LanePolicyCheck, name))
    ]
    assert operations == ["inspect"]
    # No advisory channel beside `invariants`, deliberately: the one
    # that existed let an unasserted fact read as a passing check
    # (C1, #7132 review). Everything a check has to say is asserted.
    assert _field_names(lane_execution.LanePolicyReport) == (
        "source",
        "remedy",
        "invariants",
    )
    assert _field_names(lane_execution.LanePolicyInvariant) == (
        "knob",
        "expected",
        "observed",
    )
    assert not hasattr(lane_execution, "LanePolicyObservation")


def test_history_port_has_exactly_two_operations() -> None:
    operations = sorted(
        name
        for name in vars(LaneRuntimeHistory)
        if not name.startswith("_") and callable(getattr(LaneRuntimeHistory, name))
    )
    assert operations == ["learned_priority", "record_success"]
