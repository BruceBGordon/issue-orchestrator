"""Persistent-PTY round runner: lifecycle, round handoff, and partial-write
tolerance.

Tests use a self-contained stub agent that supports a control-file gate
(``STUB_RESPONSE_GATE``) and an opt-in partial-write race
(``STUB_PARTIAL_WRITE_FIRST``). Coordination happens via gate files and
injected ``now``/``sleep`` callables rather than real wall-clock waits, in
keeping with ``tests/unit/AGENTS.md``'s ban on timing-based unit
coordination. The single exception is the real-subprocess carve-out that
file also allows: the late-trust test drives a real child process and uses
the bounded ``_real_sleep`` helper below to yield wall-clock time so that
process can make progress.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable

import pytest

from issue_orchestrator.execution.persistent_round_failures import (
    PersistentRoundError,
    PersistentRoundTimeoutError,
    persistent_round_failure_reason,
    persistent_round_idle_trace,
)
from issue_orchestrator.execution.persistent_round_runner import (
    close_persistent_session,
    open_persistent_session,
    send_round,
)
from issue_orchestrator.execution.recording_contract import recording_event_count

# Bounded *real* sleep used only where a test drives a real subprocess and must
# yield wall-clock time for it to make progress (see the late-trust test). Named
# distinctly so fake-clock sleepers are never confused with real waiting.
_real_sleep = time.sleep


# ---------------------------------------------------------------------------
# Stub agent
# ---------------------------------------------------------------------------

_STUB_AGENT_SOURCE = textwrap.dedent("""
    import json
    import os
    import sys
    import time
    from pathlib import Path

    response_file = Path(os.environ["STUB_RESPONSE_FILE"])
    gate_file = os.environ.get("STUB_RESPONSE_GATE")
    fail_on_round = int(os.environ.get("STUB_FAIL_ON_ROUND", "0"))
    partial_first = os.environ.get("STUB_PARTIAL_WRITE_FIRST", "0") == "1"


    def _wait_for_gate() -> None:
        if not gate_file:
            return
        gate_path = Path(gate_file)
        # Polite spin — duration is determined by when the test creates the
        # gate file, not by any stub-side sleep budget.
        while not gate_path.exists():
            time.sleep(0.01)


    print("[stub-agent] ready", flush=True)
    round_index = 0
    for raw in sys.stdin:
        prompt = raw.strip()
        if not prompt:
            continue
        round_index += 1
        if fail_on_round and round_index == fail_on_round:
            sys.exit(7)
        response_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"round": round_index, "prompt": prompt, "ack": True})
        if partial_first and round_index == 1:
            # Race seed: write a partially-written JSON document, wait for
            # the test to flip the gate, then complete the write. The test
            # asserts send_round() keeps polling instead of exploding on
            # the partial JSON.
            with open(response_file, "w") as f:
                f.write("{")
                f.flush()
            print(f"[stub] partial write round {round_index}", flush=True)
            _wait_for_gate()
            response_file.write_text(payload, encoding="utf-8")
            print(f"[stub] full write round {round_index}", flush=True)
            continue
        _wait_for_gate()
        response_file.write_text(payload, encoding="utf-8")
        print(f"[stub] wrote round {round_index}", flush=True)
    print("[stub] EOF", flush=True)
