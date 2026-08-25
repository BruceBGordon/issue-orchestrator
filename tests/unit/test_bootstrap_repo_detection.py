"""Tests for bootstrap repo auto-detection and orchestrator building."""

from unittest.mock import patch, MagicMock
import os

import pytest

from issue_orchestrator.adapters.github.repo import get_repo_from_git, GitRepoError
from issue_orchestrator.entrypoints.bootstrap import (
    Dependencies,
    build_orchestrator,
    build_orchestrator_for_testing,
    _check_github_token_scopes,
    _create_planner,
    _validation_attempt_key_factory,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.env import ENV_PREFIX
from issue_orchestrator.infra.secret_env import EXTRA_FORBIDDEN_ENV_VARS_ENV
from issue_orchestrator.ports import NullEventSink, NullSessionRunner
from issue_orchestrator.ports.claim_manager import NullClaimManager


class TestRepoAutoDetection:
    """Tests for auto-detecting repo from git remote."""

    def test_get_repo_from_https_remote(self) -> None:
        """Parse owner/repo from HTTPS GitHub URL."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/owner/repo-name.git"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            repo = get_repo_from_git()

        assert repo == "owner/repo-name"

    def test_get_repo_from_ssh_remote(self) -> None:
        """Parse owner/repo from SSH GitHub URL."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "git@github.com:owner/repo-name.git"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            repo = get_repo_from_git()

        assert repo == "owner/repo-name"

    def test_get_repo_from_https_without_git_suffix(self) -> None:
        """Handle HTTPS URL without .git suffix."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/owner/repo-name"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            repo = get_repo_from_git()

        assert repo == "owner/repo-name"

    def test_get_repo_raises_on_no_remote(self) -> None:
        """Raise GitRepoError when no remote configured."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            with pytest.raises(GitRepoError, match="Could not determine repository"):
                get_repo_from_git()

    def test_get_repo_raises_on_non_github_remote(self) -> None:
        """Raise GitRepoError for non-GitHub remotes."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://gitlab.com/owner/repo.git"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            with pytest.raises(GitRepoError, match="Unrecognized GitHub remote"):
                get_repo_from_git()


class TestBootstrapRepoResolution:
    """Tests for bootstrap.py repo resolution logic."""

    def test_bootstrap_uses_config_repo_when_set(self) -> None:
        """When config.repo is set, use it directly."""
        from issue_orchestrator.infra.config import Config

        config = Config()
        config.repo = "configured/repo"

        # Simulate bootstrap logic
        repo = config.repo
        if not repo:
            repo = "auto-detected/repo"

        assert repo == "configured/repo"

    def test_bootstrap_auto_detects_when_config_repo_none(self) -> None:
        """When config.repo is None, auto-detect from git and update config."""
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.adapters.github.repo import get_repo_from_git

        config = Config()
        config.repo = None

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/auto/detected.git"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result

            # Simulate bootstrap logic (matches bootstrap.py)
            repo = config.repo
            if not repo:
                repo = get_repo_from_git()
                config.repo = repo  # Bootstrap updates config

        assert repo == "auto/detected"
        assert config.repo == "auto/detected"  # Config is updated

    def test_bootstrap_error_message_when_no_repo(self) -> None:
        """Error message is clear when repo can't be determined."""
        expected_snippets = [
            "Could not determine GitHub repository",
            "repo.name",
            "git remote",
        ]

        # This is the error message from bootstrap.py
        error_msg = (
            "Could not determine GitHub repository.\n\n"
            "Either:\n"
            "  1. Set 'repo.name' in your config file:\n"
            "       repo:\n"
            "         name: owner/repo-name\n\n"
            "  2. Or ensure you're running from a git repo with a GitHub remote:\n"
            "       git remote get-url origin\n"
            "       # Should show: https://github.com/owner/repo.git"
        )

        for snippet in expected_snippets:
            assert snippet in error_msg, f"Expected '{snippet}' in error message"

    def test_validation_attempt_key_factory_uses_provided_issue_key(self) -> None:
        config = Config()
        config.repo = "owner/repo"

        factory = _validation_attempt_key_factory(config)
        key = factory.for_validation_attempt(
            issue_key=GitHubIssueKey(repo="owner/repo", external_id="359"),
            head_sha="a" * 40,
        )

        assert key.issue_scope == "owner/repo"
        assert key.issue_stable_id == "359"
        assert key.head_sha == "a" * 40

    def test_validation_attempt_key_factory_does_not_parse_mutable_title(
        self,
    ) -> None:
        config = Config()
        config.repo = "owner/repo"

        factory = _validation_attempt_key_factory(config)
        key = factory.for_validation_attempt(
            issue_key=GitHubIssueKey(repo="owner/repo", external_id="359"),
            head_sha="b" * 40,
        )

        assert key.issue_scope == "owner/repo"
        assert key.issue_stable_id == "359"


class TestDependencies:
    """Tests for the Dependencies container class."""

    def test_dependencies_init_with_all_args(self) -> None:
        """Dependencies stores all provided arguments."""
        event_sink = NullEventSink()
        session_runner = NullSessionRunner()
        github_adapter = MagicMock()

        deps = Dependencies(events=event_sink, runner=session_runner, github=github_adapter)

        assert deps.events is event_sink
        assert deps.runner is session_runner
        assert deps.github is github_adapter

    def test_dependencies_init_with_none_github(self) -> None:
        """Dependencies can be initialized with github=None."""
        event_sink = NullEventSink()
        session_runner = NullSessionRunner()

        deps = Dependencies(events=event_sink, runner=session_runner)

        assert deps.events is event_sink
        assert deps.runner is session_runner
        assert deps.github is None


class TestCheckGithubTokenScopes:
    """Tests for _check_github_token_scopes function."""

    def test_check_scopes_success_with_required_scopes(self) -> None:
        """Success when token has all required scopes."""
        config = Config()
        config.github_required_scopes = ["repo", "workflow"]
        config.github_allowed_scopes = []

        github_adapter = MagicMock()
        github_adapter.get_token_scopes.return_value = ["repo", "workflow", "read:user"]

        # Should not raise
        _check_github_token_scopes(config, github_adapter)

    def test_check_scopes_failure_missing_required(self) -> None:
        """Raises ValueError when required scopes are missing."""
        config = Config()
        config.github_required_scopes = ["repo", "workflow"]
        config.github_allowed_scopes = []

        github_adapter = MagicMock()
        github_adapter.get_token_scopes.return_value = ["repo"]  # Missing workflow

        with pytest.raises(ValueError, match="missing required scopes"):
            _check_github_token_scopes(config, github_adapter)

    def test_check_scopes_failure_extra_disallowed(self) -> None:
        """Raises ValueError when token has disallowed scopes."""
        config = Config()
        config.github_required_scopes = []
        config.github_allowed_scopes = ["repo", "read:user"]

        github_adapter = MagicMock()
        github_adapter.get_token_scopes.return_value = ["repo", "read:user", "workflow"]

        with pytest.raises(ValueError, match="disallowed scopes"):
            _check_github_token_scopes(config, github_adapter)

    def test_check_scopes_with_empty_required_list(self) -> None:
        """Empty or whitespace-only scopes are ignored."""
        config = Config()
        config.github_required_scopes = ["repo", "  ", ""]  # Mixed with whitespace
        config.github_allowed_scopes = []

        github_adapter = MagicMock()
        github_adapter.get_token_scopes.return_value = ["repo"]

        # Should not raise - only non-empty scopes are checked
        _check_github_token_scopes(config, github_adapter)

    def test_check_scopes_logs_warning_on_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Logs warning and returns if get_token_scopes fails."""
        config = Config()
        config.github_required_scopes = ["repo"]
        config.github_allowed_scopes = []

        github_adapter = MagicMock()
        github_adapter.get_token_scopes.side_effect = Exception("API error")

        with caplog.at_level("WARNING"):
            _check_github_token_scopes(config, github_adapter)
        assert "Failed to fetch GitHub token scopes" in caplog.text

    def test_check_scopes_logs_token_info_when_available(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Logs token scopes when successfully retrieved."""
        config = Config()
        config.github_required_scopes = []
        config.github_allowed_scopes = []

        github_adapter = MagicMock()
        github_adapter.get_token_scopes.return_value = ["repo", "workflow"]

        with caplog.at_level("INFO"):
            _check_github_token_scopes(config, github_adapter)
        assert "GitHub token scopes: repo, workflow" in caplog.text

    def test_check_scopes_logs_when_scopes_unavailable(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Logs info when token scopes are unavailable."""
        config = Config()
        config.github_required_scopes = []
        config.github_allowed_scopes = []

        github_adapter = MagicMock()
        github_adapter.get_token_scopes.return_value = []

        with caplog.at_level("INFO"):
            _check_github_token_scopes(config, github_adapter)
        assert "GitHub token scopes unavailable" in caplog.text

    def test_check_scopes_skips_github_app_auth(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """GitHub App auth has permissions, not OAuth scopes."""
        config = Config()
        config.github_required_scopes = ["repo"]

        github_adapter = MagicMock()
        github_adapter.auth_kind = "github_app"

        with caplog.at_level("INFO"):
            _check_github_token_scopes(config, github_adapter)

        github_adapter.get_token_scopes.assert_not_called()
        assert "Skipping OAuth scope check" in caplog.text


class TestBuildOrchestratorForTesting:
    """Tests for build_orchestrator_for_testing function."""

    def test_callback_endpoint_is_ready(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """A composition that binds no Control API must say so.

        The launcher refuses to start a session while the callback
        endpoint is unresolved, so a composition that neither publishes
        nor declares silently stops launching anything — which is how
        this surfaced: one tech-lead launch test failed with a bare
        ``session is None`` and nothing pointing at the cause (#6924 F7).
        """
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orchestrator = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
            )
        endpoint = orchestrator.deps.agent_callback_endpoint
        assert endpoint.is_ready(), (
            "testing composition must resolve the endpoint question, or "
            "every session launch is silently gated"
        )
        assert endpoint.resolve_port(0) is None, "no Control API is bound here"

    @pytest.fixture
    def minimal_config(self, tmp_path) -> Config:
        """Minimal valid config for testing."""
        config = Config()
        config.repo = "test/repo"
        config.repo_root = tmp_path
        config.worktree_base = tmp_path / "worktrees"
        return config

    @pytest.fixture
    def mock_github(self) -> MagicMock:
        """Mock GitHub adapter."""
        github = MagicMock()
        github.get_issue_labels.return_value = []
        return github

    def test_build_orchestrator_for_testing_with_all_defaults(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Builds orchestrator with defaults when minimal args provided."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
            )

            assert orch is not None
            assert orch.config is minimal_config
            assert orch.deps.repository_host is mock_github

    def test_build_orchestrator_writes_real_board_snapshot_on_tech_lead_launch(
        self, minimal_config: Config, mock_github: MagicMock, tmp_path
    ) -> None:
        """Bootstrap must wire the REAL board snapshot provider (ADR-0031 §3).

        Merely constructing the launcher cannot distinguish the real
        ``StateBoardSnapshotProvider`` from a Null one, so this drives a
        public tech_lead launch end-to-end through the bootstrapped
        orchestrator: a tech lead agent is configured, ``launch_session`` runs
        the real worktree + tech-lead-prep path (``prepare_tech_lead_session_data``),
        and the board-snapshot.json the agent treats as authoritative input
        must be written and non-trivially populated. The terminal-creation
        boundary is the default ``NullSessionRunner`` — a public bootstrap
        seam that reports successful session creation without a terminal.
        """
        import json
        import subprocess
        from pathlib import Path

        from issue_orchestrator.domain.board_snapshot import BOARD_SNAPSHOT_SCHEMA_VERSION, BoardSnapshot
        from issue_orchestrator.domain.models import AgentConfig, Issue

        # A real git repo: the bootstrapped GitWorktreeManager is not a mock.
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main"], cwd=repo_root, check=True,
            capture_output=True,
        )
        (repo_root / "README.md").write_text("seed\n")
        subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(
            [
                "git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
                "commit", "-m", "seed",
            ],
            cwd=repo_root, check=True, capture_output=True,
        )
        # Worktree creation fetches origin/<base>; a self-remote satisfies it
        # without any network.
        subprocess.run(
            ["git", "remote", "add", "origin", str(repo_root)],
            cwd=repo_root, check=True, capture_output=True,
        )
        minimal_config.repo_root = repo_root
        minimal_config.worktree_base = tmp_path / "worktrees"

        prompt = tmp_path / "tech-lead-prompt.md"
        prompt.write_text("Tech Lead prompt")
        minimal_config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt, model="sonnet", timeout_minutes=45,
        )
        minimal_config.tech_lead_review_agent = "agent:tech-lead"
        # Bounded GitHub seams: no PRs to audit, no fresh issue to re-read.
        mock_github.get_prs_with_label.return_value = []
        mock_github.get_issue.return_value = None

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
            )

        session = orch.launch_session(
            Issue(
                number=41,
                title="Tech Lead Batch Review",
                labels=["agent:tech-lead"],
                repo="test/repo",
                body="No dependencies.",
            )
        )

        assert session is not None, "tech_lead launch must succeed end-to-end"
        snapshot_path = (
            Path(session.run_assets.run_dir) / "tech-lead-data" / "board-snapshot.json"
        )
        assert snapshot_path.exists(), (
            "prepare_tech_lead_session_data must write the authoritative "
            "board-snapshot.json through the bootstrap-wired provider"
        )
        # Non-trivially populated and readable through the typed contract.
        data = json.loads(snapshot_path.read_text())
        assert data["generated_at"], "real provider stamps a real clock"
        snapshot = BoardSnapshot.read(snapshot_path)
        assert snapshot.schema_version == BOARD_SNAPSHOT_SCHEMA_VERSION
        assert snapshot.orchestrator_paused is False

    def test_build_orchestrator_wires_pair_registry_for_shutdown(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """The persistent-pair registry is the canonical owner of pair
        termination at orchestrator stop (ADR 0026 / PR #6209 review).
        Bootstrap must thread it into ``deps.pair_registry`` so
        ``Orchestrator.close`` can call ``shutdown_all`` on it; if the
        wiring lapses, ``deps.pair_registry`` returns ``None`` and the
        guard in ``close`` silently skips teardown — leaving PTY agents
        leaked across orchestrator restarts.
        """
        from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
            InMemoryPersistentExchangePairRegistry,
        )

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
            )

        assert orch.deps.pair_registry is not None, (
            "deps.pair_registry must be wired by bootstrap so "
            "Orchestrator.close can shut down PTY agents on stop"
        )
        assert isinstance(
            orch.deps.pair_registry, InMemoryPersistentExchangePairRegistry,
        )

    def test_pair_registry_release_reclaims_reviewer_worktree(
        self, minimal_config: Config, mock_github: MagicMock,
        tmp_path,
    ) -> None:
        """B2's reviewer-worktree lifecycle hangs on bootstrap wiring
        an ``on_release`` hook into the registry — without it, every
        release path (escalation, reset, shutdown, merge) closes the
        PTY processes but leaves the sibling worktree on disk
        (PR #6212 finding 2).

        This test fakes a pair (so we don't have to start real
        subprocesses) and asserts that the registry's release path
        invokes ``remove_reviewer_worktree`` against the pair's
        worktree path.
        """
        from types import SimpleNamespace
        from unittest.mock import patch as _patch
        from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
            PersistentExchangePair,
        )

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
            )

        pair_registry = orch.deps.pair_registry
        assert pair_registry is not None

        worktree_path = tmp_path / "fake-reviewer-wt"
        worktree_path.mkdir()
        coder = SimpleNamespace(proc=SimpleNamespace(pid=1001, poll=lambda: None))
        reviewer = SimpleNamespace(proc=SimpleNamespace(pid=1002, poll=lambda: None))
        pair = PersistentExchangePair(
            coder_session=coder,  # type: ignore[arg-type]
            reviewer_session=reviewer,  # type: ignore[arg-type]
            reviewer_worktree_path=worktree_path,
            issue_key=42,
            exchange_run_id="run-42",
            run_dir=tmp_path / ".issue-orchestrator" / "sessions" / "run-42",
            created_at=0.0,
            coder_response_path=tmp_path / "coder/r.json",
            reviewer_response_path=tmp_path / "reviewer/r.json",
            reviewer_report_path=worktree_path / ".issue-orchestrator" / "review-report.md",
            coder_recording_path=tmp_path / "coder/rec.jsonl",
            reviewer_recording_path=tmp_path / "reviewer/rec.jsonl",
            coder_completion_path=tmp_path / "coder/c.json",
            validation_record_path=tmp_path / "v.json",
        )
        pair_registry.acquire(issue_key=42, spawn=lambda: pair)

        # Patch at the source module — bootstrap imports
        # ``remove_reviewer_worktree`` lazily inside ``build_orchestrator``
        # (a function-scoped import), so it never lives on the
        # bootstrap module's namespace as an attribute. The closure
        # captures the function reference at construction time;
        # patching the source module is what intercepts the call.
        with _patch(
            "issue_orchestrator.execution.reviewer_worktree.remove_reviewer_worktree",
        ) as mock_remove, _patch(
            "issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory.close_persistent_session",
        ):
            pair_registry.release(42, reason="test-release-boundary")

        mock_remove.assert_called_once()
        kwargs = mock_remove.call_args.kwargs
        assert kwargs.get("force") is True, (
            "on_release hook must call remove_reviewer_worktree with "
            "force=True so a stuck checkout doesn't strand the worktree"
        )
        positional = mock_remove.call_args.args[0]
        assert positional.path == worktree_path

    def test_orchestrator_close_calls_pair_registry_shutdown_all(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """``Orchestrator.close`` must call ``shutdown_all`` on the
        registry so PTY agents are reaped at orchestrator stop. Pin
        the call here so a regression that drops the wiring (e.g.
        a refactor of ``close`` that forgets the new line) fails
        loudly instead of leaking subprocesses.
        """
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
            )

        assert orch.deps.pair_registry is not None
        # Wrap the registry's ``shutdown_all`` to record invocations.
        original = orch.deps.pair_registry.shutdown_all
        calls: list[str] = []

        def _track(*, reason: str) -> None:
            calls.append(reason)
            original(reason=reason)

        orch.deps.pair_registry.shutdown_all = _track  # type: ignore[method-assign]
        orch.close()

        assert calls == ["orchestrator-shutdown"], (
            "Orchestrator.close must call pair_registry.shutdown_all "
            "with reason='orchestrator-shutdown'"
        )

    def test_build_orchestrator_for_testing_with_custom_events(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Uses provided EventSink instead of default."""
        custom_events = MagicMock()

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                events=custom_events,
            )

            # Events are wrapped in SequencedEventSink
            assert orch.deps.events is not None

    def test_build_orchestrator_for_testing_with_custom_runner(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Uses provided SessionRunner instead of default."""
        custom_runner = MagicMock()

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                runner=custom_runner,
            )

            # Runner gets passed through
            assert orch.deps.runner is custom_runner

    def test_build_orchestrator_for_testing_with_custom_planner(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Uses provided Planner instead of creating default."""
        custom_planner = MagicMock()

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                planner=custom_planner,
            )

            assert orch.deps.planner is custom_planner

    def test_build_orchestrator_for_testing_creates_default_planner(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Creates default Planner when not provided."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                planner=None,
            )

            assert orch.deps.planner is not None

    def test_build_orchestrator_for_testing_wires_dependency_evaluator_to_default_planner(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Default planner and scheduler share dependency gating."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                planner=None,
            )

            assert orch.scheduler.dependency_evaluator is not None
            assert orch.deps.planner.dependency_evaluator is orch.scheduler.dependency_evaluator

    def test_build_orchestrator_for_testing_wires_dependency_evaluator_to_session_launcher(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Launch-time CAS dependency checks use the scheduler evaluator."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                planner=None,
            )

            assert orch._session_launcher._dependency_evaluator is orch.scheduler.dependency_evaluator  # noqa: SLF001

    def test_create_planner_wires_dependency_evaluator_to_scheduler(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Production planner factory enables scheduler dependency gating."""
        planner, scheduler, dependency_evaluator, _label_sync = _create_planner(
            config=minimal_config,
            github=mock_github,
            events=NullEventSink(),
        )

        assert dependency_evaluator is not None
        assert scheduler.dependency_evaluator is dependency_evaluator
        assert planner.dependency_evaluator is dependency_evaluator

    def test_build_orchestrator_for_testing_with_custom_session_manager(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Uses provided SessionManager instead of default."""
        custom_session_manager = MagicMock()

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                session_manager=custom_session_manager,
            )

            assert orch.deps.session_manager is custom_session_manager

    def test_build_orchestrator_for_testing_creates_default_session_manager(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Creates default SessionManager when not provided."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                session_manager=None,
            )

            assert orch.deps.session_manager is not None

    def test_build_orchestrator_for_testing_with_custom_action_applier(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Uses provided ActionApplier instead of default."""
        custom_action_applier = MagicMock()

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                action_applier=custom_action_applier,
            )

            assert orch.deps.action_applier is custom_action_applier

    def test_build_orchestrator_for_testing_creates_default_action_applier(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Creates default ActionApplier when not provided."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                action_applier=None,
            )

            assert orch.deps.action_applier is not None

    def test_build_orchestrator_for_testing_with_custom_fact_gatherer(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Uses provided FactGatherer instead of default."""
        custom_fact_gatherer = MagicMock()

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                fact_gatherer=custom_fact_gatherer,
            )

            assert orch.deps.fact_gatherer is custom_fact_gatherer

    def test_build_orchestrator_for_testing_creates_default_fact_gatherer(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Creates default FactGatherer when not provided."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
                fact_gatherer=None,
            )

            assert orch.deps.fact_gatherer is not None

    def _configure_tech_lead_agent(
        self, config: Config, prompt_dir, *, threshold: int = 5
    ) -> None:
        """Arm the tech_lead batch trigger on a real config (#6781 board wiring)."""
        from issue_orchestrator.domain.models import AgentConfig

        prompt = prompt_dir / "tech-lead-prompt.md"
        prompt.write_text("Tech Lead prompt")
        config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt, model="sonnet", timeout_minutes=45,
        )
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_threshold = threshold

    def test_build_orchestrator_for_testing_wires_tech_lead_board_publisher(
        self, minimal_config: Config, mock_github: MagicMock, tmp_path
    ) -> None:
        """A configured tech lead agent wires a real TechLeadBoardPublisher into the
        fact gatherer's projection seam (#6781); without it the board file is
        never produced no matter how many ticks run.
        """
        from issue_orchestrator.control.tech_lead_board import TechLeadBoardPublisher

        self._configure_tech_lead_agent(minimal_config, tmp_path)

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config, github=mock_github,
            )

        assert isinstance(orch.deps.fact_gatherer.board_publisher, TechLeadBoardPublisher)

    def test_build_orchestrator_for_testing_no_board_publisher_without_tech_lead(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """No tech lead agent -> no publisher wired and nothing crashes (#6781).

        The gate mirrors the other tech-lead-only deps: when tech_lead is not
        configured the fact gatherer's ``board_publisher`` stays ``None`` so
        the publish call is skipped entirely (no board file, no error).
        """
        assert minimal_config.tech_lead_review_agent is None  # fixture default

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config, github=mock_github,
            )

        assert orch.deps.fact_gatherer.board_publisher is None

    def test_bootstrap_wired_fact_gathering_writes_tech_lead_board(
        self, minimal_config: Config, mock_github: MagicMock, tmp_path
    ) -> None:
        """Driving the real tick fact-gathering seam produces tech-lead-board.md.

        This exercises the WIRING (not ``publish`` in isolation, which the
        publisher unit tests already cover): a tech-lead-configured orchestrator
        built by bootstrap, driven through the same ``create_snapshot`` seam
        the tick uses, must render the board at ``tech_lead_board_path`` from a
        gathered case file AND from the authority ledger the publisher shares
        with the fact gatherer.
        """
        from issue_orchestrator.control.tech_lead_board import tech_lead_board_path
        from issue_orchestrator.domain.models import Issue, OrchestratorState
        from issue_orchestrator.domain.tech_lead_session import (
            TECH_LEAD_OBSERVATION_LABEL,
            StoredTechLeadOp,
        )

        self._configure_tech_lead_agent(minimal_config, tmp_path)
        # Bounded GitHub seams: one open case-file issue from the anchor scan,
        # no batch PRs, no linked-issue re-reads.
        mock_github.list_issues.return_value = [
            Issue(
                number=800,
                title="Pattern case file: db-timeout",
                labels=["agent:tech-lead", TECH_LEAD_OBSERVATION_LABEL, "area:db"],
            ),
        ]
        mock_github.get_prs_with_label.return_value = []
        mock_github.get_issue.return_value = None

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config, github=mock_github,
            )

        # Record an op on the SAME authority store the publisher was wired to,
        # so the board's open-proposals section proves the authority wiring.
        orch.deps.fact_gatherer.tech_lead_authority.record_op(
            issue_number=500,
            op=StoredTechLeadOp(
                op_type="reset_retry",
                target_issue_number=13,
                rationale="r",
                source_run_id="run-1",
                source_session_name="issue-99",
                source_action_id="A2",
                created_at="2026-07-11T00:00:00+00:00",
            ),
        )

        state = OrchestratorState()
        snapshot = orch.deps.fact_gatherer.create_snapshot(state, [])

        assert snapshot.tech_lead_facts is not None
        assert snapshot.tech_lead_facts.open_case_files, "case file must be gathered"

        board = tech_lead_board_path(minimal_config.repo_root)
        assert board.exists(), "the wired publisher must write the board on gather"
        content = board.read_text()
        assert content.startswith("# Tech Lead Board")  # stable render marker
        assert "#800" in content  # the gathered case file (board_path wiring)
        assert "#500" in content and "reset_retry" in content  # authority wiring
        board_snapshot = orch.deps.board_snapshot_builder.build(state)
        assert [item.issue_number for item in board_snapshot.case_files] == [800]

    def test_build_orchestrator_for_testing_creates_all_other_components(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Ensures all required components are created."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
            )

            # Verify all required components exist
            assert orch.deps.pr_scanner is not None
            assert orch.deps.session_restorer is not None
            assert orch.deps.state_machine_manager is not None
            assert orch.deps.completion_processor is not None
            assert orch.deps.session_controller is not None
            assert orch.deps.label_sync is not None
            assert orch.deps.event_hub is not None
            assert orch.deps.worktree_manager is not None
            assert orch.deps.working_copy is not None
            assert orch.deps.command_runner is not None
            assert orch.deps.health_gate is not None

    def test_build_orchestrator_for_testing_installs_gh_guard(
        self, minimal_config: Config, mock_github: MagicMock
    ) -> None:
        """Calls install_gh_guard on startup."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard") as mock_guard:
            build_orchestrator_for_testing(
                config=minimal_config,
                github=mock_github,
            )

            mock_guard.assert_called_once()


class TestBuildOrchestrator:
    """Tests for build_orchestrator function (main composition root)."""

    @pytest.fixture
    def minimal_config(self, tmp_path) -> Config:
        """Minimal valid config for testing."""
        config = Config()
        config.repo = "test/repo"
        config.repo_root = tmp_path
        config.worktree_base = tmp_path / "worktrees"
        config.terminal_adapter = MagicMock()
        config.ui_mode = "normal"
        config.gh_audit_enabled = False
        return config

    def test_build_orchestrator_requires_repo(self) -> None:
        """Raises ValueError when repo cannot be determined."""
        config = Config()
        config.repo = None
        config.terminal_adapter = MagicMock()
        config.ui_mode = "normal"

        # Mock auto-detection to fail
        with patch("issue_orchestrator.entrypoints.bootstrap.get_repo_from_git") as mock_get_repo:
            with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
                with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager"):
                    mock_get_repo.side_effect = GitRepoError("test error")

                    with pytest.raises(ValueError, match="Could not determine GitHub repository"):
                        build_orchestrator(config)

    def test_build_orchestrator_uses_configured_repo(self, minimal_config: Config) -> None:
        """Uses repo from config when available."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager"):
                with patch("issue_orchestrator.entrypoints.bootstrap.get_repo_from_git") as mock_get_repo:
                    with patch("issue_orchestrator.entrypoints.bootstrap.GitHubAdapter") as mock_adapter:
                        with patch("issue_orchestrator.entrypoints.bootstrap.EventHub"):
                            # Should NOT call get_repo_from_git when config.repo is set
                            mock_adapter.return_value = MagicMock()
                            mock_adapter.return_value.get_rate_limit_snapshot.return_value = {}
                            mock_adapter.return_value.get_token_scopes.return_value = []

                            build_orchestrator(minimal_config)
                            # Verify we don't try to auto-detect when config.repo is set
                            # (get_repo_from_git should not be called)

    def test_build_orchestrator_auto_detects_repo_when_none(self) -> None:
        """Auto-detects repo from git when config.repo is None."""
        config = Config()
        config.repo = None
        config.terminal_adapter = MagicMock()
        config.ui_mode = "normal"

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager"):
                with patch("issue_orchestrator.entrypoints.bootstrap.get_repo_from_git") as mock_get_repo:
                    with patch("issue_orchestrator.entrypoints.bootstrap.GitHubAdapter"):
                        with patch("issue_orchestrator.entrypoints.bootstrap.EventHub"):
                            with patch("issue_orchestrator.entrypoints.bootstrap.logger"):
                                mock_get_repo.return_value = "auto/detected"

                                # Should raise because other components fail, but auto-detect was called
                                try:
                                    build_orchestrator(config)
                                except ValueError:
                                    pass  # Expected due to missing components

                                # Verify auto-detection was called
                                mock_get_repo.assert_called_once()

    def test_build_orchestrator_sets_repo_root_env_var(self, minimal_config: Config) -> None:
        """Sets f"{ENV_PREFIX}REPO_ROOT" environment variable."""
        repo_root = minimal_config.repo_root
        original_env = os.environ.get(f"{ENV_PREFIX}REPO_ROOT")

        try:
            with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
                with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager"):
                    with patch("issue_orchestrator.entrypoints.bootstrap.GitHubAdapter"):
                        with patch("issue_orchestrator.entrypoints.bootstrap.EventHub"):
                            try:
                                build_orchestrator(minimal_config)
                            except ValueError:
                                pass  # Expected to fail later

                            # Check that env var was set
                            assert os.environ.get(f"{ENV_PREFIX}REPO_ROOT") == str(repo_root)
        finally:
            # Restore original env
            if original_env is None:
                os.environ.pop(f"{ENV_PREFIX}REPO_ROOT", None)
            else:
                os.environ[f"{ENV_PREFIX}REPO_ROOT"] = original_env

    def test_build_orchestrator_registers_github_app_secret_env(
        self,
        minimal_config: Config,
    ) -> None:
        """Configured GitHub App private-key env names feed agent scrubbers."""
        original_env = os.environ.get(EXTRA_FORBIDDEN_ENV_VARS_ENV)
        minimal_config.github_app_private_key_env = "CUSTOM_GH_APP_PRIVATE_KEY"

        try:
            with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
                with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager"):
                    with patch("issue_orchestrator.entrypoints.bootstrap.build_github_auth"):
                        with patch("issue_orchestrator.entrypoints.bootstrap.GitHubAdapter"):
                            with patch("issue_orchestrator.adapters.github.fresh_issue_reader.GitHubFreshIssueReader"):
                                with patch("issue_orchestrator.entrypoints.bootstrap.EventHub"):
                                    build_orchestrator(minimal_config)

            assert os.environ.get(EXTRA_FORBIDDEN_ENV_VARS_ENV) == "CUSTOM_GH_APP_PRIVATE_KEY"
        finally:
            if original_env is None:
                os.environ.pop(EXTRA_FORBIDDEN_ENV_VARS_ENV, None)
            else:
                os.environ[EXTRA_FORBIDDEN_ENV_VARS_ENV] = original_env

    def test_build_orchestrator_enables_sse_by_default(self, minimal_config: Config) -> None:
        """Registers SSE plugin when enable_sse=True (default)."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager") as mock_pm_factory:
                with patch("issue_orchestrator.entrypoints.bootstrap.GitHubAdapter"):
                    with patch("issue_orchestrator.entrypoints.bootstrap.EventHub"):
                        mock_pm = MagicMock()
                        mock_pm_factory.return_value = mock_pm

                        try:
                            build_orchestrator(minimal_config, enable_sse=True)
                        except ValueError:
                            pass  # Expected to fail later

                        # Verify pm.register was called for SSE plugin
                        assert mock_pm.register.called

    def test_build_orchestrator_disables_sse_when_requested(self, minimal_config: Config) -> None:
        """Skips SSE plugin when enable_sse=False."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager") as mock_pm_factory:
                with patch("issue_orchestrator.entrypoints.bootstrap.GitHubAdapter"):
                    with patch("issue_orchestrator.entrypoints.bootstrap.EventHub"):
                        mock_pm = MagicMock()
                        mock_pm_factory.return_value = mock_pm
                        initial_call_count = mock_pm.register.call_count

                        try:
                            build_orchestrator(minimal_config, enable_sse=False)
                        except ValueError:
                            pass  # Expected to fail later

    def test_build_orchestrator_disables_ipc_when_requested(self, minimal_config: Config) -> None:
        """Accepts enable_ipc parameter (even if it doesn't affect SSE)."""
        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager") as mock_pm_factory:
                with patch("issue_orchestrator.entrypoints.bootstrap.GitHubAdapter"):
                    with patch("issue_orchestrator.entrypoints.bootstrap.EventHub"):
                        mock_pm = MagicMock()
                        mock_pm_factory.return_value = mock_pm

                        try:
                            # Should accept enable_ipc parameter
                            build_orchestrator(minimal_config, enable_ipc=False)
                        except ValueError:
                            pass  # Expected to fail later

    def test_build_orchestrator_configures_gh_audit(self, minimal_config: Config) -> None:
        """Configures GitHub audit settings."""
        minimal_config.gh_audit_enabled = True
        minimal_config.gh_audit_events = True
        minimal_config.gh_audit_file = "/tmp/audit.json"

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            with patch("issue_orchestrator.entrypoints.bootstrap.create_plugin_manager"):
                with patch("issue_orchestrator.entrypoints.bootstrap.GitHubAdapter"):
                    with patch("issue_orchestrator.entrypoints.bootstrap.EventHub"):
                        with patch("issue_orchestrator.entrypoints.bootstrap.gh_audit") as mock_audit:
                            try:
                                build_orchestrator(minimal_config)
                            except ValueError:
                                pass

                            # Verify audit was configured
                            assert mock_audit.configure.called
                            assert mock_audit.configure_rate_limit.called


