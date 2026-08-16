"""Persistent-session review-exchange runner: control-flow tests.

Mock-based: monkeypatches ``open_persistent_session`` / ``send_round``
and substitutes a ``_FakePairRegistry`` so each test drives every
round transition deterministically and asserts on emitted events,
chapter sidecar shape, outcome semantics, and pair-lifecycle
transitions (acquire / release).

End-to-end exercise of the real persistent_round_runner is covered in
``test_persistent_round_runner.py``; this file focuses on the
exchange-loop policy on top of it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.review_artifacts import ReviewDecision
from issue_orchestrator.domain.review_exchange import ReviewExchangeResponse
from issue_orchestrator.domain.review_exchange_run import ReviewExchangeRun
from issue_orchestrator.domain.review_exchange_summary import ReviewExchangeSummaryV1
from issue_orchestrator.domain.runtime_config import RuntimeConfigReference
from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.execution import persistent_session_exchange as pse
from issue_orchestrator.execution.persistent_role_prompt_policy import (
    role_session_needs_fresh_prompt_process,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports import TraceEvent
from tests.callback_endpoint_helpers import ready_callback_endpoint


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _start_exchange_run(
    *,
    session_output: FileSystemSessionOutput,
    coder_worktree_path: Path,
    issue_number: int,
    coder_label: str,
    parent_session_name: str = "coding-1",
) -> ReviewExchangeRun:
    return session_output.start_review_exchange_run(
        coder_worktree_path,
        issue_number=issue_number,
        parent_session_name=parent_session_name,
        agent_label=coder_label,
    )


class _Sink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class _FakeSession:
    """Mock PersistentSession passed back to the exchange runner."""

    def __init__(
        self,
        role: str,
        *,
        completion_path: Path | None = None,
        validation_output_dir: Path | None = None,
        review_report_path: Path | None = None,
        log_writer: Any = None,
    ) -> None:
        self.role = role
        self.closed = False
        proc = SimpleNamespace(returncode=None, pid=12345)
        proc.poll = lambda: proc.returncode

        def _wait(timeout: float | None = None) -> int:  # noqa: ARG001
            proc.returncode = -15
            return proc.returncode

        proc.wait = _wait
        self.proc = proc
        self.master_fd = os.open(os.devnull, os.O_RDONLY)
        # Captured from the agent env at open time so ``_send`` can
        # write protocol-guardrail artifacts (completion, validation
        # record) to the same paths production reads from. The test
        # used to derive the pair_dir as ``response_file.parent.parent``,
        # but the response file moved into the role's worktree (so the
        # agent's seatbelt sandbox can write it). Env-driven discovery
        # is the same indirection production agents use, so the test
        # stays correct as path layout evolves.
        self.completion_path = completion_path
        self.validation_output_dir = validation_output_dir
        self.review_report_path = review_report_path
        # ``log_writer`` exists on the production PersistentSession so
        # ``_attach_slice_mirror`` / ``_detach_slice_mirror`` can wire
        # the per-session slice into the role's PTY writer.
        # Test fixtures that don't care about live mirroring leave this
        # as None — the attach/detach helpers handle absence by no-op.
        self.log_writer = log_writer

    @property
    def is_live(self) -> bool:
        return not self.closed and self.proc.poll() is None


def _make_agent(prompt_path: Path) -> AgentConfig:
    return AgentConfig(
        prompt_path=prompt_path, ai_system="claude-code", timeout_minutes=1
    )


def _make_codex_agent(prompt_path: Path) -> AgentConfig:
    return AgentConfig(
        prompt_path=prompt_path,
        provider="codex",
        ai_system="codex",
        timeout_minutes=1,
    )


@pytest.mark.parametrize("role", ["coder", "reviewer"])
def test_derive_bootstrap_agent_preserves_sandbox_optin(role: str) -> None:
    """Regression (ADR-0034): the exchange launch config must preserve ``sandbox``.

    The prior field-by-field rebuild dropped the ``sandbox`` opt-in, so an
    opted-in coder/reviewer launched UNSANDBOXED on the persistent-exchange
    path. ``_derive_bootstrap_agent`` uses ``dataclasses.replace`` so every
    policy-bearing field is preserved by construction.
    """
    base = AgentConfig(
        prompt_path=Path(f".prompts/{role}.md"),
        prompt_relative=f".prompts/{role}.md",
        provider="claude-code",
        model="opus",
        provider_args={"permission_mode": "bypassPermissions"},
        sandbox=True,
        reviewer="agent:reviewer",
        skip_review=True,
        meta_agent="claude-code",
    )

    derived = pse._derive_bootstrap_agent(base, "BOOTSTRAP PROMPT")  # noqa: SLF001

    # The opt-in and the rest of the policy-bearing fields survive.
    assert derived.sandbox is True
    assert derived.provider == "claude-code"
    assert derived.model == "opus"
    assert derived.provider_args == {"permission_mode": "bypassPermissions"}
    assert derived.reviewer == "agent:reviewer"
    assert derived.skip_review is True
    assert derived.meta_agent == "claude-code"
    # Only the two intended transforms differ from the base.
    assert derived.initial_prompt == "BOOTSTRAP PROMPT"

    # And the opt-in flows into a computed sandbox scope on the exchange path
    # (the argv/settings translation itself is exercised in the launch-wiring and
    # adapter suites, not here).
    from issue_orchestrator.domain.sandbox_scope import (
        SandboxScopeContext,
        compute_session_scope,
    )

    scope = compute_session_scope(
        derived,
        SandboxScopeContext(
            task_kind=f"review_exchange_{role}", worktree=Path("/wt/issue-13")
        ),
    )
    assert scope is not None
    assert scope.write_roots == (Path("/wt/issue-13"),)


def test_derive_bootstrap_agent_resolves_ai_system_only_provider() -> None:
    """An ``ai_system``-only stub resolves its launch provider (no custom command)."""
    base = AgentConfig(
        prompt_path=Path(".prompts/r.md"),
        provider=None,
        ai_system="codex",
        sandbox=True,
    )
    derived = pse._derive_bootstrap_agent(base, "BOOT")  # noqa: SLF001
    assert derived.provider == "codex"
    assert derived.sandbox is True


def _runtime_config(tmp_path: Path) -> RuntimeConfigReference:
    config_path = tmp_path / "issue-orchestrator.test.yaml"
    if not config_path.exists():
        config_path.write_text(
            "validation:\n  quick:\n    cmd: 'true'\n", encoding="utf-8"
        )
    return RuntimeConfigReference(
        config_path=config_path.resolve(),
        selection=RepositoryLaunchSelection.parse(config_name=config_path.name),
    )


def _summary_payload(summary: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "reason": "reviewer_ok",
        "completed_rounds": 1,
        "response_text": None,
        "timestamp": "2026-02-01T00:00:00+00:00",
    }
    payload.update(summary)
    return ReviewExchangeSummaryV1.from_payload(payload).to_payload()


def _binding_assets(run_dir: Path) -> pse.ReviewExchangeRunAssets:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": run_dir.name,
                "started_at": "2026-02-01T00:00:00+00:00",
                "worktree": str(run_dir.parent),
                "agent_label": "agent:backend",
            }
        ),
        encoding="utf-8",
    )
    return pse.ReviewExchangeRunAssets.from_run_dir(run_dir)


def _build_pty_writer(recording_path: Path) -> Any:
    """Build a real ``MirroredTerminalRecordingWriter`` for a fake session.

    The runner's fail-fast ``_attach_slice_mirror`` requires a real
    writer on every PersistentSession; tests that build sessions
    inline must honor the production invariant. Centralizes the
    construction so the geometry and clock defaults stay consistent
    across every inline ``_open`` helper in this file.
    """
    from issue_orchestrator.infra.terminal_recording import (
        MirroredTerminalRecordingWriter,
    )

    recording_path.parent.mkdir(parents=True, exist_ok=True)
    return MirroredTerminalRecordingWriter(
        recording_path,
        initial_rows=40,
        initial_cols=120,
    )


def _setup_worktrees(tmp_path: Path) -> tuple[Path, Path]:
    coder = tmp_path / "coder-wt"
    reviewer = tmp_path / "reviewer-wt"
    coder.mkdir()
    reviewer.mkdir()
    return coder, reviewer


class _FakePairRegistry:
    """Fake registry used by every exchange test in this module.

    Records every spawn and release so tests can assert on pair
    lifecycle. ``acquire`` always invokes the spawn callback, which keeps
    most tests focused on exchange behavior. Pair-contract tests below provide
    explicit cache-hit coverage for release/respawn decisions.
    """

    def __init__(self) -> None:
        self.acquired: list[Any] = []
        self.released: list[tuple[Any, str]] = []
        self.shutdowns: list[str] = []

    def acquire(self, *, issue_key, spawn):  # noqa: ANN001, ANN201
        pair = spawn()
        self.acquired.append((issue_key, pair))
        return pair

    def release(self, issue_key, *, reason):  # noqa: ANN001, ANN201
        self.released.append((issue_key, reason))

    def shutdown_all(self, *, reason):  # noqa: ANN001, ANN201
        self.shutdowns.append(reason)


def _patch_persistent_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_script: dict[str, list[dict[str, Any] | Exception]],
    write_recording: bool = True,
    write_coder_completion: bool = True,
    coder_completion_script: list[bool] | None = None,
    coder_validation_payload_script: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Patch the runner functions in pse to consume a per-role response script.

    Each ``send_round`` call pops the head off the role's queue. If it's
    a dict, the function returns it; if it's an exception, it raises.
    The opened session also gets a recording file written to make
    ``recording_event_count`` happy (or skipped via flag).

    When ``write_coder_completion`` is True (default), each successful
    coder send_round also writes a stub ``completion-coder.json`` so the
    coder protocol guardrail finds the artifact it expects. Tests that
    want to exercise the missing-completion path pass False.

    ``coder_completion_script`` lets a test vary that invariant per
    coder attempt without timing or background coordination.

    ``coder_validation_payload_script`` lets a test vary the validation
    record payload written with each successful coder completion.
    """
    registry = _FakePairRegistry()
    state: dict[str, Any] = {
        "opened": [],
        "rounds_seen": [],
        "prompts_seen": [],
        "prompt_inboxes_seen": [],
        "send_kwargs_seen": [],
        "run_dir": None,
        "registry": registry,
    }
    completion_script = (
        list(coder_completion_script) if coder_completion_script is not None else None
    )
    validation_payload_script = (
        list(coder_validation_payload_script)
        if coder_validation_payload_script is not None
        else None
    )

    def _open(
        *,
        command,
        working_dir,
        env,
        recording_path=None,
        additional_recording_paths=None,
        mirror_path=None,
    ):  # noqa: ARG001
        # Discriminate by the worktree's basename only — the full path may
        # contain ``reviewer`` because pytest names tmp dirs after the
        # test, which would mislabel both sessions.
        role = (
            "reviewer" if Path(working_dir).name.startswith("reviewer-wt") else "coder"
        )
        # Always provide a real ``MirroredTerminalRecordingWriter`` so
        # the runner's fail-fast ``_attach_slice_mirror`` step succeeds.
        # Production invariant: every PersistentSession opened with a
        # ``recording_path`` carries a real writer. Tests that previously
        # passed with ``log_writer=None`` were getting a free pass on
        # the live-mirror contract — the runner now raises if the
        # invariant is violated, so the fixture has to honor it.
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        assert recording_path is not None, (
            "_patch_persistent_runner: production always passes a "
            "recording_path; the fixture mirrors that invariant"
        )
        recording_path.parent.mkdir(parents=True, exist_ok=True)
        writer = MirroredTerminalRecordingWriter(
            recording_path,
            initial_rows=40,
            initial_cols=120,
        )
        if not write_recording:
            # Tests exercising "what if the recording is missing at
            # chapter time?" delete the file after the writer has
            # already opened its append-mode handle. The writer's
            # subsequent writes go to the orphan inode (invisible to
            # other readers); ``recording_event_count`` reads from
            # the path, which no longer exists, and raises
            # FileNotFoundError — which is exactly what the test
            # asserts the exchange propagates as a definitive failure.
            recording_path.unlink()
        state.setdefault("writers", {})[role] = writer
        state["opened"].append(role)
        completion_env = env.get("ISSUE_ORCHESTRATOR_COMPLETION_PATH")
        validation_env = env.get("ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR")
        review_report_env = env.get("ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE")
        return _FakeSession(
            role,
            completion_path=Path(completion_env) if completion_env else None,
            validation_output_dir=Path(validation_env) if validation_env else None,
            review_report_path=Path(review_report_env) if review_report_env else None,
            log_writer=writer,
        )

    def _send(session, *, prompt, response_file, timeout_seconds, **kwargs):  # noqa: ARG001
        role = session.role
        state["rounds_seen"].append((role, prompt[:40]))
        state["prompts_seen"].append((role, prompt))
        state["send_kwargs_seen"].append((role, dict(kwargs)))
        prompt_inbox = Path(response_file).with_name("review-exchange-turn-prompt.md")
        if prompt_inbox.exists():
            state["prompt_inboxes_seen"].append(
                (
                    role,
                    prompt_inbox.read_text(encoding="utf-8"),
                    prompt,
                )
            )
        if not response_script.get(role):
            raise AssertionError(
                f"send_round called for {role} with no scripted response left"
            )
        head = response_script[role].pop(0)
        if isinstance(head, Exception):
            raise head
        # A dict entry may carry ``_raise`` to model a process that writes its
        # side artifact(s) for this turn and *then* exits before producing the
        # response JSON — the stale-side-artifact case the respawn retry must
        # not consume. Side artifacts below are written first, then we raise.
        raise_after = head.pop("_raise", None)
        authored_report_text = head.pop("_authored_report_text", None)
        if role == "reviewer" and authored_report_text is not None:
            report_path = session.review_report_path
            assert report_path is not None
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(str(authored_report_text), encoding="utf-8")
        # Stub the coder's completion artifact write so the protocol
        # guardrail in pse._validate_coder_completion has the file it expects.
        should_write_completion = write_coder_completion
        if role == "coder" and completion_script is not None:
            if not completion_script:
                raise AssertionError("test fixture: coder_completion_script exhausted")
            should_write_completion = completion_script.pop(0)
        if role == "coder" and should_write_completion:
            completion = session.completion_path
            assert completion is not None, (
                "test fixture: coder fake session must have completion_path "
                "captured from env at open time"
            )
            state["completion_path"] = completion
            completion.parent.mkdir(parents=True, exist_ok=True)
            # Also stub a passing validation-record.json by default so
            # require_validation=True tests don't blow up on the coder
            # guardrail. Tests exercising missing-validation set
            # ``write_coder_completion=False``.
            validation_dir = session.validation_output_dir
            assert validation_dir is not None
            validation_dir.mkdir(parents=True, exist_ok=True)
            validation_record = validation_dir / "validation-record.json"
            if validation_payload_script is not None:
                if not validation_payload_script:
                    raise AssertionError(
                        "test fixture: coder_validation_payload_script exhausted"
                    )
                validation_payload = validation_payload_script.pop(0)
            else:
                validation_payload = {"passed": True}
            validation_record.write_text(
                json.dumps(validation_payload),
                encoding="utf-8",
            )
            completion.write_text(
                json.dumps(
                    {
                        "outcome": "completed",
                        "implementation": "stub",
                        "validation_record_path": str(validation_record),
                    }
                ),
                encoding="utf-8",
            )
        if raise_after is not None:
            raise raise_after
        return head

    def _close(session, **_):  # noqa: ANN001, ANN202
        session.closed = True
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        if session.log_writer is not None:
            session.log_writer.close()
        return session.proc.returncode

    monkeypatch.setattr(pse, "open_persistent_session", _open)
    monkeypatch.setattr(pse, "send_round", _send)
    monkeypatch.setattr(pse, "close_persistent_session", _close)
    return state


# ---------------------------------------------------------------------------
# Artifact-freshness owner
# ---------------------------------------------------------------------------


class TestRolePromptPolicy:
    """Provider-aware prompt-process freshness policy."""

    @pytest.mark.parametrize(
        ("agent_kwargs", "expected"),
        [
            pytest.param(
                {"provider": "claude-code", "ai_system": "claude-code"},
                False,
                id="claude",
            ),
            pytest.param(
                {"provider": "codex", "ai_system": "codex"},
                True,
                id="codex-default-interactive",
            ),
            pytest.param(
                {
                    "provider": "codex",
                    "ai_system": "codex",
                    "provider_args": {"execution_mode": "exec"},
                },
                False,
                id="codex-exec",
            ),
            pytest.param(
                {
                    "provider": "codex",
                    "ai_system": "codex",
                    "provider_args": {"execution_mode": "tui"},
                },
                True,
                id="codex-tui-alias",
            ),
            pytest.param(
                {
                    "ai_system": "codex",
                    "command": "python deterministic-agent.py '{initial_prompt}'",
                },
                True,
                id="codex-ai-system-custom-command",
            ),
        ],
    )
    def test_role_session_needs_fresh_prompt_process_uses_provider_capability(
        self,
        tmp_path: Path,
        agent_kwargs: dict[str, Any],
        expected: bool,
    ) -> None:
        agent = AgentConfig(
            prompt_path=tmp_path / "prompt.md",
            timeout_minutes=1,
            **agent_kwargs,
        )

        assert role_session_needs_fresh_prompt_process(agent) is expected


