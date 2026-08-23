"""Every worktree-producing launch path provisions its environment.

Setup commands used to run from individual launchers, so only coding sessions
and validation retries got them: review, rework, retrospective-review and
tech-lead scratch worktrees launched without their environment. That was
invisible while the adapter symlinked the repo's venv into every worktree —
removing the symlink made it a real gap.

Acquisition is now the owner, which makes the property hold by construction
rather than by each launcher remembering. These tests pin both halves: the
owner runs setup, and no launch path can obtain a worktree without going
through it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL = REPO_ROOT / "src" / "issue_orchestrator" / "control"


def test_worktree_acquisition_has_exactly_one_call_site() -> None:
    """The property holds only while acquisition is a single choke point.

    If a launcher starts calling the port directly, it bypasses environment
    provisioning and this class of bug returns.
    """
    callers = [
        f"{path.relative_to(REPO_ROOT)}:{index}"
        for path in CONTROL.rglob("*.py")
        for index, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"worktree_manager\s*\.\s*create\s*\(", line)
    ]

    assert len(callers) == 1, (
        "Worktree acquisition must stay a single choke point so environment "
        f"setup cannot be bypassed; found {callers}"
    )
    assert "worktree_context.py" in callers[0], (
        f"acquisition moved out of the owner module: {callers[0]}"
    )


def test_acquisition_provisions_the_environment() -> None:
    """The single acquisition path must actually run setup."""
    source = (CONTROL / "worktree_context.py").read_text()
    tree = ast.parse(source)

    create = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "create"
    )
    body = ast.unparse(create)

    assert "prepare_worktree_environment" in body, (
        "acquisition must provision the worktree environment"
    )


def test_setup_has_a_single_implementation() -> None:
    """A second implementation is how the two paths drifted apart before."""
    implementations = [
        f"{path.relative_to(REPO_ROOT)}"
        for path in CONTROL.rglob("*.py")
        if "config.setup_worktree" in path.read_text()
        and path.name != "worktree_context.py"
    ]

    assert not implementations, (
        "setup command execution belongs to prepare_worktree_environment; "
        f"a second reader of config.setup_worktree exists in {implementations}"
    )


def test_every_launcher_reaches_worktrees_through_the_owner() -> None:
    """Enumerate the launch paths and prove each uses the acquisition owner."""
    launchers = {
        "launch_issue_session": CONTROL / "session_launcher.py",
        "launch_review_session": CONTROL / "session_launcher.py",
        "launch_retrospective_review_session": CONTROL / "session_launcher.py",
        "launch_validation_retry_session": CONTROL / "session_launcher.py",
        "launch_rework_session": CONTROL / "session_rework_launcher.py",
    }

    missing = []
    for name, path in launchers.items():
        tree = ast.parse(path.read_text())
        fn = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ),
            None,
        )
        if fn is None:
            missing.append(f"{name} (not found)")
            continue
        if "WorktreeContext.create" not in ast.unparse(fn):
            missing.append(name)

    assert not missing, f"these launch paths bypass the acquisition owner: {missing}"