""").strip()


def _write_stub_agent(tmp_path: Path) -> Path:
    path = tmp_path / "stub_agent.py"
    path.write_text(_STUB_AGENT_SOURCE, encoding="utf-8")
    return path


def _stub_command(stub_path: Path) -> list[str]:
    return [sys.executable, "-u", str(stub_path)]


def _stub_env(response_file: Path, **extras: str) -> dict[str, str]:
    env = dict(os.environ)
    env["STUB_RESPONSE_FILE"] = str(response_file)
    env.update(extras)
    return env


# ---------------------------------------------------------------------------
# Deterministic clock + sleep for tests that drive timeouts
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic clock that advances on each injected ``sleep`` call."""

    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def make_sleeper(
        self, on_sleep: Callable[[float], None] | None = None,
    ) -> Callable[[float], None]:
        def _sleep(seconds: float) -> None:
            self.value += seconds
            if on_sleep is not None:
                on_sleep(seconds)
        return _sleep


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Test-internal helper: poll predicate until True or assert.

    Used to wait on cross-process state (response file appears, process exits)
    without coordinating timing — the deadline is a safety net so a stuck
    test fails fast instead of hanging CI forever.
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return
        _time.sleep(0.005)
    raise AssertionError(f"Predicate did not become true within {timeout}s")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestPersistentSessionLifecycle:
    def test_session_is_live_requires_open_session_and_running_process(self) -> None:
        from issue_orchestrator.execution import persistent_round_runner

        class _Proc:
            pid = 123

            def __init__(self) -> None:
                self.return_code: int | None = None

            def poll(self) -> int | None:
                return self.return_code

        proc = _Proc()
        session = persistent_round_runner.PersistentSession(
            proc=proc,  # type: ignore[arg-type]
            master_fd=99,
        )

        assert session.is_live is True
        session.closed = True
        assert session.is_live is False
        session.closed = False
        proc.return_code = 0
        assert session.is_live is False

    def test_open_session_starts_slave_in_noncanonical_mode(self, tmp_path: Path) -> None:
        probe = tmp_path / "termios.json"
        script = tmp_path / "termios_probe.py"
        script.write_text(textwrap.dedent("""
            import json
            import os
            import sys
            import termios
            from pathlib import Path

            attrs = termios.tcgetattr(sys.stdin.fileno())
            Path(os.environ["TERMIO_PROBE_PATH"]).write_text(
                json.dumps({
                    "icanon": bool(attrs[3] & termios.ICANON),
                    "echo": bool(attrs[3] & termios.ECHO),
                }),
                encoding="utf-8",
            )
            for _raw in sys.stdin:
                pass
        """).strip(), encoding="utf-8")
        env = dict(os.environ)
        env["TERMIO_PROBE_PATH"] = str(probe)

        session = open_persistent_session(
            command=[sys.executable, "-u", str(script)],
            working_dir=tmp_path,
            env=env,
        )
        try:
            _wait_until(lambda: probe.exists() and probe.stat().st_size > 0)
        finally:
            close_persistent_session(session)

        state = json.loads(probe.read_text(encoding="utf-8"))
        assert state == {"icanon": False, "echo": False}

    def test_one_process_handles_three_sequential_rounds(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        try:
            r1 = send_round(session, prompt="alpha", response_file=response_file, timeout_seconds=5)
            r2 = send_round(session, prompt="bravo", response_file=response_file, timeout_seconds=5)
            r3 = send_round(session, prompt="charlie", response_file=response_file, timeout_seconds=5)
        finally:
            close_persistent_session(session)

        assert r1 == {"round": 1, "prompt": "alpha", "ack": True}
        assert r2 == {"round": 2, "prompt": "bravo", "ack": True}
        assert r3 == {"round": 3, "prompt": "charlie", "ack": True}

    def test_send_round_accepts_prompt_larger_than_canonical_line_limit(
        self, tmp_path: Path,
    ) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        long_prompt = "x" * 5000

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        try:
            response = send_round(
                session,
                prompt=long_prompt,
                response_file=response_file,
                timeout_seconds=5,
            )
        finally:
            close_persistent_session(session)

        assert response == {"round": 1, "prompt": long_prompt, "ack": True}

    def test_response_file_is_re_armed_each_round(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        try:
            send_round(session, prompt="round-1", response_file=response_file, timeout_seconds=5)
            assert response_file.exists()
            r2 = send_round(session, prompt="round-2", response_file=response_file, timeout_seconds=5)
        finally:
            close_persistent_session(session)
        assert r2["round"] == 2 and r2["prompt"] == "round-2"

    def test_recording_path_captures_continuous_log_across_rounds(
        self, tmp_path: Path,
    ) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        recording = tmp_path / "rec" / "terminal-recording.jsonl"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
            recording_path=recording,
        )
        try:
            send_round(session, prompt="alpha", response_file=response_file, timeout_seconds=5)
            send_round(session, prompt="bravo", response_file=response_file, timeout_seconds=5)
        finally:
            close_persistent_session(session)

        assert recording.exists()
        assert recording_event_count(recording) >= 2
        import base64
        import json as json_mod
        decoded: list[str] = []
        for line in recording.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json_mod.loads(line)
            if event.get("event_type") == "output" and event.get("data_b64"):
                decoded.append(
                    base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
                )
        combined = "".join(decoded)
        pos_a = combined.find("wrote round 1")
        pos_b = combined.find("wrote round 2")
        assert pos_a != -1 and pos_b != -1 and pos_a < pos_b

    def test_startup_interaction_runs_before_first_round_prompt(
        self,
        tmp_path: Path,
    ) -> None:
        response_file = tmp_path / "response.json"
        trust_marker = tmp_path / "trust-byte.txt"
        script = tmp_path / "trust_then_round.py"
        script.write_text(
            textwrap.dedent("""
                import json
                import os
                import sys
                from pathlib import Path

                response_file = Path(os.environ["STUB_RESPONSE_FILE"])
                trust_marker = Path(os.environ["STUB_TRUST_MARKER"])
                fd = sys.stdin.fileno()

                print("Do you trust the contents of this directory?", flush=True)
                print("1. Yes, continue", flush=True)
                print("2. No, quit", flush=True)

                first = os.read(fd, 1)
                trust_marker.write_text(repr(first), encoding="utf-8")
                if first != b"\\n":
                    sys.exit(9)

                buf = bytearray()
                while True:
                    chunk = os.read(fd, 1)
                    if not chunk:
                        sys.exit(10)
                    if chunk == b"\\n":
                        prompt = buf.decode("utf-8")
                        response_file.write_text(
                            json.dumps({"prompt": prompt, "ack": True}),
                            encoding="utf-8",
                        )
                        sys.exit(0)
                    buf.extend(chunk)
            """).strip(),
            encoding="utf-8",
        )
        env = _stub_env(
            response_file,
            STUB_TRUST_MARKER=str(trust_marker),
        )
        codex_bin = tmp_path / "codex"
        codex_bin.symlink_to(sys.executable)

        session = open_persistent_session(
            command=[str(codex_bin), "-u", str(script)],
            working_dir=tmp_path,
            env=env,
            recording_path=tmp_path / "terminal-recording.jsonl",
        )
        try:
            response = send_round(
                session,
                prompt="real round",
                response_file=response_file,
                timeout_seconds=5,
            )
        finally:
            close_persistent_session(session)

        assert trust_marker.read_text(encoding="utf-8") == "b'\\n'"
        assert response == {"prompt": "real round", "ack": True}

    def test_startup_interaction_waits_for_late_trust_prompt_without_recording(
        self,
        tmp_path: Path,
    ) -> None:
        response_file = tmp_path / "response.json"
        trust_marker = tmp_path / "trust-byte.txt"
        trust_gate = tmp_path / "show-trust"
        prompt_ready = tmp_path / "trust-rendered"
        script = tmp_path / "late_trust_then_round.py"
        script.write_text(
            textwrap.dedent("""
                import json
                import os
                import sys
                import time
                from pathlib import Path

                response_file = Path(os.environ["STUB_RESPONSE_FILE"])
                trust_marker = Path(os.environ["STUB_TRUST_MARKER"])
                trust_gate = Path(os.environ["STUB_TRUST_GATE"])
                prompt_ready = Path(os.environ["STUB_PROMPT_READY"])
                fd = sys.stdin.fileno()

                while not trust_gate.exists():
                    time.sleep(0.01)

                print("Do you trust the contents of this directory?", flush=True)
                print("1. Yes, continue", flush=True)
                print("2. No, quit", flush=True)
                prompt_ready.write_text("ready", encoding="utf-8")

                first = os.read(fd, 1)
                trust_marker.write_text(repr(first), encoding="utf-8")
                if first != b"\\n":
                    sys.exit(9)

                buf = bytearray()
                while True:
                    chunk = os.read(fd, 1)
                    if not chunk:
                        sys.exit(10)
                    if chunk == b"\\n":
                        prompt = buf.decode("utf-8")
                        response_file.write_text(
                            json.dumps({"prompt": prompt, "ack": True}),
                            encoding="utf-8",
                        )
                        sys.exit(0)
                    buf.extend(chunk)
            """).strip(),
            encoding="utf-8",
        )
        env = _stub_env(
            response_file,
            STUB_TRUST_MARKER=str(trust_marker),
            STUB_TRUST_GATE=str(trust_gate),
            STUB_PROMPT_READY=str(prompt_ready),
        )
        codex_bin = tmp_path / "codex"
        codex_bin.symlink_to(sys.executable)
        clock = _FakeClock()
        gate_opened = False

        def reveal_late_trust_prompt(seconds: float) -> None:
            # The runner drives its deadline off the real monotonic clock
            # (``now`` is deliberately not injected), so the round-trip has a
            # genuine wall-clock budget and cannot lose a race against the real
            # subprocess under CPU contention — a fake clock advanced only by
            # this sleeper would burn the whole timeout in a few poll
            # iterations regardless of real elapsed time and flake under load.
            # The injected ``sleep`` only stages the *late* trust prompt: the
            # first poll happens inside the runner's startup-interaction drain,
            # at which point we reveal the prompt and block until the stub has
            # rendered it. The real sleep keeps the poll loop yielding so the
            # subprocess gets scheduled.
            nonlocal gate_opened
            clock.value += seconds
            if not gate_opened:
                if clock.value >= 0.35:
                    gate_opened = True
                    trust_gate.touch()
                    _wait_until(prompt_ready.exists)
                return
            # Once the trust prompt is handled, the round prompt is delivered to
            # a *real* subprocess that needs real wall-clock time to read stdin
            # and write its response file. Advancing only the fake clock would
            # spin the response-poll loop through the whole timeout budget in
            # ~0s of real time, starving the subprocess under CPU contention
            # (the documented late-trust load flake). Yield real CPU here — a
            # bounded wait on a real external system, which AGENTS.md permits —
            # so completion is governed by the subprocess, not the fake clock.
            _real_sleep(seconds)

        session = open_persistent_session(
            command=[str(codex_bin), "-u", str(script)],
            working_dir=tmp_path,
            env=env,
        )
        try:
            response = send_round(
                session,
                prompt="real round",
                response_file=response_file,
                timeout_seconds=5,
                poll_interval_seconds=0.02,
                sleep=reveal_late_trust_prompt,
            )
        finally:
            close_persistent_session(session)

        assert gate_opened
        assert trust_marker.read_text(encoding="utf-8") == "b'\\n'"
        assert response == {"prompt": "real round", "ack": True}


# ---------------------------------------------------------------------------
# Failure modes — driven by gates / injected clock instead of wall-clock races
# ---------------------------------------------------------------------------


class TestPersistentSessionFailureModes:
    def test_timeout_is_driven_by_injected_clock_not_wall_time(
        self, tmp_path: Path,
    ) -> None:
        """The stub is gated and never writes a response. The runner's
        injected clock advances past the deadline on the first ``sleep``
        call, so the timeout fires immediately in real time. No
        wall-clock coordination."""
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        gate = tmp_path / "gate.never"  # never created

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file, STUB_RESPONSE_GATE=str(gate)),
        )
        try:
            clock = _FakeClock()
            # Sleep callable jumps the clock past the deadline immediately.
            sleeper = clock.make_sleeper(on_sleep=lambda _: clock.__setattr__(
                "value", max(clock.value, 100.0)
            ))
            with pytest.raises(PersistentRoundTimeoutError) as exc_info:
                send_round(
                    session,
                    prompt="never-responds",
                    response_file=response_file,
                    timeout_seconds=1.0,
                    poll_interval_seconds=0.01,
                    now=clock.now,
                    sleep=sleeper,
                )
            assert persistent_round_failure_reason(exc_info.value) == "timeout"
        finally:
            close_persistent_session(session)

    def test_unexpected_exit_mid_round_raises(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file, STUB_FAIL_ON_ROUND="1"),
        )
        try:
            with pytest.raises(PersistentRoundError, match="exited unexpectedly") as exc_info:
                send_round(
                    session,
                    prompt="will-die",
                    response_file=response_file,
                    timeout_seconds=5,
                    poll_interval_seconds=0.05,
                )
            assert persistent_round_failure_reason(exc_info.value) == (
                "process_exited_before_response"
            )
        finally:
            close_persistent_session(session)

    def test_send_round_after_close_raises(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        send_round(session, prompt="hi", response_file=response_file, timeout_seconds=5)
        close_persistent_session(session)

        with pytest.raises(PersistentRoundError, match="already closed") as exc_info:
            send_round(session, prompt="late", response_file=response_file, timeout_seconds=5)
        assert persistent_round_failure_reason(exc_info.value) == "session_closed"

    def test_prompt_idle_after_delivery_is_classified_as_not_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner as prr

        class _Proc:
            pid = 321

            def poll(self) -> None:
                return None

        monkeypatch.setattr(
            prr,
            "_submit_prompt_with_enter",
            lambda _session, payload, **_kwargs: (len(payload) + 1, None),
        )
        monkeypatch.setattr(prr, "_drain_pty_output", lambda _session: 0)
        session = prr.PersistentSession(proc=_Proc(), master_fd=99)  # type: ignore[arg-type]
        clock = _FakeClock()

        with pytest.raises(PersistentRoundTimeoutError) as exc_info:
            send_round(
                session,
                prompt="review round 2",
                response_file=tmp_path / "response.json",
                timeout_seconds=10.0,
                prompt_acceptance_idle_seconds=1.0,
                poll_interval_seconds=0.25,
                now=clock.now,
                sleep=clock.make_sleeper(),
            )

        assert persistent_round_failure_reason(exc_info.value) == "prompt_not_accepted"

    def test_round_failure_carries_the_idle_detector_trace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """#7128: idle facts leave the runner as data, not as log text.

        Kill-evidence capture writes ``idle-trace.json`` from this object, so
        the window config and the frozen ``bytes_drained`` trajectory have to
        travel on the exception rather than only into a warning line.
        """
        from issue_orchestrator.execution import persistent_round_runner as prr

        class _Proc:
            pid = 324

            def poll(self) -> None:
                return None

        monkeypatch.setattr(
            prr,
            "_submit_prompt_with_enter",
            lambda _session, payload, **_kwargs: (len(payload) + 1, None),
        )
        monkeypatch.setattr(prr, "_drain_pty_output", lambda _session: 0)
        session = prr.PersistentSession(proc=_Proc(), master_fd=99)  # type: ignore[arg-type]
        clock = _FakeClock()

        with pytest.raises(PersistentRoundTimeoutError) as exc_info:
            send_round(
                session,
                prompt="review round 3",
                response_file=tmp_path / "response.json",
                timeout_seconds=600.0,
                prompt_acceptance_idle_seconds=60.0,
                poll_interval_seconds=0.25,
                now=clock.now,
                sleep=clock.make_sleeper(),
            )

        trace = persistent_round_idle_trace(exc_info.value)
        assert trace is not None
        assert trace.window_seconds == 60.0
        assert trace.deadline_seconds == 600.0
        assert trace.poll_interval_seconds == 0.25
        assert trace.poll_iterations > 1
        # Nothing was ever drained: the signature of a prompt the agent
        # never engaged with.
        assert trace.bytes_drained_total == 0
        assert trace.idle_for_seconds >= 60.0
        assert trace.samples
        assert trace.samples[-1].bytes_drained_total == 0

    def test_deadline_timeout_also_carries_an_idle_trace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner as prr

        class _Proc:
            pid = 325

            def poll(self) -> None:
                return None

        monkeypatch.setattr(
            prr,
            "_submit_prompt_with_enter",
            lambda _session, payload, **_kwargs: (len(payload) + 1, None),
        )
        monkeypatch.setattr(prr, "_drain_pty_output", lambda _session: 3)
        session = prr.PersistentSession(proc=_Proc(), master_fd=99)  # type: ignore[arg-type]
        clock = _FakeClock()

        with pytest.raises(PersistentRoundTimeoutError) as exc_info:
            send_round(
                session,
                prompt="review round 4",
                response_file=tmp_path / "response.json",
                timeout_seconds=2.0,
                prompt_acceptance_idle_seconds=None,
                poll_interval_seconds=0.5,
                now=clock.now,
                sleep=clock.make_sleeper(),
            )

        trace = persistent_round_idle_trace(exc_info.value)
        assert trace is not None
        assert trace.window_seconds is None
        assert trace.bytes_drained_total == 12
        assert trace.elapsed_seconds >= 2.0

    def test_invalid_response_failure_carries_the_idle_trace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """#7141 finding 4: this was the one raise site without a trace."""
        from issue_orchestrator.execution import persistent_round_runner as prr
        class _Proc:
            pid = 777
            _polls = 0

            def poll(self) -> int | None:
                _Proc._polls += 1
                return 3 if _Proc._polls > 1 else None

        monkeypatch.setattr(
            prr, "_submit_prompt_with_enter",
            lambda _s, payload, **_k: (len(payload) + 1, None),
        )
        monkeypatch.setattr(prr, "_drain_pty_output", lambda _s: 5)
        response_file = tmp_path / "response.json"
        session = prr.PersistentSession(proc=_Proc(), master_fd=99)  # type: ignore[arg-type]

        clock_value = {"t": 0.0}

        def _now() -> float:
            return clock_value["t"]

        def _sleep(seconds: float) -> None:
            clock_value["t"] += seconds
            # send_round clears a stale response file before prompting, so the
            # agent's invalid JSON has to land after that.
            response_file.write_text("{not json", encoding="utf-8")

        with pytest.raises(PersistentRoundError) as exc_info:
            send_round(
                session,
                prompt="p",
                response_file=response_file,
                timeout_seconds=30.0,
                poll_interval_seconds=0.5,
                now=_now,
                sleep=_sleep,
            )

        assert persistent_round_failure_reason(exc_info.value) == "invalid_response"
        assert persistent_round_idle_trace(exc_info.value) is not None

    def test_prompt_activity_resets_not_accepted_idle_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner as prr

        class _Proc:
            pid = 322

            def poll(self) -> None:
                return None

        response_file = tmp_path / "response.json"
        clock = _FakeClock()
        poll_count = {"value": 0}

        def _drain(_session: object) -> int:
            poll_count["value"] += 1
            return 1 if poll_count["value"] in {1, 4} else 0

        def _sleep(seconds: float) -> None:
            clock.value += seconds
            if clock.value >= 1.5 and not response_file.exists():
                response_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

        monkeypatch.setattr(
            prr,
            "_submit_prompt_with_enter",
            lambda _session, payload, **_kwargs: (len(payload) + 1, None),
        )
        monkeypatch.setattr(prr, "_drain_pty_output", _drain)
        session = prr.PersistentSession(proc=_Proc(), master_fd=99)  # type: ignore[arg-type]

        response = send_round(
            session,
            prompt="review round 2",
            response_file=response_file,
            timeout_seconds=10.0,
            prompt_acceptance_idle_seconds=1.0,
            poll_interval_seconds=0.25,
            now=clock.now,
            sleep=_sleep,
        )

        assert response == {"ok": True}


# ---------------------------------------------------------------------------
# Partial-write race tolerance — the regression that reviewer #6143 caught
# ---------------------------------------------------------------------------


class TestPartialWriteTolerance:
    def test_send_round_keeps_polling_through_partial_write(
        self, tmp_path: Path,
    ) -> None:
        """The stub writes a half-formed JSON document, waits for the test
        to flip a gate, then writes the complete document. The runner must
        treat the partial JSON as 'still being written' and keep polling,
        not raise PersistentRoundError on the first JSONDecodeError.
        """
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        gate = tmp_path / "complete-write.gate"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(
                response_file,
                STUB_PARTIAL_WRITE_FIRST="1",
                STUB_RESPONSE_GATE=str(gate),
            ),
        )
        try:
            # Drive the partial-write race deterministically: a sleeper
            # that flips the gate after the runner has observed the
            # partial-JSON state. The first poll iteration happens before
            # the stub has even started; subsequent polls find partial
            # content and return None; once the gate flips, the stub
            # finishes the write and the next poll succeeds.
            polled = {"count": 0}

            def stepping_sleeper(_seconds: float) -> None:
                polled["count"] += 1
                if polled["count"] == 2:
                    # By now the stub is past its partial write and parked
                    # on the gate. Flip it to let the write complete.
                    gate.touch()

            response = send_round(
                session,
                prompt="partial-write",
                response_file=response_file,
                timeout_seconds=5.0,
                poll_interval_seconds=0.05,
                sleep=stepping_sleeper,
            )
            assert response == {"round": 1, "prompt": "partial-write", "ack": True}
            # The runner did NOT bail out on the partial-write state.
            assert polled["count"] >= 2
        finally:
            close_persistent_session(session)


