"""Unit tests for doctor hook checks."""

from datetime import datetime, timedelta, timezone
import subprocess
from unittest.mock import MagicMock

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import hooks as hook_checks
from issue_orchestrator.infra.ai_gate_state import AiGateState, AiGateResult
from issue_orchestrator.infra.repo_guardrails import (
    MANAGED_PRE_PUSH_MARKER,
    setup_repo_guardrails,
)
from issue_orchestrator.domain.models import AgentConfig


def test_check_hook_verification_no_agents_reports_ai_agent_check():
    config = Config()
    config.agents = {}

    checks = hook_checks.check_hook_verification(config)

    assert any(
        c.name == "AI Agent Hooks (Installation)" and c.status == "warning"
        for c in checks
    )


def test_check_repo_guardrails_warns_when_not_installed(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    config = Config(repo_root=tmp_path)
    config.validation.publish.cmd = "make validate-pr"

    checks = hook_checks.check_repo_guardrails(config)

    assert checks == [
        hook_checks.Check(
            name="Repo Guardrails",
            status="warning",
            detail="Not installed. Run 'issue-orchestrator setup-guardrails'.",
        )
    ]


def test_check_repo_guardrails_reports_ok_after_install(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    config = Config(repo_root=tmp_path)
    config.validation.publish.cmd = "make validate-pr"
    config.agents = {}

    setup_repo_guardrails(config)

    checks = hook_checks.check_repo_guardrails(config)

    assert len(checks) == 1
    assert checks[0].name == "Repo Guardrails"
    assert checks[0].status == "ok"
    assert "scripts/verify-pr.sh" in checks[0].detail


def test_check_repo_guardrails_reports_managed_ai_hooks_after_install(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    config = Config(repo_root=tmp_path)
    config.validation.publish.cmd = "make validate-pr"
    config.agents = {
        "agent:dev": AgentConfig(
            prompt_path=tmp_path / "prompt.md",
            command="claude --print",
        )
    }

    setup_repo_guardrails(config)

    checks = hook_checks.check_repo_guardrails(config)

    assert len(checks) == 1
    assert checks[0].status == "ok"
    assert "managed AI hooks: claude-code" in checks[0].detail


def test_check_repo_guardrails_reports_drifted_managed_ai_hook(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    config = Config(repo_root=tmp_path)
    config.validation.publish.cmd = "make validate-pr"
    config.agents = {
        "agent:dev": AgentConfig(
            prompt_path=tmp_path / "prompt.md",
            command="claude --print",
        )
    }

    setup_repo_guardrails(config)
    drifted_hook = tmp_path / ".claude" / "hooks" / "block-no-verify.sh"
    drifted_hook.write_text("#!/usr/bin/env bash\necho drifted\n")
    drifted_hook.chmod(0o755)

    checks = hook_checks.check_repo_guardrails(config)

    assert len(checks) == 1
    assert checks[0].status == "error"
    assert ".claude/hooks/block-no-verify.sh" in checks[0].detail


def test_check_repo_guardrails_reports_baked_mode_selection_drift(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    default_path = tmp_path / ".issue-orchestrator/config/modes/default/main.yaml"
    codex_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    default_path.parent.mkdir(parents=True)
    codex_path.parent.mkdir(parents=True)
    yaml_text = "validation:\n  publish:\n    cmd: make validate-pr\nagents: {}\n"
    default_path.write_text(yaml_text)
    codex_path.write_text(yaml_text)
    default_config = Config.load(default_path)
    codex_config = Config.load(codex_path)

    setup_repo_guardrails(default_config)
    checks = hook_checks.check_repo_guardrails(codex_config)

    assert checks[0].status == "error"
    assert "configuration selection drifted" in checks[0].detail


class TestAiGate:
    """Tests for _check_ai_gate_report function."""

    def test_ai_gate_disabled_returns_none(self, tmp_path):
        """Test that disabled AI gate test returns None."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 0

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is None

    def test_ai_gate_skipped_when_hooks_not_ok(self, tmp_path):
        """Test AI gate test skipped when hooks not installed."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=False,
        )

        assert result is not None
        assert result.status == "info"
        assert "Skipped" in result.detail

    def test_ai_gate_fresh_shows_previous_results(self, tmp_path, monkeypatch):
        """Test that fresh AI gate test shows previous results."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        # Mock load_ai_gate_state to return recent check (3 days ago)
        recent_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=3),
            last_results={
                "claude-code": AiGateResult(
                    success=True,
                    message="Blocked git push",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=3),
                ),
            },
        )
        # Patch at the module where the function is imported/used
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: recent_state,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "ok"
        assert "Passed" in result.detail
        # Verify days_ago calculation is correct (should be 3, not 0)
        assert "3d ago" in result.detail
        assert result.expandable is not None
        assert result.expandable["ran"] is False

    def test_ai_gate_cached_failure_retries(self, tmp_path, monkeypatch):
        """Cached failures are not trusted — the gate re-runs instead of blocking."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7
        config.hooks.ai_gate.dangerous_allow_failure = False

        # Mock load_ai_gate_state to return recent check with failure
        recent_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=2),
            last_results={
                "claude-code": AiGateResult(
                    success=False,
                    message="Did not block dangerous command",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=2),
                ),
            },
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: recent_state,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda *a, **kw: None,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        # With no agent types to test, re-run produces ok (0 failures)
        assert result.status == "ok"
        assert result.expandable["ran"] is True

    def test_ai_gate_cached_failure_retries_even_when_allowed(
        self, tmp_path, monkeypatch
    ):
        """Cached failures always re-run, even with dangerous_allow_failure=True."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7
        config.hooks.ai_gate.dangerous_allow_failure = True

        recent_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=2),
            last_results={
                "claude-code": AiGateResult(
                    success=False,
                    message="Did not block",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=2),
                ),
            },
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: recent_state,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda *a, **kw: None,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        # Re-ran with no agent types → ok
        assert result.status == "ok"
        assert result.expandable["ran"] is True

    def test_ai_gate_stale_runs_test_ai_gate(self, tmp_path, monkeypatch):
        """Test that stale AI gate test runs AI gate test."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        # Mock load_ai_gate_state to return stale check
        old_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=10),
            last_results={},
            required_agent_types=("claude-code",),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: old_state,
        )

        # Mock save_ai_gate_state
        saved_states = []
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda path, state: saved_states.append(state),
        )

        # Mock get_adapter to return an adapter with successful test_ai_gate
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked git push --no-verify")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "ok"
        assert result.expandable is not None
        assert result.expandable["ran"] is True
        assert result.expandable["triggered_by"] == "interval exceeded"
        assert len(saved_states) == 1  # State was saved

    def test_ai_gate_stale_failed_cache_reports_interval_exceeded(
        self, tmp_path, monkeypatch
    ):
        """Stale failed results should report staleness, not cached-failure retry."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        old_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=10),
            last_results={
                "claude-code": AiGateResult(
                    success=False,
                    message="Did not block",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=10),
                ),
            },
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: old_state,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda *a, **kw: None,
        )

        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked git push --no-verify")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "ok"
        assert result.expandable is not None
        assert result.expandable["ran"] is True
        assert result.expandable["triggered_by"] == "interval exceeded"

    def test_ai_gate_failure_blocks_by_default(self, tmp_path, monkeypatch):
        """Test that AI gate test failure blocks when not allowed."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7
        config.hooks.ai_gate.dangerous_allow_failure = False

        # Return stale state
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: AiGateState(),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda p, s: None,
        )

        # Mock adapter that fails AI gate test
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (
            False,
            "Claude is not authenticated; run 'claude auth login'\n"
            "Verify with 'claude auth status', then retry startup.",
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "error"
        assert result.detail == (
            "Failed: claude-code: Claude is not authenticated; run 'claude auth login'"
        )
        assert result.expandable is not None
        assert (
            "claude auth status"
            in result.expandable["results"]["claude-code"]["message"]
        )

    def test_ai_gate_failure_warns_when_allowed(self, tmp_path, monkeypatch):
        """Test that AI gate test failure only warns when dangerous_allow_failure=True."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7
        config.hooks.ai_gate.dangerous_allow_failure = True

        # Return stale state
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: AiGateState(),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda p, s: None,
        )

        # Mock adapter that fails AI gate test
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (False, "Did not block")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "warning"
        assert "allowed by config" in result.detail

    def test_ai_gate_first_run(self, tmp_path, monkeypatch):
        """Test AI gate test triggers on first run."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        # Return empty state (first run)
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: AiGateState(),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda p, s: None,
        )

        # Mock successful adapter
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.expandable["ran"] is True
        assert result.expandable["triggered_by"] == "first run"

    def test_ai_gate_expandable_details(self, tmp_path, monkeypatch):
        """Test expandable details are populated correctly."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: AiGateState(),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda p, s: None,
        )

        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked git push --no-verify")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result.expandable is not None
        assert "claude-code" in result.expandable["agents_tested"]
        assert result.expandable["results"]["claude-code"]["success"] is True
        assert "Blocked" in result.expandable["results"]["claude-code"]["message"]

    def test_cached_failure_retry_clears_stale_results(self, tmp_path, monkeypatch):
        """Retry path must clear old cached results so expandable only shows fresh data."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        # Cached state: gemini failed, claude passed
        recent_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=2),
            last_results={
                "claude-code": AiGateResult(
                    success=True,
                    message="Blocked",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=2),
                ),
                "gemini-cli": AiGateResult(
                    success=False,
                    message="Did not block",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=2),
                ),
            },
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: recent_state,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda *a, **kw: None,
        )

        # Re-run with only claude-code → gemini stale result must not appear
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result.expandable["ran"] is True
        assert "gemini-cli" not in result.expandable["results"]
        assert "claude-code" in result.expandable["results"]
        assert result.expandable["results"]["claude-code"]["success"] is True

    def test_cached_failure_retry_reports_correct_trigger(self, tmp_path, monkeypatch):
        """Retry due to cached failure must say so, not 'interval exceeded'."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        recent_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=1),
            last_results={
                "claude-code": AiGateResult(
                    success=False,
                    message="Did not block",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=1),
                ),
            },
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: recent_state,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda *a, **kw: None,
        )

        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result.expandable["ran"] is True
        assert "cached failure" in result.expandable["triggered_by"]
        assert "claude-code" in result.expandable["triggered_by"]


def test_check_worktree_hook_corruption_clean_repo_reports_ok(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    config = Config(repo_root=tmp_path)

    checks = hook_checks.check_worktree_hook_corruption(config)

    assert len(checks) == 1
    assert checks[0].name == "Pre-push Hook Corruption"
    assert checks[0].status == "ok"


def test_check_worktree_hook_corruption_flags_managed_project_hook(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    corrupt = hooks_dir / "pre-push.project"
    corrupt.write_text(f"#!/usr/bin/env bash\n# {MANAGED_PRE_PUSH_MARKER}\n")
    corrupt.chmod(0o755)
    config = Config(repo_root=tmp_path)

    checks = hook_checks.check_worktree_hook_corruption(config)

    assert len(checks) == 1
    assert checks[0].status == "error"
    assert "pre-push.project" in checks[0].detail
    assert "setup-guardrails" in checks[0].detail


def test_check_worktree_hook_corruption_scans_worktree_hook_dirs(tmp_path):
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    subprocess.run(["git", "init"], cwd=main_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    (main_repo / "file").write_text("seed\n")
    subprocess.run(
        ["git", "add", "file"], cwd=main_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    worktree_path = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "feature"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    wt_hooks = main_repo / ".git" / "worktrees" / "wt" / "hooks"
    wt_hooks.mkdir(parents=True, exist_ok=True)
    (wt_hooks / "pre-push.project").write_text(f"# {MANAGED_PRE_PUSH_MARKER}\n")

    config = Config(repo_root=main_repo)

    checks = hook_checks.check_worktree_hook_corruption(config)

    assert checks[0].status == "error"
    assert "worktrees/wt/hooks/pre-push.project" in checks[0].detail
