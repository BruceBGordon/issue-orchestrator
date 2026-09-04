"""Every declared console script must actually resolve and run.

``[project.scripts]`` is a string table: a rename in the code cannot break it
at import time, only at the moment a user runs the command. These tests close
that gap by doing exactly what the generated wrapper does — import the module,
resolve the attribute, call it with no arguments and use the return value as
the exit code — for every entry, read from ``pyproject.toml`` rather than from
a list maintained here, so a new script is covered the moment it is declared.

``lane-run`` gets more than resolution. It is installed for callers outside
this repository, so its tests exercise the property that makes that possible:
everything it reads and writes hangs off the *caller's* working directory,
never off this package's install location.
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
import tomllib
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_LANE_RUN_SCRIPT = "lane-run"
# From the lane exit-code contract in the lane_run module docstring.
_LANE_RUN_CONFIGURATION_EXIT_CODE = 78
# `python -m issue_orchestrator...` — this repo's own invocation form. The
# whole dotted token is captured and filtered afterwards; a pattern ending in
# `lane_run` would backtrack and match a renamed `lane_run_x` as if unchanged.
_MODULE_INVOCATION = re.compile(r"-m\s+(?P<module>issue_orchestrator[\w.]*)")
# The in-repo callers that name the interpreter instead of using the script.
_KNOWN_MODULE_CALLERS = ("Makefile", "docker/execenv/selftest.sh")


def _declared_console_scripts() -> dict[str, str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        return dict(tomllib.load(file)["project"]["scripts"])


def _resolve(target: str) -> Callable[..., object]:
    """Resolve a ``module:attribute`` entry point the way installers do."""
    module_name, separator, attribute = target.partition(":")
    assert separator, f"entry point {target!r} is not 'module:attribute'"
    module = importlib.import_module(module_name)
    assert hasattr(module, attribute), (
        f"{module_name} declares no {attribute!r} — the console-script table in "
        f"pyproject.toml points at a name that no longer exists."
    )
    resolved = getattr(module, attribute)
    assert callable(resolved), f"{target} resolved to a non-callable"
    return resolved


def _invoke_lane_run_entry_point() -> object:
    """Resolve and call the declared entry point exactly as the wrapper does.

    ``sys.exit(main())``: no arguments, so ``sys.argv`` is the input, and the
    return value is the process exit code.
    """
    return _resolve(_declared_console_scripts()[_LANE_RUN_SCRIPT])()


def _declare_lane(worktree: Path, work_key: str) -> None:
    """Write the caller repo's own lanes.yaml, as a foreign repo would."""
    declarations = worktree / ".issue-orchestrator"
    declarations.mkdir()
    (declarations / "lanes.yaml").write_text(
        f"lanes:\n"
        f"  {work_key}:\n"
        f"    request_cpus: 1\n"
        f"    memory_mb: 512\n"
        f"    suspendability: anywhere\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("script", "target"), sorted(_declared_console_scripts().items())
)
def test_declared_console_script_resolves(script: str, target: str) -> None:
    del script
    _resolve(target)


@pytest.mark.parametrize(
    ("script", "target"), sorted(_declared_console_scripts().items())
)
def test_declared_console_script_takes_no_required_argument(
    script: str, target: str
) -> None:
    """The generated wrapper calls ``func()``. A target that requires an
    argument would raise TypeError on the user's first invocation and on no
    test before it."""
    del script
    required = [
        name
        for name, parameter in inspect.signature(_resolve(target)).parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    assert not required, (
        f"{target} requires {required}; a console script is invoked with no arguments."
    )


def test_lane_run_is_installed_as_a_console_script() -> None:
    """Repos that are not this one dispatch lanes by name, not by reaching
    into this package's virtualenv with an absolute interpreter path."""
    assert _LANE_RUN_SCRIPT in _declared_console_scripts()


def test_lane_run_entry_point_returns_the_documented_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared target must *return* the exit code, not raise it.

    The wrapper is ``sys.exit(main())``, so an entry point wired to a function
    that returns None (or one that only ever calls ``sys.exit`` itself) would
    silently report success for a configuration error. This drives the entry
    point exactly as the wrapper does — no argv argument, so ``sys.argv`` is
    the input — on an invocation whose documented answer is 78.
    """
    monkeypatch.setattr(sys, "argv", ["lane-run", "--work-key", "unit.probe"])

    assert _invoke_lane_run_entry_point() == _LANE_RUN_CONFIGURATION_EXIT_CODE


def test_lane_run_dispatches_from_a_worktree_that_is_not_this_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of the console script, proven at the entry point.

    The lanes file, the runtime history, and the dispatch journal all resolve
    against the caller's working directory, so a foreign repo declares its own
    lanes and keeps its own dispatch artifacts. Nothing may resolve against
    this package's install location.
    """
    _declare_lane(tmp_path, "foreign.lane")
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lane-run",
            "--work-key",
            "foreign.lane",
            "--timeout-seconds",
            "60",
            "--",
            "/usr/bin/true",
        ],
    )

    assert _invoke_lane_run_entry_point() == 0
    assert (tmp_path / ".git" / "issue-orchestrator" / "lane-dispatch.jsonl").exists()


