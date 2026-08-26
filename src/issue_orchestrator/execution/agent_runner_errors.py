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
    # Claude Code subscription banners. These are genuine rate limits — the
    # rolling five-hour and weekly windows reopen on a clock — but neither
    # phrase contains any token above, so both classified as ``None`` and the
    # session simply looked idle until its wall clock fired.
    "session limit",
    "weekly limit",
)

# An exhausted balance or usage allowance. Distinct from a rate limit because
# no amount of waiting restores it; distinct from AUTH because the credential
# is valid. Codex's primary quota banner is "You've hit your usage limit …
# purchase more credits", and its typed variants are
# ``usage_limit_exceeded`` / ``workspace_member_credits_depleted``. None of
# them contain "rate limit", "429", "quota", or "throttle", so every one of
# them classified as ``None`` and fell through every consumer (#7096).
#
# Matched ahead of the rate-limit table: these phrases are the more specific
# reading, and misfiling exhaustion as a rate limit is the failure mode that
# retried an out-of-credits account until its budget was gone. The bare token
# "quota" deliberately stays with the rate-limit table — reclassifying strings
# that already had a working classification is a separate question from
# covering strings that had none.
_QUOTA_TOKENS = (
    # Typed codes and code-shaped fragments. `usage_limit` covers both
    # `usage_limit_exceeded` and `workspace_owner_usage_limit_reached`.
    "usage_limit",
    "credits_depleted",
    "insufficient_quota",
    # Distinctive prose. Deliberately NOT the bare phrases "usage limit",
    # "spend limit" or "purchase more credits": this table is matched against
    # the whole PTY transcript, which includes everything the AGENT wrote, and
    # an agent working on billing or rate-limit code emits those words in
    # ordinary reasoning. A false positive here is expensive — quota trips on
    # the first observation and has no readiness probe, so without another
    # in-flight success it parks the provider for the full cooldown. Each phrase
    # below is long enough that an agent discussing the topic is unlikely to
    # reproduce it verbatim.
    "hit your usage limit",
    "out of credits",
    "spend limit reached",
    "reached your spending limit",
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
    # Codex refresh tokens are single-use and rotate, and the CLI takes no
    # cross-process lock around the rewrite. Concurrent sessions therefore hit
    # three sibling failures of "refresh token has expired" — all of which need
    # the same human re-login, and none of which shared a token with it.
    "token was already used",
    "token was revoked",
    "signed in to another account",
    "log out and sign in again",
    # Claude Code: "OAuth session expired and could not be refreshed" is
    # already covered by "session expired", but the refresh failure is also
    # reported on its own.
    "could not be refreshed",
    # Claude Code: "Invalid auth token · Fix external auth token". "invalid api
    # key" does not match it, and "authentication" is not present.
    "invalid auth token",
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

    if any(token in lowered for token in _QUOTA_TOKENS):
        return ProviderErrorType.QUOTA
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

    A wall-clock timeout does **not** mask a human-fixable failure. An
    auth-dead provider sits at its login banner until the timeout fires, so
    classifying ``timed_out`` as TRANSIENT before reading the text is exactly
    how a credential outage got retried as a blip (#6999). An account with no
    credits left behaves identically — it renders its banner and waits — so
    both categories a retry can never fix win over the timeout (#7096). Every
    other timeout still degrades to TRANSIENT, leaving existing retry
    behaviour untouched.
    """
    classified = classify_provider_output(f"{stdout}\n{stderr}")

    if timed_out:
        if classified is not None and classified.requires_human_intervention:
            return classified
        return ProviderErrorType.TRANSIENT

    if classified is not None:
        return classified

    # Exit code-only heuristics
    if exit_code in (126, 127):
        return ProviderErrorType.FATAL

    return None
