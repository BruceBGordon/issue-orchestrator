"""Round-trip tests for the typed review-exchange manifest sections.

The point of typing these sections is to catch errors at the seam
where data crosses to/from JSON. These tests exercise both directions
(``to_manifest_fields`` for writes, ``from_manifest`` for reads) and
assert symmetry — the field set the writer emits is exactly the field
set the reader recovers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.review_exchange_manifest import (
    ReviewExchangeManifestHeader,
    ReviewExchangeRecordingPaths,
    ReviewExchangeSummaryManifestUpdate,
)


# ---------------------------------------------------------------------------
# ReviewExchangeManifestHeader
# ---------------------------------------------------------------------------


class TestReviewExchangeManifestHeader:
    def test_full_round_trip_preserves_fields(self) -> None:
        original = ReviewExchangeManifestHeader(
            exchange_dir=Path("/wt/.issue-orchestrator/sessions/r1/review-exchange"),
            parent_session_name="coding-1",
        )
        recovered = ReviewExchangeManifestHeader.from_manifest(
            original.to_manifest_fields(),
        )
        assert recovered == original

    def test_round_trip_without_parent_session_name(self) -> None:
        """Optional field stays None across the round-trip when not
        set, rather than appearing as the literal string ``"None"``."""
        original = ReviewExchangeManifestHeader(
            exchange_dir=Path("/wt/r1/review-exchange"),
        )
        recovered = ReviewExchangeManifestHeader.from_manifest(
            original.to_manifest_fields(),
        )
        assert recovered == original
        assert recovered is not None
        assert recovered.parent_session_name is None

    def test_to_manifest_omits_parent_session_when_unset(self) -> None:
        """Optional fields are omitted from the dict (not serialized
        as None) so readers detect "unset" by absence."""
        header = ReviewExchangeManifestHeader(
            exchange_dir=Path("/wt/r1/review-exchange"),
        )
        fields = header.to_manifest_fields()
        assert "parent_session_name" not in fields
        assert fields == {
            "review_exchange_dir": "/wt/r1/review-exchange",
        }

    def test_from_manifest_returns_none_when_review_exchange_dir_missing(self) -> None:
        # Empty / non-review-exchange manifest.
        assert ReviewExchangeManifestHeader.from_manifest({}) is None
        assert (
            ReviewExchangeManifestHeader.from_manifest(
                {
                    "session_name": "coding-1",
                    "started_at": "2026-05-06T00:00:00",
                }
            )
            is None
        )

    def test_from_manifest_returns_none_when_review_exchange_dir_wrong_type(
        self,
    ) -> None:
        assert (
            ReviewExchangeManifestHeader.from_manifest(
                {
                    "review_exchange_dir": 42,  # not a str
                }
            )
            is None
        )

    def test_from_manifest_returns_none_when_review_exchange_dir_empty(self) -> None:
        assert (
            ReviewExchangeManifestHeader.from_manifest(
                {
                    "review_exchange_dir": "",
                }
            )
            is None
        )

    def test_from_manifest_tolerates_legacy_runs_without_parent_session_name(
        self,
    ) -> None:
        """Backwards-compat: pre-state-machine manifests don't carry
        ``parent_session_name``. Parsing must succeed and leave the
        field None — the cache loader's ``_manifest_matches_parent_session``
        returns ``None`` (legacy fallback) on that shape."""
        recovered = ReviewExchangeManifestHeader.from_manifest(
            {
                "review_exchange_dir": "/wt/r1/review-exchange",
            }
        )
        assert recovered is not None
        assert recovered.exchange_dir == Path("/wt/r1/review-exchange")
        assert recovered.parent_session_name is None

    def test_from_manifest_ignores_wrong_typed_parent_session_name(self) -> None:
        recovered = ReviewExchangeManifestHeader.from_manifest(
            {
                "review_exchange_dir": "/wt/r1/review-exchange",
                "parent_session_name": 42,  # not a str — treat as unset
            }
        )
        assert recovered is not None
        assert recovered.parent_session_name is None


# ---------------------------------------------------------------------------
# ReviewExchangeRecordingPaths
# ---------------------------------------------------------------------------


class TestReviewExchangeRecordingPaths:
    def test_full_round_trip_preserves_fields(self) -> None:
        original = ReviewExchangeRecordingPaths(
            persistent_pair_dir=Path("/state/persistent-pairs/issue-359"),
            coder_recording=Path("/wt/r1/coder/terminal-recording.jsonl"),
            reviewer_recording=Path("/wt/r1/reviewer/terminal-recording.jsonl"),
            coder_recording_pair=Path(
                "/state/persistent-pairs/issue-359/coder/terminal-recording.jsonl"
            ),
            reviewer_recording_pair=Path(
                "/state/persistent-pairs/issue-359/reviewer/terminal-recording.jsonl"
            ),
        )
        recovered = ReviewExchangeRecordingPaths.from_manifest(
            original.to_manifest_fields(),
        )
        assert recovered == original

    @pytest.mark.parametrize(
        "missing_key",
        [
            "persistent_pair_dir",
            "coder_recording",
            "reviewer_recording",
            "coder_recording_pair",
            "reviewer_recording_pair",
        ],
    )
    def test_from_manifest_returns_none_when_any_field_missing(
        self,
        missing_key: str,
    ) -> None:
        """All five fields are required together; partial states are
        rejected wholesale because a partial set isn't usable."""
        full = {
            "persistent_pair_dir": "/state/persistent-pairs/issue-1",
            "coder_recording": "/wt/r1/coder/terminal-recording.jsonl",
            "reviewer_recording": "/wt/r1/reviewer/terminal-recording.jsonl",
            "coder_recording_pair": "/state/persistent-pairs/issue-1/coder/terminal-recording.jsonl",
            "reviewer_recording_pair": "/state/persistent-pairs/issue-1/reviewer/terminal-recording.jsonl",
        }
        del full[missing_key]
        assert ReviewExchangeRecordingPaths.from_manifest(full) is None

    def test_from_manifest_returns_none_when_field_wrong_type(self) -> None:
        # A non-string in any required slot rejects the whole section.
        assert (
            ReviewExchangeRecordingPaths.from_manifest(
                {
                    "persistent_pair_dir": 42,
                    "coder_recording": "/a",
                    "reviewer_recording": "/b",
                    "coder_recording_pair": "/c",
                    "reviewer_recording_pair": "/d",
                }
            )
            is None
        )


