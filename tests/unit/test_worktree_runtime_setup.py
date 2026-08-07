"""Tests for the worktree runtime setup owner.

These pin two things the lifecycle module used to own inline:

1. Applying setup to a worktree produces a *complete* runnable state, and says
   so through a typed result rather than leaving callers to infer it.
2. The failure semantics of each step. Runtime setup used to degrade silently
   (a phantom worktree identity, a dropped ``--no-verify`` flag, a settings
   file replaced without a word), which turned a broken worktree into a
   confusing session failure much later.
"""

import json
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import (
    WorktreeError,
    WorktreeRuntimeSetup,
    install_claude_settings,
)
from issue_orchestrator.adapters.worktree._worktree_runtime import (
    ALLOW_NO_VERIFY_DRY_RUN_PATH,
    CLAUDE_SETTINGS_FOR_AGENTS,
    WORKTREE_ID_MARKER,
)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    return root


@pytest.fixture
def worktree_path(tmp_path: Path, repo_root: Path) -> Path:
    """A worktree linked to ``repo_root`` the way ``git worktree add`` links it."""
    path = tmp_path / "repo-123"
    path.mkdir()
    gitdir = repo_root / ".git" / "worktrees" / "repo-123"
    gitdir.mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {gitdir}")
    return path


def _setup(repo_root: Path, **overrides) -> WorktreeRuntimeSetup:
    options = {"enforce_hooks": False}
    options.update(overrides)
    return WorktreeRuntimeSetup(repo_root=repo_root, **options)


class TestApplyProducesRunnableWorktree:
    """One call must leave the worktree ready for an agent session."""

    def test_apply_installs_every_runtime_artifact(self, repo_root, worktree_path):
        state = _setup(repo_root).apply(worktree_path)

        assert (worktree_path / ".claude" / "settings.json").exists()
        assert (worktree_path / WORKTREE_ID_MARKER).read_text() == state.worktree_id
        assert (worktree_path / ".venv").is_symlink()
        assert (worktree_path / ".venv").resolve() == (repo_root / ".venv").resolve()
        assert state.synced_cli_tool_paths
        for relative in state.synced_cli_tool_paths:
            assert (worktree_path / relative).exists()

    def test_apply_hides_runtime_artifacts_from_git_status(
        self, repo_root, worktree_path
    ):
        state = _setup(repo_root).apply(worktree_path)

        exclude_text = (
            repo_root / ".git" / "worktrees" / "repo-123" / "info" / "exclude"
        ).read_text()
        assert ".claude/settings.json" in exclude_text
        assert WORKTREE_ID_MARKER in exclude_text
        assert str(state.synced_cli_tool_paths[0]) in exclude_text

    def test_apply_reports_what_it_did(self, repo_root, worktree_path):
        state = _setup(
            repo_root, enforce_hooks=False, allow_no_verify_dry_run_preflight=True
        ).apply(worktree_path)

        assert state.worktree_path == worktree_path
        assert state.worktree_id.startswith("wt-")
        assert state.hooks_installed is False
        assert state.no_verify_dry_run_allowed is True

    def test_apply_is_idempotent_and_keeps_worktree_identity(
        self, repo_root, worktree_path
    ):
        setup = _setup(repo_root)

        first = setup.apply(worktree_path)
        second = setup.apply(worktree_path)

        assert second.worktree_id == first.worktree_id

    def test_hooks_are_installed_when_enforced(self, repo_root, worktree_path):
        state = WorktreeRuntimeSetup(repo_root=repo_root, enforce_hooks=True).apply(
            worktree_path
        )

        assert state.hooks_installed is True
        installed = repo_root / ".git" / "worktrees" / "repo-123" / "hooks" / "pre-push"
        assert installed.exists()

    def test_hooks_are_skipped_when_not_enforced(self, repo_root, worktree_path):
        state = WorktreeRuntimeSetup(repo_root=repo_root, enforce_hooks=False).apply(
            worktree_path
        )

        assert state.hooks_installed is False
        assert not (
            repo_root / ".git" / "worktrees" / "repo-123" / "hooks" / "pre-push"
        ).exists()


