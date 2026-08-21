"""Guards against a worktree mutating a venv shared from another checkout.

The orchestrator links the base repo's venv into every worktree it creates
(``_link_repo_venv_into_worktree``). Anything that then runs ``uv sync`` or
``pip install -e .`` through that link reinstalls the *worktree's* project into
the *shared* venv, rewriting its editable pointer. Imports silently resolve to
another checkout's half-written source until that worktree is deleted, after
which every import dangles -- including in unrelated repositories whose
pre-push gate falls through to this interpreter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.infra.doctor.checks.workspace import check_python_environment
from issue_orchestrator.infra.repo_guardrails import _render_verify_pr_script

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "venv_guard.sh"


def _run_guard(cwd: Path) -> int:
    return subprocess.run(
        [str(GUARD), "--quiet"], cwd=cwd, capture_output=True, text=True
    ).returncode


def _checkout(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True)
    return path


def _owning_checkout(tmp_path: Path, name: str) -> Path:
    """A checkout that owns a venv.

    The pyproject.toml is what makes it a *checkout*: a venv under a directory
    that owns no project is a standalone environment, which no checkout can be
    contaminated through.
    """
    path = _checkout(tmp_path, name)
    (path / ".venv").mkdir()
    (path / "pyproject.toml").write_text("[project]\nname='owner'\n")
    return path


# --------------------------------------------------------------------- guard


def test_guard_allows_a_checkout_with_no_venv(tmp_path: Path) -> None:
    assert _run_guard(_checkout(tmp_path, "solo")) == 0


def test_guard_allows_a_private_venv_directory(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "solo")
    (checkout / ".venv").mkdir()
    assert _run_guard(checkout) == 0


def test_guard_allows_a_symlink_that_stays_inside_the_checkout(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "solo")
    real = checkout / "real-venv"
    real.mkdir()
    (checkout / ".venv").symlink_to(real, target_is_directory=True)
    assert _run_guard(checkout) == 0


def test_guard_refuses_a_venv_shared_from_another_checkout(tmp_path: Path) -> None:
    """The live bug: worktree .venv -> base .venv, then `uv sync` repoints base."""
    owner = _owning_checkout(tmp_path, "base")
    worktree = _checkout(tmp_path, "worktree")
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    assert _run_guard(worktree) == 1, "sharing is outcome 1, distinct from broken"


def test_guard_reports_a_dangling_symlink_distinctly_from_sharing(tmp_path: Path) -> None:
    """BROKEN(2) must not collapse into SHARED(1).

    Callers act differently on the two: a shared venv still gets a
    dependency-only sync, while a dangling one must fail. Treating "not zero"
    as "skip" is what let ``venv-fast`` report success over a broken venv.
    """
    worktree = _checkout(tmp_path, "worktree")
    (worktree / ".venv").symlink_to(tmp_path / "gone" / ".venv", target_is_directory=True)

    assert _run_guard(worktree) == 2


def test_guard_refuses_an_unclaimed_external_environment(tmp_path: Path) -> None:
    """An external venv is refused until ownership is explicitly bound.

    A parent that is not a checkout proves only that. Two checkouts can point
    CC_VENV_PATH at one environment, and telling both they own it recreates the
    original bug.
    """
    standalone = tmp_path / "envs" / "control-center"
    standalone.mkdir(parents=True)
    checkout = _checkout(tmp_path, "worktree")

    result = subprocess.run(
        [str(GUARD), "--quiet", "--venv", str(standalone)], cwd=checkout
    )

    assert result.returncode == 3


def test_claiming_an_external_environment_makes_it_owned(tmp_path: Path) -> None:
    standalone = tmp_path / "envs" / "control-center"
    standalone.mkdir(parents=True)
    checkout = _checkout(tmp_path, "worktree")

    claim = subprocess.run(
        [str(GUARD), "claim", "--quiet", "--venv", str(standalone)], cwd=checkout
    )
    assert claim.returncode == 0

    decide = subprocess.run(
        [str(GUARD), "--quiet", "--venv", str(standalone)], cwd=checkout
    )
    assert decide.returncode == 0


def test_guard_targets_an_explicit_venv_path(tmp_path: Path) -> None:
    """Control Center mutates ${VENV_PATH}; guarding ./.venv would guard nothing."""
    owner = _owning_checkout(tmp_path, "base")
    checkout = _checkout(tmp_path, "worktree")

    result = subprocess.run(
        [str(GUARD), "--quiet", "--venv", str(owner / ".venv")], cwd=checkout
    )

    assert result.returncode == 1


def test_guard_returns_the_decision_and_its_arguments_in_one_execution(
    tmp_path: Path,
) -> None:
    """Outcome and permitted arguments must not be fetchable separately.

    Two executions let a failed second call expand to nothing, turning a
    restricted dependency sync into an unrestricted project install.
    """
    owner = _owning_checkout(tmp_path, "base")
    worktree = _checkout(tmp_path, "worktree")
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    result = subprocess.run(
        [str(GUARD), "decide", "--quiet"], cwd=worktree, capture_output=True, text=True
    )

    assert result.returncode == 1
    record = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert record["outcome"] == "shared"
    # --no-install-project keeps a dependency sync from rewriting the pointer;
    # --inexact stops it removing packages another user still needs.
    assert "--no-install-project" in record["sync_args"]
    assert "--inexact" in record["sync_args"]


def test_guard_names_the_owning_checkout_and_the_repair(tmp_path: Path) -> None:
    owner = _owning_checkout(tmp_path, "base")
    worktree = _checkout(tmp_path, "worktree")
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    result = subprocess.run([str(GUARD)], cwd=worktree, capture_output=True, text=True)

    assert str(owner) in result.stderr
    assert "dependency-only" in result.stderr


# ------------------------------------------------------- verify-pr interpreter


def test_generated_verify_pr_validates_its_chosen_interpreter() -> None:
    """It picks an interpreter by fallthrough; it must prove the import works."""
    script = _render_verify_pr_script("make validate-pr-raw")

    assert 'import issue_orchestrator' in script
    assert "cannot import issue_orchestrator" in script
    # The diagnostic must name the pointer file, not just fail opaquely.
    assert "issue_orchestrator*.pth" in script
    assert "uv pip install" in script


def test_generated_verify_pr_probe_precedes_the_validation_run() -> None:
    script = _render_verify_pr_script("make validate-pr-raw")

    assert script.index("cannot import issue_orchestrator") < script.index(
        "running cache-aware pre-push validation"
    )


# ------------------------------------------------------------------- doctor


class _FakeRunner:
    """CommandRunner stub: the probe's exit status and stdout are the contract."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self._result = SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr, timed_out=False
        )
        self.commands: list[list[str]] = []

    def run(self, command, **kwargs):  # noqa: ANN001, ANN003 - port shape
        self.commands.append(list(command))
        return self._result


