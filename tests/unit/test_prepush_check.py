"""Unit tests for the prepush_check module."""

import json
import os
import pytest
import tempfile
import subprocess
from pathlib import Path

from issue_orchestrator.entrypoints.cli_tools.prepush_check import (
    _prepush_output_dir,
    load_validation_cmd,
    run_prepush_check,
)


def _shared_timing_records(worktree: Path) -> list[dict[str, object]]:
    timings_file = worktree / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
    if not timings_file.exists():
        return []
    return [json.loads(line) for line in timings_file.read_text().splitlines()]


class TestLoadValidationCmd:
    """Tests for loading validation configuration."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            config_dir = worktree / ".issue-orchestrator"
            config_dir.mkdir()
            yield worktree

    def test_returns_none_when_no_config(self, temp_worktree):
        """Test returns None when config file doesn't exist."""
        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd is None
        assert timeout == 0
        assert dirty_check == "tracked"

    def test_returns_none_when_no_cmd(self, temp_worktree):
        """Test returns None when cmd not configured."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("some_key: value\n")

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd is None
        assert dirty_check == "tracked"

    def test_returns_cmd_when_configured(self, temp_worktree):
        """Test returns command when configured."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "pytest"
    timeout_seconds: 300
""")

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd == "pytest"
        assert timeout == 300
        assert dirty_check == "tracked"

    def test_prefers_selected_config_name_env(self, temp_worktree, monkeypatch):
        """Test selected config name env overrides default config discovery."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "main.yaml").write_text("""
validation:
  publish:
    cmd: "pytest -k selected"
    timeout_seconds: 45
""")
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_NAME", "main.yaml")

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)

        assert cmd == "pytest -k selected"
        assert timeout == 45
        assert dirty_check == "tracked"

    def test_missing_selected_config_name_raises(self, temp_worktree, monkeypatch):
        """Test explicit selected config name fails loudly when missing."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "default.yaml").write_text("""
validation:
  publish:
    cmd: "pytest"
""")
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_NAME", "main.yaml")

        with pytest.raises(
            FileNotFoundError, match="Configured file 'main.yaml' not found under"
        ):
            load_validation_cmd(temp_worktree)

    def test_uses_default_timeout(self, temp_worktree):
        """Test uses default timeout when not specified."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "make test"
""")

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd == "make test"
        assert timeout == 1800  # Publish default
        assert dirty_check == "tracked"

    def test_reads_dirty_check_mode(self, temp_worktree):
        """Test reads dirty_check from config."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "make test"
    dirty_check: "unstaged"
""")

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd == "make test"
        assert timeout == 1800
        assert dirty_check == "unstaged"


class TestRunPrepushCheck:
    """Tests for run_prepush_check function."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree with git repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            # Initialize git repo
            subprocess.run(
                ["git", "init"],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=worktree,
                capture_output=True,
            )
            (worktree / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "."],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Initial"],
                cwd=worktree,
                capture_output=True,
            )
            yield worktree

    def test_returns_0_when_no_config(self, temp_worktree):
        """Test returns 0 (pass) when no config exists."""
        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_records_error_summary_when_config_load_raises(
        self, temp_worktree, monkeypatch
    ):
        """Unexpected pre-push exceptions should still leave a summary record."""

        def fail_load_validation_cmd(worktree: Path):
            raise RuntimeError(f"cannot load validation config from {worktree}")

        monkeypatch.setattr(
            "issue_orchestrator.entrypoints.cli_tools.prepush_check."
            "load_validation_cmd",
            fail_load_validation_cmd,
        )

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            with pytest.raises(RuntimeError, match="cannot load validation config"):
                run_prepush_check(verbose=False)
        finally:
            os.chdir(orig_cwd)

        summary = next(
            record
            for record in _shared_timing_records(temp_worktree)
            if record["kind"] == "prepush_gate_summary"
        )
        assert summary["phase"] == "error"
        assert summary["error_type"] == "RuntimeError"
        assert summary["final_exit_code"] is None
        assert summary["head_sha"] is None
        assert summary["validation_allowed"] is None

    def test_returns_0_when_validation_passes(self, temp_worktree):
        """Test returns 0 when validation passes."""

        # Create config with passing command
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    timeout_seconds: 10
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_uses_selected_config_name_env_for_run(self, temp_worktree, monkeypatch):
        """Test full prepush run honors the selected config env."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        marker_file = temp_worktree / "selected-validation-ran"
        (config_dir / "main.yaml").write_text(f"""
validation:
  publish:
    cmd: "touch {marker_file}"
    timeout_seconds: 10
