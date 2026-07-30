"""Tests for the one-shot development worktree command."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "create_dev_worktree.py"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _run(["git", "init", "-b", "main"], cwd=path)
    (path / "README.md").write_text("test repository\n")
    _run(["git", "add", "README.md"], cwd=path)
    _run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "Initial commit",
        ],
        cwd=path,
    )
    return path


def _fake_make(path: Path, *, exit_code: int = 0) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        'test "$1" = "-C"\n'
        'test "$3" = "worktree-setup"\n'
        'touch "$2/.setup-complete"\n'
        f"exit {exit_code}\n"
    )
    path.chmod(0o755)
    return path


def _create(
    *,
    repo_root: Path,
    branch: str,
    make_command: Path,
    worktree_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(repo_root),
        "--branch",
        branch,
        "--make",
        str(make_command),
    ]
    if worktree_path is not None:
        command.extend(["--path", str(worktree_path)])
    return subprocess.run(command, capture_output=True, text=True)


def test_creates_default_sibling_worktree_and_runs_setup(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "project")
    fake_make = _fake_make(tmp_path / "fake-make")

    result = _create(
        repo_root=repo,
        branch="feature/setup",
        make_command=fake_make,
    )

    worktree = tmp_path / "project-wt-feature-setup"
    assert result.returncode == 0, result.stderr
    assert (worktree / ".setup-complete").is_file()
    branch = _run(["git", "branch", "--show-current"], cwd=worktree).stdout.strip()
    assert branch == "feature/setup"
    assert f"Worktree ready: {worktree}" in result.stdout


def test_invocation_from_linked_worktree_uses_primary_repo_name(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "project")
    linked = tmp_path / "existing-linked-worktree"
    _run(
        ["git", "worktree", "add", str(linked), "-b", "existing-work"],
        cwd=repo,
    )
    (linked / "linked-branch.txt").write_text("branch-specific commit\n")
    _run(["git", "add", "linked-branch.txt"], cwd=linked)
    _run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "Linked worktree commit",
        ],
        cwd=linked,
    )
    fake_make = _fake_make(tmp_path / "fake-make")

    result = _create(
        repo_root=linked,
        branch="feature/from-linked",
        make_command=fake_make,
    )

    expected = tmp_path / "project-wt-feature-from-linked"
    assert result.returncode == 0, result.stderr
    assert (expected / ".setup-complete").is_file()
    assert (expected / "linked-branch.txt").read_text() == "branch-specific commit\n"


def test_setup_failure_preserves_worktree_and_prints_retry(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "project")
    target = tmp_path / "custom-worktree"
    fake_make = _fake_make(tmp_path / "fake-make", exit_code=17)

    result = _create(
        repo_root=repo,
        branch="setup-fails",
        make_command=fake_make,
        worktree_path=target,
    )

    assert result.returncode == 17
    assert (target / ".git").is_file()
    assert (target / ".setup-complete").is_file()
    assert "The worktree was preserved." in result.stderr
    assert f"Retry with: {fake_make} -C {target} worktree-setup" in result.stderr


def test_existing_branch_fails_before_creating_target(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "project")
    _run(["git", "branch", "already-exists"], cwd=repo)
    target = tmp_path / "should-not-exist"
    fake_make = _fake_make(tmp_path / "fake-make")

    result = _create(
        repo_root=repo,
        branch="already-exists",
        make_command=fake_make,
        worktree_path=target,
    )

    assert result.returncode == 2
    assert not target.exists()
    assert "Local branch 'already-exists' already exists" in result.stderr


def test_makefile_exposes_one_shot_target() -> None:
    make_command = shutil.which("gmake") or shutil.which("make")
    if make_command is None:
        pytest.fail("GNU Make is required by the repository")

    result = subprocess.run(
        [
            make_command,
            "--no-print-directory",
            "--dry-run",
            "worktree-create",
            "BRANCH=feature/test",
            "BASE_REF=main",
            "WORKTREE_PATH=/tmp/test-worktree",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "scripts/create_dev_worktree.py" in result.stdout
    assert '--branch "feature/test"' in result.stdout
    assert '--base-ref "main"' in result.stdout
    assert '--path "/tmp/test-worktree"' in result.stdout


def test_makefile_reports_missing_branch_without_creating_worktree() -> None:
    make_command = shutil.which("gmake") or shutil.which("make")
    if make_command is None:
        pytest.fail("GNU Make is required by the repository")

    result = subprocess.run(
        [make_command, "--no-print-directory", "worktree-create"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "BRANCH is required" in result.stderr
