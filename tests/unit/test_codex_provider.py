"""Unit tests for ``CodexProvider.build_command``.

Sandbox-clean: no subprocess, no network. Asserts on the assembled
argv only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.sandbox_scope import SandboxUnsupportedError
from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider


def _cmd(**kwargs: str) -> list[str]:
    """Build the argv with sane defaults; return the list for assertions."""
    return CodexProvider().build_command(prompt="task", **kwargs)


class TestCodexJsonOutputDefault:
    """The default for ``json_output`` is **off** so the PTY recording
    captures codex's terminal UI (what a human sees at the terminal),
    not its structured JSONL event stream.

    Regression guard for tixmeup #362's reviewer-timeline content: with
    ``--json`` set, the recording is a stream of
    ``{"type":"thread.started"}`` / ``{"type":"item.completed"}`` /
    etc., which the timeline viewer's terminal renderer concatenates
    as raw, unstyled text. With ``--json`` unset, codex emits its
    normal terminal UI (formatted, ANSI-coloured) and the renderer
    plays it back faithfully.

    Nothing in this codebase parses codex stdout for protocol data —
    persistent-session review-exchange uses a response file, one-shot
    runs use ``coding-done`` callbacks. So the JSON event stream
    has no production consumer; defaulting it on was pure waste plus
    a viewer-rendering bug. Keep this test tight: any path that flips
    the default back without an explicit caller opt-in re-introduces
    the empty-looking timeline symptom.
    """

    def test_default_does_not_pass_json_flag(self) -> None:
        cmd = _cmd()
        assert "--json" not in cmd, (
            f"codex default invocation must NOT pass --json (recording "
            f"would become unstyled JSONL); got argv={cmd}"
        )

    def test_explicit_false_does_not_pass_json_flag(self) -> None:
        cmd = _cmd(json_output="false")
        assert "--json" not in cmd

    def test_explicit_true_does_pass_json_flag(self) -> None:
        """The opt-in path is still wired — automation that genuinely
        wants codex's JSONL events can request them via
        ``provider_args: {execution_mode: "exec", json_output: "true"}``
        per agent."""
        cmd = _cmd(execution_mode="exec", json_output="true")
        assert "--json" in cmd

    def test_json_output_requires_exec_mode(self) -> None:
        with pytest.raises(ValueError, match="json_output requires"):
            _cmd(json_output="true")

    @pytest.mark.parametrize("yes_value", ["TRUE", "True", "tRuE"])
    def test_truthy_string_case_insensitive_passes_json_flag(
        self,
        yes_value: str,
    ) -> None:
        """Match historical behavior: ``json_output`` is parsed
        case-insensitively. Locks the contract so a downstream caller
        passing ``"True"`` keeps working."""
        cmd = _cmd(execution_mode="exec", json_output=yes_value)
        assert "--json" in cmd

    @pytest.mark.parametrize("no_value", ["", "0", "no", "off", "False"])
    def test_falsey_strings_do_not_pass_json_flag(
        self,
        no_value: str,
    ) -> None:
        """Any non-``true`` (case-insensitive) string is treated as
        opt-out. This matches the original parser shape and keeps the
        default safe even when someone passes a typo."""
        cmd = _cmd(json_output=no_value)
        assert "--json" not in cmd


class TestCodexBaseCommand:
    """Sanity-check the rest of the argv shape so a refactor that
    moves the ``--json`` decision around doesn't accidentally drop
    other flags. These aren't exhaustive — just enough to catch a
    structural regression."""

    @pytest.fixture(autouse=True)
    def isolated_runtime(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from issue_orchestrator.infra.hooks.codex_session import (
            prepare_codex_runtime_home,
        )

        user_home = tmp_path / ".test-codex-user"
        user_home.mkdir()
        (user_home / "auth.json").write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(user_home))
        monkeypatch.setenv(
            "ISSUE_ORCHESTRATOR_CODEX_RUNTIME_ROOT",
            str(tmp_path / ".test-codex-runtime"),
        )
        prepare_codex_runtime_home()

    def test_default_starts_interactive_codex(self) -> None:
        cmd = _cmd()
        assert cmd[0] == "codex"
        assert "exec" not in cmd[:2]

    def test_default_injects_invocation_scoped_guardrail(self) -> None:
        cmd = _cmd()
        assert "features.hooks=true" in cmd
        assert "features.plugins=false" in cmd
        assert "features.plugin_sharing=false" in cmd
        assert "features.remote_plugin=false" in cmd
        hook = next(arg for arg in cmd if arg.startswith("hooks.PreToolUse="))
        assert "block_no_verify.py --mode codex" in hook
        assert "--dangerously-bypass-hook-trust" in cmd

    def test_no_scope_rejects_guardrail_runtime_inside_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from issue_orchestrator.infra.hooks import codex_session

        policy = tmp_path / "block_no_verify.py"
        policy.write_text("pass\n")
        monkeypatch.setattr(codex_session.block_no_verify, "__file__", str(policy))

        with pytest.raises(SandboxUnsupportedError, match="outside agent write roots"):
            CodexProvider().build_command(prompt="task", working_directory=tmp_path)

    def test_orchestrated_command_uses_isolated_codex_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ISSUE_ORCHESTRATOR_CODEX_RUNTIME_ROOT", str(tmp_path / "runtime")
        )
        cmd = CodexProvider().build_command(
            prompt="task", working_directory=tmp_path / "worktree"
        )

        assert cmd[0] == "env"
        assert cmd[1].startswith("CODEX_HOME=")
        assert cmd[2] == "codex"
        assert (
            f'projects."{(tmp_path / "worktree").resolve()}".trust_level="untrusted"'
            in cmd
        )

    def test_linked_worktree_also_marks_primary_checkout_untrusted(
        self, tmp_path: Path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "reviewer"
        git_dir = primary / ".git" / "worktrees" / "reviewer"
        git_dir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

        cmd = CodexProvider().build_command(
            prompt="task",
            working_directory=worktree,
        )

        assert f'projects."{worktree}".trust_level="untrusted"' in cmd
        assert f'projects."{primary}".trust_level="untrusted"' in cmd

    def test_orchestrated_command_rejects_yolo(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot use approval_mode=yolo"):
            CodexProvider().build_command(
                prompt="task",
                working_directory=tmp_path,
                approval_mode="yolo",
            )

    def test_exec_mode_uses_codex_exec(self) -> None:
        cmd = _cmd(execution_mode="exec")
        assert cmd[0] == "codex"
        assert "exec" in cmd

    def test_full_auto_default(self) -> None:
        cmd = _cmd()
        assert "--ask-for-approval" in cmd
        assert "never" in cmd
        assert "--sandbox" in cmd
        assert "workspace-write" in cmd
        assert "--full-auto" not in cmd

    def test_exec_mode_uses_supported_full_auto_equivalent(self) -> None:
        cmd = _cmd(execution_mode="exec")
        assert "--full-auto" not in cmd
        assert cmd[cmd.index("--ask-for-approval") + 1] == "on-request"
        assert cmd.index("--ask-for-approval") < cmd.index("exec")
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"

    def test_yolo_swaps_to_dangerously_bypass(self) -> None:
        cmd = _cmd(approval_mode="yolo")
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--full-auto" not in cmd

    def test_exec_yolo_flag_precedes_subcommand(self) -> None:
        cmd = _cmd(execution_mode="exec", approval_mode="yolo")
        assert cmd.index("--dangerously-bypass-approvals-and-sandbox") < cmd.index(
            "exec"
        )

    def test_prompt_is_last_arg(self) -> None:
        cmd = CodexProvider().build_command(prompt="hello world")
        assert cmd[-1] == "hello world"

    def test_provider_is_interactive_by_default(self) -> None:
        provider = CodexProvider()
        assert provider.interactive is True
        assert provider.runs_interactively() is True
        assert provider.runs_interactively(execution_mode="exec") is False
        assert provider.needs_fresh_prompt_process() is True
        assert provider.needs_fresh_prompt_process(execution_mode="exec") is False
