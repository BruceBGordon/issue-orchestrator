"""Repository setup command policy shared by Control Center adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TECH_LEAD_AGENT_LABEL = "agent:tech-lead"
TECH_LEAD_PROMPT_PATH = ".io/tech-lead.md"
WORKER_PROMPT_PATH = ".io/dev.md"

_SUPPORTED_MODELS = frozenset({"haiku", "sonnet", "opus"})


@dataclass(frozen=True)
class RepositorySetupCommand:
    """Typed choices required to build a runnable repository configuration."""

    repo_name: str
    worker_agent_label: str
    model: str
    configure_tech_lead: bool = True

    def __post_init__(self) -> None:
        if not self.repo_name.strip():
            raise ValueError("repo_name is required")
        if not self.worker_agent_label.startswith("agent:"):
            raise ValueError("worker_agent_label must start with 'agent:'")
        if self.worker_agent_label == TECH_LEAD_AGENT_LABEL:
            raise ValueError("worker_agent_label must identify a worker, not the tech lead")
        if self.model not in _SUPPORTED_MODELS:
            raise ValueError(
                f"model must be one of {sorted(_SUPPORTED_MODELS)}, got {self.model!r}"
            )

    def build_config(self) -> dict[str, Any]:
        """Build the canonical setup config without touching external systems."""
        agents: dict[str, dict[str, Any]] = {
            self.worker_agent_label: {
                "prompt": WORKER_PROMPT_PATH,
                "provider": "claude-code",
                "model": self.model,
                "ai_system": "claude-code",
            }
        }
        config: dict[str, Any] = {
            "repo": {"name": self.repo_name},
            "agents": agents,
        }

        if self.configure_tech_lead:
            agents[TECH_LEAD_AGENT_LABEL] = {
                "prompt": TECH_LEAD_PROMPT_PATH,
                "provider": "claude-code",
                "model": self.model,
                "ai_system": "claude-code",
            }
            config["review"] = {
                "tech_lead_review_agent": TECH_LEAD_AGENT_LABEL,
                "tech_lead_follow_up_agent": self.worker_agent_label,
                "tech_lead_review_label": "needs-tech-lead-review",
                "tech_lead_reviewed_label": "tech-lead-reviewed",
                "tech_lead_failed_label": "tech-lead-failed",
                "tech_lead_review_threshold": 0,
                "tech_lead_review_on_failure": True,
            }

        return config


__all__ = [
    "RepositorySetupCommand",
    "TECH_LEAD_AGENT_LABEL",
    "TECH_LEAD_PROMPT_PATH",
    "WORKER_PROMPT_PATH",
]
