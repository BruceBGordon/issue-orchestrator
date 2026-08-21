"""Unit tests for E2E worktree management."""

import subprocess

import pytest
import shutil
from pathlib import Path
from unittest.mock import patch

from issue_orchestrator.infra.e2e_worktree import (
    ensure_e2e_worktree,
    get_e2e_worktree_path,
    _sync_venv,
)

FAKE_SHA = "abc123deadbeef"


class TestGetE2EWorktreePath:
    """Test deterministic worktree path derivation."""

    def test_worktree_path_is_sibling_directory(self, tmp_path: Path):
        repo_root = tmp_path / "issue-orchestrator"
        repo_root.mkdir()

        result = get_e2e_worktree_path(repo_root)

        assert result == tmp_path / "issue-orchestrator-e2e-worktree"
        assert result.parent == repo_root.parent

    def test_worktree_path_appends_suffix(self, tmp_path: Path):
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()

        result = get_e2e_worktree_path(repo_root)

        assert result.name == "my-repo-e2e-worktree"


def _make_mock_run(extra_side_effect=None):
    """Build a side_effect that returns FAKE_SHA for rev-parse HEAD
    and delegates other calls to *extra_side_effect* or returns success."""
    def side_effect(cmd, **kwargs):
        # git rev-parse HEAD → return the fake SHA
        if cmd[:2] == ["git", "rev-parse"] and "HEAD" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{FAKE_SHA}\n", stderr="")

        if extra_side_effect is not None:
            return extra_side_effect(cmd, **kwargs)

        return subprocess.CompletedProcess(cmd, 0)

    return side_effect



def _install_venv_guard(worktree_path: Path) -> None:
    """Give the fake worktree the real mutation-authorization owner.

    ``_sync_venv`` fails closed without it, and a real worktree is a checkout of
    this repo, so it always carries the guard. Refusal paths are covered in
    tests/unit/test_venv_guard_callers.py.
    """
    repo_root = Path(__file__).resolve().parents[3]
    guard = worktree_path / "scripts" / "venv_guard.sh"
    guard.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_root / "scripts" / "venv_guard.sh", guard)
    guard.chmod(0o755)



class _DecisionRunner:
    """CommandRunner stub returning one authority decision.

    The suite patches ``subprocess.run`` module-wide, so a real runner would be
    intercepted too. Injecting the port keeps the authority's contract visible
    in the test rather than mocked away.
    """

    def __init__(self, outcome: str = "owned", sync_args: str = "--frozen --all-extras"):
        self._outcome = outcome
        self._sync_args = sync_args

    def run(self, command, **kwargs):
        from types import SimpleNamespace

        venv = ""
        if "--venv" in command:
            venv = command[command.index("--venv") + 1]
        stdout = (
            f"outcome={self._outcome}\n"
            f"sync_args={self._sync_args}\n"
            f"venv={venv}\n"
            "reason=test\n"
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="", timed_out=False)


