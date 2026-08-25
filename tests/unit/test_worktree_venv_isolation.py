"""Worktrees must own their Python environment.

An editable install records one absolute source path, so an environment can
only point at a single checkout. Sharing one venv between checkouts of the same
project therefore cannot be made safe by configuration -- whichever checkout
syncs last owns the pointer, and the others silently import its source until it
is deleted and every import dangles.

These tests pin the two properties that keep that impossible: nothing plants a
shared venv, and a worktree that still carries one from before replaces it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("gmake") or shutil.which("make")


def test_worktree_setup_never_plants_a_shared_venv() -> None:
    """Regression guard: this is how the contamination was introduced.

    A helper symlinked the repo's venv into every worktree, and the configured
    setup command then synced through the link.
    """
    adapters = REPO_ROOT / "src" / "issue_orchestrator" / "adapters" / "worktree"
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{index}: {line.strip()}"
        for path in adapters.rglob("*.py")
        for index, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"symlink_to\s*\(", line) and "venv" in line.lower()
    ]

    assert not offenders, (
        "A worktree must own its venv; symlinking a shared one back in "
        "recreates the editable-pointer contamination:\n  " + "\n  ".join(offenders)
    )


def test_no_module_still_references_the_removed_helper() -> None:
    src = REPO_ROOT / "src"
    hits = [
        str(path.relative_to(REPO_ROOT))
        for path in src.rglob("*.py")
        if "_link_repo_venv_into_worktree" in path.read_text()
    ]

    assert not hits, f"the shared-venv helper is referenced again by {hits}"


@pytest.mark.skipif(MAKE is None, reason="make is unavailable")
def test_venv_fast_replaces_a_venv_symlinked_from_another_checkout(
    tmp_path: Path,
) -> None:
    """Worktrees created before the removal still carry the symlink.

    Reusing one would sync through it, so setup replaces it rather than
    requiring an operational cleanup somebody has to remember.
    """
    owner = tmp_path / "owner"
    (owner / ".venv").mkdir(parents=True)
    sentinel = owner / ".venv" / "SENTINEL"
    sentinel.write_text("owner")

    checkout = tmp_path / "worktree"
    checkout.mkdir()
    shutil.copy2(REPO_ROOT / "Makefile", checkout / "Makefile")
    (checkout / "pyproject.toml").write_text("[project]\nname='x'\n")
    (checkout / "uv.lock").write_text("# lock\n")
    (checkout / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "venv" ]; then mkdir -p .venv; fi\n'
        "exit 0\n"
    )
    fake_uv.chmod(0o755)

    assert MAKE is not None
    subprocess.run(
        [MAKE, "venv-fast", f"UV={fake_uv}", f"SETUP_LOG={tmp_path / 'log'}"],
        cwd=checkout,
        capture_output=True,
        text=True,
    )

    assert not (checkout / ".venv").is_symlink(), "the shared link survived setup"
    assert (checkout / ".venv").is_dir(), "no private venv was created"
    assert sentinel.exists(), "the owning checkout's venv was damaged"
