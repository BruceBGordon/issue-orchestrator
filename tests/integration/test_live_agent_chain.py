"""Integration test: real Claude through the full live launch chain.

Exercises the EXACT path the orchestrator uses in production:

    AgentRunner.start() → pexpect PTY → bash -lc → provider_runner
        → SubprocessAgentRunner → Popen(/bin/sh -c "claude ...")

This test exists because unit tests and mocks repeatedly passed while
the live system failed.  We need at least one test that proves the full
chain produces output and exits cleanly with a real Claude process.

Requires: Claude CLI installed and authenticated (skips otherwise).
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import pytest

from issue_orchestrator.execution.agent_runner import AgentRunner
from issue_orchestrator.execution.agent_runner_types import AgentSpec, RetryPolicy
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_process_group_supervisor,
)
from tests.fixtures.live_agent_cli import is_claude_authenticated


pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.provider_claude,
    pytest.mark.xdist_group("pty"),
]


def _decoded_output(path: Path) -> str:
    """Decode the base64-JSONL terminal recording into raw stdout text.

    ``AgentRunner`` records session output as JSONL events with the payload
    base64-encoded under ``data_b64``. Assertions over the live process
    stdout must decode those payloads; substring checks against the raw
    JSONL bytes would silently miss matches.
    """
    if not path.exists():
        return ""
    chunks: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if isinstance(data_b64, str) and data_b64:
            chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
    return "".join(chunks)

# ---------------------------------------------------------------------------
# Markers / skip conditions
# ---------------------------------------------------------------------------

# Import-time probe is acceptable here: this module is only collected by the
# dedicated live-agent lanes (test-integration-agent / heavy runs), where a
# real provider round-trip is proportionate. The whole-suite e2e module
# (tests/e2e/test_live_agent_transport.py) defers the same probe to runtime.
_CLAUDE_READY = is_claude_authenticated()


def _live_provider_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=2,
        initial_backoff_seconds=1,
        max_backoff_seconds=1,
        jitter=False,
    )


@pytest.mark.skipif(not _CLAUDE_READY, reason="Claude CLI not installed or not authenticated")
class TestLiveAgentChain:
    """Prove the full pexpect → bash → provider_runner → Claude chain works."""

    @staticmethod
    def _venv_path_prefix() -> str:
        venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin"
        return f"{venv_bin}:{os.environ.get('PATH', '')}"

    # ------------------------------------------------------------------
    # Layer 1: SubprocessAgentRunner → Claude (no PTY, -p mode)
    # ------------------------------------------------------------------

    def test_subprocess_runner_direct(self, tmp_path: Path) -> None:
        """SubprocessAgentRunner → Claude -p works (inner layer only)."""
        from issue_orchestrator.execution.subprocess_runner import SubprocessAgentRunner

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        spec = AgentSpec(
            command=[
                "/bin/sh", "-c",
                "claude --permission-mode bypassPermissions --model haiku "
                "-p 'Reply with exactly: SUBPROCESS_TEST_OK'",
            ],
            working_dir=tmp_path,
            timeout_seconds=60,
            output_dir=run_dir,
            retry_policy=_live_provider_retry_policy(),
        )

        result = SubprocessAgentRunner().run(spec)

        assert result.exit_code == 0, (
            f"Claude exited with code {result.exit_code}. stderr: {result.stderr}"
        )

    # ------------------------------------------------------------------
    # Layer 2: pexpect PTY → Claude -p (no provider_runner)
    # ------------------------------------------------------------------

    def test_pexpect_pty_direct_claude_p_mode(self, tmp_path: Path) -> None:
        """pexpect PTY → Claude -p works and produces log output."""
        log_path = tmp_path / "ui-session.log"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        cmd = (
            f'export PATH="{self._venv_path_prefix()}" && '
            f"claude --permission-mode bypassPermissions --model haiku "
            f"-p 'Reply with exactly: PTY_DIRECT_TEST_OK'"
        )

        spec = AgentSpec(
            command=["/bin/bash", "-lc", cmd],
            working_dir=tmp_path,
            timeout_seconds=60,
            log_path=log_path,
            output_dir=run_dir,
            retry_policy=_live_provider_retry_policy(),
        )

        result = AgentRunner(build_process_group_supervisor()).run(spec)

        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}, "
            f"log:\n{log_path.read_text() if log_path.exists() else '<missing>'}"
        )

        decoded = _decoded_output(log_path)
        assert "PTY_DIRECT_TEST_OK" in decoded, (
            f"Claude output not in decoded log. Decoded content:\n{decoded}"
        )

    # ------------------------------------------------------------------
    # Layer 3: pexpect PTY → provider_runner → SubprocessAgentRunner → Claude -p
    # ------------------------------------------------------------------

    def test_full_chain_p_mode(self, tmp_path: Path) -> None:
        """Full chain with -p mode: pexpect → provider_runner → Claude."""
        log_path = tmp_path / "ui-session.log"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        claude_cmd = (
            "claude --permission-mode bypassPermissions --model haiku "
            "-p 'Reply with exactly: FULL_CHAIN_P_TEST_OK'"
        )

        provider_runner_cmd = (
            f"python -m issue_orchestrator.entrypoints.cli_tools.provider_runner "
            f"--command {claude_cmd!r} "
            f"--timeout-seconds 60 "
            f"--max-attempts 1 "
            f"--run-dir {run_dir}"
        )

        full_cmd = (
            f'export PATH="{self._venv_path_prefix()}" && {provider_runner_cmd}'
        )

        spec = AgentSpec(
            command=["/bin/bash", "-lc", full_cmd],
            working_dir=tmp_path,
            timeout_seconds=120,
            log_path=log_path,
            output_dir=run_dir,
        )

        result = AgentRunner(build_process_group_supervisor()).run(spec)
        raw_log = log_path.read_text() if log_path.exists() else "<missing>"
        decoded = _decoded_output(log_path)

        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}, log:\n{raw_log}"
        )

        assert "FULL_CHAIN_P_TEST_OK" in decoded, (
            f"Claude output not in decoded log. Decoded content:\n{decoded}"
        )

    # ------------------------------------------------------------------
    # Layer 4: THE LIVE PATH — -p mode with --append-system-prompt,
    # matching the orchestrator's production invocation pattern
    # ------------------------------------------------------------------

    def test_full_chain_production_flags(self, tmp_path: Path) -> None:
        """Full chain with production flags: -p + --append-system-prompt.

        The orchestrator uses:
            claude -p --permission-mode bypassPermissions --model haiku
                   --append-system-prompt 'system prompt'
                   'initial user prompt'

        This is what runs in production. Previously used interactive mode
        (no -p) which stalled because SubprocessAgentRunner uses stdin=DEVNULL.
        """
        log_path = tmp_path / "ui-session.log"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        system_prompt = "You are a test agent. Complete the task and exit."
        user_prompt = (
            "Reply with exactly PRODUCTION_FLAGS_OK and then use coding-done "
            "to report completion. If coding-done is not available, just reply."
        )

        # Match the exact live invocation: -p mode with
        # --append-system-prompt and positional prompt.
        # Use shlex.quote at each nesting level, matching production quoting.
        claude_cmd = (
            f"claude -p --permission-mode bypassPermissions --model haiku "
            f"--append-system-prompt {shlex.quote(system_prompt)} "
            f"{shlex.quote(user_prompt)}"
        )

        # provider_runner --command takes the whole claude invocation as
        # a single string argument — quote it for the outer shell
        provider_runner_cmd = (
            f"python -m issue_orchestrator.entrypoints.cli_tools.provider_runner "
            f"--command {shlex.quote(claude_cmd)} "
            f"--timeout-seconds 90 "
            f"--max-attempts 1 "
            f"--run-dir {run_dir}"
        )

        full_cmd = (
            f'export PATH="{self._venv_path_prefix()}" && {provider_runner_cmd}'
        )

        spec = AgentSpec(
            command=["/bin/bash", "-lc", full_cmd],
            working_dir=tmp_path,
            timeout_seconds=120,
            log_path=log_path,
            output_dir=run_dir,
        )

        runner = AgentRunner(build_process_group_supervisor())
        session = runner.start(spec)

        # Mimic terminal_subprocess._start_session_watcher
        result_holder: list = []

        def _watch() -> None:
            result_holder.append(session.wait(timeout=120))

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()

        # Monitor progress — detect stalls
        deadline = time.monotonic() + 120
        last_size = 0
        stall_start: float | None = None
        STALL_THRESHOLD = 30  # seconds without log growth = stalled

        while time.monotonic() < deadline:
            if not session.is_alive() and not watcher.is_alive():
                break

            if log_path.exists():
                current_size = log_path.stat().st_size
                if current_size != last_size:
                    last_size = current_size
                    stall_start = None
                elif stall_start is None:
                    stall_start = time.monotonic()
                elif time.monotonic() - stall_start > STALL_THRESHOLD:
                    # Output stalled — capture state for diagnosis
                    log_snapshot = log_path.read_text()
                    session.kill()
                    pytest.fail(
                        f"Log output stalled for {STALL_THRESHOLD}s at "
                        f"{current_size} bytes. Session alive={session.is_alive()}. "
                        f"Log content:\n{log_snapshot}"
                    )

            time.sleep(1)

        watcher.join(timeout=10)

        assert result_holder, "Watcher thread never completed"
        result = result_holder[0]

        raw_log = log_path.read_text() if log_path.exists() else "<missing>"
        decoded = _decoded_output(log_path)

        # Must have produced some output
        assert len(raw_log) > 0, (
            f"Log is empty — likely stuck at startup. "
            f"Content:\n{raw_log}"
        )

        # Must NOT contain raw bun internals (check decoded stdout, not the
        # JSONL wrapper — base64 would mask the marker either way, but the
        # intent is to check what the agent actually emitted).
        assert "/$bunfs/" not in decoded, (
            f"Log contains bun runtime internals:\n{decoded}"
        )

        # Should exit cleanly
        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}, timed_out={result.timed_out}. "
            f"Log:\n{raw_log}"
        )
