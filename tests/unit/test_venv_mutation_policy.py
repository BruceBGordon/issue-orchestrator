"""Behavioral coverage for the venv mutation policy, driven through real make.

These tests run the repository's actual Makefile against a fake ``uv`` that
records its argv. Source-text assertions cannot show what a recipe *did*, and
two of the defects these cover (a knowingly-stale environment reported as
success, and a dangling venv reported as success) were invisible to text
matching.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("gmake") or shutil.which("make")

pytestmark = pytest.mark.skipif(MAKE is None, reason="make is unavailable")


@dataclass(frozen=True)
class MakeRun:
    returncode: int
    stdout: str
    uv_calls: tuple[str, ...]

    @property
    def output(self) -> str:
        return self.stdout

    def synced_project(self) -> bool:
        """Did any uv sync install this checkout's project into the venv?"""
        return any(
            call.startswith("sync") and "--no-install-project" not in call
            for call in self.uv_calls
        )

    def synced_dependencies_only(self) -> bool:
        return any(
            call.startswith("sync") and "--no-install-project" in call
            for call in self.uv_calls
        )


def _make_checkout(tmp_path: Path, name: str = "checkout") -> Path:
    checkout = tmp_path / name
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "Makefile", checkout / "Makefile")
    shutil.copy2(REPO_ROOT / "scripts" / "venv_guard.sh", checkout / "scripts" / "venv_guard.sh")
    (checkout / "scripts" / "venv_guard.sh").chmod(0o755)
    (checkout / "pyproject.toml").write_text("[project]\nname='x'\n")
    (checkout / "uv.lock").write_text("# lock\n")
    return checkout


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "uv-calls.log"
    fake = tmp_path / "fake-uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        # `uv venv .venv` must behave like the real thing: it fails when .venv
        # already exists as a symlink, which is the condition B2 mishandled.
        'if [ "${1:-}" = "venv" ] && [ -L .venv ]; then exit 1; fi\n'
        'if [ "${1:-}" = "venv" ]; then mkdir -p .venv; fi\n'
        # Sub-environments (the semgrep tool venv) are steered by
        # UV_PROJECT_ENVIRONMENT; create them so unrelated recipe steps succeed.
        'if [ "${1:-}" = "sync" ] && [ -n "${UV_PROJECT_ENVIRONMENT:-}" ]; then\n'
        '  mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
        '  : > "$UV_PROJECT_ENVIRONMENT/bin/semgrep"\n'
        '  chmod +x "$UV_PROJECT_ENVIRONMENT/bin/semgrep"\n'
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake, log


