"""The chirp yield transport: acknowledged publications and the
LIBEXEC resolution path (B1, #7134 review)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor import chirp_yield_signal as chirp_module
from issue_orchestrator.domain.lane_execution import (
    LaneExecutorUnavailableError,
)
from issue_orchestrator.adapters.condor.chirp_yield_signal import (
    CHIRP_CONFIG_ENVIRONMENT_VARIABLE,
    ChirpYieldTransport,
    resolve_lane_yield_transport,
)


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture(autouse=True)
def take_the_production_backstop_out_of_these_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None of these tests is about the ten-second backstop, so none of
    them should be able to lose a race against it.

    `_CHIRP_TIMEOUT_SECONDS` bounds a chirp invocation in production, where
    ten seconds is generous for a binary that sets one job attribute. In a
    unit test it bounds a `#!/bin/sh` stub that appends a line to a file, and
    on a loaded machine that fork is not reliably faster than the backstop:
    `test_resolution_falls_back_to_the_pools_libexec` failed in the gate with
    `publish(False)` returning False, having asserted nothing about timing.
    Sibling stubs in the lane-executor suite were measured taking 16-18s under
    the same conditions.

    Raising it here costs no coverage, because nothing in this module asserts
    the backstop's behaviour — there is no test that a slow chirp is abandoned
    after `_CHIRP_TIMEOUT_SECONDS`, which is a real gap and worth writing
    separately with an injected clock rather than a real sleep.

    Same intent as `_unhurried_cancellation` in the lane-executor suite: take
    the clock out of the tests that are not about it. The lasting fix is
    #7162, which removes the fork.
    """
    monkeypatch.setattr(chirp_module, "_CHIRP_TIMEOUT_SECONDS", 300.0)


def test_publications_carry_the_attribute_and_report_acknowledgment(
    tmp_path: Path,
) -> None:
    log = tmp_path / "calls.log"
    chirp = _stub(tmp_path / "condor_chirp", f'echo "$@" >> "{log}"\nexit 0\n')
    transport = ChirpYieldTransport(chirp)
    assert transport.publish(True) is True
    assert transport.publish(False) is True
    assert log.read_text().splitlines() == [
        "set_job_attr SafeToSuspend True",
        "set_job_attr SafeToSuspend False",
    ]


def test_rejected_publication_reports_false_with_the_mechanism_detail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chirp = _stub(
        tmp_path / "condor_chirp", 'echo "no io proxy" >&2\nexit 1\n'
    )
    assert ChirpYieldTransport(chirp).publish(True) is False
    assert "no io proxy" in capsys.readouterr().err


def test_missing_binary_reports_false(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ChirpYieldTransport(tmp_path / "absent").publish(False) is False
    assert "invocation failed" in capsys.readouterr().err


def test_relative_chirp_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ChirpYieldTransport(Path("relative/condor_chirp"))


def test_resolution_outside_a_job_is_silently_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(CHIRP_CONFIG_ENVIRONMENT_VARIABLE, raising=False)
    assert resolve_lane_yield_transport() is None
    assert capsys.readouterr().err == ""


def test_resolution_prefers_path_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub(tmp_path / "condor_chirp", "exit 0\n")
    monkeypatch.setenv(CHIRP_CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "cfg"))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin")
    transport = resolve_lane_yield_transport()
    assert type(transport) is ChirpYieldTransport


def test_resolution_falls_back_to_the_pools_libexec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B1 (#7134): the macOS tarball ships condor_chirp in LIBEXEC,
    absent from PATH — resolution asks the pool's own configuration
    through the same tool boundary lanes submit through."""
    binaries = tmp_path / "bin"
    libexec = tmp_path / "libexec"
    binaries.mkdir()
    libexec.mkdir()
    chirp = _stub(libexec / "condor_chirp", "exit 0\n")
    _stub(binaries / "condor_config_val", f'echo "{libexec}"\nexit 0\n')

    class _Tools:
        query = binaries / "condor_q"
        pool_config = None

    monkeypatch.setattr(
        chirp_module.CondorTools, "resolve", staticmethod(lambda: _Tools())
    )
    monkeypatch.setenv(CHIRP_CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "cfg"))
    monkeypatch.setenv("PATH", str(binaries))
    transport = resolve_lane_yield_transport()
    assert type(transport) is ChirpYieldTransport
    log = tmp_path / "calls.log"
    _stub(chirp, f'echo "$@" >> "{log}"\nexit 0\n')
    assert transport.publish(False) is True
    assert "SafeToSuspend False" in log.read_text()


def test_resolution_inside_a_job_with_no_chirp_anywhere_is_loudly_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        chirp_module.CondorTools,
        "resolve",
        staticmethod(
            lambda: (_ for _ in ()).throw(
                LaneExecutorUnavailableError("no pool")
            )
        ),
    )
    monkeypatch.setenv(CHIRP_CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "cfg"))
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_lane_yield_transport() is None
    stderr = capsys.readouterr().err
    assert "condor_chirp was not" in stderr
    assert "will not be frozen" in stderr
