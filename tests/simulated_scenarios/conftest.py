from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from issue_orchestrator.domain.models import Issue, AgentConfig
from issue_orchestrator.domain.review_exchange_run import ReviewExchangeRunAssets
from issue_orchestrator.execution.agent_runner import AgentRunner, AgentSpec
from issue_orchestrator.ports.working_copy import (
    BranchPathsResult,
    BranchStatus,
    BranchTextFilesResult,
    CommitInfo,
    DiffResult,
    PreflightResult,
    PushResult,
    RebaseResult,
)
from issue_orchestrator.ports.worktree_manager import RegisteredWorktree, WorktreeInfo
from issue_orchestrator.infra.config import Config
from tests.conftest import (
    MockGitHubAdapter,
    MockEventSink,
    build_test_orchestrator_deps,
)
from issue_orchestrator.execution import CompositeEventSink
from issue_orchestrator.execution.timeline_event_sink import TimelineEventSink
from issue_orchestrator.execution.timeline_store import (
    SqliteTimelineStore,
    TimelineStoreConfig,
)
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.infra.orchestrator import Orchestrator
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.workflows.retrospective_review_workflow import (
    RetrospectiveReviewWorkflow,
)
from issue_orchestrator.control.workflows.review_workflow import ReviewWorkflow
from issue_orchestrator.control.workflows.rework_workflow import ReworkWorkflow


@pytest.fixture(autouse=True)
def _strip_nested_session_env(monkeypatch):
    """Allow Claude subprocess invocations from within a Claude Code session.

    Claude Code sets CLAUDECODE and CLAUDE_CODE_ENTRYPOINT to detect nested
    launches. Strip them so integration tests that spawn Claude subprocesses
    work regardless of whether the test runner itself is a Claude Code agent.
    """
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


def pytest_configure(config):
    """Register the per-test review-exchange outcome marker.

    Tests apply ``@pytest.mark.simulated_review_outcome(...)`` to override
    the conftest stub's default single-round reviewer-ok outcome. Without
    registration pytest emits a "unknown marker" warning treated as an
    error in strict mode.
    """
    config.addinivalue_line(
        "markers",
        "simulated_review_outcome(**fields): override the canned "
        "PersistentReviewExchangeRunner outcome for one test. Recognized "
        "fields: rounds, status, reason, reviewer_responses (list), "
        "coder_response_type, force_pre_validation_failure",
    )


def _validation_passed(run_dir: Path) -> bool:
    """Mirror :func:`persistent_session_exchange._validation_passed`.

    Existence of ``validation-record.json`` is not enough — the runner
    parses it and requires ``passed: true``. Invalid JSON or
    ``{"passed": false}`` must read as not-passed so a failed seeded
    record cannot let a reviewer ``ok`` slip through.
    """
    record_path = run_dir / "validation-record.json"
    if not record_path.exists():
        return False
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("passed"))