""")
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_NAME", "main.yaml")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
            assert marker_file.exists()
        finally:
            os.chdir(orig_cwd)

    def test_returns_1_when_validation_fails(self, temp_worktree):
        """Test returns 1 when validation fails."""

        # Create config with failing command
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "exit 1"
    timeout_seconds: 10
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

    def test_failed_validation_persists_hook_diagnostics(self, temp_worktree):
        """Test pre-push failures keep stdout/stderr artifacts after the hook exits."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo boom >&2 && exit 1"
    timeout_seconds: 10
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        record_path = (
            temp_worktree / ".issue-orchestrator" / "validation" / f"{head_sha}.json"
        )
        record = json.loads(record_path.read_text())
        stdout_path = temp_worktree / str(record["stdout_path"])
        stderr_path = temp_worktree / str(record["stderr_path"])
        assert stdout_path.exists()
        assert stderr_path.exists()
        assert "boom" in stderr_path.read_text()

    def test_prepush_output_dir_prunes_old_sha_directories(self, temp_worktree):
        diagnostics_root = (
            temp_worktree / ".issue-orchestrator" / "diagnostics" / "prepush"
        )
        diagnostics_root.mkdir(parents=True)
        current_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        current_dir = diagnostics_root / current_sha
        current_dir.mkdir()
        os.utime(current_dir, (50, 50))
        old_dirs = []
        for index in range(7):
            child = diagnostics_root / f"old-{index}"
            child.mkdir()
            (child / "marker").write_text(str(index))
            old_dirs.append(child)
            os.utime(child, (100 + index, 100 + index))

        current = _prepush_output_dir(temp_worktree)

        remaining = sorted(child.name for child in diagnostics_root.iterdir())
        assert current.name in remaining
        assert len(remaining) == 5
        assert "old-0" not in remaining
        assert "old-1" not in remaining
        assert "old-2" not in remaining

    def test_uses_cache_on_second_run(self, temp_worktree):
        """Test uses cache on second run."""

        # Create config with command that creates a file (to track runs)
        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        marker_file = temp_worktree / "validation_ran"
        config_path = config_dir / "default.yaml"
        config_path.write_text(f"""
validation:
  publish:
    cmd: "touch {marker_file} && echo 'ok'"
    timeout_seconds: 10
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)

            # First run creates the marker
            result1 = run_prepush_check(verbose=False)
            assert result1 == 0
            assert marker_file.exists()

            # Delete marker to verify second run uses cache
            marker_file.unlink()

            # Second run should use cache (not recreate marker)
            result2 = run_prepush_check(verbose=False)
            assert result2 == 0
            assert not marker_file.exists()  # Validation didn't run again
        finally:
            os.chdir(orig_cwd)

        summaries = [
            record
            for record in _shared_timing_records(temp_worktree)
            if record["kind"] == "prepush_gate_summary"
        ]
        assert summaries[-1]["final_exit_code"] == 0
        assert summaries[-1]["phase"] == "validation_gate"
        assert summaries[-1]["dirty_check"] == "tracked"
        dirty_elapsed = summaries[-1]["dirty_elapsed_seconds"]
        monotonic_elapsed = summaries[-1]["monotonic_elapsed_seconds"]
        wall_elapsed = summaries[-1]["wall_elapsed_seconds"]
        assert isinstance(dirty_elapsed, int | float)
        assert isinstance(monotonic_elapsed, int | float)
        assert isinstance(wall_elapsed, int | float)
        assert dirty_elapsed >= 0
        assert summaries[-1]["validation_cache_hit"] is True
        assert summaries[-1]["validation_allowed"] is True
        assert summaries[-1]["validation_record_exit_code"] == 0
        assert monotonic_elapsed >= 0
        assert wall_elapsed >= 0

    def test_blocks_when_tracked_dirty(self, temp_worktree):
        """Test blocks push when tracked files are dirty."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "tracked"
""")

        # Modify tracked file without committing
        (temp_worktree / "README.md").write_text("dirty")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

    def test_allows_when_dirty_check_off(self, temp_worktree):
        """Test allows push when dirty check is disabled."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "off"
""")

        (temp_worktree / "README.md").write_text("dirty")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_allows_staged_when_unstaged_mode(self, temp_worktree):
        """Test unstaged mode allows staged changes."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "unstaged"
""")

        (temp_worktree / "README.md").write_text("dirty")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=temp_worktree,
            check=True,
            capture_output=True,
        )

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_blocks_when_all_mode_with_untracked_files(self, temp_worktree):
        """Mode 'all' blocks when untracked files are present."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "all"
""")

        (temp_worktree / "new_untracked.txt").write_text("new file")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

    def test_rejects_invalid_dirty_mode(self, temp_worktree, capsys):
        """Test invalid dirty mode exits 1 and reports error when verbose."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "bogus"
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=True)
            captured = capsys.readouterr()
            assert result == 1
            assert "Invalid validation.publish.dirty_check value" in captured.out
        finally:
            os.chdir(orig_cwd)

    def test_dirty_only_skips_validation_command(self, temp_worktree):
        """Dirty-only mode should not execute validation command."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        marker_file = temp_worktree / "validation_ran"
        config_path = config_dir / "default.yaml"
        config_path.write_text(f"""
