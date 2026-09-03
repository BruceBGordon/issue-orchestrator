"""Every read of the process table goes through one owner.

``ps`` takes its output width from the environment and truncates the COMMAND
column to it, even into a pipe. That cost PR #7143 a CI failure, and the first
fix — ``-ww`` added by hand at each call site — drifted the same day it was
written: five sites got the flag, three of them kept inheriting ``COLUMNS``,
and a shell script got neither.

So this does not check the five sites we know about. It walks the repository
for ``ps`` invocations and requires each one to come from
``infra/process_table``, so the seventh caller cannot drift either.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.infra.process_table import (
    FULL_WIDTH_FLAG,
    ps_command,
    ps_env,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SEARCH_ROOTS = ("src", "tests", "scripts", "tools", "docker", "hooks")
OWNER_MODULE = "src/issue_orchestrator/infra/process_table.py"

# Direct invocations that deliberately do not use the owner, each with the
# reason. A shell script cannot import Python; it mirrors the owner inline and
# names it in a comment, which the shell rule below checks for.
DIRECT_INVOCATION_ALLOWLIST: dict[str, str] = {
    "scripts/start_control_center.sh": (
        "shell cannot import the owner; mirrors it inline with -ww and env -u"
    ),
}

# ``docker ps`` is a different program with a different table; the fixed-width
# lookbehind is the cheapest way to say so. Comments are stripped before
# matching, so prose about ps does not count as calling it.
_SHELL_PS = re.compile(r"(?<![\w./-])(?<!docker )ps\s+-")


def _shell_code(line: str) -> str:
    return line.split("#", maxsplit=1)[0]


@dataclass(frozen=True)
class Invocation:
    path: str
    line: int
    source: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.source}"


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
        files.extend(base.rglob("*.sh"))
    return sorted(files)


def find_direct_ps_invocations() -> tuple[Invocation, ...]:
    """Every place that spells out a ``ps`` call instead of asking the owner.

    Python: a list literal whose first element is ``"ps"`` -- what a caller
    writes when they build the invocation themselves. Using ``ps_command``
    leaves no such literal, so routing a caller through the owner is what makes
    it disappear from this list.

    Shell: a ``ps`` command with options.
    """
    found: list[Invocation] = []
    for path in _iter_files():
        relative = str(path.relative_to(REPO_ROOT))
        if relative == OWNER_MODULE:
            continue
        text = path.read_text(encoding="utf-8")
        if "ps" not in text:
            continue
        if path.suffix == ".sh":
            found.extend(
                Invocation(relative, index, line.strip())
                for index, line in enumerate(text.splitlines(), start=1)
                if _SHELL_PS.search(_shell_code(line))
            )
            continue
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if (
                isinstance(node, ast.List)
                and node.elts
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == "ps"
            ):
                found.append(Invocation(relative, node.lineno, ast.unparse(node)[:70]))
    return tuple(found)


def test_no_caller_builds_its_own_ps_invocation() -> None:
    """The induction: a new direct call fails here, it does not just drift."""
    unowned = [
        invocation
        for invocation in find_direct_ps_invocations()
        if invocation.path not in DIRECT_INVOCATION_ALLOWLIST
    ]

    assert not unowned, (
        "these read the process table without the owner, so they take their "
        "width from the environment:\n  "
        + "\n  ".join(str(item) for item in unowned)
        + "\nUse infra.process_table.ps_command/ps_env."
    )


def test_the_allowlisted_shell_script_mirrors_the_owner() -> None:
    """An allowlist entry is a promise; this is the promise being checked."""
    for relative, reason in DIRECT_INVOCATION_ALLOWLIST.items():
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert reason, f"{relative} must say why it is exempt"
        for line in text.splitlines():
            if not _SHELL_PS.search(line):
                continue
            assert FULL_WIDTH_FLAG in line, f"{relative}: {line.strip()!r} lacks -ww"
            assert "-u COLUMNS" in line, f"{relative}: {line.strip()!r} inherits COLUMNS"
        assert "process_table" in text, (
            f"{relative} must name the owner it mirrors, so the two move together"
        )


def test_the_allowlist_carries_no_stale_entries() -> None:
    live = {invocation.path for invocation in find_direct_ps_invocations()}

    stale = set(DIRECT_INVOCATION_ALLOWLIST) - live

    assert not stale, f"allowlisted files no longer invoke ps: {stale}"


def test_the_scan_finds_a_planted_direct_invocation(tmp_path: Path) -> None:
    """Anti-vacuum: a scan that matched nothing would pass forever."""
    (tmp_path / "sneaky.py").write_text(
        'import subprocess\nsubprocess.run(["ps", "-A", "-o", "pid="])\n',
        encoding="utf-8",
    )
    (tmp_path / "sneaky.sh").write_text("ps -ax -o pid=\n", encoding="utf-8")

    found = [
        node
        for path in sorted(tmp_path.glob("sneaky.*"))
        for node in _scan_one(path)
    ]

    assert len(found) == 2, f"the scan missed a planted invocation: {found}"


def _scan_one(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".sh":
        return [
            line for line in text.splitlines() if _SHELL_PS.search(_shell_code(line))
        ]
    return [
        ast.unparse(node)
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.List)
        and node.elts
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "ps"
    ]


class TestOwner:
    def test_the_width_flag_leads_the_invocation(self) -> None:
        invocation = ps_command("-A", "-o", "pid=")

        assert invocation[0] == "ps"
        assert invocation[1] == FULL_WIDTH_FLAG
        assert invocation[2:] == ["-A", "-o", "pid="]

    def test_the_width_variables_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLUMNS", "80")
        monkeypatch.setenv("LINES", "24")
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

        env = ps_env()

        assert "COLUMNS" not in env
        assert "LINES" not in env
        assert "PATH" in env, "the scrub must not replace the environment"

    def test_overrides_ride_along(self) -> None:
        assert ps_env(LC_ALL="C")["LC_ALL"] == "C"

    def test_the_invocation_this_platform_gets_is_accepted_by_this_platform(
        self,
    ) -> None:
        """macOS ps rejects ``-ww`` before a bare BSD selector; catch that here
        rather than in whatever gate first shells out."""
        completed = subprocess.run(
            ps_command("-A", "-o", "pid=,command="),
            capture_output=True,
            text=True,
            env=ps_env(),
            timeout=30,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip(), "ps returned no rows"
