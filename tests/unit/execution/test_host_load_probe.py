"""Parsing the two host probes, against their real output shapes.

These fabricate ``top`` and ``ps`` text rather than sampling the machine: the
behavior under test is the reading, and a test that needed a real busy host
would only pass on a machine already in trouble.
"""

from __future__ import annotations

import subprocess

import pytest

from issue_orchestrator.execution.host_load_probe import (
    HostProbeError,
    build_snapshot,
    parse_elapsed_seconds,
    parse_idle_percent,
    parse_process_rows,
    probe_host,
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

    def test_a_comma_decimal_locale_refuses_to_parse_instead_of_reading_clean(
        self,
    ) -> None:
        """The dangerous failure: a wedged host reading as nearly idle.

        Under a comma-decimal locale a loose number pattern captures the tail
        of ``49,90% idle`` as ``90`` — 90% idle on a host with 49.9% left. The
        env pin should stop this arriving; refusing to parse it is the backstop
        that keeps the silent-clean reading impossible either way.
        """
        comma_output = "CPU usage: 25,00% user, 25,10% sys, 49,90% idle \n"

        with pytest.raises(HostProbeError, match="CPU usage"):
            parse_idle_percent(comma_output)

    @pytest.mark.parametrize(
        "idle_token",
        [
            ".",
            ".5",
            "",
            "nan",
            "+90",  # a sign is not part of the number this field prints
            "abc90",  # leading garbage: the token must match whole
            "-90",
            "1.2.3",  # a second dot makes the whole token unreadable
        ],
    )
    def test_a_partial_token_is_refused_rather_than_half_read(
        self, idle_token: str
    ) -> None:
        """Partial matches are the danger, not absent ones.

        Every one of these ends in digits that would parse to a plausible,
        wrong, and reassuringly high idle figure — silence exactly when the
        machine is on fire.
        """
        with pytest.raises(HostProbeError, match="CPU usage"):
            parse_idle_percent(
                f"CPU usage: 1.00% user, 2.00% sys, {idle_token}% idle \n"
            )

    def test_the_real_macos_line_still_reads(self) -> None:
        line = "CPU usage: 17.99% user, 10.4% sys, 71.96% idle \n"

        assert parse_idle_percent(line) == pytest.approx(71.96)

    @pytest.mark.parametrize("idle", ["150.00", "101"])
    def test_a_reading_outside_0_100_is_not_a_percentage(self, idle: str) -> None:
        with pytest.raises(HostProbeError, match="not a percentage"):
            parse_idle_percent(f"CPU usage: 1.00% user, 2.00% sys, {idle}% idle \n")


class TestElapsedField:
    @pytest.mark.parametrize(
        ("elapsed", "seconds"),
        [
            ("00:56", 56),
            ("11:48:38", 42518),
            ("03-11:48:38", 301718),
            ("05-13:59:02", 482342),
            ("0-11:48:38", 42518),  # a zero day prefix is well-formed with hh:mm:ss
        ],
    )
    def test_supported_formats(self, elapsed: str, seconds: int) -> None:
        assert parse_elapsed_seconds(elapsed) == seconds

    @pytest.mark.parametrize(
        "elapsed",
        [
            "yesterday",
            "12",
            "",
            "1:2:3:4",
            "03-11:48",  # ps never prints a day field without hh:mm:ss
            "0-2:3",  # ...and a zero day prefix is still a day prefix
            "0-11:48",
            "-1:00",
            "11:99:38",  # sexagesimal by construction
            "11:48:99",
            "99999999-00:00:00",
        ],
    )
    def test_unparseable_field_is_an_error(self, elapsed: str) -> None:
        """Never a raw ``ValueError``: the CLI above must still exit 0."""
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

    @pytest.mark.parametrize(
        ("row", "match"),
        [
            ("nine 0 root 1.9 00:56 /sbin/launchd", "unparseable ps PID"),
            ("-7 0 root 1.9 00:56 /sbin/launchd", "negative ps PID"),
            ("1 nine root 1.9 00:56 /sbin/launchd", "unparseable ps PPID"),
            ("1 0 root nine 00:56 /sbin/launchd", "unparseable ps %CPU"),
            ("1 0 root -3.0 00:56 /sbin/launchd", "out-of-range ps %CPU"),
            ("1 0 root nan 00:56 /sbin/launchd", "out-of-range ps %CPU"),
            ("1 0 root inf 00:56 /sbin/launchd", "out-of-range ps %CPU"),
            ("1 0 root 1.9 later /sbin/launchd", "unparseable ps ETIME"),
        ],
    )
    def test_malformed_values_stay_inside_the_typed_boundary(
        self, row: str, match: str
    ) -> None:
        """Every conversion failure leaves as ``HostProbeError``, never raw.

        ``float`` happily returns ``nan`` and ``inf``; neither is a percentage.
        """
        with pytest.raises(HostProbeError, match=match):
            parse_process_rows(
                f"  PID  PPID USER              %CPU     ELAPSED COMMAND\n{row}\n"
            )


class TestProbeExecution:
    """The subprocess boundary: locale pinning and decoding."""

    def test_probes_run_under_a_pinned_c_locale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix for comma decimals is at the source, not in the regex."""
        seen: list[dict[str, str]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            env = kwargs["env"]
            assert isinstance(env, dict)
            seen.append(env)
            stdout = _TOP_OUTPUT if args[0] == "top" else _PS_OUTPUT
            return subprocess.CompletedProcess(args, 0, stdout.encode(), b"")

        monkeypatch.setattr(subprocess, "run", fake_run)

        probe_host()

        assert len(seen) == 2, "both probes must be pinned, not just top"
        for env in seen:
            assert env["LC_ALL"] == "C"
            assert env["LANG"] == "C"
            assert "PATH" in env, "the pin must extend the environment, not replace it"

    def test_the_process_table_is_read_at_unlimited_width(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COMMAND is the column this probe exists for (#7142).

        procps truncates it to ``$COLUMNS`` even into a pipe, so a stray
        ``python -c 'while True: pass'`` would arrive as a bare interpreter
        path: every row still present, every command a lie, and the debris
        section silently empty. The CI failure that found this was in the test
        helper; the same ``ps`` call is here, one layer from the gate.
        """
        seen: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            env = kwargs["env"]
            assert isinstance(env, dict)
            seen.append((args, env))
            stdout = _TOP_OUTPUT if args[0] == "top" else _PS_OUTPUT
            return subprocess.CompletedProcess(args, 0, stdout.encode(), b"")

        monkeypatch.setenv("COLUMNS", "80")
        monkeypatch.setenv("LINES", "24")
        monkeypatch.setattr(subprocess, "run", fake_run)

        probe_host()

        ps_args, ps_env = next((a, e) for a, e in seen if a[0] == "ps")
        assert "-ww" in ps_args, "ps must be told to ignore the terminal width"
        for _, env in seen:
            assert "COLUMNS" not in env, "and must not inherit one either"
            assert "LINES" not in env

    def test_undecodable_output_is_a_probe_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A process with non-UTF-8 argv must not raise UnicodeDecodeError."""

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(args, 0, b"\xff\xfe not utf-8", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(HostProbeError, match="undecodable output"):
            probe_host()

    def test_a_probe_that_cannot_run_is_a_probe_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise FileNotFoundError(args[0])

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(HostProbeError, match="could not be run"):
            probe_host()