validation:
  publish:
    cmd: "touch {marker_file} && exit 1"
    dirty_check: "tracked"
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False, dirty_only=True)
            assert result == 0
            assert not marker_file.exists()
        finally:
            os.chdir(orig_cwd)

    def test_dirty_only_still_blocks_when_dirty(self, temp_worktree):
        """Dirty-only mode must still enforce dirty-tree policy."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "tracked"
""")

        (temp_worktree / "README.md").write_text("dirty")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False, dirty_only=True)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

    def test_allows_runtime_session_latest_dirty_file(self, temp_worktree):
        """Runtime session-latest metadata should not block dirty-tree guard."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "tracked"
""")

        runtime_file = temp_worktree / ".issue-orchestrator" / "session-latest.json"
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.write_text("{}\n")
        subprocess.run(
            ["git", "add", str(runtime_file)],
            cwd=temp_worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Track runtime session metadata for test"],
            cwd=temp_worktree,
            check=True,
            capture_output=True,
        )
        runtime_file.write_text('{"latest":"abc"}\n')

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False, dirty_only=True)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_still_blocks_when_runtime_and_real_dirty_files_present(
        self, temp_worktree
    ):
        """Guard should still fail if non-excluded files are dirty."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "tracked"
""")

        runtime_file = temp_worktree / ".issue-orchestrator" / "session-latest.json"
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.write_text("{}\n")
        subprocess.run(
            ["git", "add", str(runtime_file)],
            cwd=temp_worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Track runtime session metadata for test"],
            cwd=temp_worktree,
            check=True,
            capture_output=True,
        )

        runtime_file.write_text('{"latest":"abc"}\n')
        (temp_worktree / "README.md").write_text("dirty")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False, dirty_only=True)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

    def test_allows_claude_settings_dirty_file(self, temp_worktree):
        """Claude CLI .claude/settings.json should not block dirty-tree guard."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "tracked"
""")

        claude_file = temp_worktree / ".claude" / "settings.json"
        claude_file.parent.mkdir(parents=True, exist_ok=True)
        claude_file.write_text("{}\n")
        subprocess.run(
            ["git", "add", str(claude_file)],
            cwd=temp_worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Track claude settings for test"],
            cwd=temp_worktree,
            check=True,
            capture_output=True,
        )
        claude_file.write_text('{"key":"value"}\n')

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False, dirty_only=True)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_blocks_when_list_dirty_files_returns_none(
        self, temp_worktree, monkeypatch, capsys
    ):
        """Enumeration failure (list_dirty_files -> None) must fail closed
        and skip the validation command. Regression for the silent-pass
        path where None collapsed to [] (PR #6159 reviewer feedback)."""
        from issue_orchestrator.execution import GitWorkingCopy

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        marker_file = temp_worktree / "validation_ran"
        config_path = config_dir / "default.yaml"
        config_path.write_text(f"""
validation:
  publish:
    cmd: "touch {marker_file}"
    dirty_check: "tracked"
""")

        monkeypatch.setattr(
            GitWorkingCopy, "list_dirty_files", lambda self, wt, mode: None
        )

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=True)
            captured = capsys.readouterr()
            assert result == 1
            assert "Could not enumerate dirty files" in captured.out
            assert not marker_file.exists(), (
                "validation command must not run when dirty enumeration fails"
            )
        finally:
            os.chdir(orig_cwd)

    def test_verbose_output_lists_dirty_files(self, temp_worktree, capsys):
        """Verbose dirty guard output should include dirty file paths."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "tracked"
""")

        (temp_worktree / "README.md").write_text("dirty")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=True)
            captured = capsys.readouterr()
            assert result == 1
            assert "Dirty files (showing up to 20):" in captured.out
            assert "README.md" in captured.out
        finally:
            os.chdir(orig_cwd)

    def test_verbose_output_clips_dirty_file_list(self, temp_worktree, capsys):
        """Dirty file listing should be clipped with ellipsis for long lists."""

        config_dir = temp_worktree / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  publish:
    cmd: "echo 'ok'"
    dirty_check: "all"
""")

        for i in range(25):
            (temp_worktree / f"new_{i:02}.txt").write_text("x")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=True)
            captured = capsys.readouterr()
            assert result == 1
            assert "Dirty files (showing up to 20):" in captured.out
            assert "new_00.txt" in captured.out
            assert "... and " in captured.out
            assert " more" in captured.out
        finally:
            os.chdir(orig_cwd)
