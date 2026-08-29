"""The machine-state envelope: probes, containment, and record shape.

Hermetic throughout — the platform probes are exercised through their
parsers against captured output, and the sampler is exercised with a
forced platform, so no test depends on the host it runs on.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import datetime, timezone

import pytest

from issue_orchestrator.infra.machine_state import (
    MACHINE_STATE_RECORD_KEY,
    CpuTicks,
    HostMachineStateSampler,
    default_machine_state_sampler,
    describe_exception,
    idle_percent_between,
    machine_state_fields,
    parse_proc_stat_idle_percent,
    parse_proc_stat_ticks,
    read_mach_cpu_ticks,
    sample_machine_state,
    sample_machine_state_from,
    stamp_machine_state,
    unmeasured_machine_state,
)
from issue_orchestrator.ports.machine_state import MachineState, MachineStateSampler

_PROC_STAT_BEFORE = (
    "cpu  1000 0 500 8000 100 0 0 0 0 0\n"
    "cpu0 500 0 250 4000 50 0 0 0 0 0\n"
    "intr 12345\n"
)
_PROC_STAT_AFTER = (
    "cpu  1150 0 600 8700 150 0 0 0 0 0\n"
    "cpu0 575 0 300 4350 75 0 0 0 0 0\n"
    "intr 12999\n"
)


class _FakeSampler:
    """A sampler whose reading the test dictates (the required seam)."""

    def __init__(self, state: MachineState) -> None:
        self.state = state
        self.calls = 0

    def sample(self) -> MachineState:
        self.calls += 1
        return self.state


def _reading(**overrides: object) -> MachineState:
    fields: dict[str, object] = {
        "sampled_at": datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
        "loadavg_1m": 7.91,
        "loadavg_5m": 12.51,
        "loadavg_15m": 9.0,
        "cpu_idle_percent": 85.68,
        "cpu_idle_source": "top -l 1 -n 0",
        "physical_cores": 18,
        "probe_error": None,
    }
    fields.update(overrides)
    return MachineState(**fields)  # type: ignore[arg-type]


class TestPlatformProbeParsers:
    """Both platforms answer with cumulative tick counters, so one
    delta rule serves both and only the reader differs."""

    def test_proc_stat_aggregate_line_becomes_tick_counters(self) -> None:
        assert parse_proc_stat_ticks(_PROC_STAT_BEFORE) == CpuTicks(
            total=9600, idle=8100
        )

    def test_proc_stat_without_an_aggregate_line_reports_nothing(self) -> None:
        assert parse_proc_stat_ticks("intr 1\n") is None
        assert parse_proc_stat_ticks("") is None

    def test_the_delta_rule_is_shared_by_both_platforms(self) -> None:
        assert (
            idle_percent_between(
                CpuTicks(total=1000, idle=800), CpuTicks(total=2000, idle=1750)
            )
            == 95.0
        )

    @pytest.mark.skipif(
        sys.platform != "darwin", reason="the kernel counters are darwin's"
    )
    def test_darwin_kernel_counters_are_readable_and_monotonic(self) -> None:
        """FAILS (never skips) on darwin if the Mach call stops working:
        the whole point of the envelope is that the number is real."""
        first = read_mach_cpu_ticks()
        second = read_mach_cpu_ticks()
        assert first is not None and second is not None
        assert second.total >= first.total > 0
        assert second.idle >= first.idle > 0
        assert first.idle <= first.total

    def test_linux_idle_is_the_delta_between_two_proc_stat_reads(self) -> None:
        # Total ticks moved 1000 (150 user + 100 system + 700 idle +
        # 50 iowait); idle+iowait is 750 of them.
        assert (
            parse_proc_stat_idle_percent(_PROC_STAT_BEFORE, _PROC_STAT_AFTER)
            == 75.0
        )

    def test_linux_identical_reads_report_nothing_rather_than_zero(self) -> None:
        """No elapsed ticks is no measurement — never '0% idle', which
        would read as a pegged machine."""
        assert (
            parse_proc_stat_idle_percent(_PROC_STAT_BEFORE, _PROC_STAT_BEFORE)
            is None
        )

    def test_linux_garbage_reports_nothing(self) -> None:
        assert parse_proc_stat_idle_percent("intr 1\n", "intr 2\n") is None
        assert parse_proc_stat_idle_percent("cpu  a b c d e\n", _PROC_STAT_AFTER) is None


class TestHostSampler:
    def test_unsupported_platform_still_reports_load_and_cores(self) -> None:
        """The cheap syscall facts never depend on the CPU probe: an
        unsupported platform loses idle%, not the whole reading."""
        state = HostMachineStateSampler(platform="sunos5").sample()
        assert state.probe_error is None
        assert state.loadavg_1m is not None
        assert state.physical_cores is not None and state.physical_cores >= 1
        assert state.cpu_idle_percent is None
        assert "sunos5" in state.cpu_idle_source

    def test_reading_is_reused_inside_the_minimum_interval(self) -> None:
        """The probe cost is bounded by the interval, not by how many
        records a process writes (#7127)."""
        sampler = HostMachineStateSampler(
            minimum_interval_seconds=3600.0, platform="sunos5"
        )
        first = sampler.sample()
        assert sampler.sample() is first

    def test_zero_interval_always_re_reads(self) -> None:
        sampler = HostMachineStateSampler(
            minimum_interval_seconds=0.0, platform="sunos5"
        )
        assert sampler.sample() is not sampler.sample()

    def test_negative_interval_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="minimum_interval_seconds"):
            HostMachineStateSampler(minimum_interval_seconds=-1.0)

    @pytest.mark.skipif(
        sys.platform not in ("darwin",) and not sys.platform.startswith("linux"),
        reason="the real probe only exists on the two supported platforms",
    )
    def test_real_host_probe_answers_on_this_platform(self) -> None:
        """Acceptance: cpu_idle is sourced correctly per platform. This
        FAILS (never skips) on darwin/linux if the probe stops working —
        the point of the envelope is that the number is really there."""
        state = HostMachineStateSampler(minimum_interval_seconds=0.0).sample()
        assert state.probe_error is None, state.cpu_idle_source
        assert state.cpu_idle_percent is not None, state.cpu_idle_source
        assert 0.0 <= state.cpu_idle_percent <= 100.0
        if sys.platform == "darwin":
            assert "HOST_CPU_LOAD_INFO" in state.cpu_idle_source
        else:
            assert "/proc/stat" in state.cpu_idle_source

    def test_the_process_default_sampler_is_shared(self) -> None:
        """Sharing is the mechanism that makes the interval bound real."""
        assert default_machine_state_sampler() is default_machine_state_sampler()
        assert isinstance(default_machine_state_sampler(), MachineStateSampler)


