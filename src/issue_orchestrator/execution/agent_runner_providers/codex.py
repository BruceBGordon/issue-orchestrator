"""OpenAI Codex provider implementation.

Builds command-line invocations for OpenAI's Codex CLI.

Previously in ``_vendor/agent_runner/providers/codex.py``.
"""

from collections.abc import Mapping
import json
from pathlib import Path
from typing import TYPE_CHECKING

from issue_orchestrator.ports.provider_readiness import ProviderReadiness
from issue_orchestrator.ports.provider_resilience import ProviderErrorType
from issue_orchestrator.infra.hooks.codex_session import (
    build_codex_session_hook_argv,
    prepare_codex_runtime_home,
)

from .base import CLIProvider

if TYPE_CHECKING:
    from issue_orchestrator.domain.sandbox_scope import SandboxScope
    from issue_orchestrator.ports.command_runner import CommandRunner


def _codex_project_trust_paths(working_directory: Path) -> tuple[Path, ...]:
    """Return Codex's possible trust keys for a checkout or linked worktree."""
    worktree = working_directory.resolve()
    paths = [worktree]
    git_marker = worktree / ".git"
    try:
        marker = git_marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return tuple(paths)
    prefix = "gitdir:"
    if not marker.lower().startswith(prefix):
        return tuple(paths)
    git_dir = Path(marker[len(prefix) :].strip()).expanduser()
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    else:
        git_dir = git_dir.resolve()
    if git_dir.parent.parent.name == ".git":
        primary = git_dir.parent.parent.parent
        if primary != worktree:
            paths.append(primary)
    return tuple(paths)


def _validate_orchestrated_safety(
    *,
    sandbox_scope: "SandboxScope | None",
    approval_mode: str,
    sandbox_mode: str | None,
) -> None:
    if sandbox_scope is None and approval_mode == "yolo":
        raise ValueError(
            "Orchestrated Codex sessions cannot use approval_mode=yolo; "
            "it would expose the isolated hook runtime to agent writes"
        )
    if sandbox_scope is None and sandbox_mode == "danger-full-access":
        raise ValueError(
            "Orchestrated Codex sessions without an enforcing sandbox scope "
            "cannot use sandbox=danger-full-access; it would expose the "
            "isolated hook runtime to agent writes"
        )


