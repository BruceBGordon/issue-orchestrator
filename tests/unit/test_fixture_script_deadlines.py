"""Every fixture script this repo spawns must die of its own clock.

Cleanup in a ``finally`` protects a harness that gets to run its ``finally``.
It does nothing for a harness that is SIGKILLed, loses power, or is torn down
by a pytest timeout — and the 2026-08-29 leak (#7142) is exactly what the
machine looks like afterwards. So each fixture carries an independent deadline,
and this module reproduces the escape to prove it: start the fixture, kill its
supervisor outright, and require the orphan to be gone on time.

Two separate properties, deliberately not merged:

* the *mechanism* — the script honours the deadline it is handed, tested with a
  short one so the suite stays fast;
* the *policy* — every lifetime any fixture is spawned with is finite and
  measured in minutes.

The policy half **discovers** lifetimes rather than listing them. A
hand-maintained list is a list someone forgets to extend: the first draft of
this module checked four constants and missed a ``time.sleep(600)`` lane job in
a file it already imported from. The scan below walks the whole test tree for
the shapes that produce a fixture process, so a new one is in budget or it
fails here — it cannot slide by not being mentioned.

TERM-immunity is re-asserted alongside, because a fixture that quietly started
cooperating with SIGTERM would make the contract tests that use it vacuous.

Error model, stated so it stops being re-traded
-----------------------------------------------

This is an induction test, so the two ways it can be wrong are not equal:

* a **false negative** is a real fixture with no ceiling, sliding through
  silently — the failure this module exists to prevent;
* a **false positive** is a line of prose flagged as a fixture, which someone
  reads, disagrees with, and puts in ``LIFETIME_ALLOWLIST`` with a reason.

One is a leak; the other is a decision on the record. So the scan
deliberately over-includes, and ``LIFETIME_ALLOWLIST`` is the escape — for
genuine prose exactly as much as for a justified long-running fixture.

The temptation each round is to tighten the rule until the noise stops. That
trade was made once already, by requiring a recognisable interpreter beside
``-c``, and it bought a quieter scan at the price of missing ``python3 -uc``.
Tighten *shapes* (what counts as a docstring, what parses as code); do not
tighten *reach* (which strings are examined at all) without moving the missed
fixtures somewhere they are still caught.

Two contexts, and they are not the same
---------------------------------------

A fixture spells its invocation one of two ways, and what counts as valid
differs between them. Collapsing the two is how an accepted invocation got
called impossible:

* a **command string** -- ``f"{sys.executable} -c 'import time; ...'"`` -- is
  split by a shell before Python sees it. A script attached with no quotes
  cannot survive that split: ``python -cimport time; time.sleep(9)`` arrives
  as ``-cimport`` plus stray words, and Python reports a SyntaxError.
* an **argv element** -- ``[sys.executable, "-cimport time; time.sleep(9)"]``
  -- is handed to the OS verbatim. Nothing splits it, so the attachment is
  real and CPython runs it.

The second was missed for exactly as long as the rejection was "proved" by
running the first through ``shlex.split``. Both rules are pinned by tests that
ask the real interpreter, in the shape the fixture would actually use.
"""

from __future__ import annotations

import ast
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.integration.test_condor_lane_executor import _ESCAPE_SCRIPT
from tests.unit.lane_executor_contract import _TREE_SCRIPT

# Short enough to keep this suite fast, long enough that the fixture is
# provably alive while the harness is killed.
SHORT_LIFETIME_SECONDS = 3.0
# Slack over the fixture's own deadline before we call it a leak.
EXPIRY_SLACK_SECONDS = 20.0
# "Minutes, not hours." A fixture that outlives a quarter hour outlives most of
# the gates that would then be blamed for its load. Named so the scan below
# does not discover its own budget as a fixture lifetime.
LIFETIME_BUDGET_SECONDS = 900.0

_MARKER_TIMEOUT_SECONDS = 15.0
_POLL_SECONDS = 0.02


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _await_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(_POLL_SECONDS)
    return False


