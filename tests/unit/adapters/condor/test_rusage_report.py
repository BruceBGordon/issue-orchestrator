"""The exec shim's CPU side channel: both halves of one format."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.rusage_report import (
    RUSAGE_FILE_NAME,
    busy_cores,
    compile_rusage_capture,
    measure_busy_cores,
    read_cpu_seconds,
)

# Real `times` output captured from the three shells a lane can land
# on. The shape is fixed by POSIX; only the fraction's precision
# varies, which is exactly what the parser must tolerate.
_BASH_32 = "0m0.001s 0m0.002s\n0m0.361s 0m0.014s\n"  # macOS /bin/sh
_DASH = "0m0.000000s 0m0.000000s\n0m0.200000s 0m0.000000s\n"  # Linux /bin/sh
_ZSH = "0m0.00s 0m0.00s\n0m0.01s 0m0.00s\n"


def _report(tmp_path: Path, text: str) -> Path:
    path = tmp_path / RUSAGE_FILE_NAME
    path.write_text(text, encoding="utf-8")
    return path


def test_children_line_is_read_not_the_shell_line(tmp_path: Path) -> None:
    """POSIX puts the shell's own CPU first and the children's second.
    Reading line one would report the shim's own microseconds as the
    lane's entire demand."""
    assert read_cpu_seconds(_report(tmp_path, _BASH_32)) == pytest.approx(0.375)


def test_every_shell_precision_parses(tmp_path: Path) -> None:
    assert read_cpu_seconds(_report(tmp_path, _DASH)) == pytest.approx(0.2)
    assert read_cpu_seconds(_report(tmp_path, _ZSH)) == pytest.approx(0.01)


def test_minutes_are_not_dropped(tmp_path: Path) -> None:
    """A long, wide lane spends minutes of CPU: test-unit at 8 busy
    cores for 4 minutes reports 32m of user time. Parsing seconds
    only would report 32 seconds and shrink the lane to a single
    core."""
    text = "0m0.001s 0m0.002s\n32m12.500s 1m30.000s\n"
    assert read_cpu_seconds(_report(tmp_path, text)) == pytest.approx(
        32 * 60 + 12.5 + 60 + 30.0
    )


def test_absent_report_is_not_an_error(tmp_path: Path) -> None:
    """A lane the scheduler removed at its deadline never reaches its
    shim's report. Absence means unmeasured, which is a legitimate
    state, not corruption."""
    assert read_cpu_seconds(tmp_path / RUSAGE_FILE_NAME) is None


def test_garbled_report_is_loud(tmp_path: Path) -> None:
    """A report that EXISTS but does not parse means the shim contract
    is broken; silently returning None would hide that forever."""
    for garbage in (
        "",
        "not times output at all\n",
        "0m0.001s 0m0.002s\n",  # shell line only, children truncated
        "0m0.001s 0m0.002s\nuser 0.4 sys 0.1\n",
        "0m0.001s 0m0.002s\n0mABCs 0m0.014s\n",
    ):
        with pytest.raises(ValueError):
            read_cpu_seconds(_report(tmp_path, garbage))


def test_busy_cores_divides_cpu_by_runtime() -> None:
    assert busy_cores(64.0, 8.0) == pytest.approx(8.0)
    assert busy_cores(0.85, 1.0) == pytest.approx(0.85)


def test_zero_runtime_abstains_instead_of_dividing() -> None:
    """The scheduler's event log carries whole-second timestamps, so a
    lane finishing inside one second reports runtime 0.0. That is a
    clock too coarse to divide by, not a lane using infinite cores."""
    assert busy_cores(0.4, 0.0) is None
    assert busy_cores(0.0, 0.0) is None


def test_a_lane_that_burns_no_cpu_measures_zero_not_nothing() -> None:
    """A provider-wait lane genuinely uses ~0 cores. That is a
    measurement, and must stay distinct from 'not measured'."""
    assert busy_cores(0.0, 30.0) == 0.0