def _venv(repo: Path, pointer_target: Path | None) -> Path:
    site = repo / ".venv" / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    if pointer_target is not None:
        (site / "_editable_impl_issue_orchestrator.pth").write_text(str(pointer_target))
    return repo


def test_doctor_reports_ok_when_the_import_resolves_inside_the_repo(tmp_path: Path) -> None:
    repo = _venv(_checkout(tmp_path, "repo"), None)
    runner = _FakeRunner(0, stdout=str(repo / "src" / "issue_orchestrator"))

    check = check_python_environment(repo, runner)

    assert check.status == "ok"


def test_doctor_errors_when_the_import_resolves_outside_the_repo(tmp_path: Path) -> None:
    """The silent form: the venv works, but against another checkout's source."""
    repo = _venv(_checkout(tmp_path, "repo"), None)
    other = tmp_path / "other" / "src" / "issue_orchestrator"
    runner = _FakeRunner(0, stdout=str(other))

    check = check_python_environment(repo, runner)

    assert check.status == "error"
    assert str(other) in check.detail


def test_doctor_errors_when_the_interpreter_cannot_import_at_all(tmp_path: Path) -> None:
    repo = _venv(_checkout(tmp_path, "repo"), tmp_path / "deleted-worktree" / "src")
    runner = _FakeRunner(1, stderr="ModuleNotFoundError: No module named 'issue_orchestrator'")

    check = check_python_environment(repo, runner)

    assert check.status == "error"
    assert "MISSING" in check.detail
    assert check.expandable is not None and "repair" in check.expandable


def test_doctor_does_not_claim_health_from_an_empty_site_packages(tmp_path: Path) -> None:
    """A venv with no .pth at all must not be reported healthy unexamined.

    The pointer-only implementation fell through to ``ok`` here, asserting an
    import it had never attempted.
    """
    repo = _venv(_checkout(tmp_path, "repo"), None)
    runner = _FakeRunner(1, stderr="ModuleNotFoundError")

    assert check_python_environment(repo, runner).status == "error"


def test_doctor_accepts_a_valid_non_editable_install(tmp_path: Path) -> None:
    """No .pth is normal for a wheel install; only the import matters."""
    repo = _venv(_checkout(tmp_path, "repo"), None)
    runner = _FakeRunner(0, stdout=str(repo / ".venv" / "lib" / "issue_orchestrator"))

    assert check_python_environment(repo, runner).status == "ok"


def test_doctor_is_informational_without_a_venv(tmp_path: Path) -> None:
    assert check_python_environment(_checkout(tmp_path, "repo"), _FakeRunner(0)).status == "info"


def test_doctor_probes_the_venv_interpreter_not_the_ambient_one(tmp_path: Path) -> None:
    repo = _venv(_checkout(tmp_path, "repo"), None)
    runner = _FakeRunner(0, stdout=str(repo / "src"))

    check_python_environment(repo, runner)

    assert runner.commands[0][0] == str(repo / ".venv" / "bin" / "python")


def test_doctor_reports_a_dangling_venv_as_broken_not_absent(tmp_path: Path) -> None:
    """A dangling .venv and an absent one both fail exists(); only one is benign.

    Reporting "No .venv ... using the ambient interpreter" contradicted the
    guard's BROKEN state and let startup proceed past a broken environment.
    """
    repo = _checkout(tmp_path, "repo")
    (repo / ".venv").symlink_to(tmp_path / "deleted-checkout" / ".venv", target_is_directory=True)

    check = check_python_environment(repo, _FakeRunner(0))

    assert check.status == "error"
    assert "dangling" in check.detail


def test_doctor_turns_an_unreadable_pointer_into_a_check(tmp_path: Path) -> None:
    """An unreadable .pth is a diagnosis, not a crash.

    Raising here took the whole doctor run down instead of reporting the
    broken install it was asked to look for.
    """
    repo = _venv(_checkout(tmp_path, "repo"), tmp_path / "somewhere" / "src")
    pointer = next((repo / ".venv").glob("lib/*/site-packages/*.pth"))
    pointer.chmod(0o000)
    try:
        check = check_python_environment(repo, _FakeRunner(0, stdout=str(repo / "src")))
    finally:
        pointer.chmod(0o644)

    assert check.status == "error"
    assert "Could not read" in check.detail