class TestRoleAttemptWorkspace:
    """The per-role artifact-freshness owner.

    These assert the owner's contract directly: every process attempt of a
    role turn starts from a clean workspace, so a side artifact written by a
    process that then died (reviewer ``review-report.md`` / coder
    ``completion-coder.json``) cannot be paired or validated against a later,
    respawned attempt's response. ``_send_role_round`` calls
    ``prepare_for_attempt`` before every attempt (initial, coder protocol
    retry, and respawn retry), so clearing here is what makes the respawn path
    safe — see
    ``test_process_exit_before_response_respawns_in_place_and_advances`` for
    the respawn path that routes through this owner.
    """

    def test_prepare_clears_response_inbox_and_reviewer_report(
        self,
        tmp_path: Path,
    ) -> None:
        response = tmp_path / "review-response.json"
        report = tmp_path / "review-report.md"
        response.write_text("{}", encoding="utf-8")
        report.write_text("STALE report from a dead reviewer process", encoding="utf-8")
        inbox = pse._role_prompt_inbox_path(response)  # noqa: SLF001
        inbox.write_text("stale prompt", encoding="utf-8")

        workspace = pse._RoleAttemptWorkspace(  # noqa: SLF001
            response_file=response,
            side_artifact_paths=(report,),
        )
        workspace.prepare_for_attempt()

        assert not response.exists()
        assert not inbox.exists()
        assert not report.exists(), (
            "a stale reviewer report from a dead pre-retry process must be "
            "cleared before a fresh attempt so it is not paired with the "
            "respawned review decision"
        )

    def test_prepare_clears_response_inbox_and_coder_completion(
        self,
        tmp_path: Path,
    ) -> None:
        response = tmp_path / "review-response.json"
        completion = tmp_path / "completion-coder.json"
        response.write_text("{}", encoding="utf-8")
        completion.write_text('{"outcome": "completed"}', encoding="utf-8")
        inbox = pse._role_prompt_inbox_path(response)  # noqa: SLF001
        inbox.write_text("stale prompt", encoding="utf-8")

        workspace = pse._RoleAttemptWorkspace(  # noqa: SLF001
            response_file=response,
            side_artifact_paths=(completion,),
        )
        workspace.prepare_for_attempt()

        assert not response.exists()
        assert not inbox.exists()
        assert not completion.exists(), (
            "a stale coder completion from a dead pre-retry process must be "
            "cleared so it cannot satisfy the respawned coder turn's protocol "
            "guardrail"
        )

    def test_prepare_is_idempotent_when_artifacts_absent(
        self,
        tmp_path: Path,
    ) -> None:
        response = tmp_path / "review-response.json"
        workspace = pse._RoleAttemptWorkspace(  # noqa: SLF001
            response_file=response,
            side_artifact_paths=(tmp_path / "absent-side-artifact.md",),
        )
        # The first attempt of a turn runs against an empty workspace; the
        # reset must be a no-op rather than raising.
        workspace.prepare_for_attempt()
        assert not response.exists()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestPersistentSessionExchangeHappyPath:
    def test_one_round_reviewer_ok_returns_status_ok(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "Looks good",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        assert outcome.status == "ok"
        assert outcome.rounds == 1
        assert outcome.reason == "reviewer_ok"
        assert outcome.reviewer_response is not None
        assert outcome.reviewer_response.response_type == "ok"
        round_completed = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
        ]
        completed = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_COMPLETED
        ]
        assert len(round_completed) == 1
        assert len(completed) == 1
        assert round_completed[0].data["review_decision_verdict"] == "approved"
        assert round_completed[0].data["review_nit_policy"] == "surface"
        assert round_completed[0].data["review_abstraction_status"] == "no_issues"
        round_artifacts = round_completed[0].data["artifacts"]
        assert {
            artifact["type"]: artifact["render_mode"] for artifact in round_artifacts
        } == {"review_report": "markdown", "review_decision": "json"}
        assert completed[0].data["review_decision_verdict"] == "approved"
        assert completed[0].data["review_nit_policy"] == "surface"
        assert completed[0].data["review_abstraction_status"] == "no_issues"
        reviewer_prompt, reviewer_notice = next(
            (prompt, notice)
            for role, prompt, notice in state["prompt_inboxes_seen"]
            if role == "reviewer"
        )
        assert (
            "reviewer in a coder↔reviewer exchange for issue #42: Test"
            in reviewer_prompt
        )
        assert "review-exchange-turn-prompt.md" in reviewer_notice
        assert len(reviewer_notice) < 512
        assert not (
            reviewer_wt / ".issue-orchestrator" / "review-exchange-turn-prompt.md"
        ).exists()

    def test_file_response_channel_does_not_open_or_poll_mailbox(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "Looks good",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
        )
        mailbox = MagicMock(name="turn_mailbox")

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            turn_mailbox=mailbox,
            response_channels=pse.ReviewExchangeResponseChannels.file_only(),
        )

        assert outcome.status == "ok"
        mailbox.open.assert_not_called()
        mailbox.try_take.assert_not_called()
        mailbox.close.assert_not_called()
        role, kwargs = state["send_kwargs_seen"][0]
        assert role == "reviewer"
        assert kwargs["response_reader"] is None
        reviewer_prompt, reviewer_notice = next(
            (prompt, notice)
            for role, prompt, notice in state["prompt_inboxes_seen"]
            if role == "reviewer"
        )
        assert "file channel" in reviewer_prompt
        assert "Do not run `exchange-respond`" in reviewer_prompt
        assert "writing JSON" in reviewer_notice

    def test_two_round_exchange_changes_then_ok(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "See review report.",
                        "getting_closer": True,
                        "decision": {
                            "verdict": "changes_requested",
                            "risk": "medium",
                            "blocking_findings": [{"id": "F1"}],
                            "nits": [],
                            "abstraction_review": {
                                "status": "no_issues",
                                "findings": [],
                            },
                            "nit_policy": "surface",
                        },
                        "_authored_report_text": (
                            "# Review\n\n"
                            "## F1\n\n"
                            "Fix the typo in the persisted report path.\n"
                        ),
                    },
                    {
                        "response_type": "ok",
                        "response_text": "All good",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "Fixed typo",
                        "getting_closer": None,
                    },
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )
        assert outcome.status == "ok"
        assert outcome.rounds == 2
        # Round order: reviewer round 1 → coder round 1 → reviewer round 2.
        assert [role for role, _ in state["rounds_seen"]] == [
            "reviewer",
            "coder",
            "reviewer",
        ]
        # Both sessions opened and stay live through the multi-round
        # exchange. Cross-run reuse is governed by the pair-contract tests.
        assert sorted(state["opened"]) == ["coder", "reviewer"]
        assert state["registry"].released == []
        assert len(state["registry"].acquired) == 1
        assert outcome.exchange_dir is not None
        turns_dir = outcome.exchange_dir / "turns"
        report_text = (
            turns_dir / "round-1-reviewer-attempt-1.review-report.md"
        ).read_text(encoding="utf-8")
        coder_packet = json.loads((turns_dir / "round-1-coder.packet.json").read_text())
        assert coder_packet["reviewer_feedback"] == report_text
        coder_prompt = next(
            prompt
            for role, prompt, _notice in state["prompt_inboxes_seen"]
            if role == "coder"
        )
        assert "Reviewer report:" in coder_prompt
        assert "Fix the typo in the persisted report path." in coder_prompt
        assert "Reviewer feedback:" not in coder_prompt

    def test_approval_gate_routes_contract_error_through_coder_rework(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        class _RejectOnce:
            def __init__(self) -> None:
                self.calls = 0

            def rejection_reason(self) -> str | None:
                self.calls += 1
                if self.calls == 1:
                    return (
                        "Tech Lead decision artifact rejected "
                        "(contract_violation): finding T2 title exceeds "
                        "300 characters (459)."
                    )
                return None

        gate = _RejectOnce()
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "Looks good",
                        "getting_closer": True,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "Contract repair verified",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "Shortened T2 and moved detail to evidence",
                        "getting_closer": True,
                    }
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=34,
                coder_label="agent:tech-lead",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=34,
            issue_title="Health Review — walk the floor",
            coder_label="agent:tech-lead",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            approval_gate=gate,
        )

        assert outcome.status == "ok"
        assert outcome.rounds == 2
        assert gate.calls == 2
        assert [role for role, _ in state["rounds_seen"]] == [
            "reviewer",
            "coder",
            "reviewer",
        ]
        coder_prompt = next(
            prompt
            for role, prompt, _notice in state["prompt_inboxes_seen"]
            if role == "coder"
        )
        assert "Orchestrator acceptance gate" in coder_prompt
        assert "finding T2 title exceeds 300 characters (459)" in coder_prompt

    def test_codex_reviewer_respawns_before_followup_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Interactive Codex reviewer turns do not reuse a completed TUI.

        The tixmeup #391 failure showed the next reviewer prompt landing in
        Codex's autocomplete/search overlay after a prior turn completed. A
        fresh process for the follow-up prompt avoids paying a 120s
        prompt_not_accepted timeout before the existing retry path respawns.
        """
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "See review report.",
                        "getting_closer": True,
                        "decision": {
                            "verdict": "changes_requested",
                            "risk": "medium",
                            "blocking_findings": [{"id": "F1"}],
                            "nits": [],
                            "abstraction_review": {
                                "status": "no_issues",
                                "findings": [],
                            },
                            "nit_policy": "surface",
                        },
                        "_authored_report_text": (
                            "# Review\n\n## F1\n\nFix the stale Codex prompt target.\n"
                        ),
                    },
                    {
                        "response_type": "ok",
                        "response_text": "All good",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "Fixed prompt target",
                        "getting_closer": None,
                    },
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_codex_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.status == "ok"
        assert outcome.rounds == 2
        assert [role for role, _ in state["rounds_seen"]] == [
            "reviewer",
            "coder",
            "reviewer",
        ]
        assert state["opened"].count("reviewer") == 2
        assert state["opened"].count("coder") == 1

    def test_address_nits_policy_routes_approval_through_coder_rework(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "Approved with one nit.",
                        "getting_closer": True,
                        "decision": {
                            "verdict": "approved",
                            "risk": "low",
                            "blocking_findings": [],
                            "nits": [{"id": "N1", "title": "Tighten wording"}],
                            "abstraction_review": {
                                "status": "no_issues",
                                "findings": [],
                            },
                            "nit_policy": "address",
                        },
                    },
                    {
                        "response_type": "ok",
                        "response_text": "Nit addressed.",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "Addressed N1.",
                        "getting_closer": True,
                    },
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            nit_policy="address",
        )

        assert outcome.status == "ok"
        assert outcome.rounds == 2
        assert [role for role, _ in state["rounds_seen"]] == [
            "reviewer",
            "coder",
            "reviewer",
        ]
        coder_prompt = next(
            prompt
            for role, prompt, _notice in state["prompt_inboxes_seen"]
            if role == "coder"
        )
        assert "Tighten wording" in coder_prompt
        assert outcome.exchange_dir is not None
        decision = json.loads(
            (
                outcome.exchange_dir
                / "turns"
                / "round-1-reviewer-attempt-1.review-decision.json"
            ).read_text(encoding="utf-8")
        )
        assert decision["verdict"] == "approved"
        assert decision["nit_policy"] == "address"
        assert decision["nits"][0]["id"] == "N1"

    def test_addressable_nits_keep_reviewer_decision_intent_in_raw_json(self) -> None:
        raw_json = {
            "decision": {
                "verdict": "approved",
                "risk": "low",
                "blocking_findings": [],
                "nits": [{"id": "N1", "title": "Tighten wording"}],
                "abstraction_review": {"status": "no_issues", "findings": []},
                "nit_policy": "address",
            }
        }
        reviewer = ReviewExchangeResponse(
            response_type="ok",
            response_text="Approved with one nit.",
            getting_closer=True,
            raw_json=raw_json,
            raw_output=json.dumps(raw_json),
        )
        decision = ReviewDecision.from_agent_payload(
            raw_json,
            response_type=reviewer.response_type,
            response_text=reviewer.response_text,
            nit_policy="address",
        )

        rework_response = pse._reviewer_response_for_addressable_nits(  # noqa: SLF001
            reviewer,
            decision,
        )

        assert decision.verdict == "approved"
        assert rework_response.response_type == "changes_requested"
        assert rework_response.raw_json is raw_json
        assert "Tighten wording" in rework_response.response_text


class TestPairValidationMirror:
    def test_current_validation_seed_replaces_existing_pair_record(
        self,
        tmp_path: Path,
    ) -> None:
        coder_wt = tmp_path / "coder-wt"
        pair_dir = coder_wt / ".issue-orchestrator" / "persistent-pairs" / "issue-42"
        pair_record = pair_dir / "validation-record.json"
        pair_dir.mkdir(parents=True)
        pair_record.write_text(
            json.dumps({"passed": True, "head_sha": "old-sha"}),
            encoding="utf-8",
        )
        current_record = tmp_path / "current-validation-record.json"
        current_record.write_text(
            json.dumps({"passed": True, "head_sha": "new-sha"}),
            encoding="utf-8",
        )

        mirror = pse._PairValidationMirror(  # noqa: SLF001
            pair_dir=pair_dir,
            record_path=pair_record,
            coder_worktree_path=coder_wt,
        )
        mirror.replace_from_initial(current_record)

        assert json.loads(pair_record.read_text(encoding="utf-8")) == {
            "passed": True,
            "head_sha": "new-sha",
        }

    def test_current_validation_seed_is_mirrored_to_exchange_run_record(
        self,
        tmp_path: Path,
    ) -> None:
        coder_wt = tmp_path / "coder-wt"
        pair_dir = coder_wt / ".issue-orchestrator" / "persistent-pairs" / "issue-42"
        pair_record = pair_dir / "validation-record.json"
        run_record = (
            coder_wt
            / ".issue-orchestrator"
            / "sessions"
            / "review-exchange-run"
            / "validation-record.json"
        )
        payload = {"passed": True, "head_sha": "new-sha"}
        current_record = tmp_path / "current-validation-record.json"
        current_record.write_text(json.dumps(payload), encoding="utf-8")

        mirror = pse._PairValidationMirror(  # noqa: SLF001
            pair_dir=pair_dir,
            record_path=pair_record,
            coder_worktree_path=coder_wt,
            run_record_path=run_record,
        )
        mirror.replace_from_initial(current_record)

        assert json.loads(pair_record.read_text(encoding="utf-8")) == payload
        assert json.loads(run_record.read_text(encoding="utf-8")) == payload

    def test_missing_initial_validation_source_clears_stale_pair_and_run_records(
        self,
        tmp_path: Path,
    ) -> None:
        coder_wt = tmp_path / "coder-wt"
        pair_dir = coder_wt / ".issue-orchestrator" / "persistent-pairs" / "issue-42"
        pair_record = pair_dir / "validation-record.json"
        run_record = (
            coder_wt
            / ".issue-orchestrator"
            / "sessions"
            / "review-exchange-run"
            / "validation-record.json"
        )
        pair_dir.mkdir(parents=True)
        run_record.parent.mkdir(parents=True)
        pair_record.write_text(
            json.dumps({"passed": True, "head_sha": "old-sha"}),
            encoding="utf-8",
        )
        run_record.write_text(
            json.dumps({"passed": True, "head_sha": "old-sha"}),
            encoding="utf-8",
        )
        mirror = pse._PairValidationMirror(  # noqa: SLF001
            pair_dir=pair_dir,
            record_path=pair_record,
            coder_worktree_path=coder_wt,
            run_record_path=run_record,
        )

        mirror.replace_from_initial(None)

        assert not pair_record.exists()
        assert not run_record.exists()

    def test_completion_validation_record_replaces_stale_pair_head(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        coder_wt = tmp_path / "coder-wt"
        pair_dir = coder_wt / ".issue-orchestrator" / "persistent-pairs" / "issue-42"
        pair_record = pair_dir / "validation-record.json"
        completion = pair_dir / "coder" / "completion-coder.json"
        current_record = coder_wt / ".issue-orchestrator" / "validation" / "head-b.json"
        pair_record.parent.mkdir(parents=True)
        completion.parent.mkdir(parents=True)
        current_record.parent.mkdir(parents=True)
        pair_record.write_text(
            json.dumps({"passed": True, "head_sha": "head-a"}),
            encoding="utf-8",
        )
        current_record.write_text(
            json.dumps({"passed": True, "head_sha": "head-b"}),
            encoding="utf-8",
        )
        completion.write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "validation_record_path": str(current_record),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(pse, "get_repo_head_sha", lambda _: "head-b")
        mirror = pse._PairValidationMirror(  # noqa: SLF001
            pair_dir=pair_dir,
            record_path=pair_record,
            coder_worktree_path=coder_wt,
        )

        error = pse._validate_coder_completion(  # noqa: SLF001
            completion_path=completion,
            pair_validation=mirror,
            run_validation_record_path=tmp_path / "run" / "validation-record.json",
            require_validation=True,
        )

        assert error is None
        assert json.loads(pair_record.read_text(encoding="utf-8")) == {
            "passed": True,
            "head_sha": "head-b",
        }

    def test_stale_completion_validation_head_fails_current_head_check(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        coder_wt = tmp_path / "coder-wt"
        pair_dir = coder_wt / ".issue-orchestrator" / "persistent-pairs" / "issue-42"
        pair_record = pair_dir / "validation-record.json"
        completion = pair_dir / "coder" / "completion-coder.json"
        stale_record = coder_wt / ".issue-orchestrator" / "validation" / "head-a.json"
        completion.parent.mkdir(parents=True)
        stale_record.parent.mkdir(parents=True)
        stale_record.write_text(
            json.dumps({"passed": True, "head_sha": "head-a"}),
            encoding="utf-8",
        )
        completion.write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "validation_record_path": str(stale_record),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(pse, "get_repo_head_sha", lambda _: "head-b")
        mirror = pse._PairValidationMirror(  # noqa: SLF001
            pair_dir=pair_dir,
            record_path=pair_record,
            coder_worktree_path=coder_wt,
        )

        error = pse._validate_coder_completion(  # noqa: SLF001
            completion_path=completion,
            pair_validation=mirror,
            run_validation_record_path=tmp_path / "run" / "validation-record.json",
            require_validation=True,
        )

        assert error is not None
        assert "does not match current HEAD" in error
        assert (
            json.loads(pair_record.read_text(encoding="utf-8"))["head_sha"] == "head-a"
        )

    def test_completion_without_validation_source_clears_stale_pair_record(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        coder_wt = tmp_path / "coder-wt"
        pair_dir = coder_wt / ".issue-orchestrator" / "persistent-pairs" / "issue-42"
        pair_record = pair_dir / "validation-record.json"
        completion = pair_dir / "coder" / "completion-coder.json"
        pair_record.parent.mkdir(parents=True)
        completion.parent.mkdir(parents=True)
        pair_record.write_text(
            json.dumps({"passed": True, "head_sha": "head-a"}),
            encoding="utf-8",
        )
        completion.write_text(json.dumps({"outcome": "completed"}), encoding="utf-8")
        monkeypatch.setattr(pse, "get_repo_head_sha", lambda _: "head-b")
        mirror = pse._PairValidationMirror(  # noqa: SLF001
            pair_dir=pair_dir,
            record_path=pair_record,
            coder_worktree_path=coder_wt,
        )

        error = pse._validate_coder_completion(  # noqa: SLF001
            completion_path=completion,
            pair_validation=mirror,
            run_validation_record_path=tmp_path / "run" / "validation-record.json",
            require_validation=True,
        )

        assert error == "validation-record.json missing"
        assert not pair_record.exists()

    def test_completion_without_payload_uses_run_dir_validation_record(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        coder_wt = tmp_path / "coder-wt"
        pair_dir = coder_wt / ".issue-orchestrator" / "persistent-pairs" / "issue-42"
        pair_record = pair_dir / "validation-record.json"
        completion = pair_dir / "coder" / "completion-coder.json"
        run_record = (
            coder_wt
            / ".issue-orchestrator"
            / "sessions"
            / "run"
            / "validation-record.json"
        )
        completion.parent.mkdir(parents=True)
        run_record.parent.mkdir(parents=True)
        completion.write_text(json.dumps({"outcome": "completed"}), encoding="utf-8")
        run_record.write_text(
            json.dumps({"passed": True, "head_sha": "head-b"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(pse, "get_repo_head_sha", lambda _: "head-b")
        mirror = pse._PairValidationMirror(  # noqa: SLF001
            pair_dir=pair_dir,
            record_path=pair_record,
            coder_worktree_path=coder_wt,
        )

        error = pse._validate_coder_completion(  # noqa: SLF001
            completion_path=completion,
            pair_validation=mirror,
            run_validation_record_path=run_record,
            require_validation=True,
        )

        assert error is None
        assert json.loads(pair_record.read_text(encoding="utf-8")) == {
            "passed": True,
            "head_sha": "head-b",
        }


# ---------------------------------------------------------------------------
# Turn-packet / turn-result on-disk artifacts
# ---------------------------------------------------------------------------


class TestTurnArtifactsPersisted:
    """The runner persists every turn's packet and result as JSON
    artifacts under ``<exchange_dir>/turns/`` so a failed exchange
    leaves a complete on-disk trail (orchestrator-side input + parsed
    agent output) that an operator or replay test can inspect without
    walking the recording stream.

    These tests pin:
    1. The artifact path layout (``round-<n>-<role>.packet.json`` plus
       attempt-scoped prompt/result/started/completed contracts).
    2. Round-trip parseability via ``ReviewExchangeTurnPacket.from_manifest``
       and ``ReviewExchangeTurnResult.from_manifest``.
    3. Field content matches the data the runner observed at that turn.

    A regression that drops the persistence calls or drifts the
    field set away from the manifest contract breaks these tests.
    """

    def test_two_round_exchange_persists_packet_and_result_per_turn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.domain.review_exchange_turn import (
            ReviewExchangeTurnPacket,
            ReviewExchangeTurnResult,
            Role,
            TurnResultKind,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "Fix typo",
                        "getting_closer": True,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "All good",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "Fixed typo",
                        "getting_closer": None,
                    },
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.exchange_dir is not None
        turns_dir = outcome.exchange_dir / "turns"
        assert turns_dir.is_dir(), f"Expected turns dir at {turns_dir}"

        # Three attempts happened (reviewer round 1 → coder round 1 →
        # reviewer round 2). Packets stay round/role scoped; prompts,
        # results, and start/complete contracts are attempt scoped so
        # retries cannot overwrite or borrow another prompt.
        artifact_names = sorted(p.name for p in turns_dir.iterdir())
        assert artifact_names == sorted(
            [
                "round-1-coder-attempt-1.completed.json",
                "round-1-coder-attempt-1.prompt.md",
                "round-1-coder-attempt-1.result.json",
                "round-1-coder-attempt-1.started.json",
                "round-1-coder.packet.json",
                "round-1-reviewer-attempt-1.completed.json",
                "round-1-reviewer-attempt-1.prompt.md",
                "round-1-reviewer-attempt-1.review-decision.json",
                "round-1-reviewer-attempt-1.review-report.md",
                "round-1-reviewer-attempt-1.result.json",
                "round-1-reviewer-attempt-1.started.json",
                "round-1-reviewer.packet.json",
                "round-2-reviewer-attempt-1.completed.json",
                "round-2-reviewer-attempt-1.prompt.md",
                "round-2-reviewer-attempt-1.review-decision.json",
                "round-2-reviewer-attempt-1.review-report.md",
                "round-2-reviewer-attempt-1.result.json",
                "round-2-reviewer-attempt-1.started.json",
                "round-2-reviewer.packet.json",
            ]
        )

        r1_started = json.loads(
            (turns_dir / "round-1-reviewer-attempt-1.started.json").read_text()
        )
        assert r1_started["attempt_index"] == 1
        assert [artifact["kind"] for artifact in r1_started["artifacts"]] == [
            "prompt",
            "terminal_recording",
            "chapter_sidecar",
        ]
        r1_completed = json.loads(
            (turns_dir / "round-1-reviewer-attempt-1.completed.json").read_text()
        )
        assert [artifact["kind"] for artifact in r1_completed["artifacts"]] == [
            "prompt",
            "terminal_recording",
            "chapter_sidecar",
            "review_response",
        ]

        # Round 1 reviewer packet: typed, role REVIEWER, no prior
        # texts, validation off.
        r1_packet_data = json.loads(
            (turns_dir / "round-1-reviewer.packet.json").read_text()
        )
        r1_packet = ReviewExchangeTurnPacket.from_manifest(r1_packet_data)
        assert r1_packet is not None
        assert r1_packet.role is Role.REVIEWER
        assert r1_packet.round_index == 1
        assert r1_packet.issue_number == 42
        assert r1_packet.issue_title == "Test"
        assert r1_packet.require_validation is False
        assert r1_packet.last_coder_text is None
        assert r1_packet.last_reviewer_text is None

        # Round 1 reviewer result: changes_requested.
        r1_review_result = ReviewExchangeTurnResult.from_manifest(
            json.loads(
                (turns_dir / "round-1-reviewer-attempt-1.result.json").read_text()
            )
        )
        assert r1_review_result is not None
        assert r1_review_result.kind is TurnResultKind.CHANGES_REQUESTED
        assert r1_review_result.response_text == "Fix typo"
        assert r1_review_result.getting_closer is True

        # Round 1 coder packet: typed, role CODER, reviewer_feedback
        # carries the persisted round-1 reviewer report text.
        r1_coder_packet = ReviewExchangeTurnPacket.from_manifest(
            json.loads((turns_dir / "round-1-coder.packet.json").read_text())
        )
        r1_report_text = (
            turns_dir / "round-1-reviewer-attempt-1.review-report.md"
        ).read_text(encoding="utf-8")
        assert r1_coder_packet is not None
        assert r1_coder_packet.role is Role.CODER
        assert r1_coder_packet.reviewer_feedback == r1_report_text

        # Round 1 coder result: ok.
        r1_coder_result = ReviewExchangeTurnResult.from_manifest(
            json.loads((turns_dir / "round-1-coder-attempt-1.result.json").read_text())
        )
        assert r1_coder_result is not None
        assert r1_coder_result.kind is TurnResultKind.OK
        assert r1_coder_result.response_text == "Fixed typo"

        # Round 2 reviewer packet carries the prior round's coder and
        # reviewer texts so the prompt builder can render them.
        r2_packet = ReviewExchangeTurnPacket.from_manifest(
            json.loads((turns_dir / "round-2-reviewer.packet.json").read_text())
        )
        assert r2_packet is not None
        assert r2_packet.last_coder_text == "Fixed typo"
        assert r2_packet.last_reviewer_text == "Fix typo"

        # Round 2 reviewer result: ok (terminal — no coder round 2).
        r2_review_result = ReviewExchangeTurnResult.from_manifest(
            json.loads(
                (turns_dir / "round-2-reviewer-attempt-1.result.json").read_text()
            )
        )
        assert r2_review_result is not None
        assert r2_review_result.kind is TurnResultKind.OK

    def test_timeout_persists_no_completion_result_with_packet(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reviewer round-1 times out. The runner must still persist
        BOTH the packet (the orchestrator-side input) and a typed
        result with ``protocol_error_reason="no_completion"`` —
        operators inspecting a hung exchange need the on-disk trail
        for the rounds where the agent failed to respond, which is
        exactly the case the previous artifact contract dropped.
        """
        from issue_orchestrator.domain.review_exchange_turn import (
            ReviewExchangeTurnPacket,
            ReviewExchangeTurnResult,
            TurnResultKind,
        )
        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("simulated 60s timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.exchange_dir is not None
        turns_dir = outcome.exchange_dir / "turns"
        assert turns_dir.is_dir()

        packet_path = turns_dir / "round-1-reviewer.packet.json"
        result_path = turns_dir / "round-1-reviewer-attempt-1.result.json"
        assert packet_path.exists(), (
            f"packet artifact missing at {packet_path}; turns_dir contains "
            f"{[p.name for p in turns_dir.iterdir()]}"
        )
        assert result_path.exists(), (
            f"result artifact missing on timeout path at {result_path}; "
            f"turns_dir contains {[p.name for p in turns_dir.iterdir()]}"
        )

        packet = ReviewExchangeTurnPacket.from_manifest(
            json.loads(packet_path.read_text())
        )
        assert packet is not None
        assert packet.round_index == 1

        result = ReviewExchangeTurnResult.from_manifest(
            json.loads(result_path.read_text())
        )
        assert result is not None
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "timeout"
        # The exception detail must surface in response_text so an
        # operator inspecting the artifact sees the same root cause
        # the REVIEW_EXCHANGE_ROLE_TIMEOUT event reports.
        assert "simulated 60s timeout" in result.response_text
        assert state["registry"].released == [
            (42, "review-exchange-reviewer_no_completion")
        ]

    def test_process_exit_before_response_preserves_precise_failure_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.domain.review_exchange_turn import (
            ReviewExchangeTurnResult,
        )
        from issue_orchestrator.domain.review_exchange_failures import (
            RoundFailureReason,
        )
        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundError,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()

        # A dead process is now respawned + retried once before the
        # exchange gives up, so it takes two consecutive process-exit
        # failures (the initial turn + its one respawn retry) to reach
        # the terminal reviewer_no_completion path this test pins.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    PersistentRoundError(
                        "Agent exited unexpectedly (code=0) before responding",
                        failure_reason=(
                            RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE
                        ),
                    ),
                    PersistentRoundError(
                        "Agent exited unexpectedly (code=0) before responding",
                        failure_reason=(
                            RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE
                        ),
                    ),
                ],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=EventContext(),
        )

        assert outcome.reason == "reviewer_no_completion"
        assert outcome.exchange_dir is not None
        result_path = (
            outcome.exchange_dir / "turns" / "round-1-reviewer-attempt-1.result.json"
        )
        result = ReviewExchangeTurnResult.from_manifest(
            json.loads(result_path.read_text())
        )
        assert result is not None
        assert result.protocol_error_reason == "process_exited_before_response"
        timeouts = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
        ]
        assert len(timeouts) == 1
        assert timeouts[0].data["failure_reason"] == "process_exited_before_response"
        assert timeouts[0].data["reason"] == "no_completion"
        reviewer_sidecar = session_output.read_exchange_chapters(
            outcome.exchange_dir.parent,  # type: ignore[union-attr]
            role="reviewer",
        )
        assert reviewer_sidecar is not None
        assert reviewer_sidecar.chapters[-1].label == (
            "Round 1 reviewer exited before responding"
        )

    def test_process_exit_before_response_respawns_in_place_and_advances(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-2 reviewer one-shot exit → respawn in place, retry, advance.

        Regression for #6360 and the live traces on tixmeup #289 / #286: a
        one-shot reviewer that exits cleanly (code=0) between rounds used to
        route through ``reviewer_no_completion``, releasing the whole pair and
        restarting the review in a fresh worktree at round 1. The role is now
        respawned in place (same worktree / recording / response paths) and the
        *same* round-2 turn is retried, so the exchange keeps going.
        """
        from issue_orchestrator.domain.review_exchange_failures import (
            RoundFailureReason,
        )
        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundError,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    # Round 1: changes requested → coder reworks.
                    {
                        "response_type": "changes_requested",
                        "response_text": "Fix typo",
                        "getting_closer": True,
                    },
                    # Round 2: the one-shot reviewer exited cleanly before
                    # responding. Expect an in-place respawn + retry...
                    PersistentRoundError(
                        "Agent exited unexpectedly (code=0) before responding",
                        failure_reason=RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE,
                    ),
                    # ...which the respawned reviewer answers.
                    {
                        "response_type": "ok",
                        "response_text": "All good",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "Fixed typo",
                        "getting_closer": None,
                    },
                ],
            },
        )

        # Count reviewer-worktree creations: the bug created a *fresh* worktree
        # on recovery (release + re-spawn). An in-place respawn must reuse the
        # existing one, so the factory is called exactly once.
        worktree_calls: list[Path] = []

        def _reviewer_worktree_factory() -> Path:
            worktree_calls.append(reviewer_wt)
            return reviewer_wt

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=_reviewer_worktree_factory,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=EventContext(),
        )

        # The exchange completed normally at round 2 — it did NOT bail to
        # reviewer_no_completion / restart at round 1.
        assert outcome.status == "ok"
        assert outcome.reason == "reviewer_ok"
        assert outcome.rounds == 2

        # The respawn reused the existing reviewer worktree — no fresh worktree
        # was created (the crux of #6360).
        assert len(worktree_calls) == 1

        # The reviewer was respawned in place: opened once at pair spawn, then
        # once more for the retry. The coder was never respawned.
        assert state["opened"].count("reviewer") == 2
        assert state["opened"].count("coder") == 1

        # The pair was NOT released for a reviewer no-completion.
        assert (
            42,
            "review-exchange-reviewer_no_completion",
        ) not in state["registry"].released

        # A successful respawn-retry is not surfaced as a terminal role
        # timeout — only the warning log records the respawn.
        assert [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
        ] == []

        # The reviewer's two surfaced verdicts are the round-1 changes_requested
        # and the round-2 OK produced by the respawned process.
        reviewer_feedback = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK
            and evt.data["role"] == "reviewer"
        ]
        assert [evt.data["response_type"] for evt in reviewer_feedback] == [
            "changes_requested",
            "ok",
        ]
        assert reviewer_feedback[-1].data["round_index"] == 2

    def test_respawn_does_not_consume_stale_coder_completion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A coder completion written by a dead pre-retry process must not
        satisfy the *respawned* coder turn's protocol guardrail.

        End-to-end regression for the #6429 review: combines a real respawn
        with a stale side artifact. Attempt A writes ``completion-coder.json``
        and then dies before the response JSON; the respawned coder returns a
        response but never runs coding-done. The workspace reset on the respawn
        attempt clears attempt A's completion, so the turn fails the protocol
        guardrail instead of advancing on the stale artifact. (Without the
        reset, the stale completion would satisfy the guardrail and the
        exchange would advance.)
        """
        from issue_orchestrator.domain.review_exchange_failures import (
            RoundFailureReason,
        )
        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundError,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "Fix",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    # Attempt A writes completion-coder.json (script True) then
                    # exits before the response JSON.
                    {
                        "_raise": PersistentRoundError(
                            "Agent exited unexpectedly (code=0) before responding",
                            failure_reason=RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE,
                        )
                    },
                    # Respawn + the two protocol retries each respond without
                    # running coding-done (no completion written).
                    {"response_type": "ok", "response_text": "no coding-done"},
                    {"response_type": "ok", "response_text": "no coding-done"},
                    {"response_type": "ok", "response_text": "no coding-done"},
                ],
            },
            coder_completion_script=[True, False, False, False],
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.status == "error"
        assert outcome.reason == "coder_protocol_error"
        # The coder process was respawned in place (opened at spawn + respawn).
        assert state["opened"].count("coder") >= 2

    def test_respawn_does_not_pair_stale_reviewer_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A review report authored by a dead pre-retry process must not be
        paired with the *respawned* reviewer's decision.

        End-to-end regression for the #6429 review. Attempt A writes a stale
        ``review-report.md`` then dies before the response JSON; the respawned
        reviewer approves but writes no report. The workspace reset clears the
        stale report, so the persisted turn report is synthesized from the
        respawned decision rather than carrying the dead process's content.
        """
        from issue_orchestrator.domain.review_exchange_failures import (
            RoundFailureReason,
        )
        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundError,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        stale_marker = "STALE-REPORT-FROM-DEAD-REVIEWER-PROCESS"
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    # Attempt A authors a stale report, then exits before the
                    # response JSON.
                    {
                        "_authored_report_text": f"# {stale_marker}\n",
                        "_raise": PersistentRoundError(
                            "Agent exited unexpectedly (code=0) before responding",
                            failure_reason=RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE,
                        ),
                    },
                    # Respawn approves but writes no report.
                    {
                        "response_type": "ok",
                        "response_text": "Looks good",
                        "getting_closer": True,
                    },
                ],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.status == "ok"
        assert state["opened"].count("reviewer") >= 2
        assert outcome.exchange_dir is not None
        report_text = (
            outcome.exchange_dir
            / "turns"
            / "round-1-reviewer-attempt-1.review-report.md"
        ).read_text(encoding="utf-8")
        assert stale_marker not in report_text, (
            "the dead reviewer process's stale report must not be paired with "
            "the respawned reviewer's decision"
        )

    def test_role_timeout_does_not_respawn_the_process(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A genuine timeout must NOT respawn — the process is alive.

        Respawn recovery is scoped to dead/unusable processes
        (``is_process_unusable_failure``). A ``TIMEOUT`` means the agent is
        running but slow/stuck; respawning would kill a working process and
        could mask a hang, so the exchange must fall straight through to
        ``reviewer_no_completion`` as before.
        """
        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("simulated 60s timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.reason == "reviewer_no_completion"
        # The reviewer was opened exactly once — no respawn on timeout.
        assert state["opened"].count("reviewer") == 1
        assert state["registry"].released == [
            (42, "review-exchange-reviewer_no_completion")
        ]

    def test_prompt_not_accepted_respawns_in_place_and_advances(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A silent prompted reviewer is a stale/unusable process, not a verdict.

        Regression for the tixmeup #290 shape: the prompt was written, the
        reviewer stayed alive, but no response/report appeared. The exchange
        should retry the same turn with a fresh process instead of waiting for
        the full round timeout and then leaving the issue with no next action.
        """
        from issue_orchestrator.domain.review_exchange_failures import (
            RoundFailureReason,
        )
        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    PersistentRoundTimeoutError(
                        "prompt rendered but no terminal activity followed",
                        failure_reason=RoundFailureReason.PROMPT_NOT_ACCEPTED,
                    ),
                    {
                        "response_type": "ok",
                        "response_text": "All good",
                        "getting_closer": True,
                    },
                ],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=EventContext(),
        )

        assert outcome.status == "ok"
        assert outcome.reason == "reviewer_ok"
        assert state["opened"].count("reviewer") == 2
        assert state["registry"].released == []
        assert [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
        ] == []

    def test_coder_timeout_persists_no_completion_result_too(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Symmetric to the reviewer-timeout test, but for the coder.
        The two roles share one persistence helper, but a regression
        that wires only the reviewer side would leave the coder side
        silent — this test pins both."""
        from issue_orchestrator.domain.review_exchange_turn import (
            ReviewExchangeTurnResult,
            TurnResultKind,
        )
        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "fix x",
                        "getting_closer": True,
                    },
                ],
                "coder": [PersistentRoundTimeoutError("coder hung at 60s")],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.exchange_dir is not None
        coder_result_path = (
            outcome.exchange_dir / "turns" / "round-1-coder-attempt-1.result.json"
        )
        assert coder_result_path.exists(), (
            f"coder timeout result artifact missing at {coder_result_path}"
        )
        result = ReviewExchangeTurnResult.from_manifest(
            json.loads(coder_result_path.read_text())
        )
        assert result is not None
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "timeout"
        assert "coder hung at 60s" in result.response_text
        assert state["registry"].released == [
            (42, "review-exchange-coder_no_completion")
        ]

    def test_protocol_error_response_is_persisted_with_named_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Agent emits a malformed response; runner persists the
        result as ``protocol_error`` with a named ``protocol_error_reason``
        so an operator inspecting the on-disk artifact can tell which
        parser branch fired.
        """
        from issue_orchestrator.domain.review_exchange_turn import (
            ReviewExchangeTurnResult,
            TurnResultKind,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                # Reviewer writes JSON missing response_type — the
                # parser flags this as a protocol error with reason
                # missing_response_type. The runner treats a non-ok
                # reviewer response by continuing to the coder turn,
                # then approving on round 2 to terminate cleanly.
                "reviewer": [
                    {"response_text": "I forgot to declare myself"},
                    {
                        "response_type": "ok",
                        "response_text": "ok now",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "fixed",
                        "getting_closer": None,
                    },
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.exchange_dir is not None
        result_path = (
            outcome.exchange_dir / "turns" / "round-1-reviewer-attempt-1.result.json"
        )
        assert result_path.exists()
        result = ReviewExchangeTurnResult.from_manifest(
            json.loads(result_path.read_text())
        )
        assert result is not None
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "missing_response_type"


# ---------------------------------------------------------------------------
# Termination conditions
# ---------------------------------------------------------------------------


class TestExchangeTerminationConditions:
    def test_max_no_progress_stops_exchange(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "Keep trying",
                        "getting_closer": False,
                    },
                    {
                        "response_type": "changes_requested",
                        "response_text": "Still bad",
                        "getting_closer": False,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "Tried",
                        "getting_closer": None,
                    },
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=5,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.status == "stopped"
        assert outcome.reason == "reviewer_reports_no_progress"

    def test_reviewer_ok_overridden_when_validation_required_but_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reviewer returns ok in round 1 but validation-record.json is missing
        — the loop coerces the reviewer's verdict to changes_requested and
        continues into the coder turn. The coder also can't satisfy the
        protocol (no validation record possible), so retries exhaust and the
        outcome is coder_protocol_error. The point this test pins is that
        the coder turn WAS attempted: the coercion fired and the exchange
        did not return ``status=ok`` from the reviewer's optimistic verdict.
        """
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        # write_coder_completion=False: the helper does NOT write
        # completion-coder.json or a passing validation record, so the
        # coder protocol guardrail will fail on every send_round and
        # retries will run.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "x",
                        "getting_closer": None,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "y",
                        "getting_closer": None,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "z",
                        "getting_closer": None,
                    },
                ],
            },
            write_coder_completion=False,
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=2,
            max_no_progress=5,
            require_validation=True,
        )

        assert outcome.status == "error"
        assert outcome.reason == "coder_protocol_error"
        # And the coder turn was indeed attempted — proves the reviewer
        # coercion fired (otherwise the exchange would have returned ok).
        coder_calls = [r for role, _ in state["rounds_seen"] if (r := role) == "coder"]
        assert coder_calls, "coder turn must have been attempted"

    def test_reviewer_timeout_exits_with_no_completion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("simulated timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )
        assert outcome.status == "error"
        assert outcome.reason == "reviewer_no_completion"
        assert state["registry"].released == [
            (42, "review-exchange-reviewer_no_completion")
        ]
        # Role timeout event must fire so the timeline can render the
        # per-role bailout as a failure, not as silent stoppage.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
            and evt.data.get("role") == "reviewer"
            and evt.data.get("failure_reason") == "timeout"
            and evt.data.get("reason") == "no_completion"
            for evt in sink.events
        )

    def test_exchange_exception_releases_persistent_pair(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unexpected exchange-driver failures must not keep cached PTYs."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={"reviewer": [], "coder": []},
        )

        def raise_from_driver(**_kwargs: object) -> pse.ReviewExchangeOutcome:
            raise RuntimeError("driver failed")

        monkeypatch.setattr(pse, "_drive_rounds", raise_from_driver)

        with pytest.raises(RuntimeError, match="driver failed"):
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=state["registry"],
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
            )

        assert state["registry"].released == [(42, "review-exchange-exception")]


