"""OrchestratorProcess for e2e tests.

Manages the orchestrator subprocess lifecycle: start, stop, log capture.
"""

import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from issue_orchestrator.infra.process_table import ps_command, ps_env

from .inflight_tracker import control_api_headers, trigger_refresh
from .process_group_owner import OWNED_GROUP_STOP_TIMEOUT_SECONDS, ProcessGroupOwner

if TYPE_CHECKING:
    from issue_orchestrator.infra.config import Config

logger = logging.getLogger(__name__)

# Suppress httpx INFO logs (pollutes output with API health checks)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

E2E_LOG_DIR = Path("/tmp/e2e-orchestrator-logs")
E2E_LOG_DIR.mkdir(exist_ok=True)
_GRACEFUL_STOP_TIMEOUT_SECONDS = 20


def keep_artifacts() -> bool:
    """Return True if e2e cleanup should be skipped."""
    return os.environ.get("E2E_KEEP_ARTIFACTS") == "1"


def keep_remote_artifacts() -> bool:
    """Return True if remote cleanup (PRs/branches/issues) should be skipped."""
    return os.environ.get("E2E_KEEP_REMOTE_ARTIFACTS") == "1"


class OrchestratorProcess:
    """Wrapper for orchestrator subprocess with IPC support."""

    def __init__(
        self,
        config: "Config",
        project_root: Path,
        *,
        source_root: Path | None = None,
    ):
        self.config = config
        self.project_root = project_root
        self.source_root = source_root or project_root
        self.process: subprocess.Popen | None = None
        self.ipc_socket_path: Path | None = None
        self._output_lines: list[str] = []
        self._log_thread: threading.Thread | None = None
        self._stop_logging = False
        self._log_file: Path | None = None
        self._orchestrator_log_file: Path | None = None
        self._log_handle: "open | None" = None
        self._config_dir: Path | None = None
        self._config_path: Path | None = None
        self._last_log_time: float | None = None

    def _write_e2e_config(self) -> Path:
        """Write an ephemeral config file so the CLI uses the e2e config."""
        if self._config_dir is None or not self._config_dir.exists():
            self._config_dir = Path(
                tempfile.mkdtemp(prefix=f"e2e-orchestrator-config-{os.getpid()}-")
            )
        config_dir = self._config_dir
        instance_hint = (
            self.config.claims.claimant_id
            or str(self.config.control_api_port)
            or str(id(self))
        )
        safe_hint = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in instance_hint
        )
        config_path = (
            config_dir
            / f"issue-orchestrator.e2e.{os.getpid()}.{safe_hint}.{id(self)}.yaml"
        )
        data = {
            "repo": {
                "name": self.config.repo,
                "root": str(self.config.repo_root),
                "github": {
                    "token_env": self.config.github_token_env,
                    "write_verify": {
                        "timeout_seconds": self.config.gh_write_verify_timeout_seconds,
                        "initial_delay_ms": self.config.gh_write_verify_initial_delay_ms,
                        "max_delay_ms": self.config.gh_write_verify_max_delay_ms,
                        "backoff": self.config.gh_write_verify_backoff,
                        "jitter_ms": self.config.gh_write_verify_jitter_ms,
                    },
                    "audit": {
                        "enabled": self.config.gh_audit_enabled,
                        "events": self.config.gh_audit_events,
                        "file": self.config.gh_audit_file,
                    },
                },
            },
            "worktrees": {
                "base": str(self.config.worktree_base),
                "reuse_push_preflight": self.config.reuse_push_preflight,
                "remediation": {
                    "pr_collision": self.config.worktree_remediation_pr_collision,
                    "push_rebase_retry": self.config.worktree_remediation_push_rebase_retry,
                },
            },
            "execution": {
                "terminal_adapter": self.config.terminal_adapter,
                "concurrency": {
                    "max_concurrent_sessions": self.config.max_concurrent_sessions,
                    "session_timeout_minutes": self.config.session_timeout_minutes,
                },
            },
            "ui": {
                "mode": self.config.ui_mode,
                "web_port": self.config.web_port,
                "control_api_port": self.config.control_api_port,
                "queue_refresh_seconds": self.config.queue_refresh_seconds,
            },
            "filtering": {
                "label": self.config.filtering.label,
            },
            "e2e": {
                "pr_labels": self.config.e2e_pr_labels,
            },
            "agents": {
                label: {
                    "prompt": str(cfg.prompt_path),
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "timeout_minutes": cfg.timeout_minutes,
                    "skip_review": cfg.skip_review,
                    "reviewer": cfg.reviewer,
                    "command": cfg.command,
                    "meta_agent": cfg.meta_agent,
                    "initial_prompt": cfg.initial_prompt,
                    "ai_system": cfg.ai_system,
                    "provider_args": dict(cfg.provider_args),
                    "retry_prompt_template": cfg.retry_prompt_template,
                }
                for label, cfg in self.config.agents.items()
            },
            "validation": {
                "quick": {
                    "cmd": self.config.validation.quick.cmd,
                    "timeout_seconds": self.config.validation.quick.timeout_seconds,
                },
                "publish": {
                    "cmd": self.config.validation.publish.cmd,
                    "timeout_seconds": self.config.validation.publish.timeout_seconds,
                    "dirty_check": self.config.validation.publish.dirty_check,
                },
            },
            "review": {
                "default": self.config.code_review_agent,
                "code_review_label": self.config.code_review_label,
                "code_reviewed_label": self.config.code_reviewed_label,
                "max_rework_cycles": self.config.max_rework_cycles,
                "tech_lead_review_agent": self.config.tech_lead_review_agent,
                "tech_lead_review_label": self.config.tech_lead_review_label,
                "tech_lead_reviewed_label": self.config.tech_lead_reviewed_label,
                "tech_lead_review_threshold": self.config.tech_lead_review_threshold,
                "tech_lead_review_on_failure": self.config.tech_lead_review_on_failure,
                "exchange": {
                    "mode": self.config.review_exchange_mode,
                },
            },
            "cleanup": {
                "with_tech_lead": {
                    "close_ai_session_tabs": self.config.cleanup.with_tech_lead.close_ai_session_tabs,
                    "remove_worktrees": self.config.cleanup.with_tech_lead.remove_worktrees,
                },
                "without_tech_lead": {
                    "wait_for_code_review": self.config.cleanup.without_tech_lead.wait_for_code_review,
                    "close_ai_session_tabs": self.config.cleanup.without_tech_lead.close_ai_session_tabs,
                    "remove_worktrees": self.config.cleanup.without_tech_lead.remove_worktrees,
                },
            },
            "security": {
                "dangerous": {
                    "allow_unsupported_agents": self.config.dangerous.allow_unsupported_agents,
                },
            },
        }
        # Add claims config if enabled
        if self.config.claims.enabled:
            data["claims"] = {
                "enabled": True,
                "claimant_id": self.config.claims.claimant_id,
                "lease_seconds": self.config.claims.lease_seconds,
                "renew_before_expiry_seconds": self.config.claims.renew_before_expiry_seconds,
            }
        config_path.write_text(yaml.safe_dump(data, sort_keys=False))
        self._config_path = config_path
        return config_path

    def config_path(self) -> Path:
        """Return the generated e2e config path."""
        if self._config_path is None:
            raise RuntimeError("E2E config path not initialized")
        return self._config_path

    def _log_reader(self) -> None:
        """Background thread to read and print orchestrator output."""
        import select
        import sys

        if self.process is None:
            return

        while not self._stop_logging and self.process.poll() is None:
            # Use select to check for available output
            if self.process.stderr:
                readable, _, _ = select.select([self.process.stderr], [], [], 0.5)
                if readable:
                    try:
                        line = self.process.stderr.readline()
                    except ValueError:
                        break
                    if line:
                        text = line.decode('utf-8', errors='replace').rstrip()
                        self._output_lines.append(text)
                        self._last_log_time = time.time()
                        # Always write to persistent log file
                        if self._log_handle:
                            self._log_handle.write(f"{text}\n")
                            self._log_handle.flush()
                        # Print orchestrator events with prefix (filtered for readability)
                        if any(kw in text for kw in ['[EVENT]', 'Session', 'Issue', 'PR', 'Review', 'launch', 'complet', 'start', 'ERROR', 'WARN', 'failed', 'timeout']):
                            print(f"  [ORCH] {text}", file=sys.stderr, flush=True)

    def start(self, max_issues: int = 1, extra_args: list[str] | None = None) -> None:
        """Start the orchestrator process."""
        import sys
        from datetime import datetime

        # Create persistent log file (survives Ctrl+C)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._log_file = E2E_LOG_DIR / f"e2e-{timestamp}.log"
        orchestrator_log_file = E2E_LOG_DIR / f"orchestrator-{timestamp}.log"
        self._orchestrator_log_file = orchestrator_log_file
        self._log_handle = open(self._log_file, "w")

        # Clean up old log files (keep last 10)
        log_files = sorted(E2E_LOG_DIR.glob("e2e-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_log in log_files[10:]:
            old_log.unlink()

        # Print debug paths upfront for troubleshooting
        worktree_dir = Path("/tmp/e2e-worktrees")  # E2E worktree location
        claude_logs = Path.home() / ".claude" / "logs"
        print(f"\n  {'='*60}", flush=True)
        print(f"  [E2E DEBUG PATHS]", flush=True)
        print(f"    Orchestrator log: {self._log_file}", flush=True)
        print(f"    Orchestrator file: {orchestrator_log_file}", flush=True)
        print(f"    Worktrees:        {worktree_dir}", flush=True)
        print(f"    Claude logs:      {claude_logs}", flush=True)
        print(f"    Keep artifacts:   {keep_artifacts()}", flush=True)
        print(f"    Keep remote:      {keep_remote_artifacts()}", flush=True)
        if os.environ.get("E2E_CLAUDE_ARGS"):
            print(f"    E2E_CLAUDE_ARGS:  {os.environ.get('E2E_CLAUDE_ARGS')}", flush=True)
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            print(f"    E2E_PROMPT_MODE:  {os.environ.get('E2E_CLAUDE_PROMPT_MODE')}", flush=True)
        print(f"  {'='*60}\n", flush=True)

        # Write header to log file
        self._log_handle.write(f"E2E Test Run: {timestamp}\n")
        self._log_handle.write(f"Orchestrator file: {orchestrator_log_file}\n")
        self._log_handle.write(f"Worktrees: {worktree_dir}\n")
        self._log_handle.write(f"Claude logs: {claude_logs}\n")
        self._log_handle.write(f"Keep artifacts: {keep_artifacts()}\n")
        self._log_handle.write(f"Keep remote: {keep_remote_artifacts()}\n")
        self._log_handle.write(f"E2E_KEEP_ARTIFACTS: {os.environ.get('E2E_KEEP_ARTIFACTS', '')}\n")
        self._log_handle.write(f"E2E_KEEP_REMOTE_ARTIFACTS: {os.environ.get('E2E_KEEP_REMOTE_ARTIFACTS', '')}\n")
        self._log_handle.write(f"E2E_CONTROL_API_PORT: {os.environ.get('E2E_CONTROL_API_PORT', '')}\n")
        if os.environ.get("E2E_CLAUDE_ARGS"):
            self._log_handle.write(f"E2E_CLAUDE_ARGS: {os.environ.get('E2E_CLAUDE_ARGS')}\n")
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            self._log_handle.write(f"E2E_CLAUDE_PROMPT_MODE: {os.environ.get('E2E_CLAUDE_PROMPT_MODE')}\n")
        self._log_handle.write("=" * 60 + "\n\n")
        self._log_handle.flush()

        # Prefer source .venv (has e2e deps like fastapi); fall back to pytest venv.
        # Some tests run the engine against isolated local repo roots while
        # still executing the issue-orchestrator source under test.
        preferred_bin = self.source_root / ".venv" / "bin" / "issue-orchestrator"
        venv_bin = preferred_bin if preferred_bin.exists() else Path(sys.executable).parent / "issue-orchestrator"

        # UI mode is always web (subprocess backend)
        ui_mode = "web"

        config_path = self._write_e2e_config()
        label_arg = self.config.filtering.label or "test-data"
        cmd = [
            str(venv_bin), "--config", str(config_path), "start",
            "--label", label_arg,
            "--max-issues", str(max_issues),
            "--ui-mode", ui_mode,
        ]
        print(f"  [E2E] Filtering by label: {label_arg}", flush=True)

        # Add web port
        web_port = self.config.web_port
        cmd.extend(["--port", str(web_port)])
        print(f"  [E2E] Web UI available at http://localhost:{web_port}", flush=True)

        # Add control API port
        api_port = self.config.control_api_port
        if api_port > 0:
            cmd.extend(["--api-port", str(api_port)])
            print(f"  [E2E] Control API available at http://localhost:{api_port}", flush=True)

        if extra_args:
            cmd.extend(extra_args)

        # Set up environment for e2e tests
        # NOTE: Validation config is read from worktree's config file
        # (no env var override - ensures deterministic behavior)
        env = os.environ.copy()
        env["ORCHESTRATOR_LOG_LEVEL"] = "DEBUG"
        env["ORCHESTRATOR_LOG_FILE"] = str(orchestrator_log_file)
        env["PYTHONUNBUFFERED"] = "1"
        env["ORCHESTRATOR_LOG_TO_STDERR"] = "1"
        # Skip doctor checks in E2E — they create test worktrees using a fixed
        # branch name which conflicts when multiple orchestrators share a repo.
        env["ISSUE_ORCHESTRATOR_SKIP_DOCTOR"] = "1"
        env["PYTHONPATH"] = f"{self.source_root / 'src'}:{env.get('PYTHONPATH', '')}"
        env["ORCHESTRATOR_NO_BROWSER"] = "1"
        if os.environ.get("E2E_CLAUDE_ARGS"):
            env["ORCHESTRATOR_CLAUDE_ARGS"] = os.environ["E2E_CLAUDE_ARGS"]
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            env["ORCHESTRATOR_CLAUDE_PROMPT_MODE"] = os.environ["E2E_CLAUDE_PROMPT_MODE"]
        env["ORCHESTRATOR_WORKTREE_PER_SESSION"] = os.environ.get("E2E_WORKTREE_PER_SESSION", "1")
        env["ORCHESTRATOR_DISABLE_WORKTREE_REUSE"] = os.environ.get("E2E_DISABLE_WORKTREE_REUSE", "1")
        # Ensure worktrees are created from main (which has all our test fixes)
        env["ORCHESTRATOR_WORKTREE_BASE_BRANCH"] = "main"
        # Skip pre-push hooks in e2e tests - test scripts create trivial changes that don't need validation
        env["E2E_SKIP_PUSH_HOOKS"] = "1"
        # Explicitly pass dry-run mode for e2e tests when requested
        if os.environ.get("E2E_DRY_RUN_PUSH"):
            env["E2E_DRY_RUN_PUSH"] = os.environ["E2E_DRY_RUN_PUSH"]
            print(f"  [E2E] E2E_DRY_RUN_PUSH={env['E2E_DRY_RUN_PUSH']}", flush=True)

        print(f"  [E2E] Starting orchestrator: {' '.join(cmd)}", flush=True)
        self._log_handle.write(f"Command: {' '.join(cmd)}\n\n")
        self._log_handle.flush()

        self.process = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        # Start background log reader
        self._stop_logging = False
        self._log_thread = threading.Thread(target=self._log_reader, daemon=True)
        self._log_thread.start()

        self.wait_until_ready()
        print(f"  [E2E] Orchestrator started (pid={self.process.pid})", flush=True)

    def wait_until_ready(self, timeout_seconds: float = 30.0) -> None:
        """Wait until the control API is serving requests."""
        if self.process is None:
            raise RuntimeError("Orchestrator process was not started")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Orchestrator exited before readiness check passed "
                    f"(returncode={self.process.returncode})"
                )
            if self._check_api_running():
                return
            time.sleep(0.25)

        raise TimeoutError(
            f"Orchestrator control API did not become ready on port "
            f"{self.config.control_api_port} within {timeout_seconds:.1f}s"
        )

    def stop(self) -> tuple[str, str]:
        """Stop the orchestrator and return stdout/stderr."""
        if self.process is None:
            return "", ""

        print(f"  [E2E] Stopping orchestrator (pid={self.process.pid})...", flush=True)

        # Stop the log reader thread
        self._stop_logging = True
        process_owner = ProcessGroupOwner(self.process.pid)
        owned_groups = process_owner.snapshot()

        if self.process.poll() is not None:
            stdout, stderr = self.process.communicate()
            process_owner.terminate_survivors(owned_groups)
            return self._finish_process_stop(stdout, stderr, "already stopped")

        # Send SIGTERM for graceful shutdown
        self.process.send_signal(signal.SIGTERM)

        try:
            stdout, stderr = self.process.communicate(timeout=_GRACEFUL_STOP_TIMEOUT_SECONDS)
            process_owner.terminate_survivors(owned_groups)
            return self._finish_process_stop(stdout, stderr, "stopped gracefully")
        except subprocess.TimeoutExpired:
            print("  [E2E] Graceful stop timed out; terminating owned process groups...", flush=True)
            process_owner.signal(owned_groups, signal.SIGTERM)
            try:
                stdout, stderr = self.process.communicate(timeout=OWNED_GROUP_STOP_TIMEOUT_SECONDS)
                process_owner.terminate_survivors(owned_groups)
                return self._finish_process_stop(
                    stdout, stderr, "stopped after owned-group SIGTERM"
                )
            except subprocess.TimeoutExpired:
                print("  [E2E] Force killing owned process groups...", flush=True)
                process_owner.signal(owned_groups, signal.SIGKILL)
                stdout, stderr = self.process.communicate()
                process_owner.terminate_survivors(owned_groups)
                return self._finish_process_stop(stdout, stderr, "force killed")

    def crash(self) -> tuple[str, str]:
        """Crash the engine and reap every isolated agent process group."""
        if self.process is None:
            return "", ""
        self._stop_logging = True
        process_owner = ProcessGroupOwner(self.process.pid)
        owned_groups = process_owner.snapshot()
        if self.process.poll() is None:
            print(
                f"  [E2E] Crashing orchestrator and owned agents "
                f"(pid={self.process.pid}, logical_groups={len(owned_groups)})...",
                flush=True,
            )
            process_owner.signal(owned_groups, signal.SIGKILL)
        stdout, stderr = self.process.communicate()
        process_owner.terminate_survivors(owned_groups)
        return self._finish_process_stop(stdout, stderr, "crashed")

    def _finish_process_stop(
        self,
        stdout: bytes | None,
        stderr: bytes | None,
        outcome: str,
    ) -> tuple[str, str]:
        self._cleanup_log_tailers()
        self._close_log_file()
        print(f"  [E2E] Orchestrator {outcome}", flush=True)
        return stdout.decode() if stdout else "", stderr.decode() if stderr else ""

    @property
    def log_path(self) -> Path | None:
        """Get the path to the persistent log file."""
        return self._log_file

    def orchestrator_log_path(self) -> Path | None:
        """Get the path to the orchestrator log file."""
        return self._orchestrator_log_file

    def last_log_age_seconds(self) -> float:
        """Return seconds since last orchestrator stderr log line."""
        if not self._last_log_time:
            return 0.0
        return time.time() - self._last_log_time

    def _close_log_file(self) -> None:
        """Close log file and print location for debugging."""
        if self._log_handle:
            self._log_handle.write(f"\n{'='*60}\nOrchestrator stopped at {time.strftime('%H:%M:%S')}\n")
            self._log_handle.close()
            self._log_handle = None
        if self._log_file:
            print(f"  [E2E] Full log saved to: {self._log_file}", flush=True)
        if self._config_path and self._config_path.exists():
            try:
                self._config_path.unlink()
            except OSError:
                pass
        if self._config_dir and self._config_dir.exists():
            try:
                self._config_dir.rmdir()
                self._config_dir = None
            except OSError:
                pass

    def _cleanup_log_tailers(self) -> None:
        """Stop lingering session.log tail processes from tmux pipe-pane."""
        if keep_artifacts():
            return
        try:
            result = subprocess.run(
                ps_command("-ax", "-o", "pid=,command="),
                capture_output=True,
                text=True,
                check=False,
                env=ps_env(),
            )
        except OSError:
            return
        for line in result.stdout.splitlines():
            if "cat >>" not in line:
                continue
            if ".issue-orchestrator/sessions/" not in line and ".issue-orchestrator/session.log" not in line:
                continue
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue

    def is_running(self) -> bool:
        """Check if orchestrator is still running."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def _check_api_running(self) -> bool:
        """Check if the orchestrator API is responding."""
        import httpx
        api_port = self.config.control_api_port
        if not api_port:
            # No API port configured, assume running if we got this far
            return True
        try:
            resp = httpx.get(
                f"http://127.0.0.1:{api_port}/api/status",
                timeout=2,
                headers=control_api_headers(),
            )
            return resp.status_code == 200
        except Exception:
            return False

    def request_refresh(self) -> bool:
        """Request the orchestrator to refresh issues on next tick via control API."""
        return trigger_refresh()
