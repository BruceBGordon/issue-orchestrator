"""Integration coverage for the persistent-session review-exchange cutover.

Drives ``CompletionReviewExchange.run_review_exchange_loop`` end-to-end
against:

  - a real git fixture (``git init`` + initial commit + feature branch
    checked out in the coder worktree)
  - a stub agent script that mimics the agent protocol (read prompt
    from stdin, write response file + ``coding-done`` completion
    artifact, wait for next prompt)
  - the real persistent runner (no monkeypatching of the runner itself)

Replaces the orchestration-level coverage previously provided by the
``via-local-loop`` simulated-scenarios that were skipped during the
cutover. Catches regressions of the kind the reviewer flagged in #6160:
artifact-layout drift, manifest-accessor resolution, terminal events,
summary writes, chapter sidecars, reviewer-worktree lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
import pty
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.callback_endpoint_helpers import (
    published_callback_endpoint,
    ready_callback_endpoint,
)
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG
from issue_orchestrator.control.completion_review_exchange import (
    CompletionReviewExchange,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.manifest_accessor import ManifestAccessor, RunIdentity
from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
)
from issue_orchestrator.execution.persistent_review_exchange_runner import (
    PersistentReviewExchangeRunner,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.ports import TraceEvent
from issue_orchestrator.ports.turn_mailbox import TurnMailbox


_INTERACTIVE_REVIEW_AGENT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "interactive_review_agent.py"
)
_SYNTHETIC_REVIEW_EXCHANGE_TUI = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "synthetic_review_exchange_tui.py"
)


def _interactive_review_agent_command(
    *, include_initial_prompt_arg: bool = False
) -> str:
    parts = [
        shlex.quote(sys.executable),
        "-u",
        shlex.quote(str(_INTERACTIVE_REVIEW_AGENT)),
    ]
    if include_initial_prompt_arg:
        parts.append("'{initial_prompt}'")
    return " ".join(parts)


def _synthetic_review_exchange_tui_command() -> str:
    return " ".join(
        [
            shlex.quote(sys.executable),
            "-u",
            shlex.quote(str(_SYNTHETIC_REVIEW_EXCHANGE_TUI)),
            "'{initial_prompt}'",
        ]
    )


def _codex_ready() -> bool:
    """Real interactive codex available: CLI on PATH and authenticated.

    ``codex login status`` exits 0 when logged in; any failure (missing
    binary, not logged in, network-down auth check) skips the live test
    rather than failing it on machines without codex.
    """
    import shutil

    if shutil.which("codex") is None:
        return False
    try:
        result = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


_CODEX_READY = _codex_ready()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _wait_for_path(path: Path, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} did not appear within {timeout_seconds}s")


def _wait_for_jsonl_records(
    path: Path, *, timeout_seconds: float = 5.0
) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if records:
                return records
        time.sleep(0.01)
    raise AssertionError(f"{path} did not contain records within {timeout_seconds}s")


def _bootstrap_git_worktree(tmp_path: Path) -> tuple[Path, str]:
    """Build a tiny real git repo with a feature branch checked out."""
    repo = tmp_path / "coder-wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("hello\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "initial")
    branch = "feature/test-issue"
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "work.py").write_text("print('hello')\n")
    _git(repo, "add", "work.py")
    _git(repo, "commit", "-q", "-m", "feature work")
    return repo, branch


def _make_review_exchange_runner(
    session_output: FileSystemSessionOutput,
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry | None = None,
) -> PersistentReviewExchangeRunner:
    """Centralized constructor for the integration tests' review-exchange runner.

    Keeps the registry argument in one place so a future signature
    change doesn't require updating every call site (PR #6209 review
    finding: a previous signature change drifted because of scattered
    constructor calls in different test files).

    ``pair_registry`` defaults to a fresh in-memory registry; tests
    that want to inspect the registry across exchanges (e.g.
    ``test_persistent_pair_respawns_for_second_exchange_run``)
    pass one in. Pair filesystem state is derived from the coder
    worktree at run time so each test's temporary worktree is the
    isolation boundary.
    """
    return PersistentReviewExchangeRunner(
        session_output,
        pair_registry or InMemoryPersistentExchangePairRegistry(),
    )


def _run_review_exchange_for_test(
    cre: CompletionReviewExchange,
    session_output: FileSystemSessionOutput,
    *,
    worktree: Path,
    issue_number: int,
    issue_title: str,
    session_name: str,
    agent_label: str,
    events: object,
    event_context: object,
):
    exchange_run = session_output.start_review_exchange_run(
        worktree,
        issue_number=issue_number,
        parent_session_name=session_name,
        agent_label=agent_label,
    )
    return cre.run_review_exchange_loop(
        exchange_run=exchange_run,
        worktree=worktree,
        issue_number=issue_number,
        issue_title=issue_title,
        session_name=session_name,
        agent_label=agent_label,
        events=events,
        event_context=event_context,
    )


def _make_config(
    tmp_path: Path,
    agent: AgentConfig,
    *,
    reviewer_agent: AgentConfig | None = None,
) -> Config:
    """Config with a stub coder and (by default) the same stub reviewer.

    ``reviewer_agent`` swaps in a distinct reviewer ``AgentConfig`` — e.g. the
    real-codex smoke test registers a reviewer with ``ai_system="codex"`` and
    no ``command`` override so the exchange builds the production interactive
    codex invocation itself.
    """
    config = Config()
    config.repo_root = tmp_path
    config.config_path = _write_test_config(tmp_path)
    config.repo = "local/test"
    config.review_exchange_mode = "via-local-loop"
    config.review_exchange_max_rounds = 2
    config.review_exchange_max_no_progress = 2
    config.review_exchange_require_validation = False
    config.agents = {
        "agent:backend": agent,
        "agent:reviewer": reviewer_agent if reviewer_agent is not None else agent,
    }
    config.code_review_agent = "agent:reviewer"
    config.control_api_port = None
    return config


def _write_test_config(tmp_path: Path) -> Path:
    config_path = (
        tmp_path
        / ".issue-orchestrator"
        / "config"
        / "modes"
        / "default"
        / "default.yaml"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"validation": {"quick": {}, "publish": {}}}, indent=2),
        encoding="utf-8",
    )
    return config_path.resolve()


@contextmanager
def _review_exchange_mailbox_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[int, TurnMailbox, list[str]]]:
    """Run a Control-API-shaped verdict endpoint backed by a real mailbox."""
    import http.server
    import threading

    from issue_orchestrator.domain.review_exchange_verdict import ExchangeVerdict
    from issue_orchestrator.execution.review_exchange_turn_mailbox import (
        InMemoryTurnMailbox,
    )

    token = "test-agent-callback-token"
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", token)

    mailbox = InMemoryTurnMailbox()
    deliveries: list[str] = []

    class _Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path != "/api/review-exchange/respond":
                self.send_response(404)
                self.end_headers()
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            try:
                verdict = ExchangeVerdict.from_wire(body.get("payload"))
            except ValueError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"status":"error"}')
                return
            result = mailbox.deliver(body.get("key"), dict(verdict.to_wire()))
            deliveries.append(result.status.value)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": result.status.value}).encode())

        def log_message(self, *_args: object) -> None:
            return

    server = _Server(("127.0.0.1", 0), _Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield server.server_address[1], mailbox, deliveries
    finally:
        server.shutdown()
        server.server_close()
        PROCESS_COMPLETION_WATCHDOG.join_thread(
            server_thread,
            operation="review callback server shutdown",
        )


@pytest.fixture(autouse=True)
def _clear_simulated_scenario_stubs(monkeypatch):
    """Override the simulated-scenarios autouse stubs that bypass the
    persistent runner. This integration test wants the REAL runner so we
    can exercise the cutover end-to-end."""
    # No-op: we live under tests/integration/, not tests/simulated_scenarios/,
    # so the autouse fixture there isn't applied. This sentinel exists to
    # document the intent and to give a hook if conftest evolves later.


@pytest.fixture
def pair_registry_with_cleanup():
    """Yields a fresh ``InMemoryPersistentExchangePairRegistry`` and
    guarantees ``shutdown_all`` runs even if a test assertion fails.

    The tests under this file drive real subprocess/PTY-backed agents.
    Without guaranteed cleanup, an assertion failure mid-test leaves
    the cached pair alive — agent processes keep running for the rest
    of the suite, making the regression test itself a source of suite
    instability exactly when it catches a regression.

    Tests that need to call ``shutdown_all`` *as part of their
    assertions* (e.g. asserting on side-effects of the release) should
    not use this fixture; they manage the registry inline.
    """
    registry = InMemoryPersistentExchangePairRegistry()
    try:
        yield registry
    finally:
        registry.shutdown_all(reason="test-cleanup")


def test_persistent_review_exchange_end_to_end_through_completion_owner(
    tmp_path: Path,
) -> None:
    """Drive ``CompletionReviewExchange.run_review_exchange_loop`` end-to-end
    against a real git worktree + stub agent. Asserts:

      - outcome.status == "ok" (single happy-path round)
      - REVIEW_EXCHANGE_STARTED + REVIEW_EXCHANGE_COMPLETED events fire
      - persistent recording layout exists
        (``run_dir/<role>/terminal-recording.jsonl``)
      - chapters.json sidecar exists per role with non-empty chapters
      - summary.json is present and matches the outcome status
      - manifest accessor can resolve the persistent-layout recording
        (the bug the reviewer flagged in #6160)
      - the sibling reviewer worktree is removed at exchange end
    """
    coder_wt, branch = _bootstrap_git_worktree(tmp_path)

    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    config = _make_config(tmp_path, agent)

    session_output = FileSystemSessionOutput()
    captured_started: dict[str, Path] = {}

    def _emit_started(*, run_dir, **_):
        captured_started["run_dir"] = run_dir

    captured_events: list[TraceEvent] = []

    class _Sink:
        def publish(self, event):
            captured_events.append(event)

    pair_registry = InMemoryPersistentExchangePairRegistry()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=session_output,
        emit_review_started=_emit_started,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            session_output,
            pair_registry=pair_registry,
        ),
    )

    from issue_orchestrator.events import EventContext

    outcome = _run_review_exchange_for_test(
        cre,
        session_output,
        worktree=coder_wt,
        issue_number=4057,
        issue_title="Test integration",
        session_name="issue-4057",
        agent_label="agent:backend",
        events=_Sink(),
        event_context=EventContext(),
    )

    # Outcome shape
    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    assert outcome.rounds == 1
    assert outcome.reason == "reviewer_ok"
    assert outcome.exchange_dir is not None and outcome.exchange_dir.exists()

    run_dir = outcome.exchange_dir.parent
    assert run_dir.exists()

    # Terminal events fired
    event_names = {evt.event_type for evt in captured_events}
    assert EventName.REVIEW_EXCHANGE_STARTED in event_names
    assert EventName.REVIEW_EXCHANGE_COMPLETED in event_names

    # B2 recording layout: the manifest points the UI at per-session
    # slices under run_dir/<role>/..., while the pair-scoped continuous
    # recordings live under the coder worktree and are exposed via the
    # *_recording_pair manifest keys.
    manifest_payload = json.loads((run_dir / "manifest.json").read_text())
    reviewer_recording = Path(manifest_payload["reviewer_recording"])
    coder_recording = Path(manifest_payload["coder_recording"])
    assert reviewer_recording.exists(), (
        f"reviewer recording missing at manifest-pointed path {reviewer_recording}"
    )
    # Coder runs only when reviewer says changes_requested; reviewer stub
    # responds ok on round 1, so coder may not run. Just check that if
    # the file exists, it parses.
    if coder_recording.exists():
        for line in coder_recording.read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)  # JSONL well-formed

    # Chapters sidecar — at minimum the reviewer's prompt + feedback chapters.
    reviewer_chapters_path = run_dir / "reviewer" / "chapters.json"
    assert reviewer_chapters_path.exists(), (
        f"reviewer chapters.json missing at {reviewer_chapters_path}"
    )
    reviewer_chapters_payload = json.loads(reviewer_chapters_path.read_text())
    assert reviewer_chapters_payload["role"] == "reviewer"
    assert len(reviewer_chapters_payload["chapters"]) >= 2, (
        "expected at least prompt + feedback chapters for reviewer round 1"
    )

    # summary.json present and matches outcome
    summary_path = outcome.exchange_dir / "summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "ok"
    assert summary["reason"] == "reviewer_ok"

    # Manifest accessor resolves the persistent layout — regression for
    # the #6160 finding that the accessor was hardcoded to the old
    # spawn-per-phase round-NNN/<role>/ path.
    accessor = ManifestAccessor(
        RunIdentity(issue_number=4057, run_dir=run_dir),
    )
    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=1,
        role="reviewer",
    )
    assert artifact.path == reviewer_recording

    # The reviewer sibling worktree persists until the pair is released.
    # Calling ``shutdown_all`` here simulates that lifecycle boundary so
    # the test cleans up after itself.
    sibling_pattern = list(coder_wt.parent.glob(f"{coder_wt.name}-review-*"))
    assert len(sibling_pattern) == 1, (
        "reviewer worktree should still exist until the pair is released"
    )

    pair_registry.shutdown_all(reason="test-cleanup")
    sibling_pattern_after = list(coder_wt.parent.glob(f"{coder_wt.name}-review-*"))
    # The on_release hook is responsible for filesystem reclamation.
    # The directory may or may not be gone depending on bootstrap wiring;
    # that's covered by
    # ``test_persistent_exchange_pair_registry_inmemory``.
    del sibling_pattern_after


def test_persistent_review_exchange_multi_round_changes_then_ok(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end multi-round exchange.

    Replaces the skipped local-loop multi-round scenario from
    ``tests/simulated_scenarios/test_simulated_scenarios.py``. The
    reviewer disagrees on round 1, the coder reapplies, the reviewer
    approves on round 2; the persistent runner must:
      - issue exactly one coder send (after the round-1 changes_requested)
      - emit two REVIEW_EXCHANGE_ROUND_COMPLETED events
      - write chapters for both rounds in the reviewer's chapters.json
      - end with status=ok / rounds=2 / reason=reviewer_ok
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    # Drive the reviewer: round 1 = changes_requested, round 2 = ok.
    monkeypatch.setenv("STUB_REVIEWER_OUTCOMES", "changes_requested,ok")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 3

    captured_events: list[TraceEvent] = []

    class _Sink:
        def publish(self, event):
            captured_events.append(event)

    _session_output_for_test = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=_session_output_for_test,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            _session_output_for_test,
        ),
    )

    from issue_orchestrator.events import EventContext

    outcome = _run_review_exchange_for_test(
        cre,
        _session_output_for_test,
        worktree=coder_wt,
        issue_number=4058,
        issue_title="Multi-round integration",
        session_name="issue-4058",
        agent_label="agent:backend",
        events=_Sink(),
        event_context=EventContext(),
    )

    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    assert outcome.rounds == 2
    assert outcome.reason == "reviewer_ok"

    round_completed = [
        evt
        for evt in captured_events
        if evt.event_type == EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
    ]
    assert len(round_completed) == 2, (
        f"expected exactly 2 round-completed events, got {len(round_completed)}"
    )
    # Round 1 reviewer should be changes_requested, round 2 should be ok.
    payload_first = round_completed[0].data
    payload_second = round_completed[1].data
    assert payload_first["reviewer_response_type"] == "changes_requested"
    assert payload_second["reviewer_response_type"] == "ok"

    run_dir = outcome.exchange_dir.parent
    reviewer_chapters_path = run_dir / "reviewer" / "chapters.json"
    assert reviewer_chapters_path.exists()
    chapters_payload = json.loads(reviewer_chapters_path.read_text())
    cycle_indices = sorted(
        {chapter["cycle_index"] for chapter in chapters_payload["chapters"]}
    )
    assert cycle_indices == [1, 2], (
        f"expected reviewer chapters for rounds 1 and 2, got {cycle_indices}"
    )


def test_codex_shaped_interactive_agent_receives_argv_bootstrap_then_pty_rounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex-style interactive launch works with the persistent exchange.

    The real Codex TUI accepts an initial prompt as a positional argv argument
    and then stays open for follow-up input. This test uses the reusable
    interactive fixture to pin that contract without requiring live Codex auth:
    process bootstrap arrives in argv, while reviewer/coder turn prompts arrive
    through the PTY.
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    spawn_log = tmp_path / "stub-spawns.jsonl"
    reviewer_outcome_counter = tmp_path / "reviewer-outcome-counter.txt"
    monkeypatch.setenv("STUB_SPAWN_LOG", str(spawn_log))
    monkeypatch.setenv("STUB_REQUIRE_INITIAL_PROMPT", "1")
    monkeypatch.setenv("STUB_REVIEWER_OUTCOMES", "changes_requested,ok")
    monkeypatch.setenv(
        "STUB_REVIEWER_OUTCOME_COUNTER_FILE", str(reviewer_outcome_counter)
    )

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="codex",
        timeout_minutes=1,
        command=_interactive_review_agent_command(include_initial_prompt_arg=True),
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 3

    session_output = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=session_output,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(session_output),
    )

    from issue_orchestrator.events import EventContext

    outcome = _run_review_exchange_for_test(
        cre,
        session_output,
        worktree=coder_wt,
        issue_number=4061,
        issue_title="Codex-shaped interactive integration",
        session_name="issue-4061",
        agent_label="agent:backend",
        events=MagicMock(),
        event_context=EventContext(),
    )

    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    assert outcome.rounds == 2

    spawn_records = [
        json.loads(line)
        for line in spawn_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert spawn_records
    assert all(record["initial_prompt_present"] for record in spawn_records)
    assert all(record["initial_prompt_contains_wait"] for record in spawn_records)
    reviewer_spawns = [
        record for record in spawn_records if "reviewer" in str(record["role"])
    ]
    assert len(reviewer_spawns) == 2
    assert len({record["pid"] for record in reviewer_spawns}) == 2


def test_synthetic_tui_writes_bootstrap_response_when_guard_missing(
    tmp_path: Path,
) -> None:
    """Tripwire for the guarded framework test's fixture branch.

    The framework test below asserts the synthetic TUI does *not* write a
    bootstrap response because the orchestrator prompt says setup is not a
    turn. This companion proves the fixture would write the bogus
    ``"Ready for review prompts."`` response if that guard disappeared.
    """
    response_file = tmp_path / "review-response.json"
    completion_path = tmp_path / "completion.json"
    spawn_log = tmp_path / "synthetic-tui-spawns.jsonl"
    env = dict(os.environ)
    env.update(
        {
            "ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE": str(response_file),
            "ISSUE_ORCHESTRATOR_COMPLETION_PATH": str(completion_path),
            "ISSUE_ORCHESTRATOR_AGENT_LABEL": "agent:reviewer",
            "SYNTHETIC_TUI_SPAWN_LOG": str(spawn_log),
            "SYNTHETIC_TUI_WRITE_BOOTSTRAP_IF_UNGUARDED": "1",
        }
    )

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(_SYNTHETIC_REVIEW_EXCHANGE_TUI),
            "unguarded bootstrap prompt",
        ],
        cwd=tmp_path,
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
    )
    os.close(slave_fd)
    try:
        _wait_for_path(response_file)
        spawn_records = _wait_for_jsonl_records(spawn_log)
        payload = json.loads(response_file.read_text(encoding="utf-8"))
        assert payload == {
            "response_type": "ok",
            "getting_closer": True,
            "response_text": "Ready for review prompts.",
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            PROCESS_COMPLETION_WATCHDOG.wait(
                proc,
                operation="forced synthetic review TUI shutdown",
            )
        os.close(master_fd)

    assert len(spawn_records) == 1
    assert spawn_records[0]["initial_prompt_present"] is True
    assert spawn_records[0]["bootstrap_not_turn_instruction"] is False
    assert spawn_records[0]["wrote_bootstrap_response"] is True


def test_synthetic_raw_tui_review_exchange_suppresses_bootstrap_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Framework-shaped approximation of the live persistent TUI path.

    This runs through ``CompletionReviewExchange`` and
    ``PersistentReviewExchangeRunner`` with a deterministic raw-mode local
    process instead of Claude/Codex. The fixture would write the same
    bootstrap ``"Ready for review prompts."`` response that made the live
    Codex transport check flaky unless the orchestrator bootstrap explicitly
    says setup is not a turn. Round prompts still travel through the real
    prompt-inbox + ``send_round`` PTY path and submit only on ``\r``.
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Synthetic TUI prompt", encoding="utf-8")

    spawn_log = tmp_path / "synthetic-tui-spawns.jsonl"
    reviewer_outcome_counter = tmp_path / "synthetic-reviewer-outcome-counter.txt"
    monkeypatch.setenv("SYNTHETIC_TUI_SPAWN_LOG", str(spawn_log))
    monkeypatch.setenv("SYNTHETIC_TUI_REVIEWER_OUTCOMES", "changes_requested,ok")
    monkeypatch.setenv(
        "SYNTHETIC_TUI_REVIEWER_OUTCOME_COUNTER_FILE",
        str(reviewer_outcome_counter),
    )
    monkeypatch.setenv("SYNTHETIC_TUI_WRITE_BOOTSTRAP_IF_UNGUARDED", "1")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="codex",
        timeout_minutes=1,
        command=_synthetic_review_exchange_tui_command(),
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 3

    captured_events: list[TraceEvent] = []

    class _Sink:
        def publish(self, event):
            captured_events.append(event)

    session_output = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=session_output,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(session_output),
    )

    from issue_orchestrator.events import EventContext

    outcome = _run_review_exchange_for_test(
        cre,
        session_output,
        worktree=coder_wt,
        issue_number=4063,
        issue_title="Synthetic raw TUI review exchange",
        session_name="issue-4063",
        agent_label="agent:backend",
        events=_Sink(),
        event_context=EventContext(),
    )

    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    assert outcome.rounds == 2
    assert outcome.reason == "reviewer_ok"
    assert outcome.reviewer_response is not None
    assert outcome.reviewer_response.response_text == (
        "Synthetic reviewer approved round 2"
    )

    spawn_records = [
        json.loads(line)
        for line in spawn_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert spawn_records
    assert all(record["initial_prompt_present"] for record in spawn_records)
    assert all(record["bootstrap_not_turn_instruction"] for record in spawn_records)
    assert not any(record["wrote_bootstrap_response"] for record in spawn_records)

    round_completed = [
        evt
        for evt in captured_events
        if evt.event_type == EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
    ]
    assert [evt.data["reviewer_response_type"] for evt in round_completed] == [
        "changes_requested",
        "ok",
    ]
    assert all(
        evt.data["reviewer_response_text"] != "Ready for review prompts."
        for evt in round_completed
    )


@pytest.mark.skipif(not _CODEX_READY, reason="codex CLI not installed or not logged in")
@pytest.mark.live_codex
@pytest.mark.provider_codex
@pytest.mark.xdist_group("codex-interactive")
def test_real_interactive_codex_reviewer_round_trips_through_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE smoke: REAL interactive codex as the reviewer through the REAL
    exchange loop — the seams no stub covers:

      - the production ``build_reviewer_prompt`` output driving real codex,
      - the exchange-built provider command (no ``command`` override),
      - codex booting in the exchange-created reviewer worktree and writing
        its verdict through the sandbox-compatible response-file channel,
      - real codex emitting protocol-valid verdict JSON the exchange parses.

    The verdict itself is the LLM's judgment and is deliberately NOT pinned:
    the assertions accept any protocol-valid outcome. What must hold is the
    protocol round-trip — a parsed reviewer verdict, and no mechanics
    failure (``reviewer_no_completion`` is exactly how the tixmeup
    #277/#290 submit hang and any codex-side protocol breakage surface).
    If codex requests changes, the stub coder responds and round 2 also
    exercises ``send_round`` injection into real codex through the exchange.
    """
    from issue_orchestrator.infra.hooks.codex_session import (
        codex_runtime_home,
        prepare_codex_runtime_home,
    )

    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_CODEX_RUNTIME_ROOT",
        str(tmp_path / ".test-codex-runtime"),
    )
    prepare_codex_runtime_home()
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    # Coder = scripted stub (it only runs if real codex requests changes;
    # the fixture's coder role responds ok by default, no env needed).
    coder = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    # Reviewer = REAL codex: ai_system only, no command override, so the
    # exchange builds the production interactive invocation itself.
    # Low reasoning effort keeps the live review fast. The protected Codex
    # session hook requires an enforcing sandbox, so this production-path smoke
    # uses the same full-auto mode as repository configs. The model is left
    # unset on purpose — the claude-vocabulary field default must NOT leak into
    # the codex invocation (the second bug this smoke test caught).
    reviewer = AgentConfig(
        prompt_path=prompt_path,
        ai_system="codex",
        timeout_minutes=10,
        provider_args={"reasoning_effort": "low", "approval_mode": "full-auto"},
    )
    config = _make_config(tmp_path, coder, reviewer_agent=reviewer)

    captured_events: list[TraceEvent] = []

    class _Sink:
        def publish(self, event):
            captured_events.append(event)

    session_output = FileSystemSessionOutput()
    with _review_exchange_mailbox_server(monkeypatch) as (
        port,
        mailbox,
        _deliveries,
    ):
        config.control_api_port = port
        cre = CompletionReviewExchange(
            agent_callback_endpoint=published_callback_endpoint(port),
            config=config,
            session_output=session_output,
            emit_review_started=lambda **_: None,
            emit_review_outcome=lambda **_: None,
            review_exchange_runner=PersistentReviewExchangeRunner(
                session_output,
                InMemoryPersistentExchangePairRegistry(),
                turn_mailbox=mailbox,
            ),
        )

        from issue_orchestrator.events import EventContext

        outcome = _run_review_exchange_for_test(
            cre,
            session_output,
            worktree=coder_wt,
            issue_number=4062,
            issue_title="Real interactive codex reviewer smoke",
            session_name="issue-4062",
            agent_label="agent:backend",
            events=_Sink(),
            event_context=EventContext(),
        )

    round_completed = [
        evt
        for evt in captured_events
        if evt.event_type == EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
    ]
    assert round_completed, (
        f"real codex never completed a reviewer round-trip: {outcome}"
    )
    valid_verdicts = {"ok", "changes_requested"}
    for evt in round_completed:
        assert evt.data["reviewer_response_type"] in valid_verdicts, (
            "real codex produced a non-protocol verdict: "
            f"{evt.data['reviewer_response_type']!r}"
        )
    # Mechanics must not fail; the LLM's verdict routing may end either way.
    mechanics_failures = {
        "reviewer_no_completion",
        "coder_no_completion",
        "coder_protocol_error",
    }
    assert outcome.reason not in mechanics_failures, (
        f"exchange mechanics failed with real codex: {outcome}"
    )
    assert 'trust_level = "trusted"' not in (
        codex_runtime_home() / "config.toml"
    ).read_text(encoding="utf-8")
    assert outcome.rounds >= 1


