"""The gate-entry host check must name what the 2026-08-29 post-mortem missed.

Policy only — snapshots are built directly here rather than parsed from probe
text, so these pin what the check *decides* (warn or stay quiet, debris or
daemon) independently of how the host is sampled.
"""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from issue_orchestrator.entrypoints.cli_tools.host_load_preflight import (
    emit,
    is_fixture_signature,
    main,
    report_lines,
    stray_debris,
    top_consumers,
)
from issue_orchestrator.execution.host_load_probe import (
    HostProbeError,
    HostSnapshot,
    ProcessRow,
)

OWNER = "brucegordon"
_MODULE = "issue_orchestrator.entrypoints.cli_tools.host_load_preflight"

# Run in a child interpreter: it reports a busy host (so there is something to
# print), then the scenario breaks stderr in a different way before main().
_DRIVER_PREAMBLE = """
import io
from issue_orchestrator.entrypoints.cli_tools import host_load_preflight as h
from issue_orchestrator.execution.host_load_probe import HostSnapshot, ProcessRow

h.probe_host = lambda: HostSnapshot(
    0.0, (ProcessRow(1, 9001, 't', 99.0, '00:42', 42, 'python3 -c pass'),)
)
h.current_owner = lambda: 't'

def _wrapper(*, write_fails=False, flush_fails=False):
    class Raw(io.RawIOBase):
        def writable(self):
            return True

        def write(self, b):
            if write_fails:
                raise OSError(5, 'write blew up')
            return len(b)

        def flush(self):
            if flush_fails:
                raise OSError(5, 'flush blew up')

    return io.TextIOWrapper(io.BufferedWriter(Raw()))
"""


def _row(
    *,
    pid: int,
    command: str,
    cpu_percent: float = 0.0,
    ppid: int = 9001,
    user: str = OWNER,
    elapsed: str = "00:42",
    elapsed_seconds: int = 42,
) -> ProcessRow:
    return ProcessRow(
        pid=pid,
        ppid=ppid,
        user=user,
        cpu_percent=cpu_percent,
        elapsed=elapsed,
        elapsed_seconds=elapsed_seconds,
        command=command,
    )


def _snapshot(idle_percent: float, *rows: ProcessRow) -> HostSnapshot:
    return HostSnapshot(idle_percent=idle_percent, processes=rows)


# Parented and young: load that no debris rule would ever mention, so a test
# using these proves the busy-host section named them.
def _burner(pid: int) -> ProcessRow:
    return _row(
        pid=pid,
        cpu_percent=87.3,
        command="/opt/homebrew/bin/python3.14 -c while True: pass",
    )


# The incident's actual shape: orphaned to launchd at 04:16, sampled at 06:15.
def _orphaned_burner(pid: int) -> ProcessRow:
    return _row(
        pid=pid,
        ppid=1,
        cpu_percent=87.3,
        command="/opt/homebrew/bin/python3.14 -c while True: pass",
        elapsed="01:58:15",
        elapsed_seconds=7095,
    )


_DAEMONS = (
    _row(pid=1, ppid=0, user="root", cpu_percent=1.9, command="/sbin/launchd",
         elapsed="05-13:59:02", elapsed_seconds=482342),
    _row(pid=1468, ppid=1, user="_windowserver", cpu_percent=2.4,
         command="/System/Library/PrivateFrameworks/SkyLight.framework/Resources/"
                 "WindowServer -daemon", elapsed="05-13:06:40",
         elapsed_seconds=479200),
    _row(pid=97463, ppid=1,
         command="/usr/libexec/mdworker_shared -s mdworker -c MDSImporterWorker",
         elapsed="03-11:48:38", elapsed_seconds=301718),
    _row(pid=62434, ppid=1, cpu_percent=0.1,
         command="/bin/zsh -c until [ -s out ]; do sleep 3; done",
         elapsed="01-10:30:45", elapsed_seconds=124245),
)


