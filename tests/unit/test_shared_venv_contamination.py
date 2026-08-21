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
    owner = _checkout(tmp_path, "base")
    (owner / ".venv").mkdir()
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


def test_guard_publishes_the_dependency_only_sync_arguments() -> None:
    """Callers must not re-derive the safe argument set; the owner publishes it."""
    result = subprocess.run(
        [str(GUARD), "--explain", "sync"], capture_output=True, text=True
    )

    assert result.returncode == 0
    # --no-install-project is load-bearing: it is what keeps a dependency sync
    # from rewriting the editable pointer.
    assert "--no-install-project" in result.stdout
    # --inexact stops one checkout's sync removing packages another still needs.
    assert "--inexact" in result.stdout


def test_guard_names_the_owning_checkout_and_the_repair(tmp_path: Path) -> None:
    owner = _checkout(tmp_path, "base")
    (owner / ".venv").mkdir()
    worktree = _checkout(tmp_path, "worktree")
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    result = subprocess.run([str(GUARD)], cwd=worktree, capture_output=True, text=True)

    assert str(owner) in result.stderr
    assert "make -C" in result.stderr


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