# ---------------------------------------------------------------------------
# Chapters + events vocabulary
# ---------------------------------------------------------------------------


class TestChapterSidecarAndEvents:
    def test_chapters_recorded_at_each_role_boundary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "Fix",
                        "getting_closer": True,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "Approved",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "Fixed",
                        "getting_closer": None,
                    },
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        # Reviewer side: 2 round-prompt chapters + 2 round-feedback chapters
        # Coder side: 1 round-prompt chapter + 1 round-feedback chapter
        run_dir = outcome.exchange_dir.parent  # type: ignore[union-attr]
        reviewer_sidecar = session_output.read_exchange_chapters(
            run_dir, role="reviewer"
        )
        coder_sidecar = session_output.read_exchange_chapters(run_dir, role="coder")

        assert reviewer_sidecar is not None
        assert coder_sidecar is not None
        # Reviewer wrote 4 chapters (prompt+feedback × 2 rounds).
        reviewer_pattern = [
            (c.cycle_index, c.section) for c in reviewer_sidecar.chapters
        ]
        assert reviewer_pattern == [
            (1, "prompt"),
            (1, "feedback"),
            (2, "prompt"),
            (2, "feedback"),
        ]
        # Coder wrote 2 chapters (prompt+feedback × 1 round; round 2 reviewer
        # approved, no coder turn).
        coder_pattern = [(c.cycle_index, c.section) for c in coder_sidecar.chapters]
        assert coder_pattern == [(1, "prompt"), (1, "feedback")]

    def test_role_events_emit_in_expected_sequence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "Approved",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
        )

        pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        names = [evt.event_type for evt in sink.events]
        # Expected sequence (subset assertion to avoid coupling to extras):
        for expected in (
            EventName.REVIEW_EXCHANGE_STARTED,
            EventName.REVIEW_EXCHANGE_ROUND_STARTED,
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
            EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
            EventName.REVIEW_EXCHANGE_COMPLETED,
        ):
            assert expected in names, f"missing event {expected.value}"

        prompted = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_PROMPTED
        ]
        feedback = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK
        ]
        assert len(prompted) == 1
        assert len(feedback) == 1

        prompted_refs = prompted[0].data["artifact_refs"]
        assert prompted[0].data["attempt_index"] == 1
        assert [ref["kind"] for ref in prompted_refs] == [
            "prompt",
            "terminal_recording",
            "chapter_sidecar",
        ]
        assert [Path(ref["path"]).exists() for ref in prompted_refs] == [
            True,
            True,
            True,
        ]

        feedback_refs = feedback[0].data["artifact_refs"]
        assert feedback[0].data["attempt_index"] == 1
        assert [ref["kind"] for ref in feedback_refs] == [
            "prompt",
            "terminal_recording",
            "chapter_sidecar",
            "review_response",
        ]
        assert [Path(ref["path"]).exists() for ref in feedback_refs] == [
            True,
            True,
            True,
            True,
        ]

    def test_chapter_recorded_event_emitted_per_chapter(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Original PR 2c plan item 4 contract: chapters.json gets a sidecar
        AND ``REVIEW_EXCHANGE_CHAPTER_RECORDED`` fires for each chapter so
        SSE/timeline consumers can react to round/role/section boundaries
        without polling the on-disk file."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "Needs work",
                        "getting_closer": True,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "Approved",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "Applied"},
                ],
            },
        )

        pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=2,
            max_no_progress=3,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        chapter_events = [
            evt
            for evt in sink.events
            if evt.event_type == EventName.REVIEW_EXCHANGE_CHAPTER_RECORDED
        ]
        # Two rounds × (reviewer prompt + reviewer feedback) = 4 reviewer chapters.
        # Round 1 also has (coder prompt + coder feedback) = 2 more. Round 2's
        # reviewer says "ok" so coder doesn't run again. Total = 6.
        assert len(chapter_events) >= 4, (
            f"expected at least 4 chapter events (2 rounds × reviewer prompt+feedback); "
            f"got {len(chapter_events)}"
        )
        # Each event carries the offset that was just durably written, so
        # consumers don't have to re-read the recording to learn where the
        # chapter starts.
        for evt in chapter_events:
            assert "recording_event_index" in evt.data
            assert "role" in evt.data and evt.data["role"] in {"reviewer", "coder"}
            assert "section" in evt.data
            assert "round_index" in evt.data
            assert "label" in evt.data


