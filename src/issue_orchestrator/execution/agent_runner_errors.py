"""Provider error classification for agent-runner.

Classifies provider failures into coarse categories for retry/circuit logic.

This module holds **the** classification table (#6999). Interactive TUI
providers, the one-shot ``provider_runner`` wrapper, the launch-time readiness
probe, and live-session diagnosis all classify through here — no consumer keeps
a private token list, so "what does an auth failure look like" has one owner.
"""

from __future__ import annotations

from ..ports.provider_resilience import ProviderErrorType

__all__ = [
    "ProviderErrorType",
    "classify_provider_error",
    "classify_provider_output",
]


_TRANSIENT_TOKENS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "connection reset",
    "connection refused",
    "connection error",
    "econnreset",
    "econnrefused",
    "enotfound",
    "eai_again",
    "gateway timeout",
    "bad gateway",
    "502",
    "503",
    "504",
    "500",
)

_RATE_LIMIT_TOKENS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "quota",
    "throttle",
)

# HTTP-flavoured tokens (one-shot API failures) *and* the interactive TUI
# banners that an expired CLI login renders instead of ever calling the API.
# The TUI banners are what four 90-minute zero-work sessions actually showed on
# 2026-08-04; without them the exact observed failure classified as TRANSIENT.
_AUTH_TOKENS = (
    "unauthorized",
    "forbidden",
    "authentication",
    "invalid api key",
    "invalid_api_key",
    "401",
    "403",
    # "authentication" does not match "authenticate", so both stems are listed.
    # The startup AI gate saw "Failed to authenticate: ..." (#6999 F2 round 6)
    # and had grown its own table because this one could not read that banner.
    "authenticate",
    # Claude Code TUI: "Login expired · Please run /login"
    "login expired",
    "please run /login",
    "run /login to",
    # Covers both "Session expired. Please run /login" and the AI gate's
    # "OAuth session expired and could not be refreshed".
    "session expired",
    # Codex TUI / CLI: "Not logged in", "Please run `codex login`"
    "not logged in",
    "codex login",
    "please log in",
    "please login",
    "credentials have expired",
    "oauth token has expired",
    "refresh token has expired",
)

_FATAL_TOKENS = (
    "bad request",
    "invalid request",
    "invalid argument",
    "unsupported",
    "not supported",
    "400",
)


def classify_provider_output(text: str) -> ProviderErrorType | None:
    """Classify raw provider output against the one classification table.

    The only place any provider text is matched against error tokens. Callers
    that need a typed answer about provider output — the CLI readiness probe,
    live-session diagnosis, the one-shot runner — come through here rather than
    re-listing tokens, so a new banner is learned everywhere at once.
    """
    lowered = text.lower()

    if any(token in lowered for token in _RATE_LIMIT_TOKENS):
        return ProviderErrorType.RATE_LIMIT
    if any(token in lowered for token in _AUTH_TOKENS):
        return ProviderErrorType.AUTH
    if any(token in lowered for token in _FATAL_TOKENS):
        return ProviderErrorType.FATAL
    if any(token in lowered for token in _TRANSIENT_TOKENS):
        return ProviderErrorType.TRANSIENT
    return None


def classify_provider_error(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
) -> ProviderErrorType | None:
    """Classify provider error based on output and exit status.

    Both stdout and stderr are captured via PIPE and tee'd to the parent's
    stdout/stderr in real-time so PTY output is preserved. The captured text
    is used here for transient error classification (retry logic).

    A wall-clock timeout does **not** mask an auth failure. An auth-dead
    provider sits at its login banner until the timeout fires, so classifying
    ``timed_out`` as TRANSIENT before reading the text is exactly how a
    credential outage got retried as a blip (#6999). AUTH is the one category a
    retry can never fix, so it wins over the timeout; every other timeout still
    degrades to TRANSIENT, leaving existing retry behaviour untouched.
    """
    classified = classify_provider_output(f"{stdout}\n{stderr}")

    if timed_out:
        if classified is ProviderErrorType.AUTH:
            return classified
        return ProviderErrorType.TRANSIENT

    if classified is not None:
        return classified

    # Exit code-only heuristics
    if exit_code in (126, 127):
        return ProviderErrorType.FATAL

    return None
