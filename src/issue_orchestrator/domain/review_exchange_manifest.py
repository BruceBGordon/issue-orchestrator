"""Typed review-exchange manifest sections.

The per-exchange ``manifest.json`` accumulates fields written by
several producers (run start, recording wiring, retention metadata,
artifact registration). Each producer owns a coherent slice of the
schema. This module names those slices as frozen dataclasses with
explicit ``to_manifest_fields`` (writer side) and ``from_manifest``
(reader side) methods so:

- Writers cannot forget a required field — the constructor enforces it.
- Readers can't silently see a missing/wrong-typed field — parsing
  fails loudly via ``from_manifest``.
- The contract is one place: adding a field to a section forces the
  refactor through every call site, instead of one writer drifting
  ahead of one reader (the recurring brittleness pattern that
  produced PR #6267 / #6268 / #6270's review whack-a-mole).

The dataclasses are domain-pure: ``Path`` plus ``str | None``. They
do not reach into the execution layer; the execution-side adapters
own serialization to/from the on-disk JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any



@dataclass(frozen=True)
class ReviewExchangeManifestHeader:
    """Manifest fields a review-exchange run stamps at start.

    ``exchange_dir`` is the per-run directory holding ``summary.json``
    and ``chapters.json``; the cache loader reads it via the
    ``review_exchange_dir`` manifest key.

    ``parent_session_name`` (post PR #6271 ``review-state-machine``
    refactor) names the parent coding session that triggered this
    exchange. The cache loader and the consecutive-failure counter
    use it to scope candidates to runs from the SAME coding session,
    replacing the prior layout-sensitive mtime-walk + run_dir-name
    inference. Optional only for backwards compatibility with pre-
    refactor callers; production flows always provide it.
    """

    exchange_dir: Path
    parent_session_name: str | None = None

    def to_manifest_fields(self) -> dict[str, str]:
        """Serialize to the (string, string) pairs ``update_manifest``
        merges into ``manifest.json``. Optional fields are omitted
        rather than written as ``None`` so readers detect "unset" by
        absence."""
        fields: dict[str, str] = {"review_exchange_dir": str(self.exchange_dir)}
        if self.parent_session_name is not None:
            fields["parent_session_name"] = self.parent_session_name
        return fields

    @classmethod
    def from_manifest(
        cls,
        manifest: dict[str, Any],
    ) -> "ReviewExchangeManifestHeader | None":
        """Parse a manifest dict back into a typed header.

        Returns ``None`` when ``review_exchange_dir`` is missing or
        not a string (i.e. the manifest does not represent a review-
        exchange run, or the field hasn't been stamped yet). Optional
        fields parse to ``None`` when missing or wrongly-typed; we
        deliberately do NOT raise on those, because legacy runs
        predate ``parent_session_name`` and should still parse.
        """
        exchange_dir_raw = manifest.get("review_exchange_dir")
        if not isinstance(exchange_dir_raw, str) or not exchange_dir_raw:
            return None
        parent_raw = manifest.get("parent_session_name")
        parent_session_name = (
            parent_raw if isinstance(parent_raw, str) and parent_raw else None
        )
        return cls(
            exchange_dir=Path(exchange_dir_raw),
            parent_session_name=parent_session_name,
        )


@dataclass(frozen=True)
class ReviewExchangeRecordingPaths:
    """Manifest fields wiring the timeline viewer to the recordings.

    Five paths, all required:

    - ``persistent_pair_dir``: pair-scoped state directory holding
      the canonical recordings.
    - ``coder_recording`` / ``reviewer_recording``: per-session slice
      files inside the run_dir. The viewer reads these by default so
      each exchange is self-contained.
    - ``coder_recording_pair`` / ``reviewer_recording_pair``: the
      canonical continuous PTY captures at the pair scope, kept for
      power users / cross-exchange forensics.

    Defining all five together as one dataclass enforces the writer
    invariant that the slice and pair pointers are stamped in lockstep
    — splitting them across separate ``update_manifest`` calls used
    to risk one half being set while the other was missing.
    """

    persistent_pair_dir: Path
    coder_recording: Path
    reviewer_recording: Path
    coder_recording_pair: Path
    reviewer_recording_pair: Path

    def to_manifest_fields(self) -> dict[str, str]:
        return {
            "persistent_pair_dir": str(self.persistent_pair_dir),
            "coder_recording": str(self.coder_recording),
            "reviewer_recording": str(self.reviewer_recording),
            "coder_recording_pair": str(self.coder_recording_pair),
            "reviewer_recording_pair": str(self.reviewer_recording_pair),
        }

    @classmethod
    def from_manifest(
        cls,
        manifest: dict[str, Any],
    ) -> "ReviewExchangeRecordingPaths | None":
        """Parse a manifest dict back into typed recording paths.

        Returns ``None`` if any required field is missing or wrongly-
        typed. All five fields are required together; parsing is
        all-or-nothing because a partial set isn't a usable manifest
        for this section.
        """
        keys = (
            "persistent_pair_dir",
            "coder_recording",
            "reviewer_recording",
            "coder_recording_pair",
            "reviewer_recording_pair",
        )
        values: dict[str, str] = {}
        for key in keys:
            raw = manifest.get(key)
            if not isinstance(raw, str) or not raw:
                return None
            values[key] = raw
        return cls(
            persistent_pair_dir=Path(values["persistent_pair_dir"]),
            coder_recording=Path(values["coder_recording"]),
            reviewer_recording=Path(values["reviewer_recording"]),
            coder_recording_pair=Path(values["coder_recording_pair"]),
            reviewer_recording_pair=Path(values["reviewer_recording_pair"]),
        )


@dataclass(frozen=True)
class ReviewExchangeSummaryManifestUpdate:
    """Artifact fields written when a review-exchange summary is stored.

    Terminal outcome and retention fields belong to ``TimelineEvidence``.
    """

    exchange_dir: Path
    summary_path: Path
    validation_record_path: Path | None = None

    def to_manifest_fields(self) -> dict[str, str]:
        fields = {
            "review_exchange_dir": str(self.exchange_dir),
            "review_exchange_summary_path": str(self.summary_path),
        }
        if self.validation_record_path is not None:
            fields["validation_record_path"] = str(self.validation_record_path)
        return fields
