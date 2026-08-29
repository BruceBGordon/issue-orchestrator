"""The chirp yield-signal adapter: advertisements, resolution, and the
documented degrade-to-inert exception to fail-fast."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.chirp_yield_signal import (
    CHIRP_CONFIG_ENVIRONMENT_VARIABLE,
    ChirpLaneYieldSignal,
    InertLaneYieldSignal,
    resolve_lane_yield_signal,
)


def _stub_chirp(tmp_path: Path, body: str) -> Path:
    stub = tmp_path / "condor_chirp"
    stub.write_text("#!/bin/sh\n" + body)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def test_advertisements_carry_the_attribute_both_ways(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    stub = _stub_chirp(tmp_path, f'echo "$@" >> "{log}"\nexit 0\n')
    signal = ChirpLaneYieldSignal(stub)
    signal.advertise(True)
    signal.advertise(False)
    assert log.read_text().splitlines() == [
        "set_job_attr SafeToSuspend True",
        "set_job_attr SafeToSuspend False",
    ]


def test_failure_is_loud_once_then_inert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented exception to fail-fast: a failed advertisement
    must not fail the lane (the pool's =?= semantics already keep it
    unfrozen) but must not fail silently — one loud line, then inert."""
    log = tmp_path / "calls.log"
    stub = _stub_chirp(
        tmp_path, f'echo "$@" >> "{log}"\necho "no chirp io proxy" >&2\nexit 1\n'
    )
    signal = ChirpLaneYieldSignal(stub)
    signal.advertise(True)
    signal.advertise(False)
    signal.advertise(True)
    assert len(log.read_text().splitlines()) == 1, "signal kept invoking chirp"
    stderr = capsys.readouterr().err
    assert stderr.count("going inert") == 1
    assert "no chirp io proxy" in stderr


def test_missing_binary_is_loud_once_then_inert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    signal = ChirpLaneYieldSignal(tmp_path / "never-exists")
    signal.advertise(True)
    signal.advertise(True)
    assert capsys.readouterr().err.count("going inert") == 1


def test_relative_chirp_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ChirpLaneYieldSignal(Path("relative/condor_chirp"))


def test_resolution_outside_a_job_is_silently_inert(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(CHIRP_CONFIG_ENVIRONMENT_VARIABLE, raising=False)
    assert type(resolve_lane_yield_signal()) is InertLaneYieldSignal
    assert capsys.readouterr().err == ""


def test_resolution_inside_a_job_without_chirp_is_loudly_inert(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cooperative lane that cannot advertise runs with `never`
    semantics — safe, but the operator should be told why."""
    monkeypatch.setenv(CHIRP_CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "cfg"))
    monkeypatch.setenv("PATH", str(tmp_path))
    assert type(resolve_lane_yield_signal()) is InertLaneYieldSignal
    assert "condor_chirp is not on" in capsys.readouterr().err


def test_resolution_inside_a_chirp_capable_job_finds_the_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _stub_chirp(tmp_path, "exit 0\n")
    monkeypatch.setenv(CHIRP_CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "cfg"))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin")
    signal = resolve_lane_yield_signal()
    assert type(signal) is ChirpLaneYieldSignal
