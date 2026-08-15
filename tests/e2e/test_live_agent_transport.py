"""Live agent transport acceptance checks.

These tests are e2e-managed because they run real provider CLIs and can fail
for non-code reasons such as local auth, provider availability, or network
behavior. Keep deterministic transport-contract coverage in unit/integration
tests; this module proves the live TUI contract still holds.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from issue_orchestrator.execution.persistent_round_runner import (
    close_persistent_session,
    open_persistent_session,
    send_round,
)
from tests.fixtures.live_agent_cli import (
    is_claude_authenticated,
    is_claude_available,
)

# Only cheap checks may run at collection time: this module is imported by
# every `pytest tests/e2e` invocation (including test-e2e-one and
# --collect-only), so the live auth probe is deferred into the test body via
# is_claude_authenticated(). No GitHub gating: these tests drive local
# provider PTYs and never touch the GitHub API (test_gh_activity_limit=0).
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.live_agent,
    pytest.mark.heavy_e2e,
    pytest.mark.requires_infra,
    pytest.mark.xdist_group("live-agent"),
    pytest.mark.gh_activity_limit(
        test_gh_activity_limit=0,
        system_gh_activity_limit=20,
    ),
]


def _venv_path_prefix() -> str:
    venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin"
    return f"{venv_bin}:{os.environ.get('PATH', '')}"


def _wait_for_pty_idle(
    session: object,
    *,
    quiet_seconds: float,
    max_wait: float,
) -> None:
    """Drain the PTY until output stays quiet for ``quiet_seconds``.

    codex queues stdin typed while its TUI is "Working" instead of submitting
    it, so the next round must only be injected once the agent is back at its
    idle input prompt.
    """
    from issue_orchestrator.execution.persistent_round_runner import (
        _drain_pty_output,
    )

    deadline = time.monotonic() + max_wait
    last_activity = time.monotonic()
    while time.monotonic() < deadline:
        if _drain_pty_output(session) > 0:
            last_activity = time.monotonic()
        elif time.monotonic() - last_activity >= quiet_seconds:
            return
        time.sleep(0.2)


@pytest.mark.skipif(
    not is_claude_available(),
    reason="Claude CLI not installed",
)
def test_persistent_send_round_two_rounds_real_claude() -> None:
    """A persistent real Claude TUI accepts two CR-submitted rounds.

    This is the review-exchange path: a long-lived interactive Claude process
    driven across rounds by injecting prompts on its PTY. The tixmeup
    #277/#290 hang lived here: ``send_round`` used to terminate prompts with
    ``"\n"``, which a raw-mode TUI does not treat as Enter, so the prompt
    rendered into the input box but was never submitted. This proves the
    ``"\r"`` fix against real Claude across two rounds.
    """
    if not is_claude_authenticated():
        pytest.skip("Claude CLI not authenticated")

    # Run under the repo worktree (a trusted git repo) so the interactive
    # first-run "trust this folder?" dialog never blocks — mirrors
    # production, where review-exchange worktrees are trusted. /tmp would
    # trigger that dialog for an interactive session.
    repo_root = Path(__file__).resolve().parents[2]
    work_dir = Path(tempfile.mkdtemp(prefix=".live-send-round-", dir=repo_root))
    response_file = work_dir / "review-response.json"
    system_prompt = (
        "You are an automated review-exchange test agent. Every time you "
        "receive a message, immediately use the Bash tool to run exactly:\n"
        "  printf "
        '\'{"response_type":"ok"}\' > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"\n'
        "then keep waiting for the next message. Do not exit on your own."
    )
    # Scrub CLAUDECODE so the nested-session guard does not block claude
    # when this test runs inside a Claude Code session (same scrub as the
    # is_claude_authenticated probe).
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["PATH"] = _venv_path_prefix()
    env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"] = str(response_file)

    session = open_persistent_session(
        command=[
            "claude",
            "--model",
            "haiku",
            "--permission-mode",
            "bypassPermissions",
            "--append-system-prompt",
            system_prompt,
        ],
        working_dir=work_dir,
        env=env,
    )
    try:
        from issue_orchestrator.execution.persistent_round_runner import (
            _drain_pty_output,
        )

        # Let claude boot to its idle input prompt (trusted dir -> no dialog).
        boot_deadline = time.monotonic() + 25
        while time.monotonic() < boot_deadline:
            _drain_pty_output(session)
            if session.proc.poll() is not None:
                pytest.fail(f"claude exited during boot, code={session.proc.poll()}")
            time.sleep(0.3)

        for n in (1, 2):
            response_file.unlink(missing_ok=True)
            result = send_round(
                session,
                prompt=f"round {n}: respond now",
                response_file=response_file,
                timeout_seconds=60,
                poll_interval_seconds=0.3,
                role_label=f"coder@round-{n}",
            )
            assert result == {"response_type": "ok"}, (
                f"round {n} not answered - a CR-terminated prompt must submit "
                f"to real claude. Got: {result}"
            )
            assert session.proc.poll() is None, f"claude exited after round {n}"
    finally:
        close_persistent_session(session)
        shutil.rmtree(work_dir, ignore_errors=True)


@pytest.mark.live_codex
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
def test_persistent_send_round_multi_round_real_codex_interactive() -> None:
    """The reviewer is INTERACTIVE codex (the production default): one
    persistent TUI process across multiple rounds.

    Production launches codex with a bootstrap prompt telling it to wait
    for stdin, then each real review turn is injected with ``send_round``.
    The injected rounds MUST submit via the two-write prompt+Enter contract
    — codex treats a ``\r`` batched with the prompt text as a literal
    newline in its input box (renders, never submits), which is exactly the
    tixmeup #277/#290 hang class. The command is built via the codex
    provider so this test tracks the real production invocation.
    """
    from issue_orchestrator.execution.agent_runner_providers import get_provider

    repo_root = Path(__file__).resolve().parents[2]
    work_dir = Path(tempfile.mkdtemp(prefix=".live-codex-int-", dir=repo_root))
    response_file = work_dir / "review-response.json"
    bootstrap = (
        "You are the reviewer in a persistent review exchange. Wait for "
        "the orchestrator to send each turn via stdin, write exactly one "
        "line of JSON to $ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE for "
        "each turn, then keep waiting for the next prompt. This setup "
        "message is NOT a turn: do not write to the response file until "
        "a review turn arrives via stdin."
    )
    task = (
        "Run exactly this one shell command and nothing else (no "
        "sleeping, no waiting commands): "
        'printf \'{"response_type":"ok"}\' > '
        '"$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"'
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"] = str(response_file)
    command = get_provider("codex").build_command(
        bootstrap,
        execution_mode="interactive",
        approval_mode="full-auto",
        reasoning_effort="low",
        working_directory=work_dir,
    )

    session = open_persistent_session(
        command=command,
        working_dir=work_dir,
        env=env,
    )
    try:
        assert session.proc.poll() is None, (
            "interactive codex must stay alive after launch — it is a "
            "persistent TUI, not one-shot exec mode"
        )

        for n in (1, 2, 3):
            # max_wait must comfortably exceed codex's worst-case turn time:
            # if the settle gives up while codex is still chewing on the
            # bootstrap, codex's late improvised write can land after the
            # unlink below and be misread as round n's answer.
            _wait_for_pty_idle(session, quiet_seconds=3.0, max_wait=120.0)
            response_file.unlink(missing_ok=True)
            result = send_round(
                session,
                prompt=task,
                response_file=response_file,
                timeout_seconds=120,
                poll_interval_seconds=0.3,
                role_label=f"reviewer@round-{n}",
            )
            assert result == {"response_type": "ok"}, (
                f"round {n} not answered — the prompt+separate-Enter "
                f"submit must reach interactive codex. Got: {result}"
            )
            assert session.proc.poll() is None, f"codex exited after round {n}"
    finally:
        close_persistent_session(session)
        shutil.rmtree(work_dir, ignore_errors=True)
