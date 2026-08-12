"""Tests for repo_registry module."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.infra.repo_registry import (
    RegisteredRepo,
    RepoRegistry,
    RepoRegistryTransactionOwner,
    add_repo,
    cleanup_stale_repos,
    list_repos,
    load_registry,
    remove_repo,
    record_launched_selection,
    save_registry,
    _config_dir,
    _repos_file,
)


def _hold_registry_transaction(
    config_dir: str,
    repo_path: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    os.environ["ISSUE_ORCHESTRATOR_CONFIG_DIR"] = config_dir
    selection = RepositoryLaunchSelection.parse(
        mode="codex",
        config_name="main.yaml",
    )

    def hold(registry: RepoRegistry) -> None:
        repo = registry.add(repo_path)
        repo.select_launch_configuration(selection)
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("registry transaction was not released")

    RepoRegistryTransactionOwner().mutate(hold)


def _record_registry_selection(
    config_dir: str,
    repo_path: str,
    started: multiprocessing.synchronize.Event,
) -> None:
    os.environ["ISSUE_ORCHESTRATOR_CONFIG_DIR"] = config_dir
    started.set()
    record_launched_selection(
        repo_path,
        RepositoryLaunchSelection.parse(
            mode="claude",
            config_name="main.yaml",
        ),
    )


def test_record_launched_selection_updates_an_existing_registration(
    tmp_path: Path,
) -> None:
    repos_file = tmp_path / "repos.json"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    selection = RepositoryLaunchSelection.parse(
        mode="codex",
        config_name="main.yaml",
    )
    with patch(
        "issue_orchestrator.infra.repo_registry._repos_file",
        return_value=repos_file,
    ):
        add_repo(repo_root)
        record_launched_selection(repo_root, selection)

        registered = list_repos()

    assert len(registered) == 1
    assert registered[0].launch_selection == selection


class TestConfigDir:
    """Tests for config directory resolution."""

    def test_uses_xdg_config_home_if_set(self, tmp_path: Path) -> None:
        """Uses XDG_CONFIG_HOME when set."""
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
            result = _config_dir()

        assert result == tmp_path / "issue-orchestrator"

    def test_uses_home_config_as_default(self) -> None:
        """Falls back to ~/.config when XDG_CONFIG_HOME not set."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("os.environ.get", return_value=None):
                result = _config_dir()

        assert result == Path.home() / ".config" / "issue-orchestrator"


class TestRegisteredRepo:
    """Tests for RegisteredRepo dataclass."""

    def test_sets_default_name_from_path(self) -> None:
        """Default name is the directory name."""
        repo = RegisteredRepo(path="/home/user/projects/my-repo")

        assert repo.name == "my-repo"

    def test_sets_default_timestamp(self) -> None:
        """Timestamp is set on creation."""
        repo = RegisteredRepo(path="/home/user/projects/my-repo")

        assert repo.added_at
        assert "T" in repo.added_at  # ISO format

    def test_preserves_explicit_name(self) -> None:
        """Explicit name is preserved."""
        repo = RegisteredRepo(path="/home/user/projects/my-repo", name="Custom Name")

        assert repo.name == "Custom Name"

    def test_to_dict(self) -> None:
        """Converts to dict correctly."""
        repo = RegisteredRepo(
            path="/home/user/projects/my-repo",
            name="My Repo",
            added_at="2024-01-01T00:00:00+00:00",
        )

        result = repo.to_dict()

        assert result == {
            "path": "/home/user/projects/my-repo",
            "name": "My Repo",
            "added_at": "2024-01-01T00:00:00+00:00",
            "selected_config": "default.yaml",  # Default config file name
            "selected_mode": "default",
        }

    def test_from_dict(self) -> None:
        """Creates from dict correctly."""
        data = {
            "path": "/home/user/projects/my-repo",
            "name": "My Repo",
            "added_at": "2024-01-01T00:00:00+00:00",
        }

        repo = RegisteredRepo.from_dict(data)

        assert repo.path == "/home/user/projects/my-repo"
        assert repo.name == "My Repo"
        assert repo.added_at == "2024-01-01T00:00:00+00:00"
        assert repo.selected_mode == "default"

    def test_from_dict_with_minimal_data(self) -> None:
        """Creates from dict with only required fields."""
        data = {"path": "/home/user/projects/my-repo"}

        repo = RegisteredRepo.from_dict(data)

        assert repo.path == "/home/user/projects/my-repo"
        assert repo.name == "my-repo"  # Default from path
        assert repo.added_at  # Generated


