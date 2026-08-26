"""Structural guardrail for executor-test completion watchdog ownership."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTOR_COMPLETION_CLIENTS = (
    REPO_ROOT / "tests/unit/test_test_executor_pool.py",
    REPO_ROOT / "tests/unit/executor_pool_dsl.py",
    REPO_ROOT / "tests/unit/test_executor_history_retention.py",
    REPO_ROOT / "tests/unit/test_setpgrp_pty_invariants.py",
    REPO_ROOT / "tests/unit/test_agent_phase_execution.py",
    REPO_ROOT / "tests/unit/executor_pressure_dsl.py",
    REPO_ROOT / "tests/unit/test_validate_runner.py",
    REPO_ROOT / "tests/unit/test_contained_command_capture.py",
    REPO_ROOT / "tests/unit/test_process_completion_fixture.py",
    REPO_ROOT / "tests/unit/test_process_group_terminator.py",
    REPO_ROOT / "tests/unit/test_validation_resource_sampling.py",
    REPO_ROOT / "tests/unit/test_contained_validation_command.py",
)


class CompletionGuardrailViolationKind(Enum):
    """Closed set of process-completion ownership violations."""

    SUBPROCESS_RUN = "subprocess.run"
    COMMUNICATE = "communicate"
    RESULT = "result"
    WAIT = "wait"
    JOIN = "join"
    WATCHDOG_OWNER_REBINDING = "watchdog owner rebinding"


@dataclass(frozen=True, slots=True)
class CompletionGuardrailViolation:
    """One process-completion call or binding that violates ownership."""

    path: Path
    line_number: int
    kind: CompletionGuardrailViolationKind

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("CompletionCallViolation.path must be absolute")
        if type(self.line_number) is not int or self.line_number < 1:
            raise ValueError("CompletionCallViolation.line_number must be positive")
        if type(self.kind) is not CompletionGuardrailViolationKind:
            raise ValueError(
                "CompletionGuardrailViolation.kind must be a known violation kind"
            )

    def describe(self) -> str:
        return f"{self.path}:{self.line_number}: prohibited {self.kind.value}"


@dataclass(frozen=True, slots=True)
class _CompletionModuleBindings:
    """Resolved import bindings relevant to completion-call ownership."""

    subprocess_modules: frozenset[str]
    subprocess_runs: frozenset[str]
    watchdog_owners: frozenset[str]
    watchdog_rebinding_lines: tuple[int, ...]


class ProcessCompletionCallGuardrail:
    """Require executor-test completion waits to use one watchdog owner."""

    _COMPLETION_METHODS = {
        "communicate": CompletionGuardrailViolationKind.COMMUNICATE,
        "result": CompletionGuardrailViolationKind.RESULT,
        "wait": CompletionGuardrailViolationKind.WAIT,
        "join": CompletionGuardrailViolationKind.JOIN,
    }
    _WATCHDOG_MODULE = "tests.process_completion_fixture"
    _WATCHDOG_SYMBOL = "PROCESS_COMPLETION_WATCHDOG"

    def __init__(self, client_paths: tuple[Path, ...]) -> None:
        if type(client_paths) is not tuple or not client_paths:
            raise ValueError("completion guardrail client paths must not be empty")
        if any(
            not isinstance(path, Path) or not path.is_absolute()
            for path in client_paths
        ):
            raise ValueError("completion guardrail client paths must be absolute")
        self._client_paths = client_paths

    def violations(self) -> tuple[CompletionGuardrailViolation, ...]:
        violations: list[CompletionGuardrailViolation] = []
        for path in self._client_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(self._violations_in_tree(path, tree))
        return tuple(
            sorted(
                violations,
                key=lambda violation: (
                    str(violation.path),
                    violation.line_number,
                    violation.kind.value,
                ),
            )
        )

    @classmethod
    def _violations_in_tree(
        cls,
        path: Path,
        tree: ast.AST,
    ) -> tuple[CompletionGuardrailViolation, ...]:
        bindings = cls._module_bindings(tree)
        violations = [
            CompletionGuardrailViolation(
                path,
                line_number,
                CompletionGuardrailViolationKind.WATCHDOG_OWNER_REBINDING,
            )
            for line_number in bindings.watchdog_rebinding_lines
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Name)
                and node.func.id in bindings.subprocess_runs
            ):
                violations.append(
                    CompletionGuardrailViolation(
                        path,
                        node.lineno,
                        CompletionGuardrailViolationKind.SUBPROCESS_RUN,
                    )
                )
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            receiver = node.func.value
            method_name = node.func.attr
            if (
                isinstance(receiver, ast.Name)
                and receiver.id in bindings.subprocess_modules
                and method_name == "run"
            ):
                violations.append(
                    CompletionGuardrailViolation(
                        path,
                        node.lineno,
                        CompletionGuardrailViolationKind.SUBPROCESS_RUN,
                    )
                )
                continue
            if method_name not in cls._COMPLETION_METHODS:
                continue
            if (
                isinstance(receiver, ast.Name)
                and receiver.id in bindings.watchdog_owners
            ):
                continue
            violations.append(
                CompletionGuardrailViolation(
                    path,
                    node.lineno,
                    cls._COMPLETION_METHODS[method_name],
                )
            )
        return tuple(violations)

    @classmethod
    def _module_bindings(cls, tree: ast.AST) -> _CompletionModuleBindings:
        subprocess_modules: set[str] = set()
        subprocess_runs: set[str] = set()
        watchdog_owners: set[str] = set()
        imports: list[tuple[str, bool, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name.split(".")[0]
                    imports.append((local_name, False, node.lineno))
                    if alias.name == "subprocess":
                        subprocess_modules.add(local_name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    is_watchdog_owner = (
                        node.module == cls._WATCHDOG_MODULE
                        and alias.name == cls._WATCHDOG_SYMBOL
                    )
                    imports.append((local_name, is_watchdog_owner, node.lineno))
                    if node.module == "subprocess" and alias.name == "run":
                        subprocess_runs.add(local_name)
                    if is_watchdog_owner:
                        watchdog_owners.add(local_name)

        rebinding_lines = {
            line_number
            for local_name, is_watchdog_owner, line_number in imports
            if local_name in watchdog_owners and not is_watchdog_owner
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and node.id in watchdog_owners
            ):
                rebinding_lines.add(node.lineno)
            elif isinstance(node, ast.arg) and node.arg in watchdog_owners:
                rebinding_lines.add(node.lineno)
            elif (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in watchdog_owners
            ):
                rebinding_lines.add(node.lineno)
            elif isinstance(node, ast.ExceptHandler) and node.name in watchdog_owners:
                rebinding_lines.add(node.lineno)

        if rebinding_lines:
            watchdog_owners.clear()
        return _CompletionModuleBindings(
            subprocess_modules=frozenset(subprocess_modules),
            subprocess_runs=frozenset(subprocess_runs),
            watchdog_owners=frozenset(watchdog_owners),
            watchdog_rebinding_lines=tuple(sorted(rebinding_lines)),
        )


def test_executor_completion_clients_use_shared_watchdog_owner() -> None:
    violations = ProcessCompletionCallGuardrail(
        EXECUTOR_COMPLETION_CLIENTS
    ).violations()

    assert violations == (), "\n".join(violation.describe() for violation in violations)


def test_completion_guardrail_rejects_bounded_and_unbounded_bypasses(
    tmp_path: Path,
) -> None:
    bypass = (tmp_path / "completion_bypass.py").resolve()
    bypass.write_text(
        """