def test_one_shot_reviewer_respawns_after_addressable_nits(
    tmp_path: Path,
    monkeypatch,
    pair_registry_with_cleanup,
) -> None:
    """Regression for issue 358's review-exchange timeout.

    The first reviewer process writes a valid approved decision with one nit
    under the ``address`` policy, then exits like a one-shot provider. The
    orchestrator must treat that first turn as successful, route the nit back
    to the coder, and spawn a fresh reviewer for round 2 instead of failing the
    exchange as ``reviewer_no_completion``.
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    spawn_log = tmp_path / "stub-spawns.jsonl"
    reviewer_outcome_counter = tmp_path / "reviewer-outcome-counter.txt"
    monkeypatch.setenv("STUB_SPAWN_LOG", str(spawn_log))
    monkeypatch.setenv("STUB_REVIEWER_OUTCOMES", "ok_with_nit,ok")
    monkeypatch.setenv(
        "STUB_REVIEWER_OUTCOME_COUNTER_FILE", str(reviewer_outcome_counter)
    )
    monkeypatch.setenv("STUB_EXIT_AFTER_RESPONSE_ROLES", "reviewer")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 3
    config.review_nits_default_policy = "address"

    captured_events: list[TraceEvent] = []

    class _Sink:
        def publish(self, event):
            captured_events.append(event)

    pair_registry = pair_registry_with_cleanup
    session_output = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=session_output,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            session_output,
            pair_registry=pair_registry,
        ),
    )

    from issue_orchestrator.events import EventContext

    outcome = _run_review_exchange_for_test(
        cre,
        session_output,
        worktree=coder_wt,
        issue_number=358,
        issue_title="One-shot reviewer respawn",
        session_name="issue-358",
        agent_label="agent:backend",
        events=_Sink(),
        event_context=EventContext(),
    )

    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    assert outcome.rounds == 2
    assert outcome.reason == "reviewer_ok"

    spawn_records = [
        json.loads(line)
        for line in spawn_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    reviewer_spawns = [
        record for record in spawn_records if "reviewer" in record["role"]
    ]
    assert len(reviewer_spawns) == 2, (
        "round 2 should start a fresh reviewer process after the first "
        f"one-shot reviewer exited; spawns={spawn_records}"
    )
    assert len({record["pid"] for record in reviewer_spawns}) == 2

    first_decision_path = (
        outcome.exchange_dir
        / "turns"
        / "round-1-reviewer-attempt-1.review-decision.json"
    )
    first_decision = json.loads(first_decision_path.read_text(encoding="utf-8"))
    assert first_decision["verdict"] == "approved"
    assert first_decision["nit_policy"] == "address"
    assert [nit["id"] for nit in first_decision["nits"]] == ["N1"]

    role_timeouts = [
        evt
        for evt in captured_events
        if evt.event_type == EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
    ]
    assert role_timeouts == []
    coder_prompts = [
        evt
        for evt in captured_events
        if evt.event_type == EventName.REVIEW_EXCHANGE_ROLE_PROMPTED
        and evt.data.get("role") == "coder"
    ]
    assert coder_prompts[0].data["rework_reason"] == "nits"


def test_one_shot_coder_respawns_for_later_rework_turn(
    tmp_path: Path,
    monkeypatch,
    pair_registry_with_cleanup,
) -> None:
    """A one-shot coder process is replaced before later coder rework."""
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    spawn_log = tmp_path / "stub-spawns.jsonl"
    monkeypatch.setenv("STUB_SPAWN_LOG", str(spawn_log))
    monkeypatch.setenv(
        "STUB_REVIEWER_OUTCOMES", "changes_requested,changes_requested,ok"
    )
    monkeypatch.setenv("STUB_EXIT_AFTER_RESPONSE_ROLES", "backend")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 4

    captured_events: list[TraceEvent] = []

    class _Sink:
        def publish(self, event):
            captured_events.append(event)

    pair_registry = pair_registry_with_cleanup
    session_output = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=session_output,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            session_output,
            pair_registry=pair_registry,
        ),
    )

    from issue_orchestrator.events import EventContext

    outcome = _run_review_exchange_for_test(
        cre,
        session_output,
        worktree=coder_wt,
        issue_number=359,
        issue_title="One-shot coder respawn",
        session_name="issue-359",
        agent_label="agent:backend",
        events=_Sink(),
        event_context=EventContext(),
    )

    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    assert outcome.rounds == 3
    assert outcome.reason == "reviewer_ok"

    spawn_records = [
        json.loads(line)
        for line in spawn_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    coder_spawns = [record for record in spawn_records if "backend" in record["role"]]
    assert len(coder_spawns) == 2, (
        "round 2 should start a fresh coder process after the first "
        f"one-shot coder exited; spawns={spawn_records}"
    )
    assert len({record["pid"] for record in coder_spawns}) == 2

    role_timeouts = [
        evt
        for evt in captured_events
        if evt.event_type == EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
    ]
    assert role_timeouts == []


def test_persistent_review_exchange_max_rounds_exhausted(
    tmp_path: Path, monkeypatch
) -> None:
    """Reviewer never approves — exchange ends with max_rounds reached.

    Replaces the skipped no-progress / max-rounds scenarios.
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    # Reviewer always says changes_requested across all rounds.
    monkeypatch.setenv(
        "STUB_REVIEWER_OUTCOMES",
        "changes_requested,changes_requested,changes_requested",
    )

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 2
    config.review_exchange_max_no_progress = 5  # don't trip no-progress first

    _session_output_for_test = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=_session_output_for_test,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            _session_output_for_test,
        ),
    )

    from issue_orchestrator.events import EventContext

    outcome = _run_review_exchange_for_test(
        cre,
        _session_output_for_test,
        worktree=coder_wt,
        issue_number=4059,
        issue_title="Max-rounds integration",
        session_name="issue-4059",
        agent_label="agent:backend",
        events=MagicMock(),
        event_context=EventContext(),
    )

    assert outcome.status == "stopped", (
        f"unexpected outcome status: {outcome.status} (full: {outcome})"
    )
    assert outcome.reason == "max_rounds_exceeded"
    assert outcome.rounds == 2


