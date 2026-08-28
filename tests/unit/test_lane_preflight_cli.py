"""Gate behavior of the lane-preflight CLI.

The CLI's whole job is translating a policy report into a gate
decision: exit 0 or a loud exit 78 naming what drifted. It is tested
against the PORT — a fake check standing in for any backend — never
against a scheduler.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.lane_execution import (
    LaneExecutorError,
    LaneExecutorUnavailableError,
    LanePolicyInvariant,
    LanePolicyObservation,
    LanePolicyReport,
)
from issue_orchestrator.entrypoints.cli_tools import lane_preflight
from issue_orchestrator.entrypoints.cli_tools.lane_preflight import (
    _parse_arguments,
    main,
)
from issue_orchestrator.execution.lane_backends import (
    BACKEND_ENVIRONMENT_VARIABLE,
    build_lane_policy_check,
)

_UNAVAILABLE = 78
_BACKEND_FAULT = 70


class _FakeCheck:
    def __init__(self, report: LanePolicyReport) -> None:
        self._report = report

    def inspect(self) -> LanePolicyReport:
        return self._report


def _report(
    *invariants: LanePolicyInvariant,
    observations: tuple[LanePolicyObservation, ...] = (),
) -> LanePolicyReport:
    return LanePolicyReport(
        source="/pool/etc/pool_config",
        remedy="re-apply the pool policy and re-run the gate",
        invariants=invariants,
        observations=observations,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch, outcome: LanePolicyReport | Exception
) -> None:
    def build(backend: str) -> _FakeCheck:
        del backend
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeCheck(outcome)

    monkeypatch.setattr(lane_preflight, "build_lane_policy_check", build)


def test_healthy_policy_exits_zero_and_says_what_held(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(
        monkeypatch,
        _report(
            LanePolicyInvariant(knob="KNOB_A", expected="1", observed="1"),
            LanePolicyInvariant(knob="KNOB_B", expected="", observed=""),
        ),
    )

    assert main(["--backend", "condor"]) == 0
    captured = capsys.readouterr().err
    assert "2 required setting(s) hold" in captured
    assert "/pool/etc/pool_config" in captured


def test_drift_exits_78_and_names_every_drifted_knob(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(
        monkeypatch,
        _report(
            LanePolicyInvariant(
                knob="CONCURRENCY_LIMIT_DEFAULT", expected="1", observed=""
            ),
            LanePolicyInvariant(knob="PERIODIC_EXPR_INTERVAL", expected="5", observed="5"),
            LanePolicyInvariant(
                knob="MOUNT_UNDER_SCRATCH", expected="", observed="/tmp"
            ),
        ),
    )

    assert main(["--backend", "condor"]) == _UNAVAILABLE
    captured = capsys.readouterr().err
    assert "CONCURRENCY_LIMIT_DEFAULT" in captured
    assert "MOUNT_UNDER_SCRATCH" in captured
    # The satisfied knob is not reported as a problem.
    assert "PERIODIC_EXPR_INTERVAL: expected" not in captured
    # No warn-and-continue: the message says nothing was dispatched.
    assert "no lane was dispatched" in captured
    assert "re-apply the pool policy" in captured


def test_observations_reach_the_gate_log_without_failing_the_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(
        monkeypatch,
        _report(
            LanePolicyInvariant(knob="KNOB_A", expected="1", observed="1"),
            observations=(
                LanePolicyObservation(name="91-io-load-backoff.conf", detail="in effect"),
                LanePolicyObservation(
                    name="92-io-pool-capacity.conf", detail="not installed"
                ),
            ),
        ),
    )

    assert main(["--backend", "condor"]) == 0
    captured = capsys.readouterr().err
    assert "91-io-load-backoff.conf: in effect" in captured
    assert "92-io-pool-capacity.conf: not installed" in captured


def test_unavailable_backend_exits_78_with_the_backend_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, LaneExecutorUnavailableError("no pool here"))

    assert main(["--backend", "condor"]) == _UNAVAILABLE
    assert "no pool here" in capsys.readouterr().err


def test_backend_fault_exits_70_never_disguised_as_healthy_policy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A backend that cannot be read is a software fault (70), distinct
    from one that answered and drifted (78)."""
    _install(monkeypatch, LaneExecutorError("config unreadable"))

    assert main(["--backend", "condor"]) == _BACKEND_FAULT
    captured = capsys.readouterr().err
    assert "backend fault" in captured
    assert "config unreadable" in captured


def test_direct_backend_is_a_real_check_with_nothing_to_assert(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not a skip: the direct backend answers truthfully that it has no
    external policy, which is what lets the gate call preflight
    unconditionally instead of branching on the mode."""
    assert main(["--backend", "direct"]) == 0
    captured = capsys.readouterr().err
    assert "0 required setting(s) hold" in captured
    assert build_lane_policy_check("direct").inspect().invariants == ()


def test_backend_defaults_to_the_shared_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One variable selects the backend for every lane entrypoint, so a
    shell that opted in cannot preflight one backend and run another."""
    monkeypatch.delenv(BACKEND_ENVIRONMENT_VARIABLE, raising=False)
    assert _parse_arguments([]).backend == "direct"
    monkeypatch.setenv(BACKEND_ENVIRONMENT_VARIABLE, "condor")
    assert _parse_arguments([]).backend == "condor"


def test_unknown_backend_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        main(["--backend", "nonesuch"])
