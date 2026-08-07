"""Tests for SessionRestorer - session recovery after orchestrator restart.

These tests verify the behavior of restoring session tracking after restart:
- Session restoration from discovered running sessions
- Handling of orphaned sessions (no recorded run assets)
- Error recovery during restoration
- Validation of restored session state

Tests use mock adapters at port boundaries, not internal patches.
"""

import logging
import re
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.session_restorer import SessionRestorer
from issue_orchestrator.domain.models import AgentConfig, Issue, Session
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.session_runner import DiscoveredSession
from tests.unit.session_run_helpers import make_session_run_assets


class MockRepositoryHost:
    """Mock RepositoryHost for testing SessionRestorer.

    Implements only the methods used by SessionRestorer:
    - get_issue: Returns issue by number
    """

    def __init__(self):
        self.issues: dict[int, Issue] = {}

    def get_issue(self, issue_number: int) -> Issue | None:
        """Return issue from test data."""
        return self.issues.get(issue_number)


class MockWorkingCopy:
    """Mock WorkingCopy for testing SessionRestorer.

    Implements only the methods used by SessionRestorer:
    - get_current_branch: Returns branch name for worktree
    """

    def __init__(self):
        self.branches: dict[Path, str] = {}

    def get_current_branch(self, worktree: Path) -> str | None:
        """Return configured branch name for worktree."""
        return self.branches.get(worktree)


def make_discovered_session(
    issue_number: int,
    tab_name: str | None = None,
    is_review: bool = False,
    session_name: str | None = None,
    worktree: Path | None = None,
) -> DiscoveredSession:
    """Create a DiscoveredSession for testing."""
    if tab_name is None:
        if is_review:
            tab_name = f"#100 Review PR #{issue_number}"
        else:
            tab_name = f"#{issue_number} Some task"
    discovered = DiscoveredSession(
        issue_number=issue_number,
        tab_name=tab_name,
        is_review=is_review,
    )
    if session_name:
        discovered["session_name"] = session_name
    if worktree is not None:
        asset_session_name = session_name or _asset_session_name(
            issue_number,
            tab_name,
            is_review,
        )
        run_assets = make_session_run_assets(worktree, session_name=asset_session_name)
        discovered["run_dir"] = str(run_assets.run_dir)
    return discovered


def _asset_session_name(issue_number: int, tab_name: str, is_review: bool) -> str:
    if not is_review:
        return f"issue-{issue_number}"
    match = re.search(r"\bReview PR #(\d+)\b", tab_name)
    if match:
        return f"review-{match.group(1)}"
    return f"review-{issue_number}"


def make_config(
    agents: dict[str, AgentConfig] | None = None,
    repo: str = "test/repo",
) -> Config:
    """Create a Config with the given agents."""
    config = Config()
    config.repo = repo
    if agents:
        config.agents = agents
    return config