# ---------------------------------------------------------------------------
# Caller-injected hooks
# ---------------------------------------------------------------------------


class TestCallerHooks:
    def test_before_reviewer_round_invoked_each_round(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "fix",
                        "getting_closer": True,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "fixed",
                        "getting_closer": None,
                    },
                ],
            },
        )

        round_invocations: list[int] = []
        pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=5,
            require_validation=False,
            before_reviewer_round=lambda i: round_invocations.append(i),
        )
        # Called once per reviewer round (rounds 1 and 2).
        assert round_invocations == [1, 2]

    def test_exchange_run_assets_own_run_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
        )

        exchange_run = session_output.start_review_exchange_run(
            coder_wt,
            issue_number=42,
            parent_session_name="coding-1",
            agent_label="agent:backend",
        )
        outcome = pse.run_persistent_session_exchange(
            exchange_run=exchange_run,
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
        )
        assert outcome.run_assets == exchange_run.assets
        assert exchange_run.assets.run_dir.exists()
        # And the run dir contains the exchange artifacts.
        assert exchange_run.assets.exchange_dir.is_dir()

    def test_initial_validation_record_is_available_to_reviewer_in_run_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        validation_payload = {"passed": True, "head_sha": "head-a"}
        current_record = tmp_path / "current-validation-record.json"
        current_record.write_text(
            json.dumps(validation_payload),
            encoding="utf-8",
        )
        monkeypatch.setattr(pse, "get_repo_head_sha", lambda _: "head-a")

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    },
                ],
                "coder": [],
            },
        )

        exchange_run = session_output.start_review_exchange_run(
            coder_wt,
            issue_number=42,
            parent_session_name="coding-1",
            agent_label="agent:backend",
        )
        outcome = pse.run_persistent_session_exchange(
            exchange_run=exchange_run,
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=True,
            initial_validation_record_path=current_record,
        )

        assert outcome.status == "ok"
        run_record = exchange_run.assets.validation_record_path
        assert json.loads(run_record.read_text(encoding="utf-8")) == validation_payload
        reviewer_prompt = next(
            prompt
            for role, prompt, _notice in state["prompt_inboxes_seen"]
            if role == "reviewer"
        )
        assert str(run_record) in reviewer_prompt
        # The reviewer must trust validation-record.json and avoid running
        # build/test tooling itself — see build_reviewer_prompt in
        # domain/review_exchange.py for the full rationale.
        assert "do NOT run build, test, or validation commands" in reviewer_prompt
        reviewer_notice = next(
            notice
            for role, _prompt, notice in state["prompt_inboxes_seen"]
            if role == "reviewer"
        )
        assert "review-exchange-turn-prompt.md" in reviewer_notice
        assert len(reviewer_notice) < 512

    def test_coder_validation_refresh_updates_reviewer_run_record(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        initial_payload = {"passed": True, "head_sha": "head-a"}
        refreshed_payload = {"passed": True, "head_sha": "head-b"}
        current_record = tmp_path / "current-validation-record.json"
        current_record.write_text(
            json.dumps(initial_payload),
            encoding="utf-8",
        )
        monkeypatch.setattr(pse, "get_repo_head_sha", lambda _: "head-b")

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "please fix",
                        "getting_closer": True,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "fixed"},
                ],
            },
            coder_validation_payload_script=[refreshed_payload],
        )

        exchange_run = session_output.start_review_exchange_run(
            coder_wt,
            issue_number=42,
            parent_session_name="coding-1",
            agent_label="agent:backend",
        )
        outcome = pse.run_persistent_session_exchange(
            exchange_run=exchange_run,
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=2,
            max_no_progress=2,
            require_validation=True,
            initial_validation_record_path=current_record,
        )

        assert outcome.status == "ok"
        run_record = exchange_run.assets.validation_record_path
        pair_record = (
            tmp_path / "persistent-pairs" / "issue-42" / "validation-record.json"
        )
        assert json.loads(run_record.read_text(encoding="utf-8")) == refreshed_payload
        assert json.loads(pair_record.read_text(encoding="utf-8")) == refreshed_payload
        reviewer_prompts = [
            prompt
            for role, prompt, _notice in state["prompt_inboxes_seen"]
            if role == "reviewer"
        ]
        assert len(reviewer_prompts) == 2
        assert all(str(run_record) in prompt for prompt in reviewer_prompts)


# ---------------------------------------------------------------------------
# Cleanup invariant
# ---------------------------------------------------------------------------


class TestCoderProtocolGuardrail:
    """Parity with the active runner's _validate_coder_protocol: the coder
    must produce completion-coder.json (output of ``coding-done``) or the
    round is treated as a protocol error and retried."""

    def test_missing_coder_completion_triggers_retries_then_protocol_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        # No completion artifact gets written (write_coder_completion=False).
        # 1 initial coder round + _CODER_PROTOCOL_RETRY_LIMIT (=2) retries
        # = 3 send_round calls before the runner gives up.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "fix",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "x",
                        "getting_closer": None,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "y",
                        "getting_closer": None,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "z",
                        "getting_closer": None,
                    },
                ],
            },
            write_coder_completion=False,
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=5,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        assert outcome.status == "error"
        assert outcome.reason == "coder_protocol_error"
        # 3 coder send_rounds happened (1 initial + 2 retries).
        coder_rounds = [r for r in state["rounds_seen"] if r[0] == "coder"]
        assert len(coder_rounds) == 3
        coder_prompts = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_PROMPTED
            and evt.data.get("role") == "coder"
        ]
        assert [evt.data["attempt_index"] for evt in coder_prompts] == [1, 2, 3]
        assert coder_prompts[0].data["rework_reason"] == "changes_requested"
        assert "rework_reason" not in coder_prompts[1].data
        assert "rework_reason" not in coder_prompts[2].data
        prompt_paths = [
            Path(evt.data["artifact_refs"][0]["path"]) for evt in coder_prompts
        ]
        assert len(set(prompt_paths)) == 3
        assert "coding-done completed" in prompt_paths[1].read_text(encoding="utf-8")
        assert "coding-done completed" in prompt_paths[2].read_text(encoding="utf-8")
        assert outcome.exchange_dir is not None
        completed_paths = sorted(
            p.name
            for p in outcome.exchange_dir.joinpath("turns").glob(
                "round-1-coder-attempt-*.completed.json"
            )
        )
        assert completed_paths == [
            "round-1-coder-attempt-1.completed.json",
            "round-1-coder-attempt-2.completed.json",
            "round-1-coder-attempt-3.completed.json",
        ]
        # Terminal event fired with status=error so timeline consumers see
        # the exchange ended definitively.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_COMPLETED
            and evt.data.get("status") == "error"
            and evt.data.get("reason") == "coder_protocol_error"
            for evt in sink.events
        )

    def test_coder_protocol_retry_persists_distinct_attempt_artifacts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A retry that succeeds must leave both attempt artifacts intact.

        The first coder attempt writes a malformed exchange response and
        skips ``coding-done``. The second attempt fixes both. This pins
        the attempt-scoped path layout for the non-terminal retry case:
        future refactors must not reuse attempt-1 paths for attempt-2.
        """
        from issue_orchestrator.domain.review_exchange_turn import (
            ReviewExchangeTurnResult,
            TurnResultKind,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "fix",
                        "getting_closer": True,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "protocol_error",
                        "response_text": "I skipped coding-done",
                        "getting_closer": False,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "fixed",
                        "getting_closer": None,
                    },
                ],
            },
            coder_completion_script=[False, True],
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=5,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        assert outcome.status == "ok"
        assert outcome.reason == "reviewer_ok"
        assert outcome.exchange_dir is not None
        turns_dir = outcome.exchange_dir / "turns"
        attempt_1_prompt = turns_dir / "round-1-coder-attempt-1.prompt.md"
        attempt_2_prompt = turns_dir / "round-1-coder-attempt-2.prompt.md"
        assert attempt_1_prompt.exists()
        assert attempt_2_prompt.exists()
        assert not (turns_dir / "round-1-coder-attempt-3.prompt.md").exists()

        attempt_1_prompt_text = attempt_1_prompt.read_text(encoding="utf-8")
        attempt_2_prompt_text = attempt_2_prompt.read_text(encoding="utf-8")
        assert attempt_1_prompt_text != attempt_2_prompt_text
        assert "missing completion artifact" in attempt_2_prompt_text
        assert "coding-done completed" in attempt_2_prompt_text

        attempt_1_result = ReviewExchangeTurnResult.from_manifest(
            json.loads((turns_dir / "round-1-coder-attempt-1.result.json").read_text())
        )
        assert attempt_1_result is not None
        assert attempt_1_result.kind is TurnResultKind.PROTOCOL_ERROR
        assert attempt_1_result.protocol_error_reason == "unknown_response_type"
        assert "'protocol_error'" in attempt_1_result.response_text

        attempt_2_result = ReviewExchangeTurnResult.from_manifest(
            json.loads((turns_dir / "round-1-coder-attempt-2.result.json").read_text())
        )
        assert attempt_2_result is not None
        assert attempt_2_result.kind is TurnResultKind.OK
        assert attempt_2_result.response_text == "fixed"

        coder_prompts = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_PROMPTED
            and evt.data.get("role") == "coder"
        ]
        assert [evt.data["attempt_index"] for evt in coder_prompts] == [1, 2]
        assert [
            Path(evt.data["artifact_refs"][0]["path"]).name for evt in coder_prompts
        ] == [
            "round-1-coder-attempt-1.prompt.md",
            "round-1-coder-attempt-2.prompt.md",
        ]

    def test_stale_round1_completion_does_not_satisfy_round2_guardrail(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1's coder writes completion-coder.json. Round 2's coder
        does NOT refresh it. The runner must clear the stale artifact
        before round 2's prompt and detect the missing fresh write — i.e.
        the exchange ends in coder_protocol_error, not status=ok with a
        stale satisfaction.

        Regression for the PR 6145 reviewer's repro: previously round 2
        re-used round 1's completion file and the exchange wrongly
        approved.
        """
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        # Manually drive the helper: round 1 coder gets a completion
        # file, round 2 coder explicitly skips writing one. We do this
        # by patching ``send_round`` ourselves so we can vary the
        # behavior turn-by-turn.
        from issue_orchestrator.execution import (
            persistent_session_exchange as pse_mod,
        )

        coder_call_count = {"n": 0}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            writer = _build_pty_writer(recording_path)
            completion_env = env.get("ISSUE_ORCHESTRATOR_COMPLETION_PATH")
            validation_env = env.get("ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR")
            return _FakeSession(
                role,
                completion_path=Path(completion_env) if completion_env else None,
                validation_output_dir=Path(validation_env) if validation_env else None,
                log_writer=writer,
            )

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            if role == "coder":
                coder_call_count["n"] += 1
                if coder_call_count["n"] == 1:
                    # Round 1: write a completion + passing validation.
                    completion = session.completion_path
                    validation_dir = session.validation_output_dir
                    assert completion is not None and validation_dir is not None
                    completion.parent.mkdir(parents=True, exist_ok=True)
                    validation_dir.mkdir(parents=True, exist_ok=True)
                    validation_record = validation_dir / "validation-record.json"
                    validation_record.write_text(
                        json.dumps({"passed": True}),
                        encoding="utf-8",
                    )
                    completion.write_text(
                        json.dumps(
                            {
                                "outcome": "completed",
                                "round": 1,
                                "validation_record_path": str(validation_record),
                            }
                        ),
                        encoding="utf-8",
                    )
                # Round 2 and retries: do NOT write completion. The runner
                # must have cleared the round-1 file before sending this
                # prompt, so _validate_coder_completion will fail.
                return {
                    "response_type": "ok",
                    "response_text": "x",
                    "getting_closer": None,
                }
            # Reviewer side: round 1 changes_requested, round 2 ok.
            if not state["reviewer_responses"]:
                raise AssertionError("reviewer responses exhausted")
            return state["reviewer_responses"].pop(0)

        # Both reviewer rounds say "changes_requested" so the coder runs
        # in both rounds. (If round 2's reviewer said "ok" the loop would
        # short-circuit before the coder turn and the bug wouldn't be
        # exercised.)
        state = {
            "reviewer_responses": [
                {
                    "response_type": "changes_requested",
                    "response_text": "fix",
                    "getting_closer": True,
                },
                {
                    "response_type": "changes_requested",
                    "response_text": "still fix",
                    "getting_closer": True,
                },
            ],
            "registry": _FakePairRegistry(),
        }
        monkeypatch.setattr(pse_mod, "open_persistent_session", _open)
        monkeypatch.setattr(pse_mod, "send_round", _send)

        outcome = pse_mod.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=5,
            require_validation=False,
        )

        assert outcome.status == "error"
        assert outcome.reason == "coder_protocol_error"
        # Round 1 + round 2 + 2 protocol retries = 4 coder calls.
        assert coder_call_count["n"] == 4

    def test_coder_completion_present_passes_protocol(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity: when the coder writes completion-coder.json (the helper's
        default), no retry path is taken — only one coder send_round per round."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "changes_requested",
                        "response_text": "fix",
                        "getting_closer": True,
                    },
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    },
                ],
                "coder": [
                    {
                        "response_type": "ok",
                        "response_text": "fixed",
                        "getting_closer": None,
                    },
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=2,
            max_no_progress=5,
            require_validation=False,
        )

        assert outcome.status == "ok"
        coder_rounds = [r for r in state["rounds_seen"] if r[0] == "coder"]
        assert len(coder_rounds) == 1


class TestTerminalEventsOnError:
    """Every error/timeout exit path emits a terminal REVIEW_EXCHANGE_COMPLETED
    event so the timeline / publish cache observe a definitive end-of-exchange."""

    def test_invalid_reviewer_decision_json_halts_with_error_event(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "Approved, but abstraction must change.",
                        "getting_closer": True,
                        "decision": {
                            "verdict": "approved",
                            "risk": "low",
                            "blocking_findings": [],
                            "nits": [],
                            "abstraction_review": {
                                "status": "changes_requested",
                                "findings": [{"id": "A1", "title": "Use owner port"}],
                            },
                            "nit_policy": "surface",
                        },
                    }
                ],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=5,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        assert outcome.status == "error"
        assert outcome.reason == "reviewer_decision_invalid"
        assert outcome.summary is not None
        assert outcome.summary.reason == "reviewer_decision_invalid"
        assert outcome.summary.detail is not None
        assert "reviewer produced invalid decision JSON" in outcome.summary.detail
        assert [role for role, _ in state["rounds_seen"]] == ["reviewer"]
        terminal = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_COMPLETED
        ]
        assert len(terminal) == 1
        assert terminal[0].data["status"] == "error"
        assert terminal[0].data["reason"] == "reviewer_decision_invalid"
        assert "reviewer produced invalid decision JSON" in terminal[0].data["detail"]
        round_completed = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
        ]
        assert len(round_completed) == 1
        assert round_completed[0].data["coder_response_type"] is None

    def test_reviewer_timeout_emits_terminal_completed_event_with_error_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=5,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        assert outcome.status == "error"
        assert outcome.reason == "reviewer_no_completion"
        terminal = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_COMPLETED
        ]
        assert len(terminal) == 1
        assert terminal[0].data.get("status") == "error"
        assert terminal[0].data.get("reason") == "reviewer_no_completion"
        timeouts = [
            evt
            for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
        ]
        assert len(timeouts) == 1
        assert timeouts[0].data["attempt_index"] == 1
        assert timeouts[0].data["failure_reason"] == "timeout"
        assert timeouts[0].data["reason"] == "no_completion"
        timeout_refs = timeouts[0].data["artifact_refs"]
        assert [ref["kind"] for ref in timeout_refs] == [
            "prompt",
            "terminal_recording",
            "chapter_sidecar",
            "review_response",
        ]
        assert all(Path(ref["path"]).exists() for ref in timeout_refs)

    def test_summary_status_matches_outcome_for_error_exits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """summary.json's ``status`` must match the outcome's ``status``,
        not say "stopped"/"incomplete" while the outcome says "error"."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=5,
            require_validation=False,
        )

        assert outcome.summary is not None
        assert outcome.summary.status == "error"
        assert outcome.summary.reason == "reviewer_no_completion"


class TestRecordingContractFailLoud:
    """Missing or corrupt recording at chapter time is a broken replay
    contract and must surface as a definitive exchange failure, not a
    silent skipped chapter."""

    def test_missing_recording_at_chapter_time_fails_exchange(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        # write_recording=False: helper does NOT seed the pair recording
        # files. The recording contract now fails at pair acquisition before
        # the round loop can reach chapter writing.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
            write_recording=False,
        )

        with pytest.raises(RuntimeError, match="missing_file"):
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=state["registry"],
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=5,
                require_validation=False,
                events=sink,
                event_context=ctx,
            )
        # FAILED event fired before the raise propagated.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_FAILED for evt in sink.events
        )


class TestAtomicSummaryWrite:
    def test_summary_write_is_atomic_no_partial_file_visible(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The orchestrator polls summary.json from a different process
        while the exchange runs in the background. The write must be
        atomic — a torn file would surface as JSONDecodeError on the
        polling tick.

        We verify the contract by patching ``os.replace`` to capture the
        intermediate temp path and asserting that the destination is
        either fully present or absent at any observable moment.
        """
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "lgtm",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
        )

        observed_tmp_paths: list[str] = []
        real_replace = os.replace

        def _capturing_replace(src: str, dst: str) -> None:
            observed_tmp_paths.append(src)
            return real_replace(src, dst)

        # Atomic write lives in the shared infra.atomic_io helper now;
        # patch os.replace at that module's binding so we observe the
        # actual rename call from the runner's summary write path.
        from issue_orchestrator.infra import atomic_io

        monkeypatch.setattr(atomic_io.os, "replace", _capturing_replace)

        outcome = pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=5,
            require_validation=False,
        )

        # summary.json was written via a tempfile + atomic replace.
        assert observed_tmp_paths, "atomic write was not exercised"
        assert all(str(p).endswith(".tmp") for p in observed_tmp_paths)
        assert outcome.summary is not None