class TestEnsureE2EWorktree:
    """Test worktree creation, update, and recovery."""

    @pytest.fixture
    def repo_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "issue-orchestrator"
        root.mkdir()
        return root

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_creates_worktree_when_not_exists(self, mock_run, repo_root: Path):
        """When the worktree directory doesn't exist, create it."""
        mock_run.side_effect = _make_mock_run()
        expected_wt = get_e2e_worktree_path(repo_root)

        result = ensure_e2e_worktree(repo_root, _DecisionRunner())

        assert result == expected_wt

        # Find the git worktree add call
        git_add_calls = [c for c in mock_run.call_args_list
                         if c[0][0][:2] == ["git", "worktree"]]
        assert len(git_add_calls) == 1
        cmd = git_add_calls[0][0][0]
        assert "add" in cmd
        assert "--detach" in cmd
        assert str(expected_wt) in cmd
        assert FAKE_SHA in cmd

        # A minimal pytest venv should also be prepared for repos without pyproject.toml.
        uv_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "uv"]
        assert len(uv_calls) == 2

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_updates_worktree_when_exists(self, mock_run, repo_root: Path):
        """When the worktree directory exists, force checkout + clean."""
        mock_run.side_effect = _make_mock_run()
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()  # Simulate existing worktree

        result = ensure_e2e_worktree(repo_root, _DecisionRunner())

        assert result == worktree_path

        # Find checkout call (uses the SHA, not origin/main)
        checkout_calls = [c for c in mock_run.call_args_list
                          if "checkout" in c[0][0]]
        assert len(checkout_calls) == 1
        cmd = checkout_calls[0][0][0]
        assert "-f" in cmd
        assert "--detach" in cmd
        assert FAKE_SHA in cmd

        # git clean preserves .venv, timeline, sessions, and E2E reports
        clean_calls = [c for c in mock_run.call_args_list
                       if "clean" in c[0][0]]
        assert len(clean_calls) == 1
        clean_cmd = clean_calls[0][0][0]
        assert "--exclude=.venv" in clean_cmd
        assert "--exclude=.issue-orchestrator/state/timeline.sqlite*" in clean_cmd
        assert "--exclude=.issue-orchestrator/sessions" in clean_cmd
        assert "--exclude=.issue-orchestrator/e2e-results" in clean_cmd

        # Minimal pytest venv
        uv_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "uv"]
        assert len(uv_calls) == 2

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_recovers_on_checkout_failure(self, mock_run, repo_root: Path):
        """When checkout fails, remove and recreate the worktree."""
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()  # Simulate existing worktree

        def extra(cmd, **_kwargs):
            if "checkout" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="checkout failed")
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = _make_mock_run(extra_side_effect=extra)

        result = ensure_e2e_worktree(repo_root, _DecisionRunner())

        assert result == worktree_path

        # After checkout failure: worktree remove, rev-parse HEAD (again for create), worktree add, uv sync
        remove_calls = [c for c in mock_run.call_args_list if "remove" in c[0][0]]
        assert len(remove_calls) >= 1
        add_calls = [c for c in mock_run.call_args_list
                     if c[0][0][:2] == ["git", "worktree"] and "add" in c[0][0]]
        assert len(add_calls) == 1
        uv_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "uv"]
        assert len(uv_calls) == 2

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_recovery_removes_stale_non_worktree_directory(self, mock_run, repo_root: Path):
        """When a stale directory exists but is not a registered worktree,
        recovery must rmtree it before git worktree add."""
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()
        # Place a file so the directory is non-empty
        (worktree_path / "leftover.txt").write_text("stale")

        def extra(cmd, **_kwargs):
            if "checkout" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="not a git repo")
            if "remove" in cmd:
                raise subprocess.CalledProcessError(128, cmd, stderr="not a working tree")
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = _make_mock_run(extra_side_effect=extra)

        result = ensure_e2e_worktree(repo_root, _DecisionRunner())

        assert result == worktree_path
        # The stale directory should have been deleted before worktree add
        add_calls = [c for c in mock_run.call_args_list
                     if c[0][0][:2] == ["git", "worktree"] and "add" in c[0][0]]
        assert len(add_calls) == 1
        uv_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "uv"]
        assert len(uv_calls) == 2

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_raises_on_git_failure(self, mock_run, repo_root: Path):
        """When git commands fail fatally, raise RuntimeError."""
        mock_run.side_effect = subprocess.CalledProcessError(
            128, ["git", "worktree", "add"], stderr="fatal: not a git repository"
        )

        with pytest.raises(RuntimeError, match="Failed to prepare E2E worktree"):
            ensure_e2e_worktree(repo_root, _DecisionRunner())

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_raises_on_missing_tool(self, mock_run, repo_root: Path):
        """When git or uv is not found, raise RuntimeError."""
        mock_run.side_effect = FileNotFoundError("git not found")

        with pytest.raises(RuntimeError, match="Required tool not found"):
            ensure_e2e_worktree(repo_root, _DecisionRunner())

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_uv_sync_uses_frozen_all_extras(self, mock_run, repo_root: Path):
        """Verify uv sync is called with --frozen --all-extras."""
        mock_run.side_effect = _make_mock_run()
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()
        (worktree_path / "pyproject.toml").write_text("[project]\nname = \"example\"\n")
        _install_venv_guard(worktree_path)
        (worktree_path / "uv.lock").write_text("version = 1\n")

        ensure_e2e_worktree(repo_root, _DecisionRunner())

        # Find the uv sync call
        uv_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "uv"]
        assert len(uv_calls) == 1
        cmd = uv_calls[0][0][0]
        assert "sync" in cmd
        assert "--frozen" in cmd
        assert "--all-extras" in cmd

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_uv_sync_without_lock_omits_frozen(self, mock_run, repo_root: Path):
        """Repos without uv.lock should still sync instead of failing fast."""
        mock_run.side_effect = _make_mock_run()
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()
        (worktree_path / "pyproject.toml").write_text("[project]\nname = \"example\"\n")
        _install_venv_guard(worktree_path)

        ensure_e2e_worktree(repo_root, _DecisionRunner())

        uv_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "uv"]
        assert len(uv_calls) == 1
        cmd = uv_calls[0][0][0]
        assert cmd == ["uv", "sync", "--all-extras"]

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_uv_sync_installs_missing_worker_dependencies(
        self, mock_run, repo_root: Path
    ):
        """Repos missing worker deps in their synced env get fallback installs."""
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()
        (worktree_path / "pyproject.toml").write_text("[project]\nname = \"example\"\n")
        _install_venv_guard(worktree_path)

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "rev-parse"] and "HEAD" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{FAKE_SHA}\n", stderr="")
            if cmd[:2] == ["git", "checkout"] or cmd[:2] == ["git", "clean"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[:2] == ["uv", "sync"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[0].endswith("/.venv/bin/python"):
                return subprocess.CompletedProcess(cmd, 1, stderr="No module named pytest")
            if cmd[:3] == ["uv", "pip", "install"]:
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = side_effect

        ensure_e2e_worktree(repo_root, _DecisionRunner())

        install_calls = [
            c for c in mock_run.call_args_list if c[0][0][:3] == ["uv", "pip", "install"]
        ]
        assert len(install_calls) == 1
        cmd = install_calls[0][0][0]
        assert "defusedxml>=0.7" in cmd
        assert "pytest>=8.0" in cmd

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_no_pyproject_creates_minimal_worker_venv(self, mock_run, tmp_path: Path):
        """Non-Python repos still get enough Python tooling to run E2E wrappers."""
        mock_run.return_value = subprocess.CompletedProcess(["uv"], 0)

        _sync_venv(tmp_path, _DecisionRunner())

        commands = [c[0][0] for c in mock_run.call_args_list]
        assert commands[0] == ["uv", "venv", ".venv"]
        assert commands[1] == [
            "uv",
            "pip",
            "install",
            "--python",
            str(tmp_path / ".venv" / "bin" / "python"),
            "defusedxml>=0.7",
            "pytest>=8.0",
        ]
