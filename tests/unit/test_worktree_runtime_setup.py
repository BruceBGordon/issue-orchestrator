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
from typing import Any

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


def _break_read_of(
    monkeypatch: pytest.MonkeyPatch, target: Path, error: OSError
) -> None:
    """Make exactly one path fail to read while its bytes stay intact on disk.

    This is the case the owner has to tell apart from "the content is broken",
    and it cannot be staged with ``chmod`` alone — a root test runner reads a
    ``0o000`` file happily, so the test would pass for the wrong reason there
    and fail for the wrong reason here. Only ``target`` is affected; every other
    read in the setup sequence runs for real.
    """
    original_read_text = Path.read_text

    def guarded(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise error
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)


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


class TestEnforcedHooksAreAnInvariantNotARequest:
    """``hooks_installed`` must be an observed outcome, never the input echoed.

    A worktree that reports enforced guardrails while having none is worse than
    one that fails to be created: the session runs and can push past validation.
    """

    def test_enforced_hooks_that_cannot_be_installed_fail_setup(
        self, repo_root, worktree_path, tmp_path
    ):
        missing_hook = tmp_path / "nonexistent-pre-push"

        with pytest.raises(WorktreeError, match="pre-push hook was installed"):
            WorktreeRuntimeSetup(
                repo_root=repo_root,
                enforce_hooks=True,
                pre_push_hook=missing_hook,
            ).apply(worktree_path)

        assert not (
            repo_root / ".git" / "worktrees" / "repo-123" / "hooks" / "pre-push"
        ).exists()

    def test_enforced_hooks_fail_when_the_worktree_has_no_git_link(
        self, repo_root, tmp_path
    ):
        # No ``.git`` link means hook installation has nowhere to write; the
        # old owner reported success anyway.
        detached = tmp_path / "detached"
        detached.mkdir()

        with pytest.raises(WorktreeError, match="pre-push hook was installed"):
            WorktreeRuntimeSetup(repo_root=repo_root, enforce_hooks=True).apply(detached)


class TestOwnerErrorBoundary:
    """``WorktreeError`` is the owner's whole failure surface."""

    def test_step_failures_are_translated_to_worktree_error(
        self, repo_root, worktree_path
    ):
        # A file where git's ``info/`` directory belongs makes the exclude
        # write raise a bare OSError from inside a composed step.
        (repo_root / ".git" / "worktrees" / "repo-123" / "info").write_text(
            "not a directory"
        )

        with pytest.raises(WorktreeError, match="Worktree runtime setup failed"):
            _setup(repo_root).apply(worktree_path)


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

    def test_non_utf8_identity_marker_is_regenerated(self, repo_root, worktree_path):
        # Undecodable bytes carry no identity, so replacing them loses nothing.
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"\xff\xfe not an id")

        state = _setup(repo_root).apply(worktree_path)

        assert state.worktree_id.startswith("wt-")
        assert marker.read_text() == state.worktree_id

    def test_unreadable_identity_marker_fails_without_reissuing_the_identity(
        self, repo_root, worktree_path, monkeypatch
    ):
        # A read that fails is not evidence the identity is gone. Regenerating
        # would tell every job holding "wt-original" that its worktree was
        # replaced underneath it.
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text("wt-original")
        _break_read_of(monkeypatch, marker, PermissionError("simulated read failure"))

        with pytest.raises(WorktreeError, match="read worktree identity marker"):
            _setup(repo_root).apply(worktree_path)

        assert marker.read_bytes() == b"wt-original"


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

    def test_null_hooks_are_replaced_rather_than_crashing(self, tmp_path, caplog):
        # Regression: an explicit JSON ``null`` used to be indistinguishable
        # from a missing key, so the merge tried to ``setdefault`` into None
        # and raised AttributeError out of worktree setup.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"model": "opus", "hooks": None}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-object 'hooks'" in caplog.text

    def test_missing_hooks_key_preserves_operator_settings(self, tmp_path):
        # The counterpart to the null case: absent really is absent, and an
        # operator's unrelated settings must survive the merge.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}})
        )

        install_claude_settings(tmp_path)

        settings = json.loads(settings_file.read_text())
        assert settings["model"] == "opus"
        assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
        assert self._stop_hook_commands(settings_file) == [
            CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"][0]["hooks"][0]["command"]
        ]

    def test_non_utf8_settings_are_replaced(self, tmp_path, caplog):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_bytes(b"\xff\xfe{")

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-UTF-8 Claude settings" in caplog.text

    def test_unreadable_settings_fail_without_discarding_operator_content(
        self, tmp_path, monkeypatch
    ):
        # Failing to read a file says nothing about what is in it. Overwriting
        # on a read error silently deletes operator settings that were fine.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        original = json.dumps({"model": "opus"})
        settings_file.write_text(original)
        _break_read_of(
            monkeypatch, settings_file, PermissionError("simulated read failure")
        )

        with pytest.raises(WorktreeError, match="read existing Claude settings"):
            install_claude_settings(tmp_path)

        assert settings_file.read_bytes() == original.encode()

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
