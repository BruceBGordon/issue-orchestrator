"""Regression tests for evidence-preserving live sandbox retries."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.integration import test_sandbox_os_boundary as sandbox_boundary


def test_retry_snapshots_first_attempt_breach_before_later_overwrite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    network_status = tmp_path / "network-status.txt"
    completed = tmp_path / "completed.txt"
    attempts = 0

    def fake_run(
        _cmd: list[str],
        *,
        cwd: Path,
        timeout: int,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, extra_env
        nonlocal attempts
        attempts += 1
        network_status.write_text(
            "OPENED" if attempts == 1 else "CLOSED",
            encoding="utf-8",
        )
        if attempts == 2:
            completed.write_text("done", encoding="utf-8")
        return subprocess.CompletedProcess([], 0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox_boundary, "_run", fake_run)

    _, _, snapshots = sandbox_boundary.run_until_paths_created(
        ["sandbox-probe"],
        cwd=tmp_path,
        timeout=1,
        expected_paths=(completed,),
        observed_paths=(network_status,),
    )

    assert [snapshot[network_status] for snapshot in snapshots] == [
        b"OPENED",
        b"CLOSED",
    ]
