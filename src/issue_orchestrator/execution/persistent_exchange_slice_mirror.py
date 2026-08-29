"""Per-session recording slices for a persistent review-exchange pair.

A cached role *pair* writes one long-lived ``terminal-recording.jsonl`` that
accumulates across every exchange it handles. Each exchange run additionally
wants its own slice under ``<run_dir>/<role>/`` so the timeline viewer can
replay just that run. This module owns both halves of that arrangement: the
live mirror registration on the role's PTY writer, and the offset translation
between pair-recording event indices and slice-local ones.

Extracted from ``persistent_session_exchange`` so the round-loop module keeps
only round semantics; the behaviour is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .persistent_round_runner import PersistentSession

logger = logging.getLogger(__name__)


@dataclass
class RoleSliceMirror:
    """Translate pair-recording event indices into per-session slice indices.

    The slice file at ``<run_dir>/<role>/terminal-recording.jsonl`` is
    written **continuously** by the role's
    ``MirroredTerminalRecordingWriter`` — registered at exchange start
    via ``add_mirror_recording`` and removed at exchange end. The
    timeline viewer therefore sees agent output update in near real time
    rather than waiting for a chapter boundary to flush.

    What this dataclass owns is the **offset translation** between the
    pair recording (long-lived, accumulates across every exchange the
    pair handles) and the slice (per-exchange, freshly attached). Its
    ``slice_base`` is the pair recording's event count *at exchange
    start* — the first event the slice will mirror. Chapter sidecars
    store ``pair_event_idx - slice_base`` so the viewer can scrub the
    manifest-pointed slice directly. Without that translation, a cached
    pair on exchange 2 would record chapter offsets in the hundreds
    while the slice file holds dozens of events and the web replay
    route's ``all_events[offset:]`` would return an empty window.
    """

    pair_recording: Path
    session_slice: Path
    slice_base: int

    def pair_to_slice_offset(self, pair_event_idx: int) -> int:
        """Translate a pair-recording event index into a slice-local index.

        The slice file is written as a strict subset of the pair
        recording starting at ``slice_base``; the slice's event N
        corresponds to pair event ``slice_base + N``. Chapter sidecars
        store these slice-local offsets so the viewer can scrub the
        manifest-pointed slice directly.

        Raises ``ValueError`` when ``pair_event_idx < slice_base``.
        Chapter recording happens during the exchange, after slice
        attach; an index from before exchange start is a wrong-source
        bug (caller fed an index from a different recording) and
        masking it with a clamp would silently return wrong content.
        """
        if pair_event_idx < self.slice_base:
            raise ValueError(
                f"pair_event_idx={pair_event_idx} is below "
                f"slice_base={self.slice_base}; chapter offsets must "
                "be sampled after the slice mirror is attached at "
                "exchange start. A negative slice index would index "
                "past the start of the slice and silently return "
                "content from prior exchanges.",
            )
        return pair_event_idx - self.slice_base


def attach_slice_mirror(
    session: PersistentSession,
    slice_path: Path,
) -> None:
    """Register a per-session slice with the role's PTY writer.

    Fails loudly. The slice mirror is load-bearing for the per-session
    timeline contract — without it the viewer reads an empty slice
    file from the manifest while the agent's output continues to flow
    only into the pair recording, recreating the exact "I can't see
    what the reviewer is doing" symptom this PR is supposed to fix.
    Failures here propagate up to ``run_persistent_session_exchange``'s
    top-level handler, which emits ``REVIEW_EXCHANGE_FAILED`` and
    re-raises so the orchestrator's loop bound (PR #6267) can govern
    retries / escalation rather than the silent empty-timeline mode.

    ``log_writer is None`` is a production invariant violation: every
    role session opened by ``open_persistent_session`` carries a real
    ``MirroredTerminalRecordingWriter``. Test fixtures that construct
    sessions directly must wire a writer too — not doing so would mean
    the test was getting a free pass on the live-mirror invariant.
    """
    writer = session.log_writer
    if writer is None:
        raise RuntimeError(
            f"PersistentSession has no log_writer; cannot attach "
            f"per-session slice mirror at {slice_path}. Production "
            "sessions always carry a writer; this indicates either a "
            "regression in open_persistent_session or a test fixture "
            "that bypassed the writer wiring.",
        )
    # ``seed_resize=False`` keeps the slice indexing aligned with the
    # offset translator: the first slice event corresponds to the
    # first pair event written *after* exchange start, with no
    # synthetic leading event to throw off ``pair_to_slice_offset``.
    writer.add_mirror_recording(slice_path, seed_resize=False)


def detach_slice_mirror(
    session: PersistentSession,
    slice_path: Path,
) -> None:
    """Stop mirroring writes to the per-session slice path.

    Called from a ``finally`` block, so any exception here would
    obscure the exception that put us in the finally — log and
    continue rather than mask the real failure. The flip side of
    ``attach_slice_mirror``'s fail-fast: if attach succeeded, detach
    almost never fails (the writer's path map is in-process state),
    and if detach somehow fails the worst case is the next exchange
    seeing tail bytes from this exchange in its slice — caught by
    ``test_slice_detaches_at_exchange_end_no_leak_to_next_exchange``.
    """
    writer = session.log_writer
    if writer is None:
        # The attach helper would have raised before we got here, so
        # reaching this branch means someone called detach without
        # ever calling attach. Tolerate so a partial-construction
        # cleanup path stays simple.
        return
    try:
        writer.remove_mirror_recording(slice_path)
    except (OSError, ValueError):
        logger.exception(
            "Failed to detach per-session slice mirror at %s during "
            "exchange teardown; subsequent writes from this writer "
            "may continue to target the slice file. Logging and "
            "continuing — raising here would mask the original "
            "exception that triggered the finally block.",
            slice_path,
        )


def prepare_session_slice(slice_path: Path) -> None:
    """Create the per-session slice directory and seed an empty file.

    Pre-creating an empty file keeps the timeline viewer's recording
    lookup (``ManifestAccessor.get_review_exchange_recording``) from
    404'ing while a hung exchange is mid-round and no slice events
    have been mirrored yet. ``allow_empty=False`` callers still see
    "empty" as a recoverable condition rather than "missing".
    """
    slice_path.parent.mkdir(parents=True, exist_ok=True)
    slice_path.touch(exist_ok=True)
