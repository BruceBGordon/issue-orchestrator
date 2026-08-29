"""Parsing the two host probes, against their real output shapes.

These fabricate ``top`` and ``ps`` text rather than sampling the machine: the
behavior under test is the reading, and a test that needed a real busy host
would only pass on a machine already in trouble.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.host_load_probe import (
    HostProbeError,
    build_snapshot,
    parse_elapsed_seconds,
    parse_idle_percent,
    parse_process_rows,
)

_TOP_OUTPUT = """Processes: 765 total, 6 running, 759 sleeping, 7597 threads
2026/08/29 06:15:00
Load Avg: 205.04, 190.11, 143.06
CPU usage: 87.50% user, 12.50% sys, 0.00% idle
SharedLibs: 1074M resident, 174M data, 126M linkedit.
PhysMem: 55G used (5077M wired, 29G compressor), 7946M unused.
"""

_PS_OUTPUT = """  PID  PPID USER              %CPU     ELAPSED COMMAND
    1     0 root               1.9 05-13:59:02 /sbin/launchd
41635     1 brucegordon       87.3    01:58:15 /opt/homebrew/bin/python3.14 -c while True: pass
 1468     1 _windowserver      2.4       00:56 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer -daemon
"""


class TestSnapshot:
    def test_reads_idle_and_every_process_row(self) -> None:
        snapshot = build_snapshot(_TOP_OUTPUT, _PS_OUTPUT)

        assert snapshot.idle_percent == pytest.approx(0.0)
        assert [row.pid for row in snapshot.processes] == [1, 41635, 1468]

    def test_a_row_carries_the_whole_column_set(self) -> None:
        burner = build_snapshot(_TOP_OUTPUT, _PS_OUTPUT).processes[1]

        assert burner.ppid == 1
        assert burner.user == "brucegordon"
        assert burner.cpu_percent == pytest.approx(87.3)
        assert burner.elapsed == "01:58:15"
        assert burner.elapsed_seconds == 7095
        assert burner.command.endswith("-c while True: pass")

    def test_the_command_column_keeps_its_spaces(self) -> None:
        rows = build_snapshot(_TOP_OUTPUT, _PS_OUTPUT).processes

        assert rows[2].command.endswith("WindowServer -daemon")


class TestIdlePercent:
    @pytest.mark.parametrize("idle", [0.0, 3.25, 71.96, 100.0])
    def test_reads_the_cpu_usage_line(self, idle: float) -> None:
        output = f"CPU usage: 1.00% user, 2.00% sys, {idle}% idle \n"

        assert parse_idle_percent(output) == pytest.approx(idle)

    def test_unparseable_output_is_an_error_not_an_idle_host(self) -> None:
        """A broken probe must be loud; silence is what the incident already was."""
        with pytest.raises(HostProbeError, match="CPU usage"):
            parse_idle_percent("top: command produced nothing useful\n")


class TestElapsedField:
    @pytest.mark.parametrize(
        ("elapsed", "seconds"),
        [
            ("00:56", 56),
            ("11:48:38", 42518),
            ("03-11:48:38", 301718),
            ("05-13:59:02", 482342),
        ],
    )
    def test_supported_formats(self, elapsed: str, seconds: int) -> None:
        assert parse_elapsed_seconds(elapsed) == seconds

    @pytest.mark.parametrize("elapsed", ["yesterday", "12", ""])
    def test_unparseable_field_is_an_error(self, elapsed: str) -> None:
        with pytest.raises(HostProbeError):
            parse_elapsed_seconds(elapsed)


class TestProcessRows:
    def test_empty_table_is_an_error(self) -> None:
        with pytest.raises(HostProbeError, match="no process rows"):
            parse_process_rows("  PID  PPID USER              %CPU     ELAPSED COMMAND\n")

    def test_a_short_row_is_an_error(self) -> None:
        with pytest.raises(HostProbeError, match="unparseable ps row"):
            parse_process_rows(
                "  PID  PPID USER              %CPU     ELAPSED COMMAND\n1 0 root\n"
            )
