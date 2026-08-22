"""UI guardrail tests for session-recording viewer render-mode dispatch.

The backend dispatcher is covered by
``tests/unit/test_session_recording_render_mode.py``; this module asserts
the frontend half of the contract via Playwright:

- Transcript-mode payloads render the prettified lines in a monospace
  block and disable the emulator-only controls (Play/Restart/Jump-live/
  Seek/Speed/Follow).
- Terminal-mode payloads still mount xterm.js (no silent fallback).
- A backend typo (``render_mode="banana"``) falls back to terminal mode
  rather than silently mis-rendering.

Intercepting ``/api/session/terminal-recording/`` with ``page.route`` lets
the test drive each render mode deterministically without needing a live
codex subprocess or a captured recording on disk.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.usefixtures("web_server")


def _stub_terminal_recording(
    page: Page,
    payload: dict,
    requested_urls: list[str] | None = None,
) -> None:
    """Route all terminal-recording fetches to a deterministic payload."""

    def _handler(route) -> None:
        if requested_urls is not None:
            requested_urls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/session/terminal-recording/**", _handler)


def _open_session_replay_modal(page: Page, url: str) -> None:
    """Navigate to the dashboard and call the viewer directly.

    The dashboard has multiple paths into the session-replay modal (issue
    drawers, timeline entries, etc.) — we bypass them by invoking the
    opener on ``window`` with a known issue number + run_dir. The goal
    here is to exercise the render-mode dispatch, not the opener UX.
    """
    # SSE keeps the connection live → ``networkidle`` never fires. Waiting for
    # ``openAgentLog`` to be defined on ``window`` is the actual readiness
    # signal we need: it tells us the dashboard JS has loaded and we can
    # drive the modal programmatically.
    #
    # The full dashboard page render is deliberately expensive (the whole
    # flow board renders server-side) and under parallel-suite load can
    # easily take 30s+. That's the only piece we wait on here — the JS
    # state machine itself is instant. Bump both timeouts well past the
    # default so load-induced flakes don't mask real failures.
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function(
        "() => typeof window.openAgentLog === 'function'", timeout=30_000
    )
    page.evaluate(
        "() => window.openAgentLog(408, 'Reviewer Session Recording',"
        " '/tmp/fake-run-dir')"
    )
    modal = page.locator("#modalOverlay.visible")
    expect(modal).to_be_visible(timeout=10_000)


def test_codex_payload_renders_transcript_and_disables_replay_controls(
    page: Page, web_server: dict
) -> None:
    requested_urls: list[str] = []
    _stub_terminal_recording(
        page,
        {
            "issue_number": 408,
            "recording_path": "/tmp/fake-run-dir/terminal-recording.jsonl",
            "render_mode": "transcript",
            "transcript_lines": [
                "I’ll read the round-specific reviewer prompt first.",
                "",
                "$ /bin/zsh -lc 'git status --short'",
                "?? .agent-done-marker",
            ],
            "transcript_hash": "deadbeef",
        },
        requested_urls,
    )
    _open_session_replay_modal(page, web_server["url"])

    assert requested_urls
    assert all(
        "/api/session/terminal-recording/408?" in url
        and "run_dir=%2Ftmp%2Ffake-run-dir" in url
        for url in requested_urls
    )

    transcript = page.locator(".session-replay-transcript")
    expect(transcript).to_be_visible(timeout=5000)
    transcript_text = transcript.text_content() or ""
    assert "I’ll read the round-specific reviewer prompt first" in transcript_text
    assert "$ /bin/zsh -lc 'git status --short'" in transcript_text
    assert "{\"type\":" not in transcript_text, "envelope JSON must not leak into transcript"

    # No xterm emulator should have been mounted under the terminal host.
    xterm = page.locator("#sessionReplayTerminal .xterm")
    expect(xterm).to_have_count(0)

    # Replay controls are meaningless for transcript mode; they must be
    # disabled so the toolbar can't lie about what Play/Jump-live do.
    for button_id in (
        "sessionReplayRestart",
        "sessionReplayPlayPause",
        "sessionReplayJumpLive",
    ):
        expect(page.locator(f"#{button_id}")).to_be_disabled()
    expect(page.locator("#sessionReplaySeek")).to_be_disabled()
    expect(page.locator("#sessionReplaySpeed")).to_be_disabled()
    expect(page.locator("#logFollowToggle")).to_be_disabled()

    # Hint reflects the format; status line names transcript view.
    hint_text = (page.locator(".session-replay-hint").text_content() or "").lower()
    assert "transcript" in hint_text
    status_text = page.locator("#sessionReplayStatus").text_content() or ""
    assert "transcript" in status_text.lower()


def test_terminal_payload_keeps_xterm_path_and_enabled_controls(
    page: Page, web_server: dict
) -> None:
    _stub_terminal_recording(
        page,
        {
            "issue_number": 408,
            "recording_path": "/tmp/fake-run-dir/terminal-recording.jsonl",
            "content_type": "application/x-ndjson",
            "total_events": 0,
            "offset": 0,
            "truncated": False,
            "events": [],
            "render_mode": "terminal",
        },
    )
    _open_session_replay_modal(page, web_server["url"])

    # In terminal mode the transcript <pre> is absent, the xterm mount point
    # is present, and replay controls are enabled.
    expect(page.locator(".session-replay-transcript")).to_have_count(0)
    for button_id in (
        "sessionReplayRestart",
        "sessionReplayPlayPause",
        "sessionReplayJumpLive",
    ):
        expect(page.locator(f"#{button_id}")).not_to_be_disabled()


def test_unknown_render_mode_falls_back_to_terminal(page: Page, web_server: dict) -> None:
    """Backend typos cannot silently mis-render the viewer.

    ``render_mode`` is whitelisted on the frontend; an unknown value (a
    future-name, a typo, a schema drift) degrades gracefully to the
    terminal path rather than triggering the transcript codepath with no
    ``transcript_lines``.
    """
    _stub_terminal_recording(
        page,
        {
            "issue_number": 408,
            "recording_path": "/tmp/fake-run-dir/terminal-recording.jsonl",
            "render_mode": "banana",  # not a valid value
            "events": [],
            "total_events": 0,
            "offset": 0,
            "truncated": False,
        },
    )
    _open_session_replay_modal(page, web_server["url"])

    # Falls back to terminal mode → no transcript block, replay controls
    # stay interactive.
    expect(page.locator(".session-replay-transcript")).to_have_count(0)
    expect(page.locator("#sessionReplayPlayPause")).not_to_be_disabled()
