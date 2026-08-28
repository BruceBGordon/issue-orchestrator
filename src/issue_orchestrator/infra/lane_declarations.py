# pyright: strict
"""Single home for validation-lane scheduling declarations.

Every fact the scheduler needs about a lane — measured CPU demand,
memory budget, suspendability, exclusive tokens — lives in ONE
schema-validated file, ``.issue-orchestrator/lanes.yaml``,
keyed by the lane's logical work key. The Makefile speaks only work
keys and commands; nothing scheduling-shaped hides in recipe text.

The schema is strict both ways: an undeclared lane cannot run (no
policy-by-absence), and an unknown field cannot sit unread in the
file masquerading as configuration.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

LANES_FILE_RELATIVE = Path(".issue-orchestrator/lanes.yaml")


class LaneDeclarationError(RuntimeError):
    """The declarations file is missing, malformed, or incomplete."""


class LaneDeclaration(BaseModel):
    """One lane's scheduling facts.

    ``request_cpus`` is MEASURED demand (busy cores via
    ``/usr/bin/time -l`` on the lane's direct-mode target), never the
    suite's worker count — an I/O-bound suite keeps many workers busy
    on few cores. ``suspendable`` is required, never defaulted: a lane
    nobody classified must fail loudly here, not silently opt into or
    out of machine-load freezing.
    """

    model_config = ConfigDict(extra="forbid")

    request_cpus: int = Field(ge=1)
    memory_mb: int = Field(ge=1)
    suspendable: bool
    exclusive: tuple[str, ...] = ()


class LaneDeclarations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: dict[str, LaneDeclaration]


def load_lane_declarations(worktree: Path) -> LaneDeclarations:
    """Parse and validate the whole declarations file, loudly."""
    path = worktree / LANES_FILE_RELATIVE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise LaneDeclarationError(
            f"lane declarations file not found: {path} — every lane's "
            "scheduling facts must be declared there (see "
            "docs/user/condor_lanes.md)"
        ) from error
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise LaneDeclarationError(
            f"lane declarations file is not valid YAML: {path}: {error}"
        ) from error
    try:
        return LaneDeclarations.model_validate(raw)
    except ValidationError as error:
        raise LaneDeclarationError(
            f"lane declarations file failed schema validation: {path}\n{error}"
        ) from error


def load_lane_declaration(worktree: Path, work_key: str) -> LaneDeclaration:
    """One lane's declaration; an undeclared lane is a loud error."""
    declarations = load_lane_declarations(worktree)
    declaration = declarations.lanes.get(work_key)
    if declaration is None:
        raise LaneDeclarationError(
            f"lane {work_key!r} is not declared in "
            f"{worktree / LANES_FILE_RELATIVE} — add a row with its "
            "measured request_cpus, memory_mb, and an explicit "
            "suspendable classification"
        )
    return declaration
