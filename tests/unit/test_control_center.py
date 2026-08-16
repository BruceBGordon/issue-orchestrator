"""Unit tests for control center entrypoint helpers."""

from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.entrypoints import control_center


def test_cleanup_stale_tray_helpers_kills_matching_url() -> None:
    """Cleanup should terminate tray helpers for the same dashboard URL."""
    with (
        patch(
            "issue_orchestrator.entrypoints.control_center.list_processes_matching",
            return_value=[
                (1234, "python -m issue_orchestrator.entrypoints.tray --dashboard-url http://127.0.0.1:19080/"),
                (5678, "python -m issue_orchestrator.entrypoints.tray --dashboard-url http://127.0.0.1:29080/"),
            ],
        ),
        patch("issue_orchestrator.entrypoints.control_center.os.getpid", return_value=9999),
        patch("issue_orchestrator.entrypoints.control_center.os.kill") as kill_mock,
    ):
        control_center._cleanup_stale_tray_helpers("http://127.0.0.1:19080/")  # noqa: SLF001

    kill_mock.assert_called_once()
    assert kill_mock.call_args.args[0] == 1234


def test_start_tray_icon_starts_helper_process_on_macos() -> None:
    """macOS uses tray helper subprocess to keep Cocoa loop isolated."""
    process = MagicMock(pid=1234)
    process.poll.return_value = None

    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "darwin"),
        patch("issue_orchestrator.entrypoints.control_center._cleanup_stale_tray_helpers"),
        patch(
            "issue_orchestrator.entrypoints.control_center.spawn_tray_helper",
            return_value=process,
        ) as spawn_mock,
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs["dashboard_url"] == "http://localhost:19080/"
    assert result is not None
    result.stop()
    process.stop.assert_called_once_with(graceful_timeout_seconds=2)


def test_start_tray_icon_returns_none_when_helper_exits_immediately() -> None:
    """macOS helper startup failure should degrade to no tray handle."""
    process = MagicMock(pid=1234, returncode=1)
    process.poll.return_value = 1

    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "darwin"),
        patch(
            "issue_orchestrator.entrypoints.control_center.spawn_tray_helper",
            return_value=process,
        ),
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    assert result is None


def test_start_tray_icon_returns_icon_when_available() -> None:
    """_start_tray_icon returns the tray icon when startup succeeds."""
    mock_icon = MagicMock()
    captured: dict[str, object] = {}

    def _fake_start_tray(*, dashboard_url: str, engine_status_fn):
        captured["dashboard_url"] = dashboard_url
        captured["engine_status"] = engine_status_fn()
        return mock_icon

    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "linux"),
        patch(
            "issue_orchestrator.execution.control_center_repo_status.build_repos_status",
            return_value=[{"name": "repo-a", "status": {"state": "running"}}],
        ),
        patch(
            "issue_orchestrator.entrypoints.tray.start_tray",
            side_effect=_fake_start_tray,
        ),
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    assert result is mock_icon
    assert captured["dashboard_url"] == "http://localhost:19080/"
    assert captured["engine_status"] == [("repo-a", "running")]


def test_start_tray_icon_returns_none_when_startup_fails() -> None:
    """_start_tray_icon degrades gracefully when tray startup fails."""
    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "linux"),
        patch(
            "issue_orchestrator.entrypoints.tray.start_tray",
            side_effect=RuntimeError("no tray backend"),
        ),
        patch(
            "issue_orchestrator.execution.control_center_repo_status.build_repos_status",
            return_value=[],
        ),
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    assert result is None


def _make_main_args(
    *,
    debug_http: bool,
    debug: bool = False,
    dev_no_auth: bool = False,
    host: str = "127.0.0.1",
) -> Namespace:
    return Namespace(
        port=19080,
        host=host,
        debug=debug,
        debug_http=debug_http,
        no_browser=True,
        no_tray=True,
        dev_no_auth=dev_no_auth,
    )