class TestBusyHostWarning:
    def test_names_the_consumers_when_idle_collapses(self) -> None:
        snapshot = _snapshot(0.0, *_DAEMONS, *(_burner(41600 + i) for i in range(3)))

        lines = report_lines(snapshot, owner=OWNER)

        assert lines, "a host at 0% idle must not pass in silence"
        assert "BUSY HOST" in lines[0]
        assert "0.0% CPU idle" in lines[0]
        report = "\n".join(lines)
        assert "STRAY TEST DEBRIS" not in report, "these burners are not debris"
        assert "41600" in report and "41602" in report
        assert "python3.14 -c while True: pass" in report
        assert "87.3" in report

    def test_stays_silent_on_a_healthy_host(self) -> None:
        assert report_lines(_snapshot(94.0, *_DAEMONS), owner=OWNER) == ()

    def test_the_threshold_is_the_only_trigger(self) -> None:
        """Load average is not consulted: macOS counts parked threads in it."""
        assert report_lines(_snapshot(50.0, *_DAEMONS), owner=OWNER) == ()
        assert report_lines(_snapshot(49.9, *_DAEMONS), owner=OWNER) != ()

    def test_consumers_are_ranked_and_idle_processes_excluded(self) -> None:
        snapshot = _snapshot(0.0, *_DAEMONS, _burner(41600), _burner(41601))

        consumers = top_consumers(snapshot)

        assert [row.cpu_percent for row in consumers] == sorted(
            (row.cpu_percent for row in consumers), reverse=True
        )
        assert all(row.cpu_percent > 0.0 for row in consumers)
        assert 97463 not in {row.pid for row in consumers}

    def test_report_says_it_did_not_kill_anything(self) -> None:
        snapshot = _snapshot(0.0, *_DAEMONS, _burner(41600))

        assert "Nothing was killed" in report_lines(snapshot, owner=OWNER)[-1]

    def test_the_incident_itself_reports_both_findings(self) -> None:
        """06:15 on 2026-08-29: twenty orphaned burners, 0% idle."""
        snapshot = _snapshot(
            0.0, *_DAEMONS, *(_orphaned_burner(41600 + index) for index in range(20))
        )

        report = "\n".join(report_lines(snapshot, owner=OWNER))

        assert "BUSY HOST" in report
        assert "POSSIBLE STRAY TEST DEBRIS: 20 orphaned" in report
        assert "41619" in report, "the debris table must name every orphan"


class TestStrayDebris:
    def test_orphaned_fixtures_are_named_even_on_an_idle_host(self) -> None:
        """The nine leaked ``signal.pause`` waiters burned no CPU at all."""
        waiter = _row(
            pid=18194,
            ppid=1,
            command="/opt/homebrew/bin/python3.14 -c import signal; signal.pause()",
            elapsed="03-11:48:38",
            elapsed_seconds=301718,
        )
        snapshot = _snapshot(96.0, *_DAEMONS, waiter)

        lines = report_lines(snapshot, owner=OWNER)

        assert lines, "idle debris stays invisible forever if idle gates the report"
        assert "POSSIBLE STRAY TEST DEBRIS" in lines[0]
        assert "18194" in "\n".join(lines)

    def test_system_daemons_are_not_debris(self) -> None:
        """mdworker takes ``-c``; zsh poll loops run ``sleep``. Neither is ours."""
        assert stray_debris(_snapshot(96.0, *_DAEMONS), owner=OWNER) == ()

    def test_another_users_orphan_is_not_ours_to_name(self) -> None:
        foreign = _row(
            pid=18194,
            ppid=1,
            user="someone",
            command="/usr/bin/python3 -c while True: pass",
            elapsed_seconds=301718,
        )

        assert stray_debris(_snapshot(96.0, foreign), owner=OWNER) == ()

    def test_a_young_orphan_may_still_belong_to_a_live_run(self) -> None:
        young = _row(
            pid=18194,
            ppid=1,
            command="/usr/bin/python3 -c while True: pass",
            elapsed="04:12",
            elapsed_seconds=252,
        )

        assert stray_debris(_snapshot(96.0, young), owner=OWNER) == ()

    def test_a_parented_fixture_belongs_to_its_parent(self) -> None:
        parented = _row(
            pid=18194,
            command="/usr/bin/python3 -c while True: pass",
            elapsed_seconds=301718,
        )

        assert stray_debris(_snapshot(96.0, parented), owner=OWNER) == ()

    @pytest.mark.parametrize(
        "command",
        [
            "/opt/homebrew/bin/python3.14 -c while True: pass",
            "python -c import signal; signal.pause()",
            "/opt/homebrew/Cellar/python@3.14/Resources/Python.app/Contents/MacOS/"
            "Python -c import time",
            "/bin/sleep 3600",
            "sleep 300",
        ],
    )
    def test_fixture_signatures(self, command: str) -> None:
        assert is_fixture_signature(command)

    @pytest.mark.parametrize(
        "command",
        [
            "/sbin/launchd",
            "/usr/libexec/mdworker_shared -s mdworker -c MDSImporterWorker",
            "/bin/zsh -c until [ -s out ]; do sleep 3; done",
            "/opt/homebrew/bin/python3.14 /path/to/real_script.py",
            "/opt/homebrew/bin/python3.14 -c",
            "",
        ],
    )
    def test_non_fixture_signatures(self, command: str) -> None:
        assert not is_fixture_signature(command)