def make_agent_config(
    tmp_path: Path,
) -> AgentConfig:
    """Create an AgentConfig for testing."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    return AgentConfig(
        prompt_path=prompt,
    )


class TestRestoreSessionsBasic:
    """Tests for basic session restoration behavior."""

    def test_canonical_terminal_id_prefers_persisted_session_name(self, tmp_path):
        """The persisted registry id wins over user-facing tab text."""
        config = make_config(agents={"agent:web": make_agent_config(tmp_path)})
        restorer = SessionRestorer(config, MockRepositoryHost(), MockWorkingCopy())
        discovered = make_discovered_session(
            100,
            tab_name="Review PR #456",
            is_review=True,
            session_name="review-789",
        )

        assert restorer.canonical_terminal_id(discovered) == "review-789"

    def test_canonical_terminal_id_extracts_review_pr_from_tab_name(self, tmp_path):
        """Legacy discovered review records still derive review-N from tab text."""
        config = make_config(agents={"agent:web": make_agent_config(tmp_path)})
        restorer = SessionRestorer(config, MockRepositoryHost(), MockWorkingCopy())
        discovered = make_discovered_session(100, tab_name="#100 Review PR #456", is_review=True)

        assert restorer.canonical_terminal_id(discovered) == "review-456"

    def test_canonical_terminal_id_warns_when_review_name_cannot_be_derived(
        self,
        tmp_path,
        caplog,
    ):
        """A malformed review discovery record is visible in logs before fallback."""
        config = make_config(agents={"agent:web": make_agent_config(tmp_path)})
        restorer = SessionRestorer(config, MockRepositoryHost(), MockWorkingCopy())
        discovered = make_discovered_session(100, tab_name="review title without pr", is_review=True)

        with caplog.at_level(logging.WARNING):
            assert restorer.canonical_terminal_id(discovered) == "review-100"

        assert "Could not derive review PR number" in caplog.text

    def test_restore_known_terminal_uses_canonical_name_without_fake_tab_title(
        self,
        tmp_path,
    ):
        """Known-terminal restore carries session_name without inventing tab text."""
        config = make_config(agents={"agent:web": make_agent_config(tmp_path)})
        restorer = SessionRestorer(config, MockRepositoryHost(), MockWorkingCopy())
        restorer.restore_sessions = MagicMock(return_value=[])
        run_assets = make_session_run_assets(tmp_path, session_name="issue-123")

        restorer.restore_known_terminal(
            issue_number=123,
            session_name="issue-123",
            run_dir=run_assets.run_dir,
            is_review=False,
            already_tracked=[],
        )

        running = restorer.restore_sessions.call_args.args[0]
        assert running == [
            {
                "issue_number": 123,
                "tab_name": "",
                "is_review": False,
                "session_name": "issue-123",
                "run_dir": str(run_assets.run_dir),
            }
        ]

    def test_restores_code_session_with_worktree_and_issue(self, tmp_path):
        """A discovered code session with matching worktree and issue is restored."""
        # Setup: create worktree directory
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
        )

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-test-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Act
        discovered = [make_discovered_session(123, is_review=False, worktree=worktree)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Assert
        assert len(restored) == 1
        session = restored[0]
        assert session.issue.number == 123
        assert session.terminal_id == "issue-123"
        assert session.worktree_path == worktree
        assert session.branch_name == "123-test-branch"
        assert session.key.task == TaskKind.CODE

    def test_restores_review_session_with_pr_number_from_tab_name(self, tmp_path):
        """A discovered review session extracts PR number from tab name."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-100"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:reviewer": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[100] = Issue(
            number=100,
            title="Original issue",
            labels=["agent:reviewer"],
        )

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "100-feature-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Tab name format: "#<issue> Review PR #<pr>"
        discovered = [
            make_discovered_session(
                100,
                tab_name="#100 Review PR #456",
                is_review=True,
                worktree=worktree,
            )
        ]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        session = restored[0]
        assert session.terminal_id == "review-456"  # PR number from tab name
        assert session.key.task == TaskKind.REVIEW

    def test_skips_already_tracked_sessions(self, tmp_path):
        """Sessions that are already tracked are not restored again."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
        )

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Create an already-tracked session
        existing_session = MagicMock(spec=Session)
        existing_session.terminal_id = "issue-123"

        discovered = [make_discovered_session(123)]
        restored = restorer.restore_sessions(discovered, already_tracked=[existing_session])

        # Session is already tracked, so nothing restored
        assert len(restored) == 0

    def test_skips_duplicates_within_discovered_sessions(self, tmp_path):
        """If same session appears multiple times in discovered, only restore once."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
        )

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Same issue discovered twice
        discovered = [
            make_discovered_session(123, tab_name="#123 First tab", worktree=worktree),
            make_discovered_session(123, tab_name="#123 Second tab", worktree=worktree),
        ]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Only first one is restored; second is skipped as duplicate
        assert len(restored) == 1


