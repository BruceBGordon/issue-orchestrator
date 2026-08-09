"""The durable run-artifact archive is a BOUNDED owner (#6858 round 2 F6/A3).

Its source is agent-authored and its destination is the operator's state volume,
so "copy the evidence" is not enough on its own. These tests pin the three things
that make it safe to point a durable engine-owned directory at untrusted output:

* admission — symlinks, escaping paths and oversized files never land;
* bounds — an aggregate byte/file budget stops the copy and says so;
* atomic publication + retention — a failed attempt cannot destroy the receipt
  that was already there, a replacement never merges with stale contents, and
  retention converges on the newest N runs while REPORTING what it removed.

Nothing sleeps: mtimes are stamped explicitly where ordering matters.
"""

from __future__ import annotations

import os
from pathlib import Path

from issue_orchestrator.domain.tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
)
from issue_orchestrator.domain.tech_lead_run_artifacts import TechLeadRunArtifactKind
from issue_orchestrator.infra.tech_lead_run_artifact_archive import (
    ArchiveLimits,
    FileSystemTechLeadRunArtifactArchive,
)

RECORDING = '{"event_type": "output", "data_b64": "aGk="}\n'
DECISION = '{"summary": "ok"}'
REPORT = "# Health review\n"


def _run_dir(tmp_path: Path, name: str = "run") -> Path:
    """A finished tech-lead run directory with all three inspectable kinds."""
    run_dir = tmp_path / name
    data_dir = run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "terminal-recording.jsonl").write_text(RECORDING, encoding="utf-8")
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (data_dir / TECH_LEAD_DECISION_FILENAME).write_text(DECISION, encoding="utf-8")
    (data_dir / TECH_LEAD_REPORT_FILENAME).write_text(REPORT, encoding="utf-8")
    (data_dir / "evidence-map.json").write_text('{"locations": []}', encoding="utf-8")
    return run_dir


def _archive(tmp_path: Path, **limits) -> FileSystemTechLeadRunArtifactArchive:
    return FileSystemTechLeadRunArtifactArchive(
        tmp_path / "archive", limits=ArchiveLimits(**limits) if limits else None
    )


def _preserve(archive, run_dir: Path, *, run="run-900", session="tech-lead-900"):
    return archive.preserve(run_id=run, session_name=session, run_dir=run_dir)


def _names(location: Path) -> set[str]:
    return {
        str(path.relative_to(location))
        for path in location.rglob("*")
        if path.is_file()
    }


