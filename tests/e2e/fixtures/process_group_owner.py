"""Process-group ownership for Repository Engines launched by E2E tests."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time

from issue_orchestrator.infra.process_table import ps_command, ps_env

logger = logging.getLogger(__name__)

OWNED_GROUP_STOP_TIMEOUT_SECONDS = 5
ProcessGroupSnapshot = frozenset[int]


class ProcessGroupOwner:
    """Capture and reap every process group launched below one engine PID."""

    def __init__(self, root_pid: int, *, protected_pgid: int | None = None) -> None:
        self._root_pid = root_pid
        self._protected_pgid = (
            os.getpgrp() if protected_pgid is None else protected_pgid
        )

    def snapshot(self) -> ProcessGroupSnapshot:
        """Capture groups before the root dies and descendants are reparented."""
        try:
            result = subprocess.run(
                ps_command("-ax", "-o", "pid=,ppid=,pgid="),
                capture_output=True,
                text=True,
                check=True,
                env=ps_env(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                f"Could not inspect E2E Repository Engine descendants: {exc}"
            ) from exc

        children: dict[int, set[int]] = {}
        pgid_by_pid: dict[int, int] = {}
        for line in result.stdout.splitlines():
            try:
                pid, parent_pid, pgid = (int(field) for field in line.split())
            except ValueError:
                continue
            children.setdefault(parent_pid, set()).add(pid)
            pgid_by_pid[pid] = pgid

        groups: set[int] = set()
        pending = [self._root_pid]
        visited: set[int] = set()
        while pending:
            pid = pending.pop()
            if pid in visited:
                continue
            visited.add(pid)
            if pid in pgid_by_pid:
                groups.add(pgid_by_pid[pid])
            pending.extend(children.get(pid, set()))

        snapshot = frozenset(groups)
        self._reject_protected_group(snapshot)
        logger.info(
            "[E2E PROCESS OWNER] root_pid=%d logical_process_groups=%s",
            self._root_pid,
            sorted(snapshot),
        )
        return snapshot

    def signal(
        self,
        snapshot: ProcessGroupSnapshot,
        signum: signal.Signals,
    ) -> None:
        """Signal each captured logical process group once."""
        self._reject_protected_group(snapshot)
        for pgid in sorted(snapshot):
            self._signal_owned_group(pgid, signum)

    def terminate_survivors(self, snapshot: ProcessGroupSnapshot) -> None:
        """Terminate agent groups that survived the engine's own cleanup."""
        survivors = self._living_groups(snapshot)
        if not survivors:
            return
        logger.warning(
            "[E2E PROCESS OWNER] engine exited with surviving process groups; "
            "terminating pgids=%s",
            sorted(survivors),
        )
        self.signal(survivors, signal.SIGTERM)
        deadline = time.monotonic() + OWNED_GROUP_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            survivors = self._living_groups(survivors)
            if not survivors:
                return
            time.sleep(0.05)
        self.signal(survivors, signal.SIGKILL)

    def _reject_protected_group(self, groups: ProcessGroupSnapshot) -> None:
        if self._protected_pgid in groups:
            raise RuntimeError(
                "E2E Repository Engine escaped process-group isolation: "
                f"root_pid={self._root_pid} pgid={self._protected_pgid}"
            )

    @classmethod
    def _living_groups(cls, groups: ProcessGroupSnapshot) -> ProcessGroupSnapshot:
        return frozenset(
            pgid for pgid in groups if cls._signal_owned_group(pgid, 0)
        )

    @staticmethod
    def _signal_owned_group(pgid: int, signum: int) -> bool:
        """Signal one captured group; report whether it is still ours to reap.

        A captured group can stop being ours between snapshot and teardown:
        once its leader dies the PID/PGID may be recycled by an unrelated
        process. ``ProcessLookupError`` (the group is empty) and
        ``PermissionError`` (the PGID now leads a process we do not own) both
        mean the group we captured is gone, so teardown treats them alike
        instead of crashing on a benign PID-reuse race under heavy load.
        """
        try:
            os.killpg(pgid, signum)
        except (ProcessLookupError, PermissionError):
            return False
        return True