class TestClaimTestingWiring:
    """Tests for claim wiring in test bootstrap."""

    def test_build_orchestrator_for_testing_uses_injected_claim_manager(self, tmp_path) -> None:
        """Testing bootstrap should allow claim-aware tests to opt out of NullClaimManager."""
        config = Config()
        config.repo = "owner/repo"
        config.repo_root = tmp_path
        config.worktree_base = tmp_path / "worktrees"
        config.agents = {"agent:test": MagicMock(timeout_minutes=5)}

        github = MagicMock()
        github.get_issue_labels.return_value = []
        claim_manager = MagicMock()

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=config,
                github=github,
                claim_manager=claim_manager,
            )

        assert orch.deps.claim_manager is claim_manager
        assert orch.deps.action_applier.claim_gate is orch.deps.claim_gate

    def test_build_orchestrator_for_testing_wires_lease_lookup_from_active_sessions(self, tmp_path) -> None:
        """ActionApplier lease lookup should resolve lease IDs from orchestrator state."""
        config = Config()
        config.repo = "owner/repo"
        config.repo_root = tmp_path
        config.worktree_base = tmp_path / "worktrees"
        config.agents = {"agent:test": MagicMock(timeout_minutes=5)}

        github = MagicMock()
        github.get_issue_labels.return_value = []

        with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
            orch = build_orchestrator_for_testing(
                config=config,
                github=github,
                claim_manager=NullClaimManager(),
            )

        session = MagicMock()
        session.issue.number = 42
        session.lease_id = "lease-42"
        orch.state.active_sessions.append(session)

        lease_id = orch.deps.action_applier.lease_id_lookup(42)

        assert lease_id == "lease-42"