class TestSamplingFailureNeverPropagates:
    """The one deliberate exception to fail-fast, owned in one place.

    Round 1 finding A: `except Exception` plus an f-string was not a
    boundary. The reviewer escaped it three ways, all reproduced here -
    each one could replace an ALREADY-DECIDED lane outcome before it was
    journaled, which is the precise harm this boundary exists to stop.
    """

    def test_a_raising_sampler_becomes_a_recorded_probe_error(self) -> None:
        class _Exploding:
            def sample(self) -> MachineState:
                raise RuntimeError("probe host melted")

        state = sample_machine_state(_Exploding())
        assert state.probe_error is not None
        assert "probe host melted" in state.probe_error
        assert state.loadavg_1m is None
        assert state.cpu_idle_percent is None

    def test_an_exception_whose_str_raises_is_still_contained(self) -> None:
        """Escape 1: rendering the error was itself unguarded, so a
        hostile __str__ escaped the boundary as a fresh exception."""

        class _Hostile(Exception):
            def __str__(self) -> str:
                raise ValueError("cannot render me")

        class _Sampler:
            def sample(self) -> MachineState:
                raise _Hostile()

        state = sample_machine_state(_Sampler())
        assert state.probe_error is not None
        assert "_Hostile" in state.probe_error

    def test_an_exception_whose_repr_also_raises_falls_back_to_its_type(
        self,
    ) -> None:
        """Every rendering attempt is contained, so the last resort is a
        name obtained without calling the exception's own code."""

        class _WhollyHostile(Exception):
            def __str__(self) -> str:
                raise ValueError("no str")

            def __repr__(self) -> str:
                raise ValueError("no repr")

        class _Sampler:
            def sample(self) -> MachineState:
                raise _WhollyHostile()

        state = sample_machine_state(_Sampler())
        assert state.probe_error == "_WhollyHostile"

    def test_an_enormous_rendering_cannot_bloat_the_record(self) -> None:
        class _Verbose(Exception):
            def __repr__(self) -> str:
                return "x" * 10_000_000

        class _Sampler:
            def sample(self) -> MachineState:
                raise _Verbose()

        state = sample_machine_state(_Sampler())
        assert state.probe_error is not None
        assert len(state.probe_error) <= 500

    def test_a_sampler_raising_system_exit_is_contained(self) -> None:
        """Escape 2: SystemExit is not an Exception, so it sailed
        through and would have replaced the gate's exit code with its
        own."""

        class _Exiting:
            def sample(self) -> MachineState:
                raise SystemExit(3)

        state = sample_machine_state(_Exiting())
        assert state.probe_error is not None
        assert "SystemExit" in state.probe_error

    @pytest.mark.skipif(
        not hasattr(signal, "setitimer"), reason="POSIX timers required"
    )
    def test_a_signal_handler_exiting_mid_sample_is_contained(self) -> None:
        """Escape 3: the asynchronous version of the same hole - a
        handler raising SystemExit while the probe is inside its sample
        window. Delivered for real with a live interval timer rather
        than simulated, because 'the sampler raised it' and 'the signal
        arrived during it' are different code paths."""

        def die(signum: int, frame: object) -> None:
            del signum, frame
            raise SystemExit("terminated mid-sample")

        class _Slow:
            def sample(self) -> MachineState:
                time.sleep(0.5)
                raise AssertionError("the alarm should have fired first")

        previous = signal.signal(signal.SIGALRM, die)
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.05)
            state = sample_machine_state(_Slow())
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

        assert state.probe_error is not None
        assert "SystemExit" in state.probe_error

    @pytest.mark.parametrize(
        "signal_type", [KeyboardInterrupt, GeneratorExit, asyncio.CancelledError]
    )
    def test_teardown_signals_are_never_contained(
        self, signal_type: type[BaseException]
    ) -> None:
        """The other half of the policy. Containing these would defeat
        a teardown rather than protect a record: the operator's Ctrl-C
        must win, and swallowing a cancellation or a generator close
        silently breaks the caller's own contract."""

        class _TornDown:
            def sample(self) -> MachineState:
                raise signal_type()

        with pytest.raises(signal_type):
            sample_machine_state(_TornDown())

    def test_acquiring_the_probe_is_inside_the_boundary_too(self) -> None:
        """Obtaining the sampler is as much 'the probe' as reading it;
        a composition root that blows up building one must not take the
        already-decided outcome with it."""

        def acquire() -> MachineStateSampler:
            raise RuntimeError("no sampler for you")

        state = sample_machine_state_from(acquire)
        assert state.probe_error is not None
        assert "no sampler for you" in state.probe_error

    def test_a_teardown_signal_while_acquiring_still_propagates(self) -> None:
        def acquire() -> MachineStateSampler:
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            sample_machine_state_from(acquire)

    def test_the_renderer_never_raises_whatever_it_is_handed(self) -> None:
        """The rendering helper is the boundary's last line, so it is
        pinned directly: normal errors keep their message, hostile ones
        degrade to a name, and nothing escapes."""

        class _Hostile(Exception):
            def __repr__(self) -> str:
                raise ValueError("no repr")

        assert "boom" in describe_exception(ValueError("boom"))
        assert describe_exception(_Hostile()) == "_Hostile"
        assert describe_exception(KeyboardInterrupt()) == "KeyboardInterrupt()"

    def test_a_sampler_answering_with_the_wrong_type_is_contained(self) -> None:
        class _Wrong:
            def sample(self) -> MachineState:
                return "85% idle"  # type: ignore[return-value]

        state = sample_machine_state(_Wrong())
        assert state.probe_error is not None
        assert "MachineState" in state.probe_error

    def test_a_contained_failure_still_produces_a_full_envelope(self) -> None:
        """A failed probe must not change the record's SHAPE: the same
        keys, with nulls, so a query never has to special-case it."""
        class _Exploding:
            def sample(self) -> MachineState:
                raise OSError("no")

        healthy = machine_state_fields(_reading())[MACHINE_STATE_RECORD_KEY]
        failed = stamp_machine_state(_Exploding())[MACHINE_STATE_RECORD_KEY]
        assert isinstance(healthy, dict) and isinstance(failed, dict)
        assert set(healthy) == set(failed)