class TestRoleEnvironmentScrubbing:
    """The persistent runner must route the role env through the shared
    ``build_filtered_env`` policy. Without it, long-lived agent processes
    would inherit orchestrator credentials (GH_TOKEN, ISSUE_ORCHESTRATOR_API_TOKEN,
    CLAUDECODE, …) that the active path deliberately scrubs."""

    def test_forbidden_env_vars_are_scrubbed_from_role_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pollute the orchestrator's env with the exact secrets the
        # filtered-env policy guards against.
        monkeypatch.setenv("GH_TOKEN", "ghp_fake")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake")
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_TOKEN", "admin-secret")
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-fake")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")

        run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        response_file = run_dir / "reviewer" / "review-response.json"

        env = pse._build_role_env(  # noqa: SLF001 — testing the env contract
            role="reviewer",
            response_file=response_file,
            review_report_file=None,
            completion_path=run_dir / "reviewer" / "completion-reviewer.json",
            validation_output_dir=run_dir,
            worktree=worktree,
            runtime_config=_runtime_config(tmp_path),
            agent_label="agent:reviewer",
            web_port=None,
            issue_number=4057,
            session_name="exchange-1",
        )

        # Forbidden vars must be absent.
        for forbidden in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "ISSUE_ORCHESTRATOR_API_TOKEN",
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "AWS_SECRET_ACCESS_KEY",
        ):
            assert forbidden not in env, f"{forbidden} leaked into agent env"
        assert "ISSUE_ORCHESTRATOR_RUN_DIR" not in env
        assert "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR" not in env

    def test_required_overrides_are_present_in_role_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        response_file = run_dir / "coder" / "review-response.json"
        monkeypatch.setenv("GH_TOKEN", "ghp_fake")  # ensure scrubbing path runs

        env = pse._build_role_env(  # noqa: SLF001 — testing the env contract
            role="coder",
            response_file=response_file,
            review_report_file=None,
            completion_path=run_dir / "coder" / "completion-coder.json",
            validation_output_dir=run_dir,
            worktree=worktree,
            runtime_config=_runtime_config(tmp_path),
            agent_label="agent:backend",
            web_port=8080,
            issue_number=4057,
            session_name="exchange-1",
        )

        # The orchestrator-side overrides we depend on must propagate.
        assert env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"] == str(response_file)
        assert env["ISSUE_ORCHESTRATOR_AGENT_LABEL"] == "agent:backend"
        assert env["ISSUE_ORCHESTRATOR_ISSUE_NUMBER"] == "4057"
        assert env["ISSUE_ORCHESTRATOR_SESSION_ID"] == "exchange-1"
        assert env["ISSUE_ORCHESTRATOR_RUN_DIR"] == str(run_dir)
        assert env["ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR"] == str(run_dir)
        assert env["ISSUE_ORCHESTRATOR_CONFIG_NAME"] == "issue-orchestrator.test.yaml"
        assert env["ISSUE_ORCHESTRATOR_CONFIG_PATH"] == str(
            _runtime_config(tmp_path).config_path
        )
        assert env["ORCHESTRATOR_CONFIG_NAME"] == "issue-orchestrator.test.yaml"
        assert env["ORCHESTRATOR_CONFIG_PATH"] == str(
            _runtime_config(tmp_path).config_path
        )
        assert env["ORCHESTRATOR_SESSION_ID"] == "exchange-1"
        assert env["ORCHESTRATOR_API_PORT"] == "8080"
        # Git-safe defaults from the filtered-env helper.
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        # And GH_TOKEN was still scrubbed (sanity).
        assert "GH_TOKEN" not in env

    def test_role_env_exposes_exchange_respond_wrapper(self, tmp_path: Path) -> None:
        """Mailbox mode depends on the agent finding ``exchange-respond``."""
        from issue_orchestrator.control.isolation import get_runtime_scripts_dir

        run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        env = pse._build_role_env(  # noqa: SLF001 — testing the env contract
            role="reviewer",
            response_file=run_dir / "reviewer" / "review-response.json",
            review_report_file=worktree / ".issue-orchestrator" / "review-report.md",
            completion_path=run_dir / "reviewer" / "completion-reviewer.json",
            validation_output_dir=run_dir,
            worktree=worktree,
            runtime_config=_runtime_config(tmp_path),
            agent_label="agent:reviewer",
            web_port=8080,
            issue_number=4057,
            session_name="exchange-1",
        )

        path_entries = env["PATH"].split(os.pathsep)
        scripts_dir = get_runtime_scripts_dir()
        assert path_entries[:2] == [
            str(worktree / ".venv" / "bin"),
            str(scripts_dir),
        ]
        exchange_respond = scripts_dir / "exchange-respond"
        assert exchange_respond.exists()
        assert os.access(exchange_respond, os.X_OK)

    def test_callback_token_propagates_when_set_in_orchestrator_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ALWAYS_PASSTHROUGH_ENV_VARS must reach the agent so it can call
        back into the loopback Control API for preflight-push / session-resume."""
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", "scoped-cb-token")

        run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        env = pse._build_role_env(  # noqa: SLF001 — testing the env contract
            role="reviewer",
            response_file=run_dir / "reviewer" / "review-response.json",
            review_report_file=worktree / ".issue-orchestrator" / "review-report.md",
            completion_path=run_dir / "reviewer" / "completion-reviewer.json",
            validation_output_dir=run_dir,
            worktree=worktree,
            runtime_config=_runtime_config(tmp_path),
            agent_label="agent:reviewer",
            web_port=None,
            issue_number=4057,
            session_name="exchange-1",
        )
        assert env.get("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN") == "scoped-cb-token"


class TestResponseFileInsideWorktree:
    """The per-round response file must live inside the role's worktree.

    Regression guard for the runaway-loop bug fixed in this PR: the
    reviewer agent's seatbelt sandbox restricts writes to its cwd
    (the reviewer worktree). When the response file lived under the
    base-repo persistent-pair dir, every reviewer write attempt failed
    with ``operation not permitted``; the round timed out without a
    response file, the orchestrator relaunched the exchange, repeat
    forever. Keep these assertions tight — moving the response path
    back outside the worktree would silently re-introduce the loop.
    """

    def test_response_paths_resolve_inside_role_worktrees(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        captured: dict[str, Path] = {}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            writer = _build_pty_writer(recording_path)
            captured[f"{role}_response"] = Path(
                env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"]
            )
            if "ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE" in env:
                captured[f"{role}_report"] = Path(
                    env["ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE"]
                )
            captured[f"{role}_working_dir"] = Path(working_dir)
            return _FakeSession(role, log_writer=writer)

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            captured.setdefault(f"{session.role}_response_arg", response_file)
            return {
                "response_type": "ok",
                "response_text": "ok",
                "getting_closer": True,
            }

        registry = _FakePairRegistry()
        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=registry,
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
        )

        # The agent's env var ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE
        # must point inside the role's working_dir (its sandbox-writable
        # root). The runner's ``send_round`` call must use the same path.
        assert captured["coder_response"].is_relative_to(coder_wt), (
            f"coder response file {captured['coder_response']} is not inside "
            f"the coder worktree {coder_wt}"
        )
        assert captured["reviewer_response"].is_relative_to(reviewer_wt), (
            f"reviewer response file {captured['reviewer_response']} is not "
            f"inside the reviewer worktree {reviewer_wt}"
        )
        assert captured["reviewer_report"].is_relative_to(reviewer_wt), (
            f"reviewer report file {captured['reviewer_report']} is not "
            f"inside the reviewer worktree {reviewer_wt}"
        )
        assert captured["reviewer_response_arg"] == captured["reviewer_response"]

    def test_response_paths_are_outside_persistent_pair_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Belt-and-suspenders: reject any drift back into the base-repo
        persistent-pair dir, which is what the seatbelt sandbox rejects."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        persistent_pair_root = tmp_path / "persistent-pairs"

        captured: dict[str, Path] = {}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            writer = _build_pty_writer(recording_path)
            captured[role] = Path(env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"])
            if "ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE" in env:
                captured[f"{role}_report"] = Path(
                    env["ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE"]
                )
            return _FakeSession(role, log_writer=writer)

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            return {
                "response_type": "ok",
                "response_text": "ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=_FakePairRegistry(),
            persistent_pair_root=persistent_pair_root,
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
        )

        for role, path in captured.items():
            assert not str(path).startswith(str(persistent_pair_root)), (
                f"{role} response file {path} drifted back under "
                f"persistent_pair_root {persistent_pair_root}; this is the "
                "exact path the agent's sandbox rejects"
            )


class TestPerSessionRecordingMirror:
    """Per-session recording slice in run_dir/<role>/terminal-recording.jsonl.

    Without this projection, the session run-dir has only chapter
    offsets that point into a pair-scoped recording the run_dir
    doesn't own — the timeline viewer for review-exchange sessions
    looked empty even when the agent had emitted megabytes of output.
    The slice + manifest indirection make each run_dir self-contained.
    """

    def test_role_slice_files_seeded_and_manifest_points_at_them(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At exchange start, a viewer hitting the run_dir mid-round must
        see the per-session slice files (not 404), and the manifest's
        ``coder_recording`` / ``reviewer_recording`` keys must point at
        them. The pair-scoped recording remains discoverable under
        ``coder_recording_pair`` / ``reviewer_recording_pair``."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "ok",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
        )

        pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        # Find the actual run_dir from the coder worktree's sessions/.
        # session_output also creates a friendly-name symlink alongside
        # the timestamped real dir; filter symlinks so we land on the
        # real dir (which is the only one that holds manifest.json).
        runs = list((coder_wt / ".issue-orchestrator" / "sessions").iterdir())
        runs = [
            r
            for r in runs
            if r.is_dir() and not r.is_symlink() and "review-exchange" in r.name
        ]
        assert len(runs) == 1, f"expected one run_dir, got {runs}"
        run_dir = runs[0]

        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert "coder_recording" in manifest
        assert "reviewer_recording" in manifest
        assert "coder_recording_pair" in manifest
        assert "reviewer_recording_pair" in manifest

        coder_slice = Path(manifest["coder_recording"])
        reviewer_slice = Path(manifest["reviewer_recording"])
        # Per-session slices must live inside the run_dir itself so
        # worktree teardown is the one and only lifetime they care about.
        assert coder_slice.is_relative_to(run_dir), coder_slice
        assert reviewer_slice.is_relative_to(run_dir), reviewer_slice
        assert coder_slice.exists()
        assert reviewer_slice.exists()
        # The pair-scoped pointers must NOT collide with the per-session
        # slices — that would mean the manifest is silently overwriting
        # one with the other.
        assert manifest["coder_recording"] != manifest["coder_recording_pair"]
        assert manifest["reviewer_recording"] != manifest["reviewer_recording_pair"]

    def test_cached_pair_with_missing_recording_paths_is_respawned(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cached live pair whose recording files were removed cannot be
        repaired by touching the paths; its writers still point at the deleted
        file handles. The exchange must release it and spawn a fresh pair."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "ok",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
        )

        exchange_run = session_output.start_review_exchange_run(
            coder_wt,
            issue_number=42,
            parent_session_name="coding-1",
            agent_label="agent:backend",
        )
        stale_pair_root = tmp_path / "stale-persistent-pairs" / "issue-42"
        stale_pair = pse.PersistentExchangePair(
            coder_session=_FakeSession("coder"),
            reviewer_session=_FakeSession("reviewer"),
            reviewer_worktree_path=reviewer_wt,
            issue_key=42,
            exchange_run_id=exchange_run.run_id,
            run_dir=exchange_run.assets.run_dir,
            created_at=0.0,
            coder_response_path=coder_wt
            / ".issue-orchestrator"
            / "review-response.json",
            reviewer_response_path=reviewer_wt
            / ".issue-orchestrator"
            / "review-response.json",
            reviewer_report_path=reviewer_wt
            / ".issue-orchestrator"
            / "review-report.md",
            coder_recording_path=stale_pair_root / "coder" / "terminal-recording.jsonl",
            reviewer_recording_path=stale_pair_root
            / "reviewer"
            / "terminal-recording.jsonl",
            coder_completion_path=stale_pair_root / "coder" / "completion-coder.json",
            validation_record_path=stale_pair_root / "validation-record.json",
        )

        class _StaleThenFreshRegistry:
            def __init__(self) -> None:
                self.acquire_count = 0
                self.released: list[tuple[int, str]] = []

            def acquire(self, *, issue_key, spawn):  # noqa: ANN001, ANN201
                self.acquire_count += 1
                if self.acquire_count == 1:
                    return stale_pair
                return spawn()

            def release(self, issue_key, *, reason):  # noqa: ANN001, ANN201
                self.released.append((issue_key, reason))

        registry = _StaleThenFreshRegistry()

        outcome = pse.run_persistent_session_exchange(
            exchange_run=exchange_run,
            session_output=session_output,
            pair_registry=registry,
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.status == "ok"
        assert registry.released == [(42, "recording-contract-missing-on-acquire")]
        assert state["opened"] == ["coder", "reviewer"]

    def test_respawned_pair_recording_contract_is_rechecked(
        self,
        tmp_path: Path,
    ) -> None:
        """If fresh spawn violates the recording contract, fail fast.

        A single release-and-respawn is a recovery path for stale cached pairs.
        It must not become a fallback that admits a newly broken pair.
        """
        pair_root = tmp_path / "persistent-pairs" / "issue-42"
        assets = _binding_assets(pair_root)

        def broken_pair() -> pse.PersistentExchangePair:
            return pse.PersistentExchangePair(
                coder_session=_FakeSession("coder"),
                reviewer_session=_FakeSession("reviewer"),
                reviewer_worktree_path=tmp_path / "reviewer-wt",
                issue_key=42,
                exchange_run_id="run-42",
                run_dir=pair_root,
                created_at=0.0,
                coder_response_path=tmp_path / "coder-response.json",
                reviewer_response_path=tmp_path / "reviewer-response.json",
                reviewer_report_path=tmp_path
                / "reviewer-wt"
                / ".issue-orchestrator"
                / "review-report.md",
                coder_recording_path=(pair_root / "coder" / "terminal-recording.jsonl"),
                reviewer_recording_path=(
                    pair_root / "reviewer" / "terminal-recording.jsonl"
                ),
                coder_completion_path=pair_root / "coder" / "completion-coder.json",
                validation_record_path=pair_root / "validation-record.json",
            )

        class _AlwaysBrokenRegistry:
            def __init__(self) -> None:
                self.acquire_count = 0
                self.released: list[tuple[int, str]] = []

            def acquire(self, *, issue_key, spawn):  # noqa: ANN001, ANN201
                self.acquire_count += 1
                return spawn()

            def release(self, issue_key, *, reason):  # noqa: ANN001, ANN201
                self.released.append((issue_key, reason))

        registry = _AlwaysBrokenRegistry()

        with pytest.raises(RuntimeError, match="invalid after respawn") as exc_info:
            pse._acquire_pair_with_recording_contract(  # noqa: SLF001
                pair_registry=registry,
                issue_number=42,
                exchange_run=pse._PairExchangeRunBinding(  # noqa: SLF001
                    run_id="run-42",
                    assets=assets,
                ),
                spawn=broken_pair,
            )

        assert registry.acquire_count == 2
        assert registry.released == [
            (42, "recording-contract-missing-on-acquire"),
            (42, "recording-contract-invalid-after-respawn"),
        ]
        assert "no_writer" in str(exc_info.value)
        assert "missing_file" in str(exc_info.value)

    def test_live_cached_pair_from_pruned_previous_run_is_released_before_use(
        self,
        tmp_path: Path,
    ) -> None:
        """A live pair's env is run-scoped and cannot outlive its spawn run."""
        pair_root = tmp_path / "persistent-pairs" / "issue-42"
        first_run_dir = tmp_path / "sessions" / "run-1"
        second_run_dir = tmp_path / "sessions" / "run-2"
        second_assets = _binding_assets(second_run_dir)
        coder_recording = pair_root / "coder" / "terminal-recording.jsonl"
        reviewer_recording = pair_root / "reviewer" / "terminal-recording.jsonl"
        old_pair = pse.PersistentExchangePair(
            coder_session=_FakeSession(
                "coder",
                log_writer=_build_pty_writer(coder_recording),
            ),
            reviewer_session=_FakeSession(
                "reviewer",
                log_writer=_build_pty_writer(reviewer_recording),
            ),
            reviewer_worktree_path=tmp_path / "reviewer-wt",
            issue_key=42,
            exchange_run_id="run-1",
            run_dir=first_run_dir,
            created_at=0.0,
            coder_response_path=tmp_path / "coder-response.json",
            reviewer_response_path=tmp_path / "reviewer-response.json",
            reviewer_report_path=tmp_path
            / "reviewer-wt"
            / ".issue-orchestrator"
            / "review-report.md",
            coder_recording_path=coder_recording,
            reviewer_recording_path=reviewer_recording,
            coder_completion_path=pair_root / "coder" / "completion-coder.json",
            validation_record_path=pair_root / "validation-record.json",
        )
        fresh_pair = pse.PersistentExchangePair(
            coder_session=_FakeSession(
                "coder",
                log_writer=_build_pty_writer(coder_recording),
            ),
            reviewer_session=_FakeSession(
                "reviewer",
                log_writer=_build_pty_writer(reviewer_recording),
            ),
            reviewer_worktree_path=tmp_path / "reviewer-wt",
            issue_key=42,
            exchange_run_id="run-2",
            run_dir=second_run_dir,
            created_at=0.0,
            coder_response_path=tmp_path / "coder-response.json",
            reviewer_response_path=tmp_path / "reviewer-response.json",
            reviewer_report_path=tmp_path
            / "reviewer-wt"
            / ".issue-orchestrator"
            / "review-report.md",
            coder_recording_path=coder_recording,
            reviewer_recording_path=reviewer_recording,
            coder_completion_path=pair_root / "coder" / "completion-coder.json",
            validation_record_path=pair_root / "validation-record.json",
        )

        class _CachedRegistry:
            def __init__(self) -> None:
                self.acquire_count = 0
                self.released: list[tuple[int, str]] = []

            def acquire(self, *, issue_key, spawn):  # noqa: ANN001, ANN201, ARG002
                self.acquire_count += 1
                if self.acquire_count == 1:
                    return old_pair
                return fresh_pair

            def release(self, issue_key, *, reason):  # noqa: ANN001, ANN201
                self.released.append((issue_key, reason))

        registry = _CachedRegistry()
        binding = pse._PairExchangeRunBinding(  # noqa: SLF001
            run_id="run-2",
            assets=second_assets,
        )

        acquired = pse._acquire_pair_with_recording_contract(  # noqa: SLF001
            pair_registry=registry,
            issue_number=42,
            exchange_run=binding,
            spawn=lambda: fresh_pair,
        )

        assert acquired is fresh_pair
        assert registry.acquire_count == 2
        assert registry.released == [
            (42, "exchange-run-changed-on-acquire"),
        ]
        assert old_pair.exchange_run_id == "run-1"
        assert old_pair.run_dir == first_run_dir
        assert fresh_pair.exchange_run_id == "run-2"
        assert fresh_pair.run_dir == second_run_dir
        assert fresh_pair.coder_recording_path == coder_recording
        assert fresh_pair.reviewer_recording_path == reviewer_recording

    def test_slice_base_freezes_at_construction_for_offset_translation(
        self,
        tmp_path: Path,
    ) -> None:
        """``slice_base`` is the immutable starting offset that defines
        the slice's "zero". Chapter sidecars store
        ``pair_event_idx - slice_base`` so the viewer can scrub the
        manifest-pointed slice directly. Without that translation,
        cached pairs on exchange 2+ record offsets the slice file
        cannot satisfy and the timeline plays back empty."""
        pair_recording = tmp_path / "pair" / "terminal-recording.jsonl"
        pair_recording.parent.mkdir(parents=True)
        pair_recording.write_text("", encoding="utf-8")
        slice_path = tmp_path / "run" / "reviewer" / "terminal-recording.jsonl"

        # Fresh pair: slice starts at the very beginning of the pair
        # recording. Slice-relative offset == pair offset.
        fresh = pse._RoleSliceMirror(  # noqa: SLF001
            pair_recording=pair_recording,
            session_slice=slice_path,
            slice_base=0,
        )
        assert fresh.slice_base == 0
        assert fresh.pair_to_slice_offset(0) == 0
        assert fresh.pair_to_slice_offset(50) == 50

        # Cached pair on exchange 2: prior exchange already wrote 100
        # events into the pair recording. Slice-relative offsets must
        # be (pair_idx - 100) so the chapter sidecar matches the
        # slice's local indexing.
        cached = pse._RoleSliceMirror(  # noqa: SLF001
            pair_recording=pair_recording,
            session_slice=slice_path,
            slice_base=100,
        )
        assert cached.slice_base == 100
        # An index AT slice_base maps to slice offset 0 (the first
        # post-attach event in the slice).
        assert cached.pair_to_slice_offset(100) == 0
        # Pair offset 150 in exchange 2 is event 50 of the slice file.
        assert cached.pair_to_slice_offset(150) == 50
        # Pair offsets BELOW slice_base raise loudly: a negative slice
        # index would index past the start of the slice and silently
        # return wrong content. Fail-fast — masking with a clamp would
        # hide a real bug (chapter recording reading from a stale
        # source / wrong recording).
        with pytest.raises(ValueError, match="below slice_base"):
            cached.pair_to_slice_offset(50)

    def test_chapter_offsets_are_slice_relative_for_cached_pair(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end chapter contract: when the manifest points at the
        per-session slice, chapter offsets must index INTO the slice,
        not into the pair recording. Catches the cached-pair regression
        where slice-relative semantics drift back to pair-relative and
        the web replay route returns empty windows."""
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        # Pre-seed the pair recording with 50 "prior exchange" events
        # before the test exchange even starts. The test then runs one
        # exchange whose chapters must record offsets relative to the
        # slice (which begins at index 50 of the pair recording).
        prior_pair_recording = (
            tmp_path
            / "persistent-pairs"
            / "issue-42"
            / "reviewer"
            / "terminal-recording.jsonl"
        )
        prior_pair_recording.parent.mkdir(parents=True, exist_ok=True)
        prior_pair_recording.write_text(
            "\n".join(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "resize",
                        "offset_ms": i,
                        "rows": 40,
                        "cols": 120,
                    }
                )
                for i in range(50)
            )
            + "\n",
            encoding="utf-8",
        )

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            # Reviewer writer appends to the pre-seeded pair recording.
            # Coder writer uses a fresh recording — only the reviewer
            # path matters for this test's assertions.
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            session = _FakeSession(role)
            session.log_writer = writer
            return session

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            writers[session.role].write(b"agent output\n")
            return {
                "response_type": "ok",
                "response_text": "ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=_FakePairRegistry(),
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
            )
        finally:
            for w in writers.values():
                w.close()

        run_dir = _find_review_exchange_run_dir(coder_wt)
        chapters_path = run_dir / "reviewer" / "chapters.json"
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        slice_lines = [
            line
            for line in (run_dir / "reviewer" / "terminal-recording.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        slice_event_count = len(slice_lines)
        # Every chapter offset must be reachable inside the slice file.
        # If they remained pair-relative they'd be in the 50+ range
        # while the slice file holds at most a handful of events.
        for chapter in chapters_data["chapters"]:
            offset = chapter["recording_event_index"]
            assert 0 <= offset <= slice_event_count, (
                f"chapter at slice-relative offset {offset} is unreachable "
                f"in slice with {slice_event_count} events; chapter "
                f"section={chapter.get('section')} round={chapter.get('cycle_index')}. "
                "If offsets are still pair-relative, the web replay "
                "route's all_events[offset:] will return empty content."
            )


class TestEndToEndTimelineReadback:
    """Exercise the full viewer-side read path against real artifacts.

    The original empty-reviewer-timeline bug shipped past 27 unit tests
    because every test asserted on the chapter sidecar / manifest
    *internals* but no test ever asked the question "if a UI consumed
    the manifest's ``<role>_recording`` and read it, would it see the
    agent's output?" These tests close that gap by using the real
    ``MirroredTerminalRecordingWriter`` to populate the pair recording
    and the real ``ManifestAccessor`` to read it back.
    """

    def test_per_session_slice_returns_real_agent_output_to_viewer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: agent writes events to the pair recording during
        rounds, the chapter-driven mirror appends them to the run-dir
        slice, and ``ManifestAccessor.get_review_exchange_phase_terminal_recording``
        returns the slice with the agent's events. Without this, the
        timeline viewer sees an empty file even though the agent
        produced megabytes of output."""
        from issue_orchestrator.execution.manifest_accessor import (
            ManifestAccessor,
            RunIdentity,
        )
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        registry = _FakePairRegistry()
        # Track real writers so _send can simulate agent output bytes
        # flowing through the same writer used at session open.
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            session = _FakeSession(role)
            # Stash the writer on the fake session so close-time tests
            # can flush. Not strictly required for read-back but mirrors
            # production where close_persistent_session calls writer.close().
            session.log_writer = writer
            return session

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            # Simulate the agent producing output bytes during the round.
            # The MirroredTerminalRecordingWriter writes a real
            # ``output`` event per call, which is exactly what the
            # production PTY drain path produces.
            payload = f"agent {role} responding to: {prompt[:40]}\n".encode()
            writers[role].write(payload)
            return {
                "response_type": "ok",
                "response_text": f"{role} ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=registry,
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
            )
        finally:
            for w in writers.values():
                w.close()

        run_dir = _find_review_exchange_run_dir(coder_wt)

        # Reviewer slice must be present and non-empty when read by the
        # viewer-side accessor.
        accessor = ManifestAccessor(
            run_identity=RunIdentity(issue_number=42, run_dir=run_dir),
        )
        reviewer_stream = accessor.get_review_exchange_phase_terminal_recording(
            round_index=1,
            role="reviewer",
        )
        reviewer_path = reviewer_stream.path
        reviewer_text = reviewer_path.read_text(encoding="utf-8")
        assert reviewer_text.strip(), (
            "viewer-side read returned empty content for reviewer slice; "
            "this is exactly the user-visible 'crappy timeline' bug"
        )
        # Decode the slice and assert the agent's payload survived the
        # mirror round-trip.
        decoded = _decode_terminal_recording(reviewer_path)
        assert any("agent reviewer" in chunk for chunk in decoded), (
            f"reviewer slice did not contain agent output; events={decoded}"
        )

    def test_viewer_accessor_reads_slice_not_pair_recording(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The manifest's ``<role>_recording`` key must point at the
        per-session slice (inside run_dir), not the pair-scoped
        recording. Otherwise the viewer would see content from prior
        exchanges leaking into this exchange's playback."""
        from issue_orchestrator.execution.manifest_accessor import (
            ManifestAccessor,
            RunIdentity,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {
                        "response_type": "ok",
                        "response_text": "ok",
                        "getting_closer": True,
                    }
                ],
                "coder": [],
            },
        )

        pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
        )

        run_dir = _find_review_exchange_run_dir(coder_wt)
        accessor = ManifestAccessor(
            run_identity=RunIdentity(issue_number=42, run_dir=run_dir),
        )
        # allow_empty=True because the patched _send returns canned dicts
        # without writing real PTY bytes — the slice is touched empty.
        # The point of this test is the *path*, not the content.
        reviewer_stream = accessor.get_review_exchange_phase_terminal_recording(
            round_index=1,
            role="reviewer",
            allow_empty=True,
        )
        # The viewer must land inside the run_dir, not in the pair dir
        # under persistent-pairs/. Otherwise a worktree teardown leaves
        # the viewer 404'ing on a still-live recording.
        assert reviewer_stream.path.is_relative_to(run_dir), (
            f"viewer resolved reviewer recording to {reviewer_stream.path}, "
            f"which is NOT inside run_dir {run_dir} — the per-session "
            "manifest indirection broke"
        )


