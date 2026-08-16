"""Tests for control-center runtime helpers (execution.control_center_runtime).

The headline invariant here is the **one-orchestrator-per-repo, many-repos**
guarantee: starting an orchestrator for repo B must never stop repo A's
orchestrator. ``control_start`` is the only start path that issues a
``stop_by_port(force=True)``, and every such stop is gated on whatever
``detect_orchestrator_by_port`` returns. So the eviction guard lives entirely in
``detect_orchestrator_by_port``: it must return ``None`` for any orchestrator
that does not belong to the repo being probed. If that ever regresses (e.g. the
``repo_root`` guard is dropped), starting one repo's orchestrator could evict
another's — exactly the kind of cross-repo kill we want a fast unit test to
catch, rather than discovering it from a dead orchestrator in production.

The route-layer tests in ``test_control_api_supervisor_routes.py`` mock
``detect_orchestrator_by_port``, so they do not exercise this repo-scoping — that
is what these tests cover.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.execution import control_center_runtime as ccr
from issue_orchestrator.ports.repository_engine_supervisor import (
    MultiInstanceStatus,
    SupervisorStatus,
)


def test_detect_ignores_orchestrator_belonging_to_a_different_repo(monkeypatch) -> None:
    """A live orchestrator on the probed port for a DIFFERENT repo is not detected.

    This is the cross-repo eviction guard: because ``control_start`` only stops
    what ``detect`` returns, returning ``None`` here means starting repo B can
    never target repo A's orchestrator.
    """
    monkeypatch.setattr(ccr, "_load_config_port", lambda repo, cfg, mode: 64999)
    monkeypatch.setattr(
        ccr, "_read_json", lambda url, **_: {"repo_root": "/some/OTHER/repo"}
    )

    result = ccr.detect_orchestrator_by_port(Path("/my/repo"), "test.yaml")

    assert result is None


def test_known_tracked_port_reports_a_different_repository_as_identity_drift(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ccr, "_read_json", lambda url, **_: {"repo_root": "/some/OTHER/repo"}
    )
    monkeypatch.setattr(ccr, "_annotate_orchestrator_health", lambda *_: None)

    result = ccr.inspect_orchestrator_at_port(Path("/my/repo"), 64999)

    assert result is not None
    assert result["identity_mismatch"]["repo_root"] == {
        "expected": "/my/repo",
        "observed": "/some/OTHER/repo",
    }


def test_detect_returns_none_when_nothing_answers_on_port(monkeypatch) -> None:
    monkeypatch.setattr(ccr, "_load_config_port", lambda repo, cfg, mode: 64999)
    monkeypatch.setattr(ccr, "_read_json", lambda url, **_: None)

    assert ccr.detect_orchestrator_by_port(Path("/my/repo"), "test.yaml") is None


def test_detect_returns_none_when_no_configured_port(monkeypatch) -> None:
    monkeypatch.setattr(ccr, "_load_config_port", lambda repo, cfg, mode: None)
    # _read_json must never be reached — if it is, this blows up loudly.
    monkeypatch.setattr(
        ccr,
        "_read_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not probe")),
    )

    assert ccr.detect_orchestrator_by_port(Path("/my/repo"), "test.yaml") is None


def test_detect_matches_orchestrator_for_the_same_repo(monkeypatch) -> None:
    """Same-repo orchestrator IS detected (so the guard is meaningful, not vacuous)."""
    monkeypatch.setattr(ccr, "_load_config_port", lambda repo, cfg, mode: 64999)
    monkeypatch.setattr(ccr, "_read_json", lambda url, **_: {"repo_root": "/my/repo"})
    # Avoid the real health probe (would hit the network).
    monkeypatch.setattr(
        ccr, "_annotate_orchestrator_health", lambda details, base_url: None
    )

    result = ccr.detect_orchestrator_by_port(Path("/my/repo"), "test.yaml")

    assert result is not None
    assert result["port"] == 64999


def test_load_config_for_selection_uses_the_selected_mode_directory(
    tmp_path: Path,
) -> None:
    default_config = tmp_path / ".issue-orchestrator/config/modes/default/main.yaml"
    codex_config = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    default_config.parent.mkdir(parents=True)
    codex_config.parent.mkdir(parents=True)
    default_config.write_text("ui:\n  web_port: 18080\nagents: {}\n")
    codex_config.write_text("ui:\n  web_port: 19090\nagents: {}\n")

    config = ccr.load_config_for_selection(
        tmp_path,
        RepositoryLaunchSelection.parse(mode="codex", config_name="main.yaml"),
    )

    assert config.web_port == 19090
    assert config.configuration_mode == "codex"
    assert config.config_name == "main.yaml"


def test_effective_selection_does_not_load_a_stopped_repositories_config(
    tmp_path: Path, monkeypatch
) -> None:
    selection = RepositoryLaunchSelection.parse(
        mode="codex",
        config_name="main.yaml",
    )
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[],
    )
    supervisor.status.return_value = SupervisorStatus(state="stopped")
    monkeypatch.setattr(ccr, "get_selected_launch_selection", lambda _: selection)
    monkeypatch.setattr(ccr, "detect_repository_orchestrators", lambda _: [])
    monkeypatch.setattr(
        ccr,
        "load_config_for_selection",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not load config")),
    )

    assert ccr.get_effective_launch_selection(tmp_path, supervisor) == selection
