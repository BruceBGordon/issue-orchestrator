"""Architectural guardrails for the unified agent runner hierarchy.

These tests enforce that ALL agent process spawning goes through the
runner hierarchy in ``execution/``:
- AgentRunner (pexpect PTY) for sessions with CleaningLogWriter
- SubprocessAgentRunner (Popen) for provider_runner/validation_retry

Any code that bypasses these risks creating an untested I/O path —
the root cause of #4057.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
TESTS_ROOT = Path(__file__).resolve().parents[2] / "tests"


# ---------------------------------------------------------------------------
# Files allowed to use pexpect.spawn directly (besides AgentRunner).
# Remove entries here as each file is migrated.
# ---------------------------------------------------------------------------
KNOWN_PEXPECT_SPAWN_VIOLATIONS: set[str] = set()


def _find_pexpect_spawn_calls(source_file: Path) -> list[int]:
    """Return line numbers where pexpect.spawn() is called (not type annotations)."""
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    except SyntaxError:
        return []

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match pexpect.spawn(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "spawn"
            and isinstance(func.value, ast.Name)
            and func.value.id == "pexpect"
        ):
            lines.append(node.lineno)
    return lines


class TestOnlyAgentRunnerSpawnsAgents:
    """No code outside AgentRunner may call pexpect.spawn() to create agents."""

    def test_no_unauthorized_pexpect_spawn_in_src(self) -> None:
        """Scan src/ for pexpect.spawn() calls outside the allowed set."""
        allowed = {"execution/agent_runner.py"} | KNOWN_PEXPECT_SPAWN_VIOLATIONS
        violations: list[str] = []

        for py_file in SRC_ROOT.rglob("*.py"):
            rel = str(py_file.relative_to(SRC_ROOT))
            if rel in allowed:
                continue
            spawn_lines = _find_pexpect_spawn_calls(py_file)
            if spawn_lines:
                violations.append(f"{rel}: lines {spawn_lines}")

        assert not violations, (
            "Unauthorized pexpect.spawn() calls found outside AgentRunner.\n"
            "All agent process spawning must go through execution/agent_runner.py.\n"
            "Violations:\n" + "\n".join(f"  - {v}" for v in violations)
        )

    def test_known_violations_still_exist(self) -> None:
        """Each entry in KNOWN_PEXPECT_SPAWN_VIOLATIONS must actually use pexpect.spawn.

        If a violation is removed (migrated), remove it from the allowlist.
        Stale entries indicate the migration is done but the allowlist wasn't updated.
        """
        for rel_path in KNOWN_PEXPECT_SPAWN_VIOLATIONS:
            source_file = SRC_ROOT / rel_path
            assert source_file.exists(), (
                f"Known violation {rel_path!r} does not exist — "
                f"remove it from KNOWN_PEXPECT_SPAWN_VIOLATIONS"
            )
            spawn_lines = _find_pexpect_spawn_calls(source_file)
            assert spawn_lines, (
                f"{rel_path} no longer calls pexpect.spawn() — "
                f"remove it from KNOWN_PEXPECT_SPAWN_VIOLATIONS (migration complete!)"
            )


class TestSubprocessPluginDelegatesToAgentRunner:
    """SubprocessPlugin must delegate to AgentRunner, not use pexpect directly."""

    def test_terminal_subprocess_does_not_import_pexpect(self) -> None:
        """terminal_subprocess.py must not import pexpect.

        After migration to AgentRunner, all PTY management is handled by
        AgentRunner.start(). Direct pexpect usage would re-create the
        divergent I/O path that caused the SIGTTIN bug.
        """
        source_file = SRC_ROOT / "execution" / "terminal_subprocess.py"
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "pexpect", (
                        "terminal_subprocess.py must not import pexpect — "
                        "delegate to AgentRunner instead"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module and "pexpect" in node.module:
                    pytest.fail(
                        "terminal_subprocess.py must not import from pexpect — "
                        "delegate to AgentRunner instead"
                    )

    def test_terminal_subprocess_uses_agent_runner(self) -> None:
        """terminal_subprocess.py must import from agent_runner."""
        source_file = SRC_ROOT / "execution" / "terminal_subprocess.py"
        source = source_file.read_text(encoding="utf-8")
        assert "from .agent_runner import" in source, (
            "terminal_subprocess.py must import from .agent_runner"
        )
        assert "AgentRunner" in source, (
            "terminal_subprocess.py must use AgentRunner"
        )
        assert "AgentSpec" in source, (
            "terminal_subprocess.py must use AgentSpec"
        )


def test_persistent_session_exchange_does_not_own_ad_hoc_ui_log_cleaning() -> None:
    """The legacy spawn-per-phase path was removed in favor of the
    persistent-session runner. The old guardrail required a transcript-log
    helper that delegated to SessionOutput; the persistent runner replaces
    that with one continuous terminal-recording.jsonl per role plus a
    chapters.json sidecar. What the new owner module must NOT do is reach
    into terminal-recording cleaning directly — those concerns live behind
    the recording writer in ``infra.terminal_recording``.
    """
    rel_path = SRC_ROOT / "execution" / "persistent_session_exchange.py"
    source = rel_path.read_text(encoding="utf-8")
    assert "clean_terminal_line(" not in source, (
        f"{rel_path.name} must not own ad hoc UI log cleaning"
    )
    assert "transcript.log" not in source, (
        f"{rel_path.name} must not write the legacy review-exchange transcript.log"
    )


class TestScriptSessionRunnerUsesAgentRunner:
    """ScriptSessionRunner must delegate to AgentRunner, not subprocess.run."""

    def test_no_subprocess_run_in_script_session_runner(self) -> None:
        """ScriptSessionRunner must NOT use subprocess.run for session execution.

        If this fails, someone bypassed the unified AgentRunner, re-creating
        the divergent I/O path that caused #4057.
        """
        conftest = TESTS_ROOT / "simulated_scenarios" / "conftest.py"
        tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))

        # Find the ScriptSessionRunner class
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "ScriptSessionRunner":
                continue
            source_segment = ast.get_source_segment(
                conftest.read_text(encoding="utf-8"), node,
            )
            assert source_segment is not None
            assert "subprocess.run" not in source_segment, (
                "ScriptSessionRunner must not use subprocess.run — "
                "it must delegate to AgentRunner for PTY-realistic execution"
            )
            assert "capture_output" not in source_segment, (
                "ScriptSessionRunner must not use capture_output — "
                "output flows through AgentRunner's PTY to CleaningLogWriter"
            )
            return

        pytest.fail("ScriptSessionRunner class not found in conftest.py")


class TestPtyAgentRunnerImplementation:
    """Verify the PTY AgentRunner uses the correct mechanisms."""

    def test_uses_pexpect_not_subprocess(self) -> None:
        """AgentRunner must use pexpect (PTY), not raw subprocess."""
        from issue_orchestrator.execution import agent_runner

        AgentRunner = agent_runner.AgentRunner

        source = inspect.getsource(AgentRunner)
        module_source = inspect.getsource(agent_runner)
        assert "class _PexpectSpawnWithFileDescriptors(pexpect.spawn)" in module_source, (
            "AgentRunner's PTY process must derive from pexpect.spawn"
        )
        assert "_PexpectSpawnWithFileDescriptors(" in source, (
            "AgentRunner must acquire its PTY through the descriptor-owning spawn type"
        )
        assert "subprocess.Popen" not in source, (
            "AgentRunner must not use subprocess.Popen — use pexpect.spawn"
        )
        assert "subprocess.run" not in source, (
            "AgentRunner must not use subprocess.run — use pexpect.spawn"
        )

    def test_does_not_use_pipe_capture(self) -> None:
        """AgentRunner must NOT capture stdout/stderr with PIPE."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "subprocess.PIPE" not in source, (
            "AgentRunner must not use PIPE — output flows through PTY"
        )
        assert "capture_output" not in source, (
            "AgentRunner must not use capture_output — output flows through PTY"
        )

    def test_does_not_use_setsid(self) -> None:
        """AgentRunner must NOT use start_new_session=True (setsid).

        setpgrp (process group) is correct; setsid (new session) disconnects
        the controlling terminal, breaking PTY output (see #4057).
        """
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "start_new_session" not in source, (
            "AgentRunner must not use start_new_session — "
            "process group isolation via pexpect is sufficient"
        )
        assert "setsid" not in source, (
            "AgentRunner must not use setsid — "
            "process group isolation via pexpect is sufficient"
        )

    def test_uses_cleaning_log_writer(self) -> None:
        """AgentRunner must route output through CleaningLogWriter."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "CleaningLogWriter" in source, (
            "AgentRunner must use CleaningLogWriter for ANSI/spinner filtering"
        )

    def test_uses_filtered_env(self) -> None:
        """AgentRunner must filter the environment (credential scrubbing)."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "build_filtered_env" in source, (
            "AgentRunner must use build_filtered_env for security"
        )

    def test_uses_preexec_fn(self) -> None:
        """AgentRunner must pass preexec_fn=_pty_preexec to pexpect.spawn."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "_pty_preexec" in source, (
            "AgentRunner must use preexec_fn=_pty_preexec for SIGTTIN immunity"
        )

    def test_records_and_reuses_explicit_pty_dimensions(self) -> None:
        """AgentRunner must choose PTY geometry explicitly for faithful replay."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "shutil.get_terminal_size" in source, (
            "AgentRunner must choose an explicit PTY size instead of relying on implicit defaults"
        )
        assert "dimensions=(rows, cols)" in source, (
            "AgentRunner must pass explicit PTY dimensions to pexpect.spawn for replay fidelity"
        )


