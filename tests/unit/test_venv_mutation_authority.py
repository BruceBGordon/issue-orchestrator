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
from types import SimpleNamespace

from issue_orchestrator.infra.venv_mutation import (
    GUARD_RESOURCE,
    VenvMutationAuthority,
    VenvMutationRefused,
    VenvOperation,
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

    decision = _authority().authorize(operation=VenvOperation.SYNC_DEPENDENCIES, checkout=foreign)

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

    decision = _authority().authorize(operation=VenvOperation.SYNC_DEPENDENCIES, checkout=foreign)

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
        authority.authorize(operation=VenvOperation.SYNC_DEPENDENCIES, checkout=_checkout(tmp_path, "repo"))


def test_authority_raises_its_domain_error_for_a_missing_guard(tmp_path: Path) -> None:
    authority = VenvMutationAuthority(
        LocalCommandRunner(), guard_path=tmp_path / "absent.sh"
    )

    with pytest.raises(VenvMutationRefused):
        authority.authorize(operation=VenvOperation.SYNC_DEPENDENCIES, checkout=_checkout(tmp_path, "repo"))


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
        authority.authorize(operation=VenvOperation.SYNC_DEPENDENCIES, checkout=_checkout(tmp_path, "repo"))


# ---- A2: the decision binds the target ------------------------------------


def test_pinned_env_binds_uv_to_the_authorized_environment(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "repo")
    (checkout / ".venv").mkdir()
    decision = _authority().authorize(operation=VenvOperation.SYNC_DEPENDENCIES, checkout=checkout)

    env = VenvMutationAuthority.pinned_env(decision)

    assert env["UV_PROJECT_ENVIRONMENT"] == str(decision.venv)


def test_pinned_env_drops_a_destructive_clear_override(tmp_path: Path) -> None:
    """UV_VENV_CLEAR makes `uv venv` delete and rebuild the target."""
    checkout = _checkout(tmp_path, "repo")
    (checkout / ".venv").mkdir()
    decision = _authority().authorize(operation=VenvOperation.SYNC_DEPENDENCIES, checkout=checkout)

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


# ---- a refusal must carry its own fix --------------------------------------
#
# Every caller invokes the guard with --quiet, so guidance written only to the
# guard's stderr is unreachable in practice. A refusal that reaches an operator
# or an agent as a bare "external and unclaimed" gives them nothing to act on,
# so the remedy is part of the decision record and every caller relays it.


def _decision_record(checkout: Path, venv: Path) -> dict[str, str]:
    result = subprocess.run(
        [str(GUARD_RESOURCE), "decide", "--quiet", "--checkout", str(checkout),
         "--venv", str(venv)],
        capture_output=True,
        text=True,
    )
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def test_an_unclaimed_external_venv_states_the_exact_command_to_run(
    tmp_path: Path,
) -> None:
    checkout = _checkout(tmp_path, "repo")
    (checkout / "scripts").mkdir()
    wrapper = checkout / "scripts" / "venv_guard.sh"
    wrapper.write_text("#!/usr/bin/env bash\n")
    wrapper.chmod(0o755)
    external = tmp_path / "envs" / "cc"
    external.mkdir(parents=True)

    remedy = _decision_record(checkout, external)["remedy"]

    assert "claim" in remedy
    assert str(external) in remedy
    # It must name a path the caller can actually run, not this file's
    # internal location.
    assert str(wrapper) in remedy
    # And the alternative, so claiming is a choice rather than the only exit.
    assert "make venv-fast" in remedy


def test_a_dangling_venv_states_how_to_rebuild(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "repo")
    (checkout / ".venv").symlink_to(tmp_path / "gone", target_is_directory=True)

    record = _decision_record(checkout, checkout / ".venv")

    assert record["outcome"] == "broken"
    assert "rm " in record["remedy"] and "make venv-fast" in record["remedy"]


def test_a_shared_venv_states_where_to_install_the_project(tmp_path: Path) -> None:
    owner = _checkout(tmp_path, "owner")
    (owner / ".venv").mkdir()
    worktree = _checkout(tmp_path, "wt")
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    record = _decision_record(worktree, worktree / ".venv")

    assert record["outcome"] == "shared"
    assert str(owner) in record["remedy"]


@pytest.mark.parametrize("outcome", ["broken", "unclaimed", "shared"])
def test_every_non_owned_outcome_carries_a_remedy(outcome: str, tmp_path: Path) -> None:
    """A refusal or restriction without a fix is a dead end."""
    checkout = _checkout(tmp_path, "repo")
    if outcome == "broken":
        target = checkout / ".venv"
        target.symlink_to(tmp_path / "gone", target_is_directory=True)
    elif outcome == "unclaimed":
        target = tmp_path / "envs" / "cc"
        target.mkdir(parents=True)
    else:
        owner = _checkout(tmp_path, "owner")
        (owner / ".venv").mkdir()
        target = checkout / ".venv"
        target.symlink_to(owner / ".venv", target_is_directory=True)

    record = _decision_record(checkout, target)

    assert record["outcome"] == outcome
    assert record.get("remedy", "").strip(), f"{outcome} gives no way forward"


def test_the_raised_error_carries_the_remedy(tmp_path: Path) -> None:
    """An agent sees the exception text, not the guard's stderr."""
    checkout = _checkout(tmp_path, "repo")
    external = tmp_path / "envs" / "cc"
    external.mkdir(parents=True)

    with pytest.raises(VenvMutationRefused) as raised:
        _authority().authorize(operation=VenvOperation.SYNC_DEPENDENCIES, checkout=checkout, venv=external)

    assert "To fix:" in str(raised.value)
    assert "claim" in str(raised.value)


# ================= follow-up review D1-D5 reproductions ====================


class _RealGuardRunner:
    """Executes the guard for real, so decisions under test are genuine.

    The weak version of the D1 test used a fake returning `broken`, so
    `authorize()` raised before the branch under test was ever reached. A
    reproduction has to drive the real `shared` decision.
    """

    def __init__(self) -> None:
        import subprocess as _sp

        self._run = _sp.run

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, **kwargs):
        completed = self._run(command, cwd=cwd, env=env, capture_output=True, text=True)
        return SimpleNamespace(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
        )