import subprocess
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG

subprocess.run(('command',), timeout=5)
process.communicate(timeout=5)
process.wait()
event.wait(timeout=5)
future.result(timeout=5)
thread.join(timeout=5)
PROCESS_COMPLETION_WATCHDOG.wait(process, operation='owned')
PROCESS_COMPLETION_WATCHDOG.join_thread(thread, operation='owned thread')
""",
        encoding="utf-8",
    )

    violations = ProcessCompletionCallGuardrail((bypass,)).violations()

    assert tuple(violation.kind for violation in violations) == (
        CompletionGuardrailViolationKind.SUBPROCESS_RUN,
        CompletionGuardrailViolationKind.COMMUNICATE,
        CompletionGuardrailViolationKind.WAIT,
        CompletionGuardrailViolationKind.WAIT,
        CompletionGuardrailViolationKind.RESULT,
        CompletionGuardrailViolationKind.JOIN,
    )


def test_completion_guardrail_resolves_subprocess_import_aliases(
    tmp_path: Path,
) -> None:
    bypass = (tmp_path / "aliased_subprocess_bypass.py").resolve()
    bypass.write_text(
        """
import subprocess as process_api
from subprocess import run as execute

process_api.run(('command',), timeout=5)
execute(('command',), timeout=5)
""",
        encoding="utf-8",
    )

    violations = ProcessCompletionCallGuardrail((bypass,)).violations()

    assert tuple(violation.kind for violation in violations) == (
        CompletionGuardrailViolationKind.SUBPROCESS_RUN,
        CompletionGuardrailViolationKind.SUBPROCESS_RUN,
    )


def test_completion_guardrail_rejects_watchdog_owner_rebinding(
    tmp_path: Path,
) -> None:
    bypass = (tmp_path / "shadowed_watchdog.py").resolve()
    bypass.write_text(
        """
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG

PROCESS_COMPLETION_WATCHDOG = process
PROCESS_COMPLETION_WATCHDOG.wait(timeout=5)
""",
        encoding="utf-8",
    )

    violations = ProcessCompletionCallGuardrail((bypass,)).violations()

    assert tuple(violation.kind for violation in violations) == (
        CompletionGuardrailViolationKind.WATCHDOG_OWNER_REBINDING,
        CompletionGuardrailViolationKind.WAIT,
    )