class TestOrphanedSessionHandling:
    """Tests for handling sessions without recorded run assets."""

    def test_skips_discovered_session_without_run_assets(self, tmp_path, caplog):
        """Sessions without recorded run assets are skipped and logged."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        working_copy = MockWorkingCopy()

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Session not restored
        assert len(restored) == 0

        # Warning logged
        assert "has no recorded run_dir" in caplog.text


class TestErrorRecovery:
    """Tests for error handling during session restoration."""

    def test_continues_after_exception_restoring_single_session(self, tmp_path, caplog):
        """If one session fails to restore, continue with others."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree_200 = tmp_path / "repo-200"
        worktree_200.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        # Only issue 200 exists; issue 100 will trigger cleanup path
        repo_host.issues[200] = Issue(number=200, title="Good issue", labels=["agent:web"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree_200] = "200-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [
            make_discovered_session(100),  # Will fail - no recorded run assets
            make_discovered_session(200, worktree=worktree_200),  # Will succeed
        ]

        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Only the successful session restored
        assert len(restored) == 1
        assert restored[0].issue.number == 200

    def test_exception_during_restore_is_logged(self, tmp_path, caplog):
        """Exceptions during single session restore are logged and continue."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        # Create a repo host that throws on get_issue
        class FailingRepoHost(MockRepositoryHost):
            def get_issue(self, issue_number: int) -> Issue | None:
                raise RuntimeError("Simulated API failure")

        repo_host = FailingRepoHost()
        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123, worktree=worktree)]
        with caplog.at_level(logging.ERROR):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # No sessions restored due to exception
        assert len(restored) == 0

        # Exception logged
        assert "Failed to restore session for issue #123" in caplog.text


class TestStateValidation:
    """Tests for state validation during restoration."""

    def test_skips_session_without_agent_config(self, tmp_path, caplog):
        """Sessions without available agent config are skipped."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        config = make_config(agents={})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(number=123, title="Test", labels=[])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123, worktree=worktree)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # No session restored - no agent config available means session skipped
        assert len(restored) == 0
        assert "No agent config available" in caplog.text

    def test_skips_session_without_repo_config(self, tmp_path, caplog):
        """Sessions without repo in config are skipped."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config}, repo=None)
        config.repo = None  # No repo configured
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(number=123, title="Test", labels=["agent:web"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123, worktree=worktree)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # No session restored
        assert len(restored) == 0
        assert "No repo configured" in caplog.text

    def test_creates_minimal_issue_when_issue_not_found(self, tmp_path):
        """When issue not found in repo, creates minimal issue object."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        # No issue 123 in repo - get_issue returns None

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [
            make_discovered_session(123, tab_name="#123 My task", worktree=worktree)
        ]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Session still restored with minimal issue
        assert len(restored) == 1
        session = restored[0]
        assert session.issue.number == 123
        assert session.issue.title == "123 My task"  # Tab name with # stripped

    def test_uses_fallback_agent_config_when_issue_has_no_agent_label(self, tmp_path):
        """Uses first available agent config when issue has no agent type label."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        # Issue with no agent: label
        repo_host.issues[123] = Issue(number=123, title="Test", labels=[])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123, worktree=worktree)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Session restored with fallback agent config
        assert len(restored) == 1
        assert restored[0].agent_config == agent_config


class TestBranchNameResolution:
    """Tests for branch name resolution from worktrees."""

    def test_uses_unknown_branch_when_git_fails(self, tmp_path, caplog):
        """When working copy fails to get branch, uses 'unknown' as fallback."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(number=123, title="Test", labels=["agent:web"])

        working_copy = MockWorkingCopy()
        # No branch configured for worktree - returns None

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123, worktree=worktree)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        assert restored[0].branch_name == "unknown"
        assert "Failed to get branch name" in caplog.text