def _await_recorded_pid(marker: Path) -> int:
    """The pid the fixture wrote, once it is fully written."""
    deadline = time.monotonic() + _MARKER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            return int(marker.read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(_POLL_SECONDS)
    raise AssertionError(f"fixture never recorded a pid at {marker}")


# A harness that takes cpu_load's guarantee and then dies without running it.
# `cpu_load` reaps in a finally; a SIGKILLed process has no finally, so what is
# left is the burner's own deadline and nothing else.
_CPU_LOAD_HARNESS = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[3])\n"
    "from tests.load_fixture import cpu_load\n"
    "lifetime = float(sys.argv[2])\n"
    "with cpu_load(workers=1, max_lifetime_seconds=lifetime) as pids:\n"
    "    open(sys.argv[1], 'w').write(str(pids[0]))\n"
    "    time.sleep(lifetime + 30)\n"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _spawn(script: str, marker: Path, *extra: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(marker),
            str(SHORT_LIFETIME_SECONDS),
            *extra,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _force_kill(*pids: int) -> None:
    """Backstop, so a failure here cannot leak what it spawned."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


@pytest.mark.parametrize(
    ("script", "term_immune"),
    [
        pytest.param(_TREE_SCRIPT, True, id="lane-contract-tree"),
        pytest.param(_ESCAPE_SCRIPT, False, id="condor-setsid-escapee"),
    ],
)
def test_an_orphaned_fixture_expires_without_anyone_reaping_it(
    tmp_path: Path, script: str, term_immune: bool
) -> None:
    """Kill the supervisor outright; the orphan must still go away."""
    marker = tmp_path / "grandchild.pid"
    supervisor = _spawn(script, marker)
    orphan = _await_recorded_pid(marker)
    try:
        if term_immune:
            os.kill(orphan, signal.SIGTERM)
            time.sleep(0.5)
            assert _is_alive(orphan), (
                "fixture now dies on SIGTERM, so the contract test using it no "
                "longer proves the backend killed anything"
            )

        # The reviewer's repro: nothing gets to run its cleanup.
        os.kill(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=EXPIRY_SLACK_SECONDS)
        assert orphan != supervisor.pid
        assert _await_gone(orphan, SHORT_LIFETIME_SECONDS + EXPIRY_SLACK_SECONDS), (
            f"orphaned fixture {orphan} outlived its own "
            f"{SHORT_LIFETIME_SECONDS}s deadline with no supervisor left to "
            "reap it; this is the shape that poisoned nine hours of gates"
        )
    finally:
        _force_kill(orphan, supervisor.pid)


def test_a_cpu_load_burner_expires_when_its_harness_is_sigkilled(
    tmp_path: Path,
) -> None:
    """``cpu_load``'s finally is not the last line of defence; the clock is."""
    marker = tmp_path / "burner.pid"
    harness = _spawn(_CPU_LOAD_HARNESS, marker, str(_REPO_ROOT))
    burner = _await_recorded_pid(marker)
    try:
        os.kill(harness.pid, signal.SIGKILL)
        harness.wait(timeout=EXPIRY_SLACK_SECONDS)
        assert _await_gone(burner, SHORT_LIFETIME_SECONDS + EXPIRY_SLACK_SECONDS), (
            f"burner {burner} outlived the harness that was going to reap it; "
            "this is the nine-hour incident exactly"
        )
    finally:
        _force_kill(burner, harness.pid)


# --------------------------------------------------------------------------
# Lifetime discovery
# --------------------------------------------------------------------------

TEST_TREE = Path(__file__).resolve().parents[1]

# Justified exceptions, as ``(path relative to tests/, matched source)``.
#
# An entry is a decision on the record, and there are two kinds. A fixture
# allowed to outlive the budget is the expensive kind — that is the thing that
# cost nine hours, and it should be argued for. Prose that the scan flagged
# because it quotes a real command is the cheap kind: the scan over-includes on
# purpose (see the error model above), and this is where that noise is
# answered, rather than by narrowing what the scan looks at.
LIFETIME_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # A sentence about a command, in the test that pins the error model.
        # No process is spawned from it — it is the flagged-prose example.
        ("unit/test_fixture_script_deadlines.py", "time.sleep(7200)"),
    }
)

# The shapes that give a spawned process its duration. Literal-valued only —
# a value is either statically knowable or it is not, and pretending otherwise
# would make this scan lie about its coverage.
#
# These calls count only inside string constants, which is what makes them
# precise: Python source inside a string literal is there to be executed
# somewhere else. The same call in ordinary test code is an AST node, not a
# string, and is correctly ignored — a harness waiting on its own child is not
# a fixture lifetime.
_SCRIPT_CALLS = ("time.sleep", "signal.alarm")
# The declared knobs that are threaded into a spawned script's argv.
_LIFETIME_CONSTANT_RE = re.compile(r"(LIFETIME|MAX)_SECONDS$")
# Cheap gate so the scan parses ~7% of the tree instead of all of it.
_SCAN_HINTS = ("time.sleep(", "signal.alarm(", "LIFETIME_SECONDS", "MAX_SECONDS")

# Known limits, stated rather than hidden. Neither is statically decidable:
#   * bare shell ``sleep N`` -- indistinguishable from a string *describing* a
#     command. This very file's sibling asserts on "/bin/sleep 3600" as parser
#     test data; scanning that shape would report it as an hour-long fixture.
#   * ``time.sleep(<expression>)`` -- a value computed at runtime is not a
#     literal. Both in this tree take theirs from argv, and the constants that
#     supply it are found by the constant rule above.

# Docstrings are prose that happens to sit in a string constant. A module whose
# only content is a docstring mentioning ``time.sleep(7200)`` is documentation,
# not a two-hour fixture, and reporting it would teach people to silence this.
_DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

# The end-of-options marker, and the prefix of a long option. Python has no
# long option that takes a script, so neither can introduce one.
_OPTION_TERMINATOR = "--"


@dataclass(frozen=True)
class DiscoveredLifetime:
    """One statically-valued duration handed to a spawned fixture process."""

    path: str
    line: int
    source: str
    seconds: float

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.source} ({self.seconds}s)"


