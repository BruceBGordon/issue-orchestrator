"""Integration tests for Claude Code execution.

These tests verify that we can actually execute Claude Code commands
and that the command escaping works correctly in real shells.
"""

import os
import pytest
from collections.abc import Mapping

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.provider_claude,
    # Run PTY tests sequentially in one worker to avoid Python 3.14 forkpty warning
    # (forkpty() in multi-threaded processes can deadlock)
    pytest.mark.xdist_group("pty"),
]
import subprocess
import shutil
from pathlib import Path
from uuid import uuid4

from issue_orchestrator.infra.env import ENV_PREFIX
from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalShell,
)
from tests.process_group_run import run_in_process_group
from tests.unit.session_run_helpers import make_session_run_assets

from .conftest import xdist_timeout

ISSUE_4057_PROMPT = """IMPORTANT: This worktree has 54 existing commit(s) from a previous session. Branch: 4057-ui-surface-provider-circuit-breaker-status. Commits:   - 3592261 fix: Improve session log symlink handling in provider_runner
  - 7d3af45 fix: Mark agent-done integration tests as xfail due to provider_runner issue
  - d088aab fix: Mark foreign repo lifecycle tests as xfail due to provider_runner integration issue
  - 6464759 chore: Update session tracking for issue #4057 worktree completion
  - 39cd869 chore: Update session tracking
  - 96cd3a6 fix: Add missing mock return values for tracking branch setup in worktree tests
  - 5004703 chore: Update session tracking after evaluation completion
  - 99fcf51 chore: Update session tracking after validation completion
  - 79567ab chore: Update session tracking after validation completion
  - 710e19c chore: Update session tracking after final validation
  ... and 44 more. EVALUATE this existing work BEFORE starting fresh.

Work on issue #4057: UI: Surface provider circuit breaker status. Follow the instructions in repo-specific/prompts/simple-fix.md. When done, exit with /exit."""


def is_claude_available() -> bool:
    """Check if claude CLI is available in PATH."""
    return shutil.which("claude") is not None