class TestSubprocessAgentRunnerConfiguration:
    """Verify SubprocessAgentRunner has correct subprocess configuration."""

    def test_does_not_pipe_stdout(self) -> None:
        """SubprocessAgentRunner must NOT use stdout=subprocess.PIPE.

        Stdout must inherit the parent PTY for real-time streaming to
        ui-session.log. Using PIPE causes block-buffering.
        """
        from issue_orchestrator.execution.subprocess_runner import SubprocessAgentRunner

        source = inspect.getsource(SubprocessAgentRunner)
        assert "stdout=subprocess.PIPE" not in source, (
            "SubprocessAgentRunner must NOT pipe stdout — inherit for real-time streaming"
        )

    def test_pipes_stderr(self) -> None:
        """SubprocessAgentRunner must capture stderr via PIPE for error classification."""
        from issue_orchestrator.execution.subprocess_runner import SubprocessAgentRunner

        source = inspect.getsource(SubprocessAgentRunner)
        assert "stderr=subprocess.PIPE" in source, (
            "SubprocessAgentRunner must capture stderr via PIPE for provider error classification"
        )

    def test_uses_devnull_stdin(self) -> None:
        """SubprocessAgentRunner must use stdin=subprocess.DEVNULL to prevent SIGTTIN."""
        from issue_orchestrator.execution.subprocess_runner import SubprocessAgentRunner

        source = inspect.getsource(SubprocessAgentRunner)
        assert "subprocess.DEVNULL" in source, (
            "SubprocessAgentRunner must use stdin=DEVNULL — see #4258 SIGTTIN fix"
        )

    def test_uses_agent_preexec(self) -> None:
        """SubprocessAgentRunner must use preexec_fn=_agent_preexec."""
        from issue_orchestrator.execution.subprocess_runner import SubprocessAgentRunner

        source = inspect.getsource(SubprocessAgentRunner)
        assert "_agent_preexec" in source, (
            "SubprocessAgentRunner must use preexec_fn=_agent_preexec for process group "
            "isolation and SIGTTIN/SIGTTOU immunity"
        )

    def test_extends_base_agent_runner(self) -> None:
        """SubprocessAgentRunner must extend BaseAgentRunner."""
        from issue_orchestrator.execution.agent_runner_base import BaseAgentRunner
        from issue_orchestrator.execution.subprocess_runner import SubprocessAgentRunner

        assert issubclass(SubprocessAgentRunner, BaseAgentRunner), (
            "SubprocessAgentRunner must extend BaseAgentRunner"
        )

    def test_pty_runner_extends_base_agent_runner(self) -> None:
        """AgentRunner (PTY) must extend BaseAgentRunner."""
        from issue_orchestrator.execution.agent_runner import AgentRunner
        from issue_orchestrator.execution.agent_runner_base import BaseAgentRunner

        assert issubclass(AgentRunner, BaseAgentRunner), (
            "AgentRunner must extend BaseAgentRunner"
        )