def discover_fixture_lifetimes(tree: Path) -> tuple[DiscoveredLifetime, ...]:
    """Every fixture lifetime the test tree declares, found by reading it.

    Unfiltered on purpose: discovery reports what is there and the allowlist is
    applied by the policy below, so an excused entry is still visible to the
    staleness check that keeps the allowlist honest.
    """
    found: list[DiscoveredLifetime] = []
    for path in sorted(tree.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(hint in text for hint in _SCAN_HINTS):
            continue
        relative = str(path.relative_to(tree))
        module = ast.parse(text, filename=str(path))
        skip = _docstring_constants(module) | _fstring_fragments(module)
        in_argv = _argv_sequence_members(module)
        for node in ast.walk(module):
            if id(node) in skip:
                continue
            found.extend(_lifetimes_in_node(node, relative, in_argv))
    return tuple(found)


def _docstring_constants(module: ast.Module) -> set[int]:
    """The ids of every docstring constant: module, class and function."""
    ids: set[int] = set()
    for node in ast.walk(module):
        if not isinstance(node, _DOCSTRING_HOLDERS):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _argv_sequence_members(module: ast.Module) -> set[int]:
    """String constants that are elements of a list or tuple.

    Their argv context is the sequence, not themselves: a lone element is the
    whole command, but an element of a list has siblings — including, possibly,
    a ``--`` that turns it into a filename.
    """
    ids: set[int] = set()
    for node in ast.walk(module):
        if isinstance(node, (ast.List, ast.Tuple)):
            ids.update(
                id(element)
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    return ids


def _fstring_fragments(module: ast.Module) -> set[int]:
    """Constants that belong to an f-string, which is read as a whole."""
    ids: set[int] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.JoinedStr):
            ids.update(id(part) for part in ast.walk(node) if part is not node)
    return ids


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _candidate_sources(text: str, *, lone_element: bool = True) -> list[str]:
    """The ways a spawned script hides inside one string.

    Three shapes, all present in this tree:

    * the string is the script — ``"import time\\ntime.sleep(30)\\n"``;
    * the string is an indented block waiting for ``textwrap.dedent``;
    * the string is a *command line* running an interpreter with ``-c``, the
      script quoted inside it: ``f"{sys.executable} -c 'import time; ...'"``.

    The third is read structurally rather than by pattern — see
    :func:`_scripts_in_command`. Every string is offered, with no check that an
    interpreter is visible beside the flag: that is deliberate over-inclusion,
    and requiring a recognisable interpreter is what once made ``python3 -uc``
    invisible.

    Quotes alone are still not an argument boundary: ``"the run left
    'time.sleep(7200)' burning a core"`` names no flag and is not a command.
    """
    return [
        text,
        textwrap.dedent(text),
        *_scripts_in_command(text, lone_element=lone_element),
    ]


def _scripts_in_command(text: str, *, lone_element: bool = True) -> list[str]:
    """Scripts this string would hand an interpreter with ``-c``.

    Structural, not lexical. Five rounds of review found five different
    lexical details wrong in the regex this replaces — whitespace after the
    flag, an attached script, combined clusters, greediness across the letters
    of the script, a quote as an argument boundary. Each fix was correct and
    the next one was still waiting, because a pattern was being asked to be a
    shell lexer and losing to it one detail at a time.

    So the string is turned into argv and read as argv. ``shlex`` owns the
    quoting question, which is what it is for; walking the flag cluster with
    intent removes the greediness question, because nothing backtracks; and
    reading the cluster directly removes the cluster question.
    """
    scripts: list[str] = []
    for elements in _argv_readings(text, lone_element=lone_element):
        scripts.extend(_scripts_in_argv(elements))
    return scripts


def _argv_readings(text: str, *, lone_element: bool = True) -> list[list[str]]:
    """The ways this string could be argv — the two contexts, normalised.

    A **command string** is what a shell splits; a lone **argv element** is
    passed to the OS verbatim. A string constant in the tree could be either
    and nothing distinguishes them, so both readings are produced and the same
    rule is applied to each. The distinction survives as this step, rather
    than as two rule sets that drift apart.

    Unbalanced quoting keeps the naive split, which holds on to the separated
    forms. What it cannot recover is a multi-word script whose closing quote
    is missing, because nothing is left to say where the script ends -- and
    that is the same category as the round-7 command string: `sh -c` refuses
    it with "unexpected EOF", so there is no fixture there to miss. Over-
    inclusion buys a *possible* fixture at the cost of noise; this is not one.
    """
    try:
        shell_split = shlex.split(text)
    except ValueError:
        shell_split = text.split()
    if not lone_element:
        # An element of a list is read as part of that list, above.
        return [shell_split]
    return [shell_split, [text]]


def _scripts_in_argv(elements: list[str]) -> list[str]:
    """The ``-c`` scripts in one argv list.

    ``-c`` is a short option: it may sit anywhere in a cluster, and it takes
    the rest of its own element as the script, or the next element when its
    own has nothing left. The first ``c`` wins, because at that point every
    remaining character belongs to the script — which is exactly why a script
    beginning with ``class`` is not a longer cluster.
    """
    found: list[str] = []
    for index, element in enumerate(elements):
        if element == _OPTION_TERMINATOR:
            # Everything after it is an operand. Verified: `python -- -cSCRIPT`
            # exits 2 looking for a file called `-cSCRIPT`, so there is no
            # script here to give a deadline to.
            break
        if len(element) < 2 or not element.startswith("-"):
            continue
        if element.startswith(_OPTION_TERMINATOR):
            continue
        cluster = element[1:]
        position = cluster.find("c")
        if position < 0:
            continue
        attached = cluster[position + 1 :]
        if attached:
            found.append(attached)
        elif index + 1 < len(elements):
            found.append(elements[index + 1])
    return found


def _lifetimes_from(source: str) -> list[tuple[str, float]]:
    """Lifetimes in one already-extracted script."""
    tree = _parsed(source)
    return [] if tree is None else _lifetimes_in_tree(tree)


def _lifetimes_in_script(
    source: str, *, lone_element: bool = True
) -> list[tuple[str, float]]:
    """Durations a spawned script *executes*, not ones its text mentions.

    The string has to parse as Python and the call has to be a real ``Call``
    node with a literal first argument. Prose does not parse, and text that
    happens to parse still has to be code rather than a sentence about code.

    The first candidate that parses wins, and the rest are not consulted. If
    the string is valid Python then that is what it is, and the quoted runs
    inside it are its own literals — a docstring in a generated file is
    documentation there too, and looking inside it would re-admit exactly the
    prose this excludes.
    """
    for candidate in _candidate_sources(source, lone_element=lone_element):
        tree = _parsed(candidate)
        if tree is not None:
            return _lifetimes_in_tree(tree)
    return []


def _parsed(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _lifetimes_in_tree(tree: ast.Module) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if _dotted_name(node.func) not in _SCRIPT_CALLS:
            continue
        argument = node.args[0]
        if not isinstance(argument, ast.Constant):
            continue
        value = argument.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        found.append((f"{_dotted_name(node.func)}({value})", float(value)))
    return found


def _string_source(node: ast.AST) -> str | None:
    """The text of a string node, f-strings reconstructed whole.

    An f-string reaches ``ast.walk`` as fragments, and the fragment that
    matters here is the one that lost the interpreter: ``f"{sys.executable} -c
    '...'"`` leaves ``" -c '...'"``, which no longer looks like a command. So
    the placeholders are put back as their own source text -- the reader only
    needs to see that *something* names an interpreter, not what it evaluates
    to.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append(ast.unparse(value.value))
    return "".join(parts)


def _lifetimes_in_node(
    node: ast.AST, relative: str, argv_members: set[int]
) -> list[DiscoveredLifetime]:
    if isinstance(node, (ast.List, ast.Tuple)):
        # The literal argv the reviewer's normalisation asks for: elements
        # exactly as written, siblings and all.
        elements = [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        return [
            DiscoveredLifetime(relative, node.lineno, source, seconds)
            for script in _scripts_in_argv(elements)
            for source, seconds in _lifetimes_from(script)
        ]
    source_text = _string_source(node)
    if source_text is not None:
        return [
            DiscoveredLifetime(relative, node.lineno, source, seconds)
            for source, seconds in _lifetimes_in_script(
                source_text, lone_element=id(node) not in argv_members
            )
        ]
    if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
        return []
    value = node.value.value
    # ``bool`` is an ``int``; a flag is not a duration.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return []
    return [
        DiscoveredLifetime(relative, node.lineno, target.id, float(value))
        for target in node.targets
        if isinstance(target, ast.Name) and _LIFETIME_CONSTANT_RE.search(target.id)
    ]


def test_every_discovered_fixture_lifetime_is_finite_and_in_budget() -> None:
    """A new fixture is in budget or it fails here. It cannot go unmentioned."""
    over_budget = [
        item
        for item in discover_fixture_lifetimes(TEST_TREE)
        if (item.path, item.source) not in LIFETIME_ALLOWLIST
        and (
            not math.isfinite(item.seconds)
            or not 0.0 < item.seconds <= LIFETIME_BUDGET_SECONDS
        )
    ]

    assert not over_budget, (
        f"fixture lifetimes outside the {LIFETIME_BUDGET_SECONDS}s budget:\n  "
        + "\n  ".join(str(item) for item in over_budget)
        + "\nShorten the fixture, or justify it in LIFETIME_ALLOWLIST."
    )


def test_the_scan_finds_the_fixtures_this_tree_is_known_to_have() -> None:
    """Anti-vacuum: a scan that silently matched nothing would pass forever."""
    discovered = discover_fixture_lifetimes(TEST_TREE)
    by_path: dict[str, set[float]] = {}
    for item in discovered:
        by_path.setdefault(item.path, set()).add(item.seconds)

    assert 300.0 in by_path["unit/lane_executor_contract.py"], (
        "the lane contract tree's declared lifetime went undiscovered"
    )
    condor = by_path["integration/test_condor_lane_executor.py"]
    assert 600.0 in condor, "the setsid escapee's lifetime went undiscovered"
    assert 240.0 in condor, "the owner-load spike's ceiling went undiscovered"
    # The lane job the first, hand-listed version of this test missed.
    assert any(
        item.source == "time.sleep(600)"
        and item.path == "integration/test_condor_lane_executor.py"
        for item in discovered
    ), "the sleep-600 lane job went undiscovered"
    assert len(discovered) >= 20, f"scan looks broken: only {len(discovered)} sites"


def test_documentation_is_not_a_fixture(tmp_path: Path) -> None:
    """Prose about a long sleep is not a long sleep.

    A module whose only content is a docstring mentioning ``time.sleep(7200)``
    used to fail the budget — teaching people that the way past this test is to
    stop writing things down.
    """
    (tmp_path / "doc_only.py").write_text(
        '"""Notes about fixture hygiene.\n\n'
        "We used to write time.sleep(7200) here, which was the whole problem.\n"
        '"""\n',
        encoding="utf-8",
    )
    (tmp_path / "nested_docs.py").write_text(
        "class Thing:\n"
        '    """A class docstring mentioning time.sleep(9000)."""\n\n'
        "    def method(self):\n"
        '        """A method docstring mentioning signal.alarm(9999)."""\n'
        "        return 1\n",
        encoding="utf-8",
    )
    # A docstring whose text is *itself* valid Python — a snippet with no
    # prose around it. Only the structural docstring rule can exclude this
    # one; the "must parse as code" rule happily accepts it.
    (tmp_path / "code_shaped_doc.py").write_text(
        '"""time.sleep(7200)"""\n\n\nclass Thing:\n'
        '    """signal.alarm(9000)"""\n',
        encoding="utf-8",
    )
    # Same number, in a string that is actually spawned.
    (tmp_path / "real_script.py").write_text(
        'SCRIPT = "import time\\ntime.sleep(7200)\\n"\n', encoding="utf-8"
    )

    discovered = discover_fixture_lifetimes(tmp_path)

    assert [(item.path, item.seconds) for item in discovered] == [
        ("real_script.py", 7200.0)
    ], "documentation was mistaken for a fixture, or the real script was missed"


def test_prose_that_quotes_a_call_is_not_a_fixture(tmp_path: Path) -> None:
    """Quotes in a sentence are punctuation, not an argument boundary.

    The earlier rule pulled every quoted run out of every string, so a note
    *about* a leaked burner was reported as a two-hour fixture. The quoted
    script is now only read out of a string that names an interpreter and
    hands it ``-c``.
    """
    (tmp_path / "prose.py").write_text(
        'NOTE = "the overnight run left \'time.sleep(7200)\' burning a core"\n'
        'BANNER = \'we saw "signal.alarm(9000)" in the sweep output\'\n',
        encoding="utf-8",
    )

    assert discover_fixture_lifetimes(tmp_path) == ()


def test_a_command_line_still_gives_up_its_script(tmp_path: Path) -> None:
    """Every spelling of "hand this interpreter a script" is the same fixture.

    An earlier rule required a recognisable interpreter beside the flag, which
    quietly lost ``python3 -uc``. A lifetime that hides behind a spelling is a
    lifetime with no ceiling, so the flag is what is matched.
    """
    (tmp_path / "spawner.py").write_text(
        "import sys\n"
        'PLACEHOLDER = f"{sys.executable} -c \'import time; time.sleep(11)\'"\n'
        'ESCAPED = f\'{sys.executable} -c "import time; time.sleep(12)"\'\n'
        "CLUSTERED = \"python3 -uc 'import time; time.sleep(22)'\"\n"
        "SEPARATE = \"python3.14 -u -c 'import time; time.sleep(33)'\"\n"
        "VIA_ENV = \"/usr/bin/env python -c 'import time; time.sleep(44)'\"\n"
        "VENV = \"/repo/.venv/bin/python -c 'import time; time.sleep(55)'\"\n"
        "ISOLATED = \"podman run img python -Ic 'import time; time.sleep(66)'\"\n",
        encoding="utf-8",
    )

    found = {item.seconds for item in discover_fixture_lifetimes(tmp_path)}

    assert found == {11.0, 12.0, 22.0, 33.0, 44.0, 55.0, 66.0}, (
        f"an invocation spelling hid a spawned script: {found}"
    )


# Every way of handing an interpreter a script that Python actually accepts.
# Written as command-line text, the way a fixture spells it; ``shlex.split``
# turns each into the argv a shell would produce, which is what makes the
# acceptance check below a check on Python rather than on this list.
ACCEPTED_SPELLINGS = (
    "python -c '{script}'",
    "python -c'{script}'",
    'python -c"{script}"',
    "python -uc '{script}'",
    "python -uc'{script}'",
    'python -Ic"{script}"',
    "python -u -c '{script}'",
    "python -X dev -c '{script}'",
    "python -OO -c '{script}'",
    "python3.14 -c '{script}'",
    "/usr/bin/env python -c '{script}'",
    "/repo/.venv/bin/python -c '{script}'",
    "python -c '{script}' /tmp/marker-after-the-script",
)

# Command strings Python rejects, pinned so the grammar is not widened to
# chase them. The unquoted attachment is here only as a COMMAND STRING, where
# the shell splits it apart; as a single argv element the same characters are
# a valid invocation, which is what ACCEPTED_ARGV_ELEMENTS covers.
REJECTED_COMMAND_STRINGS = (
    "python --command '{script}'",
    "python --c '{script}'",
    "python -c{script}",
)

# Spellings that arrive as ONE argv element, passed to the OS verbatim. The
# shell never sees them, so a script attached to the flag with no quotes is a
# real invocation here -- including as the last flag of a cluster.
ACCEPTED_ARGV_ELEMENTS = (
    "-c{script}",
    "-uc{script}",
    "-Ic{script}",
)

# Short, real, and instant: the interpreter check runs it for real.
PINNED_SCRIPT = "import time; time.sleep(0.01)"
PINNED_SECONDS = 0.01


class TestArgvElementsPythonAccepts:
    """The argv-element context: no shell, so the attachment is real.

    Pinned twice like the command strings — the real interpreter runs it, and
    the scan discovers it — because this is the class the scanner exists to
    catch: an invocation that works, spelled a way the rule did not read.
    """

    @pytest.mark.parametrize("element", ACCEPTED_ARGV_ELEMENTS)
    def test_the_interpreter_accepts_it(self, element: str) -> None:
        argv = [sys.executable, element.format(script=PINNED_SCRIPT)]

        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False
        )

        assert completed.returncode == 0, (
            f"{element!r} is not an invocation after all: "
            f"{completed.stderr[-200:]}"
        )

    @pytest.mark.parametrize("element", ACCEPTED_ARGV_ELEMENTS)
    def test_the_same_characters_in_a_command_string_are_not_a_fixture(
        self, element: str, tmp_path: Path
    ) -> None:
        """The two contexts, pinned on the side that must stay quiet.

        Mid-string the shell splits the text, so these characters cannot
        invoke anything —
        :meth:`TestSpellingsPythonAccepts.test_the_interpreter_rejects_these_command_strings`
        proves that with the real interpreter. Reporting it anyway would not
        be the accepted over-inclusion: over-inclusion buys a *possible*
        fixture at the cost of noise, and there is no possible fixture here.

        This is what the argv-element rule's anchor buys. Without it the rule
        fires mid-string too and this file reads as a two-hour fixture.
        """
        command = f"python {element.format(script='import time; time.sleep(7200)')}"
        (tmp_path / "note.py").write_text(
            f"COMMAND = {command!r}\n", encoding="utf-8"
        )

        assert discover_fixture_lifetimes(tmp_path) == (), (
            f"a command string that cannot spawn anything was reported: "
            f"{command!r}"
        )

    @pytest.mark.parametrize("element", ACCEPTED_ARGV_ELEMENTS)
    def test_the_scan_discovers_it(self, element: str, tmp_path: Path) -> None:
        """Spelled as a fixture would spell it: an argv list, not a string."""
        argument = element.format(script=PINNED_SCRIPT)
        (tmp_path / "spawner.py").write_text(
            f"import subprocess, sys\n"
            f"ARGV = [sys.executable, {argument!r}]\n"
            f"subprocess.run(ARGV, check=False)\n",
            encoding="utf-8",
        )

        found = {item.seconds for item in discover_fixture_lifetimes(tmp_path)}

        assert found == {PINNED_SECONDS}, (
            f"an accepted argv element hid its script: {element!r} -> {found}"
        )