def _run_claude(
    argv: list[str],
    *,
    timeout: int,
    retry_timeout: int | None = None,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    retry_timeout = retry_timeout or (timeout * 2)
    try:
        return _run_claude_once(argv, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as exc:
        # Live Claude CLI calls can intermittently stall under heavily parallelized
        # integration runs; retry once with a longer timeout before failing.
        try:
            return _run_claude_once(argv, timeout=retry_timeout, cwd=cwd, env=env)
        except subprocess.TimeoutExpired as retry_exc:
            stdout = (retry_exc.stdout or exc.stdout or "")[:500]
            stderr = (retry_exc.stderr or exc.stderr or "")[:500]
            raise AssertionError(
                "Claude command timed out after retry.\n"
                f"initial_timeout={timeout}s retry_timeout={retry_timeout}s\n"
                f"stdout (truncated): {stdout}\n"
                f"stderr (truncated): {stderr}"
            ) from retry_exc


def _run_claude_once(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | None,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess:
    """Run claude once, killing its whole process tree if it stalls.

    Process-tree cleanup is owned by ``tests/process_group_run`` so the live
    probes cannot drift apart on it.
    """
    return run_in_process_group(argv, cwd=cwd, timeout=timeout, env=env)


@pytest.fixture
def require_claude():
    """Fixture that fails fast if Claude CLI is not installed."""
    if not is_claude_available():
        pytest.fail(
            "Claude CLI not found!\n"
            "Install Claude: https://claude.ai/download"
        )


@pytest.mark.skipif(not is_claude_available(), reason="Claude CLI not installed")
class TestClaudeExecution:
    """Integration tests that actually run Claude Code."""

    def test_claude_version(self):
        """Verify claude CLI is accessible and responds to --version."""
        result = _run_claude(["claude", "--version"], timeout=xdist_timeout(30))
        assert result.returncode == 0
        assert "claude" in result.stdout.lower() or "Claude" in result.stdout

    def test_claude_print_single_turn_token(self):
        """Run Claude with a deterministic token task to verify execution works.

        This tests that:
        1. Claude can be invoked via subprocess
        2. The --print flag works for non-interactive output
        3. Claude can return a simple response
        """
        expected_token = f"CLAUDE_PRINT_OK_{uuid4().hex}"

        # Use --print for non-interactive single-turn execution
        result = _run_claude(
            [
                "claude",
                "--print",  # Output response and exit (non-interactive)
                "--model",
                "haiku",
                f"Reply with exactly this token and nothing else: {expected_token}",
            ],
            timeout=xdist_timeout(60),  # Give Claude time to respond
        )

        # Claude should exit successfully
        assert result.returncode == 0, f"Claude failed: {result.stderr}"

        assert expected_token in result.stdout, (
            f"Expected {expected_token!r} in output: {result.stdout}"
        )

    def test_claude_command_with_single_quotes(self):
        """Test that commands with single quotes work correctly via zsh -l -c wrapper.

        This is the actual pattern used by the orchestrator.
        """
        # Build a command similar to what orchestrator generates
        inner_command = (
            "claude --print --model haiku 'Reply with just the word: hello'"
        )

        # Escape single quotes for zsh wrapper (the fix we just made)
        escaped_command = inner_command.replace("'", "'\\''")

        # Wrap in zsh -l -c (like TmuxManager.create_session does)
        wrapped_command = f"zsh -l -c '{escaped_command}'"

        result = _run_claude(["bash", "-c", wrapped_command], timeout=xdist_timeout(60))

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert "hello" in result.stdout.lower(), f"Expected 'hello' in output: {result.stdout}"


    def test_claude_read_file_with_bypass_permissions(self):
        """Test that Claude can read files when permissions are bypassed.

        This tests the scenario where orchestrator runs unattended and needs
        to read instruction files without waiting for permission prompts.
        """
        # Create a test file to read
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("# Test Instructions\n\nThis is a test file for Claude to read.\n")
            test_file = f.name

        try:
            result = _run_claude(
                [
                    "claude",
                    "--print",
                    "--model",
                    "haiku",
                    "--dangerously-skip-permissions",  # Bypass permission prompts
                    f"Read the file at {test_file} and tell me what the title is. Reply with just the title text.",
                ],
                timeout=xdist_timeout(120),
            )

            assert result.returncode == 0, f"Claude failed: {result.stderr}"
            # Should find "Test Instructions" in the output
            assert "Test Instructions" in result.stdout or "test" in result.stdout.lower(), \
                f"Expected file content reference in output: {result.stdout}"
        finally:
            Path(test_file).unlink(missing_ok=True)

    def test_claude_read_from_specific_directory(self):
        """Test reading from a subdirectory like docs/10-ai/.

        This mimics what the orchestrator does when agents read their instructions.
        """
        import tempfile
        import os

        # Create a directory structure like docs/10-ai/
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir) / "docs" / "10-ai"
            ai_dir.mkdir(parents=True)

            # Create an instruction file
            instruction_file = ai_dir / "test-instructions.md"
            instruction_file.write_text("# Agent Instructions\n\nDo the thing.\n")

            result = _run_claude(
                [
                    "claude",
                    "--print",
                    "--model",
                    "haiku",
                    "--dangerously-skip-permissions",
                    f"Read {instruction_file} and confirm you can see 'Agent Instructions' in it. Reply YES or NO.",
                ],
                timeout=xdist_timeout(120),
                cwd=tmpdir,  # Run from the temp directory
            )

            assert result.returncode == 0, f"Claude failed: {result.stderr}"
            assert "YES" in result.stdout.upper() or "agent" in result.stdout.lower(), \
                f"Claude couldn't read the file: {result.stdout}"


@pytest.mark.skipif(not is_claude_available(), reason="Claude CLI not installed")
class TestClaudeWithEnvironmentIsolation:
    """Integration tests for Claude with environment isolation (no HOME isolation).

    The orchestrator scrubs sensitive environment variables (GH_TOKEN, AWS_*, etc.)
    but does NOT isolate HOME. This allows Claude to access macOS Keychain for
    subscription authentication while still preventing credential leakage.
    """

    def test_claude_works_with_scrubbed_env(self):
        """Verify Claude authenticates when dangerous env vars are scrubbed.

        This tests the actual isolation mode: scrub credentials but keep HOME.
        Claude uses macOS Keychain for subscription auth, not ~/.claude.json.
        """
        import os
        from issue_orchestrator.control.isolation import FORBIDDEN_ENV_VARS

        # Build environment with scrubbed credentials but keeping HOME
        clean_env = dict(os.environ)
        for var in FORBIDDEN_ENV_VARS:
            clean_env.pop(var, None)

        # HOME is NOT changed - Claude can access Keychain

        result = _run_claude(
            [
                "claude",
                "--print",
                "--model",
                "haiku",
                "Reply with just the word: working",
            ],
            timeout=xdist_timeout(60),
            env=clean_env,
        )

        # Claude should work with subscription auth via Keychain
        assert result.returncode == 0, f"Claude failed: {result.stderr}"
        assert "working" in result.stdout.lower(), f"Unexpected output: {result.stdout}"

    def test_claude_fails_with_isolated_home(self, tmp_path):
        """Document that HOME isolation breaks Claude subscription auth.

        Claude stores OAuth tokens in macOS Keychain, which is tied to HOME.
        When HOME is isolated to a worktree, Keychain access fails.

        This is why we disabled HOME isolation.
        """
        import os

        # Create an isolated HOME directory
        isolated_home = tmp_path / "isolated_home"
        isolated_home.mkdir()

        clean_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(isolated_home),  # This breaks Keychain access
        }

        result = subprocess.run(
            [
                "claude",
                "--print",
                "--model",
                "haiku",
                "hello",
            ],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(60),
            env=clean_env,
        )

        # Should fail with auth error (expected - documenting the limitation)
        combined_output = result.stdout + result.stderr
        assert (
            result.returncode != 0
            or "Invalid API key" in combined_output
            or "API key" in combined_output
            or "login" in combined_output.lower()
        ), f"Expected auth failure with isolated HOME, but got: {combined_output}"


class TestShellEscaping:
    """Test POSIX single-quote escaping used by terminal adapters.

    The escaping pattern replace("'", "'\\''") is POSIX standard and works
    identically in bash and zsh. Production uses 'zsh -l -c' for login shell
    PATH setup, but the escaping itself is shell-agnostic.
    """

    def test_single_quote_escaping(self):
        """Verify the POSIX single-quote escaping pattern works.

        This tests the escaping used by terminal adapters: replace("'", "'\\''")
        The pattern: end quote, escaped literal quote, start quote.
        """
        # This is the pattern: replace ' with '\''
        original = "echo 'hello world'"
        escaped = original.replace("'", "'\\''")
        wrapped = f"bash -c '{escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(5),
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert "hello world" in result.stdout

    def test_complex_quoting_pattern(self):
        """Test the quoting pattern with multiple quoted arguments."""
        command = "echo --flag 'value with spaces' 'another value'"
        escaped = command.replace("'", "'\\''")
        wrapped = f"bash -c 'cd /tmp && {escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(5),
        )

        assert result.returncode == 0
        assert "value with spaces" in result.stdout
        assert "another value" in result.stdout

    def test_nested_quotes_in_prompt(self):
        """Test quoting when the prompt itself contains quotes."""
        # Prompts may contain quotes like: "Fix the 'broken' feature"
        prompt = "This has 'single' and \"double\" quotes"
        # For shell safety, we escape single quotes
        escaped_prompt = prompt.replace("'", "'\\''")
        command = f"echo '{escaped_prompt}'"
        escaped = command.replace("'", "'\\''")
        wrapped = f"bash -c '{escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(5),
        )

        assert result.returncode == 0
        # The output should contain the original text (with quotes resolved)
        assert "single" in result.stdout or "double" in result.stdout


