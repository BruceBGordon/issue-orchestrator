"""Tests for RunManifest dataclass — load, save, enrichment."""

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.run_manifest import RunManifest, MANIFEST_FILENAME


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions" / "20260211-120000Z__issue-42"
    d.mkdir(parents=True)
    return d


def _write_manifest(run_dir: Path, data: dict) -> Path:
    data = dict(data)
    data.setdefault("run_dir", str(run_dir))
    log_path = data.setdefault("log_path", str(run_dir / "terminal-recording.jsonl"))
    data.setdefault(
        "artifacts",
        {
            "terminal_recording": {
                "kind": "terminal_recording",
                "path": log_path,
                "content_type": "application/x-ndjson",
            },
        },
    )
    p = run_dir / MANIFEST_FILENAME
    p.write_text(json.dumps(data))
    return p


# ------------------------------------------------------------------
# load / save round-trip
# ------------------------------------------------------------------

class TestLoadSave:
    def test_round_trip_preserves_fields(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {
            "session_name": "issue-42",
            "run_id": "20260211-120000Z",
            "run_dir": str(run_dir),
            "issue_number": 42,
            "started_at": "2026-02-11T12:00:00Z",
        })
        m = RunManifest.load(run_dir)
        assert m.session_name == "issue-42"
        assert m.run_id == "20260211-120000Z"
        assert m.issue_number == 42
        assert m.run_dir == run_dir

        m.save()
        raw = json.loads((run_dir / MANIFEST_FILENAME).read_text())
        assert raw["session_name"] == "issue-42"
        assert raw["issue_number"] == 42

    def test_unknown_fields_preserved(self, run_dir: Path) -> None:
        """Old manifests may have fields we don't model — they must survive."""
        _write_manifest(run_dir, {
            "session_name": "issue-42",
            "run_id": "r1",
            "run_dir": str(run_dir),
            "some_future_field": "hello",
            "retention_pinned": True,
        })
        m = RunManifest.load(run_dir)
        assert m._extra["some_future_field"] == "hello"
        assert m.retention_pinned is True

        m.save()
        raw = json.loads((run_dir / MANIFEST_FILENAME).read_text())
        assert raw["some_future_field"] == "hello"
        assert raw["retention_pinned"] is True

    def test_missing_optional_fields_default_to_none(self, run_dir: Path) -> None:
        """Minimal manifest (e.g. old format) loads cleanly."""
        _write_manifest(run_dir, {
            "session_name": "issue-1",
            "run_id": "r1",
        })
        m = RunManifest.load(run_dir)
        assert m.session_name == "issue-1"
        assert m.implementation is None
        assert m.log_tail is None
        assert m.blocked_by is None

    def test_none_fields_omitted_from_json(self, run_dir: Path) -> None:
        """to_dict() skips None values to keep the file clean."""
        m = RunManifest(session_name="s", run_id="r", run_dir=run_dir)
        d = m.to_dict()
        assert "implementation" not in d
        assert "log_tail" not in d
        assert "session_name" in d

    def test_load_file_not_found(self, run_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            RunManifest.load(run_dir)


# ------------------------------------------------------------------
# update
# ------------------------------------------------------------------

class TestUpdate:
    def test_update_sets_and_saves(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {
            "session_name": "issue-42",
            "run_id": "r1",
        })
        m = RunManifest.load(run_dir)
        m.update(implementation="Added retry logic")
        assert m.implementation == "Added retry logic"

        raw = json.loads((run_dir / MANIFEST_FILENAME).read_text())
        assert raw["implementation"] == "Added retry logic"

    def test_update_rejects_unknown_field(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        m = RunManifest.load(run_dir)
        with pytest.raises(TypeError, match="no_such_field"):
            m.update(no_such_field="bad")


# ------------------------------------------------------------------
# enrich_from_completion_record
# ------------------------------------------------------------------

class TestEnrichFromCompletionRecord:
    def test_copies_all_relevant_fields(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        m = RunManifest.load(run_dir)

        class FakeRecord:
            implementation = "Built the feature"
            problems = "Flaky test"
            attempted = "Tried 3 approaches"
            blocked_reason = "API limit"
            blocked_by = [10, 20]
            question = "Which endpoint?"
            review_summary = "Looks good"
            review_issues = "Missing tests"
            risk_level = "low"
            follow_up_issues = [
                type(
                    "FollowUp",
                    (),
                    {
                        "to_dict": staticmethod(lambda: {
                            "title": "Create ancillary issue",
                            "reason": "Unrelated but discovered while implementing the core task.",
                            "blocking": False,
                        })
                    },
                )()
            ]

        m.enrich_from_completion_record(FakeRecord())
        assert m.implementation == "Built the feature"
        assert m.problems == "Flaky test"
        assert m.attempted == "Tried 3 approaches"
        assert m.blocked_reason == "API limit"
        assert m.blocked_by == [10, 20]
        assert m.question == "Which endpoint?"
        assert m.review_summary == "Looks good"
        assert m.review_issues == "Missing tests"
        assert m.risk_level == "low"
        assert m.follow_up_issues == [{
            "title": "Create ancillary issue",
            "reason": "Unrelated but discovered while implementing the core task.",
            "blocking": False,
        }]

    def test_skips_none_values(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        m = RunManifest.load(run_dir)

        class FakeRecord:
            implementation = "Done"
            problems = None
            attempted = None
            blocked_reason = None
            blocked_by = None
            question = None
            review_summary = None
            review_issues = None
            risk_level = None

        m.enrich_from_completion_record(FakeRecord())
        assert m.implementation == "Done"
        assert m.problems is None

    def test_does_not_overwrite_existing_with_none(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        m = RunManifest.load(run_dir)
        m.implementation = "Original"

        class FakeRecord:
            implementation = None
            problems = None
            attempted = None
            blocked_reason = None
            blocked_by = None
            question = None
            review_summary = None
            review_issues = None
            risk_level = None

        m.enrich_from_completion_record(FakeRecord())
        assert m.implementation == "Original"


# ------------------------------------------------------------------
# to_dict
# ------------------------------------------------------------------

class TestToDict:
    def test_run_dir_serialised_as_string(self, run_dir: Path) -> None:
        m = RunManifest(session_name="s", run_id="r", run_dir=run_dir)
        d = m.to_dict()
        assert isinstance(d["run_dir"], str)

    def test_extra_fields_merged(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
            "custom_key": 123,
        })
        m = RunManifest.load(run_dir)
        d = m.to_dict()
        assert d["custom_key"] == 123
        assert d["session_name"] == "s"

    def test_known_fields_override_extra(self, run_dir: Path) -> None:
        """If an _extra key collides with a known field, known field wins."""
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
        })
        m = RunManifest.load(run_dir)
        m._extra["session_name"] = "stale"
        d = m.to_dict()
        assert d["session_name"] == "s"


class TestValidationOutcome:
    """The validation outcome is a discriminated union derived from three
    flat manifest fields. Writers must produce a consistent triple; readers
    surface the typed view via ``RunManifest.validation_outcome``.

    These tests pin the bug-2 scenario (Session Diagnostics dialog showed
    Status: passed alongside Reason: ``Validation failed for a949871f``).
    """

    def test_validation_outcome_none_when_unset(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        assert RunManifest.load(run_dir).validation_outcome is None

    def test_validation_outcome_passed(self, run_dir: Path) -> None:
        from issue_orchestrator.domain.artifact_contracts import ValidationPassed
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
            "validation_passed": True,
            "validation_status": "passed",
        })
        outcome = RunManifest.load(run_dir).validation_outcome
        assert isinstance(outcome, ValidationPassed)

    def test_validation_outcome_failed(self, run_dir: Path) -> None:
        from issue_orchestrator.domain.artifact_contracts import ValidationFailed
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
            "validation_passed": False,
            "validation_status": "failed",
            "validation_reason": "tests broke",
        })
        outcome = RunManifest.load(run_dir).validation_outcome
        assert isinstance(outcome, ValidationFailed)
        assert outcome.reason == "tests broke"

    def test_validation_outcome_retry(self, run_dir: Path) -> None:
        from issue_orchestrator.domain.artifact_contracts import ValidationRetry
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
            "validation_passed": False,
            "validation_status": "retry",
            "validation_reason": "flaky test",
        })
        outcome = RunManifest.load(run_dir).validation_outcome
        assert isinstance(outcome, ValidationRetry)
        assert outcome.reason == "flaky test"

    def test_validation_outcome_inconsistent_legacy_triple_drops_stale_reason(
        self, run_dir: Path
    ) -> None:
        """Bug 2 regression: an old on-disk manifest written before this
        refactor may still contain the inconsistent triple
        ``{validation_status: passed, validation_reason: "Validation
        failed for a949871f"}`` — the stale reason left behind by an
        earlier failure when the passed-path writer omitted the reason
        field. The typed property must surface ``ValidationPassed`` and
        drop the stale reason so the dialog never displays the
        contradiction again."""
        from issue_orchestrator.domain.artifact_contracts import ValidationPassed
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
            "validation_passed": True,
            "validation_status": "passed",
            # Stale reason from a prior failure — this is the on-disk
            # state the user's screenshot captured.
            "validation_reason": "Validation failed for a949871f (exit_code=1)",
        })
        outcome = RunManifest.load(run_dir).validation_outcome
        assert isinstance(outcome, ValidationPassed)
        # ValidationPassed has no reason field — the stale string is
        # unrepresentable on the typed view.
        assert not hasattr(outcome, "reason")


