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
    owner_venv = owner_root / ".venv"
    owner_venv.mkdir(parents=True, exist_ok=True)
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
