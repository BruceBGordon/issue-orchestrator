"""Unit tests for the validation module."""

import json
import pytest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

from issue_orchestrator.execution import GitWorkingCopy, LocalCommandRunner
from issue_orchestrator.ports.command_runner import CommandResult

from issue_orchestrator.control.validation import (
    ValidationRecord,
    ValidationRecordStore,
    ValidationRunner,
    ValidationCache,
    PublishGate,
    PublishGateResult,
    AgentGate,
    AgentGateResult,
    VALIDATION_SCHEMA_VERSION,
)
from issue_orchestrator.control.isolation import GRADLE_USER_HOME_ENV
from issue_orchestrator.infra.validation_timings import ValidationTimingClock


def _shared_timing_records(worktree: Path) -> list[dict[str, object]]:
    timings_file = worktree / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
    if not timings_file.exists():
        return []
    return [json.loads(line) for line in timings_file.read_text().splitlines()]


def _timing_marker_command() -> str:
    return (
        "printf '%s\\n' "
        "'[validate-timing] CONFIG validate_jobs=10 unit_parallel=auto "
        "simulated_parallel=auto integration_parallel=auto "
        "integration_agent_parallel=0 static_jobs=10 test_jobs=1 "
        "web_jobs=1 live_web_jobs=2 agent_jobs=1 e2e_jobs=1' "
        "'[validate-timing] START target=test-unit at=2026-03-14T09:10:13-0600' "
        "'[validate-timing] END target=test-unit status=0 elapsed=12s "
        "at=2026-03-14T09:10:25-0600'"
    )


class TestValidationRecord:
    """Tests for ValidationRecord dataclass."""

    def test_create_record(self):
        """Test creating a validation record."""
        record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )

        assert record.suite == "publish_gate"
        assert record.head_sha == "abc123"
        assert record.passed is True
        assert record.exit_code == 0

    def test_to_dict(self):
        """Test converting record to dictionary."""
        record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )

        data = record.to_dict()
        assert data["suite"] == "publish_gate"
        assert data["head_sha"] == "abc123"
        assert data["passed"] is True

    def test_from_dict(self):
        """Test creating record from dictionary."""
        data = {
            "schema_version": 1,
            "suite": "agent_gate",
            "head_sha": "def456",
            "passed": False,
            "exit_code": 1,
            "command": "npm test",
            "started_at": "2024-01-01T12:00:00",
            "ended_at": "2024-01-01T12:01:00",
            "timed_out": False,
            "stdout_path": None,
            "stderr_path": None,
        }

        record = ValidationRecord.from_dict(data)
        assert record.suite == "agent_gate"
        assert record.head_sha == "def456"
        assert record.passed is False

    def test_record_is_immutable(self):
        """Test that record is frozen/immutable."""
        record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )

        with pytest.raises(AttributeError):
            record.passed = False  # type: ignore