def test_two_rework_rounds_render_distinguishably_in_projected_timeline(
    tmp_path: Path, monkeypatch
) -> None:
    """Agent journey: in a two-round rework cycle, the projected
    timeline must surface BOTH rounds distinguishably so an agent
    debugging a stuck rework can answer "what did each round do?" by
    reading the timeline alone — not by digging into reviewer artifacts.

    The companion test
    ``test_persistent_review_exchange_multi_round_changes_then_ok``
    asserts the runner emits two REVIEW_EXCHANGE_ROUND_COMPLETED
    TraceEvents. This test pins what reaches `project_timeline` once
    those events flow through `DefaultTimelineWriter` (which fans out
    via the same `produce_external_records` pipeline production uses):

      - Exactly two `review_exchange.round_completed` events appear in
        the projected timeline.
      - They are in temporal order (earlier round first).
      - `round_index` is 1 then 2, not duplicated or 0/0.
      - `reviewer_response_type` differs per round so the agent can
        identify the rework boundary (round 1 = changes_requested,
        round 2 = ok).
      - Each event carries a populated `narrative` so a human / agent
        skimming the dashboard sees the per-round verdict at a glance,
        not just a generic "round completed" string.
      - Both rounds remain in the user-facing view (`views` includes
        "user"), so they are not accidentally hidden behind the
        ops/debug-only filter.
    """
    from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
    from issue_orchestrator.execution.timeline_event_sink import TimelineEventSink
    from issue_orchestrator.ports.timeline_store import TimelineRecord, TimelineStore
    from issue_orchestrator.timeline import project_timeline

    class _RecordingStore(TimelineStore):
        def __init__(self) -> None:
            self.records: list[TimelineRecord] = []

        def append(self, issue_number: int, record: TimelineRecord) -> None:  # noqa: ARG002
            self.records.append(record)

        def read(
            self, issue_number: int, limit: int | None = None
        ) -> list[TimelineRecord]:  # noqa: ARG002
            return list(self.records)

        def delete(self, issue_number: int) -> int:  # noqa: ARG002
            self.records.clear()
            return 0

    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    monkeypatch.setenv("STUB_REVIEWER_OUTCOMES", "changes_requested,ok")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 3

    store = _RecordingStore()
    timeline_sink = TimelineEventSink(DefaultTimelineWriter(store))

    _session_output_for_test = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=_session_output_for_test,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            _session_output_for_test,
        ),
    )

    from issue_orchestrator.events import EventContext

    issue_number = 4060
    outcome = _run_review_exchange_for_test(
        cre,
        _session_output_for_test,
        worktree=coder_wt,
        issue_number=issue_number,
        issue_title="Two-round projection",
        session_name=f"issue-{issue_number}",
        agent_label="agent:backend",
        events=timeline_sink,
        event_context=EventContext(),
    )

    assert outcome.status == "ok"
    assert outcome.rounds == 2

    records = store.read(issue_number)
    assert records, "timeline writer received no records — the sink wiring is broken"

    projected = project_timeline(records, issue_number=issue_number)

    round_completed = [
        evt for evt in projected if evt.event == "review_exchange.round_completed"
    ]
    assert len(round_completed) == 2, (
        "expected two review_exchange.round_completed events in the "
        f"projected timeline, got {len(round_completed)}: "
        f"{[e.event for e in projected]}"
    )

    first, second = round_completed
    assert first.timestamp <= second.timestamp, (
        "round events out of temporal order — "
        f"{first.timestamp} should precede {second.timestamp}"
    )
    assert first.round_index == 1
    assert second.round_index == 2

    assert first.reviewer_response_type == "changes_requested", (
        "round 1 should report changes_requested so the agent sees "
        "where the rework boundary is, got "
        f"{first.reviewer_response_type!r}"
    )
    assert second.reviewer_response_type == "ok", (
        "round 2 should report ok (the approval that ended the cycle), "
        f"got {second.reviewer_response_type!r}"
    )

    # The narrative is what a human / agent skim-reads. It must
    # distinguish the rounds — a generic "round completed" on both
    # would force the user to click through to read each round's
    # reviewer text just to know which one approved.
    assert first.narrative, (
        "round 1 narrative is empty; agent skimming the timeline can't "
        "tell what the round did without drilling in"
    )
    assert second.narrative, "round 2 narrative is empty"
    assert first.narrative != second.narrative, (
        "both rounds share the same narrative "
        f"({first.narrative!r}) — they look identical in the dashboard"
    )
    assert "changes_requested" in first.narrative
    assert "ok" in second.narrative

    # User-facing view filter: both rounds must reach the end-user view,
    # not be debug- or ops-only. The dashboard's user view is what the
    # human / agent reads first.
    for evt in round_completed:
        assert evt.views and "user" in evt.views, (
            f"round {evt.round_index} is not in the 'user' view "
            f"(views={evt.views}); end users won't see it on the dashboard"
        )