def test_lane_run_dispatches_where_there_is_no_repository_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller need not be a git repo — only a directory with lanes.

    Outside a repository there is nothing to share across invocations,
    so the learning loop goes inert rather than failing or inventing a
    home for its state: priority 0, recorded nowhere. The docs promise
    this works; nothing pinned it until here.
    """
    _declare_lane(tmp_path, "no.repo.lane")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lane-run",
            "--work-key",
            "no.repo.lane",
            "--timeout-seconds",
            "60",
            "--",
            "/usr/bin/true",
        ],
    )

    assert _invoke_lane_run_entry_point() == 0
    # Inert means inert: neither artifact is written, and no repository
    # is conjured to hold them. Named artifacts rather than a directory
    # listing — shared fixtures put unrelated entries under tmp_path.
    assert not (tmp_path / ".git").exists()
    assert not list(tmp_path.rglob("lane-dispatch.jsonl"))
    assert not list(tmp_path.rglob("lane-runtime-history"))


def test_lane_run_refuses_a_work_key_the_caller_never_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No policy by absence, and the message names the caller's own file."""
    _declare_lane(tmp_path, "declared.lane")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lane-run",
            "--work-key",
            "never.declared",
            "--timeout-seconds",
            "60",
            "--",
            "/usr/bin/true",
        ],
    )

    assert _invoke_lane_run_entry_point() == _LANE_RUN_CONFIGURATION_EXIT_CODE


def test_lane_run_without_a_lanes_file_names_the_callers_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A caller with no declarations at all must be sent to its own path,
    not to this repository's."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lane-run",
            "--work-key",
            "anything",
            "--timeout-seconds",
            "60",
            "--",
            "/usr/bin/true",
        ],
    )

    assert _invoke_lane_run_entry_point() == _LANE_RUN_CONFIGURATION_EXIT_CODE
    assert str(tmp_path.resolve()) in capsys.readouterr().err


def _module_invocation_sites() -> dict[str, set[str]]:
    """Every build surface that runs lane_run as ``python -m <module>``."""
    candidates = [REPO_ROOT / "Makefile"]
    candidates.extend(sorted((REPO_ROOT / "docker").rglob("*.sh")))
    candidates.extend(sorted((REPO_ROOT / "scripts").rglob("*.sh")))
    sites: dict[str, set[str]] = {}
    for path in candidates:
        modules = {
            match.group("module")
            for match in _MODULE_INVOCATION.finditer(path.read_text(encoding="utf-8"))
            if match.group("module").rpartition(".")[2] == "lane_run"
        }
        if modules:
            sites[str(path.relative_to(REPO_ROOT))] = modules
    return sites


def test_in_repo_callers_and_the_console_script_enter_the_same_module() -> None:
    """Two invocation forms, one implementation — bound by this test.

    This repo's own callers keep ``python -m <module>`` so the interpreter is
    named rather than resolved through PATH, where the `uv tool install`
    documented for foreign repos could shadow it; foreign callers get the
    console script. A rename that moved only one form would leave the gate and
    the published contract on different code, so the modules are compared
    rather than trusted.
    """
    sites = _module_invocation_sites()
    declared_module = _declared_console_scripts()[_LANE_RUN_SCRIPT].partition(":")[0]

    missing = [caller for caller in _KNOWN_MODULE_CALLERS if caller not in sites]
    assert not missing, (
        f"{missing} no longer invoke lane_run as 'python -m' — either they "
        f"moved to the console script (drop them from _KNOWN_MODULE_CALLERS) "
        f"or this probe stopped matching."
    )
    drifted = {
        caller: sorted(modules)
        for caller, modules in sites.items()
        if modules != {declared_module}
    }
    assert not drifted, (
        f"These callers name a module the console script does not: {drifted}. "
        f"pyproject.toml declares {declared_module!r}."
    )
