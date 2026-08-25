"""Typed DSL for real TERM-resistant process-tree fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TermResistantChildProgram:
    """Child program that acknowledges only after its TERM policy is active."""

    lifetime_seconds: int

    def __post_init__(self) -> None:
        if type(self.lifetime_seconds) is not int or self.lifetime_seconds <= 0:
            raise ValueError(
                "TermResistantChildProgram.lifetime_seconds must be positive"
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
