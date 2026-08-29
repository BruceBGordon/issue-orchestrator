"""The cooperative-yield plugin, tested through its PUBLIC lifecycle
only (B3/#7134 SLF001s): behavior is observed via the resolved
transport seam and the environment, never private helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.entrypoints import pytest_cooperative_yield as plugin_module


class _RecordingTransport:
    def __init__(self, ack: bool = True) -> None:
        self.ack = ack
        self.published: list[bool] = []

    def publish(self, safe: bool) -> bool:
        self.published.append(safe)
        return self.ack


def _run_inner_pytest(tmp_path: Path, test_body: str, name: str) -> int:
    # Unique module names: in-process pytest.main caches imported test
    # modules, so a shared name across tmp dirs is a collection error.
    (tmp_path / f"test_inner_{name}.py").write_text(test_body)
    return pytest.main(
        [
            str(tmp_path),
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "issue_orchestrator.entrypoints.pytest_cooperative_yield",
            "-o",
            "addopts=",
        ]
    )


def test_lifecycle_lowers_first_brackets_items_and_rests_at_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """configure forces an acknowledged False before anything (the
    stale-True predecessor fix), each item is bracketed unsafe/safe,
    and unconfigure lowers again so the rest state between processes
    is unfreezable (A3, #7134)."""
    transport = _RecordingTransport()
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(
        plugin_module, "resolve_lane_yield_transport", lambda: transport
    )
    exit_code = _run_inner_pytest(
        tmp_path,
        "def test_one():\n    pass\n\ndef test_two():\n    pass\n",
        "pair",
    )
    assert exit_code == 0
    assert transport.published == [False, False, True, False, True, False]


def test_a_failing_item_still_ends_lowered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _RecordingTransport()
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(
        plugin_module, "resolve_lane_yield_transport", lambda: transport
    )
    exit_code = _run_inner_pytest(
        tmp_path, "def test_boom():\n    raise RuntimeError('x')\n", "boom"
    )
    assert exit_code == 1
    assert transport.published == [False, False, True, False]


def test_an_unacknowledged_opening_lower_is_run_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A predecessor's stale True that cannot be lowered must abort
    the run visibly (A2, #7134) — never proceed exposed."""
    transport = _RecordingTransport(ack=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(
        plugin_module, "resolve_lane_yield_transport", lambda: transport
    )
    exit_code = _run_inner_pytest(
        tmp_path, "def test_never_runs():\n    pass\n", "fatal"
    )
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "LaneYieldError" in captured.out + captured.err
    assert transport.published == [False]


def test_xdist_workers_compose_inert_and_never_touch_the_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Workers are separate processes flipping ONE shared job
    attribute, so under xdist the plugin must say nothing at all —
    the submit-time False stands (never-frozen, fail-safe)."""
    transport = _RecordingTransport()
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    monkeypatch.setattr(
        plugin_module, "resolve_lane_yield_transport", lambda: transport
    )
    exit_code = _run_inner_pytest(
        tmp_path, "def test_one():\n    pass\n", "xdist"
    )
    assert exit_code == 0
    assert transport.published == []


def test_no_transport_resolves_to_inert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(
        plugin_module, "resolve_lane_yield_transport", lambda: None
    )
    exit_code = _run_inner_pytest(
        tmp_path, "def test_one():\n    pass\n", "inert"
    )
    assert exit_code == 0
