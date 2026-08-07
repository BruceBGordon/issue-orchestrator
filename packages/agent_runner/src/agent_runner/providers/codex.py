"""OpenAI Codex provider implementation.

This module provides the CodexProvider class for building
Codex CLI invocations.

Codex CLI flags (for `codex exec` subcommand):
- exec: Run non-interactively
- --model, -m: Model to use (e.g., gpt-5-codex)
- --ask-for-approval on-request + --sandbox workspace-write: Low-friction mode
- --dangerously-bypass-approvals-and-sandbox, --yolo: No approvals, no sandbox
- --json: Output newline-delimited JSON events
- --sandbox, -s: Sandbox policy (read-only, workspace-write, danger-full-access)
"""

from .base import CLIProvider


class CodexProvider(CLIProvider):
    """Provider for OpenAI's Codex CLI.

    Builds command-line invocations for Codex with appropriate flags
    for non-interactive, automated execution.

    Example:
        provider = CodexProvider()
        cmd = provider.build_command(
            prompt="Fix the bug in auth.py",
            model="gpt-5-codex",
        )
        # Returns: ["codex", "--ask-for-approval", "on-request", "exec",
        #           "--model", "gpt-5-codex", "--sandbox", "workspace-write",
        #           "--json", "Fix the bug in auth.py"]
    """

    @property
    def name(self) -> str:
        return "codex"

    @property
    def executable(self) -> str:
        return "codex"

    @property
    def description(self) -> str:
        return "OpenAI Codex CLI"

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        **kwargs: str,
    ) -> list[str]:
        """Build a Codex CLI command.

        Args:
            prompt: The task to perform
            model: Model name (e.g., o3). If None, uses Codex's default.
            **kwargs: Additional options:
                - approval_mode: "full-auto" (default), "yolo", or "default"
                - sandbox: Sandbox policy (read-only, workspace-write, danger-full-access)
                - json_output: Whether to emit JSON events (default: True)

        Returns:
            Command as argv list
        """
        cmd = [self.executable]

        # Approval policy is a global option and must precede the exec subcommand.
        approval_mode = kwargs.get("approval_mode", "full-auto")
        if approval_mode == "yolo":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif approval_mode == "full-auto":
            cmd.extend(["--ask-for-approval", "on-request"])
        # else: default mode, no flag needed

        # Use exec subcommand for non-interactive execution
        cmd.append("exec")

        # Model (optional - Codex will use default if not specified)
        if model:
            cmd.extend(["--model", model])

        # Sandbox policy (only if not using yolo which disables sandbox)
        sandbox = kwargs.get("sandbox")
        if sandbox is None and approval_mode == "full-auto":
            sandbox = "workspace-write"
        if sandbox and approval_mode != "yolo":
            cmd.extend(["--sandbox", sandbox])

        # JSON output for structured events (default: True for automation)
        json_output = kwargs.get("json_output", "true").lower() == "true"
        if json_output:
            cmd.append("--json")

        # The prompt itself
        cmd.append(prompt)

        return cmd
