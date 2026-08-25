"""Small dependency value containers exported by the composition root."""

from __future__ import annotations

from ..ports.event_sink import EventSink
from ..ports.repository_host import RepositoryHost
from ..ports.session_runner import SessionRunner


class Dependencies:
    """Container for the legacy minimal bootstrap dependency surface."""

    def __init__(
        self,
        events: EventSink,
        runner: SessionRunner,
        github: RepositoryHost | None = None,
    ) -> None:
        self.events = events
        self.runner = runner
        self.github = github