def _run_make(checkout: Path, target: str, tmp_path: Path) -> MakeRun:
    fake_uv, log = _fake_uv(tmp_path)
    assert MAKE is not None
    proc = subprocess.run(
        [
            MAKE,
            target,
            f"UV={fake_uv}",
            f"SETUP_LOG={tmp_path / 'setup.log'}",
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    calls = tuple(log.read_text().splitlines()) if log.exists() else ()
    return MakeRun(proc.returncode, proc.stdout + proc.stderr, calls)


def _share_venv_from(owner_root: Path, checkout: Path) -> None:
    """Link this checkout's .venv at another *checkout's* venv.

    The owner is given a pyproject.toml because that is what makes it a
    checkout. A venv under a directory that owns no project is a standalone
    environment nobody can be contaminated through.
    """
    owner_venv = owner_root / ".venv"
    owner_venv.mkdir(parents=True, exist_ok=True)
    (owner_root / "pyproject.toml").write_text("[project]\nname='owner'\n")
    (checkout / ".venv").symlink_to(owner_venv, target_is_directory=True)


# --------------------------------------------------------------- B1: staleness


def test_sync_deps_on_shared_venv_syncs_dependencies_rather_than_skipping(
    tmp_path: Path,
) -> None:
    """sync-deps runs *because* deps are stale; it must not report success idle.

    Skipping left every downstream test target running against an environment
    already known to be out of date, which can produce a false green.
    """
    checkout = _make_checkout(tmp_path)
    _share_venv_from(tmp_path / "owner", checkout)

    run = _run_make(checkout, "sync-deps", tmp_path)

    assert run.returncode == 0, run.output
    assert run.synced_dependencies_only(), run.uv_calls
    assert not run.synced_project(), (
        "a shared venv must never receive this checkout's project install"
    )


def test_sync_deps_on_owned_venv_still_installs_the_project(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").mkdir()

    run = _run_make(checkout, "sync-deps", tmp_path)

    assert run.returncode == 0, run.output
    assert run.synced_project(), run.uv_calls


def test_sync_deps_fails_on_a_dangling_venv(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").symlink_to(tmp_path / "gone", target_is_directory=True)

    run = _run_make(checkout, "sync-deps", tmp_path)

    assert run.returncode != 0, run.output
    assert not run.uv_calls, "nothing may be synced into a broken environment"


# ------------------------------------------------------------- B2: venv-fast


def test_venv_fast_fails_on_a_dangling_venv(tmp_path: Path) -> None:
    """A dangling symlink is not an absent venv.

    ``[ ! -d .venv ]`` treated it as absent, ran ``uv venv`` (which fails), had
    that status overwritten by the next assignment, and still printed Done and
    exited 0.
    """
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").symlink_to(tmp_path / "gone", target_is_directory=True)

    run = _run_make(checkout, "venv-fast", tmp_path)

    assert run.returncode != 0, f"venv-fast reported success on a dangling venv:\n{run.output}"
    assert "Done!" not in run.output


def test_venv_fast_on_shared_venv_succeeds_without_installing_the_project(
    tmp_path: Path,
) -> None:
    checkout = _make_checkout(tmp_path)
    _share_venv_from(tmp_path / "owner", checkout)

    run = _run_make(checkout, "venv-fast", tmp_path)

    assert run.returncode == 0, run.output
    assert not run.synced_project(), run.uv_calls
    assert run.synced_dependencies_only(), run.uv_calls


def test_venv_fast_on_a_fresh_checkout_creates_and_fully_syncs(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)

    run = _run_make(checkout, "venv-fast", tmp_path)

    assert run.returncode == 0, run.output
    assert any(call.startswith("venv") for call in run.uv_calls), run.uv_calls
    assert run.synced_project(), run.uv_calls


# ------------------------------------------------- destructive targets refuse


@pytest.mark.parametrize("target", ["venv", "venv-pip"])
def test_destructive_targets_refuse_a_shared_venv(target: str, tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _share_venv_from(tmp_path / "owner", checkout)

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert (tmp_path / "owner" / ".venv").exists(), "the shared venv was destroyed"


# ------------------------------------------------- R1: shared freshness state


def test_one_checkout_sync_does_not_make_another_look_fresh(tmp_path: Path) -> None:
    """DEPS_MARKER lives in the venv, so on a shared venv it is another
    checkout's state. Reading it let B skip its sync because A had just
    stamped it -- with a different lock and freshly changed versions.
    """
    owner = tmp_path / "owner"
    (owner / ".venv").mkdir(parents=True)
    a = _make_checkout(tmp_path, "a")
    b = _make_checkout(tmp_path, "b")
    _share_venv_from(owner, a)
    _share_venv_from(owner, b)

    first = _run_make(a, "sync-deps", tmp_path / "a-run")
    assert first.returncode == 0, first.output
    assert first.synced_dependencies_only(), first.uv_calls

    # B's lock predates the marker A just touched. It must still sync.
    second = _run_make(b, "sync-deps", tmp_path / "b-run")

    assert second.returncode == 0, second.output
    assert second.uv_calls, "B skipped its sync because A stamped the shared marker"
    assert second.synced_dependencies_only(), second.uv_calls


def test_shared_sync_does_not_stamp_the_owners_marker(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    (owner / ".venv").mkdir(parents=True)
    checkout = _make_checkout(tmp_path, "a")
    _share_venv_from(owner, checkout)

    _run_make(checkout, "sync-deps", tmp_path / "run")

    assert not (owner / ".venv" / ".deps-synced").exists(), (
        "a non-owning checkout stamped the shared freshness marker"
    )


# --------------------------------------------------------- R2: fail closed


@pytest.mark.parametrize("target", ["sync-deps", "venv-fast", "install"])
def test_targets_fail_closed_when_the_guard_is_missing(target: str, tmp_path: Path) -> None:
    """A guard that cannot run is not evidence of ownership.

    Routing "any other exit code" to the else branch turned a missing guard
    into a full `uv sync --frozen --all-extras`.
    """
    checkout = _make_checkout(tmp_path)
    (checkout / "scripts" / "venv_guard.sh").unlink()

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert not run.synced_project(), run.uv_calls


@pytest.mark.parametrize("target", ["sync-deps", "venv-fast", "install"])
def test_targets_fail_closed_on_an_unexpected_guard_outcome(
    target: str, tmp_path: Path
) -> None:
    checkout = _make_checkout(tmp_path)
    (checkout / "scripts" / "venv_guard.sh").write_text("#!/usr/bin/env bash\nexit 7\n")
    (checkout / "scripts" / "venv_guard.sh").chmod(0o755)

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert not run.synced_project(), run.uv_calls
