"""Tests for the local release-prep script."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "prepare_release.py"
SPEC = importlib.util.spec_from_file_location("prepare_release", SCRIPT_PATH)
assert SPEC is not None
prepare_release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = prepare_release
SPEC.loader.exec_module(prepare_release)


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _workflow_options(**overrides):
    defaults = {
        "dry_run": True,
        "sync_environment": True,
        "assume_yes": False,
        "skip_validation": False,
        "validation_command": ("make", "validate-pr"),
        "push": True,
        "create_github_release": True,
        "uv_executable": None,
    }
    defaults.update(overrides)
    return prepare_release.ReleaseWorkflowOptions(**defaults)


def _release_pr_options(**overrides):
    defaults = {
        "dry_run": True,
        "sync_environment": True,
        "assume_yes": False,
        "skip_validation": False,
        "validation_command": ("make", "validate-pr"),
        "push": True,
        "create_pull_request": True,
        "branch_name": None,
        "uv_executable": None,
    }
    defaults.update(overrides)
    return prepare_release.ReleasePrOptions(**defaults)


def _write_pyproject(path: Path, *, version: str = "0.9.0") -> None:
    path.write_text(
        f"""[project]
name = "issue-orchestrator"
version = "{version}"

[tool.example]
version = "9.9.9"
""",
        encoding="utf-8",
    )


def _write_lock(path: Path, *, version: str = "0.9.0") -> None:
    path.write_text(
        f"""version = 1

[[package]]
name = "issue-orchestrator"
version = "{version}"
source = {{ editable = "." }}