class CodexProvider(CLIProvider):
    """Provider for OpenAI's Codex CLI.

    Builds command-line invocations for Codex. Codex defaults to the
    interactive TUI so persistent review-exchange sessions can keep one live
    process and receive follow-up prompts over the PTY. Callers that need the
    one-shot automation surface can pass ``execution_mode="exec"``.
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

    @property
    def interactive(self) -> bool:
        return True

    # ``codex login status`` reads local credential state only — no API call,
    # no tokens spent — so it is affordable on every launch (#6999).
    AUTH_STATUS_ARGV = ("login", "status")
    _LOGGED_IN_MARKER = "logged in using"

    def check_readiness(self, runner: "CommandRunner") -> ProviderReadiness:
        """Probe Codex's local credential state without spawning a TUI."""
        if not self.is_available():
            return ProviderReadiness.not_installed(
                self.name, f"{self.executable} not found in PATH"
            )
        output, exit_code, timed_out = self._run_auth_probe(
            runner, [self.executable, *self.AUTH_STATUS_ARGV]
        )
        if timed_out:
            return ProviderReadiness.unknown(
                self.name,
                f"`{self.executable} login status` timed out after "
                f"{self.AUTH_PROBE_TIMEOUT_SECONDS}s",
            )
        # Auth classification first: "not logged in" also contains the
        # logged-in marker's substring, so a positive match must never win.
        if self.classify_output(output) is ProviderErrorType.AUTH:
            return ProviderReadiness.auth_expired(
                self.name,
                f"{self.executable} login status reports not logged in — "
                "run `codex login`",
            )
        if exit_code == 0 and self._LOGGED_IN_MARKER in output.lower():
            return ProviderReadiness.ready(
                self.name, f"{self.executable} login status: logged in"
            )
        return ProviderReadiness.unknown(
            self.name,
            f"`{self.executable} login status` gave no verdict (exit={exit_code})",
        )

    def runs_interactively(self, **kwargs: object) -> bool:
        return self._execution_mode(kwargs) == "interactive"

    def needs_fresh_prompt_process(self, **kwargs: object) -> bool:
        return self.runs_interactively(**kwargs)

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        *,
        sandbox_scope: "SandboxScope | None" = None,
        working_directory: Path | None = None,
        **kwargs: str,
    ) -> list[str]:
        """Build a Codex CLI command.

        Args:
            prompt: The task to perform
            model: Model name (e.g., gpt-5.3-codex). If None, uses Codex's default.
            sandbox_scope: When set, replaces provider-level approval/sandbox
                options with the orchestrator-computed Codex permission profile.
            **kwargs: Additional options:
                - execution_mode: "interactive" (default) or "exec"
                - approval_mode: "full-auto" (default), "yolo", or "default"
                - sandbox: Sandbox policy (read-only, workspace-write, danger-full-access)
                - reasoning_effort: Codex reasoning effort (low, medium, high, xhigh)
                - model_reasoning_effort: Alias for reasoning_effort
                - json_output: Emit ``--json`` (codex's structured event stream)
                  instead of the default terminal UI. Defaults to **False**.

                  Most production paths (persistent-session review-exchange,
                  one-shot agent runs) hand off via a response file or HTTP
                  callback — nothing in this codebase parses codex stdout
                  for protocol data. The PTY-backed terminal-recording
                  pipeline is the consumer that matters, and it captures
                  the agent's terminal UI for replay in the timeline
                  viewer. With ``--json`` set, the recording becomes a raw
                  JSONL stream that the terminal renderer concatenates as
                  unstyled text — exactly what the user saw on tixmeup
                  #362's reviewer log. Defaulting off makes the recording
                  match what a human running Codex in a terminal would see.
                  Automation that genuinely wants the JSON event stream can
                  opt in with ``execution_mode="exec"`` plus
                  ``json_output="true"``.
        """
        execution_mode = self._execution_mode(kwargs)
        json_output = self._truthy(kwargs.get("json_output", "false"))
        if execution_mode == "interactive" and json_output:
            raise ValueError("Codex json_output requires execution_mode='exec'")

        project_directory = working_directory or (
            sandbox_scope.working_directory if sandbox_scope is not None else None
        )
        if project_directory is None:
            raise ValueError(
                "Orchestrated Codex sessions require an explicit working_directory "
                "or sandbox scope to protect the hook policy"
            )

        approval_mode = kwargs.get("approval_mode", "full-auto")
        _validate_orchestrated_safety(
            sandbox_scope=sandbox_scope,
            approval_mode=approval_mode,
            sandbox_mode=kwargs.get("sandbox"),
        )

        scope_argv = (
            self.apply_scope(sandbox_scope) if sandbox_scope is not None else []
        )

        session_hook_argv = build_codex_session_hook_argv(
            write_roots=(
                (sandbox_scope.working_directory, *sandbox_scope.write_roots)
                if sandbox_scope is not None
                else (project_directory,)
            )
        )
        project_trust_argv = []
        project_paths = _codex_project_trust_paths(project_directory)
        for project_path in project_paths:
            project_trust_argv.extend(
                [
                    "-c",
                    "projects."
                    f"{json.dumps(str(project_path), ensure_ascii=False)}"
                    '.trust_level="untrusted"',
                ]
            )
        runtime_home = prepare_codex_runtime_home(untrusted_projects=project_paths)
        prefix = ["env", f"CODEX_HOME={runtime_home}"]
        cmd = [
            *prefix,
            self.executable,
            *session_hook_argv,
            *project_trust_argv,
            *scope_argv,
        ]
        if sandbox_scope is None:
            self._append_approval_flags(
                cmd,
                approval_mode=approval_mode,
                execution_mode=execution_mode,
            )

        if execution_mode == "exec":
            cmd.append("exec")

        # Model (optional - Codex will use default if not specified)
        if model:
            cmd.extend(["--model", model])

        self._append_reasoning_effort(cmd, kwargs)

        if sandbox_scope is None:
            self._append_sandbox_flags(
                cmd,
                kwargs,
                approval_mode=approval_mode,
            )

        if json_output:
            cmd.append("--json")

        # The prompt itself
        cmd.append(prompt)

        return cmd

    def apply_scope(self, scope: "SandboxScope") -> list[str]:
        """Translate *scope* into Codex's enforcing global argv fragment."""
        from .sandbox import CodexSandboxAdapter

        return CodexSandboxAdapter().apply_scope(scope)

    @staticmethod
    def _append_approval_flags(
        cmd: list[str],
        *,
        approval_mode: str,
        execution_mode: str,
    ) -> None:
        if approval_mode == "yolo":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif approval_mode == "full-auto":
            if execution_mode == "exec":
                cmd.extend(["--ask-for-approval", "on-request"])
            else:
                cmd.extend(["--ask-for-approval", "never"])

    @staticmethod
    def _append_reasoning_effort(
        cmd: list[str],
        kwargs: Mapping[str, object],
    ) -> None:
        reasoning_effort = kwargs.get("reasoning_effort")
        if reasoning_effort is None:
            reasoning_effort = kwargs.get("model_reasoning_effort")
        if reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])

    @staticmethod
    def _append_sandbox_flags(
        cmd: list[str],
        kwargs: Mapping[str, object],
        *,
        approval_mode: str,
    ) -> None:
        sandbox = kwargs.get("sandbox")
        if sandbox is None and approval_mode == "full-auto":
            sandbox = "workspace-write"
        if sandbox and approval_mode != "yolo":
            cmd.extend(["--sandbox", str(sandbox)])

    @staticmethod
    def _execution_mode(kwargs: Mapping[str, object]) -> str:
        raw = str(kwargs.get("execution_mode", "interactive")).strip().lower()
        if raw in {"interactive", "tui"}:
            return "interactive"
        if raw in {"exec", "non-interactive", "noninteractive"}:
            return "exec"
        raise ValueError(
            f"Codex execution_mode must be 'interactive' or 'exec' (got {raw!r})"
        )

    @staticmethod
    def _truthy(value: object) -> bool:
        return str(value).strip().lower() == "true"