def test_capture_fragment_is_a_real_shell_measurement(tmp_path: Path) -> None:
    """End-to-end through /bin/sh: the emitted fragment must produce a
    report this module's own parser reads back as the lane's CPU. A
    hand-written expectation would let the two halves drift apart."""
    report = tmp_path / RUSAGE_FILE_NAME
    # Fixed WORK, not fixed wall time: a busy-for-N-seconds loop burns
    # less than N CPU-seconds on a contended machine, which would turn
    # any lower bound here into a load test.
    burn = "total = 0\nfor index in range(3_000_000):\n    total += index * index\n"
    script = tmp_path / "shim.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"{sys.executable} -c {shlex.quote(burn)}\n"
        "__lane_status=$?\n"
        f"{compile_rusage_capture(report)}"
        'exit "$__lane_status"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    assert subprocess.run([str(script)], check=False).returncode == 0

    cpu_seconds = read_cpu_seconds(report)
    assert cpu_seconds is not None
    # The lower bound proves the children's line was read (the shell's
    # own line is microseconds); the upper bound catches a parser that
    # dropped a unit or multiplied minutes into the result.
    assert 0.05 < cpu_seconds < 60.0, cpu_seconds


def test_capture_fragment_requires_an_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        compile_rusage_capture(Path("relative/lane.rusage"))


def test_capture_fragment_quotes_awkward_directories(tmp_path: Path) -> None:
    """Run directories are scheduler-chosen temporaries; an unquoted
    path with a space would redirect into the wrong file (or two)."""
    awkward = tmp_path / "lane dir; rm -rf x" / RUSAGE_FILE_NAME
    fragment = compile_rusage_capture(awkward)
    assert str(awkward) in fragment
    assert fragment == "{ times; } >" + shlex.quote(str(awkward)) + "\n"


def test_capture_wraps_the_special_builtin_in_a_group() -> None:
    """`times` is a POSIX special built-in, and a redirection error on
    one may abort a non-interactive shell — which would swallow the
    lane's exit status the moment the run directory turned unwritable.
    Redirecting a command GROUP makes the same failure an ordinary
    failed command, which is also why no `|| :` guard is needed."""
    fragment = compile_rusage_capture(Path("/tmp/lane.rusage"))
    assert fragment.startswith("{ times; } >")
    assert "|| :" not in fragment


def test_an_unusable_report_abstains_loudly_without_failing_the_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one place that decides what a broken report means. It must
    not raise — a lane that ran correctly may not fail over its own
    instrumentation — and it must not be silent either, or the loop
    stays inert forever with nobody the wiser. The raw report goes
    into the message because the run directory holding it is deleted
    moments later on a clean completion."""
    report = _report(tmp_path, "0m0.001s 0m0.002s\ntruncated")
    assert measure_busy_cores(report, 30.0, "test-unit") is None
    warning = capsys.readouterr().err
    assert warning.startswith("[lane-cpu] WARNING test-unit:")
    assert "unusable" in warning
    assert "truncated" in warning


def test_an_absent_report_on_a_completed_lane_is_loud(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """C (#7136 review): this function is only reached for a lane that
    ran to its own exit, so a missing report means the shim never ran
    its capture — a swallowed redirection, a lost run directory, a
    shim that no longer measures. Reading that as 'normal' let a
    broken shim disable CPU learning forever with nobody the wiser.
    Non-fatal, but never silent."""
    missing = tmp_path / RUSAGE_FILE_NAME
    assert measure_busy_cores(missing, 30.0, "test-unit") is None
    warning = capsys.readouterr().err
    assert warning.startswith("[lane-cpu] WARNING test-unit:")
    assert str(missing) in warning


def test_a_coarse_runtime_abstains_quietly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one non-failure way to get no number: the report is there
    and healthy, the runtime is simply too coarse to divide by. That
    is not an instrumentation defect and must not be warned about, or
    every trivial lane cries wolf."""
    report = _report(tmp_path, _BASH_32)
    assert measure_busy_cores(report, 0.0, "test-unit") is None
    assert capsys.readouterr().err == ""


def test_a_usable_report_becomes_busy_cores(tmp_path: Path) -> None:
    report = _report(tmp_path, "0m0.001s 0m0.002s\n0m30.000s 0m30.000s\n")
    assert measure_busy_cores(report, 30.0, "test-unit") == pytest.approx(2.0)
