"""Codex hook adapter."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

from ...infra.hooks._types import (
    AiAgentAdapter,
    AiAgentType,
    HookInstallationLayout,
    ManagedHookArtifact,
    TEMPLATES_DIR,
    VerificationResult,
)

logger = logging.getLogger(__name__)


class CodexAdapter(AiAgentAdapter):
    """Adapter for OpenAI Codex CLI.

    Codex CLI uses Starlark rules files in .codex/rules/ within the project.
    Project-scoped rules override user-global defaults.
    Rules use prefix_rule() with decision="forbidden" to block commands.
    """

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.CODEX

    def _get_rules_dir(self, project_root: Path) -> Path:
        """Get the Codex rules directory for a project."""
        return project_root / ".codex" / "rules"

    def _copy_rules_file(
        self, src: Path, target: Path, files_created: list[Path]
    ) -> None:
        """Copy a rules file."""
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)
        files_created.append(target)
        logger.info(f"Installed {target}")

    def installation_layout(self, project_root: Path) -> HookInstallationLayout:
        return HookInstallationLayout(
            managed_files=(
                ManagedHookArtifact(
                    path=self._get_rules_dir(project_root) / "orchestrator.rules",
                    template_path=TEMPLATES_DIR / "codex" / "orchestrator.rules",
                ),
            )
        )

    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install Codex CLI rules.

        Installs rules into the project's .codex/rules/ directory.
        """
        files_created: list[Path] = []
        for artifact in self._managed_files(project_root):
            if artifact.template_path is None:
                continue
            artifact.path.parent.mkdir(parents=True, exist_ok=True)
            self._copy_rules_file(artifact.template_path, artifact.path, files_created)

        return files_created

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify Codex CLI rules are installed.

        Checks project-scoped rules file and, if Codex is available,
        runs execpolicy checks to validate enforcement.
        """
        checks_passed: list[str] = []
        checks_failed: list[str] = []

        rules_file = self._get_rules_dir(project_root) / "orchestrator.rules"

        if not rules_file.exists():
            checks_failed.append("rules_file_exists: orchestrator.rules not found")
            return VerificationResult(
                False, self.agent_type, checks_passed, checks_failed
            )
        checks_passed.append("rules_file_exists")

        # Verify rules file contains our blocking rules
        content = rules_file.read_text()
        required_patterns = [
            'pattern = ["git", "push", "--no-verify"]',
            'decision = "forbidden"',
            'pattern = ["gh", "pr", "merge"]',
        ]

        for pattern in required_patterns:
            if pattern in content:
                checks_passed.append(f"rule_contains:{pattern[:30]}")
            else:
                checks_failed.append(f"rule_missing:{pattern[:30]}")

        codex_bin = shutil.which("codex")
        if not codex_bin:
            checks_failed.append("execpolicy_cli_available: codex not available")
            return VerificationResult(
                False, self.agent_type, checks_passed, checks_failed
            )

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
        except Exception as e:
            checks_failed.append(f"execpolicy_check_failed:{str(e)[:40]}")

        return VerificationResult(
            success=len(checks_failed) == 0,
            meta_agent=self.agent_type,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

    def is_installed(self, project_root: Path) -> bool:
        """Check if Codex CLI rules are installed."""
        rules_file = self._get_rules_dir(project_root) / "orchestrator.rules"
        return rules_file.exists()

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
