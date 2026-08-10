"""Dashboard flow lane and compact-card builders."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from ..domain.issue_key import format_issue_label

StaleBadgeVisibilityMode = Literal["when_stale", "when_stale_and_merge_pending", "never"]
COMPACT_COLUMN_PREVIEW_LIMIT = 12


def compute_compact_card_fingerprint(card: dict[str, Any]) -> str:
    """Mirror of `compactCardState.computeCompactCardFingerprint` in
    `static/js/compact_card_state.js`. Output must match exactly so the
    server-rendered `data-card-fingerprint` attribute is interpreted as
    "no change" by the client during the first refresh and the JS skips
    replacing the DOM node.

    `phase_age` (a relative time string that ticks every few seconds) is
    intentionally excluded — including it would force every running card
    to be replaced on every tick, which causes visible flashes. The
    client syncs the phase-age text in place when a card is reused.
    """

    def _s(value: Any) -> str:
        return "" if value is None else str(value)

    raw_labels = card.get("orchestrator_labels")
    labels: list[Any] = raw_labels if isinstance(raw_labels, list) else []
    labels_str = ",".join(str(label) for label in labels)
    stale = "true" if bool(card.get("is_stale")) else "false"
    show_stale_badge = "true" if bool(card["show_stale_badge"]) else "false"
    parts = [
        _s(card.get("card_id")),
        _s(card.get("issue_number")),
        _s(card.get("issue_key")),
        _s(card.get("issue_label")),
        _s(card.get("title")),
        _s(card.get("state_label")),
        _s(card.get("phase")),
        _s(card.get("summary")),
        stale,
        show_stale_badge,
        _s(card.get("stale_reason")),
        _s(card.get("issue_url")),
        _s(card.get("pr_url")),
        _s(card.get("github_url")),
        _s(card.get("github_label")),
        _s(card.get("github_title")),
        _s(card.get("github_aria_label")),
        labels_str,
        _s(card.get("stack_signal")),
        # `provider_signal` encodes the precomputed provider-outage badge. It
        # deliberately excludes the cooldown/ETA so a counting-down outage does
        # not re-fingerprint (and re-flash) every affected card each refresh;
        # gaining or losing the badge does re-fingerprint.
        _s(card.get("provider_signal")),
        # `run_dir` binds the reused card node to a specific run. A card whose
        # other fields are unchanged but whose run directory advanced (e.g. a
        # `rework-<issue>` slot replaced by a new run) must re-fingerprint so
        # the stale `data-run-dir` — and the launch-prompt action that reads
        # it — cannot survive on a reused DOM node.
        _s(card.get("run_dir")),
    ]
    return "|".join(parts)


def compact_card(item: dict[str, Any], state_label: str | None = None) -> dict[str, Any]:
    phase = item.get("flow_stage_label") or item.get("flow_stage") or ""
    phase_age = item.get("time") or ""
    # When the source time is an ISO timestamp the phase-age must be localized
    # client-side via the shared `data-dashboard-timestamp` mechanism rather
    # than rendered raw (UTC). Relative labels ("5 min") render as-is.
    phase_age_is_timestamp = bool(item.get("time_is_timestamp"))
    blocked = item.get("blocked_summary") or ""
    summary_text = item.get("queue_wait_reason") or item.get("summary") or (f"Summary: {blocked}" if blocked else "")
    issue_number = item.get("issue_number")
    issue_key = item.get("issue_key") or None
    issue_label = item.get("issue_label") or format_issue_label(issue_number, issue_key)
    issue_url = item.get("issue_url") or item.get("url") or ""
    pr_url = item.get("pr_url") or ""
    resolved_state_label = state_label or item.get("status", "")
    primary_is_pr = resolved_state_label == "awaiting merge" and bool(pr_url)
    github_url = pr_url if primary_is_pr else issue_url
    github_label = "PR ↗" if primary_is_pr else "↗"
    github_title = "Open PR on GitHub" if primary_is_pr else "Open issue on GitHub"
    github_aria_label = (
        f"Open PR for issue #{issue_number} on GitHub"
        if primary_is_pr
        else f"Open issue #{issue_number} on GitHub"
    )
    card: dict[str, Any] = {
        "card_id": item.get("card_id") or f"issue-{issue_number}",
        "issue_number": issue_number,
        "issue_key": issue_key,
        "issue_label": issue_label,
        "title": item.get("title", ""),
        "agent_type": item.get("agent_type", ""),
        "state_label": resolved_state_label,
        "phase": phase,
        "phase_age": phase_age,
        "time_is_timestamp": phase_age_is_timestamp,
        "summary": summary_text,
        "queue_wait_reason": item.get("queue_wait_reason"),
        "blocked_summary": blocked,
        "badges": [],
        "orchestrator_labels": item.get("orchestrator_labels", []),
        "focus_action": "focus",
        "issue_url": issue_url,
        "pr_url": pr_url,
        "github_url": github_url,
        "github_label": github_label,
        "github_title": github_title,
        "github_aria_label": github_aria_label,
        "focus_hint": "Focus issue",
        "github_hint": github_title,
        # Present only for running sessions; lets the compact-card prompt
        # action open the run-scoped launch prompt.
        "run_dir": item.get("run_dir", ""),
        "last_refreshed_label": item.get("last_refreshed_label", "unknown"),
        "is_stale": bool(item.get("is_stale", False)),
        "show_stale_badge": bool(item["show_stale_badge"]),
        "stale_reason": item.get("stale_reason", ""),
        "last_refreshed_age_seconds": item.get("last_refreshed_age_seconds", -1),
        "stack_dependency": item.get("stack_dependency"),
        # Precomputed by the item builder from the typed gate view; copied here
        # so the compact card (and its fingerprint) stay in step with the model.
        "stack_signal": item.get("stack_signal") or "",
        # Precomputed chip display; carried so the server-rendered first paint and
        # the client rebuild render the identical chip.
        "stack_chip": item.get("stack_chip"),
        # Precomputed provider-outage badge + its fingerprint signal (#5980),
        # carried for the same reason as the stack chip above.
        "provider_badge": item.get("provider_badge"),
        "provider_signal": item.get("provider_signal") or "",
    }
    card["fingerprint"] = compute_compact_card_fingerprint(card)
    return card


def stamp_issue_item_stale_badge_visibility(
    items: Iterable[dict[str, Any]],
    *,
    mode: StaleBadgeVisibilityMode,
) -> None:
    """Stamp the required issue-item stale badge display policy."""
    for item in items:
        if mode == "when_stale":
            item["show_stale_badge"] = bool(item["is_stale"])
        elif mode == "when_stale_and_merge_pending":
            item["show_stale_badge"] = bool(item["is_stale"]) and bool(item["merge_pending"])
        elif mode == "never":
            item["show_stale_badge"] = False
        else:
            raise ValueError(f"unknown stale badge visibility mode: {mode}")


def exclude_flow_overlaps(
    backlog_items: list[dict[str, Any]],
    queue_items: list[dict[str, Any]],
    active_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    completed_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep scope count accurate by removing items already in a kanban column.

    Backlog is used only for scope_summary.in_scope_total; anything already
    represented in queued/running/blocked/completed should not be double-counted.
    """

    def _to_issue_number(raw: Any) -> int | None:
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        return None

    occupied_numbers = {
        issue_number
        for item in queue_items + active_items + blocked_items + completed_items
        for issue_number in [_to_issue_number(item.get("issue_number"))]
        if issue_number is not None
    }
    return [
        item
        for item in backlog_items
        for issue_number in [_to_issue_number(item.get("issue_number"))]
        if issue_number is not None and issue_number not in occupied_numbers
    ]


