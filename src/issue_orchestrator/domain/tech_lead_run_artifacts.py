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

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .session_run import SESSION_ARTIFACT_PARTS
from .tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
)

if TYPE_CHECKING:
    from .session_run import SessionRunAssets

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
class TechLeadRunSource:
    """Where one finished run's artifacts are, and the ROOT they may be read from.

    The archive used to be handed a run id, a session name and a naked
    ``run_dir``: three loose values that had already travelled together as the
    session's typed run assets, and no statement of what the path was trusted
    relative to. That is what let the adapter open the run directory by absolute
    pathname — following whatever the final component pointed at — before its
    ``O_NOFOLLOW`` walk began (#6858 round 5 F16/A5).

    So this type carries the RELATIONSHIP, not just the paths:

    * ``worktree_path`` is engine-created. Its own prefix is outside anything an
      agent can write, which is what makes it usable as a trust anchor.
    * ``run_dir`` must live under it, and the components BETWEEN them
      (``.issue-orchestrator/sessions/<run>``) are agent-writable — so they are
      exposed as plain NAMES for the adapter to open one at a time, refusing any
      that has become a symlink. Deriving them without resolving is the point: a
      resolved path has already followed the link we mean to catch.
    * Those names are validated and frozen HERE, at construction, so a source that
      would walk out of its own root — or that does not identify a session run at
      all — cannot be represented. The archive's safety must not rest on one
      upstream caller happening to enforce more than this type does.
    """

    run_id: str
    session_name: str
    worktree_path: Path
    run_dir: Path
    # The validated component names between the trusted root and the run
    # directory, derived and FROZEN at construction. Not a caller argument and not
    # a property: a property would recompute the relationship every time it was
    # asked, so a symlinked prefix that changed under a long-lived source could
    # answer differently on the second call than the constructor validated on the
    # first (#6858 round 6 F16).
    relative_run_parts: tuple[str, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        if not self.run_id or not self.session_name:
            raise ValueError(
                "a tech-lead run source needs its session run identity"
                f" (run_id={self.run_id!r}, session_name={self.session_name!r})"
            )
        for label, path in (
            ("worktree_path", self.worktree_path),
            ("run_dir", self.run_dir),
        ):
            if not path.is_absolute():
                raise ValueError(f"TechLeadRunSource.{label} must be absolute: {path}")
        object.__setattr__(self, "relative_run_parts", self._derive_parts())

    def _derive_parts(self) -> tuple[str, ...]:
        """The component names to descend, or a refusal.

        The root is compared both as recorded and as resolved, because a worktree
        reached through a symlinked prefix (macOS ``/tmp`` vs ``/private/tmp``) is
        a normal setup — while the components BELOW it are never resolved, since
        resolving them is exactly what would hide an agent-planted link.

        ``Path.relative_to`` is LEXICAL, so it happily answers ``("..", …)`` for a
        run directory that climbs back out of the root. ``O_NOFOLLOW`` does not
        help there: ``..`` is a real directory entry, not a symlink, so a walk
        handed those names would leave the trusted root without following
        anything. Each name is therefore checked here, in the type whose whole
        job is to prove the relationship — the archive must not depend on one
        upstream construction path remembering a stronger invariant than this
        value object enforces (#6858 round 6 F16/A5).
        """
        parts: tuple[str, ...] | None = None
        for base in (self.worktree_path, self.worktree_path.resolve()):
            try:
                parts = self.run_dir.relative_to(base).parts
                break
            except ValueError:
                continue
        if parts is None:
            raise ValueError(
                f"run_dir {self.run_dir} does not live under the trusted root"
                f" {self.worktree_path}"
            )
        unsafe = [part for part in parts if part in ("", ".", "..")]
        if unsafe:
            raise ValueError(
                f"run_dir {self.run_dir} reaches its trusted root through"
                f" {unsafe} — a descriptor walk given those names would leave"
                f" {self.worktree_path} without following a single symlink"
            )
        namespace = SESSION_ARTIFACT_PARTS
        if parts[: len(namespace)] != namespace or len(parts) <= len(namespace):
            raise ValueError(
                f"run_dir {self.run_dir} is not a session run directory under"
                f" {'/'.join(namespace)} of {self.worktree_path}"
            )
        return parts

    @classmethod
    def from_run_assets(cls, assets: "SessionRunAssets") -> "TechLeadRunSource":
        """The archive's view of an active session's typed run assets.

        One conversion, at the seam that already holds the assets, so no caller
        re-derives a run's identity or its root from loose values.
        """
        return cls(
            run_id=assets.identity.run_id,
            session_name=assets.identity.session_name,
            worktree_path=assets.worktree_path,
            run_dir=assets.run_dir,
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
    "TechLeadRunSource",
    "kinds_from_values",
]
