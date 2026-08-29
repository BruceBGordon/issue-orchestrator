"""Tests for scoped E2E stale orchestrator cleanup."""

from __future__ import annotations

import subprocess
from typing import Any

from tests.e2e._stale_orchestrator_cleanup import (
    is_e2e_orchestrator_start_command,
    kill_stale_e2e_orchestrators,
    stale_e2e_orchestrator_processes,
)


def test_e2e_orchestrator_start_command_is_selected() -> None:
    command = (
        "/Users/brucegordon/dev/issue-orchestrator-e2e-worktree/.venv/bin/python "
        "/Users/brucegordon/dev/issue-orchestrator-e2e-worktree/.venv/bin/issue-orchestrator "
        "--config /var/folders/sc/e2e-orchestrator-config-3255-s7a11xhp/"
        "issue-orchestrator.e2e.3255.orchestrator-a.4554401872.yaml "
        "start --label test-data --max-issues 5 --ui-mode web --port 60341 "
        "--api-port 60340 --label io-e2e-claim-test"
    )

    assert is_e2e_orchestrator_start_command(command)


def test_control_center_repo_engine_with_start_paused_is_not_selected() -> None:
    command = (
        "/opt/homebrew/Cellar/python@3.14/3.14.0_1/Frameworks/Python.framework/"
        "Versions/3.14/Resources/Python.app/Contents/MacOS/Python "
        "-m issue_orchestrator.entrypoints.run_orchestrator "
        "--repo-root /Users/brucegordon/dev/tixmeup --port 0 --no-browser "
        "--config /Users/brucegordon/dev/tixmeup/.issue-orchestrator/config/default.yaml "
        "--start-paused"
    )

    assert not is_e2e_orchestrator_start_command(command)


def test_normal_cli_start_with_repo_config_is_not_selected() -> None:
    command = (
        "/Users/brucegordon/dev/issue-orchestrator/.venv/bin/issue-orchestrator "
        "--config /Users/brucegordon/dev/issue-orchestrator/.issue-orchestrator/config/main.yaml "
        "start --ui-mode web"
    )

    assert not is_e2e_orchestrator_start_command(command)


def test_e2e_config_without_orchestrator_entrypoint_is_not_selected() -> None:
    command = (
        "/repo/.venv/bin/python /repo/scripts/not-the-orchestrator.py "
        "--config /tmp/e2e-orchestrator-config-3255-abcd/"
        "issue-orchestrator.e2e.3255.orchestrator-a.1.yaml start"
    )

    assert not is_e2e_orchestrator_start_command(command)


def test_stale_process_scan_only_returns_e2e_owned_orchestrators() -> None:
    ps_output = "\n".join(
        [
            (
                "101 /opt/python -m issue_orchestrator.entrypoints.run_orchestrator "
                "--config /Users/brucegordon/dev/tixmeup/.issue-orchestrator/config/default.yaml "
                "--start-paused"
            ),
            (
                "102 /repo/.venv/bin/issue-orchestrator "
                "--config /repo/.issue-orchestrator/config/main.yaml start"
            ),
            (
                "103 /repo/.venv/bin/python /repo/.venv/bin/issue-orchestrator "
                "--config /tmp/e2e-orchestrator-config-3255-abcd/"
                "issue-orchestrator.e2e.3255.orchestrator-a.1.yaml start"
            ),
        ]
    )

    processes = stale_e2e_orchestrator_processes(
        ps_output,
        current_pid=3255,
        pid_exists=lambda pid: False,
    )

    assert [process.pid for process in processes] == [103]


def test_cleanup_skips_e2e_orchestrators_owned_by_another_live_worker() -> None:
    ps_output = "\n".join(
        [
            (
                "201 /repo/.venv/bin/issue-orchestrator "
                "--config /tmp/e2e-orchestrator-config-3255-abcd/"
                "issue-orchestrator.e2e.3255.orchestrator-a.1.yaml start"
            ),
            (
                "202 /repo/.venv/bin/issue-orchestrator "
                "--config /tmp/e2e-orchestrator-config-9999-wxyz/"
                "issue-orchestrator.e2e.9999.orchestrator-a.1.yaml start"
            ),
        ]
    )

    processes = stale_e2e_orchestrator_processes(
        ps_output,
        current_pid=3255,
        pid_exists=lambda pid: pid == 9999,
    )

    assert [process.pid for process in processes] == [201]


def test_kill_cleanup_only_kills_selected_processes() -> None:
    calls: list[list[str]] = []
    ps_output = "\n".join(
        [
            (
                "301 /repo/.venv/bin/issue-orchestrator "
                "--config /tmp/e2e-orchestrator-config-3255-abcd/"
                "issue-orchestrator.e2e.3255.orchestrator-a.1.yaml start"
            ),
            (
                "302 /opt/python -m issue_orchestrator.entrypoints.run_orchestrator "
                "--config /Users/brucegordon/dev/tixmeup/.issue-orchestrator/config/default.yaml "
                "--start-paused"
            ),
        ]
    )

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["ps", "ax"]:
            return subprocess.CompletedProcess(args, 0, stdout=ps_output, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    killed = kill_stale_e2e_orchestrators(
        run=fake_run,
        current_pid=3255,
        pid_exists=lambda pid: False,
    )

    assert killed == 1
    assert calls == [
        ["ps", "ax", "-ww", "-o", "pid=,command="],
        ["kill", "301"],
    ]