class TestOutput:
    def test_report_goes_to_the_given_stream_with_a_greppable_prefix(self) -> None:
        stream = io.StringIO()

        emit(stream, ("first", "second"))

        assert stream.getvalue() == "[host-preflight] first\n[host-preflight] second\n"

    @pytest.mark.parametrize(
        ("scenario", "breakage"),
        [
            pytest.param(
                "closed-fd-2",
                "import os; os.close(2)",
                id="closed-fd-2",
            ),
            pytest.param(
                "raising-write",
                "import sys; sys.stderr = _wrapper(write_fails=True)",
                id="raising-write",
            ),
            pytest.param(
                "raising-final-flush",
                "import sys; sys.stderr = _wrapper(flush_fails=True)",
                id="raising-final-flush",
            ),
        ],
    )
    def test_output_failure_never_costs_the_gate_its_exit_code(
        self, scenario: str, breakage: str
    ) -> None:
        """Real subprocesses, because the exit code is the thing under test.

        A fake stream inside this process can show that ``emit`` returns; it
        cannot show what CPython does with a still-broken stream during
        interpreter shutdown, which is where the 120s come from — and it was a
        mock-stream test that let the ``closed-fd-2`` regression through.
        """
        driver = _DRIVER_PREAMBLE + breakage + "\nh.main()\n"

        completed = subprocess.run(
            [sys.executable, "-c", driver],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )

        assert completed.returncode == 0, (
            f"{scenario}: the preflight failed the gate because it could not "
            f"print (exit {completed.returncode}); stderr={completed.stderr!r}"
        )

    def test_a_real_hung_up_reader_still_exits_zero(self) -> None:
        """The reader closes the pipe before the report is written."""
        process = subprocess.Popen(
            [sys.executable, "-c", _DRIVER_PREAMBLE + "h.main()\n"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stderr is not None
            process.stderr.close()

            assert process.wait(timeout=60) == 0, (
                "the preflight failed the gate because it could not print"
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)

    def test_long_commands_are_truncated_but_keep_the_arguments(self) -> None:
        buried = _row(
            pid=99999,
            cpu_percent=91.0,
            command="/very/long/framework/path/that/goes/on/forever/Python -c "
            + "x" * 400,
        )

        table_row = report_lines(_snapshot(0.0, buried), owner=OWNER)[2]

        assert len(table_row) < 160
        assert "Python -c" in table_row, "truncation must not eat the identifying argv"

    def test_main_reports_that_it_has_no_signal_off_darwin(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(f"{_MODULE}.sys.platform", "linux")

        main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "macOS-only" in captured.err

    def test_main_survives_a_probe_that_cannot_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The gate must never be blocked by its own diagnostics."""
        monkeypatch.setattr(f"{_MODULE}.sys.platform", "darwin")

        def explode() -> None:
            raise HostProbeError("top exited 1")

        monkeypatch.setattr(f"{_MODULE}.probe_host", explode)

        main()

        assert "host probe unavailable" in capsys.readouterr().err

    def test_main_writes_the_report_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """stderr is where the publish gate's validation-stderr.log comes from."""
        monkeypatch.setattr(f"{_MODULE}.sys.platform", "darwin")
        monkeypatch.setattr(
            f"{_MODULE}.probe_host",
            lambda: _snapshot(0.0, _burner(41600)),
        )
        monkeypatch.setattr(f"{_MODULE}.current_owner", lambda: OWNER)

        main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "[host-preflight] BUSY HOST" in captured.err
        assert "41600" in captured.err

    def test_an_unresolvable_owner_does_not_break_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Owner lookup is a host fact too, and fails the same typed way."""
        monkeypatch.setattr(f"{_MODULE}.sys.platform", "darwin")
        monkeypatch.setattr(f"{_MODULE}.probe_host", lambda: _snapshot(94.0))

        def explode() -> str:
            raise HostProbeError("no passwd entry for uid 1000")

        monkeypatch.setattr(f"{_MODULE}.current_owner", explode)

        main()

        assert "host probe unavailable" in capsys.readouterr().err