# ---------------------------------------------------------------------------
# ReviewExchangeSummaryManifestUpdate
# ---------------------------------------------------------------------------


class TestReviewExchangeSummaryManifestUpdate:
    def test_to_manifest_fields_preserves_typed_summary_update(self) -> None:
        update = ReviewExchangeSummaryManifestUpdate(
            exchange_dir=Path("/wt/r1/review-exchange"),
            summary_path=Path("/wt/r1/review-exchange/summary.json"),
            validation_record_path=Path("/wt/r1/review-exchange/validation.json"),
        )

        assert update.to_manifest_fields() == {
            "review_exchange_dir": "/wt/r1/review-exchange",
            "review_exchange_summary_path": "/wt/r1/review-exchange/summary.json",
            "validation_record_path": "/wt/r1/review-exchange/validation.json",
        }

    def test_to_manifest_fields_omits_validation_path_when_unset(self) -> None:
        update = ReviewExchangeSummaryManifestUpdate(
            exchange_dir=Path("/wt/r1/review-exchange"),
            summary_path=Path("/wt/r1/review-exchange/summary.json"),
        )

        assert update.to_manifest_fields() == {
            "review_exchange_dir": "/wt/r1/review-exchange",
            "review_exchange_summary_path": "/wt/r1/review-exchange/summary.json",
        }