class TestNoVerifyDryRunFlag:
    """The flag gates a hook bypass, so both directions must actually land."""

    def test_flag_is_written_when_preflight_allows_no_verify(
        self, repo_root, worktree_path
    ):
        _setup(repo_root, allow_no_verify_dry_run_preflight=True).apply(worktree_path)

        assert (worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH).exists()

    def test_stale_flag_is_cleared_when_preflight_disallows_no_verify(
        self, repo_root, worktree_path
    ):
        stale = worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH
        stale.parent.mkdir(parents=True)
        stale.write_text("allow\n")

        _setup(repo_root, allow_no_verify_dry_run_preflight=False).apply(worktree_path)

        assert not stale.exists()

    def test_unwritable_flag_fails_setup_instead_of_leaving_it_ambiguous(
        self, repo_root, worktree_path
    ):
        # A file where the runtime directory belongs makes every write under it
        # fail; setup must surface that rather than run with an unknown flag state.
        (worktree_path / ".issue-orchestrator").write_text("not a directory")

        with pytest.raises(WorktreeError, match="no-verify dry-run flag"):
            _setup(repo_root, allow_no_verify_dry_run_preflight=True).apply(
                worktree_path
            )


class TestWorktreeIdentityFailureSemantics:
    """A worktree identity nobody can read back is worse than no worktree."""

    def test_unpersistable_identity_fails_setup(self, repo_root, worktree_path):
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.mkdir()  # a directory where the marker file belongs

        with pytest.raises(WorktreeError, match="worktree identity"):
            _setup(repo_root).apply(worktree_path)

    def test_empty_identity_marker_is_regenerated(self, repo_root, worktree_path):
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text("   \n")

        state = _setup(repo_root).apply(worktree_path)

        assert state.worktree_id.startswith("wt-")
        assert marker.read_text() == state.worktree_id


class TestInstallClaudeSettingsFailureSemantics:
    """The Stop hook is a completion guardrail; installing it cannot half-fail."""

    def _stop_hook_commands(self, settings_file: Path) -> list[str]:
        settings = json.loads(settings_file.read_text())
        return [hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]]

    def test_corrupt_settings_are_replaced_with_the_enforced_hook(
        self, tmp_path, caplog
    ):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text("{not json")

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "Replacing unreadable Claude settings" in caplog.text

    def test_wrong_shaped_hooks_are_replaced_rather_than_crashing(
        self, tmp_path, caplog
    ):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"hooks": ["not-an-object"]}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-object 'hooks'" in caplog.text

    def test_non_list_stop_hooks_are_replaced(self, tmp_path, caplog):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"hooks": {"Stop": "nope"}}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-list 'hooks.Stop'" in caplog.text

    def test_repeated_installs_do_not_duplicate_the_stop_hook(self, tmp_path):
        install_claude_settings(tmp_path)
        install_claude_settings(tmp_path)

        commands = self._stop_hook_commands(tmp_path / ".claude" / "settings.json")
        assert len(commands) == 1

    def test_existing_operator_settings_survive_the_merge(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]
                    },
                }
            )
        )

        install_claude_settings(tmp_path)

        settings = json.loads(settings_file.read_text())
        assert settings["model"] == "opus"
        assert "echo mine" in self._stop_hook_commands(settings_file)
        assert len(settings["hooks"]["Stop"]) == 2

    def test_install_does_not_mutate_the_shared_settings_template(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "echo mine"}]}]}})
        )

        install_claude_settings(tmp_path)

        assert len(CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"]) == 1

    def test_unwritable_settings_fail_setup(self, tmp_path):
        (tmp_path / ".claude").write_text("not a directory")

        with pytest.raises(WorktreeError, match="Claude settings"):
            install_claude_settings(tmp_path)
