"""The non-Make callers of the mutation owner must also fail closed.

Release prep, Control Center startup, and the E2E worktree manager each install
this project. Existing suites for them build fake repos *without*
``scripts/venv_guard.sh``, so a caller that treats a missing guard as ownership
passes every one of those tests while being wide open.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CC_SCRIPT = REPO_ROOT / "scripts" / "start_control_center.sh"
GUARD = REPO_ROOT / "scripts" / "venv_guard.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is unavailable")


def _guard_stub(path: Path, exit_code: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n")
    path.chmod(0o755)


def _run_cc_sync(root: Path, venv_path: Path) -> subprocess.CompletedProcess:
    """Source the real script and call sync_deps with mutation stubbed out."""
    harness = f"""
set -uo pipefail
ROOT_DIR={root}
VENV_PATH={venv_path}
source_guard_only() {{ :; }}
# Stub everything sync_deps would mutate with, so a refusal is observable as
# "install never ran" rather than as a failed install.
uv_bin_path() {{ echo /bin/true; }}
install_mode() {{ echo pip-editable-dev; }}
ensure_pip() {{ :; }}
verify_project_install() {{ echo "INSTALL-VERIFIED"; }}
record_deps_synced() {{ :; }}
{_sync_deps_source()}
sync_deps
"""
    return subprocess.run(
        [str(BASH), "-c", harness], capture_output=True, text=True, cwd=root
    )


def _sync_deps_source() -> str:
    """Extract venv_mutation_outcome + sync_deps from the real script."""
    text = CC_SCRIPT.read_text()
    start = text.index("venv_mutation_outcome() {")
    end = text.index("installed_project_path() {")
    body = text[start:end]
    # The real install line would run pip; neutralise just that command.
    return body.replace('"${VENV_PATH}/bin/python" -m pip install -e ".[dev]"', 'echo "INSTALL-RAN"')


@pytest.mark.parametrize(
    "guard_exit,expect_refusal",
    [(0, False), (1, True), (2, True), (7, True)],
)
def test_control_center_sync_respects_every_guard_outcome(
    guard_exit: int, expect_refusal: bool, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    _guard_stub(root / "scripts" / "venv_guard.sh", guard_exit)

    result = _run_cc_sync(root, root / ".venv")

    if expect_refusal:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "INSTALL-RAN" not in result.stdout
    else:
        assert "INSTALL-RAN" in result.stdout, result.stdout + result.stderr


def test_control_center_fails_closed_when_the_guard_is_missing(tmp_path: Path) -> None:
    """The existing CC harness builds repos with no guard at all."""
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)

    result = _run_cc_sync(root, root / ".venv")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "INSTALL-RAN" not in result.stdout


def test_control_center_guards_the_venv_it_actually_mutates(tmp_path: Path) -> None:
    """CC_VENV_PATH can differ from ROOT_DIR/.venv.

    Guarding ./.venv while installing into another path guards nothing, so the
    real guard must be consulted about VENV_PATH.
    """
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(GUARD, root / "scripts" / "venv_guard.sh")
    (root / "scripts" / "venv_guard.sh").chmod(0o755)

    owner = tmp_path / "owner"
    (owner / ".venv").mkdir(parents=True)
    (owner / "pyproject.toml").write_text("[project]\nname='owner'\n")

    result = _run_cc_sync(root, owner / ".venv")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "INSTALL-RAN" not in result.stdout


# ------------------------------------------------------------- release prep


def _release_guard_outcome(tmp_path: Path, guard_exit: int | None):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "prepare_release_under_test", REPO_ROOT / "scripts" / "prepare_release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Dataclass construction resolves its own module via sys.modules; without
    # this registration the import fails with an opaque AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    root = tmp_path / "repo"
    root.mkdir(parents=True)
    if guard_exit is not None:
        _guard_stub(root / "scripts" / "venv_guard.sh", guard_exit)
    return module, root


@pytest.mark.parametrize("guard_exit", [1, 2, 7, None])
def test_release_refuses_every_non_owned_outcome(guard_exit, tmp_path: Path) -> None:
    module, root = _release_guard_outcome(tmp_path, guard_exit)

    with pytest.raises(module.ReleasePrepError):
        module.require_owned_venv(root)


def test_release_proceeds_when_the_venv_is_owned(tmp_path: Path) -> None:
    module, root = _release_guard_outcome(tmp_path, 0)

    module.require_owned_venv(root)  # must not raise


# ------------------------------------------------------------ e2e worktree


def test_e2e_worktree_refuses_to_sync_a_shared_venv(tmp_path: Path) -> None:
    """E2E worktrees are orchestrator worktrees, so .venv is symlinked at base."""
    from issue_orchestrator.infra.e2e_worktree import (
        VenvMutationRefused,
        _require_owned_venv,
    )

    worktree = tmp_path / "wt"
    _guard_stub(worktree / "scripts" / "venv_guard.sh", 1)

    with pytest.raises(VenvMutationRefused, match="shared"):
        _require_owned_venv(worktree)


def test_e2e_worktree_fails_closed_without_a_guard(tmp_path: Path) -> None:
    from issue_orchestrator.infra.e2e_worktree import (
        VenvMutationRefused,
        _require_owned_venv,
    )

    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(VenvMutationRefused):
        _require_owned_venv(worktree)


def test_e2e_worktree_proceeds_when_owned(tmp_path: Path) -> None:
    from issue_orchestrator.infra.e2e_worktree import _require_owned_venv

    worktree = tmp_path / "wt"
    _guard_stub(worktree / "scripts" / "venv_guard.sh", 0)

    _require_owned_venv(worktree)  # must not raise
