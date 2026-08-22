"""Guardrails for run-scoped trace event construction."""

from __future__ import annotations

import ast
from pathlib import Path

from issue_orchestrator.ports.event_sink import run_scoped_event_names

RUN_SCOPED_EVENT_ENUM_NAMES = {event.name for event in run_scoped_event_names()}
RUN_SCOPED_EVENT_STRING_NAMES = {event.value for event in run_scoped_event_names()}


def _is_run_scoped_event_expr(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "EventName"
            and node.attr in RUN_SCOPED_EVENT_ENUM_NAMES
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in RUN_SCOPED_EVENT_STRING_NAMES
    return False


def test_production_code_uses_make_run_scoped_event_for_run_scoped_events() -> None:
    """Run-scoped events must be created by the typed helper, not raw TraceEvent."""
    repo_root = Path(__file__).resolve().parents[2]
    src_root = repo_root / "src" / "issue_orchestrator"
    violations: list[str] = []

    for py_file in sorted(src_root.rglob("*.py")):
        module = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "TraceEvent":
                continue
            if not node.args:
                continue
            if not _is_run_scoped_event_expr(node.args[0]):
                continue
            rel = py_file.relative_to(repo_root)
            violations.append(f"{rel}:{node.lineno}")

    assert not violations, (
        "Run-scoped events must use make_run_scoped_event(...). Violations:\n"
        + "\n".join(violations)
    )