class TestUpdateValidationOutcomeWritesConsistentTriple:
    """The typed write API (``SessionOutput.update_validation_outcome``)
    must always produce all three legacy fields together so a previous
    outcome's reason cannot survive into a fresh outcome."""

    def test_passed_after_failed_clears_reason(self, run_dir: Path) -> None:
        """The exact bug-2 mechanism, end-to-end through the adapter:
        first write a failed outcome (reason=X), then write passed.
        On disk the manifest must have ``validation_status: passed``
        AND ``validation_reason: None`` — not the stale X."""
        import json
        from issue_orchestrator.domain.artifact_contracts import (
            ValidationFailed,
            ValidationPassed,
        )
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        session_output = FileSystemSessionOutput()
        # FileSystemSessionOutput needs a worktree-shaped run_dir for
        # bootstrap_manifest_identity. Use the fixture run_dir directly.
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
        })

        session_output.update_validation_outcome(
            run_dir,
            ValidationFailed(reason="Validation failed for a949871f (exit_code=1)"),
        )
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["validation_status"] == "failed"
        assert manifest["validation_reason"].startswith("Validation failed")

        session_output.update_validation_outcome(run_dir, ValidationPassed())
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["validation_status"] == "passed"
        assert manifest["validation_passed"] is True
        # The bug fix: stale reason must be cleared, not preserved by
        # the partial-merge update.
        assert manifest.get("validation_reason") is None or "validation_reason" not in manifest