def _issue_number(item: dict[str, Any]) -> int | None:
    raw = item.get("issue_number")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _issue_numbers(items: list[dict[str, Any]]) -> set[int]:
    """Extract numeric issue numbers from card items."""
    return {
        issue_number
        for item in items
        for issue_number in [_issue_number(item)]
        if issue_number is not None
    }


def _exclude_issue_numbers(
    items: list[dict[str, Any]],
    excluded_numbers: set[int],
) -> list[dict[str, Any]]:
    """Return items whose issue number is not in excluded_numbers."""
    filtered: list[dict[str, Any]] = []
    for item in items:
        issue_number = _issue_number(item)
        if issue_number is None or issue_number not in excluded_numbers:
            filtered.append(item)
    return filtered


def apply_lane_precedence(
    queue_items: list[dict[str, Any]],
    active_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    awaiting_merge_items: list[dict[str, Any]],
    completed_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Enforce lane ownership and card stale-badge visibility.

    Precedence:
    running > blocked > awaiting-merge > queued > completed

    Mutates returned items, and the active items input, to stamp
    ``show_stale_badge``. Completed lane staleness remains a raw
    diagnostic fact but is not surfaced as an operator warning.
    """
    active_numbers = _issue_numbers(active_items)
    blocked_filtered = _exclude_issue_numbers(blocked_items, active_numbers)
    blocked_numbers = _issue_numbers(blocked_filtered)

    awaiting_filtered = _exclude_issue_numbers(awaiting_merge_items, active_numbers | blocked_numbers)
    awaiting_numbers = _issue_numbers(awaiting_filtered)

    queue_filtered = _exclude_issue_numbers(queue_items, active_numbers | blocked_numbers | awaiting_numbers)
    queue_numbers = _issue_numbers(queue_filtered)

    completed_filtered = _exclude_issue_numbers(
        completed_items,
        active_numbers | blocked_numbers | awaiting_numbers | queue_numbers,
    )
    stamp_issue_item_stale_badge_visibility(active_items, mode="when_stale")
    stamp_issue_item_stale_badge_visibility(blocked_filtered, mode="when_stale")
    stamp_issue_item_stale_badge_visibility(awaiting_filtered, mode="when_stale")
    stamp_issue_item_stale_badge_visibility(queue_filtered, mode="when_stale")
    stamp_issue_item_stale_badge_visibility(completed_filtered, mode="never")
    return queue_filtered, blocked_filtered, awaiting_filtered, completed_filtered


def _awaiting_merge_preference(item: dict[str, Any]) -> tuple[bool, bool]:
    """Rank duplicate awaiting-merge sources by visible card usefulness."""
    return (bool(item.get("pr_url")), item.get("status") == "completed")


def _dedupe_awaiting_merge_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    positions: dict[int, int] = {}
    for item in items:
        issue_number = _issue_number(item)
        if issue_number is None:
            deduped.append(item)
            continue

        existing_position = positions.get(issue_number)
        if existing_position is None:
            positions[issue_number] = len(deduped)
            deduped.append(item)
            continue

        existing_item = deduped[existing_position]
        if _awaiting_merge_preference(item) > _awaiting_merge_preference(existing_item):
            deduped[existing_position] = item

    return deduped


def build_awaiting_merge_items(
    queue_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    history_items: list[dict[str, Any]],
    *,
    exclude_issue_numbers: frozenset[int] = frozenset(),
) -> list[dict[str, Any]]:
    """Items with PRs ready to merge, drawn from all lifecycle stages.

    ``exclude_issue_numbers`` are owned by another lane (e.g. issues queued for
    rework belong to the Queued lane) and are dropped here — across *every*
    source, not just one — so a stale ``merge_pending`` signal on any single
    source cannot pull them into Awaiting Merge.
    """
    return _dedupe_awaiting_merge_items([
        item
        for item in queue_items + blocked_items + history_items
        if item.get("merge_pending") and _issue_number(item) not in exclude_issue_numbers
    ])


def build_flow_columns(
    queue_items: list[dict[str, Any]],
    queue_preview_items: list[dict[str, Any]],
    active_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    awaiting_merge_items: list[dict[str, Any]],
    completed_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Exclude merge-pending items from the queued column because they appear in awaiting-merge.
    awaiting_numbers = {item.get("issue_number") for item in awaiting_merge_items}
    queued_only = [item for item in queue_items if item.get("issue_number") not in awaiting_numbers]
    queued_preview_only = [
        item
        for item in queue_preview_items
        if item.get("issue_number") not in awaiting_numbers
    ]
    queued_cards = [
        compact_card(item, "queued")
        for item in queued_preview_only[:COMPACT_COLUMN_PREVIEW_LIMIT]
    ]
    running_cards = [
        compact_card(item, "running")
        for item in active_items[:COMPACT_COLUMN_PREVIEW_LIMIT]
    ]
    blocked_cards = [
        compact_card(item, "blocked")
        for item in blocked_items[:COMPACT_COLUMN_PREVIEW_LIMIT]
    ]
    awaiting_merge_cards = [
        compact_card(item, "awaiting merge")
        for item in awaiting_merge_items[:COMPACT_COLUMN_PREVIEW_LIMIT]
    ]
    completed_cards = [
        compact_card(item, "completed")
        for item in completed_items[:COMPACT_COLUMN_PREVIEW_LIMIT]
    ]

    return [
        {
            "id": "queued",
            "title": "Queued",
            "count": len(queued_only),
            "items": queued_cards,
            "hidden_count": max(len(queued_only) - len(queued_cards), 0),
            "expandable": True,
        },
        {
            "id": "running",
            "title": "Running",
            "count": len(active_items),
            "items": running_cards,
            "hidden_count": max(len(active_items) - len(running_cards), 0),
            "expandable": True,
        },
        {
            "id": "blocked",
            "title": "Blocked",
            "count": len(blocked_items),
            "items": blocked_cards,
            "hidden_count": max(len(blocked_items) - len(blocked_cards), 0),
            "expandable": True,
        },
        {
            "id": "awaiting-merge",
            "title": "Awaiting Merge",
            "count": len(awaiting_merge_items),
            "items": awaiting_merge_cards,
            "hidden_count": max(len(awaiting_merge_items) - len(awaiting_merge_cards), 0),
            "expandable": True,
        },
        {
            "id": "completed",
            "title": "Completed",
            "count": len(completed_items),
            "items": completed_cards,
            "hidden_count": max(len(completed_items) - len(completed_cards), 0),
            "expandable": True,
            "session_scoped": True,
        },
    ]


def select_issues_for_tab(
    active_tab: str,
    active_items: list[dict[str, Any]],
    queue_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    e2e_items: list[dict[str, Any]],
    awaiting_merge_items: list[dict[str, Any]],
    completed_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if active_tab == "kanban":
        return active_items if active_items else queue_items
    if active_tab == "blocked":
        return blocked_items
    if active_tab == "awaiting-merge":
        return awaiting_merge_items
    if active_tab == "completed":
        return completed_items
    if active_tab == "e2e":
        return e2e_items
    return active_items


def normalize_dashboard_tab(active_tab: str) -> str:
    """Map a requested (possibly legacy) tab name onto a tab that exists.

    Beside :func:`select_issues_for_tab`: which tab is being shown and what
    belongs on it are one policy, and an unknown name must resolve to a real
    tab here rather than reaching a projection that has no rows for it.
    """
    if active_tab in {"work", "active", "queue", "flow"}:
        return "kanban"
    if active_tab == "attention":
        return "kanban"
    if active_tab in {"history", "merged"}:
        return "kanban"
    if active_tab in {"kanban", "blocked", "awaiting-merge", "completed", "e2e"}:
        return active_tab
    return "kanban"
