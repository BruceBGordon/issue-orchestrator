# pyright: strict
"""Per-lane, tree-SHA-keyed gate verdicts.

The publish gate's cached verdict is monolithic: one green record per
(suite, HEAD SHA), so one flaky lane forces every green lane to re-run.
This store adds the per-lane layer underneath it: each lane target's
GREEN outcome is recorded against the exact tree SHA, and a gate re-run
at the same SHA skips lanes already proven green — a re-run after a
transient failure re-runs only the failed lanes.

Ground rules, each load-bearing:

- **Only green is recorded.** A failure verdict is never cached — a red
  lane re-runs on the next gate (classification-gated retry policy is
  a separate increment; caching failures would automate re-trusting
  them).
- **Whole-tree SHA is the key and the invalidation.** Any commit
  invalidates everything. Deliberately naive: per-lane input
  fingerprints require dependency maps whose incompleteness mints
  vacuous greens — over-invalidation wastes a re-run, under-invalidation
  fakes a verdict. Never trade the first for the second.
- **Corruption is loud, never green.** A record that fails to parse or
  disagrees with its own key raises; callers surface it and stop. The
  message names the file to delete.
- **Worktree-local, not repo-shared.** Verdicts describe one tree; they
  live beside the suite records under ``.issue-orchestrator/validation/``
  (already gitignored and runtime-managed), never in the shared git
  common dir.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

LANE_VERDICTS_RELATIVE = Path(".issue-orchestrator/validation/lanes")

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LaneVerdictError(RuntimeError):
    """The verdict store is corrupt or misused — loud, never green."""


@dataclass(frozen=True, slots=True)
class LaneVerdict:
    """One lane's recorded green at one tree SHA."""

    target: str
    tree_sha: str
    recorded_at: str


def _validated(tree_sha: str, target: str) -> tuple[str, str]:
    if type(tree_sha) is not str or not _SHA_PATTERN.match(tree_sha):
        raise LaneVerdictError(
            f"lane verdicts require a full 40-hex tree SHA, got {tree_sha!r}"
        )
    if type(target) is not str or not _TARGET_PATTERN.match(target):
        raise LaneVerdictError(
            f"lane verdict target name is not a safe make target: {target!r}"
        )
    return tree_sha, target


def _sha_directory(worktree: Path, tree_sha: str) -> Path:
    return worktree / LANE_VERDICTS_RELATIVE / tree_sha


def read_green(worktree: Path, tree_sha: str, target: str) -> LaneVerdict | None:
    """The lane's green verdict at this SHA, None if absent, loud if bad."""
    tree_sha, target = _validated(tree_sha, target)
    path = _sha_directory(worktree, tree_sha) / f"{target}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LaneVerdictError(
            f"lane verdict unreadable (delete it): {path}: {error}"
        ) from error
    try:
        raw: object = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("verdict is not an object")
        record = cast("dict[str, object]", raw)
        recorded_target = record.get("target")
        recorded_sha = record.get("tree_sha")
        exit_code = record.get("exit_code")
        recorded_at = record.get("recorded_at")
        if not isinstance(recorded_target, str) or not isinstance(
            recorded_sha, str
        ):
            raise ValueError("verdict fields are not strings")
        if not isinstance(recorded_at, str):
            raise ValueError("recorded_at is not a string")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError("exit_code is not an integer")
    except ValueError as error:
        raise LaneVerdictError(
            f"lane verdict is corrupt (delete it): {path}: {error}"
        ) from error
    # A record that disagrees with its own key is corruption, not a
    # miss: something else wrote here, and trusting any of it would be
    # a fabricated verdict.
    if recorded_target != target or recorded_sha != tree_sha:
        raise LaneVerdictError(
            f"lane verdict disagrees with its key (delete it): {path}: "
            f"records target={recorded_target!r} sha={recorded_sha!r}"
        )
    if exit_code != 0:
        raise LaneVerdictError(
            f"lane verdict stores a non-green exit (delete it): {path}: "
            f"exit_code={exit_code} — only green is ever recorded"
        )
    return LaneVerdict(
        target=target, tree_sha=tree_sha, recorded_at=recorded_at
    )


def record_green(worktree: Path, tree_sha: str, target: str) -> LaneVerdict:
    """Record the lane green at this SHA; prune verdicts of other SHAs.

    The write is atomic (mkstemp + replace) so two gates racing in one
    worktree at the same SHA both land the same verdict rather than a
    torn file. Pruning keeps only the current SHA's directory: verdicts
    for a tree that no longer exists here are dead weight, and a
    worktree has exactly one HEAD.
    """
    tree_sha, target = _validated(tree_sha, target)
    sha_directory = _sha_directory(worktree, tree_sha)
    verdict = LaneVerdict(
        target=target,
        tree_sha=tree_sha,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    payload = json.dumps(
        {
            "target": verdict.target,
            "tree_sha": verdict.tree_sha,
            "exit_code": 0,
            "recorded_at": verdict.recorded_at,
        },
        sort_keys=True,
    )
    temp_name: str | None = None
    try:
        # Directory creation and the temp file live INSIDE the
        # translation: an unwritable store (0555) is a store fault the
        # caller handles, never a raw traceback (round-1 finding 3).
        sha_directory.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=sha_directory, prefix=f".{target}.", suffix=".part"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        os.replace(temp_name, sha_directory / f"{target}.json")
    except OSError as error:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise LaneVerdictError(
            f"lane verdict could not be recorded: {target} at {tree_sha}: "
            f"{error}"
        ) from error
    _prune_other_shas(worktree, tree_sha)
    return verdict


def _prune_other_shas(worktree: Path, tree_sha: str) -> None:
    root = worktree / LANE_VERDICTS_RELATIVE
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == tree_sha:
            continue
        # Best-effort: stale verdict dirs are dead weight, not policy;
        # a prune raced by another process must not fail the record.
        try:
            for stale in entry.iterdir():
                try:
                    stale.unlink()
                except OSError:
                    pass
            entry.rmdir()
        except OSError:
            pass
