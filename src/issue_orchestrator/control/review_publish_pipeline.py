"""Mode-specific publish pipeline planning.

This module decides action ordering and whether review exchange must run
before publish actions. It keeps mode policy centralized instead of scattering
mode checks across completion processing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from ..domain.models import RequestedAction


@dataclass(frozen=True)
class PublishPipelinePlan:
    """Execution plan for completion publish actions."""

    ordered_actions: tuple[RequestedAction, ...]
    run_review_exchange_before_publish: bool


class ReviewPublishPipeline(Protocol):
    """Strategy interface for mode-specific publish planning."""

    def plan(self, requested_actions: Sequence[RequestedAction]) -> PublishPipelinePlan:
        ...


class DraftPrPublishPipeline:
    """Default GitHub draft-PR style flow."""

    def plan(self, requested_actions: Sequence[RequestedAction]) -> PublishPipelinePlan:
        return PublishPipelinePlan(
            ordered_actions=tuple(requested_actions),
            run_review_exchange_before_publish=False,
        )


class LocalLoopPublishPipeline:
    """Local loop flow: review exchange must complete before publish."""

    def plan(self, requested_actions: Sequence[RequestedAction]) -> PublishPipelinePlan:
        ordered = tuple(requested_actions)
        requires_pr = RequestedAction.CREATE_PR in ordered
        return PublishPipelinePlan(
            ordered_actions=ordered,
            run_review_exchange_before_publish=requires_pr,
        )


def resolve_review_publish_pipeline(exchange_mode: str | None) -> ReviewPublishPipeline:
    """Resolve strategy by configured review exchange mode."""
    if exchange_mode in {"via-local-loop", "via-mcp"}:
        return LocalLoopPublishPipeline()
    return DraftPrPublishPipeline()
