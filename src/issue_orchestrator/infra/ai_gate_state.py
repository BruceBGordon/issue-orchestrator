"""AI gate state persistence for hook verification.

Tracks when AI gate tests were last run and their results to avoid
running expensive checks on every startup.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Location within the orchestrator state directory
AI_GATE_STATE_FILE = ".issue-orchestrator/ai-gate-state.json"


@dataclass
class AiGateResult:
    """Result of a single agent's AI gate test."""

    success: bool
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AiGateState:
    """Persisted state for AI gate tests.

    Tracks when the last AI gate test was performed and results
    for each agent type that was tested.
    """

    last_check: datetime | None = None
    last_results: dict[str, AiGateResult] = field(default_factory=dict)
    required_agent_types: tuple[str, ...] = ()

    @property
    def recorded_agent_types(self) -> tuple[str, ...]:
        """Return the provider set, including caches written before it was explicit."""
        if self.required_agent_types:
            return self.required_agent_types
        return tuple(sorted(self.last_results))

    def is_stale(
        self,
        interval_days: int,
        required_agent_types: set[str] | None = None,
    ) -> bool:
        """Check if AI gate test needs to be run.

        Args:
            interval_days: Maximum days between checks. 0 means disabled.

        Returns:
            True if check should be run (first run or interval exceeded).
        """
        if interval_days <= 0:
            return False  # Disabled
        recorded_types = self.recorded_agent_types
        if (
            required_agent_types is not None
            and recorded_types != tuple(sorted(required_agent_types))
        ):
            return True
        if self.last_check is None:
            return True  # First run
        elapsed = datetime.now(timezone.utc) - self.last_check
        return elapsed.days >= interval_days

    def mark_checked(
        self,
        results: dict[str, tuple[bool, str]],
        required_agent_types: set[str] | None = None,
    ) -> None:
        """Update state after running AI gate tests.

        Args:
            results: Dict mapping agent_type to (success, message) tuple.
        """
        now = datetime.now(timezone.utc)
        self.last_check = now
        self.required_agent_types = tuple(sorted(required_agent_types or results))
        self.last_results = {
            agent_type: AiGateResult(
                success=success,
                message=message,
                timestamp=now,
            )
            for agent_type, (success, message) in results.items()
        }

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_results": {
                agent_type: {
                    "success": result.success,
                    "message": result.message,
                    "timestamp": result.timestamp.isoformat(),
                }
                for agent_type, result in self.last_results.items()
            },
            "required_agent_types": list(self.required_agent_types),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AiGateState":
        """Create from dict loaded from JSON.

        Raises:
            KeyError, TypeError, ValueError: If data structure is invalid.
        """
        last_check = None
        if data.get("last_check"):
            last_check = datetime.fromisoformat(data["last_check"])

        last_results = {}
        for agent_type, result_data in data.get("last_results", {}).items():
            # Validate result_data is a dict with required keys
            if not isinstance(result_data, dict):
                raise TypeError(
                    f"Expected dict for result_data, got {type(result_data)}"
                )
            timestamp = datetime.fromisoformat(result_data["timestamp"])
            last_results[agent_type] = AiGateResult(
                success=result_data["success"],
                message=result_data["message"],
                timestamp=timestamp,
            )

        raw_required = data.get("required_agent_types", [])
        if not isinstance(raw_required, list) or not all(
            isinstance(item, str) for item in raw_required
        ):
            raise TypeError("required_agent_types must be a list of strings")

        return cls(
            last_check=last_check,
            last_results=last_results,
            required_agent_types=tuple(sorted(raw_required)),
        )


def get_ai_gate_state_path(repo_root: Path) -> Path:
    """Get the path to the AI gate state file."""
    return repo_root / AI_GATE_STATE_FILE


def load_ai_gate_state(repo_root: Path) -> AiGateState:
    """Load AI gate state from disk.

    Args:
        repo_root: Root of the repository.

    Returns:
        AiGateState instance (empty defaults if file doesn't exist).
    """
    state_path = get_ai_gate_state_path(repo_root)
    if not state_path.exists():
        return AiGateState()

    try:
        with open(state_path) as f:
            data = json.load(f)
        return AiGateState.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        logger.warning("Failed to load AI gate state from %s: %s", state_path, e)
        return AiGateState()


def save_ai_gate_state(repo_root: Path, state: AiGateState) -> None:
    """Save AI gate state to disk.

    Args:
        repo_root: Root of the repository.
        state: AiGateState to persist.
    """
    state_path = get_ai_gate_state_path(repo_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with open(state_path, "w") as f:
        json.dump(state.to_dict(), f, indent=2)
