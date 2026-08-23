"""Doctor coverage for a broken editable install.

An editable install records one absolute source path. When it points at another
checkout, or at a directory that no longer exists, imports here resolve to
someone else's source or fail with a bare ModuleNotFoundError. Doctor is where
that becomes visible without a long investigation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.infra.doctor.checks.workspace import check_python_environment


def _checkout(tmp_path: Path, name: str) -> Path:
    """A source checkout of THIS project, which is what the check is scoped to."""
    path = tmp_path / name
    (path / "src" / "issue_orchestrator").mkdir(parents=True)
    (path / "src" / "issue_orchestrator" / "__init__.py").write_text("")
    return path


def _foreign_repo(tmp_path: Path, name: str) -> Path:
    """Somebody else's Python project: no issue_orchestrator source in it."""
    path = tmp_path / name
    path.mkdir(parents=True)
    return path


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


def test_doctor_flags_a_venv_directory_with_no_interpreter(tmp_path: Path) -> None:
    """A present-but-unusable .venv is not an absent one.

    Reporting "No .venv ... using the ambient interpreter" was factually wrong
    and hid an incomplete or corrupt environment.
    """
    repo = _checkout(tmp_path, "repo")
    (repo / ".venv").mkdir()

    check = check_python_environment(repo, _FakeRunner(0))

    assert check.status == "error"
    assert "no interpreter" in check.detail


def test_doctor_is_informational_only_when_the_venv_is_truly_absent(
    tmp_path: Path,
) -> None:
    repo = _checkout(tmp_path, "repo")

    assert check_python_environment(repo, _FakeRunner(0)).status == "info"


# ---- B1: the target repository is usually not this project -----------------


def test_a_foreign_python_repo_with_its_own_venv_is_not_an_error(tmp_path: Path) -> None:
    """The startup preflight must not block a normal Python target repo.

    Requiring the target's venv to import issue_orchestrator only makes sense
    when the target IS this project; for anyone else it reports a healthy
    environment as broken and suggests installing their project as this one.
    """
    repo = _foreign_repo(tmp_path, "someones-django-app")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")

    check = check_python_environment(repo, _FakeRunner(1, stderr="ModuleNotFoundError"))

    assert check.status == "info"
    assert "not an issue-orchestrator source checkout" in check.detail


def test_an_orchestrator_source_checkout_is_still_diagnosed(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, "issue-orchestrator")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")

    check = check_python_environment(repo, _FakeRunner(1, stderr="ModuleNotFoundError"))

    assert check.status == "error"
