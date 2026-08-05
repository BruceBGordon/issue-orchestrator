"""Shared fixtures for integration tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Generator

import pytest


def xdist_timeout(base_seconds: float) -> float:
    """Scale a wall-clock timeout so CPU-starved xdist workers have headroom.

    Integration tests that spawn subprocesses or wait for HTTP endpoints can
    starve for CPU when run alongside 8-16 xdist peers.  Under serial runs the
    base value is returned unchanged; under xdist the value is tripled so a
    slow process start does not flake a green test.

    This is a deliberate, conservative multiplier: large enough to absorb
    realistic scheduling jitter, small enough to keep true hangs visible.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return base_seconds * 3
    return base_seconds


@pytest.fixture
def isolated_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Run live Codex tests without inheriting personal config or MCP servers."""
    configured_home = os.environ.get("CODEX_HOME")
    source_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    isolated_home = tmp_path / "codex-home"
    isolated_home.mkdir()
    auth_file = source_home / "auth.json"
    if auth_file.is_file():
        shutil.copy2(auth_file, isolated_home / "auth.json")
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    return isolated_home


def _resolve_base_repo_root() -> Path:
    """Resolve the actual base repo root, following worktree references.

    In a git worktree, ``.git`` is a *file* containing
    ``gitdir: /base/repo/.git/worktrees/<name>``.  We follow that reference
    so the guardrail protects the real base repo, not the worktree (which is
    ephemeral and expected to have state files during integration tests).
    """
    repo_root = Path(__file__).resolve().parents[2]
    git_path = repo_root / ".git"
    if git_path.is_file():
        # Worktree: .git contains "gitdir: /path/to/.git/worktrees/<name>"
        content = git_path.read_text().strip()
        if content.startswith("gitdir:"):
            gitdir = Path(content.split(": ", 1)[1])
            # gitdir is /base/repo/.git/worktrees/<name> → base repo is 3 levels up
            return gitdir.parent.parent.parent
    return repo_root


_BASE_REPO_ROOT = _resolve_base_repo_root()
_STATE_DIR = _BASE_REPO_ROOT / ".issue-orchestrator" / "state"


@pytest.fixture(autouse=True)
def _assert_no_base_repo_state_pollution():
    """Guardrail: integration tests must never modify SQLite files in base repo.

    Snapshots the base repo's .issue-orchestrator/state/ directory before each
    test and asserts:
    1. No new .sqlite files appeared (creation pollution)
    2. No existing .sqlite files were deleted (deletion culprit detection)

    Only checks actual ``.sqlite`` database files — transient WAL-mode journal
    files (``.sqlite-wal``, ``.sqlite-shm``) are excluded because they appear
    and disappear as connections open/close and trigger false positives under
    xdist parallel execution.

    This catches any test or code path that accidentally targets the real repo
    root instead of tmp_path.
    """
    # Only check actual .sqlite database files, not transient WAL artifacts
    # (-wal, -shm) which the live orchestrator creates/removes normally.
    before = set(_STATE_DIR.glob("*.sqlite")) if _STATE_DIR.exists() else set()
    yield
    after = set(_STATE_DIR.glob("*.sqlite")) if _STATE_DIR.exists() else set()
    new_files = after - before
    assert not new_files, (
        f"Integration test created SQLite file(s) in base repo state dir!\n"
        f"New files: {new_files}\n"
        f"State dir: {_STATE_DIR}\n"
        f"Tests must use tmp_path for state, not the real repo root."
    )
    deleted_files = before - after
    assert not deleted_files, (
        f"Integration test DELETED SQLite file(s) from base repo state dir!\n"
        f"Deleted files: {deleted_files}\n"
        f"State dir: {_STATE_DIR}\n"
        f"This is the likely culprit for the mystery state file deletion."
    )


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path) -> Generator[Path, None, None]:
    """Isolate all integration tests from the production registry.

    Sets ISSUE_ORCHESTRATOR_CONFIG_DIR to a temp directory so tests
    don't pollute the user's real registry at ~/.config/issue-orchestrator/.

    This is critical for tests that:
    - Start control center as a subprocess (env var is inherited)
    - Register repos via API
    - Use the repo registry directly

    Also sets ISSUE_ORCHESTRATOR_SKIP_DOCTOR to skip slow health checks.
    """
    config_dir = tmp_path / "test-config"
    config_dir.mkdir()

    old_config = os.environ.get("ISSUE_ORCHESTRATOR_CONFIG_DIR")
    old_skip = os.environ.get("ISSUE_ORCHESTRATOR_SKIP_DOCTOR")

    os.environ["ISSUE_ORCHESTRATOR_CONFIG_DIR"] = str(config_dir)
    os.environ["ISSUE_ORCHESTRATOR_SKIP_DOCTOR"] = "1"

    yield config_dir

    # Restore original values
    if old_config is None:
        os.environ.pop("ISSUE_ORCHESTRATOR_CONFIG_DIR", None)
    else:
        os.environ["ISSUE_ORCHESTRATOR_CONFIG_DIR"] = old_config

    if old_skip is None:
        os.environ.pop("ISSUE_ORCHESTRATOR_SKIP_DOCTOR", None)
    else:
        os.environ["ISSUE_ORCHESTRATOR_SKIP_DOCTOR"] = old_skip


@pytest.fixture(autouse=True)
def _strip_nested_session_env(monkeypatch):
    """Allow Claude subprocess invocations from within a Claude Code session.

    Claude Code sets CLAUDECODE and CLAUDE_CODE_ENTRYPOINT to detect nested
    launches. Strip them so integration tests that spawn Claude subprocesses
    work regardless of whether the test runner itself is a Claude Code agent.
    """
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
