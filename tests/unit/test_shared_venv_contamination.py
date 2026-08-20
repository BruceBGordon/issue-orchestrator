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

    assert _run_guard(worktree) == 1


def test_guard_refuses_a_dangling_venv_symlink(tmp_path: Path) -> None:
    """The owning checkout was deleted; syncing would write into a void."""
    worktree = _checkout(tmp_path, "worktree")
    (worktree / ".venv").symlink_to(tmp_path / "gone" / ".venv", target_is_directory=True)

    assert _run_guard(worktree) == 1


def test_guard_names_the_owning_checkout_and_the_repair(tmp_path: Path) -> None:
    owner = _checkout(tmp_path, "base")
    (owner / ".venv").mkdir()
    worktree = _checkout(tmp_path, "worktree")
    (worktree / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    result = subprocess.run([str(GUARD)], cwd=worktree, capture_output=True, text=True)

    assert str(owner) in result.stderr
    assert "make -C" in result.stderr


@pytest.mark.parametrize(
    "target",
    ["venv", "venv-fast", "venv-pip", "install", "upgrade-deps", "sync-deps"],
)
def test_every_venv_mutating_make_target_consults_the_guard(target: str) -> None:
    """Fix the class: no mutation site may sync without asking the guard first.

    ``sync-deps`` matters most -- it is a prerequisite of ``test-unit``, so an
    ordinary test run inside a worktree would otherwise repoint the shared venv.
    """
    makefile = (REPO_ROOT / "Makefile").read_text()
    start = makefile.index(f"\n{target}:")
    body = makefile[start : start + 2000]
    recipe = body[: body.index("\n\n")] if "\n\n" in body else body

    assert "scripts/venv_guard.sh" in recipe, f"{target} mutates the venv unguarded"


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


def _venv(repo: Path, target: Path | None) -> Path:
    site = repo / ".venv" / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    if target is not None:
        (site / "_editable_impl_issue_orchestrator.pth").write_text(str(target))
    return repo


def test_doctor_reports_ok_when_the_venv_points_at_its_own_repo(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, "repo")
    (repo / "src").mkdir()
    _venv(repo, repo / "src")

    assert check_python_environment(repo).status == "ok"


def test_doctor_errors_when_the_venv_points_at_another_checkout(tmp_path: Path) -> None:
    other = tmp_path / "other" / "src"
    other.mkdir(parents=True)
    repo = _venv(_checkout(tmp_path, "repo"), other)

    check = check_python_environment(repo)

    assert check.status == "error"
    assert str(other) in check.detail


def test_doctor_errors_when_the_editable_target_was_deleted(tmp_path: Path) -> None:
    """What actually broke the host: the pointed-at worktree is gone."""
    repo = _venv(_checkout(tmp_path, "repo"), tmp_path / "deleted-worktree" / "src")

    check = check_python_environment(repo)

    assert check.status == "error"
    assert "MISSING" in check.detail
    assert check.expandable is not None and "repair" in check.expandable


def test_doctor_is_informational_without_a_venv(tmp_path: Path) -> None:
    assert check_python_environment(_checkout(tmp_path, "repo")).status == "info"
