"""Agent configuration validator."""

from pathlib import Path
import shlex
from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


def _custom_command_executable(agent, has_custom_command: bool) -> str:
    if not has_custom_command or agent.provider is not None:
        return ""
    if not isinstance(agent.command, str):
        return ""
    try:
        tokens = shlex.split(agent.command)
    except (IndexError, TypeError, ValueError):
        return ""
    while tokens:
        token = tokens.pop(0)
        if "=" in token and not token.startswith(("/", "./")):
            continue
        executable = Path(token).name
        if executable in {"command", "env"}:
            while tokens and (tokens[0] == "--" or tokens[0].startswith("-")):
                tokens.pop(0)
            continue
        return executable
    return ""


class AgentValidator(ConfigValidator):
    """Validates agent-related configuration.

    Checks:
    - At least one agent is configured
    - Prompt files exist
    - Providers are valid
    - Models are known (for claude-code provider)
    - Per-agent reviewers reference valid agents
    - Default agent provider is valid
    """

    KNOWN_CLAUDE_MODELS = {"haiku", "sonnet", "opus", "fable"}

    def validate(self, config: "Config") -> list[str]:
        from issue_orchestrator.agent_runner import is_valid_provider, list_providers

        errors = []

        # Must have at least one agent
        if not config.agents:
            errors.append(
                "No agents configured. Add at least one agent under 'agents:' in config."
            )

        # Validate default_agent.provider if set
        if config.default_agent and config.default_agent.provider is not None:
            if not is_valid_provider(config.default_agent.provider):
                errors.append(
                    f"default_agent.provider '{config.default_agent.provider}' is not valid. "
                    f"Available: {list_providers()}"
                )

        for label, agent in config.agents.items():
            errors.extend(
                self._validate_agent(
                    config, label, agent, list_providers, is_valid_provider
                )
            )

        return errors

    def _validate_agent(
        self,
        config: "Config",
        label: str,
        agent,  # AgentConfig - can't use type hint due to nested class
        list_providers,
        is_valid_provider,
    ) -> list[str]:
        """Validate a single agent configuration."""
        errors = []

        # Prompt file must exist
        if not agent.prompt_path.exists():
            errors.append(
                f"Agent '{label}': prompt file not found: {agent.prompt_path}"
            )

        # Provider must be set (from agent or default_agent) or command overridden
        has_custom_command = "command" in config.raw_agents.get(label, {})
        if agent.provider is None and not has_custom_command:
            errors.append(
                f"Agent '{label}': no provider specified and no default_agent.provider set. "
                f"Either set 'provider' on the agent, set 'default_agent.provider', "
                f"or use 'command' to specify a custom command. "
                f"Available providers: {list_providers()}"
            )
        elif agent.provider is not None and not is_valid_provider(agent.provider):
            errors.append(
                f"Agent '{label}': unknown provider '{agent.provider}'. "
                f"Available: {list_providers()}"
            )

        custom_codex = (
            agent.provider is None
            and has_custom_command
            and (
                agent.ai_system == "codex"
                or getattr(agent, "meta_agent", None) == "codex"
                or _custom_command_executable(agent, has_custom_command) == "codex"
            )
        )
        if custom_codex:
            errors.append(
                f"Agent '{label}': custom Codex commands cannot receive the "
                "orchestrator's invocation-scoped guardrail. Set "
                "'provider: codex' and move CLI options to provider_args."
            )

        # sandbox: true is a security opt-in enforceable only through a provider
        # adapter. A provider-less / custom-`command` agent launches an arbitrary
        # command the orchestrator cannot sandbox, so reject the opt-in rather than
        # let it silently launch unsandboxed (ADR-0034).
        if getattr(agent, "sandbox", False) and agent.provider is None:
            errors.append(
                f"Agent '{label}': sandbox: true is not supported for a "
                f"provider-less / custom-command agent (the launcher cannot enforce "
                f"the sandbox). Set 'provider: claude-code', or set sandbox: false."
            )

        # Model validation for claude-code provider
        if (
            agent.provider in (None, "claude-code")
            and agent.model not in self.KNOWN_CLAUDE_MODELS
        ):
            errors.append(
                f"Agent '{label}': unknown model '{agent.model}'. Known: {self.KNOWN_CLAUDE_MODELS}"
            )

        # Per-agent reviewer must reference valid agent
        if agent.reviewer and agent.reviewer not in config.agents:
            errors.append(
                f"Agent '{label}': reviewer '{agent.reviewer}' not found in agents. "
                f"Available: {list(config.agents.keys())}"
            )

        if not agent.ai_system:
            errors.append(
                f"Agent '{label}': ai_system is required. "
                "Set ai_system to a value from ai_systems.yaml (e.g., 'claude-code')."
            )
        else:
            from ..ai_systems_config import get_ai_systems_config

            systems = get_ai_systems_config(config.repo_root)
            allowed = set(systems.systems.keys()) | set(config.ai_systems_allowed)
            if agent.ai_system not in allowed:
                errors.append(
                    f"Agent '{label}': ai_system '{agent.ai_system}' is not recognized. "
                    "Add it to ai_systems.allowed or ai_systems.yaml."
                )

        return errors
