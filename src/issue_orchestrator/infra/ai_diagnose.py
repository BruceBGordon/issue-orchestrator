"""AI-powered diagnostics for the orchestrator.

This module provides structured diagnostic bundles and AI-assisted
analysis of orchestrator issues.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.repository_launch_selection import (
    RepositoryConfigurationIdentity,
    RepositoryLaunchSelection,
)

from .logging_config import read_log_tail
from .repo_identity import state_dir
from .startup_errors import read_startup_failure


@dataclass
class DiagnosticBundle:
    """A diagnostic bundle containing all relevant system state."""

    bundle_path: Path
    last_failure: dict[str, Any] | None = None
    log_tail: list[str] = field(default_factory=list)
    doctor_output: dict[str, Any] = field(default_factory=dict)
    config_files: dict[str, str] = field(default_factory=dict)
    timestamp: str = ""
    configuration_identity: dict[str, str] | None = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_summary(self) -> str:
        """Generate a text summary of the bundle for the AI prompt."""
        lines = [
            "# Diagnostic Bundle",
            f"Timestamp: {self.timestamp}",
            f"Bundle path: {self.bundle_path}",
            "",
        ]
        if self.configuration_identity:
            lines.extend(
                [
                    "## Effective Configuration Identity",
                    f"Mode: {self.configuration_identity['mode']}",
                    f"Config: {self.configuration_identity['config_name']}",
                    f"Fingerprint: {self.configuration_identity['config_fingerprint']}",
                    "",
                ]
            )

        # Last failure
        lines.append("## Last Startup Failure")
        if self.last_failure:
            lines.append(f"Phase: {self.last_failure.get('phase', 'unknown')}")
            lines.append(f"Message: {self.last_failure.get('message', 'unknown')}")
            lines.append(f"Suggested fix: {self.last_failure.get('suggested_fix', 'none')}")
            if self.last_failure.get('details'):
                lines.append(f"Details: {self.last_failure.get('details')}")
        else:
            lines.append("No recent failures recorded.")
        lines.append("")

        # Doctor output
        lines.append("## Doctor Diagnostics")
        if self.doctor_output:
            lines.append(f"Overall: {self.doctor_output.get('overall', 'unknown')}")
            for check in self.doctor_output.get('checks', []):
                status_icon = "✓" if check['status'] == 'ok' else "✗" if check['status'] == 'error' else "⚠"
                lines.append(f"  {status_icon} {check['name']}: {check['detail']}")
        lines.append("")

        # Log tail (last 20 lines)
        lines.append("## Recent Log (last 20 lines)")
        if self.log_tail:
            for line in self.log_tail[-20:]:
                lines.append(f"  {line}")
        else:
            lines.append("  (no logs available)")
        lines.append("")

        # Config files
        lines.append("## Configuration Files")
        for name, content in self.config_files.items():
            lines.append(f"### {name}")
            lines.append("```yaml")
            lines.append(content[:2000] + ("..." if len(content) > 2000 else ""))
            lines.append("```")
        lines.append("")

        return "\n".join(lines)


def _get_safe_env() -> dict[str, str]:
    """Get environment variables safe for passing to subprocesses.

    Strips sensitive values and credentials.
    """
    from .ai_keys import get_ai_providers

    unsafe_keys = {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ISSUE_ORCH_GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    } | set(get_ai_providers())

    return {k: v for k, v in os.environ.items() if k not in unsafe_keys}


def create_diagnostic_bundle(
    repo_root: Path,
    identity: RepositoryConfigurationIdentity | None = None,
) -> DiagnosticBundle:
    """Create a diagnostic bundle for a repository.

    Args:
        repo_root: Repository root path

    Returns:
        DiagnosticBundle with all collected diagnostics
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bundle_dir = state_dir(repo_root) / "diagnostics" / timestamp
    bundle_dir.mkdir(parents=True, exist_ok=True)

    bundle = DiagnosticBundle(
        bundle_path=bundle_dir,
        configuration_identity=(
            {
                **identity.selection.to_dict(),
                "config_fingerprint": identity.fingerprint,
            }
            if identity is not None
            else None
        ),
    )

    # 1. Last failure
    failure = read_startup_failure(repo_root)
    if failure:
        bundle.last_failure = failure.to_dict()
        with open(bundle_dir / "last_failure.json", "w") as f:
            json.dump(bundle.last_failure, f, indent=2)

    # 2. Log tail (same last-N read the board snapshot uses)
    log_path = state_dir(repo_root) / "logs" / "orchestrator.log"
    if log_path.exists():
        try:
            bundle.log_tail = read_log_tail(log_path, 200)
            with open(bundle_dir / "log_tail.txt", "w") as f:
                f.write("\n".join(bundle.log_tail))
        except OSError:
            pass

    # 3. Doctor output
    try:
        from .config import Config, get_config_path
        from .doctor import run_doctor
        from ..execution.command_runner import LocalCommandRunner

        config = None
        config_path = None
        selection = identity.selection if identity else _diagnostic_selection(repo_root)
        if selection is not None:
            config_path = get_config_path(
                repo_root,
                selection.config.value,
                selection.mode,
            )
            try:
                config = Config.load(config_path)
            except Exception:
                pass

        result = run_doctor(config=config, config_path=config_path, runner=LocalCommandRunner())
        bundle.doctor_output = result.to_dict()
        with open(bundle_dir / "doctor_output.json", "w") as f:
            json.dump(bundle.doctor_output, f, indent=2)
    except Exception:
        pass

    # 4. Config files, preserving mode/maintenance directory identity.
    _collect_diagnostic_configs(repo_root, bundle)

    # 5. Write bundle summary
    summary = bundle.to_summary()
    with open(bundle_dir / "summary.md", "w") as f:
        f.write(summary)

    return bundle


