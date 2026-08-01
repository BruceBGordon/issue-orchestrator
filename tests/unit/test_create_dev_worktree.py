"""Tests for the one-shot development worktree command."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
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


def _fake_python_environment_reader(path: Path) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        'printf "branch=%s\\n" "$IO_WORKTREE_CREATE_BRANCH"\n'
        'printf "base_ref=%s\\n" "$IO_WORKTREE_CREATE_BASE_REF"\n'
        'printf "worktree_path=%s\\n" "$IO_WORKTREE_CREATE_PATH"\n'
        'printf "originals=%s%s%s\\n" "${BRANCH+x}" "${BASE_REF+x}" '
        '"${WORKTREE_PATH+x}"\n'
    )
    path.chmod(0o755)
    return path


def _isolated_make_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for variable in (
        "GNUMAKEFLAGS",
        "MAKEFLAGS",
        "MAKELEVEL",
        "MAKEOVERRIDES",
        "MFLAGS",
    ):
        environment.pop(variable, None)
    return environment


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
    target = tmp_path / "custom worktree"
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
    retry_command = shlex.join(
        [str(fake_make), "-C", str(target), "worktree-setup"]
    )
    assert f"Retry with: {retry_command}" in result.stderr


def test_environment_inputs_create_worktree_and_quote_activation(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "project")
    target = tmp_path / "custom worktree"
    fake_make = _fake_make(tmp_path / "fake-make")
    environment = os.environ.copy()
    environment.update(
        BRANCH="feature/from-environment",
        BASE_REF="HEAD",
        WORKTREE_PATH=str(target),
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--make",
            str(fake_make),
        ],
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (target / ".setup-complete").is_file()
    activate_command = shlex.join(
        ["source", str(target / ".venv" / "bin" / "activate")]
    )
    assert f"Activate with: {activate_command}" in result.stdout


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


def test_rejects_target_nested_in_source_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "project")
    target = repo / "nested-worktree"
    fake_make = _fake_make(tmp_path / "fake-make")

    result = _create(
        repo_root=repo,
        branch="nested",
        make_command=fake_make,
        worktree_path=target,
    )

    assert result.returncode == 2
    assert not target.exists()
    assert "must be outside existing worktree" in result.stderr
    branches = _run(["git", "branch", "--list", "nested"], cwd=repo).stdout
    assert not branches.strip()


def test_rejects_target_nested_in_another_registered_worktree(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "project")
    linked = tmp_path / "existing-linked-worktree"
    _run(
        ["git", "worktree", "add", str(linked), "-b", "existing-work"],
        cwd=repo,
    )
    target = linked / "nested-worktree"
    fake_make = _fake_make(tmp_path / "fake-make")

    result = _create(
        repo_root=repo,
        branch="nested",
        make_command=fake_make,
        worktree_path=target,
    )

    assert result.returncode == 2
    assert not target.exists()
    assert f"must be outside existing worktree {linked}" in result.stderr


def test_makefile_transports_inputs_without_evaluating_them(tmp_path: Path) -> None:
    make_commands = {
        command
        for name in ("gmake", "make")
        if (command := shutil.which(name)) is not None
    }
    if not make_commands:
        pytest.fail("GNU Make is required by the repository")
    fake_python = _fake_python_environment_reader(tmp_path / "fake-python")
    branch = (
        'feature/quote"-$dollar-`printf shell-expanded`-'
        "$(shell printf make-expanded)"
    )
    base_ref = 'HEAD-`printf base-expanded`-$(shell printf make-base-expanded)'
    worktree_path = tmp_path / 'custom " `printf path-expanded` worktree'

    expected_output = [
        f"branch={branch}",
        f"base_ref={base_ref}",
        f"worktree_path={worktree_path}",
        "originals=",
    ]
    for make_command in make_commands:
        result = subprocess.run(
            [
                make_command,
                "--no-print-directory",
                "worktree-create",
                f"SYSTEM_PYTHON={fake_python}",
                f"BRANCH={branch}",
                f"BASE_REF={base_ref}",
                f"WORKTREE_PATH={worktree_path}",
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            env=_isolated_make_environment(),
            text=True,
        )

        assert result.stdout.splitlines() == expected_output, make_command


def test_makefile_reports_missing_branch_without_creating_worktree() -> None:
    make_command = shutil.which("gmake") or shutil.which("make")
    if make_command is None:
        pytest.fail("GNU Make is required by the repository")

    environment = _isolated_make_environment()
    for variable in ("BRANCH", "BASE_REF", "WORKTREE_PATH"):
        environment.pop(variable, None)

    result = subprocess.run(
        [make_command, "--no-print-directory", "worktree-create"],
        cwd=REPO_ROOT,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode != 0
    assert "BRANCH is required" in result.stderr
