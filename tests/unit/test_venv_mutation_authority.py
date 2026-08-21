"""The behavior-level mutation authority and its two hardest properties.

It must work against repositories that are not this one (E2E prepares
user-selected targets), and it must never authorize an environment it cannot
prove is exclusively this checkout's.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.infra.venv_mutation import (
    GUARD_RESOURCE,
    VenvMutationAuthority,
    VenvMutationRefused,
    VenvOutcome,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _authority() -> VenvMutationAuthority:
    return VenvMutationAuthority(LocalCommandRunner())


def _checkout(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text("[project]\nname='x'\n")
    return path


# ---- A3: arbitrary target repositories ------------------------------------


def test_authority_works_against_a_repo_that_does_not_carry_the_guard(
    tmp_path: Path,
) -> None:
    """E2E prepares user-selected repositories.

    A Python project has no reason to ship issue-orchestrator's internal
    script, so resolving the authority from the *target* made preparation fail
    purely because an unrelated file was absent.
    """
    foreign = _checkout(tmp_path, "someones-django-app")
    (foreign / ".venv").mkdir()
    assert not (foreign / "scripts").exists()

    decision = _authority().authorize(checkout=foreign)

    assert decision.outcome is VenvOutcome.OWNED
    assert decision.sync_args


def test_authority_resolves_the_guard_from_this_installation(tmp_path: Path) -> None:
    assert GUARD_RESOURCE.is_file()
    assert GUARD_RESOURCE.is_relative_to(REPO_ROOT / "src")


def test_authority_still_refuses_a_shared_venv_in_a_foreign_repo(tmp_path: Path) -> None:
    owner = _checkout(tmp_path, "owner")
    (owner / ".venv").mkdir()
    foreign = _checkout(tmp_path, "someones-app")
    (foreign / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    decision = _authority().authorize(checkout=foreign)

    assert decision.outcome is VenvOutcome.SHARED
    assert not decision.may_install_project
    assert "--no-install-project" in decision.sync_args


def test_authority_raises_its_domain_error_for_a_non_executable_guard(
    tmp_path: Path,
) -> None:
    """Path.exists() is not "runnable"; a mode-0644 guard raised PermissionError."""
    guard = tmp_path / "venv_guard.sh"
    guard.write_text("#!/usr/bin/env bash\nexit 0\n")
    guard.chmod(0o644)
    authority = VenvMutationAuthority(LocalCommandRunner(), guard_path=guard)

    with pytest.raises(VenvMutationRefused):
        authority.authorize(checkout=_checkout(tmp_path, "repo"))


def test_authority_raises_its_domain_error_for_a_missing_guard(tmp_path: Path) -> None:
    authority = VenvMutationAuthority(
        LocalCommandRunner(), guard_path=tmp_path / "absent.sh"
    )

    with pytest.raises(VenvMutationRefused):
        authority.authorize(checkout=_checkout(tmp_path, "repo"))


# ---- A1: a decision without arguments is not a licence --------------------


@pytest.mark.parametrize(
    "body",
    ['echo "outcome=owned"\nexit 0\n', 'echo "outcome=owned"\necho "sync_args="\nexit 0\n', "exit 0\n"],
)
def test_authority_refuses_an_authorized_outcome_with_no_arguments(
    body: str, tmp_path: Path
) -> None:
    guard = tmp_path / "guard.sh"
    guard.write_text("#!/usr/bin/env bash\n" + body)
    guard.chmod(0o755)
    authority = VenvMutationAuthority(LocalCommandRunner(), guard_path=guard)

    with pytest.raises(VenvMutationRefused):
        authority.authorize(checkout=_checkout(tmp_path, "repo"))


# ---- A2: the decision binds the target ------------------------------------


def test_pinned_env_binds_uv_to_the_authorized_environment(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "repo")
    (checkout / ".venv").mkdir()
    decision = _authority().authorize(checkout=checkout)

    env = VenvMutationAuthority.pinned_env(decision)

    assert env["UV_PROJECT_ENVIRONMENT"] == str(decision.venv)


def test_pinned_env_drops_a_destructive_clear_override(tmp_path: Path) -> None:
    """UV_VENV_CLEAR makes `uv venv` delete and rebuild the target."""
    checkout = _checkout(tmp_path, "repo")
    (checkout / ".venv").mkdir()
    decision = _authority().authorize(checkout=checkout)

    os.environ["UV_VENV_CLEAR"] = "1"
    try:
        env = VenvMutationAuthority.pinned_env(decision)
    finally:
        os.environ.pop("UV_VENV_CLEAR", None)

    assert "UV_VENV_CLEAR" not in env


# ---- A5: "not a checkout" does not prove exclusive use --------------------


def _guard(checkout: Path, venv: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GUARD_RESOURCE), *(args or ("decide",)), "--quiet",
         "--checkout", str(checkout), "--venv", str(venv)],
        capture_output=True,
        text=True,
    )


def test_an_external_venv_is_refused_until_ownership_is_bound(tmp_path: Path) -> None:
    """Two checkouts can point CC_VENV_PATH at one environment.

    A parent that is not a checkout proves only that -- not that this checkout
    is its exclusive user.
    """
    external = tmp_path / "envs" / "shared-cc"
    external.mkdir(parents=True)
    first = _checkout(tmp_path, "first")
    second = _checkout(tmp_path, "second")

    assert _guard(first, external).returncode == 3
    assert _guard(second, external).returncode == 3


def test_claiming_binds_an_external_venv_to_one_checkout(tmp_path: Path) -> None:
    external = tmp_path / "envs" / "cc"
    external.mkdir(parents=True)
    first = _checkout(tmp_path, "first")
    second = _checkout(tmp_path, "second")

    assert _guard(first, external, "claim").returncode == 0

    assert _guard(first, external).returncode == 0, "the claimant owns it"
    assert _guard(second, external).returncode == 1, (
        "a second checkout must see it as another's, not as its own"
    )


# ---- A4: the no-pyproject fallback mutates too -----------------------------


def test_e2e_refuses_the_no_pyproject_fallback_on_a_shared_venv(tmp_path: Path) -> None:
    """The fallback runs `uv venv`, which rebuilds the target through a symlink.

    Authorizing only inside `if pyproject.exists()` left this branch free to
    destroy and recreate the owning checkout's environment.
    """
    from issue_orchestrator.infra.e2e_worktree import _sync_venv

    owner = _checkout(tmp_path, "owner")
    (owner / ".venv").mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)
    assert not (worktree / "pyproject.toml").exists()

    class _Refusing:
        def run(self, command, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                returncode=2,
                stdout="outcome=broken\nreason=dangling\n",
                stderr="",
                timed_out=False,
            )

    with pytest.raises(VenvMutationRefused):
        _sync_venv(worktree, _Refusing())