class TestValidationRecordStore:
    """Tests for ValidationRecordStore."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def store(self, temp_worktree):
        """Create a store for the temp worktree."""
        return ValidationRecordStore(temp_worktree)

    def test_write_and_read_record(self, store):
        """Test writing and reading a record."""
        record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123def456",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )

        # Write
        path = store.write(record)
        assert path.exists()
        # New path format: .issue-orchestrator/validation/{sha}.json (no suite subdir)
        assert "abc123def456.json" in str(path)

        # Read - new API uses just sha (not suite + sha)
        read_record = store.read("abc123def456")
        assert read_record is not None
        assert read_record.suite == record.suite
        assert read_record.head_sha == record.head_sha
        assert read_record.passed == record.passed

    def test_read_nonexistent_returns_none(self, store):
        """Test reading a non-existent record returns None."""
        record = store.read("nonexistent")
        assert record is None

    def test_read_corrupted_json_returns_none(self, store, temp_worktree):
        """Test reading corrupted JSON returns None."""
        # Create a corrupted file in the new path format (no suite subdir)
        path = temp_worktree / ".issue-orchestrator" / "validation"
        path.mkdir(parents=True)
        (path / "corrupted.json").write_text("not valid json {{{")

        record = store.read("corrupted")
        assert record is None


class TestValidationRunner:
    """Tests for ValidationRunner."""

    class _TimeoutRunner:
        def run(self, *args, **kwargs):
            return CommandResult(returncode=-1, stdout="", stderr="", timed_out=True)

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def session_output_dir(self, temp_worktree):
        """Create a session output directory."""
        output_dir = temp_worktree / ".issue-orchestrator" / "sessions" / "test-session"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    @pytest.fixture
    def store(self, temp_worktree):
        """Create a store for the temp worktree."""
        return ValidationRecordStore(temp_worktree)

    @pytest.fixture
    def runner(self, store):
        """Create a runner with the store."""
        return ValidationRunner(store, LocalCommandRunner())

    def test_run_passing_command(self, runner, session_output_dir):
        """Test running a passing command."""
        record = runner.run(
            suite="publish_gate",
            head_sha="abc123",
            command="echo 'hello'",
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        assert record.passed is True
        assert record.exit_code == 0
        assert record.timed_out is False
        assert record.suite == "publish_gate"
        assert record.head_sha == "abc123"

    def test_run_records_offset_aware_utc_timestamps(self, runner, session_output_dir):
        record = runner.run(
            suite="publish_gate",
            head_sha="abc123",
            command="echo 'hello'",
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        started_at = datetime.fromisoformat(record.started_at)
        ended_at = datetime.fromisoformat(record.ended_at)
        assert started_at.tzinfo is not None
        assert ended_at.tzinfo is not None
        assert started_at.utcoffset() == timezone.utc.utcoffset(started_at)
        assert ended_at.utcoffset() == timezone.utc.utcoffset(ended_at)

    def test_run_failing_command(self, runner, session_output_dir):
        """Test running a failing command."""
        record = runner.run(
            suite="publish_gate",
            head_sha="abc123",
            command="exit 1",
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        assert record.passed is False
        assert record.exit_code == 1
        assert record.timed_out is False

    def test_run_timeout(self, runner, session_output_dir):
        """Test command timeout."""
        fast_runner = ValidationRunner(runner.store, self._TimeoutRunner())
        record = fast_runner.run(
            suite="publish_gate",
            head_sha="abc123",
            command="sleep 10",
            timeout_seconds=1,
            session_output_dir=session_output_dir,
        )

        assert record.passed is False
        assert record.timed_out is True
        assert record.exit_code == -1

    def test_run_handles_command_runner_exception(self, store, session_output_dir):
        """Test that runner records failures when command runner raises."""

        class FailingRunner:
            def run(self, *args, **kwargs):
                raise RuntimeError("boom")

        runner = ValidationRunner(store, FailingRunner())

        record = runner.run(
            suite="publish_gate",
            head_sha="abc123",
            command="echo 'test'",
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        assert record.passed is False
        assert record.exit_code == -1
        stderr_path = store.worktree / record.stderr_path
        assert "Validation runner error: boom" in stderr_path.read_text()

    def test_run_writes_record_to_store(self, runner, store, session_output_dir):
        """Test that running writes the record to the store."""
        record = runner.run(
            suite="agent_gate",
            head_sha="def456",
            command="echo 'test'",
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        # Read back from store (new API uses just sha)
        stored = store.read("def456")
        assert stored is not None
        assert stored.head_sha == record.head_sha
        assert stored.passed == record.passed

    def test_run_writes_record_to_session_run_dir(self, runner, session_output_dir):
        """Session run dirs should get a local validation-record.json copy."""
        record = runner.run(
            suite="agent_gate",
            head_sha="session123",
            command="echo 'ok'",
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        run_record_path = session_output_dir / "validation-record.json"
        assert run_record_path.exists()
        payload = json.loads(run_record_path.read_text())
        assert payload["head_sha"] == record.head_sha
        assert payload["passed"] == record.passed

    def test_run_appends_target_timing_records_from_captured_output(
        self,
        runner,
        temp_worktree,
        session_output_dir,
    ):
        """Raw publish commands should still emit structured per-target timings."""
        (temp_worktree / ".git").mkdir()
        command = _timing_marker_command()

        runner.run(
            suite="publish_gate",
            head_sha="timing123",
            command=command,
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        records = _shared_timing_records(temp_worktree)
        target_record = next(
            record for record in records if record["kind"] == "target_timing"
        )
        assert target_record["command"] == command
        assert target_record["target"] == "test-unit"
        assert target_record["elapsed_seconds"] == 12
        assert target_record["validate_jobs"] == "10"
        assert target_record["integration_agent_parallel"] == "0"
        assert target_record["started_at"] == "2026-03-14T09:10:13-0600"
        assert target_record["ended_at"] == "2026-03-14T09:10:25-0600"

    def test_run_does_not_append_target_timing_records_for_agent_gate(
        self,
        runner,
        temp_worktree,
        session_output_dir,
    ):
        """Captured timing markers only produce target timings for publish gate."""
        (temp_worktree / ".git").mkdir()

        runner.run(
            suite="agent_gate",
            head_sha="agenttiming123",
            command=_timing_marker_command(),
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        records = _shared_timing_records(temp_worktree)
        assert not [record for record in records if record["kind"] == "target_timing"]

    def test_run_does_not_write_record_to_non_session_dir(self, runner, temp_worktree):
        """Non-session output dirs should not get run-scoped validation-record.json."""
        output_dir = temp_worktree / ".issue-orchestrator" / "tmp-validation-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        runner.run(
            suite="agent_gate",
            head_sha="nonsession123",
            command="echo 'ok'",
            timeout_seconds=10,
            session_output_dir=output_dir,
        )

        assert not (output_dir / "validation-record.json").exists()

    def test_run_uses_per_worktree_gradle_user_home(
        self, store, temp_worktree, session_output_dir
    ):
        """Validation should not share Gradle daemons with other worktrees."""

        class RecordingRunner:
            def __init__(self):
                self.kwargs = {}

            def run(self, *args, **kwargs):
                self.kwargs = kwargs
                return CommandResult(
                    returncode=0, stdout="", stderr="", timed_out=False
                )

        command_runner = RecordingRunner()
        runner = ValidationRunner(store, command_runner)

        runner.run(
            suite="agent_gate",
            head_sha="gradlehome123",
            command="./gradlew test",
            timeout_seconds=10,
            session_output_dir=session_output_dir,
        )

        env = command_runner.kwargs["env"]
        assert env[GRADLE_USER_HOME_ENV] == str(
            temp_worktree / ".issue-orchestrator" / "tool-homes" / "gradle"
        )


class TestValidationCache:
    """Tests for ValidationCache."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def store(self, temp_worktree):
        """Create a store for the temp worktree."""
        return ValidationRecordStore(temp_worktree)

    @pytest.fixture
    def cache(self, store):
        """Create a cache with the store."""
        return ValidationCache(store)

    def test_cache_miss_when_not_exists(self, cache):
        """Test cache miss when record doesn't exist."""
        # New API: lookup(sha, command=None)
        result = cache.lookup("nonexistent")
        assert result is None

    def test_cache_hit_when_exists(self, cache, store):
        """Test cache hit when record exists."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # New API: lookup(sha)
        result = cache.lookup("abc123")
        assert result is not None
        assert result.passed is True

    def test_cache_miss_when_sha_differs(self, cache, store):
        """Test cache miss when SHA differs."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # Different SHA should miss
        result = cache.lookup("different_sha")
        assert result is None

    def test_cache_miss_when_command_differs(self, cache, store):
        """Test cache miss when command filter doesn't match."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # Same SHA but different command filter should miss
        result = cache.lookup("abc123", command="make build")
        assert result is None

        # Same command should hit
        result = cache.lookup("abc123", command="make test")
        assert result is not None

    def test_cache_miss_when_schema_version_differs(self, cache, store, temp_worktree):
        """Test cache miss when schema version differs."""
        # Write a record with old schema version (in new path format)
        path = temp_worktree / ".issue-orchestrator" / "validation"
        path.mkdir(parents=True)
        old_record = {
            "schema_version": 0,  # Old version
            "suite": "publish_gate",
            "head_sha": "abc123",
            "passed": True,
            "exit_code": 0,
            "command": "make test",
            "started_at": "2024-01-01T12:00:00",
            "ended_at": "2024-01-01T12:01:00",
            "timed_out": False,
            "stdout_path": None,
            "stderr_path": None,
        }
        (path / "abc123.json").write_text(json.dumps(old_record))

        # New API uses just sha (optional command for filtering)
        result = cache.lookup("abc123")
        assert result is None

    def test_is_valid_hit_passing(self, cache, store):
        """Test is_valid_hit returns True for passing record."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # New API uses just sha (optional command for filtering)
        assert cache.is_valid_hit("abc123") is True

    def test_is_valid_hit_failing(self, cache, store):
        """Test is_valid_hit returns False for failing record."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=False,
            exit_code=1,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # New API uses just sha
        assert cache.is_valid_hit("abc123") is False

    def test_is_valid_hit_nonexistent(self, cache):
        """Test is_valid_hit returns False when no record."""
        assert cache.is_valid_hit("nonexistent") is False


class TestPublishGate:
    """Tests for PublishGate facade."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            # Initialize a git repo so we can get HEAD SHA
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
            # Create initial commit
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

    @pytest.fixture
    def session_output_dir(self, temp_worktree):
        """Create a session output directory."""
        output_dir = temp_worktree / ".issue-orchestrator" / "sessions" / "test-session"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _attempt_key(self, temp_worktree: Path, stable_id: str):
        from issue_orchestrator.domain.attempt import AttemptKey
        from issue_orchestrator.domain.issue_key import GitHubIssueKey

        head_sha = GitWorkingCopy().get_head_sha(temp_worktree)
        assert head_sha is not None
        return AttemptKey(
            GitHubIssueKey(repo="owner/repo", external_id=stable_id),
            head_sha,
        )

    def _session_dir(self, temp_worktree: Path, run_id: str) -> Path:
        output_dir = temp_worktree / ".issue-orchestrator" / "sessions" / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def test_gate_disabled_when_no_command(self, temp_worktree):
        """Test gate is disabled when no command is configured."""
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command=None,
        )
        result = gate.check()

        assert result.allowed is True
        assert "disabled" in result.reason.lower()
        assert result.record is None
        summary = next(
            record
            for record in _shared_timing_records(temp_worktree)
            if record["kind"] == "validation_gate_summary"
        )
        assert summary["cache_lookup"] == "disabled"
        assert summary["allowed"] is True
        assert summary["record_exit_code"] is None

    def test_gate_summary_survives_wall_clock_rollback(self, temp_worktree):
        wall_values = iter(
            (
                datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
                datetime(2026, 8, 24, 11, tzinfo=timezone.utc),
            )
        )
        monotonic_values = iter((100.0, 105.0))
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command=None,
            timing_clock=ValidationTimingClock(
                wall_now=lambda: next(wall_values),
                monotonic_now=lambda: next(monotonic_values),
            ),
        )

        result = gate.check()

        assert result.allowed is True
        summary = next(
            record
            for record in _shared_timing_records(temp_worktree)
            if record["kind"] == "validation_gate_summary"
        )
        assert summary["monotonic_elapsed_seconds"] == 5.0
        assert summary["wall_elapsed_seconds"] == -3600.0

    def test_gate_appends_summary_when_head_sha_missing(self, temp_worktree):
        """Publish gate summaries should pin HEAD lookup failures."""
        working_copy = MagicMock()
        working_copy.get_head_sha.return_value = None
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=working_copy,
            command="echo 'ok'",
            timeout_seconds=10,
        )

        result = gate.check()

        assert result.allowed is False
        assert result.reason == "Cannot determine HEAD SHA"
        summary = next(
            record
            for record in _shared_timing_records(temp_worktree)
            if record["kind"] == "validation_gate_summary"
        )
        assert summary["cache_lookup"] == "head_sha_missing"
        assert summary["head_sha"] is None
        assert summary["allowed"] is False
        assert summary["record_exit_code"] is None

    def test_gate_passes_when_command_succeeds(self, temp_worktree, session_output_dir):
        """Test gate passes when validation command succeeds."""
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )
        result = gate.check(session_output_dir=session_output_dir)

        assert result.allowed is True
        assert result.record is not None
        assert result.record.passed is True
        assert result.cache_hit is False

    def test_gate_appends_summary_when_validation_runs(
        self, temp_worktree, session_output_dir
    ):
        """Publish gate checks should append an outer summary record."""
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )

        result = gate.check(session_output_dir=session_output_dir)

        assert result.allowed is True
        assert result.record is not None
        summary = next(
            record
            for record in _shared_timing_records(temp_worktree)
            if record["kind"] == "validation_gate_summary"
        )
        assert summary["gate"] == "publish_gate"
        assert summary["command"] == "echo 'ok'"
        assert summary["timeout_seconds"] == 10
        assert summary["cache_lookup"] == "miss"
        assert summary["cache_hit"] is False
        assert summary["allowed"] is True
        assert summary["record_exit_code"] == 0
        assert summary["record_timed_out"] is False
        assert summary["head_sha"] == result.record.head_sha
        monotonic_elapsed = summary["monotonic_elapsed_seconds"]
        wall_elapsed = summary["wall_elapsed_seconds"]
        assert isinstance(monotonic_elapsed, int | float)
        assert isinstance(wall_elapsed, int | float)
        assert monotonic_elapsed >= 0
        assert wall_elapsed >= 0

    def test_gate_fails_when_command_fails(self, temp_worktree, session_output_dir):
        """Test gate fails when validation command fails."""
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="exit 1",
            timeout_seconds=10,
        )
        result = gate.check(session_output_dir=session_output_dir)

        assert result.allowed is False
        assert result.record is not None
        assert result.record.passed is False
        assert "failed" in result.reason.lower()

    def test_gate_uses_cache_on_second_call(self, temp_worktree, session_output_dir):
        """Test gate uses cache on subsequent calls."""
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )

        # First call runs validation
        result1 = gate.check(session_output_dir=session_output_dir)
        assert result1.allowed is True
        assert result1.cache_hit is False

        # Second call should hit cache
        result2 = gate.check(session_output_dir=session_output_dir)
        assert result2.allowed is True
        assert result2.cache_hit is True

    def test_gate_uses_attempt_cache_for_same_issue(
        self, temp_worktree, session_output_dir
    ):
        """Attempt-scoped validation reuses a pass for the same issue and SHA."""
        from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore

        class CountingRunner:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, *args, **kwargs):
                self.calls += 1
                return CommandResult(
                    returncode=0,
                    stdout="ok",
                    stderr="",
                    timed_out=False,
                )

        attempt_store = SidecarAttemptStore(temp_worktree)
        attempt_key = self._attempt_key(temp_worktree, "123")
        runner = CountingRunner()

        first_gate = PublishGate(
            temp_worktree,
            command_runner=runner,
            working_copy=GitWorkingCopy(),
            command="make test",
            timeout_seconds=10,
            attempt_store=attempt_store,
            attempt_key=attempt_key,
        )
        first = first_gate.check(session_output_dir=session_output_dir)

        second_gate = PublishGate(
            temp_worktree,
            command_runner=runner,
            working_copy=GitWorkingCopy(),
            command="make test",
            timeout_seconds=10,
            attempt_store=attempt_store,
            attempt_key=attempt_key,
        )
        second = second_gate.check(
            session_output_dir=self._session_dir(temp_worktree, "same-issue")
        )

        assert first.cache_hit is False
        assert second.cache_hit is True
        assert runner.calls == 1

    def test_gate_does_not_fall_back_to_sha_cache_for_different_attempt(
        self, temp_worktree, session_output_dir
    ):
        """Different issues at the same SHA must not share validation cache."""
        from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore

        class CountingRunner:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, *args, **kwargs):
                self.calls += 1
                return CommandResult(
                    returncode=0,
                    stdout="ok",
                    stderr="",
                    timed_out=False,
                )

        attempt_store = SidecarAttemptStore(temp_worktree)
        runner = CountingRunner()

        first_gate = PublishGate(
            temp_worktree,
            command_runner=runner,
            working_copy=GitWorkingCopy(),
            command="make test",
            timeout_seconds=10,
            attempt_store=attempt_store,
            attempt_key=self._attempt_key(temp_worktree, "123"),
        )
        first = first_gate.check(session_output_dir=session_output_dir)

        second_gate = PublishGate(
            temp_worktree,
            command_runner=runner,
            working_copy=GitWorkingCopy(),
            command="make test",
            timeout_seconds=10,
            attempt_store=attempt_store,
            attempt_key=self._attempt_key(temp_worktree, "124"),
        )
        second = second_gate.check(
            session_output_dir=self._session_dir(temp_worktree, "different-issue")
        )

        assert first.cache_hit is False
        assert second.cache_hit is False
        assert runner.calls == 2

    def test_gate_appends_summary_on_cache_hit(self, temp_worktree, session_output_dir):
        """Publish gate summaries should distinguish cache hits from validation runs."""
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )

        gate.check(session_output_dir=session_output_dir)
        result = gate.check(session_output_dir=session_output_dir)

        assert result.cache_hit is True
        summaries = [
            record
            for record in _shared_timing_records(temp_worktree)
            if record["kind"] == "validation_gate_summary"
        ]
        assert summaries[-1]["cache_lookup"] == "hit_passed"
        assert summaries[-1]["cache_hit"] is True
        assert summaries[-1]["allowed"] is True
        assert summaries[-1]["record_exit_code"] == 0

    def test_gate_fails_when_timeout(self, temp_worktree, session_output_dir):
        """Test gate fails when command times out."""

        class TimeoutRunner:
            def run(self, *args, **kwargs):
                return CommandResult(
                    returncode=-1, stdout="", stderr="", timed_out=True
                )

        gate = PublishGate(
            temp_worktree,
            command_runner=TimeoutRunner(),
            working_copy=GitWorkingCopy(),
            command="sleep 10",
            timeout_seconds=1,
        )
        result = gate.check(session_output_dir=session_output_dir)

        assert result.allowed is False
        assert result.record is not None
        assert result.record.timed_out is True
        assert "timed out" in result.reason.lower()

    def test_cache_hit_writes_validation_record_to_session_dir(
        self, temp_worktree, session_output_dir
    ):
        """A cache-hit publish gate must materialize the cached record into
        the session run dir. Otherwise downstream consumers (manifest,
        review-exchange cache predicate, UI) read whatever stale
        ``validation-record.json`` happens to be there from an earlier
        inline run and disagree with the gate's authoritative result."""
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )

        # First call runs validation and writes the record.
        gate.check(session_output_dir=session_output_dir)
        record_path = session_output_dir / "validation-record.json"
        assert record_path.exists()

        # Simulate a stale failed snapshot landing in the same path between
        # runs (e.g., an inline pre-publish validation that ran and failed).
        stale_payload = {
            "schema_version": 1,
            "suite": "publish_gate",
            "head_sha": "stale",
            "passed": False,
            "exit_code": 1,
            "command": "echo 'ok'",
            "started_at": "2026-04-28T00:00:00",
            "ended_at": "2026-04-28T00:00:01",
            "timed_out": False,
            "stdout_path": "stale-stdout.log",
            "stderr_path": "stale-stderr.log",
        }
        record_path.write_text(json.dumps(stale_payload))

        # Second call hits cache. The stale failed snapshot must be
        # replaced by the cached pass record so the manifest agrees with
        # the gate's allowed=True decision.
        result = gate.check(session_output_dir=session_output_dir)
        assert result.cache_hit is True
        assert result.allowed is True

        refreshed = json.loads(record_path.read_text())
        assert refreshed["passed"] is True
        assert refreshed["head_sha"] != "stale"

    def test_gate_reruns_on_cached_failure(self, temp_worktree, session_output_dir):
        """Test gate re-runs validation on cached failure (flaky test resilience).

        Failures are NOT trusted from cache because they might be due to flaky
        tests or transient issues. Only passes are cached and trusted.
        """
        gate = PublishGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="exit 1",
            timeout_seconds=10,
        )

        # First call fails
        result1 = gate.check(session_output_dir=session_output_dir)
        assert result1.allowed is False
        assert result1.cache_hit is False

        # Second call should re-run, not trust cached failure
        result2 = gate.check(session_output_dir=session_output_dir)
        assert result2.allowed is False
        assert result2.cache_hit is False  # Re-ran validation, didn't trust cache