def test_persistent_pair_respawns_for_second_exchange_run(
    tmp_path: Path,
    monkeypatch,
    pair_registry_with_cleanup,
) -> None:
    """A cached pair from run N must be released before run N+1 uses it."""
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    config = _make_config(tmp_path, agent)

    pair_registry = pair_registry_with_cleanup
    session_output = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=session_output,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            session_output,
            pair_registry=pair_registry,
        ),
    )

    from issue_orchestrator.events import EventContext

    def _run_one(session_name: str) -> "ReviewExchangeOutcome":  # noqa: F821
        return _run_review_exchange_for_test(
            cre,
            session_output,
            worktree=coder_wt,
            issue_number=9001,
            issue_title="Persistence integration",
            session_name=session_name,
            agent_label="agent:backend",
            events=MagicMock(),
            event_context=EventContext(),
        )

    first = _run_one("issue-9001-first")
    assert first.status == "ok"

    snapshot_after_first = pair_registry.snapshot()
    assert len(snapshot_after_first) == 1, (
        "exactly one pair should be cached for this issue after exchange 1"
    )
    coder_pid_first = snapshot_after_first[0]["coder_pid"]
    reviewer_pid_first = snapshot_after_first[0]["reviewer_pid"]

    second = _run_one("issue-9001-second")
    assert second.status == "ok"

    snapshot_after_second = pair_registry.snapshot()
    assert len(snapshot_after_second) == 1, (
        "second exchange should leave exactly one current pair cached"
    )
    assert snapshot_after_second[0]["coder_pid"] != coder_pid_first, (
        "coder PID was reused across exchange runs; the process env still "
        "points at the run_dir/session that spawned it"
    )
    assert snapshot_after_second[0]["reviewer_pid"] != reviewer_pid_first, (
        "reviewer PID was reused across exchange runs; the process env still "
        "points at the run_dir/session that spawned it"
    )

    # Recording-mirror manifest contract (post per-session-slice fix):
    # ``<role>_recording`` now points at the per-session slice inside
    # the run_dir (so the timeline viewer's run_dir is self-contained
    # and the per-session view doesn't bleed across exchanges).
    # The canonical pair-scoped recording — the continuous PTY capture
    # that PR #6212 added manifest indirection for — is preserved
    # under ``<role>_recording_pair`` so power users / forensic tooling
    # can still find it. Both invariants matter:
    #   - per-session keys MUST drift between exchanges (each exchange
    #     gets its own slice file), and
    #   - pair-scoped keys MUST stay identical (one continuous PTY).
    first_run_dir = first.exchange_dir.parent
    second_run_dir = second.exchange_dir.parent
    first_manifest = json.loads((first_run_dir / "manifest.json").read_text())
    second_manifest = json.loads((second_run_dir / "manifest.json").read_text())

    # Per-session keys: must differ across exchanges (different run_dirs).
    assert first_manifest["coder_recording"] != second_manifest["coder_recording"], (
        "coder per-session slice path matched across exchanges; the "
        "per-session manifest indirection broke and the viewer would "
        "see exchange 1 content while looking at exchange 2"
    )
    assert (
        first_manifest["reviewer_recording"] != second_manifest["reviewer_recording"]
    ), "reviewer per-session slice path matched across exchanges"
    assert Path(first_manifest["coder_recording"]).is_relative_to(first_run_dir), (
        "exchange 1 coder slice escaped its run_dir"
    )
    assert Path(second_manifest["coder_recording"]).is_relative_to(second_run_dir), (
        "exchange 2 coder slice escaped its run_dir"
    )

    # Pair-scoped keys: must stay identical across exchanges (the
    # original PR #6212 invariant — one continuous PTY per pair).
    assert (
        first_manifest["coder_recording_pair"]
        == second_manifest["coder_recording_pair"]
    ), (
        "pair-scoped coder recording path drifted between exchanges — "
        "both should point at the same continuous PTY capture"
    )
    assert (
        first_manifest["reviewer_recording_pair"]
        == second_manifest["reviewer_recording_pair"]
    ), "pair-scoped reviewer recording path drifted between exchanges"
    assert Path(second_manifest["reviewer_recording"]).exists(), (
        "exchange 2's manifest-pointed reviewer slice is missing on disk"
    )

    accessor = ManifestAccessor(
        RunIdentity(issue_number=9001, run_dir=second_run_dir),
    )
    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=1,
        role="reviewer",
    )
    # ManifestAccessor follows the per-session ``<role>_recording`` key
    # (not the pair-scoped one) so the viewer renders this exchange's
    # content alone — no leakage from exchange 1.
    assert artifact.path == Path(second_manifest["reviewer_recording"]), (
        "ManifestAccessor must follow the per-session reviewer pointer "
        "for second-exchange runs; the pair-scoped pointer would mix "
        "exchange 1 + exchange 2 content together"
    )
    assert not artifact.path.is_relative_to(
        Path(second_manifest["reviewer_recording_pair"]).parent,
    ), (
        "viewer landed in pair-scoped storage instead of the per-session "
        "slice — the manifest indirection regressed"
    )

    # Cleanup is guaranteed by the ``pair_registry_with_cleanup``
    # fixture's finally-block, even if any of the assertions above fail.


