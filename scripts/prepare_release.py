#!/usr/bin/env python3
"""Prepare a local issue-orchestrator release version bump."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


PROJECT_NAME = "issue-orchestrator"
DEFAULT_VALIDATION_COMMAND = ("make", "validate-pr")
RELEASE_BRANCH = "main"
RELEASE_REMOTE = "origin"
RELEASE_METADATA_FILES = {"pyproject.toml", "uv.lock"}
_STABLE_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SECTION_HEADER_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")
_VERSION_ASSIGNMENT_RE = re.compile(r'^(\s*version\s*=\s*)"([^"]*)"(\s*(?:#.*)?)$')


class ReleasePrepError(RuntimeError):
    """Raised when the release cannot be prepared safely."""


@dataclass(frozen=True)
class ReleasePaths:
    """Filesystem paths used by the release-prep workflow."""

    root: Path
    pyproject: Path
    lockfile: Path
    venv_python: Path

    @classmethod
    def from_root(cls, root: Path) -> "ReleasePaths":
        root = root.resolve()
        return cls(
            root=root,
            pyproject=root / "pyproject.toml",
            lockfile=root / "uv.lock",
            venv_python=root / ".venv" / "bin" / "python",
        )


@dataclass(frozen=True)
class ReleaseWorkflowOptions:
    """Options for the full release workflow."""

    dry_run: bool
    sync_environment: bool
    assume_yes: bool
    skip_validation: bool
    validation_command: tuple[str, ...]
    push: bool
    create_github_release: bool
    uv_executable: str | None


@dataclass(frozen=True)
class ReleasePrOptions:
    """Options for creating the release bump pull request."""

    dry_run: bool
    sync_environment: bool
    assume_yes: bool
    skip_validation: bool
    validation_command: tuple[str, ...]
    push: bool
    create_pull_request: bool
    branch_name: str | None
    uv_executable: str | None


def is_prerelease_version(version: str) -> bool:
    """Return True when ``version`` is a pre-1.0 (``0.x``) release.

    While the project is in ``0.x`` the public API is explicitly unstable
    (SemVer clause 4), so every release is published as a GitHub pre-release.
    That keeps the "Latest" pointer free for the first stable ``1.0.0`` and
    makes the instability visible on the releases page rather than only in
    prose. See ``docs/user/stability.md``.
    """
    normalized = version[1:] if version[:1].lower() == "v" else version
    major, _, _ = normalized.partition(".")
    return major == "0"


def github_release_command(tag_name: str) -> list[str]:
    """Return the ``gh release create`` command for ``tag_name``.

    Single owner for the release-publishing command so the dry-run preview and
    the real invocation cannot disagree about the pre-release marking.
    """
    command = ["gh", "release", "create", tag_name, "--generate-notes"]
    if is_prerelease_version(tag_name):
        command.append("--prerelease")
    return command


def normalize_release_version(raw_version: str) -> str:
    """Return a stable SemVer release version without a leading ``v``."""
    version = raw_version.strip()
    if version[:1].lower() == "v":
        version = version[1:]
    if not _STABLE_SEMVER_RE.fullmatch(version):
        raise ReleasePrepError(
            "Release version must be stable SemVer like 1.2.3 or v1.2.3; "
            f"got {raw_version!r}"
        )
    return version


def read_project_version(pyproject_path: Path) -> str:
    """Read the PEP 621 project version from ``pyproject.toml``."""
    with pyproject_path.open("rb") as file:
        data = tomllib.load(file)
    project = data.get("project")
    if not isinstance(project, dict):
        raise ReleasePrepError(f"{pyproject_path} has no [project] table")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ReleasePrepError(f"{pyproject_path} has no [project].version string")
    return version


def write_project_version(pyproject_path: Path, new_version: str) -> str:
    """Update only ``[project].version`` and return the previous version."""
    previous_version = read_project_version(pyproject_path)
    lines = pyproject_path.read_text(encoding="utf-8").splitlines(keepends=True)

    in_project_section = False
    version_line_indexes: list[int] = []
    for index, line in enumerate(lines):
        section_match = _SECTION_HEADER_RE.match(line.rstrip("\r\n"))
        if section_match:
            section_name = section_match.group(1)
            if section_name == "project":
                in_project_section = True
                continue
            if in_project_section:
                break

        if in_project_section and _VERSION_ASSIGNMENT_RE.match(line.rstrip("\r\n")):
            version_line_indexes.append(index)

    if not version_line_indexes:
        raise ReleasePrepError(
            f"{pyproject_path} has no version assignment in [project]"
        )
    if len(version_line_indexes) > 1:
        raise ReleasePrepError(
            f"{pyproject_path} has multiple version assignments in [project]"
        )

    line_index = version_line_indexes[0]
    original_line = lines[line_index]
    newline = "\n" if original_line.endswith("\n") else ""
    body = original_line[:-1] if newline else original_line
    if body.endswith("\r"):
        body = body[:-1]
        newline = "\r\n"

    assignment_match = _VERSION_ASSIGNMENT_RE.match(body)
    if assignment_match is None:
        raise ReleasePrepError(f"Could not parse version line in {pyproject_path}")

    prefix, _old, suffix = assignment_match.groups()
    lines[line_index] = f'{prefix}"{new_version}"{suffix}{newline}'
    pyproject_path.write_text("".join(lines), encoding="utf-8")
    return previous_version


def read_lock_project_version(lock_path: Path) -> str:
    """Read the editable project version recorded in ``uv.lock``."""
    with lock_path.open("rb") as file:
        data = tomllib.load(file)
    packages = data.get("package")
    if not isinstance(packages, list):
        raise ReleasePrepError(f"{lock_path} has no package entries")

    for package in packages:
        if not isinstance(package, dict):
            continue
        if package.get("name") != PROJECT_NAME:
            continue
        source = package.get("source")
        if isinstance(source, dict) and source.get("editable") == ".":
            version = package.get("version")
            if isinstance(version, str) and version:
                return version
            raise ReleasePrepError(
                f"{lock_path} has an editable {PROJECT_NAME} without a version"
            )

    raise ReleasePrepError(f"{lock_path} has no editable {PROJECT_NAME} package entry")


def verify_project_and_lock_versions(
    paths: ReleasePaths, expected_version: str
) -> None:
    """Fail if pyproject and uv.lock do not agree on the release version."""
    project_version = read_project_version(paths.pyproject)
    if project_version != expected_version:
        raise ReleasePrepError(
            f"{paths.pyproject} version is {project_version}, expected {expected_version}"
        )

    lock_version = read_lock_project_version(paths.lockfile)
    if lock_version != expected_version:
        raise ReleasePrepError(
            f"{paths.lockfile} version is {lock_version}, expected {expected_version}; "
            "run uv lock"
        )


def find_uv(explicit_uv: str | None = None) -> str:
    """Return the uv executable path, honoring an explicit value or ``UV``."""
    candidates = [explicit_uv, os.environ.get("UV"), shutil.which("uv")]
    for candidate in candidates:
        if candidate:
            return candidate
    raise ReleasePrepError(
        "uv is required to refresh uv.lock; install uv or set UV=/path/to/uv"
    )


def run_command(command: Sequence[str], *, cwd: Path) -> None:
    """Run a command and fail loudly on non-zero exit."""
    printable = " ".join(shlex.quote(part) for part in command)
    print(f"+ {printable}")
    result = subprocess.run(command, cwd=cwd, check=False)  # noqa: S603
    if result.returncode != 0:
        raise ReleasePrepError(
            f"Command failed with exit code {result.returncode}: {printable}"
        )


def run_optional_command(
    command: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run a command and return captured output without raising."""
    return subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def run_captured_command(
    command: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run a command, capture output, and fail loudly on non-zero exit."""
    result = run_optional_command(command, cwd=cwd)
    if result.returncode != 0:
        printable = " ".join(shlex.quote(part) for part in command)
        details = "\n".join(
            part
            for part in (
                result.stdout.strip(),
                result.stderr.strip(),
            )
            if part
        )
        raise ReleasePrepError(
            f"Command failed with exit code {result.returncode}: {printable}\n{details}"
        )
    return result


def read_installed_package_version(python_executable: Path, *, cwd: Path) -> str:
    """Return installed package metadata from a Python environment."""
    command = [
        str(python_executable),
        "-c",
        (f"from importlib.metadata import version; print(version({PROJECT_NAME!r}))"),
    ]
    result = run_optional_command(command, cwd=cwd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleasePrepError(
            f"Could not read installed {PROJECT_NAME} metadata from {python_executable}: {detail}"
        )
    return result.stdout.strip()


def verify_installed_package_version(
    paths: ReleasePaths, expected_version: str
) -> None:
    """Fail if the local venv metadata would render a stale Control Center version."""
    if not paths.venv_python.exists():
        print("No .venv Python found; skipping installed metadata verification.")
        return

    installed_version = read_installed_package_version(
        paths.venv_python, cwd=paths.root
    )
    if installed_version != expected_version:
        raise ReleasePrepError(
            f"Installed package metadata is {installed_version}, expected {expected_version}. "
            "Run uv sync --frozen --all-extras before opening the Control Center."
        )


def parse_command(raw_command: str) -> tuple[str, ...]:
    """Parse a configured shell-like command into argv."""
    command = tuple(shlex.split(raw_command))
    if not command:
        raise ReleasePrepError("Command cannot be empty")
    return command


def assert_tool_available(executable: str) -> None:
    """Fail if a required executable is not on PATH."""
    if shutil.which(executable) is None:
        raise ReleasePrepError(f"Required executable not found on PATH: {executable}")


def assert_clean_worktree(root: Path) -> None:
    """Require a clean git worktree before the script owns release changes."""
    result = run_captured_command(["git", "status", "--porcelain"], cwd=root)
    status = result.stdout.strip()
    if status:
        raise ReleasePrepError(
            "Release requires a clean git worktree before it starts.\n"
            "Commit, stash, or remove these changes first:\n"
            f"{status}"
        )


def assert_origin_remote_exists(root: Path) -> None:
    """Fail if the release remote is not configured."""
    result = run_captured_command(
        ["git", "remote", "get-url", RELEASE_REMOTE], cwd=root
    )
    remote_url = result.stdout.strip()
    if not remote_url:
        raise ReleasePrepError(f"Git remote {RELEASE_REMOTE!r} has no URL")


def assert_valid_branch_name(root: Path, branch_name: str) -> None:
    """Fail if git would reject the release PR branch name."""
    result = run_optional_command(
        ["git", "check-ref-format", "--branch", branch_name], cwd=root
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleasePrepError(f"Invalid release branch name {branch_name!r}: {detail}")


def fetch_origin_main(root: Path) -> None:
    """Fetch the release branch and tags before comparing local state."""
    run_command(
        [
            "git",
            "fetch",
            "--tags",
            RELEASE_REMOTE,
            f"refs/heads/{RELEASE_BRANCH}:refs/remotes/{RELEASE_REMOTE}/{RELEASE_BRANCH}",
        ],
        cwd=root,
    )


def remediate_outdated_local_main() -> str:
    """Return the command that updates a local checkout to the merged release.

    ``make release-pr`` leaves the checkout on the release PR branch, so after
    that PR merges the operator must switch back to ``main`` and fast-forward to
    the merge commit before ``make release`` can verify and tag it.
    """
    return (
        f"git switch {RELEASE_BRANCH} && "
        f"git pull --ff-only {RELEASE_REMOTE} {RELEASE_BRANCH}"
    )


def assert_current_branch_is_main(root: Path) -> None:
    """Require releases to be run from the local main branch."""
    result = run_captured_command(["git", "branch", "--show-current"], cwd=root)
    current_branch = result.stdout.strip()
    if current_branch != RELEASE_BRANCH:
        raise ReleasePrepError(
            f"Release must run from local branch {RELEASE_BRANCH!r}; "
            f"current branch is {current_branch or '<detached HEAD>'!r}.\n"
            f"Update your checkout to the merged release commit, then re-run:\n"
            f"  {remediate_outdated_local_main()}"
        )


def git_rev_parse(root: Path, ref: str) -> str:
    """Resolve a git ref to a commit SHA."""
    result = run_captured_command(["git", "rev-parse", "--verify", ref], cwd=root)
    return result.stdout.strip()


def assert_head_matches_origin_main(root: Path) -> None:
    """Require local HEAD to exactly match fetched origin/main before release."""
    head_sha = git_rev_parse(root, "HEAD")
    origin_main_ref = f"refs/remotes/{RELEASE_REMOTE}/{RELEASE_BRANCH}"
    origin_main_sha = git_rev_parse(root, origin_main_ref)
    if head_sha != origin_main_sha:
        raise ReleasePrepError(
            f"Release must start from {RELEASE_REMOTE}/{RELEASE_BRANCH}.\n"
            f"HEAD: {head_sha}\n"
            f"{RELEASE_REMOTE}/{RELEASE_BRANCH}: {origin_main_sha}\n"
            f"Update your checkout to the merged release commit, then re-run:\n"
            f"  {remediate_outdated_local_main()}"
        )


def remote_main_sha(root: Path) -> str:
    """Read origin/main's current SHA without updating local refs."""
    result = run_optional_command(
        [
            "git",
            "ls-remote",
            "--exit-code",
            RELEASE_REMOTE,
            f"refs/heads/{RELEASE_BRANCH}",
        ],
        cwd=root,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleasePrepError(
            f"Could not resolve {RELEASE_REMOTE}/{RELEASE_BRANCH} without fetching:\n{detail}"
        )

    first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    parts = first_line.split()
    if not parts:
        raise ReleasePrepError(f"Could not parse {RELEASE_REMOTE}/{RELEASE_BRANCH} SHA")
    return parts[0]


def assert_head_matches_remote_main(root: Path) -> None:
    """Require local HEAD to match remote origin/main without mutating refs."""
    head_sha = git_rev_parse(root, "HEAD")
    origin_main_sha = remote_main_sha(root)
    if head_sha != origin_main_sha:
        raise ReleasePrepError(
            f"Release must start from current remote {RELEASE_REMOTE}/{RELEASE_BRANCH}.\n"
            f"HEAD: {head_sha}\n"
            f"{RELEASE_REMOTE}/{RELEASE_BRANCH}: {origin_main_sha}\n"
            f"Update your checkout to the merged release commit, then re-run:\n"
            f"  {remediate_outdated_local_main()}"
        )


def assert_local_tag_absent(root: Path, tag_name: str) -> None:
    """Fail if the release tag already exists locally."""
    result = run_optional_command(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag_name}"],
        cwd=root,
    )
    if result.returncode == 0:
        raise ReleasePrepError(f"Local tag already exists: {tag_name}")


def assert_local_branch_absent(root: Path, branch_name: str) -> None:
    """Fail if the release PR branch already exists locally."""
    result = run_optional_command(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=root,
    )
    if result.returncode == 0:
        raise ReleasePrepError(f"Local release branch already exists: {branch_name}")
    if result.returncode != 1:
        raise ReleasePrepError(f"Could not check local branch: {branch_name}")


def assert_remote_branch_absent(root: Path, branch_name: str) -> None:
    """Fail if the release PR branch already exists on origin."""
    result = run_optional_command(
        [
            "git",
            "ls-remote",
            "--exit-code",
            "--heads",
            RELEASE_REMOTE,
            f"refs/heads/{branch_name}",
        ],
        cwd=root,
    )
    if result.returncode == 0:
        raise ReleasePrepError(
            f"Remote release branch already exists on {RELEASE_REMOTE}: {branch_name}"
        )
    if result.returncode != 2:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleasePrepError(
            f"Could not check remote release branch {branch_name!r}:\n{detail}"
        )


def assert_remote_tag_absent(root: Path, tag_name: str) -> None:
    """Fail if the release tag already exists on origin."""
    result = run_optional_command(
        [
            "git",
            "ls-remote",
            "--exit-code",
            "--tags",
            RELEASE_REMOTE,
            f"refs/tags/{tag_name}",
        ],
        cwd=root,
    )
    if result.returncode == 0:
        raise ReleasePrepError(
            f"Remote tag already exists on {RELEASE_REMOTE}: {tag_name}"
        )
    if result.returncode != 2:
        printable = (
            f"git ls-remote --exit-code --tags {RELEASE_REMOTE} refs/tags/{tag_name}"
        )
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleasePrepError(
            f"Could not check remote tag availability: {printable}\n{detail}"
        )


def assert_github_release_absent(root: Path, tag_name: str) -> None:
    """Fail if GitHub already has a release for this tag."""
    auth_result = run_optional_command(["gh", "auth", "status"], cwd=root)
    if auth_result.returncode != 0:
        detail = auth_result.stderr.strip() or auth_result.stdout.strip()
        raise ReleasePrepError(f"GitHub CLI is not authenticated:\n{detail}")

    release_result = run_optional_command(["gh", "release", "view", tag_name], cwd=root)
    if release_result.returncode == 0:
        raise ReleasePrepError(f"GitHub release already exists: {tag_name}")


def release_metadata_changed_files(root: Path) -> set[str]:
    """Return dirty files across worktree, index, and untracked paths."""
    changed_files = set(
        run_captured_command(
            ["git", "diff", "--name-only"], cwd=root
        ).stdout.splitlines()
    )
    changed_files.update(
        run_captured_command(
            ["git", "diff", "--cached", "--name-only"], cwd=root
        ).stdout.splitlines()
    )
    changed_files.update(
        run_captured_command(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root,
        ).stdout.splitlines()
    )
    return changed_files


def assert_only_release_metadata_changed(root: Path) -> set[str]:
    """Fail if the release PR would commit anything except release metadata."""
    changed_files = release_metadata_changed_files(root)
    unexpected_files = sorted(changed_files - RELEASE_METADATA_FILES)
    if unexpected_files:
        raise ReleasePrepError(
            "Release PR changed unexpected files; only pyproject.toml and uv.lock "
            "may be dirty:\n" + "\n".join(unexpected_files)
        )
    return changed_files


def confirm_release(
    *,
    tag_name: str,
    input_func: Callable[[str], str] = input,
) -> None:
    """Ask the operator for an exact tag confirmation."""
    try:
        answer = input_func(f"Type {tag_name} to release: ").strip()
    except EOFError as exc:
        raise ReleasePrepError(
            "Release confirmation required. Re-run with --yes to skip the prompt."
        ) from exc
    if answer != tag_name:
        raise ReleasePrepError(
            f"Release cancelled; expected exact confirmation {tag_name!r}, got {answer!r}"
        )


def print_release_plan(
    *,
    tag_name: str,
    current_project_version: str,
    current_lock_version: str,
    target_version: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Print the release plan before confirmation."""
    print(f"Preparing full {PROJECT_NAME} release {tag_name}")
    print(f"Current pyproject version: {current_project_version}")
    print(f"Current uv.lock version: {current_lock_version}")
    print(f"Release source: {RELEASE_REMOTE}/{RELEASE_BRANCH}")
    print("")
    print("Plan:")
    print(
        f"  1. Require clean local {RELEASE_BRANCH} matching fetched {RELEASE_REMOTE}/{RELEASE_BRANCH}"
    )
    print(
        f"  2. Verify pyproject.toml and uv.lock already contain {target_version}"
    )
    if options.sync_environment:
        print("  3. Sync .venv and verify installed package metadata")
    else:
        print("  3. Skip .venv sync and installed metadata verification")
    if options.skip_validation:
        print("  4. Skip validation")
    else:
        print(f"  4. Run validation: {' '.join(options.validation_command)}")
    print("  5. Re-check that the git worktree is clean")
    print(f"  6. Create annotated tag {tag_name}")
    if options.push:
        print(f"  7. Push only tag {tag_name} to {RELEASE_REMOTE}")
    else:
        print("  7. Leave tag local")
    if options.create_github_release:
        print(f"  8. Create GitHub release {tag_name} with generated notes")
    else:
        print("  8. Skip GitHub release creation")


def print_dry_run_commands(
    *,
    tag_name: str,
    target_version: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Print the commands the full release would run."""
    print("")
    print("Dry run: no files, git refs, or GitHub releases will be changed.")
    print("+ git status --porcelain")
    print(f"+ git remote get-url {RELEASE_REMOTE}")
    print("+ git branch --show-current")
    print("+ git rev-parse --verify HEAD")
    print(f"+ git ls-remote --exit-code {RELEASE_REMOTE} refs/heads/{RELEASE_BRANCH}")
    if options.push:
        print(
            f"+ git ls-remote --exit-code --tags {RELEASE_REMOTE} refs/tags/{tag_name}"
        )
    if options.create_github_release:
        print("+ gh auth status")
        print(f"+ gh release view {tag_name}")
    print(f"Would verify pyproject.toml and uv.lock already contain {target_version}")
    if options.sync_environment:
        print("+ uv sync --frozen --all-extras")
    if not options.skip_validation:
        print("+ " + " ".join(shlex.quote(part) for part in options.validation_command))
    print("+ git status --porcelain")
    print(f"+ git tag -a {tag_name} -m 'Release {tag_name}'")
    if options.push:
        print(
            f"+ git push {RELEASE_REMOTE} refs/tags/{tag_name}:refs/tags/{tag_name}"
        )
    if options.create_github_release:
        print("+ " + " ".join(github_release_command(tag_name)))


def default_release_pr_branch_name(tag_name: str) -> str:
    """Return the deterministic branch name for a release bump PR."""
    return f"release-{tag_name}"


def release_pr_body(tag_name: str) -> str:
    """Return the pull request body for the generated release bump PR."""
    return (
        f"Prepare {tag_name} release metadata.\n\n"
        "After this PR is merged to main, update your local checkout to the "
        "merged commit and run the final release:\n\n"
        f"```bash\n{remediate_outdated_local_main()}\n"
        f"make release VERSION={tag_name}\n```"
    )


def print_release_pr_plan(
    *,
    tag_name: str,
    target_version: str,
    current_project_version: str,
    current_lock_version: str,
    branch_name: str,
    options: ReleasePrOptions,
) -> None:
    """Print the release PR creation plan before confirmation."""
    print(f"Preparing release bump PR for {tag_name}")
    print(f"Current pyproject version: {current_project_version}")
    print(f"Current uv.lock version: {current_lock_version}")
    print(f"Release PR branch: {branch_name}")
    print("")
    print("Plan:")
    print(f"  1. Require a clean worktree and fetch {RELEASE_REMOTE}/{RELEASE_BRANCH}")
    print(f"  2. Create {branch_name} from {RELEASE_REMOTE}/{RELEASE_BRANCH}")
    print(f"  3. Set package version to {target_version} and refresh uv.lock")
    if options.sync_environment:
        print("  4. Sync .venv and verify installed package metadata")
    else:
        print("  4. Skip .venv sync and installed metadata verification")
    print("  5. Commit only pyproject.toml and uv.lock with signoff")
    if options.skip_validation:
        print("  6. Skip validation")
    else:
        print(f"  6. Run validation: {' '.join(options.validation_command)}")
    if options.push:
        print(f"  7. Push {branch_name} to {RELEASE_REMOTE}")
    else:
        print("  7. Leave release PR branch local")
    if options.create_pull_request:
        print(f"  8. Open pull request for {tag_name}")
    else:
        print("  8. Skip pull request creation")


def print_release_pr_dry_run_commands(
    *,
    tag_name: str,
    target_version: str,
    branch_name: str,
    options: ReleasePrOptions,
) -> None:
    """Print the commands the release PR workflow would run."""
    print("")
    print("Dry run: no files, branches, commits, pushes, or PRs will be changed.")
    print("+ git status --porcelain")
    print(f"+ git remote get-url {RELEASE_REMOTE}")
    print(f"+ git ls-remote --exit-code {RELEASE_REMOTE} refs/heads/{RELEASE_BRANCH}")
    print(f"+ git show-ref --verify --quiet refs/heads/{branch_name}")
    if options.push:
        print(
            f"+ git ls-remote --exit-code --heads {RELEASE_REMOTE} refs/heads/{branch_name}"
        )
    print(f"+ git ls-remote --exit-code --tags {RELEASE_REMOTE} refs/tags/{tag_name}")
    if options.create_pull_request:
        print("+ gh auth status")
        print(f"+ gh release view {tag_name}")
    print(
        f"+ git switch --create {branch_name} --no-track {RELEASE_REMOTE}/{RELEASE_BRANCH}"
    )
    print(f"Would set [project].version to {target_version}")
    print("+ uv lock")
    if options.sync_environment:
        print("+ uv sync --frozen --all-extras")
    print("+ git add pyproject.toml uv.lock")
    print(f"+ git commit -s -m 'Release {tag_name}'")
    if not options.skip_validation:
        print("+ " + " ".join(shlex.quote(part) for part in options.validation_command))
    if options.push:
        print(f"+ git push -u {RELEASE_REMOTE} {branch_name}")
    if options.create_pull_request:
        print(
            "+ gh pr create "
            f"--base {RELEASE_BRANCH} --head {branch_name} "
            f"--title 'Release {tag_name}' --body <generated>"
        )


def run_release_preflight(
    *,
    paths: ReleasePaths,
    tag_name: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Run fail-fast checks before asking for release confirmation."""
    assert_tool_available("git")
    if options.create_github_release:
        assert_tool_available("gh")
    if options.sync_environment:
        find_uv(options.uv_executable)
    assert_clean_worktree(paths.root)
    assert_origin_remote_exists(paths.root)
    fetch_origin_main(paths.root)
    assert_current_branch_is_main(paths.root)
    assert_head_matches_origin_main(paths.root)
    assert_local_tag_absent(paths.root, tag_name)
    if options.push:
        assert_remote_tag_absent(paths.root, tag_name)
    if options.create_github_release:
        assert_github_release_absent(paths.root, tag_name)


def run_release_pr_preflight(
    *,
    paths: ReleasePaths,
    tag_name: str,
    branch_name: str,
    options: ReleasePrOptions,
) -> None:
    """Run fail-fast checks before creating a release bump PR."""
    assert_tool_available("git")
    if options.create_pull_request:
        assert_tool_available("gh")
    find_uv(options.uv_executable)
    assert_clean_worktree(paths.root)
    assert_origin_remote_exists(paths.root)
    assert_valid_branch_name(paths.root, branch_name)
    fetch_origin_main(paths.root)
    assert_local_branch_absent(paths.root, branch_name)
    if options.push:
        assert_remote_branch_absent(paths.root, branch_name)
    assert_local_tag_absent(paths.root, tag_name)
    assert_remote_tag_absent(paths.root, tag_name)
    if options.create_pull_request:
        assert_github_release_absent(paths.root, tag_name)


def run_release_pr_dry_run_preflight(
    *,
    paths: ReleasePaths,
    tag_name: str,
    branch_name: str,
    options: ReleasePrOptions,
) -> None:
    """Run read-only checks before a dry-run release bump PR."""
    assert_tool_available("git")
    if options.create_pull_request:
        assert_tool_available("gh")
    find_uv(options.uv_executable)
    assert_clean_worktree(paths.root)
    assert_origin_remote_exists(paths.root)
    remote_main_sha(paths.root)
    assert_valid_branch_name(paths.root, branch_name)
    assert_local_branch_absent(paths.root, branch_name)
    if options.push:
        assert_remote_branch_absent(paths.root, branch_name)
    assert_local_tag_absent(paths.root, tag_name)
    assert_remote_tag_absent(paths.root, tag_name)
    if options.create_pull_request:
        assert_github_release_absent(paths.root, tag_name)


def run_release_dry_run_preflight(
    *,
    paths: ReleasePaths,
    tag_name: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Run read-only checks that prove whether release can start."""
    assert_tool_available("git")
    if options.create_github_release:
        assert_tool_available("gh")
    if options.sync_environment:
        find_uv(options.uv_executable)
    assert_clean_worktree(paths.root)
    assert_origin_remote_exists(paths.root)
    assert_current_branch_is_main(paths.root)
    assert_head_matches_remote_main(paths.root)
    assert_local_tag_absent(paths.root, tag_name)
    if options.push:
        assert_remote_tag_absent(paths.root, tag_name)
    if options.create_github_release:
        assert_github_release_absent(paths.root, tag_name)


def apply_release_metadata(
    *,
    paths: ReleasePaths,
    target_version: str,
    sync_environment: bool,
    uv_executable: str | None,
) -> None:
    """Update version files, refresh the lockfile, and verify metadata."""
    uv = find_uv(uv_executable)
    previous_version = write_project_version(paths.pyproject, target_version)
    if previous_version == target_version:
        print(f"pyproject.toml already at {target_version}")
    else:
        print(f"Updated pyproject.toml: {previous_version} -> {target_version}")

    run_command([uv, "lock"], cwd=paths.root)
    verify_project_and_lock_versions(paths, target_version)
    sync_environment_if_requested(
        paths=paths,
        expected_version=target_version,
        sync_environment=sync_environment,
        uv_executable=uv_executable,
    )


def verify_release_metadata_ready(
    paths: ReleasePaths, target_version: str
) -> None:
    """Require the already-merged release metadata to match the target version."""
    try:
        verify_project_and_lock_versions(paths, target_version)
    except ReleasePrepError as exc:
        raise ReleasePrepError(
            "Release version must already be present in pyproject.toml and uv.lock. "
            f"Run `make prepare-release VERSION=v{target_version}`, merge that PR "
            f"into {RELEASE_BRANCH}, then run release from the updated "
            f"{RELEASE_BRANCH} branch.\n{exc}"
        ) from exc
    print(f"Version {target_version} already present in pyproject.toml and uv.lock.")


def require_owned_venv(root: Path) -> None:
    """Refuse to install this project into a venv it does not exclusively own.

    Routed through the same authority every other caller uses, resolved from
    this project's own tree rather than re-derived here. Fails CLOSED: a guard
    that is missing, unreadable, or non-executable is not evidence of
    ownership, and an unrunnable one must surface as this module's declared
    error rather than a raw OSError.
    """
    guard = root / "src" / "issue_orchestrator" / "resources" / "venv_guard.sh"
    try:
        completed = subprocess.run(
            [str(guard), "decide", "--quiet", "--checkout", str(root)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        outcome = completed.returncode
        remedy = ""
        for line in completed.stdout.splitlines():
            if line.startswith("remedy="):
                remedy = line.partition("=")[2].strip()
    except OSError as exc:
        raise ReleasePrepError(
            f"Cannot consult the venv mutation authority at {guard}: {exc}"
        ) from exc

    if outcome == 0:
        return
    reasons = {
        1: "its venv is shared from another checkout",
        2: "its venv is a dangling symlink",
        3: "its venv is external and not bound to this checkout",
    }
    reason = reasons.get(
        outcome, f"the venv mutation authority returned {outcome}"
    )
    message = f"Refusing to sync the environment for {root}: {reason}."
    if remedy:
        message += f"\n  To fix: {remedy}"
    raise ReleasePrepError(message)


def sync_environment_if_requested(
    *,
    paths: ReleasePaths,
    expected_version: str,
    sync_environment: bool,
    uv_executable: str | None,
) -> None:
    """Sync the local environment before verifying installed package metadata."""
    if not sync_environment:
        print(
            "Skipped environment sync; run uv sync --frozen --all-extras before opening Control Center."
        )
        return

    require_owned_venv(paths.root)
    uv = find_uv(uv_executable)
    run_command([uv, "sync", "--frozen", "--all-extras"], cwd=paths.root)
    verify_installed_package_version(paths, expected_version)


def create_release_pr_branch(paths: ReleasePaths, branch_name: str) -> None:
    """Create the release PR branch from fetched origin/main."""
    run_command(
        [
            "git",
            "switch",
            "--create",
            branch_name,
            "--no-track",
            f"{RELEASE_REMOTE}/{RELEASE_BRANCH}",
        ],
        cwd=paths.root,
    )


def commit_release_metadata(paths: ReleasePaths, tag_name: str) -> None:
    """Stage and commit only release metadata files."""
    changed_files = assert_only_release_metadata_changed(paths.root)
    if not changed_files:
        raise ReleasePrepError(
            "Release metadata did not change. "
            f"{tag_name} may already be prepared on this branch."
        )

    run_command(["git", "add", "pyproject.toml", "uv.lock"], cwd=paths.root)
    staged_files = set(
        run_captured_command(
            ["git", "diff", "--cached", "--name-only"],
            cwd=paths.root,
        ).stdout.splitlines()
    )
    unexpected_files = sorted(staged_files - RELEASE_METADATA_FILES)
    if unexpected_files:
        raise ReleasePrepError(
            "Release PR would commit unexpected staged files:\n"
            + "\n".join(unexpected_files)
        )
    if not staged_files:
        raise ReleasePrepError("Release metadata did not stage any changes.")

    run_command(["git", "commit", "-s", "-m", f"Release {tag_name}"], cwd=paths.root)


def run_release_validation(
    paths: ReleasePaths,
    *,
    skip_validation: bool,
    validation_command: tuple[str, ...],
) -> None:
    """Run the configured release validation phase."""
    if skip_validation:
        print("Skipped validation.")
        return
    run_command(validation_command, cwd=paths.root)


def create_release_tag(paths: ReleasePaths, tag_name: str) -> None:
    """Create the annotated release tag on the verified release commit."""
    assert_local_tag_absent(paths.root, tag_name)
    run_command(
        ["git", "tag", "-a", tag_name, "-m", f"Release {tag_name}"], cwd=paths.root
    )


def print_github_release_recovery_hint(tag_name: str) -> None:
    """Print the exact command that finishes a release after ``gh`` failed.

    The tag is already pushed at this point, so the full release command cannot
    be rerun. The operator has to create the GitHub release by hand — which is
    exactly the path where a remembered-by-hand ``--prerelease`` flag goes
    missing and a ``0.x`` tag publishes as a normal release. Emitting the
    command from ``github_release_command`` keeps the version-derived
    pre-release policy in charge of recovery too.
    """
    printable = " ".join(shlex.quote(part) for part in github_release_command(tag_name))
    print(
        f"\nThe {tag_name} tag was pushed but the GitHub release was not created.",
        file=sys.stderr,
    )
    print(
        "Do not rerun the release command; the remote tag already exists. "
        "Create the release from the pushed tag with exactly:",
        file=sys.stderr,
    )
    print(f"  {printable}", file=sys.stderr)


def publish_release(
    paths: ReleasePaths, tag_name: str, options: ReleaseWorkflowOptions
) -> None:
    """Push the release tag and create the GitHub release when enabled."""
    if options.push:
        run_command(
            [
                "git",
                "push",
                RELEASE_REMOTE,
                f"refs/tags/{tag_name}:refs/tags/{tag_name}",
            ],
            cwd=paths.root,
        )
    else:
        print("Skipped push; tag remains local.")

    if options.create_github_release:
        try:
            run_command(github_release_command(tag_name), cwd=paths.root)
        except ReleasePrepError:
            print_github_release_recovery_hint(tag_name)
            raise
    else:
        print("Skipped GitHub release creation.")


def publish_release_pr(
    paths: ReleasePaths,
    *,
    tag_name: str,
    branch_name: str,
    options: ReleasePrOptions,
) -> None:
    """Push the release branch and create the release bump PR."""
    if options.push:
        run_command(["git", "push", "-u", RELEASE_REMOTE, branch_name], cwd=paths.root)
    else:
        print("Skipped push; release PR branch remains local.")

    if options.create_pull_request:
        run_command(
            [
                "gh",
                "pr",
                "create",
                "--base",
                RELEASE_BRANCH,
                "--head",
                branch_name,
                "--title",
                f"Release {tag_name}",
                "--body",
                release_pr_body(tag_name),
            ],
            cwd=paths.root,
        )
    else:
        print("Skipped pull request creation.")


def print_release_pr_recovery_hint(branch_name: str) -> None:
    """Print operator recovery guidance after a release PR branch failure."""
    print(
        "\nRelease PR workflow stopped after creating the local release branch.",
        file=sys.stderr,
    )
    print(f"Current branch may be {branch_name!r}. To discard and retry:", file=sys.stderr)
    print("  git switch -", file=sys.stderr)
    print(f"  git branch -D {branch_name}", file=sys.stderr)
    print("If the branch was pushed before the failure, also run:", file=sys.stderr)
    print(f"  git push {RELEASE_REMOTE} --delete {branch_name}", file=sys.stderr)


def run_release_workflow(
    *,
    paths: ReleasePaths,
    version: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Run the full confirmed release workflow."""
    if not options.push and options.create_github_release:
        raise ReleasePrepError("--local-only cannot create a GitHub release")

    target_version = normalize_release_version(version)
    tag_name = f"v{target_version}"
    current_project_version = read_project_version(paths.pyproject)
    current_lock_version = read_lock_project_version(paths.lockfile)

    print_release_plan(
        tag_name=tag_name,
        current_project_version=current_project_version,
        current_lock_version=current_lock_version,
        target_version=target_version,
        options=options,
    )

    if options.dry_run:
        run_release_dry_run_preflight(paths=paths, tag_name=tag_name, options=options)
        verify_release_metadata_ready(paths, target_version)
        print_dry_run_commands(
            tag_name=tag_name,
            target_version=target_version,
            options=options,
        )
        return

    run_release_preflight(paths=paths, tag_name=tag_name, options=options)
    verify_release_metadata_ready(paths, target_version)
    if not options.assume_yes:
        confirm_release(tag_name=tag_name)

    sync_environment_if_requested(
        paths=paths,
        expected_version=target_version,
        sync_environment=options.sync_environment,
        uv_executable=options.uv_executable,
    )
    run_release_validation(
        paths,
        skip_validation=options.skip_validation,
        validation_command=options.validation_command,
    )
    assert_clean_worktree(paths.root)
    create_release_tag(paths, tag_name)
    publish_release(paths, tag_name, options)
    print("")
    print(f"Release complete: {tag_name}")


def run_release_pr_workflow(
    *,
    paths: ReleasePaths,
    version: str,
    options: ReleasePrOptions,
) -> None:
    """Create the release metadata PR with minimal operator steps."""
    if not options.push and options.create_pull_request:
        raise ReleasePrepError("--local-only cannot create a pull request")

    target_version = normalize_release_version(version)
    tag_name = f"v{target_version}"
    branch_name = options.branch_name or default_release_pr_branch_name(tag_name)
    current_project_version = read_project_version(paths.pyproject)
    current_lock_version = read_lock_project_version(paths.lockfile)

    print_release_pr_plan(
        tag_name=tag_name,
        target_version=target_version,
        current_project_version=current_project_version,
        current_lock_version=current_lock_version,
        branch_name=branch_name,
        options=options,
    )

    if options.dry_run:
        run_release_pr_dry_run_preflight(
            paths=paths,
            tag_name=tag_name,
            branch_name=branch_name,
            options=options,
        )
        print_release_pr_dry_run_commands(
            tag_name=tag_name,
            target_version=target_version,
            branch_name=branch_name,
            options=options,
        )
        return

    run_release_pr_preflight(
        paths=paths,
        tag_name=tag_name,
        branch_name=branch_name,
        options=options,
    )
    if not options.assume_yes:
        confirm_release(tag_name=tag_name)

    create_release_pr_branch(paths, branch_name)
    try:
        apply_release_metadata(
            paths=paths,
            target_version=target_version,
            sync_environment=options.sync_environment,
            uv_executable=options.uv_executable,
        )
        commit_release_metadata(paths, tag_name)
        run_release_validation(
            paths,
            skip_validation=options.skip_validation,
            validation_command=options.validation_command,
        )
        assert_clean_worktree(paths.root)
        publish_release_pr(
            paths,
            tag_name=tag_name,
            branch_name=branch_name,
            options=options,
        )
    except ReleasePrepError:
        print_release_pr_recovery_hint(branch_name)
        raise
    print("")
    print(f"Release PR ready for {tag_name}.")
    print("After it is merged, update your checkout to the merged commit and release:")
    print(f"  {remediate_outdated_local_main()}")
    print(f"  make release VERSION={tag_name}")


def prepare_release(
    *,
    paths: ReleasePaths,
    version: str,
    dry_run: bool,
    sync_environment: bool,
    uv_executable: str | None,
) -> None:
    """Prepare the release bump and verify the version sources."""
    target_version = normalize_release_version(version)
    tag_name = f"v{target_version}"
    current_project_version = read_project_version(paths.pyproject)
    current_lock_version = read_lock_project_version(paths.lockfile)

    print(f"Preparing {PROJECT_NAME} release {tag_name}")
    print(f"Current pyproject version: {current_project_version}")
    print(f"Current uv.lock version: {current_lock_version}")

    if dry_run:
        print(f"Dry run: would set pyproject.toml and uv.lock to {target_version}")
        if sync_environment:
            print("Dry run: would run uv sync --frozen --all-extras")
        return

    apply_release_metadata(
        paths=paths,
        target_version=target_version,
        sync_environment=sync_environment,
        uv_executable=uv_executable,
    )

    print("")
    print(f"Release file prep complete for {tag_name}.")
    print(
        f"Merge these files to {RELEASE_BRANCH}, then run `make release VERSION={tag_name}` "
        f"from the updated clean {RELEASE_BRANCH} branch."
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the issue-orchestrator release workflow. By default this asks "
            "for confirmation, verifies release metadata already exists on "
            "main, validates, tags, pushes the tag, and creates the GitHub release."
        )
    )
    parser.add_argument("version", help="Release version, e.g. 1.0.0 or v1.0.0")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root. Defaults to this script's parent repo.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Only bump pyproject.toml, refresh uv.lock, and verify local metadata.",
    )
    parser.add_argument(
        "--prepare-pr",
        action="store_true",
        help="Create, validate, push, and open the release metadata pull request.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without changing files, git refs, or GitHub releases.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not run uv sync or verify local .venv package metadata.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive release confirmation prompt.",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Do not run the release validation command.",
    )
    parser.add_argument(
        "--validation-command",
        default=" ".join(DEFAULT_VALIDATION_COMMAND),
        help="Validation command to run before tagging. Default: make validate-pr.",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help=(
            "For release: tag locally, but do not push or create a GitHub release. "
            "For --prepare-pr: commit locally, but do not push or create a PR."
        ),
    )
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="With --prepare-pr, push the release branch but skip pull request creation.",
    )
    parser.add_argument(
        "--release-branch",
        help="With --prepare-pr, use this branch name instead of release-vX.Y.Z.",
    )
    parser.add_argument(
        "--no-gh-release",
        action="store_true",
        help="Push the tag but skip gh release create.",
    )
    parser.add_argument(
        "--uv",
        help="Path to uv executable. Defaults to UV env var or uv on PATH.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = ReleasePaths.from_root(args.project_root)
        if args.prepare_only and args.prepare_pr:
            raise ReleasePrepError("--prepare-only and --prepare-pr are mutually exclusive")
        if args.no_pr and not args.prepare_pr:
            raise ReleasePrepError("--no-pr requires --prepare-pr")
        if args.release_branch and not args.prepare_pr:
            raise ReleasePrepError("--release-branch requires --prepare-pr")
        if args.no_gh_release and (args.prepare_only or args.prepare_pr):
            raise ReleasePrepError("--no-gh-release only applies to final release")
        if args.local_only and args.prepare_only:
            raise ReleasePrepError(
                "--local-only only applies to final release or --prepare-pr"
            )
        if args.prepare_pr:
            run_release_pr_workflow(
                paths=paths,
                version=args.version,
                options=ReleasePrOptions(
                    dry_run=args.dry_run,
                    sync_environment=not args.no_sync,
                    assume_yes=args.yes,
                    skip_validation=args.skip_validation,
                    validation_command=parse_command(args.validation_command),
                    push=not args.local_only,
                    create_pull_request=not args.local_only and not args.no_pr,
                    branch_name=args.release_branch,
                    uv_executable=args.uv,
                ),
            )
        elif args.prepare_only:
            prepare_release(
                paths=paths,
                version=args.version,
                dry_run=args.dry_run,
                sync_environment=not args.no_sync,
                uv_executable=args.uv,
            )
        else:
            run_release_workflow(
                paths=paths,
                version=args.version,
                options=ReleaseWorkflowOptions(
                    dry_run=args.dry_run,
                    sync_environment=not args.no_sync,
                    assume_yes=args.yes,
                    skip_validation=args.skip_validation,
                    validation_command=parse_command(args.validation_command),
                    push=not args.local_only,
                    create_github_release=not args.local_only
                    and not args.no_gh_release,
                    uv_executable=args.uv,
                ),
            )
    except ReleasePrepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
