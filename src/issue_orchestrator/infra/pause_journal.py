"""Append-only JSONL adapter for the pause journal.

Mirrors the shape of ``validate-timings.jsonl``: one JSON object per line, in a
file small enough to tail by hand, trimmed so it cannot grow without bound.

Recording is best-effort by design. A pause is already a degraded moment —
often a disk or network fault, which is exactly when a journal write is most
likely to fail — and losing an audit row must never be the thing that stops the
engine from pausing, nor turn one tick error into two.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..domain.pause_state import PauseTransition
from ..ports.pause_journal import PauseJournal

logger = logging.getLogger(__name__)

PAUSE_JOURNAL_FILENAME = "pause-journal.jsonl"

# Generous enough to cover months of real transitions, bounded so a pathological
# pause/resume loop cannot fill the disk.
_MAX_ROWS = 500


class JsonlPauseJournal(PauseJournal):
    """Persist pause transitions to ``<state_dir>/pause-journal.jsonl``."""

    def __init__(self, path: Path):
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def record(self, transition: PauseTransition) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(transition.to_json()) + "\n")
            self._trim()
        except OSError as error:
            logger.warning(
                "[PAUSE] Could not append to pause journal %s: %s",
                self._path,
                error,
            )

    def recent(self, limit: int = 20) -> list[PauseTransition]:
        return self._read_all()[-limit:] if limit > 0 else []

    def _read_all(self) -> list[PauseTransition]:
        if not self._path.exists():
            return []
        rows: list[PauseTransition] = []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            logger.warning("[PAUSE] Could not read pause journal %s: %s", self._path, error)
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(PauseTransition.from_json(json.loads(line)))
            except (ValueError, KeyError) as error:
                # One malformed row must not blind the operator to the rest.
                logger.debug("[PAUSE] Skipping malformed pause journal row: %s", error)
        return rows

    def _trim(self) -> None:
        rows = self._read_all()
        if len(rows) <= _MAX_ROWS:
            return
        keep = rows[-_MAX_ROWS:]
        payload = "".join(json.dumps(row.to_json()) + "\n" for row in keep)
        self._path.write_text(payload, encoding="utf-8")
