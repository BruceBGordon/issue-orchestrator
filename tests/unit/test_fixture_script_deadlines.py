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
"""

from __future__ import annotations

import ast
import math
import os
import re
import signal
import subprocess
import sys
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

# Justified exceptions, as ``(path relative to tests/, matched source)``. Empty,
# and worth keeping empty: an entry here is a fixture allowed to outlive the
# budget, which is the thing that cost nine hours.
LIFETIME_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()

# The shapes that give a spawned process its duration. Literal-valued only —
# a value is either statically knowable or it is not, and pretending otherwise
# would make this scan lie about its coverage.
#
# ``time.sleep``/``signal.alarm`` are matched only inside string constants,
# which is what makes them precise: Python source inside a string literal is
# there to be executed somewhere else. The same call in ordinary test code is
# an AST node, not a string, and is correctly ignored -- a harness waiting on
# its own child is not a fixture lifetime.
_SCRIPT_PATTERNS = (
    ("time.sleep", re.compile(r"\btime\.sleep\(\s*([0-9]+(?:\.[0-9]+)?)\s*\)")),
    ("signal.alarm", re.compile(r"\bsignal\.alarm\(\s*([0-9]+(?:\.[0-9]+)?)\s*\)")),
)
# The declared knobs that are threaded into a spawned script's argv.
_LIFETIME_CONSTANT_RE = re.compile(r"(LIFETIME|MAX)_SECONDS$")
# Cheap gate so the scan parses ~7% of the tree instead of all of it.
_SCAN_HINTS = ("time.sleep(", "signal.alarm(", "LIFETIME_SECONDS", "MAX_SECONDS")

# Known limits, stated rather than hidden. Neither is statically decidable:
#   * bare shell ``sleep N`` -- indistinguishable from a string *describing* a
#     command. This very file's sibling asserts on "/bin/sleep 3600" as parser
#     test data; scanning that shape would report it as an hour-long fixture.
#   * ``time.sleep(<expression>)`` -- the two in this tree take their value from
#     argv, and the constants that supply it are discovered by the rule above.


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
        for node in ast.walk(ast.parse(text, filename=str(path))):
            found.extend(_lifetimes_in_node(node, relative))
    return tuple(found)


def _lifetimes_in_node(node: ast.AST, relative: str) -> list[DiscoveredLifetime]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [
            DiscoveredLifetime(
                relative, node.lineno, match.group(0), float(match.group(1))
            )
            for _, pattern in _SCRIPT_PATTERNS
            for match in pattern.finditer(node.value)
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
        'def harness():\n'
        '    import time\n'
        '    time.sleep(0.25)\n',
        encoding="utf-8",
    )

    found = {(item.source, item.seconds) for item in discover_fixture_lifetimes(tmp_path)}

    assert ("SPAWN_LIFETIME_SECONDS", 12.5) in found
    assert ("time.sleep(42)", 42.0) in found
    assert ("signal.alarm(7)", 7.0) in found
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
