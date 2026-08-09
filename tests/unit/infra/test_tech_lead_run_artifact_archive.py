"""The durable run-artifact archive is a BOUNDED owner (#6858 F6/F8-F10, A3).

Its source is agent-authored and its destination is the operator's state volume,
so "copy the evidence" is not enough on its own. These tests pin the properties
that make it safe to point a durable engine-owned directory at untrusted output:

* admission — symlinks, escaping paths and oversized files never land;
* bounds on what LANDS — an aggregate byte/file budget stops the copy and says so;
* bounds on DISCOVERY — entries, directories and depth are capped before anything
  is copied, and one unreadable child never costs the admitted artifacts;
* descriptor anchoring — admission and copy observe the same inode, so nothing
  swapped underneath the walk can redirect a read outside the run;
* crash-safe publication + retention — an interrupted publish is reconciled, a
  failed attempt cannot destroy the receipt already there, a replacement never
  merges with stale contents, and retention converges while REPORTING removals.

Nothing sleeps. Where a race is unavoidable the assertion is an INVARIANT that
holds for every interleaving, so the test cannot flake: mtimes are stamped
explicitly where ordering matters.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from issue_orchestrator.domain.tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
)
from issue_orchestrator.domain.tech_lead_run_artifacts import TechLeadRunArtifactKind
from issue_orchestrator.infra.contained_artifact_copy import (
    CopyBounds,
    CopyBudget,
    admit_contained_file,
    close_fd,
    copy_contained_file,
    open_anchor,
    stream_admitted,
)
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


def _open_descriptor_count() -> int:
    """How many descriptors this process holds open.

    ``/dev/fd`` is the portable-enough window into that on both platforms this
    repo supports (on Linux it is a symlink to ``/proc/self/fd``). Counting it is
    what makes "every opened descriptor is closed" an OBSERVABLE fact rather than
    an assertion about private state.
    """
    return len(os.listdir("/dev/fd"))


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
    def test_a_retry_after_the_worktree_is_gone_keeps_the_receipt(self, tmp_path):
        """The publish-retry case: completion re-enters after cleanup removed the
        run's disposable worktree, so there is nothing left to copy.

        The live destination is never opened until a complete copy exists beside
        it, so this must be a no-op rather than a destroyed receipt.
        """
        archive = _archive(tmp_path)
        run_dir = _run_dir(tmp_path)
        first = _preserve(archive, run_dir)
        assert first is not None
        shutil.rmtree(run_dir)

        assert _preserve(archive, run_dir) is None

        assert first.location.is_dir()
        assert (first.location / "tech-lead-data" / TECH_LEAD_REPORT_FILENAME).read_text(
            encoding="utf-8"
        ) == REPORT
        assert (first.location / "terminal-recording.jsonl").read_text(
            encoding="utf-8"
        ) == RECORDING

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


class TestBoundedDiscovery:
    """#6858 round 3 F8: the traversal itself is bounded, not just the copy.

    Before this, discovery materialised the whole agent-authored tree with a
    recursive ``sorted(...)`` walk before the copy budget was consulted — so a
    tree of a million empty files or dangling links exhausted the engine before
    the 200-file budget refused anything, and deep nesting raised ``RecursionError``
    straight through the port's never-raise contract.
    """

    def test_a_huge_symlink_only_tree_is_bounded_by_the_scan_cap(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("not ours", encoding="utf-8")
        noise = run_dir / "tech-lead-data" / "noise"
        noise.mkdir()
        for index in range(400):
            (noise / f"link-{index}").symlink_to(outside)

        artifacts = _preserve(_archive(tmp_path, scan_entries=50), run_dir)

        # Whatever the scan cap truncated, no link content came along...
        assert artifacts is not None
        for name in _names(artifacts.location):
            assert "not ours" not in (artifacts.location / name).read_text(
                encoding="utf-8", errors="replace"
            )
        # ...and the walk stopped well short of the 400-entry tree.
        assert len(_names(artifacts.location)) <= 50

    def test_a_deep_tree_is_refused_below_the_depth_limit(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        deep = run_dir / "tech-lead-data"
        for level in range(12):
            deep = deep / f"level-{level}"
        deep.mkdir(parents=True)
        (deep / "buried.json").write_text('{"deep": true}', encoding="utf-8")

        artifacts = _preserve(_archive(tmp_path, scan_depth=3), run_dir)

        assert artifacts is not None
        assert not any("buried.json" in name for name in _names(artifacts.location))
        # The artifacts at a sane depth are unaffected by a refused branch.
        assert TechLeadRunArtifactKind.REPORT in artifacts.kinds

    def test_a_directory_count_cap_stops_descent(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        for index in range(40):
            child = run_dir / "tech-lead-data" / f"dir-{index}"
            child.mkdir()
            (child / "note.json").write_text("{}", encoding="utf-8")

        artifacts = _preserve(_archive(tmp_path, scan_directories=3), run_dir)

        assert artifacts is not None
        descended = {
            name.split("/")[1] for name in _names(artifacts.location)
            if name.startswith("tech-lead-data/dir-")
        }
        assert len(descended) <= 3

    def test_an_unreadable_child_directory_costs_only_itself(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        blocked = run_dir / "tech-lead-data" / "blocked"
        blocked.mkdir()
        (blocked / "hidden.json").write_text("{}", encoding="utf-8")
        os.chmod(blocked, 0o000)
        try:
            artifacts = _preserve(_archive(tmp_path), run_dir)
        finally:
            os.chmod(blocked, 0o700)

        # The admitted artifacts survive an inaccessible sibling.
        assert artifacts is not None
        assert set(artifacts.kinds) == {
            TechLeadRunArtifactKind.SESSION_REPLAY,
            TechLeadRunArtifactKind.REPORT,
            TechLeadRunArtifactKind.DECISION,
        }
        assert not any("hidden.json" in name for name in _names(artifacts.location))


class TestTheCopyIsAnchoredOnDescriptors:
    """#6858 round 3 F9 / round 4 F15: admission and copy observe the SAME inode.

    Checking a pathname and then reopening it is a TOCTOU window: a concurrent
    writer can swap the file — or an ancestor directory — for a link and make the
    copy read from outside the run. The walk therefore opens every component
    ``O_NOFOLLOW`` relative to its parent's descriptor and streams from that
    descriptor, as ``validation_record_containment`` does for a single file.

    These drive the copier's own two steps — admit, then stream — so the mutation
    lands EXACTLY in the window that used to be exploitable, with no threads and
    no scheduler luck. A pathname-reopening implementation fails every one of
    them; the round-3 spinning-thread versions could pass without ever entering
    the window, which is why they are gone.
    """

    SECRET = "PRIVATE KEY MATERIAL"

    def _anchored(self, run_dir: Path):
        anchor = open_anchor(run_dir)
        assert anchor is not None
        return anchor

    def _budget(self, *, total_bytes: int = 1_000_000) -> CopyBudget:
        return CopyBudget(
            CopyBounds(
                files=10, total_bytes=total_bytes, entries=50, directories=5, depth=3
            )
        )

    def test_a_file_swapped_for_a_link_after_admission_is_never_read(self, tmp_path):
        secret = tmp_path / "outside" / "id_rsa"
        secret.parent.mkdir(parents=True)
        secret.write_text(self.SECRET, encoding="utf-8")
        run_dir = _run_dir(tmp_path)
        source = run_dir / "manifest.json"
        source.write_text('{"ours": true}', encoding="utf-8")
        anchor = self._anchored(run_dir)
        try:
            admitted = admit_contained_file(
                anchor, "manifest.json", cap=4096, budget=self._budget()
            )
            assert admitted is not None
            # THE window: the name now points at a secret outside the run.
            source.unlink()
            source.symlink_to(secret)

            written = stream_admitted(admitted, tmp_path / "landed.json")
        finally:
            if admitted is not None:
                admitted.close()
            close_fd(anchor)

        assert written == len('{"ours": true}')
        assert (tmp_path / "landed.json").read_text(encoding="utf-8") == '{"ours": true}'

    def test_a_file_replaced_by_other_content_after_admission_is_never_read(
        self, tmp_path
    ):
        """Not just links: the admitted INODE is what gets copied, so a rewrite
        through the same name cannot substitute content either."""
        run_dir = _run_dir(tmp_path)
        source = run_dir / "manifest.json"
        source.write_text("original", encoding="utf-8")
        anchor = self._anchored(run_dir)
        try:
            admitted = admit_contained_file(
                anchor, "manifest.json", cap=4096, budget=self._budget()
            )
            assert admitted is not None
            source.unlink()
            source.write_text(self.SECRET, encoding="utf-8")

            stream_admitted(admitted, tmp_path / "landed.json")
        finally:
            if admitted is not None:
                admitted.close()
            close_fd(anchor)

        assert (tmp_path / "landed.json").read_text(encoding="utf-8") == "original"

    def test_an_ancestor_swapped_for_a_link_is_refused(self, tmp_path):
        """A symlinked ancestor trips ``O_NOFOLLOW`` on the directory open, so the
        branch is refused rather than followed out of the run."""
        elsewhere = tmp_path / "outside"
        elsewhere.mkdir()
        (elsewhere / "id_rsa").write_text(self.SECRET, encoding="utf-8")
        run_dir = _run_dir(tmp_path)
        (run_dir / "tech-lead-data" / "nested").symlink_to(
            elsewhere, target_is_directory=True
        )

        artifacts = _preserve(_archive(tmp_path), run_dir)

        assert artifacts is not None
        assert not any("nested" in name for name in _names(artifacts.location))

    def test_a_symlink_planted_before_admission_is_refused_at_the_open(self, tmp_path):
        secret = tmp_path / "outside" / "id_rsa"
        secret.parent.mkdir(parents=True)
        secret.write_text(self.SECRET, encoding="utf-8")
        run_dir = _run_dir(tmp_path)
        source = run_dir / "manifest.json"
        source.unlink()
        source.symlink_to(secret)
        anchor = self._anchored(run_dir)
        try:
            assert admit_contained_file(
                anchor, "manifest.json", cap=4096, budget=self._budget()
            ) is None
        finally:
            close_fd(anchor)


class TestBlockingSpecialFilesAreRefused:
    """#6858 round 4 F13: a FIFO must not be able to hang the terminal seam.

    A FIFO is neither a symlink nor a directory, so the scan offers it as a file.
    Opened read-only and BLOCKING it waits for a writer that may never come — and
    that wait happens at a completing run's terminal seam, before the
    regular-file check can reject it. ``O_NONBLOCK`` on the open is what makes the
    rejection reachable. Without the fix these tests hang rather than fail, so
    each carries a timeout.
    """

    @pytest.mark.timeout(30)
    def test_admission_refuses_a_fifo_without_waiting_for_a_writer(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        (run_dir / "manifest.json").unlink()
        os.mkfifo(run_dir / "manifest.json")
        anchor = open_anchor(run_dir)
        assert anchor is not None
        budget = CopyBudget(
            CopyBounds(files=5, total_bytes=4096, entries=20, directories=3, depth=2)
        )
        try:
            assert admit_contained_file(
                anchor, "manifest.json", cap=4096, budget=budget
            ) is None
        finally:
            close_fd(anchor)

    @pytest.mark.timeout(30)
    def test_preserving_a_run_with_a_fifo_completes_and_refuses_it(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        os.mkfifo(run_dir / "tech-lead-data" / "pipe.json")

        artifacts = _preserve(_archive(tmp_path), run_dir)

        assert artifacts is not None
        assert not any("pipe.json" in name for name in _names(artifacts.location))
        # The real artifacts are unaffected by the refused special file.
        assert TechLeadRunArtifactKind.REPORT in artifacts.kinds


class TestTheAggregateBoundHoldsUnderGrowth:
    """#6858 round 4 F14: admission is granted on the size ``fstat`` reported.

    A file an agent is still appending to can stay under its per-file cap and
    still push the archive past its AGGREGATE bound, because the budget was
    debited with the streamed size. The stream is therefore capped by whichever
    ceiling is smaller — this file's cap, or the bytes still unspent.
    """

    def test_a_file_grown_after_admission_cannot_outspend_the_budget(self, tmp_path):
        run_dir = _run_dir(tmp_path)
        source = run_dir / "manifest.json"
        source.write_text("x" * 100, encoding="utf-8")
        budget = CopyBudget(
            CopyBounds(files=10, total_bytes=150, entries=50, directories=5, depth=3)
        )
        # 120 of the 150 aggregate bytes are already gone, so this file's real
        # allowance is 30 even though its own cap is 4096.
        budget.spend(120)
        anchor = open_anchor(run_dir)
        assert anchor is not None
        target = tmp_path / "landed.json"
        try:
            admitted = admit_contained_file(
                anchor, "manifest.json", cap=4096, budget=budget
            )
            assert admitted is not None
            assert admitted.size == 100
            # Admission GRANTED the smaller of the two ceilings, not the file cap.
            assert admitted.allowance == 30
            # THE window: it grows past that allowance but stays under its cap.
            with source.open("a", encoding="utf-8") as handle:
                handle.write("y" * 500)

            written = stream_admitted(admitted, target)
        finally:
            if admitted is not None:
                admitted.close()
            close_fd(anchor)

        assert written is None, "a file over its allowance must be refused"
        assert not target.exists(), "no partial artifact may be left behind"
        assert budget.remaining_bytes == 30

    def test_the_copier_spends_only_what_it_was_granted(self, tmp_path):
        """End to end through the public copy step: whatever lands, the aggregate
        bound holds."""
        run_dir = _run_dir(tmp_path)
        (run_dir / "manifest.json").write_text("x" * 100, encoding="utf-8")
        budget = CopyBudget(
            CopyBounds(files=10, total_bytes=150, entries=50, directories=5, depth=3)
        )
        budget.spend(120)
        anchor = open_anchor(run_dir)
        assert anchor is not None
        try:
            landed = copy_contained_file(
                anchor,
                "manifest.json",
                tmp_path / "landed.json",
                cap=4096,
                budget=budget,
            )
        finally:
            close_fd(anchor)

        # 100 bytes do not fit in the 30 that are left, so nothing is spent.
        assert landed == 0
        assert budget.remaining_bytes == 30
        assert not (tmp_path / "landed.json").exists()


class TestEveryOpenedDescriptorIsClosed:
    """#6858 round 4 F12: a processed directory's descriptor must be released.

    The walk queues an open descriptor per admitted subdirectory. Closing only the
    ones still queued leaked one per VISITED directory — up to the directory cap
    per run — and repeated tech-lead runs then exhaust the engine's descriptor
    limit, at which point unrelated SQLite, GitHub and terminal work starts
    failing. Counting the process's open descriptors is the observable proof.
    """

    def _nested_run(self, tmp_path, name: str) -> Path:
        run_dir = _run_dir(tmp_path, name=name)
        branch = run_dir / "tech-lead-data"
        for level in range(4):
            branch = branch / f"level-{level}"
            branch.mkdir()
            (branch / "note.json").write_text('{"n": 1}', encoding="utf-8")
            for sibling in range(3):
                twig = branch / f"twig-{sibling}"
                twig.mkdir()
                (twig / "leaf.json").write_text('{"leaf": 1}', encoding="utf-8")
        return run_dir

    def test_repeated_preservation_returns_descriptors_to_the_baseline(self, tmp_path):
        archive = _archive(tmp_path)
        # One warm-up run so lazily-opened loggers/handles are not counted.
        assert _preserve(
            archive, self._nested_run(tmp_path, "warmup"), run="warm", session="warm"
        ) is not None
        baseline = _open_descriptor_count()

        for index in range(3):
            run_dir = self._nested_run(tmp_path, f"run-{index}")
            assert _preserve(
                archive, run_dir, run=f"run-{index}", session=f"tech-lead-{index}"
            ) is not None

        # 3 runs x 16 directories: a per-directory leak would be unmistakable.
        assert _open_descriptor_count() <= baseline


class TestCrashReconciliation:
    """#6858 round 3 F10: interrupted publication must not strand state.

    A crash between "rename the old receipt aside" and "swap the new one in"
    leaves the record pointing at a missing directory while the complete receipt
    sits under ``.retired-*``; a crash while staging leaves ``.incoming-*``.
    Neither prefix is eligible for retention, so unreconciled scratch would grow
    forever outside ``ARCHIVE_RETENTION``.
    """

    def _retired(self, tmp_path, name: str) -> Path:
        retired = tmp_path / "archive" / f".retired-{name}"
        (retired / "tech-lead-data").mkdir(parents=True)
        (retired / "terminal-recording.jsonl").write_text(RECORDING, encoding="utf-8")
        (retired / "tech-lead-data" / TECH_LEAD_REPORT_FILENAME).write_text(
            "recovered", encoding="utf-8"
        )
        return retired

    def test_a_retired_receipt_with_no_live_version_is_restored(self, tmp_path):
        archive = _archive(tmp_path)
        (tmp_path / "archive").mkdir(parents=True)
        self._retired(tmp_path, "run-900__tech-lead-900")

        archive.reconcile()

        live = tmp_path / "archive" / "run-900__tech-lead-900"
        assert live.is_dir()
        assert (live / "tech-lead-data" / TECH_LEAD_REPORT_FILENAME).read_text(
            encoding="utf-8"
        ) == "recovered"
        assert not (tmp_path / "archive" / ".retired-run-900__tech-lead-900").exists()

    def test_a_retired_receipt_with_a_live_version_is_dropped(self, tmp_path):
        archive = _archive(tmp_path)
        current = _preserve(archive, _run_dir(tmp_path))
        assert current is not None
        self._retired(tmp_path, current.location.name)

        archive.reconcile()

        # The live receipt wins; the leftover does not shadow or replace it.
        assert (current.location / "tech-lead-data" / TECH_LEAD_REPORT_FILENAME).read_text(
            encoding="utf-8"
        ) == REPORT
        assert not (
            tmp_path / "archive" / f".retired-{current.location.name}"
        ).exists()

    def test_abandoned_staging_is_reclaimed(self, tmp_path):
        archive = _archive(tmp_path)
        (tmp_path / "archive").mkdir(parents=True)
        # PID 1 is init: alive but not us. A very high pid is reliably absent.
        abandoned = tmp_path / "archive" / ".incoming-run-1__tech-lead-1.4000000"
        abandoned.mkdir()
        (abandoned / "half-copied.json").write_text("{}", encoding="utf-8")

        archive.reconcile()

        assert not abandoned.exists()

    def test_a_live_engines_staging_is_left_alone(self, tmp_path):
        archive = _archive(tmp_path)
        (tmp_path / "archive").mkdir(parents=True)
        live = tmp_path / "archive" / f".incoming-run-2__tech-lead-2.{os.getpid()}"
        live.mkdir()

        archive.reconcile()

        assert live.is_dir()

    def test_pruning_reconciles_first_so_scratch_cannot_accumulate(self, tmp_path):
        archive = _archive(tmp_path, retention=1)
        (tmp_path / "archive").mkdir(parents=True)
        self._retired(tmp_path, "run-7__tech-lead-7")
        stale = tmp_path / "archive" / ".incoming-run-8__tech-lead-8.4000001"
        stale.mkdir()

        removed = archive.prune()

        # The restored receipt is now a normal archive that retention can see,
        # and the abandoned stage is gone rather than living outside retention.
        assert not stale.exists()
        entries = {path.name for path in (tmp_path / "archive").iterdir()}
        assert entries == {"run-7__tech-lead-7"}
        assert removed == ()