class TestAgentGate:
    """Tests for AgentGate facade."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            # Initialize a git repo so we can get HEAD SHA
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
            # Create initial commit
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

    @pytest.fixture
    def session_output_dir(self, temp_worktree):
        """Create a session output directory."""
        output_dir = temp_worktree / ".issue-orchestrator" / "sessions" / "test-session"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def test_gate_disabled_when_no_command(self, temp_worktree, session_output_dir):
        """Test gate is disabled when no command is configured."""
        gate = AgentGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command=None,
        )
        result = gate.run(session_output_dir)

        assert result.passed is True
        assert "disabled" in result.reason.lower()
        assert result.record is None

    def test_gate_passes_when_command_succeeds(self, temp_worktree, session_output_dir):
        """Test gate passes when validation command succeeds."""
        gate = AgentGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )
        result = gate.run(session_output_dir)

        assert result.passed is True
        assert result.record is not None
        assert result.record.passed is True
        assert result.record.suite == "agent_gate"

    def test_agent_gate_does_not_append_publish_summary(
        self, temp_worktree, session_output_dir
    ):
        """Agent gate should not write PublishGate-only timing summaries."""
        gate = AgentGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )

        result = gate.run(session_output_dir)

        assert result.passed is True
        assert not any(
            record["kind"] == "validation_gate_summary"
            for record in _shared_timing_records(temp_worktree)
        )

    def test_gate_fails_when_command_fails(self, temp_worktree, session_output_dir):
        """Test gate fails when validation command fails."""
        gate = AgentGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="exit 1",
            timeout_seconds=10,
        )
        result = gate.run(session_output_dir)

        assert result.passed is False
        assert result.record is not None
        assert result.record.passed is False
        assert "failed" in result.reason.lower()

    def test_gate_always_runs_no_cache(self, temp_worktree, session_output_dir):
        """Test gate always runs validation (no caching)."""
        gate = AgentGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )

        # First call runs validation
        result1 = gate.run(session_output_dir)
        assert result1.passed is True
        assert result1.record is not None

        # Second call runs validation again (not cached)
        result2 = gate.run(session_output_dir)
        assert result2.passed is True
        assert result2.record is not None
        # Records should have different timestamps
        assert result2.record.started_at != result1.record.started_at

    def test_gate_fails_when_timeout(self, temp_worktree, session_output_dir):
        """Test gate fails when command times out."""

        class TimeoutRunner:
            def run(self, *args, **kwargs):
                return CommandResult(
                    returncode=-1, stdout="", stderr="", timed_out=True
                )

        gate = AgentGate(
            temp_worktree,
            command_runner=TimeoutRunner(),
            working_copy=GitWorkingCopy(),
            command="sleep 10",
            timeout_seconds=1,
        )
        result = gate.run(session_output_dir)

        assert result.passed is False
        assert result.record is not None
        assert result.record.timed_out is True
        assert "timed out" in result.reason.lower()

    def test_gate_writes_record_to_store(self, temp_worktree, session_output_dir):
        """Test gate writes validation record to store."""
        gate = AgentGate(
            temp_worktree,
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
            command="echo 'ok'",
            timeout_seconds=10,
        )
        result = gate.run(session_output_dir)

        # Verify record was written (new API uses just sha)
        store = ValidationRecordStore(temp_worktree)
        stored = store.read(result.record.head_sha)
        assert stored is not None
        assert stored.passed is True