def test_main_dev_no_auth_clears_stale_auth_state(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dev-no-auth`` must clear preexisting auth state, not inherit it.

    Regression for #6017 re-review-3 P2 on #6035. Seed the module
    globals AND the inherited env vars with stale values first, then
    call ``main()`` with ``dev_no_auth=True`` and verify everything
    is zeroed. Earlier version just declined to re-configure; stale
    values stayed live and the middleware kept enforcing.
    """
    import logging as _logging

    from issue_orchestrator.entrypoints import control_api

    prev_admin = control_api.get_configured_api_token()
    prev_agent = control_api.get_configured_agent_callback_token()
    # Seed the stale state the reviewer flagged.
    control_api.configure_api_token(
        "stale-admin-token", agent_callback="stale-agent-token"
    )
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_TOKEN", "env-stale-admin")
    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", "env-stale-agent"
    )

    with (
        patch(
            "argparse.ArgumentParser.parse_args",
            return_value=_make_main_args(debug_http=False, dev_no_auth=True),
        ),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run"),
        patch("issue_orchestrator.entrypoints.control_center.write_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig"),
        patch("issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0),
        patch("issue_orchestrator.entrypoints.control_center.threading.Thread") as thread_cls,
        caplog.at_level(_logging.ERROR, logger="issue_orchestrator.entrypoints.control_center"),
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        try:
            result = control_center.main()
        finally:
            control_api.configure_api_token(prev_admin, agent_callback=prev_agent)

    assert result == 0
    assert control_api.get_configured_api_token() is None
    assert control_api.get_configured_agent_callback_token() is None
    assert os.environ.get("ISSUE_ORCHESTRATOR_API_TOKEN") is None
    assert os.environ.get("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN") is None
    assert any(
        "authentication is DISABLED" in record.getMessage()
        for record in caplog.records
    )


def test_main_dev_no_auth_refuses_non_loopback_host() -> None:
    """``--dev-no-auth`` must refuse any non-loopback ``--host`` value
    BEFORE any startup side effect runs.

    Regression for #6017 re-review-3 P2 + re-review-4 P2 on #6035.
    Combining ``--dev-no-auth`` with ``--host 0.0.0.0`` would expose
    an unauthenticated Control API to the network. The original fix
    refused the combination, but only after
    ``write_dashboard_pid`` / browser-open thread / reaper thread
    had already run — leaving a stale PID file and potentially a
    browser tab pointed at a server that never bound. Assert here
    that none of those side-effect functions are invoked in the
    refusal path.
    """
    from issue_orchestrator.entrypoints import control_api

    prev_admin = control_api.get_configured_api_token()
    prev_agent = control_api.get_configured_agent_callback_token()

    with (
        patch(
            "argparse.ArgumentParser.parse_args",
            return_value=_make_main_args(
                debug_http=False, dev_no_auth=True, host="0.0.0.0"
            ),
        ),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run") as run_mock,
        patch(
            "issue_orchestrator.entrypoints.control_center.write_dashboard_pid"
        ) as pid_mock,
        patch(
            "issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"
        ) as clear_mock,
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig") as basic_cfg_mock,
        patch("issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0) as cleanup_mock,
        patch(
            "issue_orchestrator.entrypoints.control_center.threading.Thread"
        ) as thread_cls,
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        try:
            result = control_center.main()
        finally:
            control_api.configure_api_token(prev_admin, agent_callback=prev_agent)

    assert result == 2
    # None of the startup side-effect operations should run when we
    # refuse the combination. A successful refusal leaves no PID
    # file, no browser tab, no background thread, and no registry
    # cleanup touching disk state.
    run_mock.assert_not_called()
    pid_mock.assert_not_called()
    clear_mock.assert_not_called()
    basic_cfg_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    thread_cls.assert_not_called()


class TestLoopbackHostHelper:
    """Pin what counts as loopback."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.99", "::1", "localhost"])
    def test_loopback(self, host: str) -> None:
        from issue_orchestrator.entrypoints.control_center import _is_loopback_host

        assert _is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host", ["0.0.0.0", "::", "192.168.1.5", "10.0.0.1", "example.com", "", "not-an-ip"]
    )
    def test_not_loopback(self, host: str) -> None:
        from issue_orchestrator.entrypoints.control_center import _is_loopback_host

        assert _is_loopback_host(host) is False