def _runs_the_script(argv: list[str]) -> bool:
    """Whether this invocation actually EXECUTED the script.

    Exit status alone is not enough: `python -c"..."` as a single element
    exits 0 while merely evaluating a quoted string literal. A marker file is
    the difference between "accepted" and "ran".
    """
    marker = Path(tempfile.mkdtemp()) / "ran"
    probe = [
        item.replace(_MARKER_SCRIPT, f"open({str(marker)!r}, 'w').close()")
        for item in argv
    ]
    subprocess.run(probe, capture_output=True, timeout=60, check=False)
    return marker.exists()


_MARKER_SCRIPT = "<SCRIPT>"


class TestTheStructuralRule:
    """Argv in, scripts out — the shapes that beat five rounds of regex.

    Each case is pinned the same way: the real interpreter is asked whether
    the invocation runs, and the scan is asked whether it finds the lifetime.
    Anything that runs must be found; anything that cannot run need not be.
    """

    @pytest.mark.parametrize("flag", ["-c", "-uc", "-Ic"])
    def test_a_script_beginning_with_c_is_not_a_longer_cluster(
        self, flag: str, tmp_path: Path
    ) -> None:
        """The greediness case: `class` starts with the flag letter."""
        script = "class X: pass\nimport time; time.sleep(0.031)"

        assert _runs_the_script([sys.executable, flag + _MARKER_SCRIPT])
        (tmp_path / "f.py").write_text(
            "import sys\nARGV = [sys.executable, %r]\n" % (flag + script),
            encoding="utf-8",
        )
        assert {i.seconds for i in discover_fixture_lifetimes(tmp_path)} == {0.031}

    def test_a_script_beginning_with_a_dash(self, tmp_path: Path) -> None:
        """The script's own leading `-` is not another flag."""
        script = "-1;import time;time.sleep(0.032)"

        assert _runs_the_script([sys.executable, "-c" + _MARKER_SCRIPT])
        (tmp_path / "f.py").write_text(
            "import sys\nARGV = [sys.executable, %r]\n" % ("-c" + script),
            encoding="utf-8",
        )
        assert {i.seconds for i in discover_fixture_lifetimes(tmp_path)} == {0.032}

    def test_a_shell_quoted_whole_element(self, tmp_path: Path) -> None:
        """`shlex.join` quotes the element; `shlex.split` gives it back."""
        command = shlex.join([sys.executable, "-cimport time; time.sleep(0.033)"])

        assert _runs_the_script(shlex.split(shlex.join([sys.executable, "-c" + _MARKER_SCRIPT])))
        (tmp_path / "f.py").write_text(f"CMD = {command!r}\n", encoding="utf-8")
        assert {i.seconds for i in discover_fixture_lifetimes(tmp_path)} == {0.033}

    def test_the_flag_alone_takes_the_next_element(self, tmp_path: Path) -> None:
        command = shlex.join([sys.executable, "-c", "import time; time.sleep(0.034)"])

        (tmp_path / "f.py").write_text(f"CMD = {command!r}\n", encoding="utf-8")

        assert {i.seconds for i in discover_fixture_lifetimes(tmp_path)} == {0.034}

    def test_an_argv_element_bound_to_a_name(self, tmp_path: Path) -> None:
        """The element is often not written inside the list.

        ``FLAG_AND_SCRIPT = "-c..."`` then ``[sys.executable, FLAG_AND_SCRIPT]``
        puts the element in its own constant, with no siblings to read it
        against — so it is read as the whole argument it is.
        """
        script = "-cimport time; time.sleep(0.035)"
        assert _runs_the_script([sys.executable, "-c" + _MARKER_SCRIPT])

        (tmp_path / "f.py").write_text(
            "import subprocess, sys\n"
            f"FLAG_AND_SCRIPT = {script!r}\n"
            "subprocess.run([sys.executable, FLAG_AND_SCRIPT], check=False)\n",
            encoding="utf-8",
        )

        assert {i.seconds for i in discover_fixture_lifetimes(tmp_path)} == {0.035}

    def test_after_the_terminator_a_flag_is_a_filename(self, tmp_path: Path) -> None:
        """`--` ends the options, so nothing after it hands over a script.

        Verified: `python -- -cSCRIPT` exits 2 looking for a file of that
        name. Reporting it would not be over-inclusion — there is no fixture.
        """
        assert not _runs_the_script([sys.executable, "--", "-c" + _MARKER_SCRIPT])
        (tmp_path / "f.py").write_text(
            "import sys\nARGV = [sys.executable, '--', %r]\n"
            % "-cimport time; time.sleep(7200)",
            encoding="utf-8",
        )

        assert discover_fixture_lifetimes(tmp_path) == ()

    def test_a_command_string_no_lexer_can_read(self, tmp_path: Path) -> None:
        """Decision, with the reason: unparseable quoting is treated as prose.

        `shlex.split` raises "No closing quotation", and `sh -c` refuses the
        same string with "unexpected EOF". Nothing can say where the script
        ends because nothing closes it, and nothing can run it either — the
        round-7 category, where excluding costs no possible fixture. The
        naive split still keeps such a string in reach for the separated
        forms; only the multi-word unterminated script is out.
        """
        unterminated = "python -c 'import time; time.sleep(7200)"
        with pytest.raises(ValueError, match="closing quotation"):
            shlex.split(unterminated)
        shell = subprocess.run(
            ["/bin/sh", "-c", unterminated], capture_output=True, timeout=60,
            check=False,
        )
        assert shell.returncode != 0, "a shell would run it after all"

        (tmp_path / "f.py").write_text(
            f"CMD = {unterminated!r}\n", encoding="utf-8"
        )

        assert discover_fixture_lifetimes(tmp_path) == ()