class TestRepoRegistry:
    """Tests for RepoRegistry class."""

    def test_add_repo(self, tmp_path: Path) -> None:
        """Adding a repo works."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        result = registry.add(repo_path)

        assert result.path == str(repo_path.resolve())
        assert len(registry.repos) == 1

    def test_add_repo_with_explicit_name(self, tmp_path: Path) -> None:
        """Adding a repo preserves an explicit display name."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        result = registry.add(repo_path, name="Custom Repo")

        assert result.path == str(repo_path.resolve())
        assert result.name == "Custom Repo"
        assert len(registry.repos) == 1

    def test_add_duplicate_raises(self, tmp_path: Path) -> None:
        """Adding a duplicate raises ValueError."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()
        registry.add(repo_path)

        with pytest.raises(ValueError, match="already registered"):
            registry.add(repo_path)

    def test_remove_repo(self, tmp_path: Path) -> None:
        """Removing a repo works."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()
        registry.add(repo_path)

        result = registry.remove(repo_path)

        assert result is True
        assert len(registry.repos) == 0

    def test_remove_nonexistent_returns_false(self, tmp_path: Path) -> None:
        """Removing a non-existent repo returns False."""
        registry = RepoRegistry()

        result = registry.remove(tmp_path / "nonexistent")

        assert result is False

    def test_get_repo(self, tmp_path: Path) -> None:
        """Getting a repo by path works."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()
        registry.add(repo_path)

        result = registry.get(repo_path)

        assert result is not None
        assert result.path == str(repo_path.resolve())

    def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        """Getting a non-existent repo returns None."""
        registry = RepoRegistry()

        result = registry.get(tmp_path / "nonexistent")

        assert result is None

    def test_list_all(self, tmp_path: Path) -> None:
        """Listing all repos works."""
        registry = RepoRegistry()
        (tmp_path / "repo1").mkdir()
        (tmp_path / "repo2").mkdir()
        registry.add(tmp_path / "repo1")
        registry.add(tmp_path / "repo2")

        result = registry.list_all()

        assert len(result) == 2

    def test_to_dict(self, tmp_path: Path) -> None:
        """Converting to dict works."""
        registry = RepoRegistry()
        (tmp_path / "repo1").mkdir()
        registry.add(tmp_path / "repo1")

        result = registry.to_dict()

        assert "repos" in result
        assert len(result["repos"]) == 1

    def test_from_dict(self) -> None:
        """Creating from dict works."""
        data = {
            "repos": [
                {
                    "path": "/home/user/repo1",
                    "name": "Repo 1",
                    "added_at": "2024-01-01T00:00:00+00:00",
                }
            ]
        }

        registry = RepoRegistry.from_dict(data)

        assert len(registry.repos) == 1
        assert registry.repos[0].name == "Repo 1"


class TestLoadSaveRegistry:
    """Tests for load/save functions."""

    def test_load_empty_when_file_missing(self, tmp_path: Path) -> None:
        """Returns empty registry when file doesn't exist."""
        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=tmp_path / "nonexistent.json",
        ):
            registry = load_registry()

        assert len(registry.repos) == 0

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """Save creates config directory if needed."""
        repos_file = tmp_path / "config" / "issue-orchestrator" / "repos.json"
        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            registry = RepoRegistry()
            registry.repos.append(
                RegisteredRepo(path="/home/user/repo", name="Test")
            )

            save_registry(registry)

        assert repos_file.exists()

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Save then load preserves data."""
        repos_file = tmp_path / "repos.json"
        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            registry = RepoRegistry()
            registry.repos.append(
                RegisteredRepo(
                    path="/home/user/repo",
                    name="Test Repo",
                    added_at="2024-01-01T00:00:00+00:00",
                )
            )
            save_registry(registry)

            loaded = load_registry()

        assert len(loaded.repos) == 1
        assert loaded.repos[0].path == "/home/user/repo"
        assert loaded.repos[0].name == "Test Repo"

    def test_serialization_failure_preserves_existing_registry(
        self,
        tmp_path: Path,
    ) -> None:
        repos_file = tmp_path / "repos.json"
        existing = '{"repos": [{"path": "/kept"}]}\n'
        repos_file.write_text(existing, encoding="utf-8")
        registry = RepoRegistry(
            repos=[RegisteredRepo(path="/replacement", selected_config="main.yaml")]
        )
        registry.repos[0].selected_config = object()  # type: ignore[assignment]

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            with pytest.raises(TypeError):
                save_registry(registry)

        assert repos_file.read_text(encoding="utf-8") == existing
        assert list(tmp_path.glob(".repos.json.*.tmp")) == []

    def test_load_rejects_corrupt_json(self, tmp_path: Path) -> None:
        """A corrupt registry fails fast instead of masquerading as empty."""
        repos_file = tmp_path / "repos.json"
        repos_file.write_text("not valid json{{{")

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            with pytest.raises(json.JSONDecodeError):
                load_registry()

    def test_transactions_serialize_concurrent_different_repo_updates(
        self,
        tmp_path: Path,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        entered = context.Event()
        release = context.Event()
        second_started = context.Event()
        first_repo = tmp_path / "first"
        second_repo = tmp_path / "second"
        first_repo.mkdir()
        second_repo.mkdir()
        holder = context.Process(
            target=_hold_registry_transaction,
            args=(str(tmp_path), str(first_repo), entered, release),
        )
        contender = context.Process(
            target=_record_registry_selection,
            args=(str(tmp_path), str(second_repo), second_started),
        )

        holder.start()
        assert entered.wait(timeout=5), "first registry transaction did not start"
        gate_fd = os.open(tmp_path / ".repos.json.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(gate_fd)

        contender.start()
        assert second_started.wait(timeout=5), "second registry mutation did not start"
        release.set()
        holder.join(timeout=5)
        contender.join(timeout=5)

        assert holder.exitcode == 0
        assert contender.exitcode == 0
        with patch.dict(
            "os.environ",
            {"ISSUE_ORCHESTRATOR_CONFIG_DIR": str(tmp_path)},
        ):
            registry = load_registry()
        assert {repo.path for repo in registry.repos} == {
            str(first_repo.resolve()),
            str(second_repo.resolve()),
        }


class TestConvenienceFunctions:
    """Tests for add_repo, remove_repo, list_repos."""

    def test_add_repo_saves(self, tmp_path: Path) -> None:
        """add_repo saves to disk."""
        repos_file = tmp_path / "repos.json"
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            result = add_repo(repo_path)

        assert result.path == str(repo_path.resolve())
        assert repos_file.exists()

        # Verify persisted
        data = json.loads(repos_file.read_text())
        assert len(data["repos"]) == 1

    def test_add_repo_saves_explicit_name(self, tmp_path: Path) -> None:
        """add_repo persists an explicit display name."""
        repos_file = tmp_path / "repos.json"
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            result = add_repo(repo_path, name="Custom Repo")

        assert result.path == str(repo_path.resolve())
        assert result.name == "Custom Repo"

        data = json.loads(repos_file.read_text())
        assert data["repos"][0]["name"] == "Custom Repo"

    def test_remove_repo_saves(self, tmp_path: Path) -> None:
        """remove_repo saves to disk."""
        repos_file = tmp_path / "repos.json"
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            add_repo(repo_path)
            result = remove_repo(repo_path)

        assert result is True

        # Verify persisted
        data = json.loads(repos_file.read_text())
        assert len(data["repos"]) == 0

    def test_list_repos(self, tmp_path: Path) -> None:
        """list_repos returns all repos."""
        repos_file = tmp_path / "repos.json"
        (tmp_path / "repo1").mkdir()
        (tmp_path / "repo2").mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            add_repo(tmp_path / "repo1")
            add_repo(tmp_path / "repo2")

            result = list_repos()

        assert len(result) == 2


class TestCleanupStaleRepos:
    """Tests for cleanup_stale_repos function."""

    def test_removes_nonexistent_paths(self, tmp_path: Path) -> None:
        """Removes repos whose paths no longer exist."""
        repos_file = tmp_path / "repos.json"
        existing_repo = tmp_path / "exists"
        existing_repo.mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            # Add both existing and non-existing repos
            add_repo(existing_repo)

            # Manually add a repo with a path that doesn't exist
            registry = load_registry()
            registry.repos.append(RegisteredRepo(
                path="/nonexistent/path/to/repo",
                name="gone-repo",
            ))
            save_registry(registry)

            # Verify we have 2 repos before cleanup
            assert len(list_repos()) == 2

            # Cleanup
            removed = cleanup_stale_repos()

            # Should have removed 1 stale repo
            assert removed == 1

            # Only existing repo should remain
            repos = list_repos()
            assert len(repos) == 1
            assert repos[0].path == str(existing_repo.resolve())

    def test_no_change_when_all_exist(self, tmp_path: Path) -> None:
        """Does nothing when all repos exist."""
        repos_file = tmp_path / "repos.json"
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            add_repo(repo1)
            add_repo(repo2)

            removed = cleanup_stale_repos()

            assert removed == 0
            assert len(list_repos()) == 2

    def test_removes_multiple_stale_repos(self, tmp_path: Path) -> None:
        """Can remove multiple stale repos at once."""
        repos_file = tmp_path / "repos.json"

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            # Add repos with non-existing paths directly
            registry = RepoRegistry()
            registry.repos.append(RegisteredRepo(path="/fake/path1", name="repo1"))
            registry.repos.append(RegisteredRepo(path="/fake/path2", name="repo2"))
            registry.repos.append(RegisteredRepo(path="/fake/path3", name="repo3"))
            save_registry(registry)

            assert len(list_repos()) == 3

            removed = cleanup_stale_repos()

            assert removed == 3
            assert len(list_repos()) == 0

    def test_empty_registry(self, tmp_path: Path) -> None:
        """Does nothing on empty registry."""
        repos_file = tmp_path / "repos.json"

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            removed = cleanup_stale_repos()

            assert removed == 0
