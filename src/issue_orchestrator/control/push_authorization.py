"""Issuing and revoking the orchestrator's authorization for one dirty push.

The pre-publish gate and the push itself both run the worktree's real pre-push
hook, and a git hook can only see the filesystem. It must not be left to infer
what the current publish is for by reading stored records: run directories are
retained (seven by default) and processed records are deliberately copied into
them, so "is there an escalation record here?" answers a question about history.
Asking it that way was wrong in both directions -- retained ``completed``
history blocked a live escalation, and a retained ``blocked`` record relaxed the
guard for unrelated pushes indefinitely.

So the orchestrator states what it has already decided, for exactly as long as
the push takes, and takes the statement away again afterwards.
"""

import contextlib
import json
import logging
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

from ..domain.dirty_remediation import (
    PUSH_AUTHORIZATION_PATH,
    DirtyTreeDisposition,
    PushAuthorization,
    dirty_tree_disposition,
)
from ..domain.models import CompletionRecord

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def authorized_push(
    worktree: Path, record: CompletionRecord
) -> Generator[None]:
    """Authorize a dirty push of ``worktree`` for the body of this block.

    Only a record whose outcome earns the exemption is authorized; every other
    completion leaves no authorization at all, so the guard stays armed. A
    failure to write is safe for the same reason -- the guard simply holds.
    """
    path = worktree / PUSH_AUTHORIZATION_PATH
    issued = False

    if dirty_tree_disposition(record.outcome.value) is (
        DirtyTreeDisposition.PRESERVE_AND_ESCALATE
    ):
        authorization = PushAuthorization(
            session_id=record.session_id or "",
            outcome=record.outcome.value,
            worktree=str(worktree.resolve()),
            issued_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(authorization.to_dict(), indent=2))
            issued = True
            logger.info(
                "Authorized dirty push for %s (outcome=%s)",
                worktree,
                record.outcome.value,
            )
        except OSError:
            logger.warning("Could not write push authorization at %s", path)

    try:
        yield
    finally:
        if issued:
            path.unlink(missing_ok=True)