@pytest.fixture(autouse=True)
def _stub_persistent_review_exchange_setup(monkeypatch, request):
    """Bypass the persistent-session runner in simulated scenarios.

    The simulated-scenario harness was built around the spawn-per-phase
    review-exchange model where each round was a fresh subprocess managed
    by ``ScriptSessionRunner``. The persistent runner manages its own
    subprocesses via PTY directly, so scenarios that exercise
    review-exchange would now hit real ``git rev-parse``, ``git worktree
    add``, and PTY spawns against non-git scratch dirs.

    Replace :meth:`PersistentReviewExchangeRunner.run` with a stub that
    fabricates an outcome and the right event sequence. The default is a
    single-round reviewer-ok exchange; tests that need a different shape
    (multi-round, no-progress, max-rounds-exceeded, protocol error, etc.)
    apply ``@pytest.mark.simulated_review_outcome(...)`` to override.

    The persistent runner's own behavior is covered exhaustively by
    ``tests/unit/execution/test_persistent_session_exchange.py`` and
    ``test_persistent_round_runner.py`` against the real PTY runner;
    this conftest only stubs the integration boundary so the
    simulated-scenario harness doesn't need a real git repo or PTY.
    """
    from datetime import datetime, timezone

    from issue_orchestrator.domain.review_exchange import (
        ReviewExchangeOutcome,
        ReviewExchangeResponse,
    )
    from issue_orchestrator.domain.review_exchange_summary import (
        ReviewExchangeSummaryV1,
    )
    from issue_orchestrator.domain.runtime_config import RuntimeConfigReference
    from issue_orchestrator.events import EventName
    from issue_orchestrator.execution.persistent_review_exchange_runner import (
        PersistentReviewExchangeRunner,
    )

    marker = request.node.get_closest_marker("simulated_review_outcome")
    overrides: dict[str, object] = dict(marker.kwargs) if marker is not None else {}

    default_reviewer_responses = [
        {
            "response_type": "ok",
            "response_text": "LGTM (stubbed scenario response)",
            "getting_closer": True,
        }
    ]
    reviewer_responses_raw = overrides.get(
        "reviewer_responses",
        default_reviewer_responses,
    )
    coder_response_type = overrides.get("coder_response_type")
    rounds_override = overrides.get("rounds")
    status_override = overrides.get("status")
    reason_override = overrides.get("reason")
    # Mirror the reviewer_ok_with_validation.sh fixture: when a test
    # opts in via ``write_validation_record_passed=True``, the stub
    # writes a passing validation-record.json into the run dir before
    # the reviewer round so the runner's require_validation guard
    # accepts the reviewer-ok outcome.
    write_validation_record_passed = bool(
        overrides.get("write_validation_record_passed", False)
    )
    # Counterpart for the negative-seeded-record case: writes
    # ``{"passed": false}`` so the require_validation guard flips a
    # reviewer ``ok`` even though the file exists. Without this, the
    # simulated coverage would treat file-existence as success and
    # miss a failed/corrupt seeded record.
    write_validation_record_failed = bool(
        overrides.get("write_validation_record_failed", False)
    )

    def _stub_run(
        self,
        *,
        exchange_run,
        coder_worktree,
        issue_number,
        issue_title,  # noqa: ARG001
        coder_label,  # noqa: ARG001
        reviewer_label,  # noqa: ARG001
        coder_agent,  # noqa: ARG001
        reviewer_agent,  # noqa: ARG001
        runtime_config,
        max_rounds,
        max_no_progress,
        require_validation,
        initial_validation_record_path=None,
        approval_gate=None,
        web_port=None,  # noqa: ARG001
        nit_policy="surface",
        events=None,
        event_context=None,
    ):
        assert isinstance(runtime_config, RuntimeConfigReference)
        session_name = exchange_run.session_name
        run_dir = exchange_run.assets.run_dir
        exchange_dir = exchange_run.assets.exchange_dir
        exchange_dir.mkdir(parents=True, exist_ok=True)

        # Mirror the real runner: when an initial validation record was
        # passed in (cache hit / pre-seeded scenarios), copy it into the
        # exchange's run_dir before the require_validation guard fires.
        # Without this, scenarios named after the cache/seeding path
        # (cache_requires_validation, cache_invalid_validation_reruns)
        # silently fall through the production path they claim to test.
        if (
            initial_validation_record_path is not None
            and initial_validation_record_path.exists()
        ):
            seed_target = run_dir / "validation-record.json"
            if not seed_target.exists():
                seed_target.write_bytes(initial_validation_record_path.read_bytes())

        if write_validation_record_passed:
            (run_dir / "validation-record.json").write_text(
                json.dumps({"passed": True}),
                encoding="utf-8",
            )
        if write_validation_record_failed:
            (run_dir / "validation-record.json").write_text(
                json.dumps({"passed": False}),
                encoding="utf-8",
            )

        def _emit(name, payload):
            if events is None or event_context is None:
                return
            from issue_orchestrator.ports import make_trace_event

            enriched = dict(payload)
            enriched["run_dir"] = str(run_dir)
            enriched["session_run_id"] = exchange_run.run_id
            events.publish(make_trace_event(name, event_context.enrich(enriched)))

        _emit(
            EventName.REVIEW_EXCHANGE_STARTED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "exchange_dir": str(exchange_dir),
            },
        )

        # Walk the scripted reviewer responses, capping at max_rounds.
        # If validation is required and no record exists, the runner's
        # contract is to flip a reviewer "ok" into "changes_requested"
        # with reason "validation missing" — mirror that here so tests
        # that exercise the validation gate see the same shape.
        responses = list(reviewer_responses_raw)
        rounds_run = 0
        last_reviewer: ReviewExchangeResponse | None = None
        no_progress_streak = 0
        terminating_status: str | None = None
        terminating_reason: str | None = None

        for round_index in range(1, max_rounds + 1):
            if not responses:
                break
            entry = responses.pop(0) if len(responses) > 1 else responses[0]
            response_type = str(entry.get("response_type", "ok"))
            getting_closer = bool(entry.get("getting_closer", True))
            response_text = str(entry.get("response_text", "stub-reviewer"))

            if (
                response_type == "ok"
                and require_validation
                and not _validation_passed(run_dir)
            ):
                response_type = "changes_requested"
                response_text = "Validation record missing or failed"
                getting_closer = False

            if response_type == "ok" and approval_gate is not None:
                rejection_reason = approval_gate.rejection_reason()
                if rejection_reason is not None:
                    response_type = "changes_requested"
                    response_text = f"{rejection_reason} Address it and continue."
                    getting_closer = False

            reviewer = ReviewExchangeResponse(
                response_type=response_type,
                response_text=response_text,
                getting_closer=getting_closer,
            )
            last_reviewer = reviewer
            rounds_run = round_index
            _emit(
                EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
                {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "round_index": round_index,
                    "reviewer_response_type": reviewer.response_type,
                    "reviewer_response_text": reviewer.response_text,
                    "coder_response_type": coder_response_type,
                    "review_nit_policy": nit_policy,
                    "review_abstraction_status": "no_issues",
                },
            )

            if response_type == "ok":
                terminating_status = "ok"
                terminating_reason = "reviewer_ok"
                break
            if not getting_closer:
                no_progress_streak += 1
            else:
                no_progress_streak = 0
            if max_no_progress > 0 and no_progress_streak >= max_no_progress:
                terminating_status = "stopped"
                terminating_reason = "reviewer_reports_no_progress"
                break
        else:
            terminating_status = "stopped"
            terminating_reason = "max_rounds_exceeded"

        if terminating_status is None:
            terminating_status = "ok"
            terminating_reason = "reviewer_ok"

        # Marker-level overrides win — useful for protocol-error/error
        # scenarios that the round loop above can't naturally produce.
        if status_override is not None:
            terminating_status = str(status_override)
        if reason_override is not None:
            terminating_reason = str(reason_override)
        if rounds_override is not None:
            rounds_run = int(rounds_override)

        _emit(
            EventName.REVIEW_EXCHANGE_COMPLETED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "rounds": rounds_run,
                "status": terminating_status,
                "reason": terminating_reason,
                "review_nit_policy": nit_policy,
                "review_abstraction_status": "no_issues",
            },
        )
        summary = ReviewExchangeSummaryV1.from_payload(
            {
                "completed_rounds": rounds_run,
                "status": terminating_status,
                "response_text": last_reviewer.response_text if last_reviewer else None,
                "reason": terminating_reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        # The real runner persists summary.json atomically into
        # exchange_dir; the orchestration logic reads it on the next
        # tick to decide cache-hit / advance / halt. Mirror that here so
        # scenario tests that walk the run-dir layout match production.
        from issue_orchestrator.infra.atomic_io import atomic_write_json

        atomic_write_json(exchange_dir / "summary.json", summary.to_payload())
        return ReviewExchangeOutcome(
            status=terminating_status,
            rounds=rounds_run,
            reason=terminating_reason,
            run_assets=ReviewExchangeRunAssets.from_exchange_dir(exchange_dir),
            reviewer_response=last_reviewer,
            summary=summary,
        )

    monkeypatch.setattr(PersistentReviewExchangeRunner, "run", _stub_run)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "tests" / "simulated_scenarios" / "fixtures" / "scripts"

_RUN_DIR_RE = re.compile(r"ISSUE_ORCHESTRATOR_RUN_DIR=(['\"]?)([^'\"\s]+)\1")


class ScriptSessionRunner:
    """SessionRunner that executes commands via the unified AgentRunner.

    Uses the same PTY → raw terminal recording chain as production. This
    ensures simulated scenario tests exercise the real capture path,
    preventing regressions like #4057.
    """

    def __init__(self) -> None:
        self._last_output: dict[str, str] = {}
        self._runner = AgentRunner()

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,
    ) -> bool:
        python_bin_dir = str(Path(sys.executable).parent)

        # Extract run_dir from command to determine log/output paths.
        match = _RUN_DIR_RE.search(command)
        if match:
            run_dir = Path(match.group(2))
            if not run_dir.is_absolute():
                run_dir = (Path(working_dir) / run_dir).resolve()
        else:
            run_dir = (
                Path(working_dir) / ".issue-orchestrator" / "sessions" / "fallback"
            )

        spec = AgentSpec(
            command=["bash", "-c", command],
            working_dir=Path(working_dir),
            timeout_seconds=120,
            log_path=run_dir / "terminal-recording.jsonl",
            output_dir=run_dir,
            env_overrides={"PATH": f"{python_bin_dir}:{os.environ.get('PATH', '')}"},
        )
        result = self._runner.run(spec)

        # Populate _last_output from the canonical terminal recording for get_session_output().
        log_path = run_dir / "terminal-recording.jsonl"
        if log_path.exists():
            self._last_output[session_name] = _decode_terminal_recording(log_path)
        else:
            self._last_output[session_name] = ""

        return result.succeeded

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return False

    def session_exists_by_name(self, session_name: str) -> bool:
        return False

    def kill_session(self, session_id: int, session_name: str) -> None:
        return None

    def discover_running_sessions(self) -> list[dict]:
        return []

    def cleanup_idle_sessions(self) -> int:
        return 0

    def get_session_output(
        self, session_id: int, lines: int, session_name: str
    ) -> str | None:
        return self._last_output.get(session_name)

    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool:
        return False

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        return False

    def focus_session(self, session_id: int, session_name: str) -> bool:
        return False

    def on_orchestrator_startup(self) -> None:
        return None

    def on_orchestrator_shutdown(self) -> None:
        return None


class FastScriptSessionRunner:
    """Lightweight SessionRunner for state-machine scenarios.

    This bypasses the PTY/CleaningLogWriter path and runs the shell command
    directly, which is substantially cheaper for restart/recovery tests that
    only care about orchestrator behavior. Tests that verify ui-session.log
    filtering must opt into ``ScriptSessionRunner`` explicitly.
    """

    def __init__(self) -> None:
        self._last_output: dict[str, str] = {}

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,
    ) -> bool:
        python_bin_dir = str(Path(sys.executable).parent)

        match = _RUN_DIR_RE.search(command)
        if match:
            run_dir = Path(match.group(2))
            if not run_dir.is_absolute():
                run_dir = (Path(working_dir) / run_dir).resolve()
        else:
            run_dir = (
                Path(working_dir) / ".issue-orchestrator" / "sessions" / "fallback"
            )
        run_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["PATH"] = f"{python_bin_dir}:{env.get('PATH', '')}"
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=working_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        output = f"{result.stdout}{result.stderr}"
        log_path = run_dir / "ui-session.log"
        log_path.write_text(output, encoding="utf-8", errors="replace")
        self._last_output[session_name] = output
        return result.returncode == 0

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return False

    def session_exists_by_name(self, session_name: str) -> bool:
        return False

    def kill_session(self, session_id: int, session_name: str) -> None:
        return None

    def discover_running_sessions(self) -> list[dict]:
        return []

    def cleanup_idle_sessions(self) -> int:
        return 0

    def get_session_output(
        self, session_id: int, lines: int, session_name: str
    ) -> str | None:
        return self._last_output.get(session_name)

    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool:
        return False

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        return False

    def focus_session(self, session_id: int, session_name: str) -> bool:
        return False

    def on_orchestrator_startup(self) -> None:
        return None

    def on_orchestrator_shutdown(self) -> None:
        return None