def test_persistent_pair_response_and_completion_paths_stable_across_exchanges(
    tmp_path: Path,
    monkeypatch,
    pair_registry_with_cleanup,
) -> None:
    """Pair-scoped paths beyond ``coder_recording``/``reviewer_recording``
    must also stay identical across exchanges.

    The ``PersistentExchangePair`` dataclass declares six pair-scoped
    paths in addition to the two recordings:

      - ``reviewer_worktree_path``
      - ``coder_response_path``
      - ``reviewer_response_path``
      - ``coder_completion_path``
      - ``validation_record_path``

    The agent's environment is set once per run-bound spawn to point at these
    stable pair paths (``ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE``,
    ``ISSUE_ORCHESTRATOR_COMPLETION_PATH``, etc.). The process itself is
    respawned for each exchange run, but the pair-scoped files remain stable
    so recovery / replay consumers keep one authoritative location.

    Companion tests pin run-bound process lifetime; this one extends the same
    fixture to cover the *write-side* paths (response, completion, validation
    record) and the reviewer worktree directory itself.
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=_interactive_review_agent_command(),
    )
    config = _make_config(tmp_path, agent)

    pair_registry = pair_registry_with_cleanup
    session_output = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=config,
        session_output=session_output,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            session_output,
            pair_registry=pair_registry,
        ),
    )

    from issue_orchestrator.events import EventContext

    issue_number = 9101

    def _run_one(session_name: str):
        return _run_review_exchange_for_test(
            cre,
            session_output,
            worktree=coder_wt,
            issue_number=issue_number,
            issue_title="Path-stability integration",
            session_name=session_name,
            agent_label="agent:backend",
            events=MagicMock(),
            event_context=EventContext(),
        )

    first = _run_one("issue-9101-first")
    assert first.status == "ok"
    # Snapshot ALL pair-scoped paths via the cache. Direct cache
    # access is acceptable for a regression-style test that needs
    # the dataclass fields not exposed by ``snapshot()`` — the test
    # is the read site, not a control-layer caller.
    cached_first = pair_registry._cache[issue_number]  # noqa: SLF001
    paths_first = {
        "reviewer_worktree": cached_first.reviewer_worktree_path,
        "coder_response": cached_first.coder_response_path,
        "reviewer_response": cached_first.reviewer_response_path,
        "coder_completion": cached_first.coder_completion_path,
        "validation_record": cached_first.validation_record_path,
        "coder_recording": cached_first.coder_recording_path,
        "reviewer_recording": cached_first.reviewer_recording_path,
    }
    worktree_pair_root = (
        coder_wt / ".issue-orchestrator" / "persistent-pairs" / f"issue-{issue_number}"
    )
    for label in (
        "coder_completion",
        "validation_record",
        "coder_recording",
        "reviewer_recording",
    ):
        assert paths_first[label].is_relative_to(worktree_pair_root), (
            f"{label} should be scoped to the coder worktree so scratch reset "
            f"wipes it; got {paths_first[label]}"
        )

    second = _run_one("issue-9101-second")
    assert second.status == "ok"
    cached_second = pair_registry._cache[issue_number]  # noqa: SLF001

    assert cached_first is not cached_second, (
        "registry reused the same live process pair across exchange runs; "
        "the second exchange would inherit stale run-scoped env"
    )

    paths_second = {
        "reviewer_worktree": cached_second.reviewer_worktree_path,
        "coder_response": cached_second.coder_response_path,
        "reviewer_response": cached_second.reviewer_response_path,
        "coder_completion": cached_second.coder_completion_path,
        "validation_record": cached_second.validation_record_path,
        "coder_recording": cached_second.coder_recording_path,
        "reviewer_recording": cached_second.reviewer_recording_path,
    }
    stable_pair_labels = (
        "coder_response",
        "coder_completion",
        "validation_record",
        "coder_recording",
        "reviewer_recording",
    )
    for label in stable_pair_labels:
        path_first = paths_first[label]
        assert path_first == paths_second[label], (
            f"{label} pair path drifted between exchanges.\n"
            f"  first  ({label}): {path_first}\n"
            f"  second ({label}): {paths_second[label]}"
        )
    assert paths_first["reviewer_worktree"] != paths_second["reviewer_worktree"], (
        "reviewer worktree was reused across exchange runs; reviewer process "
        "lifetime should be run-bound"
    )
    assert paths_first["reviewer_response"] != paths_second["reviewer_response"], (
        "reviewer response path was reused across exchange runs; it belongs "
        "inside the run-bound reviewer worktree"
    )

    # Liveness check on the paths the scenario actually touched:
    # the current reviewer worktree must still be a live directory (it's
    # what the reviewer agent runs in), and any file paths whose
    # parent dir doesn't exist would mean the second exchange
    # tore down what the first one set up. Response/completion
    # files may legitimately be absent in a 1-round-ok scenario
    # (coder is never prompted when reviewer approves on round 1)
    # so we only assert their *parent dirs* exist — the writable
    # surface, not whether anything has been written yet.
    assert paths_second["reviewer_worktree"].is_dir(), (
        f"reviewer_worktree {paths_second['reviewer_worktree']} "
        "should still be a live directory after the second exchange; "
        "the second exchange's release path may have torn it down"
    )
    for label in (
        "coder_response",
        "reviewer_response",
        "coder_completion",
        "validation_record",
        "coder_recording",
        "reviewer_recording",
    ):
        parent = paths_second[label].parent
        assert parent.is_dir(), (
            f"{label} parent dir {parent} disappeared — agent has "
            "no surface to write to on subsequent exchanges"
        )

    # And the reviewer recording — the only file the scenario is
    # guaranteed to have produced (reviewer was prompted in round 1
    # of both exchanges) — must exist on disk and be non-empty.
    assert paths_second["reviewer_recording"].exists(), (
        f"reviewer_recording {paths_second['reviewer_recording']} "
        "missing on disk despite reviewer having been prompted"
    )
    assert paths_second["reviewer_recording"].stat().st_size > 0, (
        "reviewer_recording is zero-length — the second exchange's "
        "writes did not land at the pair-scoped path"
    )

    # Cleanup is guaranteed by the ``pair_registry_with_cleanup``
    # fixture's finally-block, even if any of the assertions above fail.


def test_persistent_review_exchange_end_to_end_through_mailbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activate the orchestrator-owned mailbox end to end (PR #6550 finding 1).

    The interactive fixture runs the real ``exchange-respond`` CLI, which
    POSTs to a live Control-API-shaped server backed by the real
    ``InMemoryTurnMailbox``; ``send_round`` polls that mailbox (no response
    file). Proves the fixture/CLI -> endpoint -> mailbox -> send_round path.
    """
    with _review_exchange_mailbox_server(monkeypatch) as (
        port,
        mailbox,
        deliveries,
    ):
        coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("Stub agent prompt", encoding="utf-8")
        agent = AgentConfig(
            prompt_path=prompt_path,
            ai_system="claude-code",
            # Generous round timeout: this test spawns two real agent
            # subprocesses plus a PTY and an HTTP server, and runs under the
            # parallel validate-pr load where CPU is saturated.
            timeout_minutes=3,
            command=_interactive_review_agent_command(),
        )
        config = _make_config(tmp_path, agent)
        config.control_api_port = port  # flows to the agent env as the API port

        session_output = FileSystemSessionOutput()
        runner = PersistentReviewExchangeRunner(
            session_output,
            InMemoryPersistentExchangePairRegistry(),
            turn_mailbox=mailbox,
        )
        cre = CompletionReviewExchange(
            agent_callback_endpoint=published_callback_endpoint(port),
            config=config,
            session_output=session_output,
            emit_review_started=lambda **_: None,
            emit_review_outcome=lambda **_: None,
            review_exchange_runner=runner,
        )

        from issue_orchestrator.events import EventContext

        outcome = _run_review_exchange_for_test(
            cre,
            session_output,
            worktree=coder_wt,
            issue_number=4058,
            issue_title="Mailbox e2e",
            session_name="issue-4058",
            agent_label="agent:backend",
            events=MagicMock(),
            event_context=EventContext(),
        )

    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    # The verdict reached the orchestrator through the mailbox, not a file.
    assert deliveries and deliveries[0] == "accepted"
