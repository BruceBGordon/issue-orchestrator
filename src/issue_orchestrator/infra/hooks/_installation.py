"""Shared hook installation orchestration."""

from pathlib import Path

from ..ai_gate_state import invalidate_ai_gate_state
from ._types import AiAgentAdapter, AiAgentType

SHARED_POLICY_RELATIVE_PATH = Path("scripts/agent-hooks/block_no_verify.py")
SHARED_POLICY_MANAGED_MARKER = (
    "Managed by issue-orchestrator setup-guardrails: block-no-verify helper"
)


def install_shared_hook_policy(project_root: Path) -> Path:
    """Install the canonical policy consumed by all generated agent hooks."""
    target = project_root / SHARED_POLICY_RELATIVE_PATH
    source = Path(__file__).with_name("block_no_verify.py")
    rendered = (
        "#!/usr/bin/env python3\n"
        f"# {SHARED_POLICY_MANAGED_MARKER}\n\n"
        f"{source.read_text(encoding='utf-8')}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_text(encoding="utf-8") != rendered:
        target.write_text(rendered, encoding="utf-8")
    target.chmod(0o755)
    return target


def _adapters_for_config(config) -> dict[AiAgentType, AiAgentAdapter]:
    """Build each configured adapter once without a module import cycle."""
    from .hooks import detect_agents_from_config, get_adapter

    return {
        agent_type: get_adapter(agent_type)
        for agent_type in set(detect_agents_from_config(config).values())
    }


def validate_hook_installation_targets(config, project_root: Path) -> None:
    """Validate all agent registrations before a wider setup transaction."""
    for adapter in _adapters_for_config(config).values():
        adapter.validate_installation_target(project_root)


def install_hooks_for_config(
    config, project_root: Path
) -> dict[AiAgentType, list[Path]]:
    """Install hooks for every unique AI agent in a configuration."""
    adapters = _adapters_for_config(config)
    for adapter in adapters.values():
        adapter.validate_installation_target(project_root)
    invalidate_ai_gate_state(project_root)
    install_shared_hook_policy(project_root)

    results = {}
    for agent_type, adapter in adapters.items():
        results[agent_type] = adapter.install_hooks(project_root)
    return results


__all__ = [
    "install_hooks_for_config",
    "install_shared_hook_policy",
    "validate_hook_installation_targets",
]