def _decode_terminal_recording(path: Path) -> str:
    chunks: list[str] = []
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            chunks.append(raw_line)
            continue
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if isinstance(data_b64, str) and data_b64:
            chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
    return "".join(chunks)


@dataclass
class StubWorkingCopy:
    """Stub implementing the WorkingCopy protocol for simulated scenario tests.

    Must stay in sync with ``ports/working_copy.py``.  Extra methods
    (``list_branch_names``, ``default_branch``) are kept because they satisfy
    the ``CompletionProcessor.GitAdapter`` protocol.
    """

    branch: str = "issue-1"

    def get_head_sha(self, worktree: Path) -> str | None:
        return "deadbeef"

    def get_current_branch(self, worktree: Path) -> str | None:
        return self.branch

    def get_branch_status(self, worktree: Path) -> BranchStatus | None:
        return BranchStatus(
            branch=self.branch, ahead=0, behind=0, has_remote=True, clean=True
        )

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return False

    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool:
        return False

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        return []

    def fetch(self, worktree: Path, remote: str = "origin") -> bool:
        return True

    def list_remote_branches(
        self, repo_root: Path, remote: str = "origin"
    ) -> list[str]:
        return [f"{remote}/{self.branch}"]

    def get_commits_ahead_count(
        self, repo_root: Path, branch: str, base: str = "origin/main"
    ) -> int:
        return 0

    def get_last_commit_date(self, repo_root: Path, branch: str) -> str | None:
        return None

    def rebase_on_branch(
        self, worktree: Path, target: str = "origin/main"
    ) -> RebaseResult:
        return RebaseResult(success=True, message="ok")

    def create_branch_from_current(self, worktree: Path, branch: str) -> None:
        self.branch = branch

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        force_with_lease: bool = True,
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult:
        return PushResult(success=True, branch=self.branch, remote=remote, message="ok")

    def diff_against_base(self, worktree: Path, base_ref: str) -> DiffResult:
        return DiffResult(success=True, diff_text="")

    def read_branch_text_files(
        self, worktree: Path, paths: tuple[str, ...]
    ) -> BranchTextFilesResult:
        return BranchTextFilesResult(success=True)

    def branch_post_image_paths_against_base(
        self, worktree: Path, base_ref: str
    ) -> BranchPathsResult:
        return BranchPathsResult(success=True, paths=())

    def get_issue_number_from_branch(self, worktree: Path) -> int | None:
        parts = self.branch.split("-")
        if parts and parts[0].isdigit():
            return int(parts[0])
        return None

    def push_preflight(self, worktree: Path, remote: str = "origin") -> PreflightResult:
        return PreflightResult(would_succeed=True)

    def delete_remote_branch(
        self, repo_root: Path, branch: str, remote: str = "origin"
    ) -> bool:
        return True

    # --- Extra methods for CompletionProcessor.GitAdapter protocol ---

    def list_branch_names(self, worktree: Path) -> list[str]:
        return [self.branch]

    def default_branch(self, repo_root: Path, remote: str = "origin") -> str:
        return "main"


