"""Tests for validate_runner CLI.

The validate_runner captures validation output to a known location
so agents can find failure details without re-running tests.
"""

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.infra.validation_timings import (
    ValidateTimingRecorder,
    ValidationConfiguration,
    ValidationConfigurationEntry,
)
from issue_orchestrator.entrypoints.cli_tools.validate_runner import (
    ValidationRunnerClock,
    run_validation,
)
from issue_orchestrator.entrypoints.bootstrap import build_process_group_terminator


def _with_repo_on_pythonpath(env: dict[str, str]) -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    pythonpath = env.get("PYTHONPATH")
    env = dict(env)
    env["PYTHONPATH"] = str(repo_root / "src") + (os.pathsep + pythonpath if pythonpath else "")
    return env


class TestValidateRunner:
    """Test the validate_runner CLI."""

    @pytest.fixture
    def fake_git_repo(self, tmp_path: Path) -> Path:
        """Create a fake git repo structure for testing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        return repo

    def test_captures_output_to_env_var_dir(self, fake_git_repo: Path, tmp_path: Path):
        """Test that output is captured to ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo 'test output'"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            }),
        )

        assert result.returncode == 0
        output_file = output_dir / "validation-output.log"
        assert output_file.exists()
        assert "test output" in output_file.read_text()

    def test_falls_back_to_diagnostics_dir(self, fake_git_repo: Path):
        """Test that output falls back to .issue-orchestrator/diagnostics/."""
        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo 'fallback test'"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                k: v for k, v in os.environ.items() if not k.startswith("ISSUE_ORCHESTRATOR")
            }),
        )

        assert result.returncode == 0
        output_file = fake_git_repo / ".issue-orchestrator" / "diagnostics" / "validation-output.log"
        assert output_file.exists()
        assert "fallback test" in output_file.read_text()

    def test_prints_path_on_failure(self, fake_git_repo: Path, tmp_path: Path):
        """Test that failure message includes path to output file."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "exit 1"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            }),
        )

        assert result.returncode == 1
        assert "Full output saved to:" in result.stdout
        assert "validation-output.log" in result.stdout

    def test_returns_command_exit_code(self, fake_git_repo: Path, tmp_path: Path):
        """Test that exit code matches the underlying command."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        # Test exit code 0
        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "exit 0"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            }),
        )
        assert result.returncode == 0

        # Test exit code 42
        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "exit 42"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            }),
        )
        assert result.returncode == 42

    def test_captures_stderr(self, fake_git_repo: Path, tmp_path: Path):
        """Test that stderr is captured in the output file."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo 'stderr message' >&2"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            }),
        )

        output_file = output_dir / "validation-output.log"
        assert output_file.exists()
        assert "stderr message" in output_file.read_text()

    def test_fails_if_no_command_configured(self, fake_git_repo: Path, tmp_path: Path):
        """Test that it fails with clear error if no command is provided."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            }),
        )

        assert result.returncode == 2
        assert "No validation command configured" in result.stderr

    def test_streams_output_to_terminal(self, fake_git_repo: Path, tmp_path: Path):
        """Test that output is streamed to terminal while also being captured."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo 'visible output'"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            }),
        )

        # Output should appear in terminal (stdout)
        assert "visible output" in result.stdout

        # And also be captured to file
        output_file = output_dir / "validation-output.log"
        assert "visible output" in output_file.read_text()

    def test_orchestrated_runs_emit_concise_lifecycle_markers(self, fake_git_repo: Path, tmp_path: Path):
        """Orchestrated validation should summarize stdout but keep full file output."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "printf 'line one\\nline two\\n'"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            }),
        )

        assert result.returncode == 0
        stdout_lines = result.stdout.splitlines()
        assert "line one" not in stdout_lines
        assert "line two" not in stdout_lines
        assert "[orchestrated] full output -> file; terminal shows lifecycle markers only" in result.stdout
        assert "[validate_runner] child_started pid=" in result.stdout
        assert "[validate_runner] stdout_eof pid=" in result.stdout
        assert "[validate_runner] child_exited pid=" in result.stdout
        assert "Full output saved to:" in result.stdout

        output_file = output_dir / "validation-output.log"
        content = output_file.read_text()
        assert "line one" in content
        assert "line two" in content
        assert "[validate_runner] child_started pid=" in content
        assert "[validate_runner] child_exited pid=" in content

    def test_appends_target_timing_records_to_shared_git_dir(self, fake_git_repo: Path):
        """Timing markers should be persisted as JSONL under the shared git dir."""
        command = (
            "printf '[validate-timing] CONFIG validate_jobs=10 unit_parallel=auto "
            "simulated_parallel=auto integration_parallel=auto static_jobs=10 "
            "test_jobs=1 web_jobs=1 live_web_jobs=2 agent_jobs=1 e2e_jobs=1\\n'"
            " && printf '[validate-timing] START target=test-unit at=2026-03-14T09:10:13-0600\\n'"
            " && printf '[validate-timing] END target=test-unit status=0 elapsed=12s "
            "at=2026-03-14T09:10:25-0600\\n'"
        )

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", command,
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                k: v for k, v in os.environ.items() if not k.startswith("ISSUE_ORCHESTRATOR")
            }),
        )

        assert result.returncode == 0
        timings_file = fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        assert timings_file.exists()

        records = [json.loads(line) for line in timings_file.read_text().splitlines()]
        target_record = next(record for record in records if record["kind"] == "target_timing")
        assert target_record["target"] == "test-unit"
        assert target_record["elapsed_seconds"] == 12
        assert target_record["validate_jobs"] == "10"
        assert target_record["unit_parallel"] == "auto"
        assert target_record["simulated_parallel"] == "auto"
        assert target_record["integration_parallel"] == "auto"
        assert target_record["static_jobs"] == "10"
        assert target_record["test_jobs"] == "1"
        assert target_record["web_jobs"] == "1"
        assert target_record["live_web_jobs"] == "2"
        assert target_record["agent_jobs"] == "1"
        assert target_record["e2e_jobs"] == "1"
        assert target_record["host_name"]
        assert target_record["host_system"]
        assert target_record["host_machine"]
        assert isinstance(target_record["host_cpu_count"], int)
        assert target_record["host_memory_bytes"] is None or isinstance(
            target_record["host_memory_bytes"], int
        )
        assert target_record["started_at"] == "2026-03-14T09:10:13-0600"
        assert target_record["ended_at"] == "2026-03-14T09:10:25-0600"

    def test_rejects_malformed_known_timing_marker(self, fake_git_repo: Path):
        """A known marker prefix must never silently lose calibration evidence."""
        recorder = ValidateTimingRecorder(worktree=fake_git_repo, command="make validate")

        recorder.process_line("[validate-timing] CONFIG validate_jobs=10 unit_parallel=auto\n")
        with pytest.raises(ValueError, match="malformed validation timing marker"):
            recorder.process_line("[validate-timing] CONFIG \n")

    @pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process cleanup")
    def test_duplicate_marker_cleans_up_child_sampler_and_finalizes_timing(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        child_pid_path = tmp_path / "validation-child.pid"
        resistant_child = (
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        )
        cooperative_leader = (
            "import signal, subprocess, sys; "
            f"descendant = subprocess.Popen([sys.executable, '-c', {resistant_child!r}], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); "
            f"open({str(child_pid_path)!r}, 'w').write(str(descendant.pid)); "
            "signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0)); "
            "print('[validate-timing] START target=duplicate at=one', flush=True); "
            "print('[validate-timing] START target=duplicate at=two', flush=True); "
            "signal.pause()"
        )
        command = (
            f"exec {shlex.quote(sys.executable)} -c "
            f"{shlex.quote(cooperative_leader)}"
        )
        sampler_threads_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "validate-resource-sampler"
        }

        with pytest.raises(ValueError, match="duplicate START marker"):
            run_validation(
                command,
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                process_group_terminator=build_process_group_terminator(),
            )

        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        sampler_threads_after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "validate-resource-sampler"
        }
        assert sampler_threads_after == sampler_threads_before
        records = [
            json.loads(line)
            for line in (
                fake_git_repo
                / ".git"
                / "issue-orchestrator"
                / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["exit_code"] == 0

    def test_appends_run_summary_record_to_shared_git_dir(self, fake_git_repo: Path):
        """Each validate run should append a run summary record."""
        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo ok",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                k: v for k, v in os.environ.items() if not k.startswith("ISSUE_ORCHESTRATOR")
            }),
        )

        assert result.returncode == 0
        timings_file = fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        assert timings_file.exists()

        records = [json.loads(line) for line in timings_file.read_text().splitlines()]
        summary_record = next(record for record in records if record["kind"] == "run_summary")
        assert summary_record["command"] == "echo ok"
        assert summary_record["worktree"] == str(fake_git_repo)
        assert summary_record["exit_code"] == 0
        assert isinstance(summary_record["total_elapsed_seconds"], float)
        assert isinstance(summary_record["monotonic_elapsed_seconds"], float)
        assert isinstance(summary_record["wall_elapsed_seconds"], float)
        assert summary_record["total_elapsed_seconds"] == pytest.approx(
            summary_record["monotonic_elapsed_seconds"], abs=0.01
        )
        assert isinstance(summary_record["wall_started_at"], str)
        assert isinstance(summary_record["wall_ended_at"], str)
        assert summary_record["host_name"]
        assert summary_record["host_memory_bytes"] is None or isinstance(
            summary_record["host_memory_bytes"], int
        )

    def test_appends_resource_samples_to_shared_git_dir(self, fake_git_repo: Path):
        """Validate runs should persist periodic resource samples."""
        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", f"\"{sys.executable}\" -c \"import time; time.sleep(0.5)\"",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath({
                k: v for k, v in os.environ.items() if not k.startswith("ISSUE_ORCHESTRATOR")
            }),
        )

        assert result.returncode == 0
        timings_file = fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        records = [json.loads(line) for line in timings_file.read_text().splitlines()]
        resource_records = [record for record in records if record["kind"] == "resource_sample"]
        assert resource_records, "expected at least one resource_sample record"
        sample = resource_records[0]
        assert sample["worktree"] == str(fake_git_repo)
        assert "recorded_at" in sample
        assert sample["host_name"]
        assert isinstance(sample["host_cpu_count"], int)

    def test_wall_clock_rollback_cannot_corrupt_elapsed_duration(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        wall_start = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
        wall_rollback = datetime(2026, 8, 24, 11, tzinfo=timezone.utc)
        wall_calls = 0
        monotonic_calls = 0

        def wall_now() -> datetime:
            nonlocal wall_calls
            wall_calls += 1
            return wall_start if wall_calls == 1 else wall_rollback

        def monotonic_now() -> float:
            nonlocal monotonic_calls
            monotonic_calls += 1
            return 100.0 if monotonic_calls == 1 else 105.0

        result = run_validation(
            "true",
            tmp_path / "output",
            fake_git_repo,
            clock=ValidationRunnerClock(wall_now, monotonic_now),
            process_group_terminator=build_process_group_terminator(),
        )

        assert result == 0
        records = [
            json.loads(line)
            for line in (
                fake_git_repo
                / ".git"
                / "issue-orchestrator"
                / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["total_elapsed_seconds"] == 5.0
        assert summary["monotonic_elapsed_seconds"] == 5.0
        assert summary["wall_elapsed_seconds"] == -3600.0

    def test_default_run_ids_are_unique_within_the_same_second(
        self,
        fake_git_repo: Path,
    ) -> None:
        first = ValidateTimingRecorder(fake_git_repo, "first")
        second = ValidateTimingRecorder(fake_git_repo, "second")

        assert first.run_id != second.run_id
        assert "-pid" in first.run_id


class TestReadHeadSha:
    """R9 (#6824): the E2E head reader resolves HEAD via file reads, no subprocess."""

    def test_reads_sha_from_loose_ref(self, tmp_path: Path) -> None:
        from issue_orchestrator.infra.validation_timings import read_head_sha

        git = tmp_path / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n")

        assert read_head_sha(tmp_path) == "a" * 40

    def test_reads_detached_head_sha_directly(self, tmp_path: Path) -> None:
        from issue_orchestrator.infra.validation_timings import read_head_sha

        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("b" * 40 + "\n")

        assert read_head_sha(tmp_path) == "b" * 40

    def test_reads_sha_from_packed_refs(self, tmp_path: Path) -> None:
        from issue_orchestrator.infra.validation_timings import read_head_sha

        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            + "c" * 40 + " refs/heads/main\n"
        )

        assert read_head_sha(tmp_path) == "c" * 40

    def test_no_subprocess_import_in_e2e_slot_policy(self) -> None:
        # The infra boundary: e2e_slot_policy must not shell out (infra/AGENTS.md).
        import issue_orchestrator.infra.e2e_slot_policy as mod

        assert not hasattr(mod, "subprocess")
