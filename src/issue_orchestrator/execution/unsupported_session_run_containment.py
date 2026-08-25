"""Session-runner adapter for containing unrestorable live terminals."""

from __future__ import annotations

import logging

from ..domain.session_restoration import UnsupportedSessionRun
from ..ports.session_runner import SessionRunner


logger = logging.getLogger(__name__)


class SessionRunnerUnsupportedSessionRunContainment:
    """Stop an unsupported live run through the terminal lifecycle owner."""

    def __init__(self, runner: SessionRunner) -> None:
        self._runner = runner

    def contain(self, run: UnsupportedSessionRun) -> None:
        if type(run) is not UnsupportedSessionRun:
            raise ValueError(
                "SessionRunnerUnsupportedSessionRunContainment.contain requires "
                "UnsupportedSessionRun"
            )
        logger.error(
            "[ORPHAN] Containing unsupported live session: issue=%d session=%s "
            "reason=%s",
            run.issue_number,
            run.session_name,
            run.reason,
        )
        self._runner.kill_session(run.issue_number, run.session_name)
