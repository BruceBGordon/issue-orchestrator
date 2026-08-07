"""Shared worktree adapter exceptions.

Lives in its own module so runtime-setup helpers can fail fast with the same
error type ``_worktree.py`` raises, without importing the lifecycle module
back and creating an import cycle.
"""

from __future__ import annotations

__all__ = ["WorktreeError"]


class WorktreeError(Exception):
    """Raised when a worktree operation fails."""