class TempWorktreeManager:
    def __init__(self, base: Path) -> None:
        self.base = base

    def create(
        self,
        repo_root: Path,
        issue_number: int,
        issue_title: str,
        worktree_base: Path | None = None,
        enforce_hooks: bool = True,
        pre_push_hook: Path | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        seed_ref: str | None = None,
        reuse_options=None,
        worktree_name: str | None = None,
    ):
        worktree = (worktree_base or self.base) / (worktree_name or f"sim-wt-{issue_number}")
        worktree.mkdir(parents=True, exist_ok=True)
        final_branch = branch_name or f"{issue_number}-sim"
        # Use a real git repo so branch/introspection commands work in scenarios.
        subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", final_branch],
            cwd=worktree,
            check=True,
            capture_output=True,
        )
        return WorktreeInfo(path=worktree, branch_name=final_branch)

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        del worktree_path, force
        return None

    def remove_checkout_and_branch(
        self, worktree_path: Path, *, force: bool = False
    ) -> None:
        del worktree_path, force
        return None

    def can_remove_without_user_changes(self, worktree_path: Path) -> bool:
        del worktree_path
        return False

    def list_registered(self, repo_root: Path) -> tuple[RegisteredWorktree, ...]:
        del repo_root
        return ()

    def extract_issue_number(self, branch_name: str) -> int | None:
        parts = branch_name.split("-")
        return int(parts[0]) if parts and parts[0].isdigit() else None