def test_d1_shared_venv_without_pyproject_recreates_nothing(tmp_path: Path) -> None:
    """`shared` permits dependency work, never recreation.

    Authorizing the target alone accepted `shared` and then let the
    no-pyproject branch run `uv venv`, rebuilding the owning checkout's
    environment through the symlink.
    """
    import subprocess as _sp
    from unittest.mock import patch

    from issue_orchestrator.infra.e2e_worktree import _sync_venv

    owner = _checkout(tmp_path, "owner")
    (owner / ".venv").mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)
    assert not (worktree / "pyproject.toml").exists()

    calls: list[tuple] = []
    # Build the runner BEFORE patching, so it holds the real subprocess.run and
    # the decision under test is genuine rather than mocked away.
    runner = _RealGuardRunner()
    with patch("issue_orchestrator.infra.e2e_worktree.subprocess.run") as mocked:
        mocked.side_effect = lambda *a, **k: (
            calls.append(tuple(a[0])),
            _sp.CompletedProcess(a[0], 0),
        )[1]
        with pytest.raises(VenvMutationRefused, match="recreate"):
            _sync_venv(worktree, runner)

    assert calls == [], f"a mutation ran against the owner's venv: {calls}"


def test_recreate_is_permitted_on_an_owned_venv(tmp_path: Path) -> None:
    """The restriction must not break the ordinary case."""
    checkout = _checkout(tmp_path, "repo")

    decision = _authority().authorize(
        checkout=checkout, operation=VenvOperation.RECREATE
    )

    assert decision.outcome is VenvOutcome.OWNED


def test_shared_still_permits_a_dependency_sync(tmp_path: Path) -> None:
    owner = _checkout(tmp_path, "owner")
    (owner / ".venv").mkdir()
    worktree = _checkout(tmp_path, "wt")
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    decision = _authority().authorize(
        checkout=worktree, operation=VenvOperation.SYNC_DEPENDENCIES
    )

    assert decision.outcome is VenvOutcome.SHARED
    assert "--no-install-project" in decision.sync_args


def test_shared_forbids_installing_this_project(tmp_path: Path) -> None:
    owner = _checkout(tmp_path, "owner")
    (owner / ".venv").mkdir()
    worktree = _checkout(tmp_path, "wt")
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    with pytest.raises(VenvMutationRefused, match="install-project"):
        _authority().authorize(
            checkout=worktree, operation=VenvOperation.INSTALL_PROJECT
        )


# ---- D3: a claim must not be stealable ------------------------------------


def test_d3_a_second_checkout_cannot_steal_a_claim(tmp_path: Path) -> None:
    """Both checkouts reporting ownership defeats the exclusivity the marker proves."""
    external = tmp_path / "envs" / "shared"
    external.mkdir(parents=True)
    first = _checkout(tmp_path, "first")
    second = _checkout(tmp_path, "second")

    assert _guard(first, external, "claim").returncode == 0
    assert _guard(second, external, "claim").returncode == 1, "B stole A's claim"

    assert _guard(first, external).returncode == 0
    assert _guard(second, external).returncode == 1


def test_reclaiming_your_own_environment_is_idempotent(tmp_path: Path) -> None:
    external = tmp_path / "envs" / "mine"
    external.mkdir(parents=True)
    checkout = _checkout(tmp_path, "repo")

    assert _guard(checkout, external, "claim").returncode == 0
    assert _guard(checkout, external, "claim").returncode == 0


def test_a_refused_claim_names_the_holder(tmp_path: Path) -> None:
    external = tmp_path / "envs" / "shared"
    external.mkdir(parents=True)
    first = _checkout(tmp_path, "first")
    second = _checkout(tmp_path, "second")
    _guard(first, external, "claim")

    result = _guard(second, external, "claim")

    assert str(first) in result.stdout