# ---------------------------------------------------------------------------
# PTY prompt-write loop — locks in the #6160 e2e regression fix
# ---------------------------------------------------------------------------


class TestWriteFullHandlesNonBlockingPtyWrites:
    """The PTY master fd is non-blocking, so ``os.write`` may return fewer
    bytes than requested or raise ``BlockingIOError`` when the kernel's
    PTY input buffer is nearly full. The previous code path called
    ``os.write(fd, payload)`` once and ignored the return value, so any
    unwritten suffix was silently dropped — the agent received a
    truncated prompt and the round hung forever.

    These tests pin ``_write_full`` against three failure modes:
    short writes, ``BlockingIOError``, and zero-byte writes — all on
    a deadline so a kernel that never drains raises the timeout error.
    """

    def test_loops_until_full_payload_is_written_through_short_writes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner
        from issue_orchestrator.execution.persistent_round_runner import (
            _write_full,  # noqa: PLC2701 — private helper is the contract under test
        )

        payload = b"abcdefghij"
        observed: list[bytes] = []
        # Simulate a kernel that only accepts 3 bytes per call until done.
        def fake_write(_fd: int, buf: bytes) -> int:
            n = min(3, len(buf))
            observed.append(buf[:n])
            return n

        monkeypatch.setattr(persistent_round_runner.os, "write", fake_write)
        clock = _FakeClock()
        sleeper = clock.make_sleeper()

        written = _write_full(
            fd=99, payload=payload,
            deadline=clock.now() + 5.0, now=clock.now, sleep=sleeper,
        )
        assert written == len(payload)
        assert b"".join(observed) == payload

    def test_retries_on_blocking_io_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner
        from issue_orchestrator.execution.persistent_round_runner import (
            _write_full,  # noqa: PLC2701 — private helper is the contract under test
        )

        payload = b"hello"
        attempts = {"count": 0}
        def fake_write(_fd: int, buf: bytes) -> int:
            attempts["count"] += 1
            if attempts["count"] <= 2:
                raise BlockingIOError("buffer full")
            return len(buf)

        monkeypatch.setattr(persistent_round_runner.os, "write", fake_write)
        clock = _FakeClock()
        sleeper = clock.make_sleeper()

        written = _write_full(
            fd=99, payload=payload,
            deadline=clock.now() + 5.0, now=clock.now, sleep=sleeper,
        )
        assert written == len(payload)
        assert attempts["count"] == 3

    def test_raises_timeout_when_kernel_never_drains(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner
        from issue_orchestrator.execution.persistent_round_runner import (
            _write_full,  # noqa: PLC2701 — private helper is the contract under test
        )

        def always_blocking(_fd: int, _buf: bytes) -> int:
            raise BlockingIOError("buffer perpetually full")

        monkeypatch.setattr(persistent_round_runner.os, "write", always_blocking)
        clock = _FakeClock()
        # Deadline already in the past so the very first iteration trips.
        sleeper = clock.make_sleeper()

        with pytest.raises(PersistentRoundTimeoutError, match="Could not write"):
            _write_full(
                fd=99, payload=b"x",
                deadline=clock.now() - 1.0, now=clock.now, sleep=sleeper,
            )

    def test_closed_fd_surfaces_as_round_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner
        from issue_orchestrator.execution.persistent_round_runner import (
            _write_full,  # noqa: PLC2701 — private helper is the contract under test
        )

        def closed_fd(_fd: int, _buf: bytes) -> int:
            raise OSError("bad file descriptor")

        monkeypatch.setattr(persistent_round_runner.os, "write", closed_fd)
        clock = _FakeClock()

        with pytest.raises(PersistentRoundError, match="bad file descriptor"):
            _write_full(
                fd=99,
                payload=b"x",
                deadline=clock.now() + 5.0,
                now=clock.now,
                sleep=clock.make_sleeper(),
                role_label="coder@round-1",
                pid=123,
            )

    def test_send_round_uses_separate_write_deadline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner

        class _Proc:
            pid = 456

            def poll(self) -> None:
                return None

        captured: dict[str, float] = {}

        def fake_write_full(_fd: int, _payload: bytes, **kwargs: object) -> int:
            captured["deadline"] = kwargs["deadline"]  # type: ignore[assignment]
            raise PersistentRoundTimeoutError("stop after write deadline capture")

        monkeypatch.setattr(persistent_round_runner, "_write_full", fake_write_full)
        session = persistent_round_runner.PersistentSession(
            proc=_Proc(),  # type: ignore[arg-type]
            master_fd=99,
        )
        clock = _FakeClock()
        clock.value = 10.0

        with pytest.raises(PersistentRoundTimeoutError, match="deadline capture"):
            send_round(
                session,
                prompt="hello",
                response_file=tmp_path / "response.json",
                timeout_seconds=100.0,
                write_timeout_seconds=3.0,
                now=clock.now,
                sleep=clock.make_sleeper(),
            )

        assert captured["deadline"] == 13.0

    def test_send_round_logs_prompt_write_timeout_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner

        class _Proc:
            pid = 789

            def poll(self) -> None:
                return None

        def fake_write_full(_fd: int, _payload: bytes, **_kwargs: object) -> int:
            raise PersistentRoundTimeoutError(
                "Could not write 42 bytes to PTY fd=99 role=reviewer@round-1 "
                "within deadline (0 bytes accepted before timeout)"
            )

        monkeypatch.setattr(persistent_round_runner, "_write_full", fake_write_full)
        session = persistent_round_runner.PersistentSession(
            proc=_Proc(),  # type: ignore[arg-type]
            master_fd=99,
        )
        clock = _FakeClock()

        with caplog.at_level(
            logging.WARNING,
            logger="issue_orchestrator.execution.persistent_round_runner",
        ):
            with pytest.raises(PersistentRoundTimeoutError):
                send_round(
                    session,
                    prompt="hello",
                    response_file=tmp_path / "response.json",
                    timeout_seconds=100.0,
                    write_timeout_seconds=3.0,
                    now=clock.now,
                    sleep=clock.make_sleeper(),
                    role_label="reviewer@round-1",
                )

        messages = "\n".join(caplog.messages)
        assert "prompt write timeout role=reviewer@round-1 pid=789" in messages
        assert "likely_stale_persistent_session=True" in messages
        assert "0 bytes accepted before timeout" in messages



# ---------------------------------------------------------------------------
# Prompt submission — the tixmeup #277/#290 root cause.
#
# A persistent agent TUI reads stdin in RAW mode, where Enter is the carriage
# return (\r); a line feed (\n) is just a literal newline in the input box and
# does NOT submit. Worse, codex's TUI treats even a \r BATCHED into the same
# write as the prompt text as a literal newline — the prompt renders into the
# box but is never submitted and the round hangs to its full timeout. The
# contract send_round must honor: write the prompt text, let the echo settle,
# then write a standalone \r (a real Enter keypress). claude accepts either
# form; codex requires the separate Enter.
#
# The stub below faithfully models the raw-mode agent: it puts its stdin in
# raw mode and only "submits" the accumulated line on \r, treating \n as a
# literal. This reproduces the real-agent behavior deterministically (no
# agent CLI needed), so the regression is guarded in plain unit runs. The real
# Claude and Codex TUI acceptances live in
# tests/e2e/test_live_agent_transport.py.
# ---------------------------------------------------------------------------

_RAW_MODE_SUBMIT_STUB = textwrap.dedent(r"""
    import json, os, sys, tty
    from pathlib import Path

    resp = Path(os.environ["STUB_RESPONSE_FILE"])
    tty.setraw(sys.stdin.fileno())  # Enter == \r; \n is a literal newline (no submit)
    # Signal that raw mode is active and we are reading, so the test only sends
    # the prompt afterwards — otherwise the PTY's default ICRNL would translate
    # the \r to \n before raw mode took effect and the distinction would be lost.
    Path(os.environ["STUB_READY_FILE"]).write_text("1", encoding="utf-8")
    buf = b""
    while True:
        ch = os.read(0, 1)
        if not ch:
            break
        if ch == b"\r":                 # Enter -> submit the accumulated line
            line = buf.decode("utf-8", "replace").strip()
            buf = b""
            if not line:
                continue
            resp.parent.mkdir(parents=True, exist_ok=True)
            resp.write_text(json.dumps({"submitted": line}), encoding="utf-8")
        else:                            # \n and everything else -> literal input, NOT a submit
            buf += ch
""").strip()


def _write_raw_mode_stub(tmp_path: Path) -> Path:
    path = tmp_path / "raw_mode_stub.py"
    path.write_text(_RAW_MODE_SUBMIT_STUB, encoding="utf-8")
    return path


class TestPromptSubmissionTerminator:
    def test_enter_is_a_separate_write_from_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Unit guard on the two-write submit contract: the prompt text is
        written with NO terminator batched in, then the Enter ("\\r") arrives
        as its own write. codex's TUI treats a \\r batched with the prompt as
        a literal newline inside its input box (the prompt renders but never
        submits — the tixmeup #277/#290 hang class), and \\n never submits to
        any raw-mode TUI. Validated against real claude and real codex in
        tests/e2e/test_live_agent_transport.py."""
        from issue_orchestrator.execution import persistent_round_runner as prr

        writes: list[bytes] = []

        def fake_write_full(_fd: int, payload: bytes, **_kwargs: object) -> int:
            writes.append(payload)
            if len(writes) == 2:
                raise PersistentRoundTimeoutError("stop after the Enter write")
            return len(payload)

        monkeypatch.setattr(prr, "_write_full", fake_write_full)

        class _Proc:
            pid = 1

            def poll(self) -> None:
                return None

        session = prr.PersistentSession(proc=_Proc(), master_fd=99)  # type: ignore[arg-type]
        clock = _FakeClock()
        with pytest.raises(PersistentRoundTimeoutError):
            send_round(
                session, prompt="hello", response_file=tmp_path / "r.json",
                timeout_seconds=5, now=clock.now, sleep=clock.make_sleeper(),
            )
        assert writes[0] == b"hello", "prompt write must carry no terminator"
        assert writes[1] == b"\r", "submit must be a standalone Enter write"

    def test_carriage_return_submits_to_a_raw_mode_agent(self, tmp_path: Path) -> None:
        """End-to-end over a real PTY: a raw-mode agent (Enter == \\r) receives
        and submits the prompt, so send_round gets its response."""
        stub = _write_raw_mode_stub(tmp_path)
        response_file = tmp_path / "response.json"
        ready_file = tmp_path / "raw-ready"
        session = open_persistent_session(
            command=[sys.executable, "-u", str(stub)],
            working_dir=tmp_path,
            env=_stub_env(response_file, STUB_READY_FILE=str(ready_file)),
        )
        try:
            _wait_until(ready_file.exists)  # raw mode active before we send
            r = send_round(
                session, prompt="round-1", response_file=response_file,
                timeout_seconds=5, poll_interval_seconds=0.02,
            )
        finally:
            close_persistent_session(session)
        assert r == {"submitted": "round-1"}

    def test_newline_terminator_would_not_submit_to_a_raw_mode_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Proves the fix is load-bearing: regress the Enter keystroke to the
        legacy \\n and the same raw-mode agent never submits -> send_round
        times out (the original tixmeup #277/#290 hang)."""
        from issue_orchestrator.execution import persistent_round_runner as prr

        orig_write_full = prr._write_full  # noqa: SLF001

        def lf_write_full(
            fd: int,
            payload: bytes,
            *,
            deadline: float,
            now: Callable[[], float],
            sleep: Callable[[float], None],
            role_label: str | None = None,
            pid: int | None = None,
            heartbeat_seconds: float = 5.0,
            drain_output: Callable[[], int] | None = None,
        ) -> int:
            if payload == b"\r":  # regress the Enter write to the buggy \n
                payload = b"\n"
            return orig_write_full(
                fd,
                payload,
                deadline=deadline,
                now=now,
                sleep=sleep,
                role_label=role_label,
                pid=pid,
                heartbeat_seconds=heartbeat_seconds,
                drain_output=drain_output,
            )

        monkeypatch.setattr(prr, "_write_full", lf_write_full)

        stub = _write_raw_mode_stub(tmp_path)
        response_file = tmp_path / "response.json"
        ready_file = tmp_path / "raw-ready"
        session = open_persistent_session(
            command=[sys.executable, "-u", str(stub)],
            working_dir=tmp_path,
            env=_stub_env(response_file, STUB_READY_FILE=str(ready_file)),
        )
        try:
            _wait_until(ready_file.exists)
            # A plain advancing fake clock (not a jump-to-expiry sleeper): the
            # echo-settle drain between the two writes also consumes ``sleep``
            # calls, and a jump there would expire the round before the \n
            # Enter regression was ever written to the agent.
            clock = _FakeClock()
            with pytest.raises(PersistentRoundTimeoutError):
                send_round(
                    session, prompt="round-1", response_file=response_file,
                    timeout_seconds=1.0, poll_interval_seconds=0.02,
                    now=clock.now, sleep=clock.make_sleeper(),
                )
        finally:
            close_persistent_session(session)
        assert not response_file.exists()


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


class TestCloseSession:
    def test_close_terminates_running_agent_and_returns_exit_code(
        self, tmp_path: Path,
    ) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        send_round(session, prompt="hello", response_file=response_file, timeout_seconds=5)

        rc = close_persistent_session(session, grace_seconds=2.0)
        # The close path waits for the process internally; once it returns,
        # the process must be reaped (no race window to wait around).
        assert session.closed is True
        assert session.proc.poll() is not None
        assert rc is not None

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        close_persistent_session(session)
        close_persistent_session(session)
        assert session.closed is True


class TestSendRoundResponseReaderChannel:
    """send_round can take its response from an injected reader (the
    TurnMailbox channel) instead of the response file. The PTY pumping is
    unchanged; only the response source differs.
    """

    def test_reader_value_is_returned_and_file_is_ignored(
        self, tmp_path: Path,
    ) -> None:
        # The stub agent writes its own JSON to the response file, but with a
        # reader provided send_round must return the reader's value — proving
        # the file is not consulted when the mailbox is the channel.
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        verdict = {"response_type": "ok", "response_text": "delivered via mailbox"}

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        try:
            response = send_round(
                session,
                prompt="hello",
                response_file=response_file,
                timeout_seconds=5,
                response_reader=lambda: verdict,
            )
        finally:
            close_persistent_session(session)

        assert response == verdict

    def test_reader_channel_does_not_unlink_the_response_file(
        self, tmp_path: Path,
    ) -> None:
        # The file channel clears the response file before each round; the
        # reader channel must leave the filesystem untouched.
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        sentinel = tmp_path / "sentinel.json"
        sentinel.write_text("{}", encoding="utf-8")

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        try:
            send_round(
                session,
                prompt="hello",
                response_file=sentinel,
                timeout_seconds=5,
                response_reader=lambda: {"response_type": "ok", "response_text": "x"},
            )
        finally:
            close_persistent_session(session)

        assert sentinel.exists()

    def test_mailbox_mode_exit_with_stale_file_is_respawnable_not_invalid(
        self, tmp_path: Path,
    ) -> None:
        # Finding #2: in mailbox mode a stale/legacy response file left by an
        # agent that exits without delivering must NOT downgrade the round to
        # the non-respawnable INVALID_RESPONSE. It is a process that exited
        # before responding (respawnable) — the fail-safe contract.
        from issue_orchestrator.execution.persistent_round_runner import (
            RoundFailureReason,
        )

        stale_file = tmp_path / "response.json"
        agent = tmp_path / "stale_then_exit.py"
        agent.write_text(textwrap.dedent(f"""
            import json, time
            from pathlib import Path
            Path({str(stale_file)!r}).write_text(
                json.dumps({{"response_type": "ok", "response_text": "stale legacy"}}),
                encoding="utf-8",
            )
            time.sleep(0.4)
        """).strip(), encoding="utf-8")

        session = open_persistent_session(
            command=[sys.executable, "-u", str(agent)],
            working_dir=tmp_path,
            env=dict(os.environ),
        )
        try:
            with pytest.raises(PersistentRoundError) as excinfo:
                send_round(
                    session,
                    prompt="go",
                    response_file=stale_file,
                    timeout_seconds=5,
                    # Mailbox never delivers — models a forgotten exchange-respond.
                    response_reader=lambda: None,
                )
        finally:
            close_persistent_session(session)

        assert stale_file.exists()  # the legacy file is present...
        assert persistent_round_failure_reason(excinfo.value) == (
            RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE.value
        )
