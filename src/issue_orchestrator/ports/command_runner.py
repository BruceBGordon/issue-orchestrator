"""CommandRunner port for executing local shell commands.

Execution-only: control layer requests command execution; adapters implement it.
"""

import locale
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class OutputNewlines(str, Enum):
    r"""Newline policy applied to captured child-process output.

    ``TRANSLATED`` is Python's universal-newline behaviour: the child's
    ``\r\n`` and its bare ``\r`` both reach the caller as ``\n``. That is the
    right default for output read as human text or split with
    :meth:`str.splitlines`.

    ``PRESERVED`` decodes the captured bytes verbatim, so every terminator the
    child actually wrote survives the transport. Callers that must apply a
    *producer's* own physical-line rule need it: Git delimits patch records and
    blob lines with LF alone, and a bare ``\r`` inside one of those lines is
    ordinary content. Translating it splits one record into two, which silently
    detaches source text from the ``+`` marking it as an addition.

    The two modes differ in newline fidelity only -- ``PRESERVED`` decodes with
    the same encoding text mode would have used -- so a caller can opt in
    without inheriting any other transport difference.
    """

    TRANSLATED = "translated"
    PRESERVED = "preserved"

    @property
    def capture_bytes(self) -> bool:
        """Whether runners must capture raw bytes and decode via :meth:`decode`.

        Universal-newline translation happens in the reader that decodes the
        child's bytes, so it can only be avoided by capturing undecoded output.
        """

        return self is OutputNewlines.PRESERVED

    def decode(self, captured: str | bytes | None, *, errors: str = "strict") -> str:
        """Return one captured stream as text under this newline policy.

        Accepts either the ``str`` a text-mode capture already produced or the
        ``bytes`` a byte-exact capture yields, so a runner decodes both modes
        through one call. ``errors`` defaults to ``"strict"`` to keep undecodable
        output a loud command failure rather than a silently mangled string.
        """

        if captured is None:
            return ""
        if isinstance(captured, bytes):
            return captured.decode(locale.getpreferredencoding(False), errors=errors)
        return captured


@dataclass(frozen=True)
class CommandResult:
    """Result of a command execution."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandRunner(Protocol):
    """Protocol for running local commands."""

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
        newlines: OutputNewlines = OutputNewlines.TRANSLATED,
    ) -> CommandResult:
        """Run a command and return the result."""
        ...


class NullCommandRunner:
    """CommandRunner that always fails (for tests and defaults)."""

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
        newlines: OutputNewlines = OutputNewlines.TRANSLATED,
    ) -> CommandResult:
        return CommandResult(
            returncode=1,
            stdout="",
            stderr="NullCommandRunner: command execution not available",
            timed_out=False,
        )