class TestWorktreeFromRunAssets:
    """Tests for typed worktree restoration from run assets."""

    def test_uses_worktree_recorded_in_run_assets(self, tmp_path):
        """Worktree comes from the run manifest, not sibling directory search."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "custom" / "agent-worktree"
        worktree.mkdir(parents=True)

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(number=123, title="Test", labels=["agent:web"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-feature"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123, worktree=worktree)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        assert restored[0].worktree_path == worktree

class TestReviewSessionSpecifics:
    """Tests specific to review session restoration."""

    def test_review_session_uses_issue_number_when_pr_not_in_tab(self, tmp_path):
        """Review session falls back to issue number if PR number not in tab name."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-100"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:reviewer": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[100] = Issue(number=100, title="Test", labels=["agent:reviewer"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "100-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Tab name without PR number pattern
        discovered = [
            make_discovered_session(
                100,
                tab_name="#100 Review Something",
                is_review=True,
                worktree=worktree,
            )
        ]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        # Falls back to issue number as PR number
        assert restored[0].terminal_id == "review-100"

    def test_review_session_has_correct_task_kind(self, tmp_path):
        """Review sessions have TaskKind.REVIEW in their session key."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-100"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:reviewer": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[100] = Issue(number=100, title="Test", labels=["agent:reviewer"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "100-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(100, is_review=True, worktree=worktree)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        assert restored[0].key.task == TaskKind.REVIEW


class TestRestoredTechLeadScope:
    """A restored tech-lead run keeps its scope, so the global barrier holds.

    Before #6994 round 1 F3 a restart rebuilt tech-lead sessions with no
    ``tech_lead_scope``. A running whole-board review therefore stopped counting
    as global: targeted work launched alongside an exclusive review, and the
    dashboard reported the anchor as an ordinary running issue. Everything
    needed to rebuild the grant is durable — the anchor's marker label and the
    cohort ledger — so these pin that it IS rebuilt.
    """

    @staticmethod
    def _restorer(tmp_path, issue, *, authority=None):
        from issue_orchestrator.control.health_review_trigger import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        _ = HEALTH_REVIEW_MARKER_LABEL
        config = make_config(agents={"agent:tech-lead": make_agent_config(tmp_path)})
        config.tech_lead_review_agent = "agent:tech-lead"
        repo_host = MockRepositoryHost()
        repo_host.issues[issue.number] = issue
        working_copy = MockWorkingCopy()
        return (
            SessionRestorer(config, repo_host, working_copy, authority),
            config,
        )

    def _restore(self, tmp_path, issue, *, authority=None):
        worktree = tmp_path / f"repo-{issue.number}"
        worktree.mkdir()
        restorer, config = self._restorer(tmp_path, issue, authority=authority)
        working_copy = restorer.working_copy
        working_copy.branches[worktree] = "main"
        discovered = [
            make_discovered_session(issue.number, is_review=False, worktree=worktree)
        ]
        restored = restorer.restore_sessions(discovered, already_tracked=[])
        return restored, config

    def test_a_restored_health_review_is_still_a_global_run(self, tmp_path):
        from issue_orchestrator.control.health_review_trigger import (
            HEALTH_REVIEW_MARKER_LABEL,
        )
        from issue_orchestrator.control.tech_lead_run_admission import (
            has_active_global_run,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

        issue = Issue(
            number=900,
            title="Health Review — walk the floor",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
        )
        restored, config = self._restore(tmp_path, issue)

        assert len(restored) == 1
        scope = restored[0].tech_lead_scope
        assert scope is not None
        assert scope.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert has_active_global_run(config, restored) is True

    def test_a_restored_batch_review_is_still_a_global_run(self, tmp_path):
        from issue_orchestrator.control.tech_lead_run_admission import (
            has_active_global_run,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

        issue = Issue(
            number=800,
            title="Tech Lead Batch Review (3 PRs)",
            labels=["agent:tech-lead"],
        )
        restored, config = self._restore(tmp_path, issue)

        scope = restored[0].tech_lead_scope
        assert scope is not None
        assert scope.flavor is TechLeadSessionFlavor.BATCH_REVIEW
        assert has_active_global_run(config, restored) is True

    def test_a_restored_investigation_is_not_a_global_barrier(self, tmp_path):
        """The conservative default must not swallow targeted runs.

        A restored FAILURE_INVESTIGATION has to come back issue-scoped, or every
        restart would silently block all other tech-lead work.
        """
        from issue_orchestrator.control.tech_lead_run_admission import (
            has_active_global_run,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

        issue = Issue(number=42, title="Broken thing", labels=["agent:tech-lead"])
        restored, config = self._restore(tmp_path, issue)

        scope = restored[0].tech_lead_scope
        assert scope is not None
        assert scope.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        assert has_active_global_run(config, restored) is False

    def test_a_restored_storm_review_recovers_its_owned_cohort(self, tmp_path):
        from issue_orchestrator.control.health_review_trigger import (
            HEALTH_REVIEW_MARKER_LABEL,
        )
        from issue_orchestrator.domain.models import DiscoveredFailure

        class _Authority:
            def load_storm_cohort(self, *, anchor_issue_number):
                assert anchor_issue_number == 900
                return (
                    DiscoveredFailure(
                        issue_number=7, issue_title="a", failure_reason="timed_out"
                    ),
                    DiscoveredFailure(
                        issue_number=5, issue_title="b", failure_reason="timed_out"
                    ),
                )

        issue = Issue(
            number=900,
            title="Health Review",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
        )
        restored, _ = self._restore(tmp_path, issue, authority=_Authority())

        assert restored[0].tech_lead_scope.problem_issue_numbers == (5, 7)

    def test_a_restored_global_run_still_blocks_targeted_launches(self, tmp_path):
        """The end the barrier exists for (#6994 round 1 F3)."""
        from issue_orchestrator.control.health_review_trigger import (
            HEALTH_REVIEW_MARKER_LABEL,
        )
        from issue_orchestrator.control.tech_lead_run_admission import (
            plan_tech_lead_launch_gate,
        )
        from issue_orchestrator.domain.models import (
            DiscoveredFailure,
            PendingTechLeadReview,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

        issue = Issue(
            number=900,
            title="Health Review",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
        )
        restored, config = self._restore(tmp_path, issue)
        queued = PendingTechLeadReview(
            42,
            "Investigate #42",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=42, issue_title="Investigate", failure_reason="timed_out"
            ),
        )

        gate = plan_tech_lead_launch_gate(config, [queued], restored)

        assert gate.launchable == ()
        assert list(gate.held) == [queued]

    def test_the_dashboard_reports_a_restored_global_run_as_global(self, tmp_path):
        """The projection reads the same recovered stamp the gate does."""
        from issue_orchestrator.control.health_review_trigger import (
            HEALTH_REVIEW_MARKER_LABEL,
        )
        from issue_orchestrator.domain.models import OrchestratorState
        from issue_orchestrator.view_models.tech_lead_run_actions import (
            STATUS_RUNNING,
            read_tech_lead_run_actions,
        )

        issue = Issue(
            number=900,
            title="Health Review",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
        )
        restored, config = self._restore(tmp_path, issue)
        state = OrchestratorState()
        state.active_sessions = list(restored)

        view = read_tech_lead_run_actions(config, state)

        assert view.global_status == STATUS_RUNNING
        assert view.global_barrier_active is True
        # The anchor is NOT a board card an operator can aim the targeted
        # action at, so it must not appear as a running investigation.
        assert view.running_issue_numbers == ()