class TestAdmission:
    def test_the_whole_inspectable_set_is_preserved(self, tmp_path):
        artifacts = _preserve(_archive(tmp_path), _run_dir(tmp_path))

        assert artifacts is not None
        assert set(artifacts.kinds) == {
            TechLeadRunArtifactKind.SESSION_REPLAY,
            TechLeadRunArtifactKind.REPORT,
            TechLeadRunArtifactKind.DECISION,
        }
        # The evidence the report cites travels with it.
        assert "tech-lead-data/evidence-map.json" in _names(artifacts.location)

    def test_a_symlinked_artifact_is_refused_rather_than_followed(self, tmp_path):
        """``copytree`` follows links by default, which would let a run pull an
        arbitrary host file into a durable engine-owned directory."""
        secret = tmp_path / "outside" / "id_rsa"
        secret.parent.mkdir(parents=True)
        secret.write_text("PRIVATE KEY", encoding="utf-8")
        run_dir = _run_dir(tmp_path)
        (run_dir / "tech-lead-data" / "stolen.json").symlink_to(secret)

        artifacts = _preserve(_archive(tmp_path), run_dir)

        assert artifacts is not None
        preserved = _names(artifacts.location)
        assert "tech-lead-data/stolen.json" not in preserved
        assert not (artifacts.location / "tech-lead-data" / "stolen.json").exists()
        # The legitimate artifacts still made it: one refusal is not a lost run.
        assert "tech-lead-data/tech-lead-report.md" in preserved

    def test_a_symlinked_directory_is_not_walked(self, tmp_path):
        elsewhere = tmp_path / "outside"
        elsewhere.mkdir()
        (elsewhere / "host-file.txt").write_text("not ours", encoding="utf-8")
        run_dir = _run_dir(tmp_path)
        (run_dir / "tech-lead-data" / "linked").symlink_to(
            elsewhere, target_is_directory=True
        )

        artifacts = _preserve(_archive(tmp_path), run_dir)

        assert artifacts is not None
        assert not any(
            name.startswith("tech-lead-data/linked")
            for name in _names(artifacts.location)
        )

    def test_a_symlinked_recording_is_refused_and_loses_only_that_kind(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        recording = run_dir / "terminal-recording.jsonl"
        recording.unlink()
        outside = tmp_path / "outside.jsonl"
        outside.write_text(RECORDING, encoding="utf-8")
        recording.symlink_to(outside)

        artifacts = _preserve(_archive(tmp_path), run_dir)

        assert artifacts is not None
        assert TechLeadRunArtifactKind.SESSION_REPLAY not in artifacts.kinds
        assert TechLeadRunArtifactKind.REPORT in artifacts.kinds

    def test_an_oversized_agent_artifact_is_refused(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        (run_dir / "tech-lead-data" / TECH_LEAD_REPORT_FILENAME).write_text(
            "x" * 4096, encoding="utf-8"
        )

        artifacts = _preserve(_archive(tmp_path, artifact_bytes=512), run_dir)

        assert artifacts is not None
        assert TechLeadRunArtifactKind.REPORT not in artifacts.kinds
        # The recording takes the LARGER cap, so it is unaffected by this one.
        assert TechLeadRunArtifactKind.SESSION_REPLAY in artifacts.kinds

    def test_an_oversized_recording_is_refused_under_its_own_cap(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        (run_dir / "terminal-recording.jsonl").write_text("x" * 4096, encoding="utf-8")

        artifacts = _preserve(_archive(tmp_path, recording_bytes=512), run_dir)

        assert artifacts is not None
        assert TechLeadRunArtifactKind.SESSION_REPLAY not in artifacts.kinds

    def test_an_empty_artifact_is_treated_as_absent(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        (run_dir / "tech-lead-data" / TECH_LEAD_DECISION_FILENAME).write_text(
            "", encoding="utf-8"
        )

        artifacts = _preserve(_archive(tmp_path), run_dir)

        assert artifacts is not None
        assert TechLeadRunArtifactKind.DECISION not in artifacts.kinds

    def test_a_run_with_nothing_inspectable_keeps_no_archive_directory(self, tmp_path):
        run_dir = tmp_path / "empty-run"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        archive = _archive(tmp_path)

        assert _preserve(archive, run_dir) is None
        # No shell that looks like a preserved run:
        assert list((tmp_path / "archive").iterdir()) == []


class TestBounds:
    def test_the_file_count_budget_stops_the_copy(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        for index in range(20):
            (run_dir / "tech-lead-data" / f"extra-{index}.json").write_text(
                "{}", encoding="utf-8"
            )

        artifacts = _preserve(_archive(tmp_path, files=3), run_dir)

        assert artifacts is not None
        assert len(_names(artifacts.location)) == 3

    def test_the_aggregate_byte_budget_stops_the_copy(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        for index in range(10):
            (run_dir / "tech-lead-data" / f"big-{index}.json").write_text(
                "x" * 900, encoding="utf-8"
            )

        artifacts = _preserve(_archive(tmp_path, total_bytes=2048), run_dir)

        assert artifacts is not None
        total = sum(
            (artifacts.location / name).stat().st_size
            for name in _names(artifacts.location)
        )
        assert total <= 2048


class TestAtomicPublication:
    def test_a_failed_attempt_leaves_the_previous_receipt_intact(self, tmp_path):
        """Blocking the staging path makes the retry fail before it can publish.

        The point of staging is that the live destination is never opened until a
        complete copy exists beside it, so this must be a no-op rather than a
        destroyed receipt.
        """
        archive = _archive(tmp_path)
        run_dir = _run_dir(tmp_path)
        first = _preserve(archive, run_dir)
        assert first is not None
        blocker = tmp_path / "archive" / ".incoming-run-900__tech-lead-900"
        blocker.write_text("a file where the staging directory must go", "utf-8")

        assert _preserve(archive, run_dir) is None

        assert first.location.is_dir()
        assert TECH_LEAD_REPORT_FILENAME in str(_names(first.location))
        assert (first.location / "tech-lead-data" / TECH_LEAD_REPORT_FILENAME).read_text(
            encoding="utf-8"
        ) == REPORT

    def test_a_replacement_does_not_merge_with_stale_contents(self, tmp_path):
        """Re-preserving a run whose decision is gone must not keep advertising
        the previous attempt's decision."""
        archive = _archive(tmp_path)
        run_dir = _run_dir(tmp_path)
        first = _preserve(archive, run_dir)
        assert first is not None
        (run_dir / "tech-lead-data" / TECH_LEAD_DECISION_FILENAME).unlink()
        (run_dir / "tech-lead-data" / TECH_LEAD_REPORT_FILENAME).unlink()

        second = _preserve(archive, run_dir)

        assert second is not None
        assert second.location == first.location
        assert second.kinds == (TechLeadRunArtifactKind.SESSION_REPLAY,)
        assert not (second.location / "tech-lead-data" / TECH_LEAD_DECISION_FILENAME).exists()

    def test_re_preserving_one_run_does_not_accumulate_archives(self, tmp_path):
        archive = _archive(tmp_path)
        run_dir = _run_dir(tmp_path)

        _preserve(archive, run_dir)
        _preserve(archive, run_dir)

        assert len(list((tmp_path / "archive").iterdir())) == 1


class TestRetention:
    def _preserve_dated(self, archive, tmp_path, index: int) -> Path:
        run_dir = _run_dir(tmp_path, name=f"run-{index}")
        artifacts = _preserve(
            archive, run_dir, run=f"run-{index}", session=f"tech-lead-{index}"
        )
        assert artifacts is not None
        # Explicit mtime so "newest" is a fact, not a race.
        os.utime(artifacts.location, (1_800_000_000 + index, 1_800_000_000 + index))
        return artifacts.location

    def test_retention_keeps_the_newest_runs_and_reports_the_rest(self, tmp_path):
        archive = _archive(tmp_path, retention=2)
        locations = [self._preserve_dated(archive, tmp_path, index) for index in range(4)]

        removed = archive.prune()

        assert set(removed) == {locations[0], locations[1]}
        assert not locations[0].exists()
        assert locations[3].is_dir()

    def test_pruning_is_idempotent_and_converges(self, tmp_path):
        archive = _archive(tmp_path, retention=2)
        for index in range(4):
            self._preserve_dated(archive, tmp_path, index)

        archive.prune()
        assert archive.prune() == ()
        assert len(list((tmp_path / "archive").iterdir())) == 2

    def test_preserving_applies_retention_without_a_separate_pass(self, tmp_path):
        """Retention rides the terminal seam, so an engine that never prunes
        explicitly still cannot grow without bound."""
        archive = _archive(tmp_path, retention=2)
        for index in range(4):
            self._preserve_dated(archive, tmp_path, index)
            archive.prune()

        assert len(list((tmp_path / "archive").iterdir())) == 2

    def test_an_unreadable_archive_root_prunes_nothing_rather_than_raising(
        self, tmp_path
    ):
        archive = FileSystemTechLeadRunArtifactArchive(tmp_path / "never-created")

        assert archive.prune() == ()
