"""The cooperative-yield plugin: unsafe during items, safe between them,
inert wherever advertising would be wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.entrypoints import pytest_cooperative_yield as plugin_module


class _RecordingSignal:
    def __init__(self) -> None:
        self.advertised: list[bool] = []

    def advertise(self, safe: bool) -> None:
        self.advertised.append(safe)


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


def test_unsafe_during_items_safe_between_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two passing items produce False/True around each, plus the
    final safe advertisement at unconfigure — the submit description's
    initial False covers everything before the first boundary."""
    signal = _RecordingSignal()
    monkeypatch.setattr(plugin_module, "_build_signal", lambda: signal)
    exit_code = _run_inner_pytest(
        tmp_path,
        "def test_one():\n    pass\n\ndef test_two():\n    pass\n",
        "pair",
    )
    assert exit_code == 0
    assert signal.advertised == [False, True, False, True, True]


def test_a_failing_item_still_ends_at_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The finally-shaped hookwrapper: an item that raises still flips
    the lane back to safe — a crashed test must not leave the lane
    permanently unfreezable OR frozen mid-cleanup."""
    signal = _RecordingSignal()
    monkeypatch.setattr(plugin_module, "_build_signal", lambda: signal)
    exit_code = _run_inner_pytest(
        tmp_path, "def test_boom():\n    raise RuntimeError('x')\n", "boom"
    )
    assert exit_code == 1
    assert signal.advertised == [False, True, True]


def test_none_signal_means_total_silence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(plugin_module, "_build_signal", lambda: None)
    exit_code = _run_inner_pytest(
        tmp_path, "def test_one():\n    pass\n", "silent"
    )
    assert exit_code == 0


def test_xdist_workers_stay_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workers are separate processes flipping ONE shared job
    attribute: 'between items' in one worker is mid-item in the
    others, so under xdist the plugin must say nothing at all —
    degrading the lane to never-frozen, the fail-safe direction."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    assert plugin_module._build_signal() is None


def test_outside_xdist_resolution_is_delegated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    sentinel = _RecordingSignal()
    monkeypatch.setattr(
        plugin_module, "resolve_lane_yield_signal", lambda: sentinel
    )
    assert plugin_module._build_signal() is sentinel