class TestEnvelopeShape:
    def test_every_field_is_rendered_under_one_key(self) -> None:
        envelope = machine_state_fields(_reading())
        assert set(envelope) == {MACHINE_STATE_RECORD_KEY}
        fields = envelope[MACHINE_STATE_RECORD_KEY]
        assert fields == {
            "sampled_at": "2026-08-29T12:00:00+00:00",
            "loadavg_1m": 7.91,
            "loadavg_5m": 12.51,
            "loadavg_15m": 9.0,
            "cpu_idle_percent": 85.68,
            "cpu_idle_source": "top -l 1 -n 0",
            "physical_cores": 18,
            "probe_error": None,
        }

    def test_a_fake_sampler_is_all_a_record_writer_needs(self) -> None:
        sampler = _FakeSampler(_reading(cpu_idle_percent=1.5))
        fields = stamp_machine_state(sampler)[MACHINE_STATE_RECORD_KEY]
        assert isinstance(fields, dict)
        assert fields["cpu_idle_percent"] == 1.5
        assert sampler.calls == 1

    def test_rendering_a_non_reading_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="MachineState"):
            machine_state_fields({"loadavg_1m": 1.0})  # type: ignore[arg-type]


class TestReadingValidation:
    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="sampled_at"):
            _reading(sampled_at=datetime(2026, 8, 29, 12, 0))

    def test_impossible_measurements_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="cpu_idle_percent"):
            _reading(cpu_idle_percent=101.0)
        with pytest.raises(ValueError, match="loadavg_1m"):
            _reading(loadavg_1m=-1.0)
        with pytest.raises(ValueError, match="physical_cores"):
            _reading(physical_cores=0)

    def test_a_reading_without_an_error_must_carry_the_syscall_facts(self) -> None:
        """'No error and no numbers' would be an unreportable third
        state; the two real outcomes stay distinguishable."""
        with pytest.raises(ValueError, match="load average"):
            _reading(loadavg_1m=None)

    def test_an_unmeasured_reading_invents_nothing(self) -> None:
        state = unmeasured_machine_state("OSError: nope", source="probe absent")
        assert state.probe_error == "OSError: nope"
        assert state.cpu_idle_source == "probe absent"
        assert state.loadavg_1m is None
        assert state.loadavg_5m is None
        assert state.loadavg_15m is None
        assert state.physical_cores is None
        assert state.sampled_at.tzinfo is not None
