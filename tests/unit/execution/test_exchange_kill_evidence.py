"""Retained kill-evidence capture and the composer-state discriminator (#7128).

Every recording here is a hand-built fixture in the real on-disk format
(``{event_type, offset_ms, data_b64}`` NDJSON plus resize rows). No live
providers, no PTYs, no clocks — the recorder takes an injected clock and the
classifier is a pure function of the file.

The discriminator reads the *rendered viewport* (``infra.terminal_viewport``),
so fixtures here paint screens the way a real TUI does: cursor addressing and
erase-in-line, not append-only text (#7141 finding 1).
"""

from __future__ import annotations

import base64
import json
import threading
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
    CapturedKillEvidence,
    INDEX_FILENAME,
    RECORDING_COPY_FILENAME,
    RUN_IDENTITY_FILENAME,
    ExchangeKillEvidenceRecorder,
    RoundIdentity,
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


def _recording(
    path: Path, chunks: Iterable[bytes], *, resize: bool = True, cols: int = 120
) -> Path:
    rows: list[dict[str, object]] = []
    if resize:
        rows.append(
            {
                "schema_version": 1,
                "event_type": "resize",
                "offset_ms": 0,
                "rows": 40,
                "cols": cols,
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
# tag in the transcript; only the footer band differs — which is the whole
# point of the classifier.
_PROMPT_ECHO = (
    b"\x1b[1;1H\x1b[2m> Review-exchange reviewer turn round=2 attempt=1 is ready."
    b"\x1b[0m"
)
_STRANDED_FOOTER = (
    b"\x1b[34;2H\x1b[K  \xe2\x8f\xb5\xe2\x8f\xb5 tab to queue message"
)
_EMPTIED_FOOTER = b"\x1b[34;2H\x1b[K  Working\xe2\x80\xa6 (esc to interrupt)"


def _stranded_recording(tmp_path: Path, name: str = "stranded.jsonl") -> Path:
    return _recording(tmp_path / name, (_PROMPT_ECHO, _STRANDED_FOOTER))


def _emptied_recording(tmp_path: Path, name: str = "emptied.jsonl") -> Path:
    return _recording(tmp_path / name, (_PROMPT_ECHO, _EMPTIED_FOOTER))


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


def _identity(
    tmp_path: Path,
    *,
    recording: Path,
    worktree: Path | None = None,
    respawn_retries: int = 0,
    issue_number: int = 7128,
    role: str = "reviewer",
) -> RoundIdentity:
    exchange_dir = tmp_path / "run" / "exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    return RoundIdentity(
        issue_number=issue_number,
        role=role,
        round_index=2,
        attempt_index=1,
        respawn_retries=respawn_retries,
        session_name="review-exchange-7128-20260829T040000Z",
        exchange_run_id="run-7128-abc",
        agent_pid=4242,
        recording_path=recording,
        run_dir=tmp_path / "run",
        exchange_dir=exchange_dir,
        worktree=worktree if worktree is not None else _main_worktree(tmp_path),
        response_file=tmp_path / "review-response.json",
        prompt_marker="round=2 attempt=1",
    )


def _trace() -> object:
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
    return detector.snapshot(200.0)


def _capture(
    recorder: ExchangeKillEvidenceRecorder, tmp_path: Path, **kwargs: object
) -> CapturedKillEvidence | None:
    """Register a round and capture it the way the round loop does.

    Production always captures through a ticket — that is the arbitration
    token that stops the inner and outer paths both retaining one round — so
    the tests drive the same entry rather than a facts-shaped side door.
    """
    ticket = recorder.round_started(_identity(tmp_path, **kwargs))  # type: ignore[arg-type]
    return recorder.capture_declared_failure(
        ticket,
        failure_reason="prompt_not_accepted",
        error_text="Agent did not produce terminal output for 120.0s",
        idle_trace=_trace(),  # type: ignore[arg-type]
    )


def _recorder(root: Path, **overrides: object) -> ExchangeKillEvidenceRecorder:
    return ExchangeKillEvidenceRecorder(
        resolve_root=lambda _worktree: root,
        clock=lambda: _FROZEN_AT,
        **overrides,  # type: ignore[arg-type]
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
        assert verdict.replayed_from_start is True

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

    def test_a_holding_footer_outranks_a_busy_footer(self, tmp_path: Path) -> None:
        """Both can be on screen at once, and that IS the stranded case.

        "esc to interrupt" only says the agent is busy; "tab to queue message"
        says the composer is holding text the agent never took. A prompt
        stranded while the agent works shows both, so the holding marker wins
        on meaning — not on which was painted last.
        """
        both = _recording(
            tmp_path / "both.jsonl",
            (
                b"\x1b[33;2H\x1b[K  Working (esc to interrupt)",
                b"\x1b[34;2H\x1b[K  tab to queue message",
            ),
        )
        reversed_paint = _recording(
            tmp_path / "both2.jsonl",
            (
                b"\x1b[34;2H\x1b[K  tab to queue message",
                b"\x1b[33;2H\x1b[K  Working (esc to interrupt)",
            ),
        )

        assert (
            classify_composer_state(both).state is ComposerState.COMPOSER_STRANDED
        )
        assert (
            classify_composer_state(reversed_paint).state
            is ComposerState.COMPOSER_STRANDED
        )

    def test_no_marker_is_undetermined_not_a_guess(self, tmp_path: Path) -> None:
        verdict = classify_composer_state(
            _recording(tmp_path / "quiet.jsonl", (b"nothing familiar here",))
        )

        assert verdict.state is ComposerState.UNDETERMINED
        assert verdict.matched_marker is None

    def test_missing_recording_is_undetermined_with_the_path(
        self, tmp_path: Path
    ) -> None:
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
            (b"\xff\xfe\x00", b"\x1b[34;2H\x1b[K  tab to queue message"),
        )

        assert classify_composer_state(path).state is ComposerState.COMPOSER_STRANDED


class TestDiscriminatorRefusesToGuess:
    """#7141 finding 1: a verdict must never come from unreadable history."""

    def test_erased_footer_cannot_produce_a_stranded_verdict(
        self, tmp_path: Path
    ) -> None:
        path = _recording(
            tmp_path / "cleared.jsonl",
            (
                _PROMPT_ECHO,
                _STRANDED_FOOTER,
                b"\x1b[2J\x1b[H",
                b"\x1b[1;1HProvider is streaming a fresh answer now.",
            ),
        )

        verdict = classify_composer_state(path, prompt_marker="round=2 attempt=1")

        assert verdict.state is not ComposerState.COMPOSER_STRANDED
        assert verdict.prompt_marker_present is False

    def test_repainted_footer_row_reflects_the_current_render(
        self, tmp_path: Path
    ) -> None:
        path = _recording(
            tmp_path / "repaint.jsonl", (_STRANDED_FOOTER, _EMPTIED_FOOTER)
        )

        assert classify_composer_state(path).state is ComposerState.COMPOSER_EMPTIED

    def test_half_written_recording_is_undetermined(self, tmp_path: Path) -> None:
        path = _stranded_recording(tmp_path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"schema_version": 1, "event_type": "outp')

        verdict = classify_composer_state(path)

        assert verdict.state is ComposerState.UNDETERMINED
        assert "incomplete" in verdict.evidence_snippet

    def test_unparseable_row_is_undetermined(self, tmp_path: Path) -> None:
        path = _stranded_recording(tmp_path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{ this is not json at all\n")

        assert classify_composer_state(path).state is ComposerState.UNDETERMINED

    def test_content_scrolled_off_the_screen_is_not_matched(
        self, tmp_path: Path
    ) -> None:
        """Only what the viewport still shows can support a verdict."""
        chunks = [_STRANDED_FOOTER]
        chunks.extend(b"filler line %d\r\n" % index for index in range(200))
        path = _recording(tmp_path / "scrolled.jsonl", chunks)

        assert classify_composer_state(path).state is not (
            ComposerState.COMPOSER_STRANDED
        )

    def test_replay_is_bounded_and_says_so(self, tmp_path: Path) -> None:
        path = _recording(
            tmp_path / "big.jsonl",
            (b"x" * 6000, _STRANDED_FOOTER),
        )

        verdict = classify_composer_state(path, replay_bytes=2048)

        assert verdict.replayed_from_start is False
        assert verdict.state is ComposerState.COMPOSER_STRANDED


# ---------------------------------------------------------------------------
# Retained root
# ---------------------------------------------------------------------------


class TestRetainedRootResolution:
    def test_primary_worktree_anchors_on_the_repository_root(
        self, tmp_path: Path
    ) -> None:
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

    def test_capture_refuses_a_volatile_home(self, tmp_path: Path) -> None:
        """No retained root means no capture — never a write into the run dir."""
        plain = tmp_path / "plain"
        plain.mkdir()
        recorder = ExchangeKillEvidenceRecorder(clock=lambda: _FROZEN_AT)

        assert (
            _capture(
                recorder,
                tmp_path,
                recording=_stranded_recording(tmp_path),
                worktree=plain,
            )
            is None
        )


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class TestCaptureArtifacts:
    def test_writes_recording_copy_idle_trace_and_identity(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recording = _stranded_recording(tmp_path)

        captured = _capture(_recorder(root), tmp_path, recording=recording)

        assert captured is not None
        directory = captured.directory
        assert (directory / RECORDING_COPY_FILENAME).read_text(
            encoding="utf-8"
        ) == recording.read_text(encoding="utf-8")
        assert (directory / IDLE_TRACE_FILENAME).exists()
        assert (directory / RUN_IDENTITY_FILENAME).exists()

    def test_directory_name_carries_the_correlation_keys(self, tmp_path: Path) -> None:
        """Naming supports gate→session correlation without mtime archaeology."""
        captured = _capture(_recorder(tmp_path / "retained"), tmp_path, recording=_stranded_recording(tmp_path))

        assert captured is not None
        assert captured.directory.name == (
            f"{_STAMP}__issue-7128__reviewer__round-2-attempt-1-respawn-0"
        )

    def test_identity_pins_branch_head_sha_and_run_paths(self, tmp_path: Path) -> None:
        expected = _identity(tmp_path, recording=_stranded_recording(tmp_path))

        captured = _capture(
            _recorder(tmp_path / "retained"),
            tmp_path,
            recording=expected.recording_path,
        )

        assert captured is not None
        identity = json.loads(
            (captured.directory / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["branch"] == _BRANCH
        assert identity["head_sha"] == _SHA
        assert identity["session_name"] == expected.session_name
        assert identity["exchange_run_id"] == "run-7128-abc"
        assert identity["issue_key"] == "issue-7128"
        assert identity["failure_reason"] == "prompt_not_accepted"
        assert identity["agent_pid"] == 4242
        assert identity["original_recording"] == str(expected.recording_path)
        assert identity["run_dir"] == str(expected.run_dir)
        assert identity["exchange_dir"] == str(expected.exchange_dir)
        assert identity["composer_state"]["state"] == "composer_stranded"
        assert identity["retained_dir"] == str(captured.directory)

    def test_idle_trace_file_holds_the_window_and_trajectory(
        self, tmp_path: Path
    ) -> None:
        captured = _capture(_recorder(tmp_path / "retained"), tmp_path, recording=_stranded_recording(tmp_path))

        assert captured is not None
        payload = json.loads(
            (captured.directory / IDLE_TRACE_FILENAME).read_text(encoding="utf-8")
        )
        trace = payload["idle_trace"]
        assert trace["window_seconds"] == 120.0
        assert trace["poll_iterations"] == 2
        assert trace["bytes_drained_total"] == 64
        assert trace["idle_for_seconds"] == 199.0
        assert payload["idle_trace_unavailable"] is None

    def test_cross_reference_runs_both_ways(self, tmp_path: Path) -> None:
        expected = _identity(tmp_path, recording=_stranded_recording(tmp_path))

        captured = _capture(
            _recorder(tmp_path / "retained"),
            tmp_path,
            recording=expected.recording_path,
        )

        assert captured is not None
        back = expected.exchange_dir / (
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

        _capture(recorder, tmp_path, recording=recording, respawn_retries=0)
        _capture(recorder, tmp_path, recording=recording, respawn_retries=1)

        lines = (root / INDEX_FILENAME).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["respawn_retries"] for line in lines] == [0, 1]

    def test_respawn_retries_get_their_own_capture(self, tmp_path: Path) -> None:
        """Each respawn-retry attempt is a separate kill with its own evidence."""
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)

        first = _capture(recorder, tmp_path, recording=recording)
        second = _capture(recorder, tmp_path, recording=recording, respawn_retries=1)

        assert first is not None and second is not None
        assert first.directory != second.directory
        assert second.directory.name.endswith("respawn-1")

    def test_same_second_collision_gets_a_distinct_directory(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)

        first = _capture(recorder, tmp_path, recording=recording)
        second = _capture(recorder, tmp_path, recording=recording)

        assert first is not None and second is not None
        assert second.directory.name.endswith("-2")


class TestCaptureAtomicity:
    """#7141 finding 3: a capture is all-or-nothing under its final name."""

    def test_a_failed_artifact_write_leaves_no_final_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from issue_orchestrator.execution import exchange_kill_evidence as module

        root = tmp_path / "retained"
        recorder = _recorder(root)
        calls: list[Path] = []
        real_write = module.write_json

        def _flaky(path: Path, payload: dict[str, object]) -> None:
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full on the second artifact")
            real_write(path, payload)

        monkeypatch.setattr(module, "write_json", _flaky)

        assert (
            _capture(recorder, tmp_path, recording=_stranded_recording(tmp_path))
            is None
        )
        assert list(root.iterdir()) == [], "a partial capture must leave nothing behind"

    def test_a_successful_capture_leaves_no_staging_directory(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"

        captured = _capture(_recorder(root), tmp_path, recording=_stranded_recording(tmp_path))

        assert captured is not None
        assert [entry.name for entry in root.iterdir() if entry.is_dir()] == [
            captured.directory.name
        ]
        assert not list(captured.directory.glob("*.part"))

    def test_index_repairs_a_trailing_partial_line_before_appending(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        root.mkdir(parents=True)
        index = root / INDEX_FILENAME
        index.write_text(
            '{"kind": "exchange_kill_evidence", "issue_key": "issue-1"}\n'
            '{"kind": "exchange_kill_evi',
            encoding="utf-8",
        )

        captured = _capture(_recorder(root), tmp_path, recording=_stranded_recording(tmp_path))

        assert captured is not None
        lines = index.read_text(encoding="utf-8").splitlines()
        for line in lines:
            json.loads(line)
        assert len(lines) == 2

    def test_index_leaves_a_well_framed_file_alone(self, tmp_path: Path) -> None:
        root = tmp_path / "retained"
        root.mkdir(parents=True)
        index = root / INDEX_FILENAME
        index.write_text('{"kind": "keep me"}\n', encoding="utf-8")

        _capture(_recorder(root), tmp_path, recording=_stranded_recording(tmp_path))

        lines = index.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0])["kind"] == "keep me"
        assert len(lines) == 2


class TestRecordingCopyRobustness:
    def test_huge_recording_keeps_the_tail_and_stays_valid_ndjson(
        self, tmp_path: Path
    ) -> None:
        chunks = [b"filler %d\r\n" % index for index in range(400)]
        chunks.append(_STRANDED_FOOTER)
        recording = _recording(tmp_path / "huge.jsonl", chunks)

        captured = _capture(
            _recorder(tmp_path / "retained", max_copy_bytes=2_000),
            tmp_path,
            recording=recording,
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

        captured = _capture(_recorder(tmp_path / "retained"), tmp_path, recording=recording)

        assert captured is not None
        copied = (captured.directory / RECORDING_COPY_FILENAME).read_text(
            encoding="utf-8"
        )
        for line in copied.splitlines():
            json.loads(line)

    def test_missing_recording_still_produces_identity_and_idle_trace(
        self, tmp_path: Path
    ) -> None:

        captured = _capture(
            _recorder(tmp_path / "retained"), tmp_path,
            recording=tmp_path / "never-written.jsonl",
        )

        assert captured is not None
        assert not (captured.directory / RECORDING_COPY_FILENAME).exists()
        assert (captured.directory / IDLE_TRACE_FILENAME).exists()
        identity = json.loads(
            (captured.directory / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["recording_present"] is False
        assert identity["composer_state"]["state"] == "undetermined"

    def test_unreadable_recording_still_retains_identity_and_idle_trace(
        self, tmp_path: Path
    ) -> None:
        """A corrupt recording must not cost us the other two artifacts."""
        bogus = tmp_path / "recording-is-a-directory.jsonl"
        bogus.mkdir()

        captured = _capture(_recorder(tmp_path / "retained"), tmp_path, recording=bogus)

        assert captured is not None
        assert (captured.directory / IDLE_TRACE_FILENAME).exists()
        identity = json.loads(
            (captured.directory / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["recording_copy_error"] is not None
        assert identity["composer_state"]["state"] == "undetermined"
        assert not list(captured.directory.glob("*.part"))


class TestCaptureNeverRaisesIntoTheFailurePath:
    def test_unwritable_retained_root_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("this is a file, not a directory", encoding="utf-8")

        with caplog.at_level("ERROR"):
            captured = _capture(_recorder(blocked), tmp_path, recording=_stranded_recording(tmp_path))

        assert captured is None
        assert "kill-evidence" in caplog.text

    def test_a_broken_clock_cannot_mask_the_round_failure(self, tmp_path: Path) -> None:
        def _explode() -> datetime:
            raise RuntimeError("clock exploded")

        recorder = ExchangeKillEvidenceRecorder(
            resolve_root=lambda _worktree: tmp_path / "retained", clock=_explode
        )

        assert (
            _capture(recorder, tmp_path, recording=_stranded_recording(tmp_path))
            is None
        )


# ---------------------------------------------------------------------------
# In-flight registry and the outer kill path
# ---------------------------------------------------------------------------


class TestInFlightRounds:
    """#7141 finding 2: the round that never got to declare its own failure."""

    def test_a_registered_round_is_visible_until_it_finishes(
        self, tmp_path: Path
    ) -> None:
        recorder = _recorder(tmp_path / "retained")
        identity = _identity(tmp_path, recording=_stranded_recording(tmp_path))

        ticket = recorder.round_started(identity)
        assert recorder.in_flight_for(7128) == (identity,)

        recorder.round_finished(ticket)
        assert recorder.in_flight_for(7128) == ()

    def test_round_finished_is_idempotent(self, tmp_path: Path) -> None:
        recorder = _recorder(tmp_path / "retained")
        ticket = recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        recorder.round_finished(ticket)
        recorder.round_finished(ticket)

        assert recorder.in_flight_for(7128) == ()

    def test_teardown_captures_every_in_flight_round_for_the_issue(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)
        recorder.round_started(_identity(tmp_path, recording=recording, role="coder"))
        recorder.round_started(
            _identity(tmp_path, recording=recording, role="reviewer")
        )
        recorder.round_started(
            _identity(tmp_path, recording=recording, issue_number=9999)
        )

        captured = recorder.capture_abandoned_rounds(7128, reason="deadline exceeded")

        assert len(captured) == 2
        roles = {
            json.loads((path / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8"))[
                "role"
            ]
            for path in captured
        }
        assert roles == {"coder", "reviewer"}

    def test_abandoned_capture_records_the_typed_reason_and_cause(
        self, tmp_path: Path
    ) -> None:
        recorder = _recorder(tmp_path / "retained")
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        captured = recorder.capture_abandoned_rounds(
            7128, reason="supervisor deadline exceeded"
        )

        identity = json.loads(
            (captured[0] / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["failure_reason"] == "abandoned_by_teardown"
        assert "supervisor deadline exceeded" in identity["error"]

    def test_a_wedged_round_contributes_its_live_idle_trajectory(
        self, tmp_path: Path
    ) -> None:
        """For a stuck worker the frozen bytes_drained series IS the diagnosis."""
        recorder = _recorder(tmp_path / "retained", monotonic=lambda: 900.0)
        ticket = recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )
        detector = RoundIdleDetector(
            window_seconds=None,
            deadline_seconds=7200.0,
            poll_interval_seconds=0.1,
            round_started_at=0.0,
            activity_since=0.0,
            recording_bytes=0,
        )
        detector.observe(1.0, drained=42, recording_bytes=42)
        detector.observe(600.0, drained=0, recording_bytes=42)
        ticket.attach_detector(detector)

        captured = recorder.capture_abandoned_rounds(7128, reason="deadline")

        payload = json.loads(
            (captured[0] / IDLE_TRACE_FILENAME).read_text(encoding="utf-8")
        )
        assert payload["idle_trace"]["bytes_drained_total"] == 42
        assert payload["idle_trace"]["idle_for_seconds"] == 899.0
        assert payload["idle_trace_unavailable"] is None

    def test_a_round_with_no_detector_yet_says_why_the_trace_is_missing(
        self, tmp_path: Path
    ) -> None:
        recorder = _recorder(tmp_path / "retained")
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        captured = recorder.capture_abandoned_rounds(7128, reason="deadline")

        payload = json.loads(
            (captured[0] / IDLE_TRACE_FILENAME).read_text(encoding="utf-8")
        )
        assert payload["idle_trace"] is None
        assert "no idle detector existed" in payload["idle_trace_unavailable"]

    def test_capture_is_single_shot_per_round(self, tmp_path: Path) -> None:
        """A second teardown must not re-capture a round already retained."""
        recorder = _recorder(tmp_path / "retained")
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        first = recorder.capture_abandoned_rounds(7128, reason="deadline")
        second = recorder.capture_abandoned_rounds(7128, reason="deadline again")

        assert len(first) == 1
        assert second == ()

    def test_no_in_flight_rounds_is_a_quiet_no_op(self, tmp_path: Path) -> None:
        recorder = _recorder(tmp_path / "retained")

        assert recorder.capture_abandoned_rounds(7128, reason="deadline") == ()

    def test_teardown_capture_never_raises(self, tmp_path: Path) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        recorder = _recorder(blocked)
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        assert recorder.capture_abandoned_rounds(7128, reason="deadline") == ()

    def test_registry_is_safe_under_concurrent_rounds(self, tmp_path: Path) -> None:
        recorder = _recorder(tmp_path / "retained")
        recording = _stranded_recording(tmp_path)
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def _churn(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(20):
                    ticket = recorder.round_started(
                        _identity(
                            tmp_path, recording=recording, issue_number=1000 + index
                        )
                    )
                    recorder.round_finished(ticket)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=_churn, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert recorder.in_flight_for(1000) == ()


class TestConcurrentCaptures:
    """Staging + rename + index append must hold up under real contention.

    The capture path grew a hidden staging directory, an atomic rename, and a
    self-healing index append (#7141 finding 3); all three are new places for
    concurrent captures to collide.
    """

    def test_64_concurrent_captures_get_distinct_directories(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)
        barrier = threading.Barrier(64)
        results: list[Path] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _one_capture() -> None:
            try:
                barrier.wait(timeout=30)
                captured = _capture(recorder, tmp_path, recording=recording)
                assert captured is not None
                with lock:
                    results.append(captured.directory)
            except BaseException as exc:  # pragma: no cover - reported below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_one_capture) for _ in range(64)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        assert len(results) == 64
        assert len(set(results)) == 64, "captures collided on one directory"
        for directory in results:
            assert (directory / RUN_IDENTITY_FILENAME).exists()

    def test_64_concurrent_captures_leave_a_parseable_index(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)
        barrier = threading.Barrier(64)
        errors: list[BaseException] = []

        def _one_capture() -> None:
            try:
                barrier.wait(timeout=30)
                _capture(recorder, tmp_path, recording=recording)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=_one_capture) for _ in range(64)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        lines = (root / INDEX_FILENAME).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 64
        for line in lines:
            assert json.loads(line)["kind"] == "exchange_kill_evidence"

    def test_no_staging_directories_survive_concurrent_captures(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)
        threads = [
            threading.Thread(
                target=lambda: _capture(recorder, tmp_path, recording=recording)
            )
            for _ in range(16)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        leftovers = [entry.name for entry in root.iterdir() if entry.name.startswith(".")]
        assert leftovers == []


class TestUndecodableRecordingIsNotTrusted:
    """#7141 round 2 finding 1b: a decode gap means the stream is not sound."""

    def test_invalid_base64_forces_undetermined(self, tmp_path: Path) -> None:
        path = _stranded_recording(tmp_path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                '{"schema_version": 1, "event_type": "output", "offset_ms": 99, '
                '"data_b64": "!!not base64!!"}\n'
            )

        verdict = classify_composer_state(path)

        assert verdict.state is ComposerState.UNDETERMINED
        assert "incomplete" in verdict.evidence_snippet

    def test_a_missing_payload_field_forces_undetermined(self, tmp_path: Path) -> None:
        path = _stranded_recording(tmp_path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"schema_version": 1, "event_type": "output"}\n')

        assert classify_composer_state(path).state is ComposerState.UNDETERMINED

    def test_a_clean_recording_is_still_classified(self, tmp_path: Path) -> None:
        """The guard must not swallow the healthy case."""
        assert (
            classify_composer_state(_stranded_recording(tmp_path)).state
            is ComposerState.COMPOSER_STRANDED
        )


class TestOneCapturePerRound:
    """#7141 round 2 finding 2: inner and outer must not both capture a round."""

    def test_the_outer_capture_wins_and_the_inner_becomes_a_no_op(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)
        identity = _identity(tmp_path, recording=recording)
        ticket = recorder.round_started(identity)

        # Teardown captures while the sources still exist.
        outer = recorder.capture_abandoned_rounds(7128, reason="deadline")
        # The unwinding worker then tries to capture the same round; by now the
        # pair is gone and its recording with it.
        recording.unlink()
        inner = recorder.capture_declared_failure(
            ticket,
            failure_reason="prompt_not_accepted",
            error_text="boom",
            idle_trace=None,
        )

        assert len(outer) == 1
        assert inner is None, "the second capture of one round must be a no-op"
        directories = [entry for entry in root.iterdir() if entry.is_dir()]
        assert len(directories) == 1

    def test_the_back_reference_points_at_the_capture_that_was_kept(
        self, tmp_path: Path
    ) -> None:
        """The reported symptom: the pointer named the evidence-poor copy."""
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)
        identity = _identity(tmp_path, recording=recording)
        ticket = recorder.round_started(identity)

        recorder.capture_abandoned_rounds(7128, reason="deadline")
        recording.unlink()
        recorder.capture_declared_failure(
            ticket,
            failure_reason="prompt_not_accepted",
            error_text="boom",
            idle_trace=None,
        )

        pointer = json.loads(
            (
                identity.exchange_dir
                / "round-2-reviewer-attempt-1-respawn-0.kill-evidence.json"
            ).read_text(encoding="utf-8")
        )
        retained = json.loads(
            (Path(pointer["retained_dir"]) / RUN_IDENTITY_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        assert retained["recording_present"] is True
        assert retained["failure_reason"] == "abandoned_by_teardown"

    def test_the_inner_capture_wins_when_no_teardown_intervenes(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        ticket = recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        inner = recorder.capture_declared_failure(
            ticket,
            failure_reason="prompt_not_accepted",
            error_text="boom",
            idle_trace=None,
        )
        outer = recorder.capture_abandoned_rounds(7128, reason="deadline")

        assert inner is not None
        assert outer == ()
        assert len([entry for entry in root.iterdir() if entry.is_dir()]) == 1

    def test_a_finished_round_cannot_be_captured(self, tmp_path: Path) -> None:
        recorder = _recorder(tmp_path / "retained")
        ticket = recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )
        recorder.round_finished(ticket)

        assert (
            recorder.capture_declared_failure(
                ticket,
                failure_reason="timeout",
                error_text="boom",
                idle_trace=None,
            )
            is None
        )

    def test_concurrent_inner_and_outer_produce_exactly_one_capture(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root)
        recording = _stranded_recording(tmp_path)
        ticket = recorder.round_started(_identity(tmp_path, recording=recording))
        barrier = threading.Barrier(2)
        results: list[object] = []
        lock = threading.Lock()

        def _inner() -> None:
            barrier.wait(timeout=10)
            got = recorder.capture_declared_failure(
                ticket, failure_reason="timeout", error_text="x", idle_trace=None
            )
            with lock:
                results.append(got)

        def _outer() -> None:
            barrier.wait(timeout=10)
            got = recorder.capture_abandoned_rounds(7128, reason="deadline")
            with lock:
                results.append(got)

        threads = [threading.Thread(target=_inner), threading.Thread(target=_outer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert len([entry for entry in root.iterdir() if entry.is_dir()]) == 1


class TestAtomicityEdges:
    """#7141 round 2 finding 3: the gaps either side of the staged write."""

    def test_a_failed_staging_creation_leaves_no_claimed_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final-name claim must be cleaned up if staging never happens."""
        from issue_orchestrator.execution import exchange_kill_evidence as module

        root = tmp_path / "retained"
        recorder = _recorder(root)
        real_mkdir = Path.mkdir
        calls: list[Path] = []

        def _flaky_mkdir(self: Path, *args: object, **kwargs: object) -> None:
            calls.append(self)
            if self.name.startswith(".") and self.name.endswith(module.STAGING_SUFFIX):
                raise OSError("no space left for the staging directory")
            real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _flaky_mkdir)

        assert (
            _capture(recorder, tmp_path, recording=_stranded_recording(tmp_path))
            is None
        )
        monkeypatch.undo()
        assert list(root.iterdir()) == [], "an empty claimed directory survived"

    def test_a_short_index_write_is_repaired_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A committed capture must never leave a torn index behind it."""
        from issue_orchestrator.execution import exchange_kill_evidence as module

        root = tmp_path / "retained"
        recorder = _recorder(root)
        from issue_orchestrator.execution import exchange_kill_artifacts as artifacts

        real_write = artifacts.os.write

        def _short_write(fd: int, data: bytes) -> int:
            return real_write(fd, data[:11])

        monkeypatch.setattr(artifacts.os, "write", _short_write)
        _capture(recorder, tmp_path, recording=_stranded_recording(tmp_path))
        monkeypatch.undo()

        index = root / INDEX_FILENAME
        raw = index.read_text(encoding="utf-8") if index.exists() else ""
        assert raw == "" or raw.endswith("\n"), f"torn index left behind: {raw!r}"
        for line in raw.splitlines():
            json.loads(line)

    def test_a_capture_survives_an_index_failure(self, tmp_path: Path) -> None:
        """The index is a convenience; the artifacts are the evidence."""
        root = tmp_path / "retained"
        root.mkdir(parents=True)
        (root / INDEX_FILENAME).mkdir()  # an index that cannot be written

        captured = _capture(_recorder(root), tmp_path, recording=_stranded_recording(tmp_path))

        assert captured is not None
        assert (captured.directory / RUN_IDENTITY_FILENAME).exists()


class TestCaptureBudget:
    """#7141 round 2 finding 4: teardown capture runs under the registry lock."""

    def test_an_exhausted_budget_abandons_the_capture(self, tmp_path: Path) -> None:
        root = tmp_path / "retained"
        recorder = _recorder(root, capture_budget_seconds=0.0)
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        assert recorder.capture_abandoned_rounds(7128, reason="deadline") == ()

    def test_an_exhausted_budget_says_what_it_abandoned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        recorder = _recorder(tmp_path / "retained", capture_budget_seconds=0.0)
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        with caplog.at_level("WARNING"):
            recorder.capture_abandoned_rounds(7128, reason="deadline")

        assert "budget" in caplog.text.lower()
        assert "round-2-attempt-1-respawn-0" in caplog.text

    def test_a_budget_that_expires_mid_capture_still_retains_identity(
        self, tmp_path: Path
    ) -> None:
        """Partial evidence beats none when the disk is the thing stalling."""
        ticks = iter([0.0, 0.0, 0.0, 500.0, 500.0, 500.0, 500.0, 500.0, 500.0])
        recorder = _recorder(
            tmp_path / "retained",
            capture_budget_seconds=10.0,
            monotonic=lambda: next(ticks, 500.0),
        )
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        captured = recorder.capture_abandoned_rounds(7128, reason="deadline")

        assert len(captured) == 1
        identity = json.loads(
            (captured[0] / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["recording_copy_error"] is not None
        assert "budget" in identity["recording_copy_error"].lower()

    def test_a_generous_budget_does_not_disturb_the_happy_path(
        self, tmp_path: Path
    ) -> None:
        recorder = _recorder(tmp_path / "retained", capture_budget_seconds=600.0)
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        captured = recorder.capture_abandoned_rounds(7128, reason="deadline")

        assert len(captured) == 1
        identity = json.loads(
            (captured[0] / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["recording_present"] is True
        assert identity["recording_copy_error"] is None


class _StepClock:
    """Monotonic stand-in that jumps only when the test says so."""

    def __init__(self, *, jump_after_calls: int | None = None) -> None:
        self._value = 0.0
        self._jump_after = jump_after_calls
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self._jump_after is not None and self.calls > self._jump_after:
            self._value = 1_000.0
        return self._value

    def expire(self) -> None:
        self._value = 1_000.0


class TestReplayRunsUnderTheBudget:
    """#7141 round 3 finding 3: no stage escapes the one budget."""

    def test_a_replay_that_overruns_the_budget_is_not_trusted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from issue_orchestrator.execution import exchange_kill_evidence as module

        root = tmp_path / "retained"
        clock = _StepClock()
        recorder = ExchangeKillEvidenceRecorder(
            resolve_root=lambda _worktree: root,
            clock=lambda: _FROZEN_AT,
            monotonic=clock,
            capture_budget_seconds=10.0,
        )
        real = module.classify_composer_state

        def _stalling(*args: object, **kwargs: object) -> object:
            verdict = real(*args, **kwargs)  # type: ignore[arg-type]
            clock.expire()  # the stage took longer than the budget allowed
            return verdict

        monkeypatch.setattr(module, "classify_composer_state", _stalling)
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        captured = recorder.capture_abandoned_rounds(7128, reason="deadline")

        assert len(captured) == 1
        identity = json.loads(
            (captured[0] / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["composer_state"]["state"] == "undetermined"
        assert "not trusted" in identity["composer_state"]["evidence_snippet"]

    def test_a_replay_abandoned_mid_way_says_so(self, tmp_path: Path) -> None:
        """The reported hole: the replay ran on regardless and was believed."""
        root = tmp_path / "retained"
        # Survive entry, the per-round check and the pre-replay gate, then
        # expire once the replay is consuming events.
        clock = _StepClock(jump_after_calls=3)
        recorder = ExchangeKillEvidenceRecorder(
            resolve_root=lambda _worktree: root,
            clock=lambda: _FROZEN_AT,
            monotonic=clock,
            capture_budget_seconds=10.0,
        )
        chunks = [_STRANDED_FOOTER]
        chunks.extend(b"filler %d\r\n" % index for index in range(200))
        recording = _recording(tmp_path / "long.jsonl", chunks)
        recorder.round_started(_identity(tmp_path, recording=recording))

        captured = recorder.capture_abandoned_rounds(7128, reason="deadline")

        assert len(captured) == 1
        identity = json.loads(
            (captured[0] / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["composer_state"]["state"] == "undetermined"
        assert "abandoned" in identity["composer_state"]["evidence_snippet"]

    def test_the_replay_receives_the_budget_as_its_abort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from issue_orchestrator.execution import exchange_kill_evidence as module

        seen: dict[str, object] = {}

        def _spy(path: Path, **kwargs: object) -> object:
            seen.update(kwargs)
            return module.undetermined_composer_state("spied")

        monkeypatch.setattr(module, "classify_composer_state", _spy)
        recorder = _recorder(tmp_path / "retained")
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        recorder.capture_abandoned_rounds(7128, reason="deadline")

        assert callable(seen["abort"])

    def test_a_healthy_capture_still_gets_its_verdict(self, tmp_path: Path) -> None:
        recorder = _recorder(tmp_path / "retained", capture_budget_seconds=600.0)
        recorder.round_started(
            _identity(tmp_path, recording=_stranded_recording(tmp_path))
        )

        captured = recorder.capture_abandoned_rounds(7128, reason="deadline")

        identity = json.loads(
            (captured[0] / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["composer_state"]["state"] == "composer_stranded"


class TestControlCharactersCannotForgeAMarker:
    """#7141 round 5: a marker split across rows is not on the screen at all."""

    def test_a_nel_inside_the_marker_span_defeats_a_stranded_verdict(
        self, tmp_path: Path
    ) -> None:
        """The reported reproduction: xterm splits the row, so no row matches."""
        footer = "\u001b[34;2H\u001b[K  tab to \u0085queue message".encode("utf-8")
        path = _recording(tmp_path / "nel.jsonl", (footer,))

        verdict = classify_composer_state(path)

        assert verdict.state is not ComposerState.COMPOSER_STRANDED

    def test_the_same_footer_without_the_nel_is_still_stranded(
        self, tmp_path: Path
    ) -> None:
        """Control: the marker still works, so this is not a blanket refusal."""
        footer = "\u001b[34;2H\u001b[K  tab to queue message".encode("utf-8")
        path = _recording(tmp_path / "clean.jsonl", (footer,))

        assert classify_composer_state(path).state is ComposerState.COMPOSER_STRANDED

    def test_a_vertical_tab_inside_the_marker_span_defeats_it_too(
        self, tmp_path: Path
    ) -> None:
        footer = b"\x1b[34;2H\x1b[K  tab to \x0bqueue message"
        path = _recording(tmp_path / "vt.jsonl", (footer,))

        assert (
            classify_composer_state(path).state is not ComposerState.COMPOSER_STRANDED
        )


class TestTerminalModesCannotForgeAMarker:
    """#7141 round 6: DECAWM decides whether the footer gets a row of its own."""

    def test_autowrap_off_defeats_a_stranded_verdict(self, tmp_path: Path) -> None:
        """The reported reproduction: with autowrap off the footer never lands.

        120 X's fill the row and park the cursor; every following character
        overwrites the last cell, so no rendered row contains the marker. The
        old model wrapped anyway and read a verdict off a row xterm never drew.
        """
        overflow = "\u001b[?7l" + "X" * 120 + "tab to queue message"
        path = _recording(tmp_path / "nowrap.jsonl", (overflow.encode("utf-8"),),
                          cols=120)

        verdict = classify_composer_state(path)

        assert verdict.state is not ComposerState.COMPOSER_STRANDED

    def test_autowrap_on_still_finds_the_wrapped_footer(self, tmp_path: Path) -> None:
        """Control: with wrapping on the footer does get a row, and is found."""
        overflow = "X" * 120 + "tab to queue message"
        path = _recording(tmp_path / "wrap.jsonl", (overflow.encode("utf-8"),),
                          cols=120)

        assert classify_composer_state(path).state is ComposerState.COMPOSER_STRANDED

    def test_an_unmodelled_grid_mode_refuses_a_verdict(self, tmp_path: Path) -> None:
        """Origin mode moves the grid and is not modelled, so no verdict."""
        footer = "\u001b[?6h\u001b[34;2H\u001b[K  tab to queue message"
        path = _recording(tmp_path / "decom.jsonl", (footer.encode("utf-8"),))

        verdict = classify_composer_state(path)

        assert verdict.state is ComposerState.UNDETERMINED
        assert "?6h" in verdict.evidence_snippet

    def test_the_modes_real_recordings_use_do_not_refuse(self, tmp_path: Path) -> None:
        """Refusing on cursor-visibility or synchronised output would gut this."""
        footer = (
            "\u001b[?25l\u001b[?2026h\u001b[34;2H\u001b[K  tab to queue message"
            "\u001b[?2026l\u001b[?25h"
        )
        path = _recording(tmp_path / "common.jsonl", (footer.encode("utf-8"),))

        assert classify_composer_state(path).state is ComposerState.COMPOSER_STRANDED