def _argv_running_this_interpreter(spelling: str) -> list[str]:
    """The argv a shell would build, pointed at the interpreter running us.

    The interpreter token is the one substituted, which is argv[0] except for
    ``env``, where the interpreter is its first argument. Replacing argv[0]
    there would ask this Python to run a file called "python".
    """
    argv = shlex.split(spelling.format(script=PINNED_SCRIPT))
    target = 1 if Path(argv[0]).name == "env" else 0
    argv[target] = sys.executable
    return argv


class TestSpellingsPythonAccepts:
    """Pin the flag grammar to the interpreter, not to my reading of it.

    Each spelling is run by the real Python *and* fed to the scan. A spelling
    the interpreter accepts must be discovered; the discovery rule cannot
    quietly drift away from what actually spawns a process.
    """

    @pytest.mark.parametrize("spelling", ACCEPTED_SPELLINGS)
    def test_the_interpreter_accepts_it(self, spelling: str) -> None:
        argv = _argv_running_this_interpreter(spelling)

        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False
        )

        assert completed.returncode == 0, (
            f"{spelling!r} is not an invocation after all: {completed.stderr[-200:]}"
        )

    @pytest.mark.parametrize("spelling", ACCEPTED_SPELLINGS)
    def test_the_scan_discovers_it(self, spelling: str, tmp_path: Path) -> None:
        command = spelling.format(script=PINNED_SCRIPT)
        (tmp_path / "spawner.py").write_text(
            f"COMMAND = {command!r}\n", encoding="utf-8"
        )

        found = {item.seconds for item in discover_fixture_lifetimes(tmp_path)}

        assert found == {PINNED_SECONDS}, (
            f"a real invocation hid behind its spelling: {spelling!r} -> {found}"
        )

    @pytest.mark.parametrize("spelling", REJECTED_COMMAND_STRINGS)
    def test_the_interpreter_rejects_these_command_strings(
        self, spelling: str
    ) -> None:
        """Scoped to command strings, which is all this can prove.

        ``shlex.split`` models a shell, so this says what happens when the
        text is split before Python sees it — and nothing at all about the
        same characters arriving as one argv element. Reading it as the
        stronger claim is what left the attached form undiscovered; that case
        is now :meth:`TestArgvElementsPythonAccepts.test_the_interpreter_
        accepts_it`, and it passes.

        If Python ever grows a long option, this fails and the grammar gets
        revisited rather than silently missing it.
        """
        argv = _argv_running_this_interpreter(spelling)

        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False
        )

        assert completed.returncode != 0, f"{spelling!r} works and is not covered"


