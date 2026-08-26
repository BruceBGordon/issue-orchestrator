"""Tests for CompletionProcessor - verifies orchestrator applies labels correctly.

These tests verify that when an agent writes a completion.json, the orchestrator
(via CompletionProcessor) correctly executes the requested actions including
label application.

Architecture reminder:
- Agent writes completion.json with requested_actions
- Orchestrator reads it and calls CompletionProcessor.process()
- CompletionProcessor executes actions via adapters (labels, PR, comments)
"""

import json
import pytest

from issue_orchestrator.domain.dirty_remediation import (
    ESCALATION_STATUSES,
    NEVER_DESTROY_UNKNOWN,
    DirtyTreeDisposition,
    blocked_reason,
    dirty_tree_disposition,
)
from issue_orchestrator.entrypoints.cli_tools.agent_done import (
    STATUS_TO_ACTIONS,
    AgentStatus,
)
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, call, patch

from issue_orchestrator.domain.models import (
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    COMPLETION_RECORD_PATH,
    AgentConfig,
)
from issue_orchestrator.domain.review_exchange import ReviewExchangeOutcome
from issue_orchestrator.domain.review_exchange_run import (
    ReviewExchangeRun,
    ReviewExchangeRunAssets,
)
from issue_orchestrator.domain.review_exchange_summary import ReviewExchangeSummaryV1
from issue_orchestrator.domain.runtime_config import RuntimeConfigReference
from tests.callback_endpoint_helpers import ready_callback_endpoint
from issue_orchestrator.control.completion_processor import (
    CompletionProcessor,
    ProcessingResult,
    LabelAdapter,
    PRAdapter,
    GitAdapter,
)
from issue_orchestrator.control.review_exchange_pr_comment import (
    GITHUB_COMMENT_BODY_LIMIT,
)
from issue_orchestrator.control.background_job_supervisor import BackgroundJobSupervisor
from issue_orchestrator.control.pre_publish_gate import PrePublishGateResult
from issue_orchestrator.execution.review_artifact_reader import (
    ManifestReviewArtifactReader,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from issue_orchestrator.ports.background_job import CompletedJob
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.review_artifact_reader import (
    ReviewArtifactContent,
    ReviewArtifactReadCommand,
)
from issue_orchestrator.ports.working_copy import (
    BranchPathsResult,
    BranchTextFile,
    BranchTextFilesResult,
    DiffResult,
    PushResult,
)
from issue_orchestrator.domain.events import EventBus, SessionEvent
from issue_orchestrator.infra.issue_diagnostics import DiagnosticReference
from tests.unit.session_run_helpers import make_session_run_assets


# ==================== Fixtures ====================


def _write_test_config(tmp_path: Path) -> Path:
    config_path = (
        tmp_path
        / ".issue-orchestrator"
        / "config"
        / "modes"
        / "default"
        / "default.yaml"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(
            "validation:\n  quick:\n    cmd: 'true'\n", encoding="utf-8"
        )
    return config_path


def _review_exchange_outcome(
    exchange_run: ReviewExchangeRun,
    *,
    status: str = "ok",
    rounds: int = 1,
    reason: str = "reviewer_ok",
    summary: dict[str, object] | None = None,
) -> ReviewExchangeOutcome:
    reason = {
        "approved": "reviewer_ok",
        "boom": "coder_protocol_error",
        "max_no_progress": "reviewer_reports_no_progress",
        "no-validation": "coder_protocol_error",
    }.get(reason, reason)
    typed_summary = None
    if summary is not None:
        payload = {
            "completed_rounds": rounds,
            "status": status,
            "reason": reason,
            "response_text": None,
            "timestamp": "2026-02-01T00:00:00+00:00",
        }
        payload.update(summary)
        typed_summary = ReviewExchangeSummaryV1.from_payload(payload)
    return ReviewExchangeOutcome(
        status=status,
        rounds=rounds,
        reason=reason,
        run_assets=exchange_run.assets,
        summary=typed_summary,
    )


class _FixedReviewExchangeSessionOutput(FileSystemSessionOutput):
    def __init__(self, review_run_dir: Path) -> None:
        super().__init__()
        self.review_run_dir = review_run_dir

    def start_review_exchange_run(
        self,
        worktree_path: Path,
        *,
        issue_number: int,
        parent_session_name: str,
        agent_label: str,
    ) -> ReviewExchangeRun:
        self.review_run_dir.mkdir(parents=True, exist_ok=True)
        assets = ReviewExchangeRunAssets.from_run_dir(self.review_run_dir)
        assets.exchange_dir.mkdir(parents=True, exist_ok=True)
        return ReviewExchangeRun(
            session_name=f"review-exchange-{issue_number}",
            run_id=self.review_run_dir.name.split("__", 1)[0],
            parent_session_name=parent_session_name,
            assets=assets,
        )


class _FakeReviewArtifactReader:
    def __init__(self, content: str) -> None:
        self.content = content
        self.commands: list[ReviewArtifactReadCommand] = []

    def read_review_artifact(
        self,
        command: ReviewArtifactReadCommand,
    ) -> ReviewArtifactContent:
        self.commands.append(command)
        return ReviewArtifactContent(
            issue_number=command.issue_number,
            run_dir=command.run_dir,
            artifact_path=Path(command.artifact_path),
            artifact_type=command.artifact_type,
            content_type="text/markdown",
            content=self.content,
        )


class _RunningReviewExchangeJobRunner:
    def __init__(self, running_ids: set[str]) -> None:
        self.running_ids = set(running_ids)

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:  # noqa: ARG002
        return False

    def is_running(self, job_id: str) -> bool:
        return job_id in self.running_ids

    def drain_completed(self) -> list[CompletedJob]:
        return []


class _CapturingReviewExchangeRunner:
    """Review-exchange port fake that records public ``run`` calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs) -> ReviewExchangeOutcome:
        self.calls.append(dict(kwargs))
        return _review_exchange_outcome(
            kwargs["exchange_run"],
            status="ok",
            rounds=1,
            reason="reviewer_ok",
        )

    def job_timeout_seconds(self, **_kwargs) -> float:
        return 60.0


@pytest.fixture
def mock_label_adapter():
    """Mock adapter for label operations."""
    adapter = Mock(spec=LabelAdapter)
    adapter.add_label = Mock()
    adapter.remove_label = Mock()
    return adapter


@pytest.fixture
def mock_pr_adapter():
    """Mock adapter for PR operations."""
    adapter = Mock(spec=PRAdapter)
    adapter.get_prs_for_issue = Mock(return_value=[])
    adapter.get_prs_for_branch = Mock(return_value=[])
    adapter.create_pr = Mock(
        return_value=PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="issue-123",
            body="Test body",
            state="open",
            labels=[],
        )
    )
    adapter.add_comment = Mock(return_value="comment-id")
    return adapter


@pytest.fixture
def mock_git_adapter():
    """Mock adapter for git operations."""
    adapter = Mock(spec=GitAdapter)
    adapter.push = Mock(
        return_value=PushResult(
            success=True,
            branch="issue-123",
            remote="origin",
            message="Pushed",
        )
    )
    adapter.rebase_on_branch = Mock(
        return_value=MagicMock(success=True, message="Rebased")
    )
    adapter.create_branch_from_current = Mock()
    adapter.list_branch_names = Mock(return_value=["issue-123"])
    adapter.get_current_branch = Mock(return_value="issue-123")
    adapter.get_head_sha = Mock(return_value=None)
    adapter.has_uncommitted_changes = Mock(return_value=False)
    adapter.has_tracked_changes = Mock(return_value=False)
    adapter.list_dirty_files = Mock(return_value=[])
    adapter.diff_against_base = Mock(
        return_value=DiffResult(success=True, diff_text="")
    )
    adapter.read_branch_text_files = Mock(
        return_value=BranchTextFilesResult(success=True)
    )
    adapter.branch_post_image_paths_against_base = Mock(
        return_value=BranchPathsResult(success=True, paths=())
    )
    return adapter


@pytest.fixture
def event_bus():
    """EventBus for capturing emitted events."""
    return EventBus()


@pytest.fixture
def tech_lead_authority_store(tmp_path):
    """Retained launch-authority collaborator for Tech Lead completion tests."""
    from issue_orchestrator.infra.tech_lead_authority_store import (
        SqliteTechLeadAuthorityStore,
    )

    return SqliteTechLeadAuthorityStore.for_repo(tmp_path)


@pytest.fixture
def processor(mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus):
    """Create a CompletionProcessor with mocked adapters."""
    return CompletionProcessor(
        agent_callback_endpoint=ready_callback_endpoint(),
        label_adapter=mock_label_adapter,
        pr_adapter=mock_pr_adapter,
        git_adapter=mock_git_adapter,
        event_bus=event_bus,
        session_output=FileSystemSessionOutput(),
        label_config={
            "blocked": "blocked",
            "needs_human": "needs-human",
            "code_reviewed": "code-reviewed",
            "needs_rework": "needs-rework",
            "code_review": "needs-code-review",
            "in_progress": "in-progress",
        },
    )


def make_record(
    outcome: CompletionOutcome,
    requested_actions: list[RequestedAction],
    summary: str = "Test summary",
    **kwargs,
) -> CompletionRecord:
    """Helper to create CompletionRecord with required fields."""
    return CompletionRecord(
        session_id="test-session",
        timestamp=datetime.now().isoformat(),
        outcome=outcome,
        summary=summary,
        requested_actions=requested_actions,
        **kwargs,
    )


@pytest.fixture
def worktree_with_completion(tmp_path):
    """Factory for creating worktrees with completion records."""

    def _create(record: CompletionRecord) -> Path:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path = record_dir / "completion.json"
        record_path.write_text(json.dumps(record.to_dict()))
        # Create session output directory if session_id is present
        if record.session_id:
            session_dir = record_dir / "sessions" / record.session_id
            session_dir.mkdir(parents=True, exist_ok=True)
        return worktree

    return _create


# ==================== Unit Tests ====================


class TestCompletionProcessorLabelActions:
    """Tests for label-related actions from completion records."""

    def test_completed_outcome_does_not_add_labels_directly(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Completed outcome requests push/PR, no label actions needed."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert result.success
        mock_label_adapter.add_label.assert_not_called()
        mock_label_adapter.remove_label.assert_not_called()


class _FakeStackGate:
    """Duck-typed StackBaseGate returning canned decisions (#6596).

    ``decide_publish`` (PR creation) returns ``decision``; ``decide_work`` (push
    retry rebase base) returns ``work_decision`` (defaulting to ``decision``).
    """

    def __init__(self, decision, work_decision=None):
        self._decision = decision
        self._work_decision = work_decision if work_decision is not None else decision
        self.calls: list[int] = []
        self.work_calls: list[int] = []

    def decide_publish(self, issue_number, worktree):
        self.calls.append(issue_number)
        return self._decision

    def decide_work(self, issue_number):
        self.work_calls.append(issue_number)
        return self._work_decision


class TestStackPublishGateWiring:
    """The processor consumes the stack publish gate to base the PR on the
    predecessor branch (#6596) and to fail fast when the gate is blocked."""

    @staticmethod
    def _publish_record() -> CompletionRecord:
        return make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            implementation="Added the feature",
        )

    def test_stack_successor_pr_bases_on_predecessor_branch(
        self, processor, mock_pr_adapter, worktree_with_completion
    ):
        from issue_orchestrator.control.stack_publish_gate import StackPublishDecision

        processor.attach_stack_publish_gate(
            _FakeStackGate(StackPublishDecision(
                is_stack=True, allowed=True, base_branch="20-base",
            ))
        )
        worktree = worktree_with_completion(self._publish_record())

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert result.success
        assert mock_pr_adapter.create_pr.call_args.kwargs["base"] == "20-base"

    def test_blocked_stack_publish_gate_halts_pr_creation(
        self, processor, mock_pr_adapter, worktree_with_completion
    ):
        from issue_orchestrator.control.stack_publish_gate import StackPublishDecision

        processor.attach_stack_publish_gate(
            _FakeStackGate(StackPublishDecision(
                is_stack=True, allowed=False,
                reason="Stack publish gate blocked: publish: blocked (base_branch_conflict)",
            ))
        )
        worktree = worktree_with_completion(self._publish_record())

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert not result.success
        mock_pr_adapter.create_pr.assert_not_called()
        assert any("base_branch_conflict" in e for e in result.errors)

    def test_non_stack_decision_keeps_default_base(
        self, processor, mock_pr_adapter, worktree_with_completion
    ):
        from issue_orchestrator.control.stack_publish_gate import StackPublishDecision

        processor.attach_stack_publish_gate(
            _FakeStackGate(StackPublishDecision.not_stack())
        )
        worktree = worktree_with_completion(self._publish_record())

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert result.success
        # No stack base -> the processor's default base (main) is used.
        assert mock_pr_adapter.create_pr.call_args.kwargs["base"] == "main"


class TestStackPublishGatePRReuse:
    """A reused stack-successor PR must target the gate's required base (#6596).

    F2: existing-PR reuse previously matched only on issue + head branch, so a
    successor PR opened against the wrong base could be reused and pick up
    review-completion labels while still targeting the wrong branch.
    """

    @staticmethod
    def _publish_record() -> CompletionRecord:
        return make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            implementation="Added the feature",
        )

    @staticmethod
    def _stack_gate():
        from issue_orchestrator.control.stack_publish_gate import StackPublishDecision

        return _FakeStackGate(
            StackPublishDecision(is_stack=True, allowed=True, base_branch="20-base")
        )

    @staticmethod
    def _existing_pr(base_branch: str | None):
        return PRInfo(
            number=99,
            title="#123 Existing PR",
            url="https://github.com/owner/repo/pull/99",
            branch="123-feature",
            body="Body",
            state="open",
            labels=[],
            base_branch=base_branch,
        )

    def _run(self, processor, mock_git_adapter, worktree_with_completion):
        mock_git_adapter.get_current_branch.return_value = "123-feature"
        worktree = worktree_with_completion(self._publish_record())
        return processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

    def test_reuse_with_correct_stack_base_does_not_retarget(
        self, processor, mock_pr_adapter, mock_git_adapter, worktree_with_completion
    ):
        processor.attach_stack_publish_gate(self._stack_gate())
        mock_pr_adapter.get_prs_for_issue.return_value = [self._existing_pr("20-base")]

        result = self._run(processor, mock_git_adapter, worktree_with_completion)

        assert result.success
        assert result.pr_url == "https://github.com/owner/repo/pull/99"
        mock_pr_adapter.set_pr_base.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()

    def test_reuse_with_wrong_base_retargets_through_owned_op(
        self, processor, mock_pr_adapter, mock_git_adapter, worktree_with_completion
    ):
        processor.attach_stack_publish_gate(self._stack_gate())
        mock_pr_adapter.get_prs_for_issue.return_value = [self._existing_pr("main")]

        result = self._run(processor, mock_git_adapter, worktree_with_completion)

        assert result.success
        assert result.pr_url == "https://github.com/owner/repo/pull/99"
        mock_pr_adapter.set_pr_base.assert_called_once_with(99, "20-base")
        mock_pr_adapter.create_pr.assert_not_called()

    def test_reuse_blocks_when_retarget_fails(
        self, processor, mock_pr_adapter, mock_git_adapter, worktree_with_completion
    ):
        processor.attach_stack_publish_gate(self._stack_gate())
        mock_pr_adapter.get_prs_for_issue.return_value = [self._existing_pr("main")]
        mock_pr_adapter.set_pr_base.side_effect = RuntimeError("403 forbidden")

        result = self._run(processor, mock_git_adapter, worktree_with_completion)

        assert not result.success
        mock_pr_adapter.create_pr.assert_not_called()
        assert any("retarget failed" in e for e in result.errors)


class TestStackCreatedPRBaseEnforcement:
    """Every stack PR from the create/collision path must target the gate's base.

    F2: ``create_pr`` is idempotent by head branch, so it can return an existing
    PR on the wrong base even when the issue-scoped reuse preflight misses it. The
    processor must retarget (or fail closed) on the returned PR before labels.
    """

    @staticmethod
    def _publish_record() -> CompletionRecord:
        return make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            implementation="Added the feature",
        )

    @staticmethod
    def _stack_gate():
        from issue_orchestrator.control.stack_publish_gate import StackPublishDecision

        return _FakeStackGate(
            StackPublishDecision(is_stack=True, allowed=True, base_branch="20-base")
        )

    def _run(self, processor, mock_pr_adapter, mock_git_adapter, worktree_with_completion):
        # Issue-scoped reuse preflight finds nothing, but create_pr is idempotent
        # and returns an existing PR targeting the wrong (default) base.
        mock_pr_adapter.get_prs_for_issue.return_value = []
        mock_git_adapter.get_current_branch.return_value = "123-feature"
        mock_pr_adapter.create_pr.return_value = PRInfo(
            number=42, title="#123: Test", url="https://github.com/owner/repo/pull/42",
            branch="123-feature", body="", state="open", labels=[], base_branch="main",
        )
        worktree = worktree_with_completion(self._publish_record())
        return processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

    def test_idempotent_create_wrong_base_is_retargeted(
        self, processor, mock_pr_adapter, mock_git_adapter, worktree_with_completion
    ):
        processor.attach_stack_publish_gate(self._stack_gate())

        result = self._run(processor, mock_pr_adapter, mock_git_adapter, worktree_with_completion)

        assert result.success
        mock_pr_adapter.set_pr_base.assert_called_once_with(42, "20-base")

    def test_idempotent_create_wrong_base_halts_on_retarget_failure(
        self, processor, mock_pr_adapter, mock_git_adapter, worktree_with_completion
    ):
        processor.attach_stack_publish_gate(self._stack_gate())
        mock_pr_adapter.set_pr_base.side_effect = RuntimeError("403 forbidden")

        result = self._run(processor, mock_pr_adapter, mock_git_adapter, worktree_with_completion)

        assert not result.success
        assert any("retarget failed" in e for e in result.errors)
        # Review-completion labels must not be applied to the wrong-base PR.
        assert not any(
            c.args and c.args[0] == 42 for c in mock_pr_adapter.add_comment.call_args_list
        )


class TestRuntimeArtifactBranchGuard:
    """Pre-publish guard rejects committed IO runtime artifacts (#6659).

    The guard consumes branch-tip post-image paths from
    ``branch_post_image_paths_against_base`` (a path-oriented Git query) rather
    than parsing diff text, so it sees every change shape git can emit —
    text/binary/empty-file additions and rename/copy destinations — uniformly.
    The diff-shape coverage lives with the adapter in
    ``test_git_working_copy.py``; here we verify the policy on the resulting
    path list.
    """

    @staticmethod
    def _publish_record() -> CompletionRecord:
        return make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            implementation="Added the feature",
        )

    @pytest.mark.parametrize(
        "artifact_path",
        [
            ".issue-orchestrator/persistent-pairs/issue-6594/coder/terminal-recording.jsonl",
            ".issue-orchestrator/review-exchange-turn-prompt.md",
            ".issue-orchestrator/review-feedback/cycle-1.md",
            ".issue-orchestrator/review-response.json",
            ".issue-orchestrator/review-report.md",
            # A binary blob and an empty-file addition both surface as plain
            # branch-tip paths here — no diff text is parsed — so they are
            # blocked just like any other runtime output.
            ".issue-orchestrator/tool-homes/blob.bin",
        ],
    )
    def test_committed_runtime_artifact_blocks_publish(
        self,
        processor,
        mock_git_adapter,
        mock_pr_adapter,
        worktree_with_completion,
        artifact_path,
    ):
        worktree = worktree_with_completion(self._publish_record())
        mock_git_adapter.branch_post_image_paths_against_base = Mock(
            return_value=BranchPathsResult(
                success=True, paths=("src/app.py", artifact_path)
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=6594,
            issue_title="Test Issue",
        )

        assert not result.success
        assert "runtime artifacts" in (result.message or "")
        assert artifact_path in (result.message or "")
        # Fails before any publish action runs.
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()

    def test_tracked_config_yaml_does_not_block(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        worktree = worktree_with_completion(self._publish_record())
        mock_git_adapter.branch_post_image_paths_against_base = Mock(
            return_value=BranchPathsResult(
                success=True,
                paths=(
                    ".issue-orchestrator/config/modes/default/main.yaml",
                    "src/app.py",
                ),
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert result.success

    def test_path_scan_failure_fails_closed(
        self, processor, mock_git_adapter, mock_pr_adapter, worktree_with_completion
    ):
        # The earlier test-skip scan reads diff text and must pass, so the
        # runtime-artifact path scan is the one that fails here.
        worktree = worktree_with_completion(self._publish_record())
        mock_git_adapter.diff_against_base = Mock(
            return_value=DiffResult(success=True, diff_text="")
        )
        mock_git_adapter.branch_post_image_paths_against_base = Mock(
            return_value=BranchPathsResult(success=False, error="fatal: bad revision")
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert not result.success
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()


class TestReviewExchangeModeResolution:
    """Tests for review exchange mode selection and derivation."""

    def _make_config(self, tmp_path: Path) -> Config:
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")
        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "auto"
        config.code_review_agent = "agent:reviewer"
        config.config_path = _write_test_config(tmp_path)
        config.agents = {
            "agent:coder": AgentConfig(
                prompt_path=coder_prompt, ai_system="claude-code"
            ),
            "agent:reviewer": AgentConfig(
                prompt_path=reviewer_prompt, ai_system="codex"
            ),
        }
        return config

    def _make_processor(self, config: Config) -> CompletionProcessor:
        return CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=Mock(spec=LabelAdapter),
            pr_adapter=Mock(spec=PRAdapter),
            git_adapter=Mock(spec=GitAdapter),
            session_output=FileSystemSessionOutput(),
            event_bus=EventBus(),
            label_config={},
            config=config,
        )

    def test_auto_mode_uses_mcp_when_supported(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        processor = self._make_processor(config)

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )

        assert processor._resolve_review_exchange_mode("agent:coder") == "via-mcp"  # noqa: SLF001

    def test_explicit_local_loop_mode_does_not_depend_on_review_enabled(self, tmp_path):
        config = self._make_config(tmp_path)
        config.review_enabled = False
        config.review_exchange_mode = "via-local-loop"
        processor = self._make_processor(config)

        assert processor._resolve_review_exchange_mode("agent:coder") == "via-local-loop"  # noqa: SLF001


class TestReviewExchangeExecution:
    """Tests for review exchange execution paths."""

    def _make_config(self, tmp_path: Path) -> Config:
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")
        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-mcp"
        config.code_review_agent = "agent:reviewer"
        config.config_path = _write_test_config(tmp_path)
        config.agents = {
            "agent:coder": AgentConfig(
                prompt_path=coder_prompt, ai_system="claude-code"
            ),
            "agent:reviewer": AgentConfig(
                prompt_path=reviewer_prompt, ai_system="codex"
            ),
        }
        return config

    def _make_processor(self, config: Config) -> CompletionProcessor:
        from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
            InMemoryPersistentExchangePairRegistry,
        )
        from issue_orchestrator.execution.persistent_review_exchange_runner import (
            PersistentReviewExchangeRunner,
        )

        session_output = FileSystemSessionOutput()
        return CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=Mock(spec=LabelAdapter),
            pr_adapter=Mock(spec=PRAdapter),
            git_adapter=Mock(spec=GitAdapter),
            session_output=session_output,
            review_exchange_runner=PersistentReviewExchangeRunner(
                session_output,
                InMemoryPersistentExchangePairRegistry(),
            ),
            event_bus=EventBus(),
            label_config={},
            config=config,
        )

    def test_exchange_failure_halts_before_pr_creation(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
        monkeypatch,
    ) -> None:
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="error", rounds=1, reason="boom"
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is False
        assert result.pr_url is None
        assert result.errors and "review_exchange" in result.errors[0]
        mock_pr_adapter.create_pr.assert_not_called()

    def test_exchange_success_marks_review_labels(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        config.worktree_remediation_pr_collision = "reuse_open"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="ok", rounds=2, reason="reviewer_ok"
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        assert result.actions_taken is not None
        assert result.actions_taken[0] == "Review exchange passed"
        mock_label_adapter.add_label.assert_any_call(42, "code-reviewed")
        mock_label_adapter.remove_label.assert_any_call(42, "needs-code-review")

    @pytest.mark.parametrize(
        ("create_pr_error", "manifest", "expected_error"),
        [
            pytest.param(
                RuntimeError("connection reset"),
                {"reset_from_scratch": True},
                "connection reset",
                id="different-error",
            ),
            pytest.param(
                RuntimeError("No commits between main and issue-123"),
                None,
                "Cannot create PR: no commits between main and issue-123",
                id="no-commits-without-fresh-manifest",
            ),
        ],
    )
    def test_unrecovered_create_pr_failure_after_review_fails_loudly(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
        create_pr_error: RuntimeError,
        manifest: dict[str, object] | None,
        expected_error: str,
    ) -> None:
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
                RequestedAction.POST_COMMENT,
            ],
            comment_body="Should not be posted after PR creation failure.",
        )
        worktree = worktree_with_completion(record)
        if manifest is not None:
            run_dir = worktree / ".issue-orchestrator" / "sessions" / record.session_id
            (run_dir / "manifest.json").write_text(json.dumps(manifest))
        mock_git_adapter.default_branch.return_value = "main"
        mock_pr_adapter.create_pr.side_effect = create_pr_error
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="ok", rounds=1, reason="reviewer_ok"
            )
        )
        processor._emit_publish_failed = MagicMock()  # noqa: SLF001

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is False
        assert result.errors is not None
        assert any(expected_error in error for error in result.errors)
        processor._emit_publish_failed.assert_called_once()  # noqa: SLF001
        emit_kwargs = processor._emit_publish_failed.call_args.kwargs  # noqa: SLF001
        assert emit_kwargs["issue_number"] == 123
        assert emit_kwargs["stage"] == RequestedAction.CREATE_PR.value
        assert expected_error in emit_kwargs["error"]
        # Failure diagnostics still post; the requested success comment must not.
        comments = [
            call_args.args[1]
            for call_args in mock_pr_adapter.add_comment.call_args_list
        ]
        assert "Should not be posted after PR creation failure." not in comments
        assert any("Orchestrator Processing Failed" in comment for comment in comments)
        mock_label_adapter.add_label.assert_not_called()

    def test_existing_pr_reuse_does_not_bypass_local_loop_review(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """Existing PR reuse must run local-loop review before returning success."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        mock_pr_adapter.get_prs_for_issue.return_value = [
            PRInfo(
                number=99,
                title="#123 Existing PR",
                url="https://github.com/owner/repo/pull/99",
                branch="issue-123",
                body="Body",
                state="open",
                labels=[],
            )
        ]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="error", rounds=1, reason="boom"
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is False
        assert result.pr_url is None
        processor._run_review_exchange_loop.assert_called_once()  # noqa: SLF001
        mock_pr_adapter.create_pr.assert_not_called()

    def test_existing_pr_reuse_after_local_loop_success_marks_review_complete(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """Reused PR should still get local-loop completion labels/comment."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        config.worktree_remediation_pr_collision = "reuse_open"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
            review_artifact_reader=ManifestReviewArtifactReader(),
        )
        mock_pr_adapter.get_prs_for_issue.return_value = [
            PRInfo(
                number=99,
                title="#123 Existing PR",
                url="https://github.com/owner/repo/pull/99",
                branch="issue-123",
                body="Body",
                state="open",
                labels=[],
            )
        ]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="ok", rounds=2, reason="reviewer_ok"
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        assert result.pr_url == "https://github.com/owner/repo/pull/99"
        assert result.review_exchange_completed is True
        processor._run_review_exchange_loop.assert_called_once()  # noqa: SLF001
        mock_pr_adapter.create_pr.assert_not_called()
        mock_label_adapter.add_label.assert_any_call(99, "code-reviewed")
        mock_label_adapter.remove_label.assert_any_call(99, "needs-code-review")

    def test_existing_pr_reuse_ignores_prior_attempt_branch_and_creates_new_pr(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
        caplog,
    ) -> None:
        """Scratch retries must not reuse an open PR from an older branch."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        config.worktree_remediation_pr_collision = "reuse_open"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        mock_git_adapter.get_current_branch.return_value = "123-fresh-branch"
        mock_pr_adapter.get_prs_for_issue.return_value = [
            PRInfo(
                number=99,
                title="#123 Existing PR",
                url="https://github.com/owner/repo/pull/99",
                branch="123-old-branch",
                body="Body",
                state="open",
                labels=[],
            )
        ]
        mock_pr_adapter.create_pr.return_value = PRInfo(
            number=100,
            title="#123 Fresh PR",
            url="https://github.com/owner/repo/pull/100",
            branch="123-fresh-branch",
            body="Body",
            state="open",
            labels=[],
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="ok", rounds=1, reason="reviewer_ok"
            )
        )

        with caplog.at_level("INFO"):
            result = processor.process(
                worktree,
                run_assets=make_session_run_assets(worktree),
                issue_number=123,
                issue_title="Test Issue",
                agent_label="agent:coder",
            )

        assert result.success is True
        assert result.pr_url == "https://github.com/owner/repo/pull/100"
        mock_pr_adapter.create_pr.assert_called_once()
        assert (
            "Ignoring open PR from prior attempt for issue #123: pr=99 branch=123-old-branch expected_branch=123-fresh-branch"
            in caplog.text
        )

    def test_local_loop_emits_review_started_and_approved_trace_events(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """Local-loop success should publish explicit review lifecycle trace events."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        config.review_exchange_require_validation = True
        review_artifact_reader = _FakeReviewArtifactReader(
            "# Review Report\n\nNo issues.\n"
        )
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
            review_artifact_reader=review_artifact_reader,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        worktree = worktree_with_completion(record)
        captured_report: list[Path] = []

        def run_review_exchange(**kw):
            exchange_run = kw["exchange_run"]
            report = (
                exchange_run.assets.exchange_dir
                / "turns"
                / "round-001.reviewer.attempt-001.review-report.md"
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("# Review Report\n\nNo issues.\n", encoding="utf-8")
            captured_report.append(report)
            return _review_exchange_outcome(
                exchange_run,
                status="ok",
                rounds=1,
                reason="reviewer_ok",
                summary={
                    "artifacts": [
                        {
                            "type": "review_report",
                            "label": "Review report",
                            "value": str(report),
                            "render_mode": "markdown",
                        }
                    ]
                },
            )

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=run_review_exchange
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        review_comment = mock_pr_adapter.add_comment.call_args.args[1]
        assert "✅ Review completed via via-local-loop loop." in review_comment
        assert "- Validation: required and passed" in review_comment
        assert "---" in review_comment
        assert "# Review Report" in review_comment
        exchange_run = processor._run_review_exchange_loop.call_args.kwargs[  # noqa: SLF001
            "exchange_run"
        ]
        report = captured_report[0]
        assert review_artifact_reader.commands == [
            ReviewArtifactReadCommand(
                issue_number=123,
                run_dir=exchange_run.assets.run_dir,
                artifact_path=str(report),
                artifact_type="review_report",
            )
        ]
        event_names = sink.event_names()
        assert str(EventName.REVIEW_STARTED) in event_names
        assert str(EventName.REVIEW_APPROVED) in event_names
        assert event_names.index(str(EventName.REVIEW_STARTED)) < event_names.index(
            str(EventName.REVIEW_APPROVED)
        )
        review_started = sink.last_event(str(EventName.REVIEW_STARTED))
        review_approved = sink.last_event(str(EventName.REVIEW_APPROVED))
        assert review_started is not None
        assert review_approved is not None
        assert review_started.data.get("run_dir") == str(exchange_run.assets.run_dir)
        assert review_approved.data.get("run_dir") == str(exchange_run.assets.run_dir)
        review_events = [
            event for event in sink.events if str(event.name).startswith("review.")
        ]
        assert review_events, "Expected review lifecycle events to be emitted"
        for event in review_events:
            assert event.data.get("run_dir") == str(exchange_run.assets.run_dir), (
                f"missing run_dir on {event.name}"
            )

    def test_review_completion_comment_uses_run_scoped_artifact_reader(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """PR comments must not read review-report paths outside the artifact policy."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
            review_artifact_reader=ManifestReviewArtifactReader(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        worktree = worktree_with_completion(record)
        review_exchange_run = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260218-030045Z__review-exchange-123"
        )
        stray_report = review_exchange_run / "review-report.md"
        stray_report.parent.mkdir(parents=True, exist_ok=True)
        stray_report.write_text(
            "# Stray Review Report\n\nDo not include.\n", encoding="utf-8"
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"],
                status="ok",
                rounds=1,
                reason="reviewer_ok",
                summary={
                    "artifacts": [
                        {
                            "type": "review_report",
                            "label": "Review report",
                            "value": str(stray_report),
                            "render_mode": "markdown",
                        }
                    ]
                },
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        review_comment = mock_pr_adapter.add_comment.call_args.args[1]
        assert "✅ Review completed via via-local-loop loop." in review_comment
        assert "# Stray Review Report" not in review_comment
        assert "Do not include." not in review_comment

    def test_review_completion_comment_includes_cumulative_exchange_transcript(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """The final PR comment should answer what happened in each exchange turn."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
            review_artifact_reader=ManifestReviewArtifactReader(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        worktree = worktree_with_completion(record)

        def run_review_exchange(**kw):
            exchange_run = kw["exchange_run"]
            turns_dir = exchange_run.assets.exchange_dir / "turns"
            turns_dir.mkdir(parents=True)
            round_1_report = turns_dir / "round-1-reviewer-attempt-1.review-report.md"
            round_1_report.write_text(
                "# Review Round 1\n\nF1 details.\n", encoding="utf-8"
            )
            (turns_dir / "round-1-reviewer-attempt-1.result.json").write_text(
                json.dumps(
                    {"kind": "changes_requested", "response_text": "Reviewer summary 1"}
                ),
                encoding="utf-8",
            )
            (turns_dir / "round-1-coder-attempt-1.result.json").write_text(
                json.dumps({"kind": "ok", "response_text": "Coder fixed F1."}),
                encoding="utf-8",
            )
            round_2_report = turns_dir / "round-2-reviewer-attempt-1.review-report.md"
            round_2_report.write_text(
                "# Review Round 2\n\nApproved final.\n", encoding="utf-8"
            )
            (turns_dir / "round-2-reviewer-attempt-1.result.json").write_text(
                json.dumps({"kind": "ok", "response_text": "Approved."}),
                encoding="utf-8",
            )
            return _review_exchange_outcome(
                exchange_run,
                status="ok",
                rounds=2,
                reason="reviewer_ok",
                summary={
                    "artifacts": [
                        {
                            "type": "review_report",
                            "label": "Review report",
                            "value": str(round_2_report),
                            "render_mode": "markdown",
                        }
                    ]
                },
            )

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=run_review_exchange
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        review_comment = mock_pr_adapter.add_comment.call_args.args[1]
        assert "## Review Exchange Transcript" in review_comment
        assert review_comment.index("### Round 1 Reviewer") < review_comment.index(
            "### Round 1 Coder"
        )
        assert review_comment.index("### Round 1 Coder") < review_comment.index(
            "### Round 2 Reviewer"
        )
        assert "# Review Round 1" in review_comment
        assert "F1 details." in review_comment
        assert "Coder fixed F1." in review_comment
        assert "# Review Round 2" in review_comment
        assert "Approved final." in review_comment

    def test_review_completion_comment_truncates_cumulative_exchange_transcript(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """The generated PR comment must stay under GitHub's body limit."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
            review_artifact_reader=ManifestReviewArtifactReader(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        worktree = worktree_with_completion(record)

        def run_review_exchange(**kw):
            exchange_run = kw["exchange_run"]
            turns_dir = exchange_run.assets.exchange_dir / "turns"
            turns_dir.mkdir(parents=True)
            report = turns_dir / "round-1-reviewer-attempt-1.review-report.md"
            report.write_text("# Review Round 1\n\n" + ("x" * 80_000), encoding="utf-8")
            (turns_dir / "round-1-reviewer-attempt-1.result.json").write_text(
                json.dumps({"kind": "ok", "response_text": "Approved."}),
                encoding="utf-8",
            )
            return _review_exchange_outcome(
                exchange_run,
                status="ok",
                rounds=1,
                reason="reviewer_ok",
                summary={
                    "artifacts": [
                        {
                            "type": "review_report",
                            "label": "Review report",
                            "value": str(report),
                            "render_mode": "markdown",
                        }
                    ]
                },
            )

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=run_review_exchange
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        review_comment = mock_pr_adapter.add_comment.call_args.args[1]
        assert len(review_comment) <= GITHUB_COMMENT_BODY_LIMIT
        assert "Review exchange transcript truncated" in review_comment
        assert "Full per-turn artifacts remain" in review_comment

    def test_local_loop_failure_emits_review_changes_requested_trace_event(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """Local-loop halt should publish review.started then review.changes_requested."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        worktree = worktree_with_completion(record)
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="error", rounds=1, reason="boom"
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is False
        assert result.review_exchange_halted is True
        assert result.pr_url is None
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()
        event_names = sink.event_names()
        assert str(EventName.REVIEW_STARTED) in event_names
        assert str(EventName.REVIEW_CHANGES_REQUESTED) in event_names
        assert event_names.index(str(EventName.REVIEW_STARTED)) < event_names.index(
            str(EventName.REVIEW_CHANGES_REQUESTED)
        )
        review_started = sink.last_event(str(EventName.REVIEW_STARTED))
        review_changes = sink.last_event(str(EventName.REVIEW_CHANGES_REQUESTED))
        assert review_started is not None
        assert review_changes is not None
        exchange_run = processor._run_review_exchange_loop.call_args.kwargs[  # noqa: SLF001
            "exchange_run"
        ]
        assert review_started.data.get("run_dir") == str(exchange_run.assets.run_dir)
        assert review_changes.data.get("run_dir") == str(exchange_run.assets.run_dir)

    def test_exchange_uses_cached_summary_after_restart(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260201-000000Z__review-exchange-123"
        )
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "summary.json").write_text(
            json.dumps(
                {
                    "completed_rounds": 2,
                    "status": "ok",
                    "reason": "reviewer_ok",
                    "response_text": "Looks good",
                    "timestamp": "2026-02-01T00:00:00Z",
                }
            )
        )
        validation_record = run_dir / "validation-record.json"
        validation_record.write_text(
            json.dumps({"passed": True, "head_sha": "same-sha"})
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            validation_record_path=str(validation_record),
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=AssertionError("exchange should not re-run")
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is True
        assert result.review_exchange_completed is True
        # Cache-replay must be tagged so the timeline narrates it as a
        # replay rather than claiming a fresh 2-round review happened
        # in this run (issue #228 regression).
        review_started = sink.last_event(str(EventName.REVIEW_STARTED))
        review_approved = sink.last_event(str(EventName.REVIEW_APPROVED))
        assert review_started is not None
        assert review_approved is not None
        assert review_started.data.get("cached") is True
        assert review_approved.data.get("cached") is True

    def test_cached_exchange_non_ok_status_emits_cached_changes_requested(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        # Symmetric to test_exchange_uses_cached_summary_after_restart but for
        # the non-ok branch: if a prior run persisted a changes_requested
        # outcome, the replay must also be tagged cached=True so the timeline
        # narrates it as a replay rather than a fresh reviewer verdict.
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260201-000000Z__review-exchange-123"
        )
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        # Use a real production halt status — coder_protocol_error
        # — instead of the non-production "changes_requested" the
        # legacy test used. The state-machine refactor (PR #6271)
        # only recognizes statuses the runner actually writes
        # (ok / stopped / error). Both old and new implementations
        # halt on this cache hit; the test's intent (cached non-OK
        # → replay marker, no fresh exchange) is preserved.
        (exchange_dir / "summary.json").write_text(
            json.dumps(
                {
                    "completed_rounds": 3,
                    "status": "error",
                    "reason": "coder_protocol_error",
                    "response_text": "Still three open comments.",
                    "timestamp": "2026-02-01T00:00:00Z",
                    "head_sha": "same-sha",
                    "validation_passed": True,
                }
            )
        )
        validation_record = run_dir / "validation-record.json"
        validation_record.write_text(
            json.dumps({"passed": True, "head_sha": "same-sha"})
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            validation_record_path=str(validation_record),
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=AssertionError("exchange should not re-run on cache hit")
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is False
        review_started = sink.last_event(str(EventName.REVIEW_STARTED))
        review_changes = sink.last_event(str(EventName.REVIEW_CHANGES_REQUESTED))
        assert review_started is not None
        assert review_changes is not None
        assert review_started.data.get("cached") is True
        assert review_changes.data.get("cached") is True
        # The summary string must surface the real failure cause
        # (``coder_protocol_error``) — not a literal ``cached_summary``
        # placeholder. Operators read this string to decide what's
        # broken; opaque "cached_summary" hides the actual reason.
        assert "coder_protocol_error" in review_changes.data.get("summary", "")
        # Fresh review.approved must not be emitted on the non-ok cache path.
        assert sink.last_event(str(EventName.REVIEW_APPROVED)) is None
        # The processor's recorded errors should also carry the real
        # reason so downstream halt messages and ticket-routing don't
        # collapse distinct failure modes onto one opaque token.
        assert any("coder_protocol_error" in err for err in result.errors), (
            result.errors
        )

    def test_cached_exchange_max_rounds_exceeded_preserves_real_reason(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        # Second matrix point: a cached ``max_rounds_exceeded`` halt
        # surfaces the real reason in the emitted summary and recorded
        # errors. The earlier ``coder_protocol_error`` test pins one
        # row of the ``REUSE_HALT`` matrix; this one pins a second
        # (different) row so a regression that re-introduces the
        # ``"cached_summary"`` overwrite breaks both tests, not one.
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260201-000000Z__review-exchange-123"
        )
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "summary.json").write_text(
            json.dumps(
                {
                    "completed_rounds": 5,
                    "status": "stopped",
                    "reason": "max_rounds_exceeded",
                    "response_text": "Hit round limit without convergence.",
                    "timestamp": "2026-02-01T00:00:00Z",
                    "head_sha": "same-sha",
                    "validation_passed": True,
                }
            )
        )
        validation_record = run_dir / "validation-record.json"
        validation_record.write_text(
            json.dumps({"passed": True, "head_sha": "same-sha"})
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            validation_record_path=str(validation_record),
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=AssertionError("exchange should not re-run on cache hit")
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is False
        review_changes = sink.last_event(str(EventName.REVIEW_CHANGES_REQUESTED))
        assert review_changes is not None
        assert review_changes.data.get("cached") is True
        assert "max_rounds_exceeded" in review_changes.data.get("summary", "")
        assert any("max_rounds_exceeded" in err for err in result.errors), result.errors

    def test_cached_exchange_requires_validation_record(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260201-000000Z__review-exchange-123"
        )
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "summary.json").write_text(
            json.dumps(
                {
                    "completed_rounds": 2,
                    "status": "ok",
                    "reason": "reviewer_ok",
                    "response_text": "Looks good",
                    "timestamp": "2026-02-01T00:00:00Z",
                }
            )
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="error", rounds=1, reason="no-validation"
            )
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is False
        assert result.errors

    def test_cached_exchange_uses_manifest_pointer(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        config = self._make_config(tmp_path)
        session_output = FileSystemSessionOutput()
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        session_name = "20260201-000000Z__review-exchange-123"
        issue_run_dir = session_output.ensure_run_dir(worktree, session_name)
        exchange_run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260201-000001Z__review-exchange-123-r1"
        )
        exchange_dir = exchange_run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "summary.json").write_text(
            json.dumps(
                {
                    "completed_rounds": 2,
                    "status": "ok",
                    "reason": "reviewer_ok",
                    "response_text": "Looks good",
                    "timestamp": "2026-02-01T00:00:00Z",
                }
            )
        )
        validation_record = exchange_run_dir / "validation-record.json"
        validation_record.write_text(
            json.dumps({"passed": True, "head_sha": "same-sha"})
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            validation_record_path=str(validation_record),
        )
        session_output.update_manifest(
            issue_run_dir,
            {"review_exchange_dir": str(exchange_dir)},
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=AssertionError("exchange should not re-run")
        )

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is True
        assert result.review_exchange_completed is True

    def test_auto_mode_falls_back_to_local_loop(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "auto"
        processor = self._make_processor(config)

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: False,
        )

        assert processor._resolve_review_exchange_mode("agent:coder") == "via-local-loop"  # noqa: SLF001

    def test_auto_mode_without_agent_label_returns_none(self, tmp_path):
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "auto"
        processor = self._make_processor(config)

        assert processor._resolve_review_exchange_mode(None) is None  # noqa: SLF001

    def test_via_mcp_requires_supported_pair(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-mcp"
        processor = self._make_processor(config)

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: False,
        )

        with pytest.raises(ValueError, match="supported ai_system pair"):
            processor._resolve_review_exchange_mode("agent:coder")  # noqa: SLF001

    def test_run_review_exchange_uses_default_reviewer(self, tmp_path):
        """Default reviewer resolution flows through the injected
        ``ReviewExchangeRunner`` port. Inject a capture stub instead of
        monkeypatching execution-layer symbols (the runner now owns the
        worktree + persistent-runner dispatch internally)."""
        config = self._make_config(tmp_path)
        captured: dict[str, object] = {}

        class _CaptureRunner:
            def run(self, **kwargs):
                captured["coder_label"] = kwargs["coder_label"]
                captured["reviewer_label"] = kwargs["reviewer_label"]
                captured["runtime_config"] = kwargs["runtime_config"]
                return _review_exchange_outcome(
                    kwargs["exchange_run"],
                    status="ok",
                    rounds=1,
                    reason="reviewer_ok",
                )

        processor = CompletionProcessor(
            label_adapter=Mock(spec=LabelAdapter),
            pr_adapter=Mock(spec=PRAdapter),
            git_adapter=Mock(spec=GitAdapter),
            session_output=FileSystemSessionOutput(),
            review_exchange_runner=_CaptureRunner(),
            event_bus=EventBus(),
            label_config={},
            config=config,
            # This test drives a real exchange, so it reaches the
            # callback endpoint and must inject one.
            agent_callback_endpoint=ready_callback_endpoint(),
        )

        exchange_run = ReviewExchangeRun(
            session_name="review-exchange-1",
            run_id="review-run-1",
            parent_session_name="session-1",
            assets=ReviewExchangeRunAssets.from_run_dir(
                tmp_path / ".issue-orchestrator" / "sessions" / "review-run-1"
            ),
        )
        processor._run_review_exchange_loop(  # noqa: SLF001
            exchange_run=exchange_run,
            worktree=tmp_path,
            issue_number=1,
            issue_title="Test",
            session_name="session-1",
            agent_label="agent:coder",
        )

        assert captured["coder_label"] == "agent:coder"
        assert captured["reviewer_label"] == "agent:reviewer"
        assert config.config_path is not None
        assert captured["runtime_config"] == RuntimeConfigReference(
            config_path=config.config_path.resolve(),
            selection=config.launch_selection,
        )

    def test_resolve_agent_label_from_completion_path(self, tmp_path):
        coder_prompt = tmp_path / "coder.md"
        coder_prompt.write_text("Coder prompt")
        config = Config()
        config.agents = {
            "agent:backend": AgentConfig(
                prompt_path=coder_prompt, ai_system="claude-code"
            )
        }
        processor = self._make_processor(config)

        label, error = processor._resolve_agent_label_from_completion_path(  # noqa: SLF001
            ".issue-orchestrator/sessions/issue-123/completion-agent_backend.json"
        )

        assert error is None
        assert label == "agent:backend"

    def test_blocked_outcome_adds_blocked_label(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Blocked outcome should add the blocked label."""
        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[
                RequestedAction.ADD_BLOCKED_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="Blocked on dependency",
            blocked_reason="Waiting for API access",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert result.success
        mock_label_adapter.add_label.assert_called_once_with(123, "blocked")

    def test_needs_human_outcome_adds_needs_human_label(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Needs-human outcome should add the needs-human label."""
        record = make_record(
            outcome=CompletionOutcome.NEEDS_HUMAN,
            requested_actions=[
                RequestedAction.ADD_NEEDS_HUMAN_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="Need clarification",
            question="Should we use Redis or Memcached?",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert result.success
        mock_label_adapter.add_label.assert_called_once_with(123, "needs-human")

    def test_review_approved_adds_code_reviewed_removes_review_label(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Approved review should add code-reviewed and remove needs-code-review."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_APPROVED,
            requested_actions=[
                RequestedAction.ADD_CODE_REVIEWED_LABEL,
                RequestedAction.REMOVE_NEEDS_REWORK_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="LGTM",
            review_summary="Code looks good",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=42,
            issue_title="PR Title",
        )

        assert result.success
        mock_label_adapter.add_label.assert_called_once_with(42, "code-reviewed")
        mock_label_adapter.remove_label.assert_has_calls(
            [call(42, "needs-rework"), call(42, "needs-code-review")]
        )

    def test_review_changes_requested_adds_needs_rework_removes_review_label(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Changes requested should add needs-rework and remove needs-code-review."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
            requested_actions=[
                RequestedAction.ADD_NEEDS_REWORK_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="Need fixes",
            review_issues="Missing error handling",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=42,
            issue_title="PR Title",
        )

        assert result.success
        mock_label_adapter.add_label.assert_called_once_with(42, "needs-rework")
        mock_label_adapter.remove_label.assert_called_once_with(42, "needs-code-review")

    def test_review_changes_requested_writes_feedback_file(
        self, processor, worktree_with_completion
    ):
        """Changes requested with review_issues should write feedback file to run dir."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
            requested_actions=[
                RequestedAction.ADD_NEEDS_REWORK_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            ],
            summary="Need fixes",
            review_issues="Missing error handling and unit tests",
        )
        worktree = worktree_with_completion(record)
        run_assets = make_session_run_assets(worktree)

        # Process with pr_number to indicate review session
        result = processor.process(
            worktree,
            run_assets=run_assets,
            issue_number=42,
            issue_title="Fix bug",
            pr_number=456,
        )

        assert result.success
        feedback_file = run_assets.run_dir / "reviewer-feedback.json"
        assert feedback_file.exists(), "Feedback file should be written"
        # Verify content
        feedback_data = json.loads(feedback_file.read_text())
        assert feedback_data["pr_number"] == 456
        assert feedback_data["review_issues"] == "Missing error handling and unit tests"
        assert "timestamp" in feedback_data

    def test_review_without_issues_does_not_write_feedback_file(
        self, processor, worktree_with_completion
    ):
        """Review without review_issues should not write feedback file."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_APPROVED,
            requested_actions=[
                RequestedAction.ADD_CODE_REVIEWED_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            ],
            summary="Looks good",
            review_issues=None,  # No issues
        )
        worktree = worktree_with_completion(record)
        run_assets = make_session_run_assets(worktree)

        result = processor.process(
            worktree,
            run_assets=run_assets,
            issue_number=42,
            issue_title="Fix bug",
            pr_number=456,
        )

        assert result.success
        assert not (run_assets.run_dir / "reviewer-feedback.json").exists()


class TestCompletionProcessorPRActions:
    """Tests for PR-related actions from completion records."""

    def test_create_pr_action_calls_adapter(
        self, processor, mock_pr_adapter, worktree_with_completion
    ):
        """CREATE_PR action should create a PR via adapter."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Add feature",
        )

        assert result.success
        assert result.pr_url == "https://github.com/owner/repo/pull/42"
        mock_pr_adapter.create_pr.assert_called_once()
        call_args = mock_pr_adapter.create_pr.call_args
        assert call_args.kwargs["title"] == "#123: Add feature"
        assert call_args.kwargs["head"] == "issue-123"
        assert call_args.kwargs["draft"] is True

    def test_push_failure_halts_pr_creation(
        self, processor, mock_git_adapter, mock_pr_adapter, worktree_with_completion
    ):
        """Push failure should stop later CREATE_PR actions."""
        mock_git_adapter.push.return_value = PushResult(
            success=False,
            branch="issue-123",
            remote="origin",
            message="pre-push hook failed",
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Add feature",
        )

        assert not result.success
        assert any("Push failed" in err for err in result.errors)
        mock_pr_adapter.create_pr.assert_not_called()

    def test_create_pr_with_labels_applies_labels_to_pr(
        self, processor, mock_pr_adapter, mock_label_adapter, worktree_with_completion
    ):
        """PR labels from completion record should be applied to the created PR.

        This is critical for e2e test cleanup - PRs must be labeled so they
        can be identified and cleaned up by the test fixture.
        """
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
            pr_labels=["test-data", "e2e-test"],  # Labels to apply to PR
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Add feature",
        )

        assert result.success
        # PR was created with number 42 (from mock)
        assert result.pr_url == "https://github.com/owner/repo/pull/42"
        # Labels should be applied to the PR (number 42), not the issue (123)
        label_calls = mock_label_adapter.add_label.call_args_list
        assert len(label_calls) == 2
        assert label_calls[0] == ((42, "test-data"),)
        assert label_calls[1] == ((42, "e2e-test"),)

    def test_create_pr_without_labels_does_not_add_labels(
        self, processor, mock_pr_adapter, mock_label_adapter, worktree_with_completion
    ):
        """PR creation without pr_labels should not call add_label."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
            # No pr_labels field
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Add feature",
        )

        assert result.success
        # No labels should be added (no add_label calls)
        mock_label_adapter.add_label.assert_not_called()

    def test_post_comment_action_with_body(
        self, processor, mock_pr_adapter, worktree_with_completion
    ):
        """POST_COMMENT action should post comment via adapter."""
        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[
                RequestedAction.ADD_BLOCKED_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="Blocked",
            blocked_reason="API unavailable",
            comment_body="## Blocked\n\nWaiting for API access.",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test Issue",
        )

        assert result.success
        mock_pr_adapter.add_comment.assert_called_once_with(
            123, "## Blocked\n\nWaiting for API access."
        )


class TestTechLeadCompletionEffects:
    """Tech Lead completion effects (#6768 B1 / ADR-0031).

    Tech Lead prompts promise the orchestrator posts no comments; a clean audit
    (zero commits) must complete as success rather than publish-failure.
    Non-tech-lead sessions keep the pre-existing behavior on both counts.
    """

    NO_COMMITS_ERROR = RuntimeError(
        "Validation Failed: No commits between main and issue-123"
    )

    def _make_processor(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        *,
        review_exchange_runner=None,
        tech_lead_authority=None,
    ) -> CompletionProcessor:
        prompt = tmp_path / "tech-lead.md"
        prompt.write_text("Tech Lead prompt")
        config = Config()
        config.repo_root = tmp_path  # authority store home
        config.tech_lead_review_agent = "agent:tech-lead"
        config.agents = {
            "agent:tech-lead": AgentConfig(prompt_path=prompt),
            "agent:coder": AgentConfig(prompt_path=prompt),
        }
        if review_exchange_runner is not None:
            config.review_enabled = True
            config.review_exchange_mode = "via-local-loop"
            config.review_exchange_require_validation = False
            config.code_review_agent = "agent:reviewer"
            config.config_path = _write_test_config(tmp_path)
            config.agents["agent:reviewer"] = AgentConfig(prompt_path=prompt)
        mock_git_adapter.default_branch.return_value = "main"
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )

        if tech_lead_authority is None:
            tech_lead_authority = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
        return CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            review_exchange_runner=review_exchange_runner,
            event_bus=event_bus,
            label_config={},
            config=config,
            tech_lead_authority=tech_lead_authority,
        )

    @staticmethod
    def _completed_record() -> CompletionRecord:
        return make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
                RequestedAction.POST_COMMENT,
            ],
            summary="Audited PRs",
            implementation="Audited 3 PRs, no concerns",
            comment_body="## Implementation\n\nAudited 3 PRs.",
        )

    def _process(self, processor, worktree, *, agent_label: str, run_assets=None):
        return processor.process(
            worktree,
            run_assets=run_assets or make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Batch Review",
            agent_label=agent_label,
        )

    def _armed_run_assets(self, authority_store, worktree):
        """Run assets with launch authority + valid empty-audit pair."""
        run_assets = make_session_run_assets(worktree)
        self._record_launch_authority(authority_store, run_assets)
        self._plant_valid_pair(run_assets.run_dir)
        return run_assets

    def test_clean_tech_lead_audit_completes_without_publish_failure(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        tech_lead_authority_store,
        worktree_with_completion,
    ):
        """No-change audit: NoCommitsBetweenError is success, no labels/comment."""
        processor = self._make_processor(
            tmp_path,
            mock_label_adapter,
            mock_pr_adapter,
            mock_git_adapter,
            event_bus,
            tech_lead_authority=tech_lead_authority_store,
        )
        processor._emit_publish_failed = MagicMock()  # noqa: SLF001
        mock_pr_adapter.create_pr.side_effect = self.NO_COMMITS_ERROR
        worktree = worktree_with_completion(self._completed_record())
        run_assets = self._armed_run_assets(tech_lead_authority_store, worktree)

        result = self._process(
            processor, worktree, agent_label="agent:tech-lead", run_assets=run_assets
        )

        assert result.success is True
        assert not result.errors
        processor._emit_publish_failed.assert_not_called()  # noqa: SLF001
        # No comment: neither the requested one nor a failure diagnostic.
        mock_pr_adapter.add_comment.assert_not_called()

    def test_changed_tech_lead_audit_publishes_pr_but_posts_no_comment(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        tech_lead_authority_store,
        worktree_with_completion,
    ):
        """Changed audit: PR is created, but the completion comment is dropped."""
        processor = self._make_processor(
            tmp_path,
            mock_label_adapter,
            mock_pr_adapter,
            mock_git_adapter,
            event_bus,
            tech_lead_authority=tech_lead_authority_store,
        )
        worktree = worktree_with_completion(self._completed_record())
        run_assets = self._armed_run_assets(tech_lead_authority_store, worktree)

        result = self._process(
            processor, worktree, agent_label="agent:tech-lead", run_assets=run_assets
        )

        assert result.success is True
        assert result.pr_url == "https://github.com/owner/repo/pull/42"
        mock_pr_adapter.create_pr.assert_called_once()
        mock_pr_adapter.add_comment.assert_not_called()

    def test_non_tech_lead_completion_still_posts_comment(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        """Control: a non-tech-lead session's requested comment still posts."""
        processor = self._make_processor(
            tmp_path, mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus
        )
        worktree = worktree_with_completion(self._completed_record())

        result = self._process(processor, worktree, agent_label="agent:coder")

        assert result.success is True
        mock_pr_adapter.add_comment.assert_called_once_with(
            123, "## Implementation\n\nAudited 3 PRs."
        )

    def test_non_tech_lead_no_commits_is_still_a_publish_failure(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        """Control: NoCommitsBetweenError stays critical for non-tech-lead sessions."""
        processor = self._make_processor(
            tmp_path, mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus
        )
        processor._emit_publish_failed = MagicMock()  # noqa: SLF001
        mock_pr_adapter.create_pr.side_effect = self.NO_COMMITS_ERROR
        worktree = worktree_with_completion(self._completed_record())

        result = self._process(processor, worktree, agent_label="agent:coder")

        assert result.success is False
        assert any("create_pr" in error for error in result.errors)
        processor._emit_publish_failed.assert_called_once()  # noqa: SLF001

    # --- #6761 finding 3 + re-review finding 1: processing-path validation --

    @staticmethod
    def _record_launch_authority(authority_store, run_assets):
        """Record launch authority + matching worktree assignment (empty batch)."""
        import json as _json

        from issue_orchestrator.domain.tech_lead_session import (
            TECH_LEAD_ASSIGNMENT_FILENAME,
            TechLeadAssignment,
            TechLeadLaunchAuthority,
            TechLeadSessionFlavor,
        )

        run_dir = run_assets.run_dir
        TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW).write(
            run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        )
        manifest_path = run_dir / "manifest.json"
        manifest = _json.loads(manifest_path.read_text())
        manifest["tech_lead_assignment"] = str(
            run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        )
        manifest_path.write_text(_json.dumps(manifest))
        authority_store.record(
            run_id=run_assets.run_id,
            session_name=run_assets.session_name,
            authority=TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.BATCH_REVIEW,
                anchor_issue_number=123,
            ),
        )

    @staticmethod
    def _plant_valid_pair(run_dir):
        import json as _json

        data_dir = run_dir / "tech-lead-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "tech-lead-decision.json").write_text(
            _json.dumps(
                {
                    "schema_version": 1,
                    "summary": "Clean audit.",
                    "findings": [],
                    "proposed_actions": [],
                }
            )
        )
        (data_dir / "tech-lead-report.md").write_text("# Report\n\nNothing found.\n")

    def test_tech_lead_completion_supplies_approval_gate_to_review_pipeline(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        tech_lead_authority_store,
        worktree_with_completion,
    ):
        """The completion producer classifies Tech Lead sessions and wires a gate."""
        from issue_orchestrator.control.tech_lead_approval_gate import (
            TechLeadDecisionApprovalGate,
        )
        review_runner = _CapturingReviewExchangeRunner()
        processor = self._make_processor(
            tmp_path,
            mock_label_adapter,
            mock_pr_adapter,
            mock_git_adapter,
            event_bus,
            review_exchange_runner=review_runner,
            tech_lead_authority=tech_lead_authority_store,
        )
        worktree = worktree_with_completion(self._completed_record())
        run_assets = make_session_run_assets(worktree)
        self._record_launch_authority(tech_lead_authority_store, run_assets)
        self._plant_valid_pair(run_assets.run_dir)

        result = self._process(
            processor,
            worktree,
            agent_label="agent:tech-lead",
            run_assets=run_assets,
        )

        assert result.success is True
        assert len(review_runner.calls) == 1
        gate = review_runner.calls[0]["approval_gate"]
        assert isinstance(gate, TechLeadDecisionApprovalGate)
        assert gate.rejection_reason() is None

    def test_ordinary_completion_supplies_no_approval_gate(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        """The completion producer must not attach Tech Lead policy to other agents."""
        review_runner = _CapturingReviewExchangeRunner()
        processor = self._make_processor(
            tmp_path,
            mock_label_adapter,
            mock_pr_adapter,
            mock_git_adapter,
            event_bus,
            review_exchange_runner=review_runner,
        )
        worktree = worktree_with_completion(self._completed_record())

        result = self._process(processor, worktree, agent_label="agent:coder")

        assert result.success is True
        assert len(review_runner.calls) == 1
        assert review_runner.calls[0]["approval_gate"] is None

    def test_completed_tech_lead_session_without_pair_records_critical_error(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        tech_lead_authority_store,
        worktree_with_completion,
    ):
        """Missing/invalid pair rejects the completion in the PRE-ACTION
        phase (#6769 finding 1): failed result, tagged critical error, and
        ZERO push/PR/comment calls — the agent's requested publish never
        executes."""
        from issue_orchestrator.control.completion_types import (
            ERROR_PREFIX_TECH_LEAD_DECISION,
        )
        processor = self._make_processor(
            tmp_path,
            mock_label_adapter,
            mock_pr_adapter,
            mock_git_adapter,
            event_bus,
            tech_lead_authority=tech_lead_authority_store,
        )
        worktree = worktree_with_completion(self._completed_record())
        run_assets = make_session_run_assets(worktree)
        self._record_launch_authority(tech_lead_authority_store, run_assets)

        result = processor.process(
            worktree,
            run_assets=run_assets,
            issue_number=123,
            issue_title="Batch Review",
            agent_label="agent:tech-lead",
        )

        assert result.success is False
        assert any(
            error.startswith(f"{ERROR_PREFIX_TECH_LEAD_DECISION}: missing_decision")
            for error in result.errors
        )
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()
        mock_pr_adapter.add_comment.assert_not_called()

    def test_completed_tech_lead_session_without_launch_authority_is_critical(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        """No orchestrator authority record => the completion is rejected
        BEFORE any requested action executes (#6769 finding 1): the reviewer
        reproduced a success=True result with one real push and one real PR
        creation; both must now be zero."""
        from issue_orchestrator.control.completion_types import (
            ERROR_PREFIX_TECH_LEAD_AUTHORITY,
        )

        processor = self._make_processor(
            tmp_path, mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus
        )
        worktree = worktree_with_completion(self._completed_record())
        run_assets = make_session_run_assets(worktree)
        self._plant_valid_pair(run_assets.run_dir)

        result = processor.process(
            worktree,
            run_assets=run_assets,
            issue_number=123,
            issue_title="Batch Review",
            agent_label="agent:tech-lead",
        )

        assert result.success is False
        assert any(
            error.startswith(f"{ERROR_PREFIX_TECH_LEAD_AUTHORITY}: missing_authority")
            for error in result.errors
        )
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()
        mock_pr_adapter.add_comment.assert_not_called()

    def test_tampered_assignment_rejects_before_any_action(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        tech_lead_authority_store,
        worktree_with_completion,
    ):
        """A worktree assignment copy that no longer mirrors the recorded
        launch authority is tamper evidence (#6761 rr F1): rejected in the
        pre-action phase with zero push/PR/comment calls (#6769 finding 1)."""
        from issue_orchestrator.control.completion_types import (
            ERROR_PREFIX_TECH_LEAD_AUTHORITY,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            TECH_LEAD_ASSIGNMENT_FILENAME,
            TechLeadAssignment,
            TechLeadSessionFlavor,
        )
        processor = self._make_processor(
            tmp_path,
            mock_label_adapter,
            mock_pr_adapter,
            mock_git_adapter,
            event_bus,
            tech_lead_authority=tech_lead_authority_store,
        )
        worktree = worktree_with_completion(self._completed_record())
        run_assets = self._armed_run_assets(tech_lead_authority_store, worktree)
        # Agent flips its copy from batch review to a focused investigation.
        TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=999,
        ).write(run_assets.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME)

        result = processor.process(
            worktree,
            run_assets=run_assets,
            issue_number=123,
            issue_title="Batch Review",
            agent_label="agent:tech-lead",
        )

        assert result.success is False
        assert any(
            error.startswith(f"{ERROR_PREFIX_TECH_LEAD_AUTHORITY}: scope_tampered")
            for error in result.errors
        )
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()
        mock_pr_adapter.add_comment.assert_not_called()

    def test_completed_tech_lead_session_with_valid_pair_has_no_tech_lead_error(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        tech_lead_authority_store,
        worktree_with_completion,
    ):
        from issue_orchestrator.control.completion_types import (
            ERROR_PREFIX_TECH_LEAD_DECISION,
        )
        processor = self._make_processor(
            tmp_path,
            mock_label_adapter,
            mock_pr_adapter,
            mock_git_adapter,
            event_bus,
            tech_lead_authority=tech_lead_authority_store,
        )
        worktree = worktree_with_completion(self._completed_record())
        run_assets = make_session_run_assets(worktree)
        self._record_launch_authority(tech_lead_authority_store, run_assets)
        self._plant_valid_pair(run_assets.run_dir)

        result = processor.process(
            worktree,
            run_assets=run_assets,
            issue_number=123,
            issue_title="Batch Review",
            agent_label="agent:tech-lead",
        )

        assert not result.errors


class TestCompletionProcessorGitActions:
    """Tests for git-related actions from completion records."""

    def test_push_branch_action_calls_adapter(
        self, processor, mock_git_adapter, worktree_with_completion, monkeypatch
    ):
        """PUSH_BRANCH action should push via adapter."""
        monkeypatch.delenv("E2E_SKIP_PUSH_HOOKS", raising=False)
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_git_adapter.push.assert_called_once_with(worktree, skip_hooks=False)

    def test_push_failure_is_recorded(
        self, processor, mock_git_adapter, mock_pr_adapter, worktree_with_completion
    ):
        """Failed push should be recorded in result."""
        mock_git_adapter.push.return_value = PushResult(
            success=False,
            branch="issue-123",
            remote="origin",
            message="Remote rejected",
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert any("Push failed" in err for err in result.errors)
        mock_pr_adapter.add_comment.assert_called_once()
        comment = mock_pr_adapter.add_comment.call_args[0][1]
        assert "Orchestrator Processing Failed" in comment
        assert "Push failed" in comment

    def test_push_failure_emits_publish_failed_event(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """On push failure a publish.failed trace event carries the real error."""
        mock_git_adapter.push.return_value = PushResult(
            success=False,
            branch="issue-123",
            remote="origin",
            message="git command timed out: pre-push hook stuck",
            retryable=False,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        publish_failed = [
            event
            for event in sink.events
            if str(event.name) == str(EventName.PUBLISH_FAILED)
        ]
        assert len(publish_failed) == 1
        payload = publish_failed[0].data
        assert payload["stage"] == "push_branch"
        assert "pre-push hook stuck" in payload["error"]
        assert payload["branch"] == "issue-123"
        assert payload["issue_number"] == 123

    def test_push_non_fast_forward_retries_after_rebase(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """Non-fast-forward push should retry after rebase."""
        mock_git_adapter.push.side_effect = [
            PushResult(
                success=False,
                branch="issue-123",
                remote="origin",
                message="non-fast-forward",
            ),
            PushResult(
                success=True,
                branch="issue-123",
                remote="origin",
                message="Pushed",
            ),
        ]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_git_adapter.rebase_on_branch.assert_called_once_with(
            worktree, "origin/main"
        )
        assert mock_git_adapter.push.call_count == 2

    def test_push_retry_rebases_stack_successor_on_predecessor(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """F2: a stack successor's non-ff push retry rebases on the predecessor
        branch via the stack work gate, not the default base."""
        from issue_orchestrator.control.stack_publish_gate import StackPublishDecision

        processor.attach_stack_publish_gate(_FakeStackGate(
            StackPublishDecision.not_stack(),
            work_decision=StackPublishDecision(
                is_stack=True, allowed=True, base_branch="20-base"
            ),
        ))
        mock_git_adapter.push.side_effect = [
            PushResult(success=False, branch="issue-123", remote="origin", message="non-fast-forward"),
            PushResult(success=True, branch="issue-123", remote="origin", message="Pushed"),
        ]
        worktree = worktree_with_completion(make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
        ))

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_git_adapter.rebase_on_branch.assert_called_once_with(worktree, "origin/20-base")
        assert mock_git_adapter.push.call_count == 2

    def test_push_retry_fails_closed_when_stack_gate_blocked(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """F2: a blocked/ambiguous stack gate halts the non-ff push retry — no
        default-base rebase and no second push onto the wrong shape."""
        from issue_orchestrator.control.stack_publish_gate import StackPublishDecision

        processor.attach_stack_publish_gate(_FakeStackGate(
            StackPublishDecision.not_stack(),
            work_decision=StackPublishDecision.blocked(
                "Stack work gate blocked: work: blocked (ambiguous_stack_base)",
                retryable=False,
            ),
        ))
        mock_git_adapter.push.side_effect = [
            PushResult(success=False, branch="issue-123", remote="origin", message="non-fast-forward"),
            PushResult(success=True, branch="issue-123", remote="origin", message="Pushed"),
        ]
        worktree = worktree_with_completion(make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
        ))

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        mock_git_adapter.rebase_on_branch.assert_not_called()
        assert mock_git_adapter.push.call_count == 1
        assert any("ambiguous_stack_base" in e for e in result.errors)


class TestCompletionProcessorValidation:
    """Tests for validation logic."""

    def test_no_completion_record_returns_failure(self, processor, tmp_path):
        """Missing completion record should return failure."""
        worktree = tmp_path / "empty-worktree"
        worktree.mkdir()

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert "no completion record found" in result.message.lower()

    def test_invalid_json_returns_failure(self, processor, tmp_path):
        """Invalid JSON in completion record should return failure."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir()
        (record_dir / "completion.json").write_text("not valid json{")

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success

    def test_protected_branch_push_rejected(
        self, processor, mock_git_adapter, mock_pr_adapter, worktree_with_completion
    ):
        """Push to main branch should be rejected."""
        mock_git_adapter.get_current_branch.return_value = "main"
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert "protected branch" in result.message.lower()
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.add_comment.assert_called_once()
        comment = mock_pr_adapter.add_comment.call_args[0][1]
        assert "Orchestrator Processing Failed" in comment
        assert "protected branch" in comment.lower()


class TestCompletionProcessorEvents:
    """Tests for event emission during processing."""

    def test_successful_completion_emits_event(
        self, processor, event_bus, worktree_with_completion
    ):
        """Successful processing should emit completed event."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        # Subscribe to capture events
        events_received = []
        event_bus.subscribe(SessionEvent.COMPLETED, lambda e: events_received.append(e))

        processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert len(events_received) == 1
        assert events_received[0].entity_id == 123


class TestCompletionProcessorDirtyPolicy:
    """Tests for dirty-tree policy enforcement before push."""

    def test_push_rejected_when_tracked_dirty(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        config = Config()
        config.validation.publish.dirty_check = "tracked"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_tracked_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = ["src/feature.py", "README.md"]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert result.failure_kind == "validation_failed"
        assert "working tree is dirty" in result.message.lower()
        assert "dirty files: src/feature.py, readme.md." in result.message.lower()
        # Built from the remediation owner so the reason cannot drift from the
        # policy the agent-facing surfaces render.
        assert result.errors == [
            "Validation: "
            + blocked_reason("Override with validation.publish.dirty_check.")
            + " Dirty files: src/feature.py, README.md."
        ]
        assert NEVER_DESTROY_UNKNOWN in result.errors[0]
        mock_git_adapter.push.assert_not_called()
        mock_label_adapter.add_label.assert_called_once_with(123, "validation-failed")
        mock_pr_adapter.add_comment.assert_called_once()

    @pytest.mark.parametrize(
        "outcome,label",
        [
            (CompletionOutcome.BLOCKED, "blocked"),
            (CompletionOutcome.NEEDS_HUMAN, "needs_human"),
        ],
    )
    def test_escalation_is_routed_to_a_human_despite_a_dirty_tree(
        self,
        outcome,
        label,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        """The escalation must survive the orchestrator, not just the CLI.

        `coding-done blocked` is rung 3 of the remediation ladder: what an agent
        runs when it finds a dirty file it must not resolve on its own. Round 3
        made the CLI accept it. That is only half the path -- STATUS_TO_ACTIONS
        gives both escalation statuses PUSH_BRANCH, so the record then reached
        `validate_worktree_state`, which applied the dirty policy and rejected
        it. The dead end had moved one boundary downstream: the agent exited 0
        believing it had escalated, and the human was never told.
        """
        config = Config()
        config.validation.publish.dirty_check = "tracked"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={label: label},
            config=config,
        )
        mock_git_adapter.has_tracked_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = ["operator_notes.py"]
        action = (
            RequestedAction.ADD_BLOCKED_LABEL
            if outcome is CompletionOutcome.BLOCKED
            else RequestedAction.ADD_NEEDS_HUMAN_LABEL
        )
        record = make_record(
            outcome=outcome,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                action,
                RequestedAction.POST_COMMENT,
            ],
            summary="cannot classify dirty file operator_notes.py",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success, (
            "escalation must reach the human; got "
            f"{result.failure_kind}: {result.message}"
        )
        assert result.failure_kind != "validation_failed"
        # The human-routing action actually ran.
        mock_label_adapter.add_label.assert_any_call(123, label)

    def test_escalation_publishes_only_committed_content(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        """Accepting a dirty tree must not publish the uncommitted part of it.

        Pushing sends commits, so the preserved files -- which are by definition
        not in HEAD -- cannot ride along. This pins that the push is the only
        publishing step taken, and that nothing stages or commits the dirty
        files on the way.
        """
        config = Config()
        config.validation.publish.dirty_check = "tracked"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={"blocked": "blocked"},
            config=config,
        )
        mock_git_adapter.has_tracked_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = ["operator_notes.py"]
        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.ADD_BLOCKED_LABEL,
            ],
            summary="cannot classify dirty file operator_notes.py",
        )
        worktree = worktree_with_completion(record)

        processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        # Nothing may turn the preserved file into published content.
        for forbidden in ("stage_all", "add_all", "commit", "commit_all"):
            call = getattr(mock_git_adapter, forbidden, None)
            if call is not None and hasattr(call, "assert_not_called"):
                call.assert_not_called()

    def test_push_rejected_when_all_mode_and_untracked_present(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        config = Config()
        config.validation.publish.dirty_check = "all"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_uncommitted_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = ["tmp.out"]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert "working tree is dirty" in result.message.lower()
        mock_git_adapter.push.assert_not_called()

    def test_push_allows_when_all_mode_and_only_planted_untracked(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        # Reproduces the mode=all parity gap with the agent's coding-done
        # check. has_uncommitted_changes fires on planted-untracked paths,
        # but list_dirty_files filters them out (filter_orchestrator_untracked_planted),
        # leaving an empty list. The previous gate required dirty_files to be
        # non-empty before short-circuiting to pass, so this case fell through
        # to a confusing "Working tree is dirty" with no files listed.
        config = Config()
        config.validation.publish.dirty_check = "all"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_uncommitted_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = []
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_git_adapter.push.assert_called_once()

    def test_push_allows_runtime_only_dirty_files(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        config = Config()
        config.validation.publish.dirty_check = "tracked"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_tracked_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = [
            ".issue-orchestrator/session-latest.json"
        ]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_git_adapter.push.assert_called_once()

    def test_push_blocked_when_dirty_listing_reports_enumeration_failure(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        """list_dirty_files returning ``None`` signals an enumeration
        failure: we don't know whether the dirty entries are the safe
        planted/runtime kind or real blocking changes. The boolean
        ``has_*`` helpers fail closed by returning True on error;
        ``list_dirty_files`` must do the same, and the policy must NOT
        collapse ``None`` to ``[] -> pass`` (#6159 review feedback).
        """
        config = Config()
        config.validation.publish.dirty_check = "all"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_uncommitted_changes.return_value = True
        # The exact shape from the reviewer's repro: dirty=True from the
        # boolean check, list_dirty_files returns None (couldn't enumerate).
        mock_git_adapter.list_dirty_files.return_value = None
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        mock_git_adapter.push.assert_not_called()

    def test_push_allowed_when_dirty_check_off(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ):
        config = Config()
        config.validation.publish.dirty_check = "off"
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_tracked_changes.return_value = True
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_git_adapter.push.assert_called_once()

    def test_failed_processing_emits_failed_event(
        self, processor, event_bus, mock_git_adapter, worktree_with_completion
    ):
        """Failed processing should emit failed event."""
        mock_git_adapter.push.return_value = PushResult(
            success=False,
            branch="issue-123",
            remote="origin",
            message="Rejected",
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        events_received = []
        event_bus.subscribe(SessionEvent.FAILED, lambda e: events_received.append(e))

        processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert len(events_received) == 1


class TestCompletionProcessorAuditLogging:
    """Tests for audit logging of all actions."""

    def test_all_actions_logged(self, processor, worktree_with_completion, caplog):
        """All executed actions should be logged for audit."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_APPROVED,
            requested_actions=[
                RequestedAction.ADD_CODE_REVIEWED_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="LGTM",
            review_summary="Looks good",
            comment_body="Approved!",
        )
        worktree = worktree_with_completion(record)

        import logging

        with caplog.at_level(logging.INFO):
            processor.process(
                worktree,
                run_assets=make_session_run_assets(worktree),
                issue_number=42,
                issue_title="Test PR",
            )

        # Verify key actions are logged
        log_text = caplog.text
        assert "Executing action: add_code_reviewed_label" in log_text
        assert "Executing action: remove_code_review_label" in log_text
        assert "Processing completion for #42" in log_text

    def test_result_includes_actions_taken(self, processor, worktree_with_completion):
        """Result should list all actions taken for audit."""
        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[
                RequestedAction.ADD_BLOCKED_LABEL,
            ],
            summary="Blocked",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.actions_taken is not None
        assert any("blocked" in action.lower() for action in result.actions_taken)


class TestCompletionProcessorPublishGate:
    """Tests for publish gate validation before publishing.

    Key invariant: Cannot publish (push/PR) without validation passing.
    """

    @pytest.fixture
    def mock_publish_gate(self):
        """Mock PublishGate for testing."""
        from unittest.mock import Mock
        from issue_orchestrator.control.validation import PublishGateResult

        gate = Mock()
        gate.check = Mock(
            return_value=PublishGateResult(
                allowed=True,
                reason="Validation passed",
            )
        )
        return gate

    @pytest.fixture
    def processor_with_gate(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        """Processor with publish gate configured."""
        return CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
        )

    @pytest.fixture
    def mock_pre_publish_gate(self):
        gate = Mock()
        gate.check.return_value = PrePublishGateResult(
            allowed=True,
            reason="Pre-push hook passed",
            command="/tmp/hooks/pre-push",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            exit_code=0,
            stdout="",
            stderr="",
            hook_path="/tmp/hooks/pre-push",
            head_sha="abc123",
            ran=True,
        )
        return gate

    def test_cannot_publish_without_validation_passing(
        self,
        processor_with_gate,
        mock_publish_gate,
        mock_git_adapter,
        worktree_with_completion,
    ):
        """CRITICAL: Publish actions must be blocked when validation fails.

        This test proves the invariant: cannot publish without tests_passed.
        """
        from issue_orchestrator.control.validation import PublishGateResult

        # Configure gate to fail
        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=False,
            reason="Validation failed: pyright found 3 errors",
        )

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor_with_gate.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        # Processing must fail
        assert not result.success
        assert "validation failed" in result.message.lower()
        # Push must NOT have been called
        mock_git_adapter.push.assert_not_called()

    def test_publish_allowed_when_validation_passes(
        self,
        processor_with_gate,
        mock_publish_gate,
        mock_git_adapter,
        worktree_with_completion,
    ):
        """Publish actions proceed when validation passes."""
        from issue_orchestrator.control.validation import PublishGateResult

        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=True,
            reason="Validation passed",
        )

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor_with_gate.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_git_adapter.push.assert_called_once()

    def test_non_publish_actions_bypass_gate(
        self,
        processor_with_gate,
        mock_publish_gate,
        mock_label_adapter,
        worktree_with_completion,
    ):
        """Non-publish actions (labels, comments) don't require validation."""
        from issue_orchestrator.control.validation import PublishGateResult

        # Gate would fail if checked, but shouldn't be checked for label-only actions
        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=False,
            reason="Would fail",
        )

        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[RequestedAction.ADD_BLOCKED_LABEL],
            summary="Blocked",
        )
        worktree = worktree_with_completion(record)

        result = processor_with_gate.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        # Label actions should succeed without gate check
        assert result.success
        mock_label_adapter.add_label.assert_called_once()

    def test_validation_failed_label_added_on_gate_failure(
        self,
        processor_with_gate,
        mock_publish_gate,
        mock_label_adapter,
        mock_git_adapter,
        mock_pr_adapter,
        worktree_with_completion,
    ):
        """When validation fails, the validation-failed label should be added to the issue."""
        from issue_orchestrator.control.validation import PublishGateResult

        # Configure gate to fail
        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=False,
            reason="Validation failed: tests failed",
        )

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor_with_gate.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        # Processing must fail
        assert not result.success
        assert "validation failed" in result.message.lower()

        # validation-failed label must be added
        mock_label_adapter.add_label.assert_called_once_with(123, "validation-failed")
        mock_pr_adapter.add_comment.assert_called_once()
        comment = mock_pr_adapter.add_comment.call_args[0][1]
        assert "Validation Failed" in comment
        assert "Validation failed: tests failed" in comment

    def test_validation_failure_captured_in_session_output(
        self, processor_with_gate, mock_publish_gate, tmp_path
    ):
        """Validation failure output should be written into session output."""
        from issue_orchestrator.control.validation import (
            PublishGateResult,
            ValidationRecord,
            ValidationRecordStore,
        )
        from issue_orchestrator.domain.models import CompletionRecord
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir(parents=True, exist_ok=True)

        completion_record = CompletionRecord(
            session_id="issue-123",
            timestamp=datetime.now().isoformat(),
            outcome=CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        (record_dir / "completion.json").write_text(
            json.dumps(completion_record.to_dict())
        )

        session_output = FileSystemSessionOutput()
        run = session_output.start_run(worktree, "issue-123", issue_number=123)

        # Validation output is written directly to session output dir
        (run.run_dir / "validation-stdout.log").write_text("validation stdout")
        (run.run_dir / "validation-stderr.log").write_text("validation stderr")

        store = ValidationRecordStore(worktree)
        validation_record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123",
            passed=False,
            exit_code=1,
            command="make validate",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            timed_out=False,
            stdout_path=str(
                (run.run_dir / "validation-stdout.log").relative_to(worktree)
            ),
            stderr_path=str(
                (run.run_dir / "validation-stderr.log").relative_to(worktree)
            ),
        )
        store.write(validation_record)

        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=False,
            reason="Validation failed",
            record=validation_record,
        )

        result = processor_with_gate.process(
            worktree,
            run_assets=run,
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        run_dir = run.run_dir
        assert (run_dir / "validation-stdout.log").read_text() == "validation stdout"
        assert (run_dir / "validation-stderr.log").read_text() == "validation stderr"
        assert (run_dir / "validation-record.json").exists()
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest.get("validation_record_path") == str(
            run_dir / "validation-record.json"
        )
        # Manifest carries the typed validation outcome via the three
        # legacy flat fields. The publish-gate-failed path used to write
        # `validation_failure_reason` (an inconsistent typo'd field) —
        # the typed ValidationFailed outcome routes through the canonical
        # `validation_reason` field instead.
        assert manifest.get("validation_passed") is False
        assert manifest.get("validation_status") == "failed"
        assert manifest.get("validation_reason") == "Validation failed"
        assert "validation_failure_reason" not in manifest
        assert "ended_at" in manifest  # Must be set so UI shows correct status

    def test_pre_publish_gate_runs_before_push_and_keeps_hooks_enabled(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
        mock_pre_publish_gate,
        worktree_with_completion,
    ):
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            pre_publish_gate=mock_pre_publish_gate,
            session_output=FileSystemSessionOutput(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_pre_publish_gate.check.assert_called_once_with(worktree)
        mock_git_adapter.push.assert_called_once_with(worktree, skip_hooks=False)

    def test_pre_publish_gate_failure_adds_validation_failed_and_blocks_push(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
        worktree_with_completion,
    ):
        pre_publish_gate = Mock()
        pre_publish_gate.check.return_value = PrePublishGateResult(
            allowed=False,
            reason="ERROR: Test-skipping patterns detected",
            command="/tmp/hooks/pre-push",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            exit_code=1,
            stdout="ERROR: Test-skipping patterns detected\n",
            stderr="",
            hook_path="/tmp/hooks/pre-push",
            head_sha="abc123",
            ran=True,
        )
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            pre_publish_gate=pre_publish_gate,
            session_output=FileSystemSessionOutput(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert result.failure_kind == "validation_failed"
        mock_git_adapter.push.assert_not_called()
        mock_label_adapter.add_label.assert_called_once_with(123, "validation-failed")
        comment = mock_pr_adapter.add_comment.call_args[0][1]
        assert "Validation Failed" in comment
        assert "Test-skipping patterns detected" in comment

    def test_running_review_exchange_defers_before_publish_preconditions(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
        worktree_with_completion,
    ):
        mock_git_adapter.has_tracked_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = ["src/app.py"]
        # The background review-exchange job id is run-scoped (#6675):
        # issue:session_name:run_id. Bind the running job to this run's id.
        run_id = "20260603T000000000000Z"
        supervisor = BackgroundJobSupervisor(
            _RunningReviewExchangeJobRunner(
                {f"review-exchange:123:test-session:{run_id}"}
            )
        )
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            background_job_supervisor=supervisor,
            session_output=FileSystemSessionOutput(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree, run_id=run_id),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success is True
        assert result.review_exchange_deferred is True
        mock_git_adapter.has_tracked_changes.assert_not_called()
        mock_git_adapter.diff_against_base.assert_not_called()
        mock_publish_gate.check.assert_not_called()
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()
        mock_label_adapter.add_label.assert_not_called()

    def test_test_skip_guard_blocks_before_review_exchange_or_push(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
        worktree_with_completion,
    ):
        mock_git_adapter.diff_against_base.return_value = DiffResult(
            success=True,
            diff_text=(
                "diff --git a/src/test/kotlin/FooTest.kt b/src/test/kotlin/FooTest.kt\n"
                "--- a/src/test/kotlin/FooTest.kt\n"
                "+++ b/src/test/kotlin/FooTest.kt\n"
                "@@ -20,0 +21,1 @@\n"
                "+        assumeTrue(PostgresTestSupport.isAvailable())\n"
            ),
        )
        mock_git_adapter.read_branch_text_files.return_value = BranchTextFilesResult(
            success=True,
            files=(
                BranchTextFile(
                    path="src/test/kotlin/FooTest.kt",
                    content=(
                        "\n" * 20
                        + "        assumeTrue(PostgresTestSupport.isAvailable())\n"
                    ),
                ),
            ),
        )
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert result.failure_kind == "validation_failed"
        assert "Newly added test-skip guard" in result.message
        mock_git_adapter.read_branch_text_files.assert_called_once_with(
            worktree, ("src/test/kotlin/FooTest.kt",)
        )
        mock_publish_gate.check.assert_not_called()
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()
        mock_label_adapter.add_label.assert_called_once_with(123, "validation-failed")

    def test_test_skip_guard_uses_branch_tip_multiline_context(
        self,
        processor,
        mock_git_adapter,
        worktree_with_completion,
    ):
        mock_git_adapter.diff_against_base.return_value = DiffResult(
            success=True,
            diff_text=(
                "diff --git a/tests/test_guard.py b/tests/test_guard.py\n"
                "--- a/tests/test_guard.py\n"
                "+++ b/tests/test_guard.py\n"
                "@@ -10,0 +11,1 @@\n"
                '+pytest.skip("fixture text")\n'
            ),
        )
        mock_git_adapter.read_branch_text_files.return_value = BranchTextFilesResult(
            success=True,
            files=(
                BranchTextFile(
                    path="tests/test_guard.py",
                    content=(
                        "\n" * 9
                        + 'fixture = """documentation\n'
                        + 'pytest.skip("fixture text")\n'
                        + '"""\n'
                    ),
                ),
            ),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success
        mock_git_adapter.read_branch_text_files.assert_called_once_with(
            worktree, ("tests/test_guard.py",)
        )
        mock_git_adapter.push.assert_called_once()

    @pytest.mark.parametrize(
        ("branch_files_result", "expected_message"),
        [
            (
                BranchTextFilesResult(
                    success=False,
                    error="fatal: path missing from HEAD",
                ),
                "Could not read branch-tip test files",
            ),
            (
                BranchTextFilesResult(
                    success=True,
                    files=(
                        BranchTextFile(
                            path="tests/test_guard.py",
                            content='\npytest.skip("different text")\n',
                        ),
                    ),
                ),
                "Branch-tip content does not match diff",
            ),
        ],
    )
    def test_test_skip_guard_branch_tip_failure_fails_closed(
        self,
        processor,
        mock_git_adapter,
        worktree_with_completion,
        branch_files_result,
        expected_message,
    ):
        mock_git_adapter.diff_against_base.return_value = DiffResult(
            success=True,
            diff_text=(
                "diff --git a/tests/test_guard.py b/tests/test_guard.py\n"
                "--- a/tests/test_guard.py\n"
                "+++ b/tests/test_guard.py\n"
                "@@ -1,0 +2,1 @@\n"
                '+pytest.skip("real skip")\n'
            ),
        )
        mock_git_adapter.read_branch_text_files.return_value = branch_files_result
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert expected_message in result.message
        mock_git_adapter.push.assert_not_called()

    def test_test_skip_guard_unparsable_diff_fails_closed(
        self,
        processor,
        mock_git_adapter,
        worktree_with_completion,
    ):
        mock_git_adapter.diff_against_base.return_value = DiffResult(
            success=True,
            diff_text=(
                "diff --git a/tests/test_guard.py b/tests/test_guard.py\n"
                "--- a/tests/test_guard.py\n"
                "+++ b/tests/test_guard.py\n"
                "@@@ -1,1 -1,1 +1,1 @@@\n"
                '+pytest.skip("real skip")\n'
            ),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert "Could not parse branch diff for banned test skips" in result.message
        mock_git_adapter.read_branch_text_files.assert_not_called()
        mock_git_adapter.push.assert_not_called()

    def test_test_skip_guard_scans_source_resembling_diff_headers(
        self,
        processor,
        mock_git_adapter,
        worktree_with_completion,
    ):
        mock_git_adapter.diff_against_base.return_value = DiffResult(
            success=True,
            diff_text=(
                "diff --git a/tests/test_guard.py b/tests/test_guard.py\n"
                "--- a/tests/test_guard.py\n"
                "+++ b/tests/test_guard.py\n"
                "@@ -0,0 +1,1 @@\n"
                '+++pytest.skip("real skip")\n'
            ),
        )
        mock_git_adapter.read_branch_text_files.return_value = BranchTextFilesResult(
            success=True,
            files=(
                BranchTextFile(
                    path="tests/test_guard.py",
                    content='++pytest.skip("real skip")\n',
                ),
            ),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree,
            run_assets=make_session_run_assets(worktree),
            issue_number=123,
            issue_title="Test",
        )

        assert not result.success
        assert "Newly added test-skip guard" in result.message
        mock_git_adapter.read_branch_text_files.assert_called_once_with(
            worktree, ("tests/test_guard.py",)
        )
        mock_git_adapter.push.assert_not_called()

    def test_pre_publish_gate_failure_reroutes_back_into_review_exchange(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")
        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-local-loop"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(
                prompt_path=coder_prompt, ai_system="claude-code"
            ),
            "agent:reviewer": AgentConfig(
                prompt_path=reviewer_prompt, ai_system="codex"
            ),
        }

        pre_publish_gate = Mock()
        pre_publish_gate.check.return_value = PrePublishGateResult(
            allowed=False,
            reason="ERROR: Test-skipping patterns detected",
            command="/tmp/hooks/pre-push",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            exit_code=1,
            stdout="",
            stderr="validation stderr\n",
            hook_path="/tmp/hooks/pre-push",
            head_sha="abc123",
            ran=True,
        )
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            pre_publish_gate=pre_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir(parents=True, exist_ok=True)
        completion_record = CompletionRecord(
            session_id="issue-123",
            timestamp=datetime.now().isoformat(),
            outcome=CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        (record_dir / "completion.json").write_text(
            json.dumps(completion_record.to_dict())
        )
        processor.session_output.start_run(worktree, "issue-123", issue_number=123)
        review_exchange = processor._review_exchange  # noqa: SLF001
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"], status="ok", rounds=1, reason="approved"
            )
        )

        with patch.object(
            review_exchange,
            "prepare_review_exchange",
            return_value=(
                SimpleNamespace(
                    ordered_actions=[
                        RequestedAction.PUSH_BRANCH,
                        RequestedAction.CREATE_PR,
                    ]
                ),
                None,
                None,
                False,
                False,
                False,
            ),
        ):
            result = processor.process(
                worktree,
                run_assets=make_session_run_assets(worktree),
                issue_number=123,
                issue_title="Test Issue",
                agent_label="agent:coder",
            )

        assert result.success
        assert result.review_exchange_deferred is True
        assert result.validation_failed_rerouted is True
        assert result.actions_taken == [
            "Validation failed; returned to coder rework via review exchange",
            "Review exchange passed",
        ]
        assert result.errors == []
        mock_git_adapter.push.assert_not_called()
        mock_label_adapter.add_label.assert_not_called()
        mock_pr_adapter.add_comment.assert_not_called()
        validation_record_path = processor._run_review_exchange_loop.call_args.kwargs[  # noqa: SLF001
            "initial_validation_record_path"
        ]
        assert validation_record_path.exists()
        record_data = json.loads(validation_record_path.read_text())
        assert record_data["passed"] is False
        assert record_data["command"] == "/tmp/hooks/pre-push"

    def test_pre_publish_gate_failure_review_exchange_halt_avoids_validation_failed_label(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")
        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-local-loop"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(
                prompt_path=coder_prompt, ai_system="claude-code"
            ),
            "agent:reviewer": AgentConfig(
                prompt_path=reviewer_prompt, ai_system="codex"
            ),
        }

        pre_publish_gate = Mock()
        pre_publish_gate.check.return_value = PrePublishGateResult(
            allowed=False,
            reason="ERROR: Test-skipping patterns detected",
            command="/tmp/hooks/pre-push",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            exit_code=1,
            stdout="",
            stderr="validation stderr\n",
            hook_path="/tmp/hooks/pre-push",
            head_sha="abc123",
            ran=True,
        )
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            pre_publish_gate=pre_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir(parents=True, exist_ok=True)
        completion_record = CompletionRecord(
            session_id="issue-123",
            timestamp=datetime.now().isoformat(),
            outcome=CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        (record_dir / "completion.json").write_text(
            json.dumps(completion_record.to_dict())
        )
        processor.session_output.start_run(worktree, "issue-123", issue_number=123)
        review_exchange = processor._review_exchange  # noqa: SLF001
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"],
                status="stopped",
                rounds=1,
                reason="max_no_progress",
            )
        )

        with patch.object(
            review_exchange,
            "prepare_review_exchange",
            return_value=(
                SimpleNamespace(
                    ordered_actions=[
                        RequestedAction.PUSH_BRANCH,
                        RequestedAction.CREATE_PR,
                    ]
                ),
                None,
                None,
                False,
                False,
                False,
            ),
        ):
            result = processor.process(
                worktree,
                run_assets=make_session_run_assets(worktree),
                issue_number=123,
                issue_title="Test Issue",
                agent_label="agent:coder",
            )

        assert not result.success
        assert result.review_exchange_halted is True
        assert result.failure_kind is None
        assert result.errors == [
            "review_exchange: stopped (reviewer_reports_no_progress)"
        ]
        assert result.actions_taken == []
        mock_git_adapter.push.assert_not_called()
        mock_label_adapter.add_label.assert_not_called()
        mock_pr_adapter.add_comment.assert_not_called()

    def test_reroute_pre_publish_validation_failure_requires_session_name(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )

        result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
            worktree=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name=None,
            agent_label="agent:coder",
            record=record,
            run_assets=make_session_run_assets(tmp_path),
        )

        assert result is None

    def test_validation_reroute_budget_halts_after_max_attempts_on_same_sha(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        """The reroute path must bound consecutive attempts on the same head_sha.

        Otherwise a permanently-failing validation forms an infinite loop:
        every tick re-enters the reroute, the predicate fix sends the
        exchange off, the exchange may eventually return ok-but-still-fails,
        and we go around again. Counter is keyed per (session, head_sha)
        so SHA advancing naturally resets the budget.
        """
        config = Config()
        config.review_exchange_max_rounds = 3  # tighten for the test
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run = processor.session_output.start_run(worktree, "issue-1", issue_number=1)
        validation_record = run.run_dir / "validation-record.json"
        validation_record.write_text(
            json.dumps({"passed": False, "head_sha": "deadbeef" * 5})
        )

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )

        # Stub the inner exchange call so we observe budget enforcement at
        # this layer specifically — the test should not depend on the
        # downstream exchange's own bounds firing.
        run_review_exchange_if_needed = MagicMock(
            return_value=("via-local-loop", None, False, True)  # deferred
        )
        processor._review_exchange.run_review_exchange_if_needed = (  # noqa: SLF001
            run_review_exchange_if_needed
        )

        # Three attempts within budget, all return success(deferred).
        for _ in range(3):
            result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
                worktree=worktree,
                issue_number=1,
                issue_title="Test",
                session_name=run.session_name,
                agent_label="agent:coder",
                record=record,
                run_assets=run,
            )
            assert result is not None
            assert result.success is True
            assert result.review_exchange_halted is False

        # Fourth attempt exceeds the budget → halt with explicit failure.
        result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
            worktree=worktree,
            issue_number=1,
            issue_title="Test",
            session_name=run.session_name,
            agent_label="agent:coder",
            record=record,
            run_assets=run,
        )
        assert result is not None
        assert result.success is False
        assert result.review_exchange_halted is True
        assert "budget is exhausted" in result.message
        # The exchange must not be invoked once the budget is exhausted.
        assert run_review_exchange_if_needed.call_count == 3

    def test_validation_reroute_budget_does_not_count_polling_ticks(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        """While the background review-exchange job is still running,
        the reroute is just polling — no new attempt was made. Counting
        these polls would let a slow exchange exhaust the budget before
        it has a chance to finish, halting issues that are actually
        making progress in the background."""
        from issue_orchestrator.control.background_job_supervisor import (
            BackgroundJobSupervisor,
        )

        config = Config()
        config.review_exchange_max_rounds = 2
        # A fake runner that always reports the job as running, so
        # ``is_review_exchange_running`` returns True every tick.
        fake_runner = MagicMock()
        fake_runner.is_running.return_value = True
        fake_runner.submit.return_value = False
        fake_runner.take_failure.return_value = None
        fake_runner.drain_completed.return_value = []
        supervisor = BackgroundJobSupervisor(fake_runner)

        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
            background_job_supervisor=supervisor,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run = processor.session_output.start_run(worktree, "issue-1", issue_number=1)
        validation_record = run.run_dir / "validation-record.json"
        validation_record.write_text(json.dumps({"passed": False, "head_sha": "aaa"}))

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        # Stub the inner exchange call to mirror what the real one does on
        # a polling tick: returns deferred=True without doing new work.
        processor._review_exchange.run_review_exchange_if_needed = MagicMock(  # noqa: SLF001
            return_value=("via-local-loop", None, False, True)
        )

        # Many polling ticks well past the configured budget — none halt.
        for _ in range(10):
            result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
                worktree=worktree,
                issue_number=1,
                issue_title="Test",
                session_name=run.session_name,
                agent_label="agent:coder",
                record=record,
                run_assets=run,
            )
            assert result is not None
            assert result.success is True
            assert result.review_exchange_halted is False

    def test_validation_reroute_budget_resets_when_sha_advances(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        """SHA advancing means the coder made progress; budget should reset."""
        config = Config()
        config.review_exchange_max_rounds = 2
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run = processor.session_output.start_run(worktree, "issue-1", issue_number=1)
        validation_record = run.run_dir / "validation-record.json"

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        processor._review_exchange.run_review_exchange_if_needed = MagicMock(  # noqa: SLF001
            return_value=("via-local-loop", None, False, True)
        )

        # Two attempts on SHA "aaa" — within budget.
        validation_record.write_text(json.dumps({"passed": False, "head_sha": "aaa"}))
        for _ in range(2):
            result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
                worktree=worktree,
                issue_number=1,
                issue_title="Test",
                session_name=run.session_name,
                agent_label="agent:coder",
                record=record,
                run_assets=run,
            )
            assert result is not None and result.success is True

        # SHA advances. Budget should reset, so two more attempts succeed.
        validation_record.write_text(json.dumps({"passed": False, "head_sha": "bbb"}))
        for _ in range(2):
            result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
                worktree=worktree,
                issue_number=1,
                issue_title="Test",
                session_name=run.session_name,
                agent_label="agent:coder",
                record=record,
                run_assets=run,
            )
            assert result is not None and result.success is True

        # Now SHA "bbb"'s budget is at 2; a third attempt halts.
        result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
            worktree=worktree,
            issue_number=1,
            issue_title="Test",
            session_name=run.session_name,
            agent_label="agent:coder",
            record=record,
            run_assets=run,
        )
        assert result is not None
        assert result.success is False
        assert result.review_exchange_halted is True


def test_cleanup_failure_posts_diagnostic_comment(
    tmp_path,
    processor,
    mock_pr_adapter,
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    completion_dir = worktree / ".issue-orchestrator"
    completion_dir.mkdir()

    record = make_record(CompletionOutcome.COMPLETED, [])
    record_path = worktree / COMPLETION_RECORD_PATH
    record_path.write_text(json.dumps(record.to_dict()))

    with patch.object(CompletionProcessor, "cleanup_record", return_value=False):
        with patch(
            "issue_orchestrator.control.completion_failure_reporting.write_issue_diagnostic"
        ) as mock_write:
            mock_write.return_value = DiagnosticReference(
                worktree_name="worktree",
                relative_path=".issue-orchestrator/diagnostics/diag.json",
            )

            processor.process(
                worktree,
                123,
                "Test issue",
                run_assets=make_session_run_assets(worktree),
            )

    mock_pr_adapter.add_comment.assert_called_once()
    comment = mock_pr_adapter.add_comment.call_args[0][1]
    assert "Diagnostic file" in comment
    assert "Worktree: `worktree`" in comment


class TestRunScopedArtifacts:
    def test_process_preserves_completion_record_in_run_dir(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(
            worktree,
            "coding-1",
            issue_number=123,
            agent_label="agent:web",
            completion_path=".issue-orchestrator/sessions/20260201-000000Z__coding-1/completion-agent_web.json",
        )
        completion_rel = (
            f".issue-orchestrator/sessions/{run.run_dir.name}/completion-agent_web.json"
        )
        completion_path = worktree / completion_rel
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[],
            implementation="Implemented the issue",
            problems="None",
        )
        completion_path.write_text(json.dumps(record.to_dict()))

        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
            event_bus=event_bus,
            label_config={},
        )

        result = processor.process(
            worktree,
            run_assets=run,
            issue_number=123,
            issue_title="Test Issue",
            completion_path=completion_rel,
            agent_label="agent:web",
        )

        preserved_path = run.run_dir / "completion-record.json"
        manifest = json.loads((run.run_dir / "manifest.json").read_text())

        assert result.success is True
        assert result.completion_record_path == str(preserved_path)
        assert preserved_path.exists()
        assert not completion_path.exists()
        assert manifest["completion_record_path"] == str(preserved_path)

    def test_review_exchange_summary_is_stored_in_review_run_dir(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
    ) -> None:
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")

        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-local-loop"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(
                prompt_path=coder_prompt, ai_system="claude-code"
            ),
            "agent:reviewer": AgentConfig(
                prompt_path=reviewer_prompt, ai_system="codex"
            ),
        }

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        review_run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260201-000001Z__review-exchange-123"
        )
        session_output = _FixedReviewExchangeSessionOutput(review_run_dir)
        coding_run = session_output.start_run(
            worktree,
            "coding-1",
            issue_number=123,
            agent_label="agent:coder",
            completion_path=".issue-orchestrator/sessions/20260201-000000Z__coding-1/completion-agent_coder.json",
        )
        completion_rel = f".issue-orchestrator/sessions/{coding_run.run_dir.name}/completion-agent_coder.json"
        completion_path = worktree / completion_rel
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            implementation="Implemented the issue",
            problems="None",
        )
        completion_path.write_text(json.dumps(record.to_dict()))

        exchange_dir = review_run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (review_run_dir / "validation-record.json").write_text(
            json.dumps({"passed": True})
        )

        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=lambda **kw: _review_exchange_outcome(
                kw["exchange_run"],
                status="ok",
                rounds=1,
                reason="reviewer_ok",
                summary={
                    "completed_rounds": 1,
                    "status": "ok",
                    "reason": "reviewer_ok",
                    "response_text": "Looks good",
                    "timestamp": "2026-02-01T00:00:00Z",
                },
            )
        )

        result = processor.process(
            worktree,
            run_assets=coding_run,
            issue_number=123,
            issue_title="Test Issue",
            completion_path=completion_rel,
            agent_label="agent:coder",
        )

        assert result.success is True
        assert (review_run_dir / "review-exchange" / "summary.json").exists()
        assert not (coding_run.run_dir / "review-exchange" / "summary.json").exists()

    def test_review_exchange_preserves_completion_record_before_loop_starts(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
    ) -> None:
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")

        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-local-loop"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(
                prompt_path=coder_prompt, ai_system="claude-code"
            ),
            "agent:reviewer": AgentConfig(
                prompt_path=reviewer_prompt, ai_system="codex"
            ),
        }

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        review_run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260201-000001Z__review-exchange-123"
        )
        session_output = _FixedReviewExchangeSessionOutput(review_run_dir)
        coding_run = session_output.start_run(
            worktree,
            "coding-1",
            issue_number=123,
            agent_label="agent:coder",
            completion_path=".issue-orchestrator/sessions/20260201-000000Z__coding-1/completion-agent_coder.json",
        )
        completion_rel = f".issue-orchestrator/sessions/{coding_run.run_dir.name}/completion-agent_coder.json"
        completion_path = worktree / completion_rel
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            implementation="Implemented the issue",
            problems="None",
        )
        completion_path.write_text(json.dumps(record.to_dict()))

        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )

        def run_exchange(*args, **kwargs):  # noqa: ANN002, ANN003
            assert (coding_run.run_dir / "completion-record.json").exists()
            return _review_exchange_outcome(
                kwargs["exchange_run"],
                status="ok",
                rounds=1,
                reason="reviewer_ok",
                summary={
                    "completed_rounds": 1,
                    "status": "ok",
                    "reason": "reviewer_ok",
                    "response_text": "Looks good",
                    "timestamp": "2026-02-01T00:00:00Z",
                },
            )

        processor._run_review_exchange_loop = MagicMock(side_effect=run_exchange)  # noqa: SLF001

        result = processor.process(
            worktree,
            run_assets=coding_run,
            issue_number=123,
            issue_title="Test Issue",
            completion_path=completion_rel,
            agent_label="agent:coder",
        )

        assert result.success is True
        assert (coding_run.run_dir / "completion-record.json").exists()