def _diagnostic_selection(repo_root: Path) -> RepositoryLaunchSelection | None:
    """Choose desired selection when valid, otherwise first typed inventory item."""
    from .config import list_configs, list_modes
    from .repo_registry import load_registry

    inventory = [
        RepositoryLaunchSelection.parse(mode=mode, config_name=config_name)
        for mode in list_modes(repo_root)
        for config_name in list_configs(repo_root, mode)
    ]
    desired = next(
        (
            repo.launch_selection
            for repo in load_registry().repos
            if repo.path == str(repo_root.resolve())
        ),
        None,
    )
    if desired in inventory:
        return desired
    return inventory[0] if inventory else None


def _collect_diagnostic_configs(
    repo_root: Path,
    bundle: DiagnosticBundle,
) -> None:
    """Copy every non-symlink YAML config with its relative identity intact."""
    from .config import get_config_dir

    config_dir = get_config_dir(repo_root)
    if not config_dir.exists():
        return
    for config_file in sorted(config_dir.rglob("*.yaml")):
        if config_file.is_symlink() or not config_file.is_file():
            continue
        try:
            content = config_file.read_text()
            relative = config_file.relative_to(config_dir)
            bundle.config_files[relative.as_posix()] = content
            destination = bundle.bundle_path / "config" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(config_file, destination)
        except OSError:
            pass


@dataclass
class DiagnoseResult:
    """Result of an AI diagnosis."""

    success: bool
    report_path: Path | None = None
    report_content: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "success": self.success,
            "report_path": str(self.report_path) if self.report_path else None,
            "report_content": self.report_content,
            "error": self.error,
        }


def run_ai_diagnose(
    repo_root: Path,
    timeout_seconds: int = 120,
    identity: RepositoryConfigurationIdentity | None = None,
) -> DiagnoseResult:
    """Run AI diagnosis on the orchestrator.

    This creates a diagnostic bundle and invokes Claude to analyze it.

    Args:
        repo_root: Repository root path
        timeout_seconds: Maximum time for AI analysis

    Returns:
        DiagnoseResult with report or error
    """
    # Create diagnostic bundle
    bundle = create_diagnostic_bundle(repo_root, identity)

    # Build the prompt
    prompt = f"""You are diagnosing issues with the issue-orchestrator system.

Analyze the following diagnostic bundle and provide:
1. A summary of what appears to be wrong
2. The likely root cause
3. Specific steps to fix the issue
4. Any warnings about potential related issues

Be concise and actionable. Focus on the most critical issues first.

{bundle.to_summary()}
"""

    # Write prompt to bundle
    prompt_path = bundle.bundle_path / "prompt.md"
    with open(prompt_path, "w") as f:
        f.write(prompt)

    # Try to invoke Claude via subprocess
    # This requires the user to have claude or claude-code installed
    report_path = bundle.bundle_path / "diagnose_report.md"

    # Check if claude-code or claude is available
    claude_cmd = None
    if shutil.which("claude"):
        claude_cmd = "claude"
    elif shutil.which("claude-code"):
        claude_cmd = "claude-code"

    if not claude_cmd:
        # Fall back to a static analysis (no AI)
        return DiagnoseResult(
            success=False,
            report_path=None,
            error="claude or claude-code not found in PATH. Install Claude Code to enable AI diagnosis.",
        )

    try:
        # Build command
        cmd = [
            claude_cmd,
            "--print",  # Print output instead of interactive mode
            "--dangerously-skip-permissions",  # Non-interactive
            prompt,
        ]

        # Run with safe environment
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_get_safe_env(),
            cwd=str(repo_root),
        )

        report_content = result.stdout
        if result.returncode != 0 and not report_content:
            report_content = f"AI analysis failed: {result.stderr}"

        # Write report
        with open(report_path, "w") as f:
            f.write("# AI Diagnosis Report\n\n")
            f.write(f"Generated: {bundle.timestamp}\n\n")
            f.write(report_content)

        return DiagnoseResult(
            success=True,
            report_path=report_path,
            report_content=report_content,
        )

    except subprocess.TimeoutExpired:
        return DiagnoseResult(
            success=False,
            error=f"AI analysis timed out after {timeout_seconds}s",
        )
    except Exception as e:
        return DiagnoseResult(
            success=False,
            error=f"AI analysis failed: {str(e)}",
        )
