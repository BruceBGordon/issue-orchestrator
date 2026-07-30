"""Claude Code provider implementation.

Builds command-line invocations for Anthropic's Claude Code CLI.

Previously in ``_vendor/agent_runner/providers/claude.py``.
"""

from typing import TYPE_CHECKING

from .base import CLIProvider

if TYPE_CHECKING:
    from issue_orchestrator.domain.sandbox_scope import SandboxScope


class ClaudeCodeProvider(CLIProvider):
    """Provider for Anthropic's Claude Code CLI.

    Runs Claude Code as an interactive TUI session. The initial prompt
    is passed as a positional argument (not ``-p``), which starts the TUI
    and immediately begins working while still showing the full interactive
    output. Follow-up prompts can be delivered via PTY stdin.
    """

    # Model name mappings (short names to full IDs if needed)
    MODEL_ALIASES: dict[str, str] = {
        "haiku": "haiku",
        "sonnet": "sonnet",
        "opus": "opus",
    }
    EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def executable(self) -> str:
        return "claude"

    @property
    def description(self) -> str:
        return "Anthropic Claude Code CLI"

    @property
    def interactive(self) -> bool:
        return True

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        *,
        sandbox_scope: "SandboxScope | None" = None,
        **kwargs: str,
    ) -> list[str]:
        """Build a Claude Code CLI command for interactive mode.

        The prompt is passed as a positional argument (without ``-p``),
        which starts the interactive TUI and immediately begins working.

        Args:
            prompt: The task to perform (passed as positional arg)
            model: Model name (haiku, sonnet, opus, or full model ID). None for default.
            sandbox_scope: When set, replaces the default ``bypassPermissions``
                (yolo) launch with a bounded OS sandbox — ``--permission-mode
                dontAsk`` plus inline ``--settings`` describing the read/write
                roots, egress, and denied credentials. ``None`` (default) keeps
                the existing command byte-for-byte.
            **kwargs: Additional options:
                - permission_mode: Permission handling mode (default: bypassPermissions).
                  Ignored when ``sandbox_scope`` is set (``dontAsk`` is forced).
                - effort: Claude effort level (low, medium, high, xhigh, max)
                - reasoning_effort: Alias for effort
                - system_prompt: Additional system prompt text
                - max_turns: Maximum conversation turns
        """
        cmd = [self.executable]

        # Model (optional - Claude will use default if not specified)
        if model:
            resolved_model = self.MODEL_ALIASES.get(model, model)
            cmd.extend(["--model", resolved_model])

        effort = self._resolve_effort(kwargs)
        if effort:
            cmd.extend(["--effort", effort])

        if sandbox_scope is not None:
            # Bounded OS sandbox: dontAsk + inline --settings. Replaces the
            # default bypassPermissions (yolo) permission-mode flag.
            cmd.extend(self.apply_scope(sandbox_scope))
        else:
            # Permission mode (default to bypassPermissions for automation)
            permission_mode = kwargs.get("permission_mode", "bypassPermissions")
            cmd.extend(["--permission-mode", permission_mode])

        # Optional system prompt
        system_prompt = kwargs.get("system_prompt")
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        # Optional max turns
        max_turns = kwargs.get("max_turns")
        if max_turns:
            cmd.extend(["--max-turns", str(max_turns)])

        # Disable MCP servers — worktree .mcp.json can contain configs
        # (e.g. Playwright) that hang in automated/headless contexts.
        cmd.extend(["--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"])

        # Verbose mode (more detailed TUI output)
        verbose = kwargs.get("verbose")
        if verbose and str(verbose).lower() not in ("false", "0", "no", ""):
            cmd.append("--verbose")

        # Initial prompt as positional argument — starts TUI working immediately
        # without -p flag, so full interactive output is preserved.
        if prompt:
            cmd.append(prompt)

        return cmd

    def apply_scope(self, scope: "SandboxScope") -> list[str]:
        """Translate a :class:`SandboxScope` into claude-code sandbox argv.

        Under the trusted-repository contract (ADR-0034) the target repo's
        configuration is trusted policy, so no settings-source guard is needed:
        we translate the scope directly into Claude's native OS sandbox
        (``--permission-mode dontAsk`` + inline ``--settings``). The boundary the
        adapter still enforces is against the *agent* — worktree-scoped writes,
        denied secrets/egress, and denied self-modification of the policy files.
        """
        from .sandbox import ClaudeSandboxAdapter

        return ClaudeSandboxAdapter().apply_scope(scope)

    @classmethod
    def _resolve_effort(cls, kwargs: dict[str, str]) -> str | None:
        effort = cls._normalize_effort(kwargs.get("effort"))
        reasoning_effort = cls._normalize_effort(kwargs.get("reasoning_effort"))
        if effort and reasoning_effort and effort != reasoning_effort:
            raise ValueError(
                "Claude effort and reasoning_effort must match when both are set"
            )
        normalized = effort or reasoning_effort
        if normalized is None:
            return None
        if normalized not in cls.EFFORT_LEVELS:
            allowed = ", ".join(cls.EFFORT_LEVELS)
            raise ValueError(
                f"Claude effort must be one of {allowed}; got {normalized!r}"
            )
        return normalized

    @staticmethod
    def _normalize_effort(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        return normalized or None
