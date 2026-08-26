"""Typed DSL for real TERM-resistant process-tree fixtures."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


PROCESS_CONTAINMENT_WATCHDOG_SECONDS = 30.0
_CONTAINMENT_POLL_SECONDS = 0.05
_PROCESS_STATE_PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class TermResistantChildProgram:
    """Child program that acknowledges only after its TERM policy is active."""

    lifetime_seconds: int

    def __post_init__(self) -> None:
        if (
            type(self.lifetime_seconds) is not int
            or self.lifetime_seconds <= PROCESS_CONTAINMENT_WATCHDOG_SECONDS
        ):
            raise ValueError(
                "TermResistantChildProgram.lifetime_seconds must exceed the "
                "containment watchdog"
            )

    def python_source(self) -> str:
        """Return a child program whose stdout line is a readiness handshake."""
        return (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})\n"
            "print(os.getpid(), flush=True)\n"
            f"time.sleep({self.lifetime_seconds})\n"
        )


@dataclass(frozen=True, slots=True)
class ProcessTreeMember:
    """Portable containment observation for one real fixture process."""

    process_id: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError("ProcessTreeMember.process_id must be an integer above 1")

    def is_executable(self) -> bool:
        """Return false for an absent process or a non-executable zombie."""
        return self._is_executable(_PROCESS_STATE_PROBE_TIMEOUT_SECONDS)

    def _is_executable(self, probe_timeout_seconds: float) -> bool:
        try:
            os.kill(self.process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise AssertionError(
                f"cannot observe fixture process {self.process_id}"
            ) from error
        try:
            observation = subprocess.run(
                ("ps", "-o", "stat=", "-p", str(self.process_id)),
                check=False,
                capture_output=True,
                text=True,
                timeout=probe_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                f"ps did not observe fixture process {self.process_id} within "
                f"{probe_timeout_seconds:.3f} seconds"
            ) from error
        status = observation.stdout.strip()
        if observation.returncode == 0 and status:
            return not status.startswith("Z")
        try:
            os.kill(self.process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise AssertionError(
                f"cannot observe fixture process {self.process_id}"
            ) from error
        if observation.returncode == 0:
            raise AssertionError(
                f"ps returned no state for fixture process {self.process_id}"
            )
        raise AssertionError(
            f"ps failed for fixture process {self.process_id}: "
            f"returncode={observation.returncode} stderr={observation.stderr!r}"
        )

    def assert_contained(self) -> None:
        """Wait until this member is absent or unable to execute user code."""
        watchdog_deadline = time.monotonic() + PROCESS_CONTAINMENT_WATCHDOG_SECONDS
        while True:
            remaining_seconds = watchdog_deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise AssertionError(
                    f"fixture process {self.process_id} remained executable "
                    f"for {PROCESS_CONTAINMENT_WATCHDOG_SECONDS:.0f} seconds"
                )
            if not self._is_executable(
                min(_PROCESS_STATE_PROBE_TIMEOUT_SECONDS, remaining_seconds)
            ):
                return
            time.sleep(min(_CONTAINMENT_POLL_SECONDS, remaining_seconds))


@dataclass(frozen=True, slots=True)
class DirectChildProcessCohort:
    """Exact direct children matching one module-owned process role."""

    parent_process_id: int
    module_name: str
    process_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        ProcessTreeMember(self.parent_process_id)
        if (
            type(self.module_name) is not str
            or not self.module_name
            or any(character.isspace() for character in self.module_name)
        ):
            raise ValueError(
                "DirectChildProcessCohort.module_name must be a non-empty token"
            )
        if type(self.process_ids) is not tuple or not self.process_ids:
            raise ValueError(
                "DirectChildProcessCohort.process_ids must not be empty"
            )
        for process_id in self.process_ids:
            ProcessTreeMember(process_id)

    @classmethod
    def observe_exact(
        cls,
        *,
        parent_process_id: int,
        module_name: str,
        expected_count: int,
    ) -> DirectChildProcessCohort:
        """Resolve an exact child cohort after its parent readiness handshake."""
        ProcessTreeMember(parent_process_id)
        if type(expected_count) is not int or expected_count < 1:
            raise ValueError("expected child process count must be positive")
        if (
            type(module_name) is not str
            or not module_name
            or any(character.isspace() for character in module_name)
        ):
            raise ValueError("child process module name must be a non-empty token")
        try:
            observation = subprocess.run(
                ("ps", "-axo", "pid=,ppid=,command="),
                check=False,
                capture_output=True,
                text=True,
                timeout=PROCESS_CONTAINMENT_WATCHDOG_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                "process-child observation exceeded the containment watchdog: "
                f"parent={parent_process_id} module={module_name!r}"
            ) from error
        if observation.returncode != 0:
            raise AssertionError(
                "could not observe direct process children: "
                f"returncode={observation.returncode} "
                f"stderr={observation.stderr!r}"
            )
        process_ids: list[int] = []
        for line in observation.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if len(fields) != 3:
                continue
            process_id_text, parent_id_text, command = fields
            try:
                process_id = int(process_id_text)
                observed_parent_id = int(parent_id_text)
            except ValueError:
                continue
            if observed_parent_id == parent_process_id and module_name in command:
                process_ids.append(process_id)
        if len(process_ids) != expected_count:
            raise AssertionError(
                "unexpected direct process-child cohort: "
                f"parent={parent_process_id} module={module_name!r} "
                f"expected={expected_count} observed={process_ids}"
            )
        return cls(parent_process_id, module_name, tuple(process_ids))

    def crash_one(self) -> None:
        """Hard-crash one cohort member without affecting its peers."""
        os.kill(self.process_ids[0], signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class CooperativeTermResistantProcessTreeProgram:
    """Leader that publishes readiness after both TERM policies are active."""

    descendant_pid_path: Path
    descendant_lifetime_seconds: int
    readiness_lines: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_absolute_pid_path(self.descendant_pid_path)
        TermResistantChildProgram(self.descendant_lifetime_seconds)
        _require_readiness_lines(self.readiness_lines)

    def python_source(self) -> str:
        """Return a cooperative leader with a fully initialized child tree."""
        readiness = "".join(
            f"print({line!r}, flush=True)\n" for line in self.readiness_lines
        )
        return (
            f"{_ready_descendant_source(self.descendant_lifetime_seconds)}"
            "signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))\n"
            f"{_publish_descendant_source(self.descendant_pid_path)}"
            f"{readiness}"
            "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})\n"
            "signal.pause()\n"
        )


@dataclass(frozen=True, slots=True)
class ParentCrashProcessTreeProgram:
    """TERM-resistant tree that publishes both leader and descendant identity."""

    leader_pid_path: Path
    descendant_pid_path: Path
    descendant_lifetime_seconds: int

    def __post_init__(self) -> None:
        _require_absolute_pid_path(self.leader_pid_path)
        _require_absolute_pid_path(self.descendant_pid_path)
        if self.leader_pid_path == self.descendant_pid_path:
            raise ValueError("leader and descendant identity paths must be distinct")
        TermResistantChildProgram(self.descendant_lifetime_seconds)

    def python_source(self) -> str:
        """Return a fully ready tree whose leader exits cooperatively on TERM."""
        return (
            "import os\n"
            f"{_ready_descendant_source(self.descendant_lifetime_seconds)}"
            "signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))\n"
            f"{_publish_descendant_source(self.descendant_pid_path)}"
            f"pathlib.Path({str(self.leader_pid_path)!r}).write_text(\n"
            "    str(os.getpid()), encoding='utf-8'\n"
            ")\n"
            "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})\n"
            "signal.pause()\n"
        )


@dataclass(frozen=True, slots=True)
class ExitingTermResistantProcessTreeProgram:
    """Leader that exits only after its resistant descendant is ready."""

    descendant_pid_path: Path
    descendant_lifetime_seconds: int
    leader_exit_code: int

    def __post_init__(self) -> None:
        _require_absolute_pid_path(self.descendant_pid_path)
        TermResistantChildProgram(self.descendant_lifetime_seconds)
        if (
            type(self.leader_exit_code) is not int
            or not 0 <= self.leader_exit_code <= 255
        ):
            raise ValueError(
                "ExitingTermResistantProcessTreeProgram.leader_exit_code must "
                "be in [0, 255]"
            )

    def python_source(self) -> str:
        """Return an exiting leader with a fully initialized child tree."""
        return (
            f"{_ready_descendant_source(self.descendant_lifetime_seconds)}"
            f"{_publish_descendant_source(self.descendant_pid_path)}"
            f"raise SystemExit({self.leader_exit_code})\n"
        )


def _ready_descendant_source(lifetime_seconds: int) -> str:
    child_source = TermResistantChildProgram(lifetime_seconds).python_source()
    return (
        "import pathlib, signal, subprocess, sys\n"
        "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})\n"
        "descendant = subprocess.Popen(\n"
        f"    [sys.executable, '-c', {child_source!r}],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.PIPE,\n"
        "    stderr=subprocess.DEVNULL,\n"
        "    text=True,\n"
        ")\n"
        "if descendant.stdout is None:\n"
        "    raise RuntimeError('descendant readiness pipe was not created')\n"
        "reported_pid = int(descendant.stdout.readline())\n"
        "if reported_pid != descendant.pid:\n"
        "    raise RuntimeError('descendant readiness identity mismatch')\n"
    )


def _publish_descendant_source(descendant_pid_path: Path) -> str:
    _require_absolute_pid_path(descendant_pid_path)
    return (
        f"pathlib.Path({str(descendant_pid_path)!r}).write_text(\n"
        "    str(reported_pid), encoding='utf-8'\n"
        ")\n"
    )


def _require_absolute_pid_path(pid_path: Path) -> None:
    if not isinstance(pid_path, Path) or not pid_path.is_absolute():
        raise ValueError("process-tree descendant_pid_path must be an absolute Path")


def _require_readiness_lines(readiness_lines: tuple[str, ...]) -> None:
    if type(readiness_lines) is not tuple:
        raise ValueError("process-tree readiness_lines must be a tuple")
    for line in readiness_lines:
        if type(line) is not str or not line or "\n" in line or "\r" in line:
            raise ValueError(
                "process-tree readiness lines must be non-empty single lines"
            )
