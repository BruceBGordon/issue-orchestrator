"""Typed containment contract for live runs that cannot be restored safely."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UnsupportedSessionRun:
    """One live terminal whose persisted launch policy is not trustworthy."""

    issue_number: int
    session_name: str
    reason: str

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("UnsupportedSessionRun.issue_number must be positive")
        if type(self.session_name) is not str or not self.session_name:
            raise ValueError("UnsupportedSessionRun.session_name must not be empty")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("UnsupportedSessionRun.reason must not be empty")