# ---- D4: contradictory or partial records are not authorization ------------


@pytest.mark.parametrize(
    "body,reason",
    [
        # outcome says owned, status says otherwise
        ('echo "outcome=owned"; echo "sync_args=--frozen"; echo "operation=sync-dependencies";'
         ' echo "allowed=yes"; echo "venv=$5"; exit 7\n', "contradict"),
        # a shared outcome delivered with an owned exit code
        ('echo "outcome=shared"; echo "sync_args=--frozen --no-install-project";'
         ' echo "operation=sync-dependencies"; echo "allowed=yes"; echo "venv=$5"; exit 0\n',
         "contradict"),
        # answers about a different environment
        ('echo "outcome=owned"; echo "sync_args=--frozen"; echo "operation=sync-dependencies";'
         ' echo "allowed=yes"; echo "venv=/somewhere/else"; exit 0\n', "different environment"),
        # answers about a different operation
        ('echo "outcome=owned"; echo "sync_args=--frozen"; echo "operation=recreate";'
         ' echo "allowed=yes"; echo "venv=$5"; exit 0\n', "not the requested"),
        # required fields missing
        ('echo "outcome=owned"; exit 0\n', "incomplete"),
    ],
)
def test_d4_contradictory_records_are_refused(
    body: str, reason: str, tmp_path: Path
) -> None:
    guard = tmp_path / "guard.sh"
    guard.write_text("#!/usr/bin/env bash\n" + body)
    guard.chmod(0o755)
    authority = VenvMutationAuthority(LocalCommandRunner(), guard_path=guard)

    with pytest.raises(VenvMutationRefused, match=reason):
        authority.authorize(
            checkout=_checkout(tmp_path, "repo"),
            operation=VenvOperation.SYNC_DEPENDENCIES,
        )


def test_d4_a_timed_out_decision_is_refused(tmp_path: Path) -> None:
    class _TimedOut:
        def run(self, command, **kwargs):
            return SimpleNamespace(
                returncode=0, stdout="outcome=owned\n", stderr="", timed_out=True
            )

    authority = VenvMutationAuthority(_TimedOut())

    with pytest.raises(VenvMutationRefused, match="timed out"):
        authority.authorize(
            checkout=_checkout(tmp_path, "repo"),
            operation=VenvOperation.SYNC_DEPENDENCIES,
        )


# ---- F3: a relative target would weaken the pin ---------------------------


def test_a_relative_target_is_canonicalised_before_pinning(tmp_path: Path) -> None:
    """UV_PROJECT_ENVIRONMENT is resolved against uv's cwd, not the caller's."""
    checkout = _checkout(tmp_path, "repo")
    (checkout / ".venv").mkdir()

    decision = _authority().authorize(
        checkout=checkout,
        operation=VenvOperation.SYNC_DEPENDENCIES,
        venv=Path(".venv"),
    )

    assert decision.venv.is_absolute()
    assert VenvMutationAuthority.pinned_env(decision)["UV_PROJECT_ENVIRONMENT"].startswith("/")


# ---- the shared validator is the only validator ---------------------------


def _check(record: str, *, venv: str, operation: str, exit_code: int):
    return subprocess.run(
        [str(GUARD_RESOURCE), "check", "--quiet", "--venv", venv,
         "--operation", operation, "--exit", str(exit_code)],
        input=record, capture_output=True, text=True,
    )


def _record(**fields) -> str:
    base = {
        "outcome": "owned", "sync_args": "--frozen --all-extras",
        "venv": "/x", "operation": "sync-dependencies", "allowed": "yes",
    }
    base.update(fields)
    return "".join(f"{k}={v}\n" for k, v in base.items())


def test_check_refuses_a_field_stated_twice() -> None:
    """"First occurrence wins" would let a record contradict itself."""
    record = _record() + "outcome=shared\n"

    assert _check(record, venv="/x", operation="sync-dependencies", exit_code=0).returncode != 0


def test_check_refuses_owned_but_not_permitted() -> None:
    """Echoing the outcome code made owned+allowed=no exit 0."""
    record = _record(allowed="no")

    assert _check(record, venv="/x", operation="sync-dependencies", exit_code=0).returncode != 0


@pytest.mark.parametrize(
    "fields,exit_code",
    [
        ({"venv": "/elsewhere"}, 0),
        ({"operation": "recreate"}, 0),
        ({"outcome": "shared"}, 0),
        ({}, 7),
    ],
)
def test_check_refuses_records_that_disagree_with_the_request(fields, exit_code) -> None:
    assert _check(_record(**fields), venv="/x", operation="sync-dependencies",
                  exit_code=exit_code).returncode != 0


def test_check_echoes_the_validated_arguments() -> None:
    """Callers must take the arguments from the validated record, not re-derive."""
    result = _check(_record(), venv="/x", operation="sync-dependencies", exit_code=0)

    assert result.returncode == 0
    assert result.stdout.strip() == "--frozen --all-extras"
