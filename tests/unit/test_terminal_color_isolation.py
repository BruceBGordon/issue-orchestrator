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
import re
import sys
from pathlib import Path

import pytest

from tests.conftest import TERMINAL_TEST_COLUMNS
from tests.process_group_run import run_in_process_group

# The exact tests #7155 reported failing, named individually rather than by
# file. The child run has to stay small: this test already runs inside a lane
# that is itself under xdist, and a child pytest collecting three whole files
# (131 tests) makes the guard sensitive to load rather than to the bug. Naming
# the cases keeps the child at six tests and the signal about colour only.
ORIGINALLY_FAILING = [
    "tests/unit/test_ai_gate_cli.py::TestAiGateCLI"
    "::test_setup_hooks_reports_invalid_codex_registration",
    "tests/unit/test_ai_gate_cli.py::TestAiGateCLI"
    "::test_setup_hooks_reports_flat_config_layout_error",
    # Parametrized; the node id without a suffix runs both cases.
    "tests/unit/test_ai_gate_cli.py::TestAiGateCLI"
    "::test_setup_hooks_prints_actionable_ai_gate_failure_details",
    "tests/unit/test_cli.py::TestCmdSetupGuardrails"
    "::test_cmd_setup_guardrails_reports_flat_config_layout_error",
    "tests/unit/test_trace_issue.py::TestCmdTrace::test_no_entries_found",
]
# Five node ids, one of them parametrized with two cases.
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

    What this DOES prove: the CLI suites are immune to a colour-forcing
    ambient environment, which is the bug that cost four sessions a round of
    confusion each.

    What it does NOT prove, despite an earlier version of this docstring
    claiming so: that the autouse fixture is load-bearing. Deleting
    ``isolate_terminal_color_env`` leaves every other test in this file
    passing — verified, not reasoned. The child imports the same
    ``tests/conftest.py``, whose MODULE-LEVEL block pops the colour variables
    and pins ``COLUMNS`` before collection, so the child is protected with or
    without the fixture.

    The two mechanisms cover different things and both are wanted: the
    module-level block fixes consoles built during collection (which is most
    of them, since test modules import the CLI at module scope), and the
    fixture stops one test's environment changes leaking into the next. The
    fixture's own behaviour is asserted by
    ``test_the_fixture_restores_an_environment_a_test_dirties`` below, which
    is a real control for it.
    """
    env = dict(os.environ)
    # Strip the options this process was invoked with. `PYTEST_ADDOPTS` rides
    # in the ENVIRONMENT, and the child disables xdist and the cache provider,
    # so `PYTEST_ADDOPTS="-n 2"` makes the child exit 4 with `unrecognized
    # arguments: -n`. (A command-line `-n`, which is how this repo's gate
    # supplies it, does not reach the child — the vector is the env var.)
    #
    # The `PYTEST_XDIST_*` vars go too. The unit lane runs at `-n 12`, so the
    # child would inherit `PYTEST_XDIST_WORKER=gwN` while itself running
    # `-p no:xdist`; `control/isolation.py` keys the orchestrator IPC socket
    # path off exactly that name, so an inherited value points a child at a
    # live worker's socket. Not reproduced by these six cases — stripped
    # because a child pretending to be a worker it is not is a hazard, not
    # because it has bitten yet.
    for inherited in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_CURRENT_TEST",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        env.pop(inherited, None)
    env["FORCE_COLOR"] = "3"
    # A width narrow enough to force the wrap that split "hooks must be a JSON
    # object"; the fixture must override it rather than merely strip colour.
    env["COLUMNS"] = "60"
    env.pop("NO_COLOR", None)

    # A1: the repo's process-lifecycle owner, per tests/AGENTS.md. A bare
    # `subprocess.run` timeout kills only the pytest leader; collection,
    # fixtures and the CLI tests themselves can leave descendants behind, and
    # a second partial lifecycle implementation is exactly what that rule
    # exists to prevent.
    completed = run_in_process_group(
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
        timeout=600,
    )

    assert completed.returncode == 0, (
        "CLI suites must be immune to FORCE_COLOR in the ambient environment.\n"
        f"stdout tail:\n{completed.stdout[-4000:]}\n"
        f"stderr tail:\n{completed.stderr[-2000:]}"
    )
    # A child that ran FEWER cases than asked proves less than it appears to.
    #
    # Read the SUMMARY LINE, not the whole stream. `log_cli = true`
    # (pyproject.toml) makes the child print a node id per test even under
    # `-q`, so a parametrize id is free text in this output: an id containing
    # the words "6 passed" satisfied a naive `re.search(r"(\d+) passed")`
    # while only five of the six cases ran — the exact vacuity this guard
    # exists to prevent.
    #
    # The count itself is NOT pinned. Tying it to a constant means a
    # legitimate new param case in an unrelated CLI file fails this test with
    # a message about stale node ids, which is a lie. What must hold is that
    # nothing was silently dropped: every case either passed, or the run is
    # not evidence.
    summary = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    assert "deselected" not in summary and "skipped" not in summary, (
        "cases were dropped from the child run, so it is not evidence about "
        f"colour at all: {summary!r}\n"
        f"stdout tail:\n{completed.stdout[-2000:]}"
    )
    passed = re.search(r"(\d+) passed", summary)
    assert passed and int(passed.group(1)) >= len(ORIGINALLY_FAILING), (
        f"expected at least {len(ORIGINALLY_FAILING)} cases (one per node id, "
        f"more when a case is parametrized); summary was {summary!r}\n"
        f"stdout tail:\n{completed.stdout[-2000:]}"
    )


def test_the_fixture_restores_an_environment_a_test_dirties() -> None:
    """A real control for the autouse fixture, which the child run is not.

    The module-level scrub runs ONCE, at conftest import, so it cannot undo
    anything a test sets afterwards. That is the fixture's job and the only
    thing that distinguishes it from the block above it.

    This invokes the ACTUAL fixture body via ``__wrapped__`` rather than
    re-implementing it: a copy of the logic would pass whether or not the
    fixture existed, which is a trap this file has already fallen into once.

    BOTH variables are dirtied on purpose. Asserting `COLUMNS` without having
    changed it passed even with the fixture's `setenv` deleted, because the
    module-level block had already pinned it — an assertion that could only
    ever succeed.
    """
    from tests import conftest as shared_conftest

    os.environ["FORCE_COLOR"] = "3"
    os.environ["COLUMNS"] = "17"
    patcher = pytest.MonkeyPatch()
    try:
        shared_conftest.isolate_terminal_color_env.__wrapped__(patcher)

        assert "FORCE_COLOR" not in os.environ, (
            "the fixture did not strip a colour variable a test had set"
        )
        assert os.environ["COLUMNS"] == TERMINAL_TEST_COLUMNS, (
            "the fixture did not re-pin a width a test had changed"
        )
    finally:
        patcher.undo()
        os.environ.pop("FORCE_COLOR", None)
        os.environ["COLUMNS"] = TERMINAL_TEST_COLUMNS


def test_the_colour_fixture_is_applied_without_being_requested() -> None:
    """Autouse, proven through pytest rather than through its internals.

    An earlier version read `_fixture_function_marker` off the decorated
    fixture. That was wrong twice: it is a private attribute (SLF001), and it
    does not exist on the declared floor of `pytest>=8.0`, so the test would
    raise `AttributeError` on a supported version. The repo's ruff run scans
    `src` only, so a green gate said nothing about it.

    What autouse actually means is observable without touching internals: a
    case that never mentions the fixture still gets it. So a child suite is
    written INTO `tests/unit/`, where the real shared conftest applies, with
    two cases in order — the first dirties the environment, the second
    asserts it came back clean. Neither requests the fixture by name.

    Goes red if `autouse=True` is dropped (the second case sees the dirt) and
    if the scrub loop is deleted (same). Serial by construction, and the probe
    file is removed in `finally`.
    """
    probe = _repo_root() / "tests" / "unit" / f"_autouse_probe_{os.getpid()}.py"
    probe.write_text(
        "import os\n"
        "\n"
        "def test_a_dirties_the_environment():\n"
        "    os.environ['FORCE_COLOR'] = '3'\n"
        "\n"
        "def test_b_sees_it_cleaned_without_asking():\n"
        "    assert 'FORCE_COLOR' not in os.environ\n",
        encoding="utf-8",
    )
    try:
        completed = run_in_process_group(
            [
                sys.executable, "-m", "pytest",
                str(probe.relative_to(_repo_root())),
                "-q", "-p", "no:xdist", "-p", "no:cacheprovider",
                "-p", "no:randomly", "--no-header",
            ],
            cwd=_repo_root(),
            env={k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")},
            timeout=300,
        )
    finally:
        probe.unlink(missing_ok=True)

    assert completed.returncode == 0, (
        "a case that never requested the colour fixture did not get it, so "
        "the fixture is not protecting the tests it exists for.\n"
        f"stdout tail:\n{completed.stdout[-2000:]}"
    )
    summary = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    assert "2 passed" in summary, f"probe did not run both cases: {summary!r}"
