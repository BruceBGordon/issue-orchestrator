"""Integration tests for Codex CLI execution.

These tests verify that we can actually execute Codex CLI commands
and that the completion command protocol works end-to-end.

Note: AgentRunner does NOT capture stdout/stderr. Output flows through
the parent's PTY (pexpect) to CleaningLogWriter. These tests verify
exit codes, working directory, and completion protocol — not output capture.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.xdist_group("codex"),
    pytest.mark.usefixtures("isolated_codex_home"),
]

from issue_orchestrator.infra.env import ENV_PREFIX

from .conftest import xdist_timeout


def is_codex_available() -> bool:
    """Check if codex CLI is available in PATH."""
    return shutil.which("codex") is not None


@pytest.fixture
def require_codex():
    """Fixture that fails fast if Codex CLI is not installed."""
    if not is_codex_available():
        pytest.fail("Codex CLI not found!\nInstall Codex: npm install -g @openai/codex")


@pytest.mark.skipif(not is_codex_available(), reason="Codex CLI not installed")
class TestCodexExecution:
    """Integration tests that actually run Codex CLI."""

    def test_codex_version(self):
        """Verify codex CLI is accessible and responds to --version."""
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(30),
        )
        assert result.returncode == 0
        # Version output should contain a version number
        assert result.stdout.strip() or result.stderr.strip()

    def test_codex_help(self):
        """Verify codex CLI responds to --help."""
        result = subprocess.run(
            ["codex", "exec", "--help"],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(30),
        )
        assert result.returncode == 0
        assert "non-interactively" in result.stdout.lower()

    def test_codex_simple_calculation(self, tmp_path):
        """Run Codex with a simple calculation task to verify execution works.

        This tests that:
        1. Codex can be invoked via subprocess
        2. The exec subcommand works for non-interactive output
        3. Codex can perform a simple task and return results
        """
        # Initialize a git repo (Codex requires this)
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        # Create a dummy file so git has something
        (tmp_path / "README.md").write_text("# Test\n")
        subprocess.run(
            ["git", "add", "."], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        result = subprocess.run(
            [
                "codex",
                "--ask-for-approval",
                "on-request",
                "exec",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "What is 2 + 2? Reply with just the number, no explanation.",
            ],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(120),
            cwd=tmp_path,
        )

        # Codex should exit successfully
        assert result.returncode == 0, f"Codex failed: {result.stderr}"

        # The output should contain "4"
        assert "4" in result.stdout, f"Expected '4' in output: {result.stdout}"


@pytest.mark.skipif(not is_codex_available(), reason="Codex CLI not installed")
class TestCodexWithAgentRunner:
    """Integration tests for Codex via AgentRunner."""

    def test_codex_via_agent_runner_returns_exit_code(self, tmp_path):
        """Test that AgentRunner correctly runs Codex and returns exit code.

        AgentRunner inherits stdout/stderr (PTY passthrough). We verify
        process management, not output capture.
        """
        # Use vendored AgentRunner — Codex tests exercise the subprocess-based
        # runner (not the unified pexpect-based one in execution.agent_runner).
        from issue_orchestrator.execution.subprocess_runner import (
            SubprocessAgentRunner as AgentRunner,
        )
        from issue_orchestrator.execution.agent_runner_types import AgentSpec as RunSpec
        from issue_orchestrator.agent_runner.providers import CodexProvider

        # Set up a git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        (tmp_path / "README.md").write_text("# Test\n")
        subprocess.run(
            ["git", "add", "."], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        provider = CodexProvider()
        cmd = provider.build_command(
            prompt="Echo the word SUCCESS and exit.",
            execution_mode="exec",
            working_directory=tmp_path,
            json_output="false",
        )

        runner = AgentRunner()
        spec = RunSpec(
            command=cmd,
            working_dir=tmp_path,
            timeout_seconds=120,
            output_dir=output_dir,
        )

        result = runner.run(spec)

        assert result.duration_seconds > 0
        print(f"Codex exit_code: {result.exit_code}")
        print(f"Codex stderr (launch errors only): {result.stderr}")


@pytest.mark.skipif(not is_codex_available(), reason="Codex CLI not installed")
class TestCodexAgentDoneInvocation:
    """Integration tests for completion command invocation from Codex.

    These tests verify the critical path: Codex can invoke coding-done/reviewer-done
    and write completion.json, which is how sessions signal completion.
    """

    def test_agent_done_invocable_from_codex(self, tmp_path, require_codex):
        """Verify Codex can invoke completion commands in worktree-like environment.

        This tests the exact mechanism the orchestrator relies on:
        1. PATH includes scripts directory with agent-done wrapper
        2. Codex runs with exec subcommand (non-interactive)
        3. Codex invokes coding-done via shell
        4. completion.json is written
        """
        # Get the scripts directory (where completion command wrappers live)
        repo_root = Path(__file__).parent.parent.parent
        scripts_dir = repo_root / "src" / "issue_orchestrator" / "scripts"

        # Create worktree-like structure with git repo + local origin remote.
        # Completion command preflight now validates push viability, so origin must exist.
        remote_repo = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote_repo)], capture_output=True, check=True
        )

        worktree = tmp_path / "test-worktree"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=worktree,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=worktree,
            capture_output=True,
            check=True,
        )
        (worktree / "README.md").write_text("# Test\n")
        subprocess.run(
            ["git", "add", "."], cwd=worktree, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=worktree,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_repo)],
            cwd=worktree,
            capture_output=True,
            check=True,
        )

        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()

        # Build environment like orchestrator does:
        # - Prepend scripts directory to PATH
        # - Set completion path
        env = dict(os.environ)
        env["PATH"] = f"{scripts_dir}:{env.get('PATH', '')}"
        env[f"{ENV_PREFIX}COMPLETION_PATH"] = str(completion_dir / "completion.json")

        # Run Codex with exec asking it to invoke the completion command
        prompt = (
            "You are in a test. Run this exact bash command and nothing else:\n"
            "agent-done completed --implementation 'test' --problems 'none'\n"
            "Do not explain, just run the command using the shell tool."
        )

        result = subprocess.run(
            [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(180),
            cwd=str(worktree),
            env=env,
        )

        # Log output for debugging
        print(f"Codex stdout: {result.stdout}")
        print(f"Codex stderr: {result.stderr}")
        print(f"Return code: {result.returncode}")

        # Check for completion.json
        completion_files = list(completion_dir.glob("completion*.json"))
        assert len(completion_files) > 0, (
            f"No completion.json written!\n"
            f"Codex stdout: {result.stdout}\n"
            f"Codex stderr: {result.stderr}\n"
            f"Return code: {result.returncode}\n"
            f"Files in {completion_dir}: {list(completion_dir.iterdir())}"
        )

        # Validate completion record
        completion_path = completion_files[0]
        completion_data = json.loads(completion_path.read_text())
        assert completion_data.get("outcome") == "completed", (
            f"Unexpected outcome: {completion_data}"
        )

    def test_codex_file_creation(self, tmp_path, require_codex):
        """Test that Codex can create files (simpler than completion command).

        This is a simpler verification that Codex can execute commands
        and create files in the working directory.
        """
        # Set up a git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        (tmp_path / "README.md").write_text("# Test\n")
        subprocess.run(
            ["git", "add", "."], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        verify_file = tmp_path / "codex_was_here.txt"

        result = subprocess.run(
            [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                f"Create a file at {verify_file} containing exactly the text VERIFIED. "
                "Use the write_file tool or echo command. Do not explain, just do it.",
            ],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(180),
            cwd=str(tmp_path),
        )

        print(f"Codex stdout: {result.stdout}")
        print(f"Codex stderr: {result.stderr}")
        print(f"Return code: {result.returncode}")

        # Verify Codex created the file
        assert verify_file.exists(), (
            f"Codex did not create verification file. Output: {result.stdout}"
        )
        content = verify_file.read_text().strip()
        assert "VERIFIED" in content, f"Verification file has wrong content: {content}"


@pytest.mark.skipif(not is_codex_available(), reason="Codex CLI not installed")
class TestCodexWithAgentRunnerFullPath:
    """E2E test that runs Codex through AgentRunner with full orchestrator path."""

    def test_codex_via_agent_runner_with_agent_done(self, tmp_path, require_codex):
        """Run Codex via AgentRunner and verify completion command works.

        This tests the full integration path:
        1. AgentRunner invokes Codex
        2. Codex executes task
        3. Codex calls coding-done
        4. completion.json is written

        Note: AgentRunner does NOT capture output. Output flows through the
        parent's PTY. We only verify process management and completion protocol.
        """
        # Use vendored AgentRunner — Codex tests exercise the subprocess-based
        # runner (not the unified pexpect-based one in execution.agent_runner).
        from issue_orchestrator.execution.subprocess_runner import (
            SubprocessAgentRunner as AgentRunner,
        )
        from issue_orchestrator.execution.agent_runner_types import AgentSpec as RunSpec
        from issue_orchestrator.agent_runner.providers import CodexProvider

        # Get scripts directory
        repo_root = Path(__file__).parent.parent.parent
        scripts_dir = repo_root / "src" / "issue_orchestrator" / "scripts"

        # Set up worktree
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=worktree,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=worktree,
            capture_output=True,
            check=True,
        )
        (worktree / "README.md").write_text("# Test\n")
        subprocess.run(
            ["git", "add", "."], cwd=worktree, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=worktree,
            capture_output=True,
            check=True,
        )

        io_dir = worktree / ".issue-orchestrator"
        io_dir.mkdir()

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        provider = CodexProvider()
        prompt = (
            "You are in a test. Run this exact bash command:\n"
            "agent-done completed --implementation 'AgentRunner test' --problems 'none'\n"
            "Do not explain, just run it."
        )

        cmd = provider.build_command(
            prompt=prompt,
            execution_mode="exec",
            approval_mode="full-auto",
            working_directory=worktree,
            json_output="false",
        )

        runner = AgentRunner()
        spec = RunSpec(
            command=cmd,
            working_dir=worktree,
            timeout_seconds=180,
            output_dir=output_dir,
            env_overrides={
                "PATH": f"{scripts_dir}:{os.environ.get('PATH', '')}",
                f"{ENV_PREFIX}COMPLETION_PATH": str(io_dir / "completion.json"),
            },
        )

        result = runner.run(spec)

        print(f"AgentRunner result:")
        print(f"  exit_code: {result.exit_code}")
        print(f"  timed_out: {result.timed_out}")
        print(f"  duration: {result.duration_seconds:.1f}s")
        print(
            f"  stderr (launch errors): {result.stderr[:500] if result.stderr else 'empty'}"
        )

        # Check for completion.json
        completion_files = list(io_dir.glob("completion*.json"))
        assert len(completion_files) > 0, (
            f"No completion.json written!\n"
            f"stderr: {result.stderr}\n"
            f"Files in {io_dir}: {list(io_dir.iterdir())}"
        )

        # Validate completion record
        completion_path = completion_files[0]
        completion_data = json.loads(completion_path.read_text())
        assert completion_data.get("outcome") == "completed"
        assert "AgentRunner test" in completion_data.get("implementation", "")
