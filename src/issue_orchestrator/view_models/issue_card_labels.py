"""How an issue's orchestrator labels project onto a dashboard card.

One owner for the label-driven parts of a card: which labels show as pills,
the blocked-reason summary, and the provider-outage badge. Card builders and
the row renderers consume these projections; none of them re-derives label
semantics (prefixes, blocking categories, human descriptions) for itself.
"""

from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict

from ..control.label_manager import LabelManager

# Colour is never the only signal: the badge always carries this text.
PROVIDER_BADGE_TONE = "blocked"
PROVIDER_BADGE_TITLE = (
    "Blocked by a provider outage — the orchestrator will not launch work for "
    "this issue until the provider circuit closes. See the provider health "
    "panel for the current cooldown."
)


class ProviderBadgeView(BaseModel):
    """Precomputed provider-outage badge for a queue/kanban row (issue #5980).

    Precomputed server-side (like ``stack_chip``) so the first-paint Jinja DOM
    and the client rebuild render identical markup from identical inputs.

    Deliberately carries no cooldown/ETA: those tick every second, and folding
    them into a per-card payload would re-fingerprint every affected card on
    every refresh and flash the DOM. The live countdown belongs to the global
    banner and health panel.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tone: str
    label_text: str
    title: str


def blocked_summary(
    labels: list[str],
    lm: LabelManager,
    dependency_summary: str | None = None,
) -> str | None:
    reasons: list[str] = []
    blocking = lm.get_blocking(labels)
    if blocking:
        reasons.append(lm.describe(blocking[0]))
    if dependency_summary:
        reasons.append(dependency_summary)
    return " • ".join(reasons) if reasons else None


def display_labels(labels: list[str], lm: LabelManager) -> list[str]:
    """Labels shown as pills in UI cards.

    Include orchestrator-owned labels and agent routing labels.
    """
    visible = set(lm.get_ours(labels))
    visible.update(label for label in labels if label.startswith("agent:"))
    return sorted(visible)


def is_provider_blocked(labels: Iterable[str], lm: LabelManager) -> bool:
    """Whether *labels* carry the configured provider-unavailable label.

    Prefix/config aware: the label text comes from :class:`LabelManager`, so a
    repo that configures a label prefix or renames the label keeps working.
    """
    wanted = lm.provider_unavailable.casefold()
    return any(str(label).casefold() == wanted for label in labels)


def provider_badge(labels: Iterable[str], lm: LabelManager) -> ProviderBadgeView | None:
    """The provider-outage badge for a card, or ``None`` when unaffected."""
    if not is_provider_blocked(labels, lm):
        return None
    return ProviderBadgeView(
        tone=PROVIDER_BADGE_TONE,
        label_text=lm.describe(lm.provider_unavailable),
        title=PROVIDER_BADGE_TITLE,
    )


def provider_badge_payload(badge: ProviderBadgeView | None) -> Any:
    """Serialize a precomputed provider badge for embedding in a card payload."""
    return badge.model_dump(mode="json") if badge is not None else None


def provider_signal(badge: ProviderBadgeView | None) -> str:
    """Fingerprint-safe encoding of the badge a card renders.

    Empty when no badge shows. Encodes every field the row renderers read, so
    a card that gains or loses the badge — or whose badge text changes because
    the label was reconfigured — re-fingerprints and is rebuilt.
    """
    if badge is None:
        return ""
    return ":".join((badge.tone, badge.label_text, badge.title))


__all__ = [
    "PROVIDER_BADGE_TITLE",
    "PROVIDER_BADGE_TONE",
    "ProviderBadgeView",
    "blocked_summary",
    "display_labels",
    "is_provider_blocked",
    "provider_badge",
    "provider_badge_payload",
    "provider_signal",
]
