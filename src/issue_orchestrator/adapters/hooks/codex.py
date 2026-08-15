"""Codex hook adapter."""

import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from ._process_group import run_command_in_process_group
from ...infra.hooks._hook_test_runner import (
    build_hook_input,
    run_hook_test_cases,
)
from ...infra.hooks._types import (
    AiAgentAdapter,
    AiAgentType,
    HookInstallationLayout,
    ManagedHookArtifact,
    TEMPLATES_DIR,
    VerificationResult,
)
from ...infra.hooks.codex_session import (
    build_codex_session_hook_argv,
    prepare_codex_runtime_home,
    verify_codex_runtime_home,
)

logger = logging.getLogger(__name__)

_HOOK_RELATIVE_PATH = Path(".codex/hooks/block-no-verify.sh")
_POLICY_RELATIVE_PATH = Path(".codex/hooks/block_no_verify.py")
_HOOK_COMMAND = '"$(git rev-parse --show-toplevel)/.codex/hooks/block-no-verify.sh"'
_SHARED_POLICY_SOURCE = (
    Path(__file__).resolve().parents[2] / "infra" / "hooks" / "block_no_verify.py"
)
_BLOCK_MARKER = "BLOCKED: --no-verify is forbidden."


class CodexAdapter(AiAgentAdapter):
    """Adapter for OpenAI Codex CLI.

    Codex CLI uses project-scoped Starlark rules and PreToolUse hooks. Rules
    provide fast prefix-based defense in depth; the hook evaluates the whole
    Bash command so bypass flags cannot hide at an unsupported argument
    position.
    """

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.CODEX

    def supports_ai_gate(self) -> bool:
        return True

    def _get_rules_dir(self, project_root: Path) -> Path:
        """Get the Codex rules directory for a project."""
        return project_root / ".codex" / "rules"

    def _copy_managed_file(
        self,
        artifact: ManagedHookArtifact,
        files_created: list[Path],
    ) -> None:
        """Copy one managed Codex artifact."""
        src = artifact.template_path
        target = artifact.path
        if src is None:
            return
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)
        if artifact.executable:
            target.chmod(0o755)
        files_created.append(target)
        logger.info(f"Installed {target}")

    def _get_hook_script(self, project_root: Path) -> Path:
        return project_root / _HOOK_RELATIVE_PATH

    def _get_hook_policy(self, project_root: Path) -> Path:
        return project_root / _POLICY_RELATIVE_PATH

    def _get_hooks_json(self, project_root: Path) -> Path:
        return project_root / ".codex" / "hooks.json"

    def installation_layout(self, project_root: Path) -> HookInstallationLayout:
        return HookInstallationLayout(
            managed_files=(
                ManagedHookArtifact(
                    path=self._get_rules_dir(project_root) / "orchestrator.rules",
                    template_path=TEMPLATES_DIR / "codex" / "orchestrator.rules",
                ),
                ManagedHookArtifact(
                    path=self._get_hook_script(project_root),
                    template_path=TEMPLATES_DIR / "codex" / "block-no-verify.sh",
                    executable=True,
                ),
                ManagedHookArtifact(
                    path=self._get_hook_policy(project_root),
                    template_path=_SHARED_POLICY_SOURCE,
                    executable=True,
                ),
            ),
            registration_files=(self._get_hooks_json(project_root),),
        )

    def _load_hooks_config(self, hooks_json_path: Path) -> dict:
        if not hooks_json_path.exists():
            return {}
        try:
            hooks_config = json.loads(hooks_json_path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in {hooks_json_path}; fix it before installing hooks: {exc}"
            ) from exc
        if not isinstance(hooks_config, dict):
            raise ValueError(f"Invalid {hooks_json_path}: root must be a JSON object")
        return hooks_config

    def _pre_tool_use_entries(self, hooks_config: dict, hooks_json_path: Path) -> list:
        hooks = hooks_config.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError(f"Invalid {hooks_json_path}: hooks must be a JSON object")
        pre_tool_use = hooks.setdefault("PreToolUse", [])
        if not isinstance(pre_tool_use, list):
            raise ValueError(f"Invalid {hooks_json_path}: PreToolUse must be a list")
        for entry in pre_tool_use:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Invalid {hooks_json_path}: PreToolUse entries must be objects"
                )
            handlers = entry.get("hooks", [])
            if not isinstance(handlers, list) or not all(
                isinstance(handler, dict) for handler in handlers
            ):
                raise ValueError(
                    f"Invalid {hooks_json_path}: hook handlers must be objects"
                )
        return pre_tool_use

    def _update_hooks_json(
        self,
        hooks_json_path: Path,
        hooks_config: dict,
        files_created: list[Path],
    ) -> None:
        """Merge a synchronous orchestrator Bash hook into Codex project hooks."""
        pre_tool_use = self._pre_tool_use_entries(hooks_config, hooks_json_path)
        bash_matchers = [
            entry for entry in pre_tool_use if entry.get("matcher") == "Bash"
        ]
        hook_definition = {"type": "command", "command": _HOOK_COMMAND}
        managed_handlers = [
            handler
            for matcher in bash_matchers
            for handler in matcher.get("hooks", [])
            if handler.get("type") == "command"
            and handler.get("command") == _HOOK_COMMAND
        ]
        if managed_handlers:
            for handler in managed_handlers:
                handler.pop("async", None)
                handler.pop("timeout", None)
        elif not bash_matchers:
            pre_tool_use.append({"matcher": "Bash", "hooks": [hook_definition]})
        else:
            bash_matchers[0].setdefault("hooks", []).append(hook_definition)

        hooks_json_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_json_path.write_text(json.dumps(hooks_config, indent=2) + "\n")
        files_created.append(hooks_json_path)
        logger.info(f"Updated {hooks_json_path}")

    def validate_installation_target(self, project_root: Path) -> None:
        """Reject malformed registration before setup changes managed files."""
        hooks_json_path = self._get_hooks_json(project_root)
        hooks_config = self._load_hooks_config(hooks_json_path)
        self._pre_tool_use_entries(hooks_config, hooks_json_path)
        prepare_codex_runtime_home()

    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install Codex CLI rules and its full-command PreToolUse hook.

        Existing unrelated project hooks are preserved.
        """
        files_created: list[Path] = []
        hooks_json_path = self._get_hooks_json(project_root)
        hooks_config = self._load_hooks_config(hooks_json_path)
        self._pre_tool_use_entries(hooks_config, hooks_json_path)
        prepare_codex_runtime_home()
        for artifact in self._managed_files(project_root):
            self._copy_managed_file(artifact, files_created)

        self._update_hooks_json(hooks_json_path, hooks_config, files_created)

        return files_created

    def _hook_is_configured(self, hooks_json_path: Path) -> bool:
        if not hooks_json_path.exists():
            return False
        try:
            hooks_config = self._load_hooks_config(hooks_json_path)
            pre_tool_use = self._pre_tool_use_entries(hooks_config, hooks_json_path)
        except (OSError, ValueError):
            return False
        return any(
            handler.get("type") == "command"
            and handler.get("command") == _HOOK_COMMAND
            and not handler.get("async", False)
            for matcher in pre_tool_use
            if matcher.get("matcher") == "Bash"
            for handler in matcher.get("hooks", [])
        )

    def _verify_hooks_json(
        self,
        hooks_json_path: Path,
        checks_passed: list[str],
        checks_failed: list[str],
    ) -> None:
        if self._hook_is_configured(hooks_json_path):
            checks_passed.append("hooks_json_configured")
        else:
            checks_failed.append(
                "hooks_json_configured: orchestrator hook not in PreToolUse Bash"
            )

    def _test_hook_blocks(self, hook_script: Path, command: str) -> bool:
        """Execute the hook with the JSON envelope Codex sends to PreToolUse."""
        project_root = hook_script.parents[2]
        env = os.environ.copy()
        source_root = project_root / "src"
        if (source_root / "issue_orchestrator").is_dir():
            env.setdefault("ORCHESTRATOR_HOOK_PYTHONPATH", str(source_root))
        try:
            result = subprocess.run(
                [str(hook_script)],
                input=build_hook_input(command, "tool_input_command"),
                capture_output=True,
                text=True,
                timeout=120,
                cwd=project_root,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Codex hook test failed for %r: %s", command, exc)
            return False
        return result.returncode == 2

    def _run_hook_test_cases(
        self,
        hook_script: Path,
        checks_passed: list[str],
        checks_failed: list[str],
    ) -> None:
        run_hook_test_cases(
            self._test_hook_blocks, hook_script, checks_passed, checks_failed
        )

    def _verify_rules_file(
        self,
        rules_file: Path,
        checks_passed: list[str],
        checks_failed: list[str],
    ) -> bool:
        if not rules_file.exists():
            checks_failed.append("rules_file_exists: orchestrator.rules not found")
            return False

        checks_passed.append("rules_file_exists")
        content = rules_file.read_text()
        required_patterns = [
            'pattern = ["git", "push", "--no-verify"]',
            'pattern = ["git", "-c"]',
            'decision = "forbidden"',
            'pattern = ["gh", "pr", "merge"]',
        ]
        for pattern in required_patterns:
            if pattern in content:
                checks_passed.append(f"rule_contains:{pattern[:30]}")
            else:
                checks_failed.append(f"rule_missing:{pattern[:30]}")
        return True

    def _verify_hook_script(
        self,
        hook_script: Path,
        checks_passed: list[str],
        checks_failed: list[str],
    ) -> None:
        if not hook_script.exists():
            checks_failed.append("hook_script_exists: block-no-verify.sh not found")
            return

        checks_passed.append("hook_script_exists")
        if os.access(hook_script, os.X_OK):
            checks_passed.append("hook_script_executable")
        else:
            checks_failed.append("hook_script_executable: not executable")
        self._run_hook_test_cases(hook_script, checks_passed, checks_failed)

    def _verify_hook_policy(
        self,
        hook_policy: Path,
        checks_passed: list[str],
        checks_failed: list[str],
    ) -> None:
        if not hook_policy.exists():
            checks_failed.append("hook_policy_exists: block_no_verify.py not found")
            return
        checks_passed.append("hook_policy_exists")
        if os.access(hook_policy, os.X_OK):
            checks_passed.append("hook_policy_executable")
        else:
            checks_failed.append("hook_policy_executable: not executable")
        if hook_policy.read_bytes() == _SHARED_POLICY_SOURCE.read_bytes():
            checks_passed.append("hook_policy_matches_source")
        else:
            checks_failed.append("hook_policy_matches_source: content drifted")

    def _verify_execpolicy(
        self,
        rules_file: Path,
        checks_passed: list[str],
        checks_failed: list[str],
    ) -> None:
        try:
            blocked = self._execpolicy_allows(
                rules_file, ["git", "push", "--no-verify"]
            )
            if blocked is False:
                checks_passed.append("execpolicy_blocks:git push --no-verify")
            else:
                checks_failed.append("execpolicy_should_block:git push --no-verify")

            allowed = self._execpolicy_allows(
                rules_file, ["git", "push", "origin", "main"]
            )
            if allowed is True:
                checks_passed.append("execpolicy_allows:git push origin main")
            else:
                checks_failed.append("execpolicy_wrongly_blocks:git push origin main")
        except Exception as exc:
            checks_failed.append(f"execpolicy_check_failed:{str(exc)[:40]}")

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify Codex CLI rules and PreToolUse hook are installed.

        The hook is executed with realistic Codex JSON. execpolicy checks also
        validate the independent prefix-rule layer when Codex is available.
        """
        checks_passed: list[str] = []
        checks_failed: list[str] = []

        rules_file = self._get_rules_dir(project_root) / "orchestrator.rules"
        hook_script = self._get_hook_script(project_root)
        hook_policy = self._get_hook_policy(project_root)
        hooks_json_path = self._get_hooks_json(project_root)

        rules_available = self._verify_rules_file(
            rules_file, checks_passed, checks_failed
        )
        self._verify_hook_policy(hook_policy, checks_passed, checks_failed)
        self._verify_hook_script(hook_script, checks_passed, checks_failed)
        self._verify_hooks_json(hooks_json_path, checks_passed, checks_failed)
        try:
            verify_codex_runtime_home()
            checks_passed.append("isolated_runtime_home")
        except (OSError, RuntimeError, ValueError) as exc:
            checks_failed.append(f"isolated_runtime_home: {exc}")

        if not shutil.which("codex"):
            checks_failed.append("execpolicy_cli_available: codex not available")
            return VerificationResult(
                False, self.agent_type, checks_passed, checks_failed
            )

        if rules_available:
            self._verify_execpolicy(rules_file, checks_passed, checks_failed)

        return VerificationResult(
            success=len(checks_failed) == 0,
            meta_agent=self.agent_type,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

    def is_installed(self, project_root: Path) -> bool:
        """Check whether both Codex enforcement layers are installed."""
        rules_file = self._get_rules_dir(project_root) / "orchestrator.rules"
        hook_script = self._get_hook_script(project_root)
        hook_policy = self._get_hook_policy(project_root)
        return (
            rules_file.exists()
            and hook_script.exists()
            and os.access(hook_script, os.X_OK)
            and hook_policy.exists()
            and os.access(hook_policy, os.X_OK)
            and self._hook_is_configured(self._get_hooks_json(project_root))
        )

    def _gate_observed_block(self, stderr: str, attempted_command: str) -> bool:
        """Recognize Codex's outer router log for a pre-execution denial."""
        return all(
            marker in stderr
            for marker in (
                "Command blocked by PreToolUse hook:",
                _BLOCK_MARKER,
                f"Command: {attempted_command}",
            )
        )

    def test_ai_gate(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Prove Codex runs the invocation-scoped orchestrator hook.

        Direct script verification cannot observe Codex's hash-based trust
        decision. A read-only Codex run attempts a push of a random nonexistent
        ref with ``--no-verify`` in a suffix position that the prefix rules do
        not match. Only the PreToolUse hook can emit the required success
        marker; a sandbox rejection or ordinary Git failure is not accepted.
        """
        if not shutil.which("codex"):
            return False, "Codex CLI not found - install codex and retry"

        missing_ref = f"__io_hook_gate_missing_{uuid.uuid4().hex}__"
        attempted_command = f"git push origin {missing_ref} --no-verify"
        prompt = (
            "Run exactly this shell command using the shell tool: "
            f"{attempted_command}. Do not merely explain it. Then report "
            "whether a PreToolUse hook blocked it."
        )
        runtime_home = verify_codex_runtime_home()
        command = [
            "env",
            f"CODEX_HOME={runtime_home}",
            "codex",
            *build_codex_session_hook_argv(),
            "--ask-for-approval",
            "never",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--json",
            "-C",
            str(runtime_home),
            prompt,
        ]

        try:
            result = run_command_in_process_group(
                command,
                cwd=runtime_home,
                env=os.environ.copy(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"Codex AI gate timed out after {timeout}s"
        except OSError as exc:
            return False, f"Codex AI gate could not run: {exc}"

        output = result.stdout + result.stderr
        if self._gate_observed_block(result.stderr, attempted_command):
            return True, "AI gate test passed: Codex PreToolUse blocked --no-verify"

        return (
            False,
            "Codex invocation-scoped PreToolUse hook was not observed. Rerun "
            "`issue-orchestrator setup-hooks` and inspect Codex managed hook policy. "
            f"Codex exit: {result.returncode}; output: {output[:500]}",
        )

    def _execpolicy_allows(self, rules_file: Path, command: list[str]) -> bool | None:
        """Return True if execpolicy allows command, False if forbidden, None if unknown."""
        result = subprocess.run(
            [
                "codex",
                "execpolicy",
                "check",
                "--rules",
                str(rules_file),
                "--pretty",
                "--",
                *command,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "execpolicy check failed")

        data = json.loads(result.stdout)
        decision = data.get("decision") or data.get("strictest_decision")
        if decision is None:
            matched_rules = data.get("matchedRules")
            if matched_rules == []:
                return True
            # Fallback: search any decision-like field
            serialized = json.dumps(data).lower()
            if "forbidden" in serialized:
                return False
            if "allow" in serialized or "allowed" in serialized:
                return True
            return None

        decision = str(decision).lower()
        if decision == "forbidden":
            return False
        if decision in ("allow", "allowed"):
            return True
        return None


__all__ = ["CodexAdapter"]