@pytest.fixture
def scenario_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    return repo_root


def build_config(
    repo_root: Path,
    *,
    coder_command: str,
    reviewer_command: str,
    review_exchange_mode: str = "via-local-loop",
    review_exchange_require_validation: bool = False,
    review_exchange_max_rounds: int = 5,
    review_exchange_max_no_progress: int = 2,
    validation_cmd: str | None = None,
    max_validation_retries: int = 0,
) -> Config:
    config = Config()
    config.repo_root = repo_root
    config.config_path = _write_runtime_config(repo_root, validation_cmd)
    config.repo = "local/test"
    config.worktree_base = repo_root / ".issue-orchestrator" / "worktrees"
    config.worktree_base.mkdir(parents=True, exist_ok=True)
    config.queue_refresh_seconds = 0
    # Simulated scenarios advance quickly without wall-clock waits; use
    # immediate network sync to keep mocked PR/label transitions deterministic.
    config.fetch_layer_network_sync_seconds = 0
    config.max_concurrent_sessions = 1
    config.review_enabled = True
    config.code_review_agent = "agent:reviewer"
    config.code_review_label = "needs-code-review"
    config.code_reviewed_label = "code-reviewed"
    config.review_exchange_mode = review_exchange_mode
    config.review_exchange_require_validation = review_exchange_require_validation
    config.review_exchange_max_rounds = review_exchange_max_rounds
    config.review_exchange_max_no_progress = review_exchange_max_no_progress
    config.filtering.label = "simulated-scenario"
    config.retry.max_validation_retries = max_validation_retries
    # Use port 0 so the completion command's push preflight check cannot accidentally
    # connect to a real orchestrator running on the default port (8080).
    # Connection to port 0 always fails with URLError → preflight skipped.
    config.web_port = 0

    if validation_cmd:
        config.validation.quick.cmd = validation_cmd
        config.validation.quick.timeout_seconds = 5
        config.validation.publish.cmd = validation_cmd
        config.validation.publish.timeout_seconds = 5

    prompt = repo_root / "prompt.md"
    prompt.write_text("Simulated scenario prompt")

    config.agents = {
        "agent:coder": AgentConfig(
            prompt_path=prompt,
            model="test",
            timeout_minutes=1,
            command=coder_command,
            reviewer="agent:reviewer",
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=prompt,
            model="test",
            timeout_minutes=1,
            command=reviewer_command,
        ),
    }
    return config


