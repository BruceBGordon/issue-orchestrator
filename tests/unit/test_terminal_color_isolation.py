"""Rich output under test must not vary with the shell that launched pytest.

Guards the fix for #7155, where ``FORCE_COLOR=3`` in an agent shell made six
CLI tests fail — twice over, once from ANSI markup splitting an expected
substring and once from rich adopting the real terminal width and wrapping a
line mid-assertion.

The in-process tests below pin the fixture's contract. The subprocess test is
the one that actually reproduces the reported bug: it runs the originally
failing tests in a child pytest whose environment carries ``FORCE_COLOR``,
which is the only way to exercise a fixture that strips it before any test in
this process can observe it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import TERMINAL_TEST_COLUMNS

# The exact tests that #7155 reported failing, across all three files.
ORIGINALLY_FAILING = [
    "tests/unit/test_ai_gate_cli.py",
    "tests/unit/test_cli.py",
    "tests/unit/test_trace_issue.py",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "variable", ["FORCE_COLOR", "CLICOLOR_FORCE", "CLICOLOR", "NO_COLOR"]
)
def test_colour_forcing_variables_are_stripped(variable: str) -> None:
    """No colour-forcing variable survives into a test's environment.

    The autouse fixture is what makes CLI substring assertions legitimate; if
    a variable leaks, those assertions become shell-dependent again.
    """
    assert variable not in os.environ


def test_terminal_width_is_pinned() -> None:
    """Width is fixed, so a wrap cannot land in the middle of an assertion."""
    assert os.environ["COLUMNS"] == TERMINAL_TEST_COLUMNS


def test_rich_renders_plain_text_at_the_pinned_width() -> None:
    """The end state the fixture exists to produce, asserted on rich itself.

    Checks the rendering rather than the environment: a console built the way
    the CLI builds one emits no escape codes and wraps at the pinned width.
    """
    from rich.console import Console

    console = Console()

    assert console.width == int(TERMINAL_TEST_COLUMNS)
    assert not console.is_terminal
    with console.capture() as captured:
        console.print("[red]config/modes/<mode>/[/red]")
    rendered = captured.get()
    assert "\x1b[" not in rendered
    assert "config/modes/<mode>/" in rendered


def test_cli_suites_pass_with_force_color_set_in_the_environment() -> None:
    """The actual #7155 reproduction: a child pytest carrying FORCE_COLOR=3.

    A negative control for the fixture as a whole — deleting
    ``isolate_terminal_color_env`` from ``tests/conftest.py`` makes this fail
    with the original ``assert 'config/modes/<mode>/' in ...`` error, which is
    what stops the guard from silently rotting into a test that would pass
    with or without the fix.
    """
    env = dict(os.environ)
    env["FORCE_COLOR"] = "3"
    # A width narrow enough to force the wrap that split "hooks must be a JSON
    # object"; the fixture must override it rather than merely strip colour.
    env["COLUMNS"] = "60"
    env.pop("NO_COLOR", None)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *ORIGINALLY_FAILING,
            "-q",
            "-p",
            "no:xdist",
            "-p",
            "no:cacheprovider",
            "--no-header",
        ],
        cwd=_repo_root(),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert completed.returncode == 0, (
        "CLI suites must be immune to FORCE_COLOR in the ambient environment.\n"
        f"stdout tail:\n{completed.stdout[-4000:]}\n"
        f"stderr tail:\n{completed.stderr[-2000:]}"
    )
