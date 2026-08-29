"""Retained kill-evidence capture and the composer-state discriminator (#7128).

Every recording here is a hand-built fixture in the real on-disk format
(``{event_type, offset_ms, data_b64}`` NDJSON plus resize rows). No live
providers, no PTYs, no clocks — the recorder takes an injected clock and the
classifier is a pure function of the file.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pytest

from issue_orchestrator.domain.exchange_kill_evidence import (
    ComposerState,
    RoundIdleDetector,
)
from issue_orchestrator.execution.exchange_kill_evidence import (
    IDLE_TRACE_FILENAME,
    INDEX_FILENAME,
    RECORDING_COPY_FILENAME,
    RUN_IDENTITY_FILENAME,
    ExchangeKillEvidenceRecorder,
    RoundKillFacts,
    build_exchange_kill_evidence_recorder,
    classify_composer_state,
    resolve_retained_diagnostics_root,
)

_BRANCH = "kill-evidence-retention-7128"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_FROZEN_AT = datetime(2026, 8, 29, 4, 5, 6, tzinfo=timezone.utc)
_STAMP = "20260829T040506Z"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _recording(path: Path, chunks: Iterable[bytes], *, resize: bool = True) -> Path:
    rows: list[dict[str, object]] = []
    if resize:
        rows.append(
            {
                "schema_version": 1,
                "event_type": "resize",
                "offset_ms": 0,
                "rows": 40,
                "cols": 120,
            }
        )
    for index, chunk in enumerate(chunks, start=1):
        rows.append(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": index * 10,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


# The two screens the discriminator has to tell apart. Both echo the same turn
# tag; only the footer differs — which is the whole point of the classifier.
_PROMPT_ECHO = (
    b"\x1b[2m> Review-exchange reviewer turn round=2 attempt=1 is ready.\x1b[0m\r\n"
)
_STRANDED_TAIL = (
    _PROMPT_ECHO,
    b"\x1b[2m  \xe2\x8f\xb5\xe2\x8f\xb5 tab to queue message\x1b[0m\r\n",
)
_EMPTIED_TAIL = (
    _PROMPT_ECHO,
    b"\x1b[1m\xe2\x8f\xba Compacting conversation\x1b[0m (esc to interrupt)\r\n",
)


def _stranded_recording(tmp_path: Path) -> Path:
    return _recording(tmp_path / "stranded.jsonl", _STRANDED_TAIL)


def _emptied_recording(tmp_path: Path) -> Path:
    return _recording(tmp_path / "emptied.jsonl", _EMPTIED_TAIL)


def _main_worktree(root: Path) -> Path:
    """A checkout shaped like a primary worktree (``.git`` is a directory)."""
    worktree = root / "repo"
    git_dir = worktree / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{_BRANCH}\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / _BRANCH).write_text(f"{_SHA}\n", encoding="utf-8")
    return worktree


def _linked_worktree(root: Path) -> tuple[Path, Path]:
    """A linked worktree (``.git`` is a file) plus the repo root it points at."""
    repo = _main_worktree(root)
    linked = root / "wt" / "agent-worktree"
    linked.mkdir(parents=True)
    admin = repo / ".git" / "worktrees" / "agent-worktree"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "HEAD").write_text(f"ref: refs/heads/{_BRANCH}\n", encoding="utf-8")
    (linked / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return linked, repo


def _facts(
    tmp_path: Path,
    *,
    recording: Path,
    worktree: Path | None = None,
    respawn_retries: int = 0,
    prompt_marker: str = "round=2 attempt=1",
) -> RoundKillFacts:
    exchange_dir = tmp_path / "run" / "exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    detector = RoundIdleDetector(
        window_seconds=120.0,
        deadline_seconds=3600.0,
        poll_interval_seconds=0.1,
        round_started_at=0.0,
        activity_since=0.0,
        recording_bytes=10,
    )
    detector.observe(1.0, drained=64, recording_bytes=74)
    detector.observe(200.0, drained=0, recording_bytes=74)
    return RoundKillFacts(
        issue_number=7128,
        role="reviewer",
        round_index=2,
        attempt_index=1,
        respawn_retries=respawn_retries,
        failure_reason="prompt_not_accepted",
        error_text="Agent did not produce terminal output for 120.0s",
        session_name="review-exchange-7128-20260829T040000Z",
        exchange_run_id="run-7128-abc",
        agent_pid=4242,
        recording_path=recording,
        run_dir=tmp_path / "run",
        exchange_dir=exchange_dir,
        worktree=worktree if worktree is not None else _main_worktree(tmp_path),
        response_file=tmp_path / "review-response.json",
        prompt_marker=prompt_marker,
        idle_trace=detector.snapshot(200.0),
    )


def _recorder(root: Path, **overrides: object) -> ExchangeKillEvidenceRecorder:
    return ExchangeKillEvidenceRecorder(
        root, clock=lambda: _FROZEN_AT, **overrides  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Discriminator
# ---------------------------------------------------------------------------


class TestComposerStateDiscriminator:
    def test_stranded_composer_fixture(self, tmp_path: Path) -> None:
        verdict = classify_composer_state(
            _stranded_recording(tmp_path), prompt_marker="round=2 attempt=1"
        )

        assert verdict.state is ComposerState.COMPOSER_STRANDED
        assert verdict.matched_marker == "queue_message_footer"
        assert "tab to queue message" in verdict.evidence_snippet
        assert verdict.prompt_marker_present is True
        assert verdict.scanned_events == 2

    def test_emptied_composer_fixture(self, tmp_path: Path) -> None:
        verdict = classify_composer_state(
            _emptied_recording(tmp_path), prompt_marker="round=2 attempt=1"
        )

        assert verdict.state is ComposerState.COMPOSER_EMPTIED
        assert verdict.matched_marker == "interrupt_footer"
        assert "to interrupt" in verdict.evidence_snippet

    def test_prompt_echo_alone_does_not_decide(self, tmp_path: Path) -> None:
        """A submitted prompt is *also* echoed, so echo presence is not the signal."""
        verdict = classify_composer_state(
            _recording(tmp_path / "echo.jsonl", (_PROMPT_ECHO,)),
            prompt_marker="round=2 attempt=1",
        )

        assert verdict.state is ComposerState.UNDETERMINED
        assert verdict.prompt_marker_present is True

    def test_latest_marker_wins_when_both_families_appear(self, tmp_path: Path) -> None:
        stranded_then_submitted = _recording(
            tmp_path / "both.jsonl",
            (*_STRANDED_TAIL, *_EMPTIED_TAIL),
        )
        submitted_then_stranded = _recording(
            tmp_path / "both2.jsonl",
            (*_EMPTIED_TAIL, *_STRANDED_TAIL),
        )

        assert (
            classify_composer_state(stranded_then_submitted).state
            is ComposerState.COMPOSER_EMPTIED
        )
        assert (
            classify_composer_state(submitted_then_stranded).state
            is ComposerState.COMPOSER_STRANDED
        )

    def test_no_marker_is_undetermined_not_a_guess(self, tmp_path: Path) -> None:
        verdict = classify_composer_state(
            _recording(tmp_path / "quiet.jsonl", (b"nothing familiar here\r\n",))
        )

        assert verdict.state is ComposerState.UNDETERMINED
        assert verdict.matched_marker is None

    def test_missing_recording_is_undetermined_with_the_path(self, tmp_path: Path) -> None:
        verdict = classify_composer_state(tmp_path / "gone.jsonl")

        assert verdict.state is ComposerState.UNDETERMINED
        assert "gone.jsonl" in verdict.evidence_snippet

    def test_empty_recording_is_undetermined(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")

        assert classify_composer_state(path).state is ComposerState.UNDETERMINED

    def test_non_utf8_pty_bytes_do_not_break_the_decode(self, tmp_path: Path) -> None:
        path = _recording(
            tmp_path / "binary.jsonl",
            (b"\xff\xfe\x00garbage", b"  tab to queue message\r\n"),
        )

        assert classify_composer_state(path).state is ComposerState.COMPOSER_STRANDED

    def test_malformed_rows_and_bad_base64_are_skipped(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "mixed.jsonl", (b"  tab to queue message\r\n",))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event_type": "output", "data_b64": "!!not base64!!"}\n')
            handle.write("{ this is not json at all\n")
            handle.write('{"event_type": "output"}\n')
            handle.write('{"partial": "row with no newline"')

        verdict = classify_composer_state(path)

        assert verdict.state is ComposerState.COMPOSER_STRANDED
        assert verdict.scanned_events == 1

    def test_decode_work_is_bounded_to_the_tail(self, tmp_path: Path) -> None:
        """A huge recording is not fully decoded — only its final events are."""
        chunks = [b"  tab to queue message\r\n"]
        chunks.extend(b"filler line %d\r\n" % index for index in range(2_000))
        chunks.append(b"  working (esc to interrupt)\r\n")
        path = _recording(tmp_path / "huge.jsonl", chunks)
        assert path.stat().st_size > 100_000

        verdict = classify_composer_state(path, tail_events=5)

        assert verdict.scanned_events <= 5
        # The stranded marker is far outside the window, so it must not win.
        assert verdict.state is ComposerState.COMPOSER_EMPTIED

    def test_byte_window_drops_the_partial_leading_row(self, tmp_path: Path) -> None:
        path = _recording(
            tmp_path / "windowed.jsonl",
            (b"x" * 4_000, b"  tab to queue message\r\n"),
        )

        verdict = classify_composer_state(path, tail_bytes=512)

        assert verdict.state is ComposerState.COMPOSER_STRANDED
        assert verdict.scanned_events == 1


# ---------------------------------------------------------------------------
# Retained root resolution
# ---------------------------------------------------------------------------


class TestRetainedRootResolution:
    def test_primary_worktree_anchors_on_the_repository_root(self, tmp_path: Path) -> None:
        worktree = _main_worktree(tmp_path)

        root = resolve_retained_diagnostics_root(worktree)

        assert root == (
            worktree / ".issue-orchestrator" / "diagnostics" / "exchange-kills"
        )

    def test_linked_worktree_retains_outside_the_worktree(self, tmp_path: Path) -> None:
        """The whole point: evidence must outlive the worktree it came from."""
        linked, repo = _linked_worktree(tmp_path)

        root = resolve_retained_diagnostics_root(linked)

        assert root is not None
        assert root == repo / ".issue-orchestrator" / "diagnostics" / "exchange-kills"
        assert linked not in root.parents

    def test_non_git_directory_has_no_retained_home(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        assert resolve_retained_diagnostics_root(plain) is None
        assert build_exchange_kill_evidence_recorder(plain) is None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class TestCaptureArtifacts:
    def test_writes_recording_copy_idle_trace_and_identity(self, tmp_path: Path) -> None:
        root = tmp_path / "retained"
        recording = _stranded_recording(tmp_path)
        facts = _facts(tmp_path, recording=recording)

        captured = _recorder(root).capture(facts)

        assert captured is not None
        directory = captured.directory
        assert (directory / RECORDING_COPY_FILENAME).read_text(
            encoding="utf-8"
        ) == recording.read_text(encoding="utf-8")
        assert (directory / IDLE_TRACE_FILENAME).exists()
        assert (directory / RUN_IDENTITY_FILENAME).exists()

    def test_directory_name_carries_the_correlation_keys(self, tmp_path: Path) -> None:
        """Naming supports gate→session correlation without mtime archaeology."""
        captured = _recorder(tmp_path / "retained").capture(
            _facts(tmp_path, recording=_stranded_recording(tmp_path))
        )

        assert captured is not None
        assert captured.directory.name == (
            f"{_STAMP}__issue-7128__reviewer__round-2-attempt-1-respawn-0"
        )

    def test_identity_pins_branch_head_sha_and_run_paths(self, tmp_path: Path) -> None:
        facts = _facts(tmp_path, recording=_stranded_recording(tmp_path))

        captured = _recorder(tmp_path / "retained").capture(facts)

        assert captured is not None
        identity = json.loads(
            (captured.directory / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["branch"] == _BRANCH
        assert identity["head_sha"] == _SHA
        assert identity["session_name"] == facts.session_name
        assert identity["exchange_run_id"] == "run-7128-abc"
        assert identity["issue_key"] == "issue-7128"
        assert identity["failure_reason"] == "prompt_not_accepted"
        assert identity["agent_pid"] == 4242
        assert identity["original_recording"] == str(facts.recording_path)
        assert identity["run_dir"] == str(facts.run_dir)
        assert identity["exchange_dir"] == str(facts.exchange_dir)
        assert identity["composer_state"]["state"] == "composer_stranded"

    def test_idle_trace_file_holds_the_window_and_trajectory(self, tmp_path: Path) -> None:
        captured = _recorder(tmp_path / "retained").capture(
            _facts(tmp_path, recording=_stranded_recording(tmp_path))
        )

        assert captured is not None
        payload = json.loads(
            (captured.directory / IDLE_TRACE_FILENAME).read_text(encoding="utf-8")
        )
        trace = payload["idle_trace"]
        assert trace["window_seconds"] == 120.0
        assert trace["poll_iterations"] == 2
        assert trace["bytes_drained_total"] == 64
        assert trace["idle_for_seconds"] == 199.0
        assert [s["bytes_drained_total"] for s in trace["samples"]] == [64]

    def test_cross_reference_runs_both_ways(self, tmp_path: Path) -> None:
        facts = _facts(tmp_path, recording=_stranded_recording(tmp_path))

        captured = _recorder(tmp_path / "retained").capture(facts)

        assert captured is not None
        back = facts.exchange_dir / (
            "round-2-reviewer-attempt-1-respawn-0.kill-evidence.json"
        )
        pointer = json.loads(back.read_text(encoding="utf-8"))
        assert pointer["retained_dir"] == str(captured.directory)
        assert pointer["composer_state"] == "composer_stranded"
        identity = json.loads(
            (captured.directory / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["back_reference"] == str(back)

    def test_index_accumulates_one_line_per_capture(self, tmp_path: Path) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)

        recorder.capture(_facts(tmp_path, recording=recording, respawn_retries=0))
        recorder.capture(_facts(tmp_path, recording=recording, respawn_retries=1))

        lines = (root / INDEX_FILENAME).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["respawn_retries"] for line in lines] == [0, 1]

    def test_respawn_retries_get_their_own_capture(self, tmp_path: Path) -> None:
        """Each respawn-retry attempt is a separate kill with its own evidence."""
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)

        first = recorder.capture(_facts(tmp_path, recording=recording))
        second = recorder.capture(
            _facts(tmp_path, recording=recording, respawn_retries=1)
        )

        assert first is not None and second is not None
        assert first.directory != second.directory
        assert second.directory.name.endswith("respawn-1")

    def test_same_second_collision_gets_a_distinct_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)

        first = recorder.capture(_facts(tmp_path, recording=recording))
        second = recorder.capture(_facts(tmp_path, recording=recording))

        assert first is not None and second is not None
        assert second.directory.name.endswith("-2")


class TestRecordingCopyRobustness:
    def test_huge_recording_keeps_the_tail_and_stays_valid_ndjson(
        self, tmp_path: Path
    ) -> None:
        chunks = [b"filler %d\r\n" % index for index in range(400)]
        chunks.append(b"  tab to queue message\r\n")
        recording = _recording(tmp_path / "huge.jsonl", chunks)

        captured = _recorder(tmp_path / "retained", max_copy_bytes=2_000).capture(
            _facts(tmp_path, recording=recording)
        )

        assert captured is not None
        assert captured.recording_truncated is True
        copied = (captured.directory / RECORDING_COPY_FILENAME).read_text(
            encoding="utf-8"
        )
        rows = [json.loads(line) for line in copied.splitlines()]
        assert rows, "the tail must survive truncation"
        assert copied.endswith("\n")
        identity = json.loads(
            (captured.directory / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["recording_source_bytes"] > identity["recording_bytes_copied"]

    def test_half_written_final_row_is_trimmed_from_the_copy(
        self, tmp_path: Path
    ) -> None:
        """The source is open for append; the retained copy still parses."""
        recording = _stranded_recording(tmp_path)
        with recording.open("a", encoding="utf-8") as handle:
            handle.write('{"schema_version": 1, "event_type": "out')

        captured = _recorder(tmp_path / "retained").capture(
            _facts(tmp_path, recording=recording)
        )

        assert captured is not None
        copied = (captured.directory / RECORDING_COPY_FILENAME).read_text(
            encoding="utf-8"
        )
        for line in copied.splitlines():
            json.loads(line)

    def test_missing_recording_still_produces_identity_and_idle_trace(
        self, tmp_path: Path
    ) -> None:
        facts = _facts(tmp_path, recording=tmp_path / "never-written.jsonl")

        captured = _recorder(tmp_path / "retained").capture(facts)

        assert captured is not None
        assert not (captured.directory / RECORDING_COPY_FILENAME).exists()
        assert (captured.directory / IDLE_TRACE_FILENAME).exists()
        identity = json.loads(
            (captured.directory / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["recording_present"] is False
        assert identity["composer_state"]["state"] == "undetermined"

    def test_no_partial_artifact_is_left_under_the_retained_name(
        self, tmp_path: Path
    ) -> None:
        captured = _recorder(tmp_path / "retained").capture(
            _facts(tmp_path, recording=_stranded_recording(tmp_path))
        )

        assert captured is not None
        assert not list(captured.directory.glob("*.part"))


class TestCaptureNeverRaisesIntoTheFailurePath:
    def test_unwritable_retained_root_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("this is a file, not a directory", encoding="utf-8")

        with caplog.at_level("ERROR"):
            captured = _recorder(blocked).capture(
                _facts(tmp_path, recording=_stranded_recording(tmp_path))
            )

        assert captured is None
        assert "kill-evidence" in caplog.text

    def test_unreadable_recording_still_retains_identity_and_idle_trace(
        self, tmp_path: Path
    ) -> None:
        """A corrupt recording must not cost us the other two artifacts."""
        # A directory where a recording file is expected: exists() succeeds but
        # every read raises.
        bogus = tmp_path / "recording-is-a-directory.jsonl"
        bogus.mkdir()

        captured = _recorder(tmp_path / "retained").capture(
            _facts(tmp_path, recording=bogus)
        )

        assert captured is not None
        assert (captured.directory / IDLE_TRACE_FILENAME).exists()
        identity = json.loads(
            (captured.directory / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["recording_copy_error"] is not None
        assert identity["composer_state"]["state"] == "undetermined"
        assert "classification failed" in identity["composer_state"]["evidence_snippet"]
        assert not list(captured.directory.glob("*.part"))

    def test_a_broken_clock_cannot_mask_the_round_failure(self, tmp_path: Path) -> None:
        def _explode() -> datetime:
            raise RuntimeError("clock exploded")

        recorder = ExchangeKillEvidenceRecorder(tmp_path / "retained", clock=_explode)

        assert recorder.capture(
            _facts(tmp_path, recording=_stranded_recording(tmp_path))
        ) is None
