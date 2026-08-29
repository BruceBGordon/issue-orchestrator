"""Pair teardown retains rounds that never declared a failure (#7141 finding 2).

A supervisor wall-clock deadline kills a worker wedged inside ``send_round``.
That round never reaches the round loop's own failure branch, so before this the
whole kill produced no retained evidence at all — the case the supervisor exists
for, and the case the real incidents were.

The capture lives in the pair registry's teardown because the registry is what
destroys the evidence: closing the sessions ends the recording and the release
hook reclaims the reviewer worktree. Putting it there makes "capture before
destroy" structural rather than a comment, and covers every release path
(supervisor deadline, operator cancel, orchestrator shutdown) with one owner.
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pytest

from issue_orchestrator.execution.exchange_kill_evidence import (
    RUN_IDENTITY_FILENAME,
    ExchangeKillEvidenceRecorder,
    RoundIdentity,
)
from issue_orchestrator.execution import (
    persistent_exchange_pair_registry_inmemory as registry_module,
)
from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
    PersistentExchangePair,
)

_BRANCH = "kill-evidence-retention-7128"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_FROZEN_AT = datetime(2026, 8, 29, 4, 5, 6, tzinfo=timezone.utc)


def _recording(path: Path, chunks: Iterable[bytes]) -> Path:
    rows: list[dict[str, object]] = [
        {
            "schema_version": 1,
            "event_type": "resize",
            "offset_ms": 0,
            "rows": 40,
            "cols": 120,
        }
    ]
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


def _worktree(root: Path) -> Path:
    worktree = root / "repo"
    (worktree / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (worktree / ".git" / "HEAD").write_text(
        f"ref: refs/heads/{_BRANCH}\n", encoding="utf-8"
    )
    (worktree / ".git" / "refs" / "heads" / _BRANCH).write_text(
        f"{_SHA}\n", encoding="utf-8"
    )
    return worktree


def _identity(tmp_path: Path, recording: Path) -> RoundIdentity:
    return RoundIdentity(
        issue_number=42,
        role="reviewer",
        round_index=1,
        attempt_index=1,
        respawn_retries=0,
        session_name="review-exchange-42-20260829T040000Z",
        exchange_run_id="run-42-abc",
        agent_pid=999,
        recording_path=recording,
        run_dir=tmp_path / "run",
        exchange_dir=tmp_path / "run" / "exchange",
        worktree=_worktree(tmp_path),
        response_file=tmp_path / "response.json",
        prompt_marker="round=1 attempt=1",
    )


def _recorder(root: Path) -> ExchangeKillEvidenceRecorder:
    return ExchangeKillEvidenceRecorder(
        resolve_root=lambda _worktree: root, clock=lambda: _FROZEN_AT
    )


def _pair(
    tmp_path: Path,
    recording: Path,
    closed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> PersistentExchangePair:
    """A pair whose session close destroys the recording, as the real one does."""

    class _Proc:
        pid = 4242
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
            self.returncode = 0
            return 0

    class _Session:
        def __init__(self, label: str) -> None:
            self.label = label
            self.proc = _Proc()
            self.closed = False
            self.log_writer = None

        @property
        def is_live(self) -> bool:
            return not self.closed

    coder, reviewer = _Session("coder"), _Session("reviewer")

    def _close(session: object) -> int:
        closed.append(session.label)  # type: ignore[attr-defined]
        recording.unlink(missing_ok=True)
        return 0

    monkeypatch.setattr(registry_module, "close_persistent_session", _close)
    return PersistentExchangePair(
        coder_session=coder,  # type: ignore[arg-type]
        reviewer_session=reviewer,  # type: ignore[arg-type]
        reviewer_worktree_path=tmp_path / "reviewer-wt",
        issue_key=42,
        exchange_run_id="run-42-abc",
        run_dir=tmp_path / "run",
        coder_response_path=tmp_path / "coder-response.json",
        reviewer_response_path=tmp_path / "reviewer-response.json",
        reviewer_report_path=tmp_path / "review-report.md",
        coder_recording_path=tmp_path / "coder-rec.jsonl",
        reviewer_recording_path=tmp_path / "reviewer-rec.jsonl",
        coder_completion_path=tmp_path / "completion.json",
        validation_record_path=tmp_path / "validation.json",
        created_at=0.0,
    )


class TestPairTeardownRetainsWedgedRounds:
    def test_a_wedged_round_is_captured_when_the_pair_is_released(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        retained = tmp_path / "retained"
        recorder = _recorder(retained)
        registry = InMemoryPersistentExchangePairRegistry(kill_evidence=recorder)
        recording = _recording(
            tmp_path / "rec.jsonl", (b"\x1b[34;2H\x1b[K  tab to queue message",)
        )
        closed: list[str] = []
        registry.acquire(issue_key=42, spawn=lambda: _pair(tmp_path, recording, closed, monkeypatch))
        identity = _identity(tmp_path, recording)

        wedged = threading.Event()
        release_worker = threading.Event()

        def _worker() -> None:
            # Stands in for a worker stuck inside send_round with the round
            # registered: it never reaches the round loop's failure branch.
            ticket = recorder.round_started(identity)
            wedged.set()
            release_worker.wait(timeout=5)
            recorder.round_finished(ticket)

        thread = threading.Thread(target=_worker)
        thread.start()
        assert wedged.wait(timeout=5)

        registry.release(42, reason="supervisor deadline exceeded")
        release_worker.set()
        thread.join(timeout=5)

        assert closed, "the release must really close the sessions"
        captures = [entry for entry in retained.iterdir() if entry.is_dir()]
        assert len(captures) == 1
        payload = json.loads(
            (captures[0] / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert payload["failure_reason"] == "abandoned_by_teardown"
        assert payload["branch"] == _BRANCH
        assert payload["composer_state"]["state"] == "composer_stranded"

    def test_the_recording_is_copied_before_the_sessions_destroy_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering is structural here, not a comment that can drift."""
        retained = tmp_path / "retained"
        recorder = _recorder(retained)
        registry = InMemoryPersistentExchangePairRegistry(kill_evidence=recorder)
        recording = _recording(
            tmp_path / "rec.jsonl", (b"\x1b[34;2H\x1b[K  tab to queue message",)
        )
        closed: list[str] = []
        registry.acquire(issue_key=42, spawn=lambda: _pair(tmp_path, recording, closed, monkeypatch))
        recorder.round_started(_identity(tmp_path, recording))

        registry.release(42, reason="deadline")

        assert not recording.exists(), "the release must really destroy the source"
        captures = [entry for entry in retained.iterdir() if entry.is_dir()]
        assert (captures[0] / "terminal-recording.jsonl").read_text(
            encoding="utf-8"
        ), "the copy must have been taken while the source still existed"

    def test_shutdown_all_captures_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Orchestrator stop is a kill path like any other."""
        retained = tmp_path / "retained"
        recorder = _recorder(retained)
        registry = InMemoryPersistentExchangePairRegistry(kill_evidence=recorder)
        recording = _recording(tmp_path / "rec.jsonl", (b"x",))
        registry.acquire(issue_key=42, spawn=lambda: _pair(tmp_path, recording, [], monkeypatch))
        recorder.round_started(_identity(tmp_path, recording))

        registry.shutdown_all(reason="orchestrator shutting down")

        assert [entry for entry in retained.iterdir() if entry.is_dir()]

    def test_a_release_with_no_round_in_flight_is_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Normal completion boundaries release pairs constantly."""
        retained = tmp_path / "retained"
        registry = InMemoryPersistentExchangePairRegistry(
            kill_evidence=_recorder(retained)
        )
        recording = _recording(tmp_path / "rec.jsonl", (b"x",))
        registry.acquire(issue_key=42, spawn=lambda: _pair(tmp_path, recording, [], monkeypatch))

        registry.release(42, reason="exchange completed")

        assert not retained.exists() or list(retained.iterdir()) == []

    def test_a_capture_failure_never_blocks_the_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("a file where the retained root should be", encoding="utf-8")
        recorder = _recorder(blocked)
        registry = InMemoryPersistentExchangePairRegistry(kill_evidence=recorder)
        recording = _recording(tmp_path / "rec.jsonl", (b"x",))
        closed: list[str] = []
        registry.acquire(issue_key=42, spawn=lambda: _pair(tmp_path, recording, closed, monkeypatch))
        recorder.round_started(_identity(tmp_path, recording))

        registry.release(42, reason="deadline")

        assert closed, "the teardown must complete even when retention fails"

    def test_a_registry_always_has_a_recorder(self) -> None:
        """No composition can leave the outer kill path unwired."""
        assert InMemoryPersistentExchangePairRegistry().kill_evidence is not None
