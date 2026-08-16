"""Process-environment setup owned by the composition root."""

from __future__ import annotations

import os
import sys

ISSUE_ORCHESTRATOR_PYTHON_ENV = "ISSUE_ORCHESTRATOR_PYTHON"


def export_orchestrator_python() -> None:
    """Expose an interpreter that child worktree hooks can import from.

    The pre-push hook installed in target-repository worktrees calls a helper
    from this package, while non-Python targets have no project virtualenv that
    can import it. Operator overrides remain authoritative.
    """
    os.environ.setdefault(ISSUE_ORCHESTRATOR_PYTHON_ENV, sys.executable)