def test_main_disables_uvicorn_access_log_by_default() -> None:
    """main() starts uvicorn with access logs disabled by default."""
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_make_main_args(debug_http=False)),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run") as run_mock,
        patch("issue_orchestrator.entrypoints.control_center.write_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig"),
        patch("issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0),
        patch("issue_orchestrator.entrypoints.control_center.threading.Thread") as thread_cls,
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        result = control_center.main()

    assert result == 0
    assert run_mock.call_args.kwargs["access_log"] is False


def test_main_enables_uvicorn_access_log_with_debug_http_flag() -> None:
    """main() enables uvicorn access logs when --debug-http is set."""
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_make_main_args(debug_http=True)),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run") as run_mock,
        patch("issue_orchestrator.entrypoints.control_center.write_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig"),
        patch("issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0),
        patch("issue_orchestrator.entrypoints.control_center.threading.Thread") as thread_cls,
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        result = control_center.main()

    assert result == 0
    assert run_mock.call_args.kwargs["access_log"] is True


def test_main_debug_sets_default_engine_log_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control Center --debug opts repository engines into DEBUG logs by default."""
    from issue_orchestrator.infra.supervisor import ENGINE_LOG_LEVEL_ENV

    monkeypatch.delenv(ENGINE_LOG_LEVEL_ENV, raising=False)
    with (
        patch(
            "argparse.ArgumentParser.parse_args",
            return_value=_make_main_args(debug_http=False, debug=True),
        ),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run"),
        patch("issue_orchestrator.entrypoints.control_center.write_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig"),
        patch("issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0),
        patch("issue_orchestrator.entrypoints.control_center.threading.Thread") as thread_cls,
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        result = control_center.main()

    assert result == 0
    assert os.environ[ENGINE_LOG_LEVEL_ENV] == "DEBUG"


def test_main_debug_preserves_explicit_engine_log_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit engine log-level env remains the source of truth."""
    from issue_orchestrator.infra.supervisor import ENGINE_LOG_LEVEL_ENV

    monkeypatch.setenv(ENGINE_LOG_LEVEL_ENV, "WARNING")
    with (
        patch(
            "argparse.ArgumentParser.parse_args",
            return_value=_make_main_args(debug_http=False, debug=True),
        ),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run"),
        patch("issue_orchestrator.entrypoints.control_center.write_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig"),
        patch("issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0),
        patch("issue_orchestrator.entrypoints.control_center.threading.Thread") as thread_cls,
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        result = control_center.main()

    assert result == 0
    assert os.environ[ENGINE_LOG_LEVEL_ENV] == "WARNING"


def test_start_buttons_are_disabled_while_start_is_pending() -> None:
    """Control Center tracks in-flight starts and disables start buttons."""
    source_root = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
    template = (source_root / "templates" / "control_center.html").read_text(encoding="utf-8")
    script = (source_root / "static" / "js" / "control_center.js").read_text(encoding="utf-8")

    # Static script URLs carry a ``?v={{ static_version }}`` cache-buster
    # placeholder; the route handler substitutes the token at render time.
    # Test against the template's pre-render shape.
    assert '<script src="/static/js/browser_auth.js?v={{ static_version }}"></script>' in template
    assert '<script src="/static/js/control_center.js?v={{ static_version }}"></script>' in template
    assert "document.getElementById('sidebarCloseCC').addEventListener('click'" in script
    assert "menuCloseCC" not in script
    assert "const pendingRepoStarts = new Set();" in script
    assert "const REPO_START_POLL_INTERVAL_MS = 250;" in script
    assert "const REPO_START_TIMEOUT_MS = 15000;" in script
    assert "function getSelectedRepoConfig(repo, preferredConfig = null, mode = null)" in script
    assert (
        "function requiresExplicitRepoConfigSelection(repo, selectedConfig, mode = null)"
        in script
    )
    assert "if (pendingRepoStarts.has(path))" in script
    assert "pendingRepoStarts.add(path);" in script
    assert "pendingRepoStarts.delete(path);" in script
    assert "async function waitForRepoToBeReady(path)" in script
    assert "await waitForRepoToBeReady(path);" in script
    assert "config = getValidRepoConfig(repo, config, mode);" in script
    assert "throw new Error('Select a valid config before starting this repository engine');" in script
    assert (
        "function showDoctorResultsModal(title, data, prefixMessage = null, "
        "prefixClass = 'info', context = {})" in script
    )
    assert 'id="repairGuardrailsBtn"' in template
    assert "function hasRepairableRepoGuardrails(data)" in script
    assert "function repairGuardrails()" in script
    assert "const confirmed = window.confirm(" in script
    assert "This will overwrite issue-orchestrator managed hook files if they drifted." in script
    assert "if (!confirmed) {" in script
    assert "/control/orchestrator/guardrails/repair" in script
    assert "Repair Guardrails" in template
    assert "{ repoRoot: path, configName: config, mode }" in script
    assert "Guardrails repaired (${fileCount} file${fileCount === 1 ? '' : 's'} written)" in script
    assert "if (!data) {" in script
    assert "if (error.error === 'doctor_failed' && error.doctor)" in script
    assert "Start Blocked — ${repoLabel}" in script
    assert "See Doctor Results." in script
    assert "null," in script
    assert "Choose config..." in script
    assert "Saved config is unavailable:" in script
    assert "Select a valid config before starting this repository engine." in script
    assert "Starting..." in script
    assert "Starting repository engine..." in script
    assert "Waiting for engine to become ready." in script
    assert 'id="repoStopModal"' in template
    assert 'id="confirmRepoStop"' in template
    assert 'id="repoStopModeGraceful"' in template
    assert 'id="repoStopModeForce"' in template
    assert 'id="shutdownModeGraceful"' in template
    assert 'id="shutdownModeForce"' in template
    assert "Mode: immediate force" in script
    assert "Mode: graceful (~${formatGracefulTimeout(op.graceful_timeout_seconds || 120)}), then force if needed" in script
    assert "State: control center unavailable" in script
    assert "Control Center is closing..." in script
    assert "showControlCenterClosedFallback" in script
    assert "attemptControlCenterTabClose" in script
    assert 'data-action="stop-force"' not in template
    assert 'data-action="stop-force"' not in script
    assert "getRepoDashboardUrl(repo, { embedded: true, theme: iframeTheme })" in script
    assert "buildDashboardUrlFromBase(repo.dashboard_url, options)" in script
    # isRepoDashboardReady = engine process alive and serving (used by
    # the start-RPC's wait loop). isRepoFullyReady additionally gates on
    # repo.status.startup_status === "complete" — the CC backend probes
    # each engine's /api/status and stamps that field on the snapshot.
    assert "Boolean(repo.dashboard_url)" in script
    assert "function isRepoFullyReady" in script
    assert "repo.status?.startup_status === 'complete'" in script
    assert "http://127.0.0.1:${port}" not in template


def test_issue_audit_errors_surface_upstream_status() -> None:
    """Control Center issue/audit loads should show GitHub status details."""
    source_root = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
    script = (source_root / "static" / "js" / "control_center.js").read_text(encoding="utf-8")

    assert "function formatIssueAuditError(response, data)" in script
    assert "data?.upstream_status_code" in script
    assert "`GitHub HTTP ${data.upstream_status_code}`" in script
    assert "if (!response.ok || data.error)" in script


def test_control_center_uses_packaged_brand_assets() -> None:
    """Header logo and favicon should be package-static assets."""
    source_root = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
    template = (source_root / "templates" / "control_center.html").read_text(encoding="utf-8")

    assert '<link rel="icon" type="image/svg+xml" href="/static/brand/logo.svg">' in template
    assert '<img class="sidebar-logo-icon" src="/static/brand/logo.svg"' in template
    assert "/favicon.ico" not in template
