"""How stale a dashboard card is, and why.

Extracted from ``dashboard.py`` (#6858 rework 1). Answering "is this card stale,
and what should we tell the reader" is a distinct concern from projecting cards:
it reads only the orchestrator heartbeat and a couple of thresholds, produces
plain strings, and none of it touches card payloads. Splitting it keeps
``dashboard.py`` inside its line budget and gives the staleness wording one place
to change.

``dashboard.py`` still owns ``_refresh_meta_for_issue``, which assembles the card
fields from these answers.
"""

#: Floor so a misconfiguration can't false-positive every tick.
TICK_STALL_FLOOR_SECONDS = 5


def format_age_seconds(seconds: float | int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def stale_reason_for(
    *,
    state,
    age_seconds: float | None,
    is_stale: bool,
    stale_threshold: int,
    tick_stall_threshold: int,
    now_ts: float,
) -> str:
    """Explain staleness using the orchestrator heartbeat when possible.

    Plain "refresh age > threshold" is rarely the real story: the underlying
    cause is almost always that the main loop is stuck doing something slow
    (subprocess, GH API call, lock). When ``last_tick_completed_at`` shows
    the loop hasn't finished a tick recently, say so and name the phase —
    that's actionable. Fall back to the legacy threshold text when the
    heartbeat looks healthy but GitHub refresh happens to lag.
    """
    if age_seconds is None:
        return "Not refreshed from GitHub yet"
    stall_reason = tick_stall_reason(state, now_ts, tick_stall_threshold)
    if stall_reason:
        return stall_reason
    if is_stale:
        return f"Older than {format_age_seconds(stale_threshold)} stale threshold"
    return ""


def tick_stall_reason(state, now_ts: float, threshold_seconds: int) -> str:
    last_completed = getattr(state, "last_tick_completed_at", 0.0) or 0.0
    last_started = getattr(state, "last_tick_started_at", 0.0) or 0.0
    if last_completed <= 0 and last_started <= 0:
        return ""  # orchestrator hasn't reported a heartbeat yet
    reference = last_completed if last_completed > 0 else last_started
    tick_age = now_ts - reference
    if tick_age <= threshold_seconds:
        return ""
    phase = (getattr(state, "current_tick_phase", "") or "").strip() or "idle"
    age_label = format_age_seconds(tick_age)
    return (
        f"Orchestrator tick stalled — last completion {age_label} ago (phase: {phase})"
    )