def test_a_flagged_command_in_prose_is_accepted_noise(tmp_path: Path) -> None:
    """The error model, pinned so it is not quietly re-traded.

    Prose that quotes a real ``python -c`` command IS flagged. That is the
    deliberate direction: a false positive is a line someone reads and
    allowlists, a false negative is an unbounded fixture nobody sees. The
    allowlist is the escape, and it is as legitimate for prose as for a
    fixture.
    """
    (tmp_path / "note.py").write_text(
        'HOW_WE_REPRODUCED = "we ran python -c \'import time; time.sleep(7200)\'"\n',
        encoding="utf-8",
    )

    flagged = discover_fixture_lifetimes(tmp_path)

    assert [item.seconds for item in flagged] == [7200.0]
    # ...and what an owner does about it is write the entry below, not a
    # tighter rule. This is the exact key LIFETIME_ALLOWLIST takes.
    assert [(item.path, item.source) for item in flagged] == [
        ("note.py", "time.sleep(7200)")
    ]


def test_the_scanner_recognises_every_shape_it_claims(tmp_path: Path) -> None:
    """Prove each pattern on synthetic source, including the decoys.

    ``signal.alarm`` has no literal-valued use in the tree today, so without
    this the pattern would be untested and free to rot.
    """
    (tmp_path / "sample_fixture.py").write_text(
        'SPAWN_LIFETIME_SECONDS = 12.5\n'
        'OTHER_TIMEOUT_SECONDS = 99999.0\n'
        'SCRIPT = "import time\\ntime.sleep(42)\\n"\n'
        'ALARMED = "import signal\\nsignal.alarm(7)\\n"\n'
        # An indented block waiting for textwrap.dedent, and a command line
        # with the script quoted inside it: both real shapes in this tree.
        # The interpreter is named the way the tree names it — the gate looks
        # for that, so a placeholder called anything else is not a command.
        'INDENTED = \'\'\'\n    import time\n    time.sleep(64)\n\'\'\'\n'
        'COMMAND = f"{sys.executable} -c \\"import time; time.sleep(81)\\""\n'
        'def harness():\n'
        '    import time\n'
        '    time.sleep(0.25)\n',
        encoding="utf-8",
    )

    found = {(item.source, item.seconds) for item in discover_fixture_lifetimes(tmp_path)}

    assert ("SPAWN_LIFETIME_SECONDS", 12.5) in found
    assert ("time.sleep(42)", 42.0) in found
    assert ("signal.alarm(7)", 7.0) in found
    assert ("time.sleep(64)", 64.0) in found, "an indented script block was missed"
    assert ("time.sleep(81)", 81.0) in found, "a quoted -c script was missed"
    assert not any(source == "OTHER_TIMEOUT_SECONDS" for source, _ in found), (
        "a plain timeout is not a fixture lifetime"
    )
    assert not any(seconds == 0.25 for _, seconds in found), (
        "a harness waiting on its own child is not a spawned fixture"
    )


def test_the_allowlist_carries_no_stale_entries() -> None:
    """An allowlist entry would outlive the fixture it excused, unsupervised."""
    live = {(item.path, item.source) for item in discover_fixture_lifetimes(TEST_TREE)}

    stale = LIFETIME_ALLOWLIST - live

    assert not stale, f"LIFETIME_ALLOWLIST excuses fixtures that no longer exist: {stale}"