@pytest.mark.skipif(not is_claude_available(), reason="Claude CLI not installed")
class TestClaudeViaAdapterPath:
    """E2E test that runs Claude through the same path as tmux adapters.

    This is the critical test that verifies the full orchestrator session path:
    1. Create a worktree directory
    2. Build isolation prefix (scrub env, but NOT isolate HOME)
    3. Wrap in zsh -l -c '...' like the adapters do
    4. Run Claude and verify it authenticates via macOS Keychain
    """

    def test_claude_via_adapter_isolation_path(self, tmp_path, require_claude):
        """Run Claude through the exact isolation path used by terminal adapters.

        This tests the fix for: "Invalid API key · Please run /login"

        The orchestrator's terminal adapters (tmux) wrap commands like:
            zsh -l -c '{isolation_prefix}cd "{worktree}" && claude ...'

        With isolate_home=False, Claude can access macOS Keychain for auth.
        With isolate_home=True (the bug), Keychain access fails.

        VERIFICATION: Claude must create a file we can check, proving it actually ran.
        """
        from issue_orchestrator.control.isolation import build_isolation_prefix

        # Create a fake worktree directory (like orchestrator does)
        worktree = tmp_path / "test-worktree"
        worktree.mkdir()

        # Create the .issue-orchestrator directory (required for completion commands)
        io_dir = worktree / ".issue-orchestrator"
        io_dir.mkdir()

        # Build isolation prefix EXACTLY like the adapters do
        # This is the critical part - isolate_home=False allows Keychain auth
        isolation_prefix = build_isolation_prefix(
            worktree=worktree,
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=False,  # Must be False for Keychain auth
        )

        # The verification file Claude must create
        verify_file = worktree / "claude_was_here.txt"

        # Build the claude command - ask Claude to create a file we can verify
        # This proves Claude actually ran and executed tools, not just responded
        claude_cmd = (
            f"claude --print --model haiku --dangerously-skip-permissions "
            f"'Create a file at {verify_file} containing exactly the text VERIFIED. "
            f"Use the Write tool. Reply with DONE when complete.'"
        )

        # Escape single quotes for zsh wrapper (same as tmux adapter)
        escaped_cmd = claude_cmd.replace("'", "'\\''")

        # Build full command like tmux adapter does:
        # zsh -l -c '{isolation_prefix}cd "{worktree}" && {command}'
        full_cmd = f'{isolation_prefix}cd "{worktree}" && {escaped_cmd}'
        zsh_wrapped = f"zsh -l -c '{full_cmd}'"

        # Run it (simulates what tmux sends to the terminal)
        result = subprocess.run(
            ["bash", "-c", zsh_wrapped],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(120),
        )

        combined = result.stdout + result.stderr

        # Should NOT see auth failure
        assert "Invalid API key" not in combined, f"Auth failed (HOME isolation bug?): {combined}"
        assert "Please run /login" not in combined, f"Auth failed: {combined}"

        # Should succeed
        assert result.returncode == 0, f"Claude failed: {combined}"

        # CRITICAL: Verify Claude actually created the file
        assert verify_file.exists(), f"Claude did not create verification file. Output: {combined}"
        content = verify_file.read_text().strip()
        assert "VERIFIED" in content, f"Verification file has wrong content: {content}"

    def test_claude_via_subprocess_backend(self, tmp_path, require_claude, monkeypatch):
        """Run Claude via subprocess backend in a real git worktree."""
        from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin

        source_repo_root = Path(__file__).resolve().parents[2]
        worktree = tmp_path / f"real-worktree-{uuid4().hex[:8]}"
        subprocess.run(
            [
                "git", "-C", str(source_repo_root), "worktree", "add",
                "--detach", str(worktree), "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            (worktree / ".issue-orchestrator").mkdir(exist_ok=True)
            plugin_state_root = tmp_path / "plugin-state-root"
            plugin_state_root.mkdir()
            monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(plugin_state_root))

            escaped_prompt = ISSUE_4057_PROMPT.replace('"', '\\"')
            session_name = "issue-999"
            run_assets = make_session_run_assets(worktree, session_name=session_name)
            claude_cmd = (
                f"export {ENV_PREFIX}RUN_DIR='{run_assets.run_dir}' && "
                "claude --print --model haiku --dangerously-skip-permissions "
                f"\"{escaped_prompt}\""
            )

            plugin = SubprocessPlugin()
            created = plugin.create_session(
                session_id=999,
                command=claude_cmd,
                interaction_intent=TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE,
                shell=TerminalShell.BASH,
                working_dir=str(worktree),
                title="Claude subprocess integration",
                session_name=session_name,
            )
            assert created is True

            log_path = run_assets.log_path

            # Confirm real #4057-like session startup and run-scoped log creation.
            import time
            deadline = time.monotonic() + 60
            session_became_live = False
            while time.monotonic() < deadline:
                if plugin.session_exists(0, session_name):
                    session_became_live = True
                if session_became_live and log_path.exists():
                    break
                time.sleep(0.2)
            else:
                plugin.kill_session(0, session_name)
                raise AssertionError("Claude subprocess session did not become live with run-scoped log")

            if plugin.session_exists(0, session_name):
                plugin.kill_session(0, session_name)
                exit_deadline = time.monotonic() + 30
                while time.monotonic() < exit_deadline:
                    if not plugin.session_exists(0, "issue-999"):
                        break
                    time.sleep(0.2)

            assert log_path.exists()
            log_size = log_path.stat().st_size
            log_preview = log_path.read_text(errors="replace").splitlines()[:12]
            print(f"ui-session.log path: {log_path}")
            print(f"ui-session.log bytes: {log_size}")
            print("ui-session.log preview:")
            for line in log_preview:
                print(line)
        finally:
            subprocess.run(
                ["git", "-C", str(source_repo_root), "worktree", "remove", "--force", str(worktree)],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_claude_via_adapter_path_with_home_isolation_fails(self, tmp_path):
        """Document that HOME isolation breaks Claude auth (the bug we fixed).

        This test proves WHY isolate_home must be False.
        """
        from issue_orchestrator.control.isolation import build_isolation_prefix

        worktree = tmp_path / "isolated-worktree"
        worktree.mkdir()

        # Build isolation with HOME isolation ENABLED (the bug)
        isolation_prefix = build_isolation_prefix(
            worktree=worktree,
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=True,  # This breaks Keychain auth!
        )

        claude_cmd = "claude --print --model haiku 'hello'"
        escaped_cmd = claude_cmd.replace("'", "'\\''")
        full_cmd = f'{isolation_prefix}cd "{worktree}" && {escaped_cmd}'
        zsh_wrapped = f"zsh -l -c '{full_cmd}'"

        result = subprocess.run(
            ["bash", "-c", zsh_wrapped],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(60),
        )

        combined = result.stdout + result.stderr

        # Should fail with auth error (expected - documenting the bug)
        auth_failed = (
            result.returncode != 0
            or "Invalid API key" in combined
            or "API key" in combined
            or "login" in combined.lower()
        )
        assert auth_failed, f"Expected auth failure with HOME isolation, got: {combined}"


@pytest.mark.skipif(not is_claude_available(), reason="Claude CLI not installed")
class TestAgentDoneInvocation:
    """Integration tests for completion command invocation from Claude.

    These tests verify the critical path: Claude can invoke coding-done/reviewer-done
    and write completion.json, which is how sessions signal completion.
    """

    def test_agent_done_invocable_from_claude(self, tmp_path):
        """Verify Claude can invoke completion commands in worktree-like environment.

        This tests the exact mechanism the orchestrator relies on:
        1. PATH includes scripts directory with agent-done wrapper
        2. Claude runs with -p flag (non-interactive)
        3. Claude invokes coding-done via Bash
        4. completion.json is written

        If this test fails, sessions will fail without completion.
        """
        import json
        from pathlib import Path

        # Get the scripts directory (where completion command wrappers live)
        repo_root = Path(__file__).parent.parent.parent
        scripts_dir = repo_root / "src" / "issue_orchestrator" / "scripts"

        # Create worktree-like structure with git repo
        # (Claude Code may refuse to run commands in non-git directories)
        worktree = tmp_path / "test-worktree"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()

        # Build environment like orchestrator does:
        # - Prepend scripts directory to PATH
        # - Set completion path
        import os
        env = dict(os.environ)
        env["PATH"] = f"{scripts_dir}:{env.get('PATH', '')}"
        env[f"{ENV_PREFIX}COMPLETION_PATH"] = str(completion_dir / "completion.json")

        # Run Claude with -p asking it to invoke the completion command
        prompt = (
            "You are in a test. Run this exact bash command and nothing else:\n"
            "agent-done completed --implementation 'test' --problems 'none'\n"
            "Do not explain, just run the command."
        )

        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model", "haiku",
                "--permission-mode", "bypassPermissions",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(120),
            cwd=str(worktree),
            env=env,
        )

        # Log output for debugging
        print(f"Claude stdout: {result.stdout}")
        print(f"Claude stderr: {result.stderr}")
        print(f"Return code: {result.returncode}")

        # Check for completion.json
        completion_files = list(completion_dir.glob("completion*.json"))
        assert len(completion_files) > 0, (
            f"No completion.json written!\n"
            f"Claude stdout: {result.stdout}\n"
            f"Claude stderr: {result.stderr}\n"
            f"Return code: {result.returncode}\n"
            f"Files in {completion_dir}: {list(completion_dir.iterdir())}"
        )

        # Validate completion record
        completion_path = completion_files[0]
        completion_data = json.loads(completion_path.read_text())
        assert completion_data.get("outcome") == "completed", (
            f"Unexpected outcome: {completion_data}"
        )

    def test_agent_done_wrapper_resolves_correctly(self):
        """Verify the agent-done wrapper script finds the real completion command.

        This tests the wrapper at scripts/agent-done can locate
        and execute the venv-installed coding-done/reviewer-done.
        """
        import os
        from pathlib import Path

        repo_root = Path(__file__).parent.parent.parent
        wrapper = repo_root / "src" / "issue_orchestrator" / "scripts" / "agent-done"
        venv_agent_done = repo_root / ".venv" / "bin" / "agent-done"

        # Wrapper should exist and be executable
        assert wrapper.exists(), f"Wrapper not found at {wrapper}"
        assert os.access(wrapper, os.X_OK), f"Wrapper not executable: {wrapper}"

        # Venv completion commands should exist (if venv is set up)
        if venv_agent_done.exists():
            # Run wrapper with --help to verify it forwards correctly
            env = dict(os.environ)
            env["PATH"] = f"{wrapper.parent}:{env.get('PATH', '')}"

            result = subprocess.run(
                ["agent-done", "--help"],
                capture_output=True,
                text=True,
                timeout=xdist_timeout(10),
                env=env,
            )

            assert result.returncode == 0, f"agent-done --help failed: {result.stderr}"
            assert "completed" in result.stdout.lower(), (
                f"Unexpected help output: {result.stdout}"
            )

    def test_completion_json_written_to_worktree_not_main_repo(self, tmp_path):
        """CRITICAL: Verify completion.json is written to worktree, not main repo.

        This is the exact bug that caused sessions to silently fail:
        - Claude ran in the main repo instead of the worktree
        - completion.json was written to main repo's .issue-orchestrator/
        - Orchestrator never detected completion (looking in worktree)
        - Reviews never ran, PRs never created

        The fix is that _setup_and_run must cd to working_dir FIRST.
        This test verifies that behavior end-to-end.

        KEY: coding-done uses Path.cwd() to determine where to write.
        Without cd to worktree, cwd is main repo, so completion goes there.
        With cd to worktree, cwd is worktree, so completion goes there.
        """
        import json

        # Simulate the bug scenario: main repo vs worktree
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").touch()  # Mark as git root
        (main_repo / ".issue-orchestrator").mkdir()

        worktree = tmp_path / "worktree-issue-123"
        worktree.mkdir()
        (worktree / ".git").touch()  # Worktrees have .git file
        worktree_io_dir = worktree / ".issue-orchestrator"
        worktree_io_dir.mkdir()

        # Get scripts directory
        from pathlib import Path
        repo_root = Path(__file__).parent.parent.parent
        scripts_dir = repo_root / "src" / "issue_orchestrator" / "scripts"

        # Build environment like orchestrator does
        # NOTE: We do NOT set ISSUE_ORCHESTRATOR_COMPLETION_PATH - coding-done uses cwd!
        import os
        env = dict(os.environ)
        env["PATH"] = f"{scripts_dir}:{env.get('PATH', '')}"
        # Clear any existing path to test cwd behavior
        env.pop(f"{ENV_PREFIX}COMPLETION_PATH", None)

        # The key test: we cd to worktree, then run the completion command
        # This simulates what _setup_and_run does with the cd fix
        cmd = f'cd "{worktree}" && agent-done completed --implementation "test" --problems "none"'

        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(10),
            env=env,
            # Start in main_repo to simulate the bug scenario
            cwd=str(main_repo),
        )

        # Log for debugging
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        print(f"return code: {result.returncode}")

        # Check completion.json is in WORKTREE (not main repo)
        worktree_completion = worktree_io_dir / "completion.json"
        main_completion = main_repo / ".issue-orchestrator" / "completion.json"

        assert worktree_completion.exists(), (
            f"completion.json NOT in worktree! "
            f"Worktree dir: {list(worktree_io_dir.iterdir())}, "
            f"Main repo dir: {list((main_repo / '.issue-orchestrator').iterdir())}"
        )
        assert not main_completion.exists(), (
            f"completion.json incorrectly written to main repo instead of worktree!"
        )

        # Verify content
        completion = json.loads(worktree_completion.read_text())
        assert completion["outcome"] == "completed"

    def test_completion_json_written_to_wrong_place_without_cd(self, tmp_path):
        """Verify the BUG: without cd, completion.json goes to wrong place.

        This documents the bug behavior to ensure we don't regress.
        Without the `cd` fix, coding-done would use cwd (main repo).
        """
        import json

        # Same setup as above
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").touch()
        (main_repo / ".issue-orchestrator").mkdir()

        worktree = tmp_path / "worktree-issue-123"
        worktree.mkdir()
        (worktree / ".git").touch()
        (worktree / ".issue-orchestrator").mkdir()

        from pathlib import Path
        repo_root = Path(__file__).parent.parent.parent
        scripts_dir = repo_root / "src" / "issue_orchestrator" / "scripts"

        import os
        env = dict(os.environ)
        env["PATH"] = f"{scripts_dir}:{env.get('PATH', '')}"
        env.pop(f"{ENV_PREFIX}COMPLETION_PATH", None)

        # NO cd - simulates the bug
        cmd = 'agent-done completed --implementation "test" --problems "none"'

        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(10),
            env=env,
            # cwd is main_repo - this is where the completion command will write
            cwd=str(main_repo),
        )

        # Without cd, completion goes to main_repo (the bug!)
        main_completion = main_repo / ".issue-orchestrator" / "completion.json"
        worktree_completion = worktree / ".issue-orchestrator" / "completion.json"

        # Document the bug: without cd, it goes to wrong place
        assert main_completion.exists(), (
            "BUG TEST: Expected completion.json in main_repo when no cd"
        )
        assert not worktree_completion.exists(), (
            "BUG TEST: Should NOT be in worktree when no cd"
        )


@pytest.mark.skipif(not is_claude_available(), reason="Claude CLI not installed")
def test_ai_gate_production_path_works():
    """The production AI gate path must produce a pass/fail result.

    Runs in a subprocess with CLAUDECODE stripped from the environment,
    because claude -p returns empty output when nested inside a running
    Claude Code session.  This matches production behavior where the
    orchestrator starts outside Claude Code.
    """
    import sys

    project_root = Path(__file__).parent.parent.parent
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    inner_timeout = xdist_timeout(120)
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter; "
            f"import pathlib; s, m = ClaudeCodeAdapter().test_ai_gate(pathlib.Path('{project_root}'), timeout={inner_timeout}); "
            "print(f'{s}|{m}')",
        ],
        capture_output=True, text=True, env=env, timeout=xdist_timeout(180),
    )
    assert result.returncode == 0, f"Gate subprocess failed: {result.stderr}"
    output = result.stdout.strip()
    assert "|" in output, f"Unexpected gate output: {output}"
    success_str, message = output.split("|", 1)
    assert success_str == "True", f"AI gate production path failed: {message}"
    assert "blocked" in message.lower(), f"Unexpected gate message: {message}"
