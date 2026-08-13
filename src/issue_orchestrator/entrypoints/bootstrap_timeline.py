"""Composition helper for durable Timeline services."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from ..execution import (
    DefaultTimelineReader,
    DefaultTimelineWriter,
    SqliteTimelineStore,
    TimelineEventSink,
    TimelineStoreConfig,
)
from ..execution.timeline_evidence import FileSystemTimelineEvidence
from ..infra.config import Config
from ..infra.repo_identity import state_dir
from ..ports.event_sink import EventSink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimelineComposition:
    """Concrete Timeline services sharing one store and instance identity."""

    instance_id: str
    store: SqliteTimelineStore
    reader: DefaultTimelineReader
    writer: DefaultTimelineWriter
    evidence: FileSystemTimelineEvidence
    sink: EventSink


def create_timeline_composition(config: Config) -> TimelineComposition:
    """Build the production Timeline storage, retention, and event pipeline."""
    instance_id = str(uuid4())
    logger.info("Orchestrator instance_id=%s", instance_id)
    repository_state = state_dir(config.repo_root)
    store = SqliteTimelineStore(
        repository_state / "timeline.sqlite",
        TimelineStoreConfig(max_records=config.timeline.max_records),
        instance_id=instance_id,
    )
    reader = DefaultTimelineReader(store)
    writer = DefaultTimelineWriter(store)
    evidence = FileSystemTimelineEvidence(
        archive_root=repository_state / "timeline-evidence",
        timeline_store=store,
    )
    return TimelineComposition(
        instance_id=instance_id,
        store=store,
        reader=reader,
        writer=writer,
        evidence=evidence,
        sink=TimelineEventSink(writer),
    )
