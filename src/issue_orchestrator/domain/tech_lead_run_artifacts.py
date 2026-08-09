"""What a finished tech-lead run leaves behind to be INSPECTED (#6858 F4).

ADR-0033 promises the session log, the evidence map, the decision and the
proposals on the winning engine's dashboard. Nothing kept that promise: a
failure investigation writes every one of those files inside a *disposable*
scratch worktree (#6823), and normal completion always removes that worktree —
so a record that only remembered ``(run_id, session_name)`` pointed at a
directory that no longer existed by the time an operator clicked.

This module is the vocabulary of the fix. A run's artifacts are a LOCATION
(engine-owned, outside every worktree) plus the set of inspectable KINDS found
there, and the kind→member map below is the one place the layout is written
down: the archive that copies the files and the reader that serves them resolve
through the same table, so a renamed artifact cannot leave one half of the
drill-down silently pointing at nothing.

Kinds are what an OPERATOR asks for, not what is on disk. ``session_replay`` is
the terminal recording because "watch what it did" is the question; the board
snapshot and evidence map are preserved alongside (they are the run's evidence)
but are not separate buttons — they are read through the report that cites them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from .tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
)

# The run-relative directory the tech-lead agent writes its artifacts into.
TECH_LEAD_DATA_DIRNAME = "tech-lead-data"
# The canonical raw terminal capture, at the run directory's root.
TERMINAL_RECORDING_FILENAME = "terminal-recording.jsonl"


class TechLeadRunArtifactKind(str, Enum):
    """An inspectable thing a recorded run can be drilled into.

    Deliberately operator-facing rather than file-facing: a kind is a question
    ("what did it do?", "what did it conclude?"), and the member map below is
    the only place that question turns into a filename.
    """

    SESSION_REPLAY = "session_replay"
    REPORT = "report"
    DECISION = "decision"

    @property
    def member(self) -> PurePosixPath:
        """Where this kind lives, relative to a run directory."""
        return _MEMBER_BY_KIND[self]


_MEMBER_BY_KIND: dict[TechLeadRunArtifactKind, PurePosixPath] = {
    TechLeadRunArtifactKind.SESSION_REPLAY: PurePosixPath(
        TERMINAL_RECORDING_FILENAME
    ),
    TechLeadRunArtifactKind.REPORT: PurePosixPath(
        TECH_LEAD_DATA_DIRNAME, TECH_LEAD_REPORT_FILENAME
    ),
    TechLeadRunArtifactKind.DECISION: PurePosixPath(
        TECH_LEAD_DATA_DIRNAME, TECH_LEAD_DECISION_FILENAME
    ),
}

# The order kinds are offered in: watch it, read the verdict, then the machine
# data. One order, so every surface lists them the same way.
ARTIFACT_KIND_ORDER: tuple[TechLeadRunArtifactKind, ...] = (
    TechLeadRunArtifactKind.SESSION_REPLAY,
    TechLeadRunArtifactKind.REPORT,
    TechLeadRunArtifactKind.DECISION,
)


@dataclass(frozen=True, slots=True)
class TechLeadRunArtifacts:
    """Where one recorded run's preserved artifacts are, and what is there.

    ``location`` is an ABSOLUTE, engine-owned directory that no cleanup path
    touches — never the run's original directory, which lives under a worktree
    and may already be gone. The layout inside it mirrors the run directory, so
    the existing run-scoped artifact readers work against it unchanged.

    ``kinds`` is what was actually found and copied, never what was hoped for: a
    run that died before writing a decision advertises only its replay, and the
    surface offering the drill-down therefore cannot offer a button that 404s.
    """

    location: Path
    kinds: tuple[TechLeadRunArtifactKind, ...]

    def __post_init__(self) -> None:
        if not self.location.is_absolute():
            raise ValueError(
                "TechLeadRunArtifacts.location must be absolute; got"
                f" {self.location}"
            )
        if not self.kinds:
            raise ValueError(
                "TechLeadRunArtifacts with no kinds is not a locator, it is the"
                " absence of one — use None instead."
            )
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError(f"duplicate artifact kinds: {self.kinds}")

    def has(self, kind: TechLeadRunArtifactKind) -> bool:
        """True when ``kind`` was preserved for this run."""
        return kind in self.kinds

    def path_for(self, kind: TechLeadRunArtifactKind) -> Path:
        """The preserved file for ``kind``.

        Raises for a kind this run does not have rather than returning a path
        that is not there: a caller asking for a missing artifact has a bug,
        and answering with a plausible path defers the failure to a reader.
        """
        if not self.has(kind):
            raise KeyError(f"{kind.value} was not preserved for this run")
        return self.location / Path(kind.member)


def kinds_from_values(values: "tuple[str, ...] | list[str]") -> tuple[
    TechLeadRunArtifactKind, ...
]:
    """Parse stored kind values, dropping any this build no longer knows.

    History is inspection, not control: a row written by a newer engine that
    names a fourth kind loses that one button rather than making the whole
    history unreadable.
    """
    known: list[TechLeadRunArtifactKind] = []
    for value in values:
        try:
            kind = TechLeadRunArtifactKind(value)
        except ValueError:
            continue
        if kind not in known:
            known.append(kind)
    return tuple(kind for kind in ARTIFACT_KIND_ORDER if kind in known)


__all__ = [
    "ARTIFACT_KIND_ORDER",
    "TECH_LEAD_DATA_DIRNAME",
    "TERMINAL_RECORDING_FILENAME",
    "TechLeadRunArtifactKind",
    "TechLeadRunArtifacts",
    "kinds_from_values",
]