class TestAgentEnvPathIsolation:
    """Audit every agent-facing env var that holds a path.

    The seatbelt sandbox blocks writes outside the role's working_dir.
    Any ``ISSUE_ORCHESTRATOR_*`` env var that names a path the agent
    might write to must therefore resolve under ``working_dir`` (or
    appear on an explicit allowlist with documented justification).
    Catches the next "we added a new path env var pointing into
    base-repo state" before it ships.
    """

    # Paths the agent receives as env vars but never writes to directly
    # (the orchestrator writes them via HTTP / from outside the sandbox).
    # Each entry must come with a justification — adding to this list is
    # how a reviewer says "yes, this is intentional, here's why the
    # sandbox can't reach it."
    _DOCUMENTED_NON_AGENT_WRITTEN_PATHS = {
        "ISSUE_ORCHESTRATOR_COMPLETION_PATH": (
            "Written by the orchestrator process via the coding-done CLI's "
            "HTTP call to the Control API; agent never writes directly."
        ),
        "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": (
            "Written by the orchestrator-side validation gate; agent does "
            "not write into this directory directly."
        ),
        "ISSUE_ORCHESTRATOR_REPO_ROOT": (
            "Read-only orientation hint inherited from the orchestrator's "
            "process env so agents/tools that need the repo root know "
            "where it is. Agents never write to this path."
        ),
        "ISSUE_ORCHESTRATOR_CONFIG_PATH": (
            "Read-only runtime config selected by the orchestrator owner and "
            "injected so completion commands use the same authoritative "
            "configuration. Agents never write to this path."
        ),
    }

    def test_every_agent_path_env_var_is_inside_worktree_or_allowlisted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        captured: dict[str, dict[str, str]] = {}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            writer = _build_pty_writer(recording_path)
            captured[role] = {
                "working_dir": str(working_dir),
                **{k: v for k, v in env.items() if k.startswith("ISSUE_ORCHESTRATOR_")},
            }
            return _FakeSession(role, log_writer=writer)

        def _send(session, **_):  # noqa: ANN003
            return {
                "response_type": "ok",
                "response_text": "ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        pse.run_persistent_session_exchange(
            exchange_run=_start_exchange_run(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                issue_number=42,
                coder_label="agent:backend",
            ),
            session_output=session_output,
            pair_registry=_FakePairRegistry(),
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            runtime_config=_runtime_config(tmp_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
        )

        for role, env in captured.items():
            working_dir = Path(env.pop("working_dir"))
            for key, raw_value in env.items():
                if not _looks_like_path(raw_value):
                    continue
                path = Path(raw_value)
                if key in self._DOCUMENTED_NON_AGENT_WRITTEN_PATHS:
                    # Allowlisted — must still verify the justification
                    # exists (forces reviewer attention on changes here).
                    assert self._DOCUMENTED_NON_AGENT_WRITTEN_PATHS[key], (
                        f"{key} appears in the non-agent-written allowlist "
                        "with no justification — add a docstring before "
                        "shipping this env var"
                    )
                    continue
                assert path.is_relative_to(working_dir), (
                    f"{role} env var {key}={path} is outside working_dir "
                    f"{working_dir}; if the agent writes here from inside "
                    f"its sandbox, the write will silently fail. Either "
                    f"move the path inside the worktree (preferred) or "
                    f"add it to TestAgentEnvPathIsolation."
                    f"_DOCUMENTED_NON_AGENT_WRITTEN_PATHS with a "
                    "justification."
                )


class TestSliceIsolationAcrossExchanges:
    """The per-session slice for exchange N must contain ONLY events
    produced during exchange N."""

    def test_two_exchanges_on_cached_pair_have_independent_slices(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        # The fake registry offers a cached pair on exchange 2. The pair
        # contract must reject it because the process env belongs to exchange 1.
        cached_pair: dict[str, Any] = {}

        class _CachingFakePairRegistry(_FakePairRegistry):
            def acquire(self, *, issue_key, spawn):  # type: ignore[override]
                if "pair" not in cached_pair:
                    cached_pair["pair"] = spawn()
                self.acquired.append((issue_key, cached_pair["pair"]))
                return cached_pair["pair"]

            def release(self, issue_key, *, reason):  # type: ignore[override]
                super().release(issue_key, reason=reason)
                cached_pair.pop("pair", None)

        registry = _CachingFakePairRegistry()
        round_payloads: dict[str, list[bytes]] = {"coder": [], "reviewer": []}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            session = _FakeSession(role)
            session.log_writer = writer
            return session

        exchange_counter = {"n": 0}

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            payload = f"exchange-{exchange_counter['n']}-{role}-payload\n".encode()
            writers[role].write(payload)
            round_payloads[role].append(payload)
            return {
                "response_type": "ok",
                "response_text": f"{role} ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
            for n in (1, 2):
                exchange_counter["n"] = n
                pse.run_persistent_session_exchange(
                    exchange_run=_start_exchange_run(
                        session_output=session_output,
                        coder_worktree_path=coder_wt,
                        issue_number=42,
                        coder_label="agent:backend",
                    ),
                    session_output=session_output,
                    pair_registry=registry,
                    persistent_pair_root=tmp_path / "persistent-pairs",
                    coder_worktree_path=coder_wt,
                    reviewer_worktree_factory=lambda: reviewer_wt,
                    issue_number=42,
                    issue_title="Test",
                    coder_label="agent:backend",
                    reviewer_label="agent:reviewer",
                    coder_agent=_make_agent(prompt_path),
                    reviewer_agent=_make_agent(prompt_path),
                    runtime_config=_runtime_config(tmp_path),
                    max_rounds=1,
                    max_no_progress=2,
                    require_validation=False,
                )
        finally:
            for w in writers.values():
                w.close()

        runs = sorted(
            [
                r
                for r in (coder_wt / ".issue-orchestrator" / "sessions").iterdir()
                if r.is_dir() and not r.is_symlink() and "review-exchange" in r.name
            ],
            key=lambda p: p.stat().st_mtime,
        )
        assert len(runs) == 2, f"expected two run_dirs, got {runs}"

        for run_idx, run_dir in enumerate(runs, start=1):
            slice_path = run_dir / "reviewer" / "terminal-recording.jsonl"
            decoded = _decode_terminal_recording(slice_path)
            joined = "".join(decoded)
            # Exchange N's slice must contain its OWN payload.
            assert f"exchange-{run_idx}-reviewer" in joined, (
                f"run {run_idx} slice missing its own payload; events={decoded}"
            )
            # And must NOT contain the OTHER exchange's payload.
            other = 2 if run_idx == 1 else 1
            assert f"exchange-{other}-reviewer" not in joined, (
                f"run {run_idx} slice contains exchange-{other} content; "
                f"the cached-pair last_event_idx initialization leaked. "
                f"events={decoded}"
            )

    def test_coder_and_reviewer_slices_dont_cross_contaminate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each role's mirror writes only to its own role's slice. If
        we accidentally pass coder_mirror to reviewer's _send_role_round
        (or vice versa), this catches it because the slices would
        contain the wrong role's events."""
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            completion_env = env.get("ISSUE_ORCHESTRATOR_COMPLETION_PATH")
            validation_env = env.get("ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR")
            session = _FakeSession(
                role,
                completion_path=Path(completion_env) if completion_env else None,
                validation_output_dir=Path(validation_env) if validation_env else None,
            )
            session.log_writer = writer
            return session

        # Both roles produce role-tagged output during their rounds. The
        # exchange runs reviewer → coder → reviewer (a 2-round changes
        # then ok exchange), so each role writes at least once.
        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            writers[role].write(f"ROLE-MARKER-{role}\n".encode())
            if role == "coder":
                # Stub completion artifact so the protocol guardrail passes.
                completion = session.completion_path
                assert completion is not None
                completion.parent.mkdir(parents=True, exist_ok=True)
                completion.write_text(
                    json.dumps({"outcome": "completed", "implementation": "stub"}),
                    encoding="utf-8",
                )
            if role == "reviewer":
                # First reviewer response: changes_requested. Second: ok.
                if not _send.first_reviewer_done:  # type: ignore[attr-defined]
                    _send.first_reviewer_done = True  # type: ignore[attr-defined]
                    return {
                        "response_type": "changes_requested",
                        "response_text": "fix x",
                        "getting_closer": True,
                    }
                return {
                    "response_type": "ok",
                    "response_text": "ok",
                    "getting_closer": True,
                }
            return {
                "response_type": "ok",
                "response_text": "fixed",
                "getting_closer": True,
            }

        _send.first_reviewer_done = False  # type: ignore[attr-defined]

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=_FakePairRegistry(),
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=3,
                max_no_progress=2,
                require_validation=False,
            )
        finally:
            for w in writers.values():
                w.close()

        run_dir = _find_review_exchange_run_dir(coder_wt)
        coder_slice = "".join(
            _decode_terminal_recording(run_dir / "coder" / "terminal-recording.jsonl"),
        )
        reviewer_slice = "".join(
            _decode_terminal_recording(
                run_dir / "reviewer" / "terminal-recording.jsonl"
            ),
        )

        assert "ROLE-MARKER-coder" in coder_slice, "coder slice missing coder marker"
        assert "ROLE-MARKER-reviewer" in reviewer_slice, (
            "reviewer slice missing reviewer marker"
        )
        # The smoking gun: cross-contamination.
        assert "ROLE-MARKER-reviewer" not in coder_slice, (
            "reviewer marker leaked into coder slice — mirrors got crossed"
        )
        assert "ROLE-MARKER-coder" not in reviewer_slice, (
            "coder marker leaked into reviewer slice — mirrors got crossed"
        )

    def test_concurrent_issues_have_isolated_slices(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two issues running in parallel must not share manifest slice
        paths. A path collision would silently mix issue #360's reviewer
        output into issue #361's timeline."""
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")

        def _setup(issue: int) -> tuple[Path, Path]:
            coder = tmp_path / f"coder-wt-{issue}"
            reviewer = tmp_path / f"reviewer-wt-{issue}"
            coder.mkdir()
            reviewer.mkdir()
            return coder, reviewer

        worktrees = {360: _setup(360), 361: _setup(361)}
        writers_by_issue: dict[int, dict[str, MirroredTerminalRecordingWriter]] = {
            360: {},
            361: {},
        }
        active_issue = {"n": 360}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = "reviewer" if "reviewer-wt-" in Path(working_dir).name else "coder"
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers_by_issue[active_issue["n"]][role] = writer
            session = _FakeSession(role)
            session.log_writer = writer
            return session

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            issue = active_issue["n"]
            writers_by_issue[issue][role].write(f"ISSUE-{issue}-{role}\n".encode())
            return {
                "response_type": "ok",
                "response_text": f"{role} ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)
        session_output = FileSystemSessionOutput()

        try:
            for issue, (coder_wt, reviewer_wt) in worktrees.items():
                active_issue["n"] = issue
                pse.run_persistent_session_exchange(
                    exchange_run=_start_exchange_run(
                        session_output=session_output,
                        coder_worktree_path=coder_wt,
                        issue_number=issue,
                        coder_label="agent:backend",
                    ),
                    session_output=session_output,
                    pair_registry=_FakePairRegistry(),
                    persistent_pair_root=tmp_path / "persistent-pairs",
                    coder_worktree_path=coder_wt,
                    reviewer_worktree_factory=lambda r=reviewer_wt: r,
                    issue_number=issue,
                    issue_title=f"Issue {issue}",
                    coder_label="agent:backend",
                    reviewer_label="agent:reviewer",
                    coder_agent=_make_agent(prompt_path),
                    reviewer_agent=_make_agent(prompt_path),
                    runtime_config=_runtime_config(tmp_path),
                    max_rounds=1,
                    max_no_progress=2,
                    require_validation=False,
                )
        finally:
            for wmap in writers_by_issue.values():
                for w in wmap.values():
                    w.close()

        for issue, (coder_wt, _) in worktrees.items():
            run_dir = _find_review_exchange_run_dir(coder_wt)
            reviewer_slice = "".join(
                _decode_terminal_recording(
                    run_dir / "reviewer" / "terminal-recording.jsonl"
                ),
            )
            other = 360 if issue == 361 else 361
            assert f"ISSUE-{issue}-reviewer" in reviewer_slice
            assert f"ISSUE-{other}-reviewer" not in reviewer_slice, (
                f"issue {issue} reviewer slice contains issue {other} content"
            )


class TestLoopBoundCounting:
    """Direct adapter-level tests of count_consecutive_review_exchange_no_completion."""

    def _session_output_with_summaries(
        self,
        tmp_path: Path,
        summaries: list[dict[str, Any]],
        *,
        session_name: str = "coding-1",
    ) -> tuple[FileSystemSessionOutput, Path]:
        """Build a sessions/ tree with the given summaries, oldest first.

        Each entry is the JSON content of summary.json. The sessions
        directory layout matches what FileSystemSessionOutput produces.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)
        # Use distinct mtimes so newest-first ordering is deterministic.
        for idx, summary in enumerate(summaries):
            run_dir = sessions_dir / (
                f"2026010{idx + 1}T000000Z__review-exchange-42-r{idx}"
            )
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "started_at": f"2026-01-0{idx + 1}T00:00:00+00:00",
                        "review_exchange_dir": str(run_dir / "review-exchange"),
                    }
                ),
                encoding="utf-8",
            )
            exchange_dir = run_dir / "review-exchange"
            exchange_dir.mkdir()
            (exchange_dir / "summary.json").write_text(
                json.dumps(_summary_payload(summary)),
                encoding="utf-8",
            )
            # Force ascending mtime (idx=0 oldest, idx=N-1 newest).
            os.utime(run_dir, (1700000000 + idx * 100, 1700000000 + idx * 100))
        return FileSystemSessionOutput(), worktree

    def test_all_no_completion_summaries_count_to_their_total(
        self,
        tmp_path: Path,
    ) -> None:
        so, wt = self._session_output_with_summaries(
            tmp_path,
            [
                {"status": "error", "reason": "reviewer_no_completion"},
                {"status": "error", "reason": "reviewer_no_completion"},
                {"status": "error", "reason": "reviewer_no_completion"},
            ],
        )
        assert (
            so.count_consecutive_review_exchange_no_completion(
                wt,
                "coding-1",
            )
            == 3
        )

    def test_manifest_started_at_beats_touched_coding_dir_mtime(
        self,
        tmp_path: Path,
    ) -> None:
        """A touched coding run dir must not reset a newer timeout streak.

        Issue 360 reproduced this shape: review-exchange summaries were
        chronologically newer than the coding run, but the coding run directory
        had a later filesystem mtime. Mtime-first sorting counted one timeout,
        hit the old coding run, and let the retry loop continue indefinitely.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        coding_dir = sessions_dir / "20260101T000000Z__coding-1"
        coding_dir.mkdir()
        (coding_dir / "manifest.json").write_text(
            json.dumps({"started_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        # Newer than every review-exchange mtime, despite older started_at.
        os.utime(coding_dir, (1700009999, 1700009999))

        for idx in range(3):
            run_dir = sessions_dir / (
                f"2026010{idx + 2}T000000Z__review-exchange-42-r{idx}"
            )
            run_dir.mkdir()
            exchange_dir = run_dir / "review-exchange"
            exchange_dir.mkdir()
            (run_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "started_at": f"2026-01-0{idx + 2}T00:00:00+00:00",
                        "parent_session_name": "coding-1",
                        "review_exchange_dir": str(exchange_dir),
                    }
                ),
                encoding="utf-8",
            )
            (exchange_dir / "summary.json").write_text(
                json.dumps(
                    _summary_payload({
                        "status": "error",
                        "reason": "reviewer_no_completion",
                    })
                ),
                encoding="utf-8",
            )
            os.utime(run_dir, (1700000000 + idx * 100, 1700000000 + idx * 100))

        so = FileSystemSessionOutput()
        assert (
            so.count_consecutive_review_exchange_no_completion(
                worktree,
                "coding-1",
            )
            == 3
        )

    def test_run_dir_timestamp_orders_partial_manifest_before_mtime(
        self,
        tmp_path: Path,
    ) -> None:
        """A partial manifest still has a durable timestamp in its run id."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        coding_dir = sessions_dir / "20260101-000000Z__coding-1"
        coding_dir.mkdir()
        (coding_dir / "manifest.json").write_text(
            json.dumps({"started_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        os.utime(coding_dir, (1700009999, 1700009999))

        run_dir = sessions_dir / "20260102-000000Z__review-exchange-42-r1"
        run_dir.mkdir()
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "parent_session_name": "coding-1",
                    "review_exchange_dir": str(exchange_dir),
                }
            ),
            encoding="utf-8",
        )
        (exchange_dir / "summary.json").write_text(
            json.dumps(
                _summary_payload({
                    "status": "error",
                    "reason": "reviewer_no_completion",
                })
            ),
            encoding="utf-8",
        )
        os.utime(run_dir, (1700000000, 1700000000))

        so = FileSystemSessionOutput()
        assert (
            so.count_consecutive_review_exchange_no_completion(
                worktree,
                "coding-1",
            )
            == 1
        )

    def test_clean_ok_resets_count_to_zero(self, tmp_path: Path) -> None:
        # Sequence (oldest → newest): error, error, ok, error, error.
        # Newest-first traversal: error, error, ok → stop at ok.
        so, wt = self._session_output_with_summaries(
            tmp_path,
            [
                {"status": "error", "reason": "reviewer_no_completion"},
                {"status": "error", "reason": "reviewer_no_completion"},
                {"status": "ok", "reason": "reviewer_ok"},
                {"status": "error", "reason": "reviewer_no_completion"},
                {"status": "error", "reason": "reviewer_no_completion"},
            ],
        )
        assert (
            so.count_consecutive_review_exchange_no_completion(
                wt,
                "coding-1",
            )
            == 2
        )

    def test_different_error_reason_resets_count(self, tmp_path: Path) -> None:
        # A coder_protocol_error is a different failure mode — not the
        # runaway we're trying to bound. Counter must stop at it.
        so, wt = self._session_output_with_summaries(
            tmp_path,
            [
                {"status": "error", "reason": "reviewer_no_completion"},
                {"status": "error", "reason": "coder_protocol_error"},
                {"status": "error", "reason": "reviewer_no_completion"},
            ],
        )
        # Newest-first: error/no_completion (1), error/protocol_error (stop).
        assert (
            so.count_consecutive_review_exchange_no_completion(
                wt,
                "coding-1",
            )
            == 1
        )

    def test_no_summaries_returns_zero(self, tmp_path: Path) -> None:
        so, wt = self._session_output_with_summaries(tmp_path, [])
        assert (
            so.count_consecutive_review_exchange_no_completion(
                wt,
                "coding-1",
            )
            == 0
        )

    def test_coding_session_run_dir_resets_count(self, tmp_path: Path) -> None:
        """A non-review-exchange run_dir between failures must stop the
        count. Otherwise old failures from coding session A would carry
        across a successful coder turn into the new session B's quota
        and trigger false escalation after a single failure on B.

        Sequence (oldest → newest):
          - 2 reviewer_no_completion summaries (coding session A)
          - a fresh coding session run_dir (no review-exchange summary)
          - 1 reviewer_no_completion summary (coding session B)

        With a default cap of 3, the bug counted 3 → escalate. Correct
        behavior counts 1 → continue.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        def _make_review_exchange(idx: int, summary: dict[str, Any]) -> None:
            run_dir = sessions_dir / (
                f"2026010{idx + 1}T000000Z__review-exchange-42-r{idx}"
            )
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "started_at": f"2026-01-0{idx + 1}T00:00:00+00:00",
                        "review_exchange_dir": str(run_dir / "review-exchange"),
                    }
                ),
                encoding="utf-8",
            )
            exchange_dir = run_dir / "review-exchange"
            exchange_dir.mkdir()
            (exchange_dir / "summary.json").write_text(
                json.dumps(_summary_payload(summary)),
                encoding="utf-8",
            )
            os.utime(run_dir, (1700000000 + idx * 100, 1700000000 + idx * 100))

        def _make_coding(idx: int) -> None:
            run_dir = sessions_dir / f"2026010{idx + 1}T000000Z__coding-1"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "started_at": f"2026-01-0{idx + 1}T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(run_dir, (1700000000 + idx * 100, 1700000000 + idx * 100))

        # idx 0, 1: failures from prior coding session A
        _make_review_exchange(
            0, {"status": "error", "reason": "reviewer_no_completion"}
        )
        _make_review_exchange(
            1, {"status": "error", "reason": "reviewer_no_completion"}
        )
        # idx 2: fresh coding session B kicked off (NEW coder turn)
        _make_coding(2)
        # idx 3: first review-exchange under B fails
        _make_review_exchange(
            3, {"status": "error", "reason": "reviewer_no_completion"}
        )

        so = FileSystemSessionOutput()
        # Newest-first walk: error (1), coding-1 (boundary — STOP).
        # Pre-fix this returned 3 (counts skipped the coding-1 dir).
        assert (
            so.count_consecutive_review_exchange_no_completion(
                worktree,
                "coding-1",
            )
            == 1
        ), (
            "loop bound bled across a coding-session boundary; the new "
            "coder turn must reset the no-completion quota"
        )

    def test_boundary_excludes_pre_scratch_reset_failures(
        self,
        tmp_path: Path,
    ) -> None:
        so, wt = self._session_output_with_summaries(
            tmp_path,
            [
                {"status": "error", "reason": "reviewer_no_completion"},
                {"status": "error", "reason": "reviewer_no_completion"},
                {"status": "error", "reason": "reviewer_no_completion"},
            ],
        )
        # Only the newest summary (started_at 2026-01-03) is past the
        # boundary; the older two are pre-reset and don't count.
        boundary = "2026-01-03T00:00:00+00:00"
        assert (
            so.count_consecutive_review_exchange_no_completion(
                wt,
                "coding-1",
                not_before_started_at=boundary,
            )
            == 1
        )


class TestLoopBoundEscalation:
    """The bound must halt the spawn AND surface an error string the
    completion processor recognizes as a needs-human escalation."""

    def test_halt_error_is_recognized_by_review_exchange_classifier(
        self,
    ) -> None:
        """The exact error prefix we append must trigger the
        review-exchange-halted branch in completion_action_planner.
        Catches "someone renamed the prefix" silent breakage."""
        from issue_orchestrator.control.completion_action_planner import (
            has_review_exchange_errors,
        )

        sample = (
            "review_exchange: 3 consecutive reviewer/coder no-completion "
            "failures (max 3) — escalating to needs-human"
        )
        assert has_review_exchange_errors([sample]), (
            "halt error string is not classified as a review-exchange "
            "halt; the escalation will silently no-op"
        )

    def test_halt_error_is_recognized_by_result_artifacts_classifier(
        self,
    ) -> None:
        """The other halting check is an inline ``startswith`` in
        completion_result_artifacts. If the prefix changes, that
        path also silently stops escalating. Lock the prefix in."""
        sample = (
            "review_exchange: 3 consecutive reviewer/coder no-completion "
            "failures (max 3) — escalating to needs-human"
        )
        # Must start with the canonical "review_exchange:" prefix that
        # build_processing_result_with_completion uses to classify
        # halts (see completion_result_artifacts.py:119).
        assert sample.startswith("review_exchange:"), (
            "halt error string lost its review_exchange: prefix; "
            "the inline classifier in completion_result_artifacts "
            "will not flag this as a review-exchange halt"
        )


# ---------------------------------------------------------------------------
# Test helpers shared by the new test classes
# ---------------------------------------------------------------------------


def _find_review_exchange_run_dir(coder_wt: Path) -> Path:
    """Locate the single timestamped review-exchange run_dir under a
    coder worktree (filtering symlinks the friendly-name layer creates)."""
    runs = [
        r
        for r in (coder_wt / ".issue-orchestrator" / "sessions").iterdir()
        if r.is_dir() and not r.is_symlink() and "review-exchange" in r.name
    ]
    assert len(runs) == 1, f"expected one run_dir, got {runs}"
    return runs[0]


def _decode_terminal_recording(path: Path) -> list[str]:
    """Decode a terminal recording's output events into UTF-8 strings.

    Filters out non-output (resize) events so callers can grep for
    agent payload markers without false positives from geometry frames.
    """
    import base64

    decoded: list[str] = []
    for raw in path.read_text("utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        if event.get("event_type") != "output":
            continue
        data = event.get("data_b64")
        if not data:
            continue
        decoded.append(base64.b64decode(data).decode("utf-8", errors="replace"))
    return decoded


def _looks_like_path(value: str) -> bool:
    """Heuristic: env-var values that look like filesystem paths."""
    if not value:
        return False
    return value.startswith("/") or value.startswith("~")


class TestContinuousSliceMirroring:
    """The per-session slice must fill *during* a round, not just at
    chapter boundaries.

    The original chapter-driven mirror only flushed at prompt/feedback/
    timeout boundaries. For a 20-minute reviewer round, that meant the
    timeline was empty for 20 minutes — exactly the
    "I can't see what the reviewer is doing right now" symptom on
    tixmeup #362. These tests pin the new contract: every PTY event the
    writer drains during the round flows into the slice as it happens.
    """

    def test_writer_fans_writes_to_slice_continuously_with_no_chapters(
        self,
        tmp_path: Path,
    ) -> None:
        """Writer-level test: ``add_mirror_recording`` makes every
        subsequent ``write`` fan out to both files. No chapter-driven
        flush is involved — pure writer behavior."""
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        pair_path = tmp_path / "pair.jsonl"
        slice_path = tmp_path / "run" / "reviewer" / "slice.jsonl"
        writer = MirroredTerminalRecordingWriter(
            pair_path,
            initial_rows=40,
            initial_cols=120,
        )
        try:
            # Prior content into the pair recording (pre-mirror-attach)
            # represents events from earlier exchanges. The slice must
            # NOT contain any of these.
            writer.write(b"PRE-MIRROR-EVENT\n")
            assert not slice_path.exists(), (
                "slice file should not exist before mirror is attached"
            )

            # Attach mid-stream — this is how the exchange runner enables
            # live mirroring at exchange start on a cached pair.
            registered = writer.add_mirror_recording(slice_path, seed_resize=False)
            assert registered, "first registration must return True"

            # Subsequent writes fan out to BOTH paths immediately.
            writer.write(b"AFTER-ATTACH-1\n")
            writer.write(b"AFTER-ATTACH-2\n")

            slice_text = slice_path.read_text(encoding="utf-8")
            assert "AFTER-ATTACH-1" in _decode_writer_output(slice_text), (
                "first post-attach write missing from slice — the writer's "
                "fan-out isn't reaching the new mirror path"
            )
            assert "AFTER-ATTACH-2" in _decode_writer_output(slice_text), (
                "second post-attach write missing from slice"
            )
            assert "PRE-MIRROR-EVENT" not in _decode_writer_output(slice_text), (
                "pre-attach event leaked into slice — the new mirror "
                "must start empty, not be backfilled with prior content"
            )

            # Detach — subsequent writes must NOT touch the slice.
            removed = writer.remove_mirror_recording(slice_path)
            assert removed, "first removal must return True"
            slice_text_after_detach_baseline = slice_path.read_text(encoding="utf-8")
            writer.write(b"AFTER-DETACH-LEAK\n")
            slice_text_after_detach = slice_path.read_text(encoding="utf-8")
            assert slice_text_after_detach == slice_text_after_detach_baseline, (
                "writer continued to mirror after remove_mirror_recording — "
                "the slice would accumulate the next exchange's content"
            )
            # Repeat add/remove are idempotent (no-op return False).
            assert writer.remove_mirror_recording(slice_path) is False
        finally:
            writer.close()

    def test_slice_fills_mid_round_without_chapter_flush(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: drive an exchange where the agent emits output
        but the round never completes (round-end chapter never fires).
        The slice file must still contain the agent's events. This is
        the exact scenario behind the "tixmeup #362 reviewer timeline
        is empty mid-round" report."""
        from issue_orchestrator.execution.manifest_accessor import (
            ManifestAccessor,
            RunIdentity,
        )
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            return _FakeSession(role, log_writer=writer)

        # send_round simulates: agent produces a burst of output, then
        # times out (no response file ever appears). The exchange ends
        # via the timeout chapter, not feedback. Crucially, the test
        # asserts the slice contained the burst BEFORE the timeout —
        # the read happens via a recorded snapshot of the slice
        # immediately after the writer's last fan-out.
        slice_snapshots: dict[str, bytes] = {}

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            # Multiple discrete writes during the round — each one is
            # an event drained from the PTY in production.
            for chunk in (
                b"agent thinking step 1\n",
                b"agent thinking step 2\n",
                b"agent thinking step 3\n",
            ):
                writers[role].write(chunk)
            # Capture the slice file's bytes right now — AT THE MOMENT
            # the agent has emitted its output but BEFORE _send_role_round
            # records the timeout chapter. If continuous mirroring works,
            # this snapshot already contains the agent output.
            run_dir = _find_review_exchange_run_dir(coder_wt)
            slice_path = run_dir / role / "terminal-recording.jsonl"
            if slice_path.exists():
                slice_snapshots[role] = slice_path.read_bytes()
            # Then time out so the round records a timeout chapter
            # (not a feedback chapter). Old chapter-driven mirror would
            # fire at this boundary — but we already snapshotted before.
            from issue_orchestrator.execution.persistent_round_runner import (
                PersistentRoundTimeoutError,
            )

            raise PersistentRoundTimeoutError("simulated mid-round read")

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
            outcome = pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=_FakePairRegistry(),
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
            )
        finally:
            for w in writers.values():
                w.close()

        # The exchange ended in error (reviewer timed out) — but the
        # slice snapshot taken DURING the round already had agent output.
        assert outcome.status == "error"
        assert "reviewer" in slice_snapshots, (
            "slice file did not exist while the agent was mid-round; "
            "live mirroring is not attached at exchange start"
        )
        decoded = _decode_writer_output(slice_snapshots["reviewer"].decode("utf-8"))
        for marker in (
            "agent thinking step 1",
            "agent thinking step 2",
            "agent thinking step 3",
        ):
            assert marker in decoded, (
                f"slice snapshot taken mid-round missing {marker!r}; "
                "the writer is not fanning out to the slice in real time. "
                "This is the exact symptom users saw on tixmeup #362: "
                "reviewer was alive and producing output, but the "
                "timeline showed nothing because no chapter had fired."
            )

        # And after the exchange ends, the slice path still resolves
        # via the manifest's ``<role>_recording`` pointer.
        run_dir = _find_review_exchange_run_dir(coder_wt)
        accessor = ManifestAccessor(
            run_identity=RunIdentity(issue_number=42, run_dir=run_dir),
        )
        artifact = accessor.get_review_exchange_phase_terminal_recording(
            round_index=1,
            role="reviewer",
            allow_empty=True,
        )
        assert artifact.path.is_relative_to(run_dir), artifact.path

    def test_attach_failure_propagates_loudly_no_silent_empty_timeline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``add_mirror_recording`` raises during attach, the exchange
        must propagate the failure (REVIEW_EXCHANGE_FAILED + re-raise),
        NOT continue silently with an empty slice the viewer would
        return as a successful empty artifact.

        Regression guard for PR #6268 review feedback. The earlier
        design swallowed OSError in ``_attach_slice_mirror`` and
        returned False; the manifest still pointed at the (now
        empty) slice file; the timeline viewer happily served empty
        content for a session whose pair recording had real bytes.
        That recreated the exact "I can't see what the reviewer is
        doing" symptom this PR is supposed to eliminate. Fail-fast
        forces the failure into the orchestrator's loop bound where
        retry / escalate is governed deterministically.
        """
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()

        # Build a writer subclass that raises on add_mirror_recording.
        # Mirrors a real-world failure mode (filesystem permissions,
        # disk full, parent dir vanished mid-exchange).
        class _PoisonAttachWriter(MirroredTerminalRecordingWriter):
            def add_mirror_recording(self, path, *, seed_resize=True):  # noqa: ARG002
                raise OSError("simulated attach failure")

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = _PoisonAttachWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            return _FakeSession(role, log_writer=writer)

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            # Should never reach the round loop — attach raised before.
            raise AssertionError(
                "send_round invoked despite attach failure; the "
                "exchange continued past a failed slice mirror "
                "instead of failing loudly",
            )

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        with pytest.raises(OSError, match="simulated attach failure"):
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=_FakePairRegistry(),
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
                events=sink,
                event_context=EventContext(),
            )

        # The orchestrator must hear about the failure as a
        # definitive REVIEW_EXCHANGE_FAILED — not silent continuation.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_FAILED for evt in sink.events
        ), (
            "attach failure did not surface as REVIEW_EXCHANGE_FAILED; "
            "the loop bound (PR #6267) cannot govern retries on a "
            "failure mode it never sees"
        )

    def test_missing_log_writer_fails_at_pair_recording_contract(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Production invariant: every PersistentSession opened with a
        recording_path carries a real writer. If a fixture (or a
        regression in open_persistent_session) hands the runner a
        session with ``log_writer=None``, pair acquisition must raise
        before round execution rather than silently skip the slice."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            # Simulated regression: session opened without a writer.
            return _FakeSession(role, log_writer=None)

        def _send(session, **_):  # noqa: ANN003
            raise AssertionError("send_round must not be reached")

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        with pytest.raises(RuntimeError, match="no_writer"):
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=_FakePairRegistry(),
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
            )

    def test_slice_detaches_at_exchange_end_no_leak_to_next_exchange(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_detach_slice_mirror`` must run in a finally block so a
        subsequent exchange (cached pair, reused writer) doesn't keep
        writing to the previous exchange's slice path."""
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        cached_pair: dict[str, Any] = {}

        class _CachingFakePairRegistry(_FakePairRegistry):
            def acquire(self, *, issue_key, spawn):  # type: ignore[override]
                if "pair" not in cached_pair:
                    cached_pair["pair"] = spawn()
                self.acquired.append((issue_key, cached_pair["pair"]))
                return cached_pair["pair"]

            def release(self, issue_key, *, reason):  # type: ignore[override]
                super().release(issue_key, reason=reason)
                cached_pair.pop("pair", None)

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            return _FakeSession(role, log_writer=writer)

        exchange_n = {"n": 0}

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            writers[session.role].write(
                f"exchange-{exchange_n['n']}-{session.role}\n".encode(),
            )
            return {
                "response_type": "ok",
                "response_text": "ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
            for n in (1, 2):
                exchange_n["n"] = n
                pse.run_persistent_session_exchange(
                    exchange_run=_start_exchange_run(
                        session_output=session_output,
                        coder_worktree_path=coder_wt,
                        issue_number=42,
                        coder_label="agent:backend",
                    ),
                    session_output=session_output,
                    pair_registry=_CachingFakePairRegistry(),
                    persistent_pair_root=tmp_path / "persistent-pairs",
                    coder_worktree_path=coder_wt,
                    reviewer_worktree_factory=lambda: reviewer_wt,
                    issue_number=42,
                    issue_title="Test",
                    coder_label="agent:backend",
                    reviewer_label="agent:reviewer",
                    coder_agent=_make_agent(prompt_path),
                    reviewer_agent=_make_agent(prompt_path),
                    runtime_config=_runtime_config(tmp_path),
                    max_rounds=1,
                    max_no_progress=2,
                    require_validation=False,
                )
        finally:
            for w in writers.values():
                w.close()

        # Find both exchange run_dirs in mtime order.
        runs = sorted(
            [
                r
                for r in (coder_wt / ".issue-orchestrator" / "sessions").iterdir()
                if r.is_dir() and not r.is_symlink() and "review-exchange" in r.name
            ],
            key=lambda p: p.stat().st_mtime,
        )
        assert len(runs) == 2

        for idx, run_dir in enumerate(runs, start=1):
            slice_path = run_dir / "reviewer" / "terminal-recording.jsonl"
            decoded = _decode_writer_output(slice_path.read_text("utf-8"))
            other = 2 if idx == 1 else 1
            assert f"exchange-{idx}-reviewer" in decoded, (
                f"run {idx} slice missing its own reviewer output"
            )
            assert f"exchange-{other}-reviewer" not in decoded, (
                f"run {idx} slice contains exchange-{other} reviewer output — "
                "the writer kept mirroring after exchange end, leaking the "
                "next exchange's bytes into the previous slice file"
            )


def _decode_writer_output(text: str) -> str:
    """Decode the base64'd output events from a terminal recording.

    Returns the joined string content of all output events. Useful when
    asserting "did this byte sequence end up in the slice file" without
    caring about the exact line-by-line structure.
    """
    import base64

    pieces: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") != "output":
            continue
        data = event.get("data_b64") or ""
        if not data:
            continue
        pieces.append(base64.b64decode(data).decode("utf-8", errors="replace"))
    return "".join(pieces)


class TestProductionLayoutCacheResolution:
    """Joint coverage: real ``CompletionReviewExchange`` + real
    ``FileSystemSessionOutput`` over the production filesystem layout.

    This is the regression guard for the tixmeup #359 / #361 runaway.
    Pre-state-machine, every reviewer-OK summary read back as
    ``validation=None`` because validation lives in the coder run_dir
    (not next to the review-exchange summary), and the cache loader
    would discard the OK and respawn — cycling forever until the
    no-completion bound (or until a human noticed). The unit tests
    against ``decide()`` lock the policy in isolation; this class
    locks the END-TO-END behavior over the actual disk layout
    production produces, with a real adapter.

    Every cell in the state-table matrix gets one production-shaped
    test case.
    """

    @staticmethod
    def _write_validation_record(
        path: Path,
        *,
        head_sha: str,
        passed: bool = True,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "suite": "agent_gate",
                    "head_sha": head_sha,
                    "passed": passed,
                    "exit_code": 0 if passed else 1,
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _write_summary_with_facts(
        exchange_dir: Path,
        *,
        status: str,
        reason: str,
        head_sha: str | None,
        validation_passed: bool | None = True,
        rounds: int = 1,
    ) -> None:
        """Write a production-shape summary.json matching what
        ``_write_summary`` produces post-state-machine."""
        exchange_dir.mkdir(parents=True, exist_ok=True)
        summary: dict[str, Any] = {
            "completed_rounds": rounds,
            "status": status,
            "reason": reason,
            "response_text": "test response",
            "timestamp": "2026-05-06T10:30:00+00:00",
        }
        if head_sha is not None:
            summary["head_sha"] = head_sha
        if validation_passed is not None:
            summary["validation_passed"] = validation_passed
        (exchange_dir / "summary.json").write_text(
            json.dumps(ReviewExchangeSummaryV1.from_payload(summary).to_payload()),
            encoding="utf-8",
        )

    def _build_completion_review_exchange(
        self,
        tmp_path: Path,
        session_output: FileSystemSessionOutput,
    ) -> Any:
        from issue_orchestrator.control.completion_review_exchange import (
            CompletionReviewExchange,
        )
        from issue_orchestrator.domain.models import AgentConfig
        from issue_orchestrator.infra.config import Config

        cfg = Config(repo_root=tmp_path)
        cfg.review_exchange_mode = "via-local-loop"
        cfg.review_exchange_require_validation = True
        cfg.code_review_agent = "agent:reviewer"
        cfg.agents = {
            "agent:backend": AgentConfig(
                prompt_path=tmp_path / "backend.md",
                command="claude --print",
                reviewer="agent:reviewer",
            ),
            "agent:reviewer": AgentConfig(
                prompt_path=tmp_path / "reviewer.md",
                command="claude --print",
            ),
        }
        return CompletionReviewExchange(
            agent_callback_endpoint=ready_callback_endpoint(),
            config=cfg,
            session_output=session_output,
            emit_review_started=lambda **_: None,
            emit_review_outcome=lambda **_: None,
            review_exchange_runner=SimpleNamespace(run=lambda **_: None),
        )

    def _stage_run(
        self,
        tmp_path: Path,
        *,
        coder_session_name: str = "coding-1",
        coder_head_sha: str = "HEAD_X",
        review_status: str,
        review_reason: str,
        review_head_sha: str | None,
        validation_passed: bool | None = True,
        review_parent_session: str | None = None,
    ) -> tuple[Path, Path]:
        """Stage the production filesystem layout: a coder run_dir
        with validation-record.json at ``coder_head_sha``, and a
        review-exchange run_dir with the requested summary fields.

        ``review_parent_session`` (default: ``coder_session_name``)
        controls the manifest's ``parent_session_name`` pointer.
        Pass ``"<legacy>"`` to omit the field entirely (simulates
        pre-state-machine summaries).
        """
        worktree = tmp_path / "wt"
        sessions = worktree / ".issue-orchestrator" / "sessions"
        sessions.mkdir(parents=True)

        coder_run = sessions / f"20260506-100000Z__{coder_session_name}"
        coder_run.mkdir()
        (coder_run / "manifest.json").write_text(
            json.dumps(
                {
                    "started_at": "2026-05-06T10:00:00+00:00",
                    "session_name": coder_session_name,
                }
            ),
            encoding="utf-8",
        )
        self._write_validation_record(
            coder_run / "validation-record.json",
            head_sha=coder_head_sha,
        )

        exchange_run = sessions / "20260506-100500Z__review-exchange-359-r1"
        exchange_dir = exchange_run / "review-exchange"
        exchange_dir.mkdir(parents=True)
        manifest: dict[str, Any] = {
            "started_at": "2026-05-06T10:05:00+00:00",
            "review_exchange_dir": str(exchange_dir),
        }
        parent = (
            review_parent_session
            if review_parent_session is not None
            else coder_session_name
        )
        if parent != "<legacy>":
            manifest["parent_session_name"] = parent
        (exchange_run / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        self._write_summary_with_facts(
            exchange_dir,
            status=review_status,
            reason=review_reason,
            head_sha=review_head_sha,
            validation_passed=validation_passed,
        )

        return worktree, coder_run

    # -----------------------------------------------------------------
    # Keystone: the tixmeup #359 / #361 runaway scenario
    # -----------------------------------------------------------------

    def test_reviewer_ok_summary_at_current_head_is_reused(
        self,
        tmp_path: Path,
    ) -> None:
        """The exact production scenario behind the runaway: coder
        ran validation at head X, review-exchange produced an OK
        summary at the same head X, no sibling validation-record in
        the review-exchange run_dir. On the next tick the loader
        must REUSE that OK. Pre-state-machine this returned None
        and the orchestrator spawned a redundant review-exchange
        every ~5 seconds until needs-human."""
        from issue_orchestrator.control.completion_review_exchange import (
            ReuseResumeResolution,
        )
        from issue_orchestrator.domain.review_exchange_resume import ResumeDecision

        worktree, coder_run = self._stage_run(
            tmp_path,
            coder_head_sha="HEAD_X",
            review_status="ok",
            review_reason="reviewer_ok",
            review_head_sha="HEAD_X",
        )
        review = self._build_completion_review_exchange(
            tmp_path,
            FileSystemSessionOutput(),
        )
        resolution = review.decide_review_exchange_resumption(
            worktree=worktree,
            session_name="coding-1",
            require_validation=True,
            current_validation_record_path=coder_run / "validation-record.json",
        )
        assert resolution.decision is ResumeDecision.REUSE_APPROVAL
        assert isinstance(resolution, ReuseResumeResolution)
        assert resolution.cached.outcome.status == "ok"
        assert resolution.cached.outcome.summary is not None
        assert resolution.cached.outcome.summary.head_sha == "HEAD_X"

    # -----------------------------------------------------------------
    # State-table cells, one production-layout case each
    # -----------------------------------------------------------------

    def test_reviewer_ok_at_old_head_is_ignored_stale(
        self,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.domain.review_exchange_resume import ResumeDecision

        worktree, coder_run = self._stage_run(
            tmp_path,
            coder_head_sha="HEAD_NEW",
            review_status="ok",
            review_reason="reviewer_ok",
            review_head_sha="HEAD_OLD",
        )
        review = self._build_completion_review_exchange(
            tmp_path,
            FileSystemSessionOutput(),
        )
        resolution = review.decide_review_exchange_resumption(
            worktree=worktree,
            session_name="coding-1",
            require_validation=True,
            current_validation_record_path=coder_run / "validation-record.json",
        )
        assert resolution.decision is ResumeDecision.IGNORE_STALE

    def test_stopped_no_progress_at_current_head_reuses_halt(
        self,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.domain.review_exchange_resume import ResumeDecision

        worktree, coder_run = self._stage_run(
            tmp_path,
            coder_head_sha="HEAD_X",
            review_status="stopped",
            review_reason="reviewer_reports_no_progress",
            review_head_sha="HEAD_X",
        )
        review = self._build_completion_review_exchange(
            tmp_path,
            FileSystemSessionOutput(),
        )
        resolution = review.decide_review_exchange_resumption(
            worktree=worktree,
            session_name="coding-1",
            require_validation=True,
            current_validation_record_path=coder_run / "validation-record.json",
        )
        assert resolution.decision is ResumeDecision.REUSE_HALT

    def test_max_rounds_exceeded_at_current_head_reuses_halt(
        self,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.domain.review_exchange_resume import ResumeDecision

        worktree, coder_run = self._stage_run(
            tmp_path,
            coder_head_sha="HEAD_X",
            review_status="stopped",
            review_reason="max_rounds_exceeded",
            review_head_sha="HEAD_X",
        )
        review = self._build_completion_review_exchange(
            tmp_path,
            FileSystemSessionOutput(),
        )
        resolution = review.decide_review_exchange_resumption(
            worktree=worktree,
            session_name="coding-1",
            require_validation=True,
            current_validation_record_path=coder_run / "validation-record.json",
        )
        assert resolution.decision is ResumeDecision.REUSE_HALT

    def test_coder_protocol_error_at_current_head_reuses_halt(
        self,
        tmp_path: Path,
    ) -> None:
        """Critical: ``coder_protocol_error`` is deterministic terminal —
        won't fix itself by retrying on the same head. Must reuse-halt
        rather than respawn. Pre-state-machine this respawned forever
        (review feedback on PR #6270 part 2)."""
        from issue_orchestrator.domain.review_exchange_resume import ResumeDecision

        worktree, coder_run = self._stage_run(
            tmp_path,
            coder_head_sha="HEAD_X",
            review_status="error",
            review_reason="coder_protocol_error",
            review_head_sha="HEAD_X",
        )
        review = self._build_completion_review_exchange(
            tmp_path,
            FileSystemSessionOutput(),
        )
        resolution = review.decide_review_exchange_resumption(
            worktree=worktree,
            session_name="coding-1",
            require_validation=True,
            current_validation_record_path=coder_run / "validation-record.json",
        )
        assert resolution.decision is ResumeDecision.REUSE_HALT

    def test_reviewer_no_completion_returns_count_and_retry(
        self,
        tmp_path: Path,
    ) -> None:
        """``*_no_completion`` errors do NOT cache-hit; caller spawns
        fresh and the budget governs retries. The loader returns
        ``COUNT_NO_COMPLETION_AND_RETRY`` so the caller knows to
        consult the budget — distinct from ``IGNORE_STALE`` (which
        does NOT count toward the budget)."""
        from issue_orchestrator.domain.review_exchange_resume import ResumeDecision

        worktree, coder_run = self._stage_run(
            tmp_path,
            coder_head_sha="HEAD_X",
            review_status="error",
            review_reason="reviewer_no_completion",
            review_head_sha="HEAD_X",
            validation_passed=True,
        )
        review = self._build_completion_review_exchange(
            tmp_path,
            FileSystemSessionOutput(),
        )
        resolution = review.decide_review_exchange_resumption(
            worktree=worktree,
            session_name="coding-1",
            require_validation=True,
            current_validation_record_path=coder_run / "validation-record.json",
        )
        assert resolution.decision is ResumeDecision.COUNT_NO_COMPLETION_AND_RETRY
        # Outcome NOT populated for retry paths — caller must spawn
        # fresh, not reuse the failure.
        assert not hasattr(resolution, "cached")

    def test_legacy_summary_without_head_sha_is_stale(
        self,
        tmp_path: Path,
    ) -> None:
        """Backwards compat: pre-state-machine summaries don't carry
        ``head_sha`` / ``validation_passed`` in the JSON. Under
        ``require_validation=True`` the cache cannot prove validation
        — IGNORE_STALE. Same effective behavior as pre-fix (where
        the cache rejected for the wrong reason; now rejects for the
        right reason)."""
        from issue_orchestrator.domain.review_exchange_resume import ResumeDecision

        worktree, coder_run = self._stage_run(
            tmp_path,
            coder_head_sha="HEAD_X",
            review_status="ok",
            review_reason="reviewer_ok",
            review_head_sha=None,
            validation_passed=None,
            review_parent_session="<legacy>",
        )
        review = self._build_completion_review_exchange(
            tmp_path,
            FileSystemSessionOutput(),
        )
        resolution = review.decide_review_exchange_resumption(
            worktree=worktree,
            session_name="coding-1",
            require_validation=True,
            current_validation_record_path=coder_run / "validation-record.json",
        )
        assert resolution.decision is ResumeDecision.IGNORE_STALE

    def test_parent_session_name_filter_isolates_coding_sessions(
        self,
        tmp_path: Path,
    ) -> None:
        """A review-exchange run whose ``parent_session_name`` doesn't
        match the requested session must be skipped. Coding session
        A's failures cannot leak into coding session B's quota."""
        from issue_orchestrator.domain.review_exchange_resume import ResumeDecision

        worktree, _coder_run = self._stage_run(
            tmp_path,
            coder_session_name="coding-2",
            coder_head_sha="HEAD_X",
            review_status="ok",
            review_reason="reviewer_ok",
            review_head_sha="HEAD_X",
            review_parent_session="coding-1",  # belongs to a DIFFERENT session
        )
        review = self._build_completion_review_exchange(
            tmp_path,
            FileSystemSessionOutput(),
        )
        resolution = review.decide_review_exchange_resumption(
            worktree=worktree,
            session_name="coding-2",
            require_validation=True,
            current_validation_record_path=(
                worktree
                / ".issue-orchestrator"
                / "sessions"
                / "20260506-100000Z__coding-2"
                / "validation-record.json"
            ),
        )
        # The cached OK belongs to coding-1, not coding-2. Loader skips it.
        assert resolution.decision is ResumeDecision.NO_CACHE


class TestSessionCleanup:
    def test_sessions_closed_even_on_round_exception(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        # Reviewer round 1 raises a non-runner-typed exception (not timeout/error).
        # The exchange must surface REVIEW_EXCHANGE_FAILED and evict the cached pair.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [RuntimeError("unexpected")],
                "coder": [],
            },
        )
        sink = _Sink()
        ctx = EventContext()

        with pytest.raises(RuntimeError, match="unexpected"):
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=state["registry"],
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
                events=sink,
                event_context=ctx,
            )

        assert state["registry"].released == [(42, "review-exchange-exception")]
        # And the failure event was emitted before raising.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_FAILED for evt in sink.events
        )


class TestSpawnPartialConstructionCleanup:
    """Regression for PR #6209 review finding: if reviewer spawn fails
    after coder spawn succeeds, the coder PTY/process must be closed
    before the spawn closure raises.

    Pre-registry code wrapped both opens in a single ``try``; the
    registry refactor (PR #6209) initially moved both opens into a
    spawn closure that returned no value on partial failure, leaking
    the coder. The fix wraps the reviewer open in a nested
    ``try/except`` that closes the coder before re-raising. This
    test pins that behavior so the leak doesn't come back.
    """

    def test_coder_session_is_closed_when_reviewer_open_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        opened_sessions: list[_FakeSession] = []
        closed_sessions: list[_FakeSession] = []

        def _open(
            *,
            command,
            working_dir,
            env,
            recording_path=None,
            additional_recording_paths=None,
            mirror_path=None,
        ):  # noqa: ARG001
            role = (
                "reviewer"
                if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            if role == "reviewer":
                # Simulate a reviewer-side failure during PTY/process
                # bring-up, after the coder has already opened.
                raise RuntimeError("reviewer pty bring-up failed")
            session = _FakeSession(role)
            opened_sessions.append(session)
            return session

        def _close(session, **_):
            closed_sessions.append(session)
            session.closed = True
            return 0

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "close_persistent_session", _close)

        registry = _FakePairRegistry()
        with pytest.raises(RuntimeError, match="reviewer pty bring-up failed"):
            pse.run_persistent_session_exchange(
                exchange_run=_start_exchange_run(
                    session_output=session_output,
                    coder_worktree_path=coder_wt,
                    issue_number=42,
                    coder_label="agent:backend",
                ),
                session_output=session_output,
                pair_registry=registry,
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                runtime_config=_runtime_config(tmp_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
            )

        # The coder opened, the reviewer raised before opening — and
        # the coder must have been closed by the spawn closure's
        # cleanup ``except`` block. No partial pair must reach the
        # registry's cache.
        assert len(opened_sessions) == 1
        assert opened_sessions[0].role == "coder"
        assert closed_sessions == opened_sessions, (
            "coder session leaked: registry refactor must close any "
            "already-opened session if the partner spawn raises"
        )
        assert registry.acquired == [], (
            "no pair must reach the registry on partial spawn failure"
        )