def _write_runtime_config(repo_root: Path, validation_cmd: str | None) -> Path:
    config_path = (
        repo_root
        / ".issue-orchestrator"
        / "config"
        / "modes"
        / "default"
        / "default.yaml"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    quick = {"cmd": validation_cmd, "timeout_seconds": 5} if validation_cmd else {}
    config_path.write_text(
        json.dumps(
            {
                "validation": {
                    "quick": quick,
                    "publish": quick,
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return config_path.resolve()


class FreshIssueReader:
    def __init__(self, labels_by_issue: dict[int, set[str]]) -> None:
        self._labels_by_issue = labels_by_issue

    def read_issue_labels(self, issue_number: int) -> list[str]:
        return sorted(self._labels_by_issue.get(issue_number, set()))


def build_orchestrator(
    repo_root: Path,
    issues: list[Issue],
    config: Config,
    *,
    repo_host: MockGitHubAdapter | None = None,
    events: MockEventSink | None = None,
    runner: ScriptSessionRunner | FastScriptSessionRunner | None = None,
    worktree_manager: TempWorktreeManager | None = None,
    working_copy: StubWorkingCopy | None = None,
    lease_renewer: object | None = None,
    reconcile: bool = False,
    fresh_labels: dict[int, set[str]] | None = None,
) -> tuple[Orchestrator, MockGitHubAdapter, MockEventSink, DefaultTimelineReader]:
    repo_host = repo_host or MockGitHubAdapter()
    repo_host.issues = issues

    events = events or MockEventSink()
    runner = runner or FastScriptSessionRunner()
    worktree_manager = worktree_manager or TempWorktreeManager(base=repo_root)
    working_copy = working_copy or StubWorkingCopy()
    timeline_store = SqliteTimelineStore(
        state_dir(repo_root) / "timeline.sqlite",
        TimelineStoreConfig(max_records=config.timeline.max_records),
    )
    timeline_writer = DefaultTimelineWriter(timeline_store)
    timeline_reader = DefaultTimelineReader(timeline_store)
    composite_events = CompositeEventSink(events, TimelineEventSink(timeline_writer))
    scheduler = Scheduler(config=config)
    planner = Planner(
        config=config,
        scheduler=scheduler,
        review_workflow=ReviewWorkflow(config=config, events=composite_events),
        retrospective_review_workflow=RetrospectiveReviewWorkflow(
            config=config,
            events=composite_events,
        ),
        rework_workflow=ReworkWorkflow(config=config, events=composite_events),
    )

    deps = build_test_orchestrator_deps(
        config,
        repo_host,
        composite_events,
        runner,
        worktree_manager,
        working_copy=working_copy,
        lease_renewer=lease_renewer,
        planner=planner,
        timeline_reader=timeline_reader,
        timeline_writer=timeline_writer,
    )

    if reconcile:
        deps.action_applier.reconcile = True
        labels_by_issue = fresh_labels or {}
        deps.action_applier.fresh_issue_reader = FreshIssueReader(labels_by_issue)

    orchestrator = Orchestrator(config=config, deps=deps)
    orchestrator.deps.completion_processor.set_event_emitter(
        composite_events,
        orchestrator.event_context,
    )
    return orchestrator, repo_host, events, timeline_reader


def run_until(orchestrator: Orchestrator, predicate, max_ticks: int = 10) -> None:
    for _ in range(max_ticks):
        orchestrator.tick()
        if predicate():
            return
    raise AssertionError("predicate not satisfied before max_ticks")


def run_until_event(
    orchestrator: Orchestrator, events: MockEventSink, name, max_ticks: int = 10
) -> None:
    def _predicate() -> bool:
        return any(e.name == name for e in events.events)

    run_until(orchestrator, _predicate, max_ticks=max_ticks)


def run_until_pending_reviews(
    orchestrator: Orchestrator, expected: int, max_ticks: int = 10
) -> None:
    def _predicate() -> bool:
        return len(orchestrator.state.pending_reviews) >= expected

    run_until(orchestrator, _predicate, max_ticks=max_ticks)
