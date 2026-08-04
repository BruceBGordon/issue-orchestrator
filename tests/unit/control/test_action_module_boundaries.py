"""The action modules' dependency direction is one-way (#6957 review F7).

``actions`` imports ``tech_lead_actions`` and re-exports every name from it, so
``tech_lead_actions`` must NOT import ``actions`` back. It used to: the reverse
import only worked because ``Action``/``ActionType`` happened to be bound on the
partially initialized module before the forward import ran, which made a
harmless import reordering able to break module initialization.

``action_base`` is the dependency root. These tests pin that, statically (so
they fail on the import statement itself rather than on a lucky runtime order)
and dynamically (so a fresh, isolated import of the leaf module still works).
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src"
CONTROL = SRC / "issue_orchestrator" / "control"


def _sibling_imports(module: str) -> set[str]:
    """Sibling control modules *module* imports at module scope."""
    tree = ast.parse((CONTROL / f"{module}.py").read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_tech_lead_actions_depends_on_the_root_not_on_actions() -> None:
    imports = _sibling_imports("tech_lead_actions")

    assert "action_base" in imports
    assert "actions" not in imports, (
        "tech_lead_actions must import Action/ActionType from action_base;"
        " importing them from `actions` closes a cycle, because `actions`"
        " imports this module"
    )


def test_actions_is_the_re_export_and_action_base_is_the_root() -> None:
    assert {"action_base", "tech_lead_actions"} <= _sibling_imports("actions")
    assert not _sibling_imports("action_base") & {"actions", "tech_lead_actions"}


@pytest.mark.parametrize(
    "module",
    (
        "issue_orchestrator.control.action_base",
        "issue_orchestrator.control.tech_lead_actions",
        "issue_orchestrator.control.actions",
    ),
)
def test_each_action_module_imports_standalone(module: str) -> None:
    """Importing any of the three FIRST must succeed in a fresh interpreter."""
    env = dict(os.environ)
    # Pin this worktree's sources ahead of whatever the venv has installed, so
    # the guardrail tests the code under review rather than an older snapshot.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {module} as m; assert m.Action is not None",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr


def test_re_exported_action_roots_are_the_same_objects() -> None:
    from issue_orchestrator.control import action_base, actions, tech_lead_actions

    assert actions.Action is action_base.Action
    assert actions.ActionType is action_base.ActionType
    assert tech_lead_actions.Action is action_base.Action
    assert tech_lead_actions.ActionType is action_base.ActionType