[[package]]
name = "other-package"
version = "9.9.9"
source = {{ registry = "https://pypi.org/simple" }}
""",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("raw_version", "expected"),
    [
        ("1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.3"),
        ("V1.2.3", "1.2.3"),
    ],
)
def test_normalize_release_version_accepts_stable_semver(
    raw_version: str, expected: str
) -> None:
    assert prepare_release.normalize_release_version(raw_version) == expected


@pytest.mark.parametrize("raw_version", ["1.2", "1.2.3rc1", "01.2.3", "latest"])
def test_normalize_release_version_rejects_non_release_versions(
    raw_version: str,
) -> None:
    with pytest.raises(prepare_release.ReleasePrepError):
        prepare_release.normalize_release_version(raw_version)


def test_write_project_version_updates_only_project_table(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(pyproject)

    previous = prepare_release.write_project_version(pyproject, "1.0.0")

    assert previous == "0.9.0"
    assert prepare_release.read_project_version(pyproject) == "1.0.0"
    content = pyproject.read_text(encoding="utf-8")
    assert '[tool.example]\nversion = "9.9.9"' in content


def test_read_lock_project_version_uses_editable_project_entry(tmp_path: Path) -> None:
    lockfile = tmp_path / "uv.lock"
    _write_lock(lockfile, version="1.2.3")

    assert prepare_release.read_lock_project_version(lockfile) == "1.2.3"


def test_verify_project_and_lock_versions_requires_matching_release(
    tmp_path: Path,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="1.2.3")
    _write_lock(tmp_path / "uv.lock", version="1.2.2")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    with pytest.raises(prepare_release.ReleasePrepError, match="uv.lock"):
        prepare_release.verify_project_and_lock_versions(paths, "1.2.3")


def test_confirm_release_requires_exact_tag() -> None:
    with pytest.raises(prepare_release.ReleasePrepError, match="expected exact"):
        prepare_release.confirm_release(
            tag_name="v1.0.0",
            input_func=lambda _prompt: "1.0.0",
        )


@pytest.mark.parametrize(
    ("tag_name", "expects_prerelease"),
    [
        ("v0.1.0", True),
        ("v0.10.0", True),
        ("0.11.0", True),
        ("v1.0.0", False),
        ("v2.3.4", False),
    ],
)
def test_github_release_command_marks_0x_releases_as_prerelease(
    tag_name: str, expects_prerelease: bool
) -> None:
    """0.x is explicitly unstable, so those releases must not claim 'Latest'."""
    command = prepare_release.github_release_command(tag_name)

    assert command[:4] == ["gh", "release", "create", tag_name]
    assert ("--prerelease" in command) is expects_prerelease


def test_publish_release_creates_prerelease_for_0x_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    commands: list[list[str]] = []

    monkeypatch.setattr(
        prepare_release,
        "run_command",
        lambda command, cwd=None: commands.append(list(command)),
    )

    prepare_release.publish_release(
        paths, "v0.11.0", _workflow_options(dry_run=False)
    )

    assert commands[-1] == [
        "gh",
        "release",
        "create",
        "v0.11.0",
        "--generate-notes",
        "--prerelease",
    ]


@pytest.mark.parametrize(
    ("tag_name", "expected_command"),
    [
        ("v0.11.0", "gh release create v0.11.0 --generate-notes --prerelease"),
        ("v1.0.0", "gh release create v1.0.0 --generate-notes"),
    ],
)
def test_publish_release_prints_exact_recovery_command_when_gh_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tag_name: str,
    expected_command: str,
) -> None:
    """The tag is pushed and ``gh`` failed: recovery must not rely on memory.

    This is the one path where the release command cannot simply be rerun, so
    an operator typing ``gh release create`` by hand is exactly how a ``0.x``
    tag ends up published as a normal release.
    """
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    pushed: list[list[str]] = []

    def fail_on_gh(command, cwd=None):
        pushed.append(list(command))
        if command[0] == "gh":
            raise prepare_release.ReleasePrepError("Command failed with exit code 1")

    monkeypatch.setattr(prepare_release, "run_command", fail_on_gh)

    with pytest.raises(prepare_release.ReleasePrepError):
        prepare_release.publish_release(
            paths, tag_name, _workflow_options(dry_run=False)
        )

    assert pushed[0][:2] == ["git", "push"]
    stderr = capsys.readouterr().err
    assert expected_command in stderr
    assert "already exists" in stderr
    assert ("--prerelease" in stderr) is tag_name.startswith("v0.")


def test_parse_command_rejects_empty_command() -> None:
    with pytest.raises(prepare_release.ReleasePrepError, match="empty"):
        prepare_release.parse_command("")


def test_full_release_dry_run_prints_single_workflow_without_mutating_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml")
    _write_lock(tmp_path / "uv.lock")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    preflight_calls: list[str] = []

    def fake_dry_run_preflight(*, paths, tag_name, options):  # noqa: ANN001
        preflight_calls.append(tag_name)

    monkeypatch.setattr(
        prepare_release,
        "run_release_dry_run_preflight",
        fake_dry_run_preflight,
    )

    prepare_release.run_release_workflow(
        paths=paths,
        version="v0.9.0",
        options=_workflow_options(),
    )

    output = capsys.readouterr().out
    assert "Preparing full issue-orchestrator release v0.9.0" in output
    assert "Version 0.9.0 already present in pyproject.toml and uv.lock." in output
    assert "Dry run: no files, git refs, or GitHub releases will be changed." in output
    assert "+ git status --porcelain" in output
    assert "+ git ls-remote --exit-code origin refs/heads/main" in output
    assert "+ make validate-pr" in output
    assert "+ git push origin refs/tags/v0.9.0:refs/tags/v0.9.0" in output
    assert "+ gh release create v0.9.0 --generate-notes --prerelease" in output
    assert "git commit" not in output
    assert "HEAD:main" not in output
    assert preflight_calls == ["v0.9.0"]
    assert prepare_release.read_project_version(tmp_path / "pyproject.toml") == "0.9.0"
    assert prepare_release.read_lock_project_version(tmp_path / "uv.lock") == "0.9.0"


def test_full_release_requires_version_already_merged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="0.9.0")
    _write_lock(tmp_path / "uv.lock", version="0.9.0")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    monkeypatch.setattr(
        prepare_release,
        "run_release_dry_run_preflight",
        lambda *, paths, tag_name, options: None,
    )

    with pytest.raises(prepare_release.ReleasePrepError, match="prepare-release"):
        prepare_release.run_release_workflow(
            paths=paths,
            version="v1.0.0",
            options=_workflow_options(),
        )


def test_full_release_rejects_github_release_without_push(tmp_path: Path) -> None:
    _write_pyproject(tmp_path / "pyproject.toml")
    _write_lock(tmp_path / "uv.lock")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    with pytest.raises(prepare_release.ReleasePrepError, match="local-only"):
        prepare_release.run_release_workflow(
            paths=paths,
            version="v1.0.0",
            options=_workflow_options(push=False, create_github_release=True),
        )


def test_release_pr_dry_run_prints_single_workflow_without_mutating_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml")
    _write_lock(tmp_path / "uv.lock")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    preflight_calls: list[str] = []

    def fake_dry_run_preflight(*, paths, tag_name, branch_name, options):  # noqa: ANN001
        preflight_calls.append(f"{branch_name}:{tag_name}")

    monkeypatch.setattr(
        prepare_release,
        "run_release_pr_dry_run_preflight",
        fake_dry_run_preflight,
    )

    prepare_release.run_release_pr_workflow(
        paths=paths,
        version="v1.0.0",
        options=_release_pr_options(),
    )

    output = capsys.readouterr().out
    assert "Preparing release bump PR for v1.0.0" in output
    assert "Release PR branch: release-v1.0.0" in output
    assert "Dry run: no files, branches, commits, pushes, or PRs" in output
    assert "+ git status --porcelain" in output
    assert "+ git switch --create release-v1.0.0 --no-track origin/main" in output
    assert "Would set [project].version to 1.0.0" in output
    assert "+ git commit -s -m 'Release v1.0.0'" in output
    assert "+ make validate-pr" in output
    assert "+ git push -u origin release-v1.0.0" in output
    assert "gh pr create --base main --head release-v1.0.0" in output
    assert preflight_calls == ["release-v1.0.0:v1.0.0"]
    assert prepare_release.read_project_version(tmp_path / "pyproject.toml") == "0.9.0"
    assert prepare_release.read_lock_project_version(tmp_path / "uv.lock") == "0.9.0"


def test_release_pr_rejects_pull_request_without_push(tmp_path: Path) -> None:
    _write_pyproject(tmp_path / "pyproject.toml")
    _write_lock(tmp_path / "uv.lock")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    with pytest.raises(prepare_release.ReleasePrepError, match="local-only"):
        prepare_release.run_release_pr_workflow(
            paths=paths,
            version="v1.0.0",
            options=_release_pr_options(push=False, create_pull_request=True),
        )


def test_release_pr_dry_run_preflight_checks_remote_main_without_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        prepare_release,
        "assert_tool_available",
        lambda executable: calls.append(f"tool:{executable}"),
    )
    monkeypatch.setattr(
        prepare_release,
        "find_uv",
        lambda uv_executable: calls.append("uv"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_clean_worktree",
        lambda root: calls.append("clean"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_origin_remote_exists",
        lambda root: calls.append("remote"),
    )
    monkeypatch.setattr(
        prepare_release,
        "remote_main_sha",
        lambda root: calls.append("remote_main") or "abc123",
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_valid_branch_name",
        lambda root, branch_name: calls.append("branch_name"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_local_branch_absent",
        lambda root, branch_name: calls.append("local_branch"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_remote_branch_absent",
        lambda root, branch_name: calls.append("remote_branch"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_local_tag_absent",
        lambda root, tag_name: calls.append("local_tag"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_remote_tag_absent",
        lambda root, tag_name: calls.append("remote_tag"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_github_release_absent",
        lambda root, tag_name: calls.append("gh_release"),
    )

    prepare_release.run_release_pr_dry_run_preflight(
        paths=prepare_release.ReleasePaths.from_root(tmp_path),
        tag_name="v1.0.0",
        branch_name="release-v1.0.0",
        options=_release_pr_options(),
    )

    assert calls == [
        "tool:git",
        "tool:gh",
        "uv",
        "clean",
        "remote",
        "remote_main",
        "branch_name",
        "local_branch",
        "remote_branch",
        "local_tag",
        "remote_tag",
        "gh_release",
    ]


def test_assert_current_branch_is_main_rejects_other_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_release,
        "run_captured_command",
        lambda _command, *, cwd: _completed(stdout="feature\n"),
    )

    with pytest.raises(prepare_release.ReleasePrepError) as exc_info:
        prepare_release.assert_current_branch_is_main(tmp_path)

    message = str(exc_info.value)
    assert "local branch 'main'" in message
    assert "git switch main && git pull --ff-only origin main" in message


def test_assert_head_matches_origin_main_rejects_outdated_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_rev_parse(_root: Path, ref: str) -> str:
        return {
            "HEAD": "aaa",
            "refs/remotes/origin/main": "bbb",
        }[ref]

    monkeypatch.setattr(prepare_release, "git_rev_parse", fake_rev_parse)

    with pytest.raises(prepare_release.ReleasePrepError) as exc_info:
        prepare_release.assert_head_matches_origin_main(tmp_path)

    message = str(exc_info.value)
    assert "origin/main" in message
    assert "git switch main && git pull --ff-only origin main" in message


def test_assert_head_matches_remote_main_rejects_outdated_remote_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prepare_release, "git_rev_parse", lambda _root, _ref: "aaa")
    monkeypatch.setattr(prepare_release, "remote_main_sha", lambda _root: "bbb")

    with pytest.raises(prepare_release.ReleasePrepError) as exc_info:
        prepare_release.assert_head_matches_remote_main(tmp_path)

    message = str(exc_info.value)
    assert "current remote origin/main" in message
    assert "git switch main && git pull --ff-only origin main" in message


def test_commit_release_metadata_rejects_unexpected_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_release,
        "release_metadata_changed_files",
        lambda _root: {"pyproject.toml", "README.md"},
    )

    with pytest.raises(prepare_release.ReleasePrepError, match="README.md"):
        prepare_release.commit_release_metadata(
            prepare_release.ReleasePaths.from_root(tmp_path),
            "v1.0.0",
        )


def test_commit_release_metadata_commits_only_expected_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        prepare_release,
        "release_metadata_changed_files",
        lambda _root: {"pyproject.toml", "uv.lock"},
    )
    monkeypatch.setattr(
        prepare_release,
        "run_captured_command",
        lambda command, *, cwd: _completed(stdout="pyproject.toml\nuv.lock\n"),
    )
    monkeypatch.setattr(
        prepare_release,
        "run_command",
        lambda command, *, cwd: commands.append(tuple(command)),
    )

    prepare_release.commit_release_metadata(
        prepare_release.ReleasePaths.from_root(tmp_path),
        "v1.0.0",
    )

    assert commands == [
        ("git", "add", "pyproject.toml", "uv.lock"),
        ("git", "commit", "-s", "-m", "Release v1.0.0"),
    ]


def test_commit_release_metadata_rejects_no_metadata_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_release,
        "release_metadata_changed_files",
        lambda _root: set(),
    )

    with pytest.raises(prepare_release.ReleasePrepError, match="did not change"):
        prepare_release.commit_release_metadata(
            prepare_release.ReleasePaths.from_root(tmp_path),
            "v1.0.0",
        )


def test_release_metadata_changed_files_unions_all_dirty_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_captured(command, *, cwd):  # noqa: ANN001
        command_key = tuple(command)
        outputs = {
            ("git", "diff", "--name-only"): "pyproject.toml\n",
            ("git", "diff", "--cached", "--name-only"): "uv.lock\n",
            ("git", "ls-files", "--others", "--exclude-standard"): "notes.txt\n",
        }
        return _completed(stdout=outputs[command_key])

    monkeypatch.setattr(prepare_release, "run_captured_command", fake_run_captured)

    assert prepare_release.release_metadata_changed_files(tmp_path) == {
        "pyproject.toml",
        "uv.lock",
        "notes.txt",
    }


def test_assert_valid_branch_name_rejects_git_invalid_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_release,
        "run_optional_command",
        lambda command, *, cwd: _completed(returncode=1, stderr="fatal: invalid"),
    )

    with pytest.raises(prepare_release.ReleasePrepError, match="Invalid release branch"):
        prepare_release.assert_valid_branch_name(tmp_path, "bad branch")


def test_assert_local_branch_absent_rejects_unexpected_git_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_release,
        "run_optional_command",
        lambda command, *, cwd: _completed(returncode=128),
    )

    with pytest.raises(prepare_release.ReleasePrepError, match="Could not check"):
        prepare_release.assert_local_branch_absent(tmp_path, "release-v1.0.0")


def test_assert_remote_branch_absent_rejects_unexpected_git_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_release,
        "run_optional_command",
        lambda command, *, cwd: _completed(returncode=128, stderr="fatal: network"),
    )

    with pytest.raises(prepare_release.ReleasePrepError, match="fatal: network"):
        prepare_release.assert_remote_branch_absent(tmp_path, "release-v1.0.0")


def test_apply_release_metadata_resolves_uv_before_mutating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    events: list[str] = []

    def fake_find_uv(_explicit_uv):  # noqa: ANN001
        events.append("find_uv")
        return "uv"

    def fake_write_project_version(_pyproject, _version):  # noqa: ANN001
        events.append("write_project_version")
        return "0.9.0"

    monkeypatch.setattr(prepare_release, "find_uv", fake_find_uv)
    monkeypatch.setattr(prepare_release, "write_project_version", fake_write_project_version)
    monkeypatch.setattr(
        prepare_release,
        "run_command",
        lambda _command, *, cwd: events.append("run_command"),
    )
    monkeypatch.setattr(
        prepare_release,
        "verify_project_and_lock_versions",
        lambda _paths, _version: events.append("verify_versions"),
    )
    monkeypatch.setattr(
        prepare_release,
        "sync_environment_if_requested",
        lambda *, paths, expected_version, sync_environment, uv_executable: events.append(
            "sync_environment"
        ),
    )

    prepare_release.apply_release_metadata(
        paths=paths,
        target_version="1.0.0",
        sync_environment=True,
        uv_executable=None,
    )

    assert events[:2] == ["find_uv", "write_project_version"]


def test_release_pr_workflow_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="0.9.0")
    _write_lock(tmp_path / "uv.lock", version="0.9.0")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(
        prepare_release,
        "run_release_pr_preflight",
        lambda *, paths, tag_name, branch_name, options: calls.append("preflight"),
    )
    monkeypatch.setattr(
        prepare_release,
        "confirm_release",
        lambda *, tag_name: calls.append("confirm"),
    )
    monkeypatch.setattr(
        prepare_release,
        "create_release_pr_branch",
        lambda _paths, _branch_name: calls.append("branch"),
    )
    monkeypatch.setattr(
        prepare_release,
        "apply_release_metadata",
        lambda *, paths, target_version, sync_environment, uv_executable: calls.append(
            "metadata"
        ),
    )
    monkeypatch.setattr(
        prepare_release,
        "commit_release_metadata",
        lambda _paths, _tag_name: calls.append("commit"),
    )
    monkeypatch.setattr(
        prepare_release,
        "run_release_validation",
        lambda _paths, *, skip_validation, validation_command: calls.append("validate"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_clean_worktree",
        lambda _root: calls.append("clean_after_validation"),
    )
    monkeypatch.setattr(
        prepare_release,
        "publish_release_pr",
        lambda _paths, *, tag_name, branch_name, options: calls.append("publish"),
    )

    prepare_release.run_release_pr_workflow(
        paths=paths,
        version="v1.0.0",
        options=_release_pr_options(dry_run=False),
    )

    assert calls == [
        "preflight",
        "confirm",
        "branch",
        "metadata",
        "commit",
        "validate",
        "clean_after_validation",
        "publish",
    ]


def test_release_pr_body_includes_main_switch_handoff() -> None:
    body = prepare_release.release_pr_body("v1.0.0")

    assert "git switch main && git pull --ff-only origin main" in body
    assert "make release VERSION=v1.0.0" in body


def test_release_pr_success_message_includes_main_switch_handoff(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="0.9.0")
    _write_lock(tmp_path / "uv.lock", version="0.9.0")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    monkeypatch.setattr(
        prepare_release,
        "run_release_pr_preflight",
        lambda *, paths, tag_name, branch_name, options: None,
    )
    monkeypatch.setattr(prepare_release, "confirm_release", lambda *, tag_name: None)
    monkeypatch.setattr(
        prepare_release, "create_release_pr_branch", lambda _paths, _branch_name: None
    )
    monkeypatch.setattr(
        prepare_release,
        "apply_release_metadata",
        lambda *, paths, target_version, sync_environment, uv_executable: None,
    )
    monkeypatch.setattr(
        prepare_release, "commit_release_metadata", lambda _paths, _tag_name: None
    )
    monkeypatch.setattr(
        prepare_release,
        "run_release_validation",
        lambda _paths, *, skip_validation, validation_command: None,
    )
    monkeypatch.setattr(prepare_release, "assert_clean_worktree", lambda _root: None)
    monkeypatch.setattr(
        prepare_release,
        "publish_release_pr",
        lambda _paths, *, tag_name, branch_name, options: None,
    )

    prepare_release.run_release_pr_workflow(
        paths=paths,
        version="v1.0.0",
        options=_release_pr_options(dry_run=False),
    )

    output = capsys.readouterr().out
    assert "git switch main && git pull --ff-only origin main" in output
    assert "make release VERSION=v1.0.0" in output


def test_release_pr_failure_after_branch_prints_recovery_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="0.9.0")
    _write_lock(tmp_path / "uv.lock", version="0.9.0")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    monkeypatch.setattr(
        prepare_release,
        "run_release_pr_preflight",
        lambda *, paths, tag_name, branch_name, options: None,
    )
    monkeypatch.setattr(
        prepare_release,
        "confirm_release",
        lambda *, tag_name: None,
    )
    monkeypatch.setattr(
        prepare_release,
        "create_release_pr_branch",
        lambda _paths, _branch_name: None,
    )

    def fail_metadata(**_kwargs):  # noqa: ANN003
        raise prepare_release.ReleasePrepError("metadata failed")

    monkeypatch.setattr(prepare_release, "apply_release_metadata", fail_metadata)

    with pytest.raises(prepare_release.ReleasePrepError, match="metadata failed"):
        prepare_release.run_release_pr_workflow(
            paths=paths,
            version="v1.0.0",
            options=_release_pr_options(dry_run=False),
        )

    stderr = capsys.readouterr().err
    assert "stopped after creating the local release branch" in stderr
    assert "git switch -" in stderr
    assert "git branch -D release-v1.0.0" in stderr
    assert "git push origin --delete release-v1.0.0" in stderr


def test_full_release_rechecks_clean_tree_after_validation_before_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="1.0.0")
    _write_lock(tmp_path / "uv.lock", version="1.0.0")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(
        prepare_release,
        "run_release_preflight",
        lambda *, paths, tag_name, options: calls.append("preflight"),
    )
    monkeypatch.setattr(
        prepare_release,
        "verify_release_metadata_ready",
        lambda _paths, _version: calls.append("verify_metadata"),
    )
    monkeypatch.setattr(
        prepare_release,
        "confirm_release",
        lambda *, tag_name: calls.append("confirm"),
    )
    monkeypatch.setattr(
        prepare_release,
        "sync_environment_if_requested",
        lambda *, paths, expected_version, sync_environment, uv_executable: calls.append(
            "sync"
        ),
    )
    monkeypatch.setattr(
        prepare_release,
        "run_release_validation",
        lambda _paths, *, skip_validation, validation_command: calls.append("validate"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_clean_worktree",
        lambda _root: calls.append("clean_after_validation"),
    )
    monkeypatch.setattr(
        prepare_release,
        "create_release_tag",
        lambda _paths, _tag_name: calls.append("tag"),
    )
    monkeypatch.setattr(
        prepare_release,
        "publish_release",
        lambda _paths, _tag_name, _options: calls.append("publish"),
    )

    prepare_release.run_release_workflow(
        paths=paths,
        version="v1.0.0",
        options=_workflow_options(dry_run=False),
    )

    assert calls == [
        "preflight",
        "verify_metadata",
        "confirm",
        "sync",
        "validate",
        "clean_after_validation",
        "tag",
        "publish",
    ]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["v1.0.0", "--no-pr"], "--no-pr requires --prepare-pr"),
        (["v1.0.0", "--release-branch", "release-test"], "--release-branch requires"),
        (
            ["v1.0.0", "--prepare-pr", "--no-gh-release"],
            "--no-gh-release only applies",
        ),
        (
            ["v1.0.0", "--prepare-only", "--local-only"],
            "--local-only only applies",
        ),
    ],
)
def test_main_rejects_mode_scoped_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    result = prepare_release.main([*argv, "--project-root", str(tmp_path)])

    assert result == 1
    assert message in capsys.readouterr().err