class TestEscalationVocabularyIsShared:
    """The disposition owner and the requested-action map must not diverge.

    Round 4 found them contradicting each other on their first revision: the
    owner's safety note asserted that escalation statuses do not request
    PUSH_BRANCH, while STATUS_TO_ACTIONS gives it to both. The CLI therefore
    accepted a dirty escalation that the orchestrator immediately rejected.
    These tests make the two halves one decision, so the next edit to either
    side has to face the other.
    """

    def test_escalation_statuses_exist_in_both_vocabularies(self):
        """The set is read as AgentStatus at one boundary, CompletionOutcome at the other."""
        cli_statuses = {
            AgentStatus.COMPLETED,
            AgentStatus.BLOCKED,
            AgentStatus.NEEDS_HUMAN,
            AgentStatus.APPROVED,
            AgentStatus.CHANGES_REQUESTED,
        }
        outcome_values = {o.value for o in CompletionOutcome}
        for status in ESCALATION_STATUSES:
            assert status in cli_statuses, status
            assert status in outcome_values, status

    def test_escalation_statuses_still_request_push(self):
        """The owner's exemption is justified by *what* a push publishes.

        A push sends committed history, so preserved files cannot ride along,
        and committed work still reaches the remote rather than being stranded
        in a worktree that later gets cleaned. If PUSH_BRANCH is ever removed
        from these statuses, that reasoning no longer applies and the owner's
        exemption must be revisited -- so this pins it deliberately.
        """
        for status in ESCALATION_STATUSES:
            assert RequestedAction.PUSH_BRANCH in STATUS_TO_ACTIONS[status], status

    def test_the_owner_exempts_exactly_the_escalation_statuses(self):
        for status in ESCALATION_STATUSES:
            assert (
                dirty_tree_disposition(status)
                is DirtyTreeDisposition.PRESERVE_AND_ESCALATE
            ), status

        for outcome in CompletionOutcome:
            expected = (
                DirtyTreeDisposition.PRESERVE_AND_ESCALATE
                if outcome.value in ESCALATION_STATUSES
                else DirtyTreeDisposition.REJECT
            )
            assert dirty_tree_disposition(outcome.value) is expected, outcome

    def test_completed_is_never_exempt(self):
        """Publishing finished work still requires a clean tree."""
        assert (
            dirty_tree_disposition(CompletionOutcome.COMPLETED.value)
            is DirtyTreeDisposition.REJECT
        )
