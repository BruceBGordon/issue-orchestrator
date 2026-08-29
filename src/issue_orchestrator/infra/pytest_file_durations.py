"""pytest plugin: teach the slicer what each test file costs.

Loaded explicitly (``-p issue_orchestrator.infra.pytest_file_durations``)
by the suite-slice lane, and by nothing else. That opt-in is the point:
an always-on plugin would also learn from a developer running a single
test out of a file and record that file as nearly free.

The capture point is inside pytest, which is deliberately *outside* any
execution backend's vocabulary — a scheduler backend re-invokes the
identical direct recipe inside its job, so the same durations are
captured, in the same way, in every mode. There is no regeneration step
and no human trigger: a green slice teaches, a red one does not.

Two rules keep a recorded weight honest:

- Only a zero exit status records. An aborted run (``-x``) stops early
  and would teach every file it never reached that it is free.
- Only files selected *whole* record. A node-level selection measures
  part of a file; storing that as the file's weight would make a fat
  file look thin and the run after would fatten it again.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..adapters.json_file_duration_history import FileDurationHistoryError
from ..ports.file_duration_history import FileDurationHistory
from .file_duration_store import open_file_duration_history

if TYPE_CHECKING:  # pragma: no cover - types only, pytest is not a runtime dep
    import pytest

PLUGIN_NAME = "issue-orchestrator-file-durations"
_NODE_SEPARATOR = "::"
_STORE_FAULT_EXIT_CODE = 70


def pytest_configure(config: "pytest.Config") -> None:
    """Register the recorder on the session that sees every report.

    Under xdist the controller receives its workers' reports and the
    workers each see only their own shard, so exactly one recorder —
    the controller's — holds the whole truth. A worker registering its
    own would persist a fraction of a file as the file's weight.
    """
    if hasattr(config, "workerinput"):
        return
    config.pluginmanager.register(
        FileDurationRecorder(
            whole_file_selections=whole_file_selections(config),
            history=open_file_duration_history(Path(config.rootpath)),
        ),
        PLUGIN_NAME,
    )


class FileDurationRecorder:
    """Total each wholly-run file's time; persist it if the run was green."""

    def __init__(
        self,
        *,
        whole_file_selections: frozenset[str],
        history: FileDurationHistory,
    ) -> None:
        self._history = history
        # Seeded at zero so a file whose every test was deselected by a
        # marker expression still records what it truly costs this lane
        # — nothing — instead of keeping the naive default forever.
        self._totals: dict[str, float] = {
            path: 0.0 for path in whole_file_selections
        }

    def pytest_runtest_logreport(self, report: "pytest.TestReport") -> None:
        path = report.nodeid.split(_NODE_SEPARATOR, 1)[0]
        if path not in self._totals:
            return
        # Every phase counts: setup and teardown are as much of the
        # slice's wall time as the call is.
        self._totals[path] += float(report.duration)

    def pytest_sessionfinish(
        self, session: "pytest.Session", exitstatus: int
    ) -> None:
        if int(exitstatus) != 0:
            return
        try:
            self._history.record_success(self._totals)
        except FileDurationHistoryError as error:
            # Loud, never silent: a store this run cannot write is a
            # store that has stopped teaching, and a learning loop that
            # fails quietly decays back into baked constants.
            print(f"[file-durations] {error}", file=sys.stderr)
            session.exitstatus = _STORE_FAULT_EXIT_CODE


def whole_file_selections(config: "pytest.Config") -> frozenset[str]:
    """The rootdir-relative files this run selected in their entirety.

    A file named by any node-level argument is excluded even if it was
    also named whole, because the run then covers it more than once and
    the total is not the file's cost.
    """
    partial: set[str] = set()
    whole: set[str] = set()
    for argument in config.args:
        head, separator, _ = str(argument).partition(_NODE_SEPARATOR)
        relative = _relative_to_rootdir(config, head)
        if relative is None:
            continue
        (partial if separator else whole).add(relative)
    return frozenset(whole - partial)


def _relative_to_rootdir(config: "pytest.Config", raw: str) -> str | None:
    """Normalize one selection to the form pytest node ids use."""
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(config.invocation_params.dir) / candidate
    if not candidate.is_file():
        # Directories and unmatched patterns are not whole-file facts.
        return None
    try:
        return candidate.resolve().relative_to(Path(config.rootpath).resolve()).as_posix()
    except ValueError:
        return None
