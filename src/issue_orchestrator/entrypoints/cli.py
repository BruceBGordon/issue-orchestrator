import argparse
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config

from rich.console import Console

from ..infra.logging_config import setup_logging
from .cli_auth_commands import cmd_auth, cmd_keys
from .cli_dry_run import run_dry_run as _run_dry_run
from .cli_hook_commands import cmd_setup_guardrails, cmd_setup_hooks, cmd_verify
from .cli_parser import CLICommandHandlers, build_parser
from .cli_queue_commands import cmd_audit
from .cli_run_modes import (
    run_locked_cli_engine,
)
from .cli_support import (
    client_dashboard_link as _client_dashboard_link,
    get_repository_host as _get_repository_host,
    load_config as _load_config,
    resolve_repo as _resolve_repo,
    run_test_setup as _run_test_setup,
)
from .cli_tech_lead import cmd_health_review, cmd_tech_lead
from .cli_utility_commands import cmd_demo, cmd_doctor, cmd_trace

console = Console()
logger = logging.getLogger(__name__)
def _control_api_headers() -> dict[str, str]:
    """Return request headers for the Control API, with bearer token if set.

    See security issue #5987 (F3). The orchestrator exports
    ``ISSUE_ORCHESTRATOR_API_TOKEN`` on startup. This helper reads the
    env var first, then falls back to the on-disk token file **only
    when it already exists** — deliberately not calling
    ``load_or_create_token`` here so that running the CLI (or a unit
    test that exercises a CLI function) never writes into the user's
    home directory.
    """
    token = os.environ.get("ISSUE_ORCHESTRATOR_API_TOKEN")
    if not token:
        try:
            from ..infra.api_token import default_token_path

            path = default_token_path()
            if path.exists():
                token = path.read_text().strip() or None
        except OSError:
            token = None
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _apply_cli_overrides(args: argparse.Namespace, config: "Config") -> None:  # noqa: C901, PLR0912 - one branch per CLI override, inherent to config mapping
    """Apply CLI argument overrides to config."""
    # Handle milestone override
    if hasattr(args, "milestones") and args.milestones:
        milestones = [m.strip() for m in args.milestones.split(",") if m.strip()]
        config.filtering.milestones = milestones
        config.filtering.milestone = None
        console.print(f"[cyan]Filtering by milestones: {', '.join(milestones)}[/cyan]")
    elif hasattr(args, "milestone") and args.milestone:
        config.filtering.milestone = args.milestone
        config.filtering.milestones = []
        console.print(f"[cyan]Filtering by milestone: {args.milestone}[/cyan]")

    # Handle label override
    if hasattr(args, "label") and args.label:
        config.filtering.label = args.label
        console.print(f"[cyan]Filtering by label: {args.label}[/cyan]")

    # Handle single issue filter
    if hasattr(args, "issue") and args.issue:
        config.filtering.issue = args.issue
        console.print(f"[cyan]Processing only issue #{args.issue}[/cyan]")

    # Handle ui_mode override
    if hasattr(args, "ui_mode") and args.ui_mode:
        config.ui_mode = args.ui_mode
    console.print(f"[dim]UI mode: {config.ui_mode}[/dim]")

    # Handle queue_refresh override
    if hasattr(args, "queue_refresh") and args.queue_refresh is not None:
        config.queue_refresh_seconds = args.queue_refresh

    # Handle GH audit overrides
    if hasattr(args, "gh_audit") and args.gh_audit:
        config.gh_audit_enabled = True
    if hasattr(args, "gh_audit_events") and args.gh_audit_events:
        config.gh_audit_events = True
    if hasattr(args, "gh_audit_file") and args.gh_audit_file is not None:
        config.gh_audit_file = args.gh_audit_file

    # Handle max_issues override
    if hasattr(args, "max_issues") and args.max_issues is not None:
        config.filtering.max_to_start = args.max_issues
        if config.filtering.max_to_start > 0:
            console.print(
                f"[dim]Max issues to start: {config.filtering.max_to_start}[/dim]"
            )

    # Handle review workflow overrides
    if hasattr(args, "review_label") and args.review_label is not None:
        config.tech_lead_review_label = args.review_label
        console.print(f"[dim]Review label: {config.tech_lead_review_label}[/dim]")
    if hasattr(args, "review_threshold") and args.review_threshold is not None:
        config.tech_lead_review_threshold = args.review_threshold
        if config.tech_lead_review_threshold > 0:
            console.print(
                f"[dim]Review threshold: {config.tech_lead_review_threshold} PRs[/dim]"
            )


def cmd_start(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912 - CLI entry point with config/logging/validation/startup phases
    """Start the orchestrator."""
    debug = getattr(args, "debug", False)
    no_dashboard = getattr(args, "no_dashboard", False)
    log_level = "DEBUG" if debug else "INFO"

    console.print("[green]Starting issue-orchestrator...[/green]")

    try:
        from .bootstrap import build_orchestrator
        # Load config first - repo_root is derived from config file location
        config = _load_config(args)
        from ..infra.config_paths import require_engine_launch_config_path

        if config.config_path is not None:
            require_engine_launch_config_path(config.config_path)
        loaded_effective_config = config.to_dict()
        _apply_cli_overrides(args, config)
        if config.to_dict() != loaded_effective_config:
            config.refresh_config_fingerprint()

        # Set up logging to repo-scoped log file
        log_file = setup_logging(
            repo_root=config.repo_root,
            level=log_level,
            console_output=no_dashboard,
            log_retention_days=config.log_retention_days,
        )

        if debug and log_file:
            console.print(f"[dim]Debug logging enabled (tail -f {log_file})[/dim]")

        # Validate configuration early - fail fast with clear errors
        validation_errors = config.validate()
        if validation_errors:
            console.print("[red]Configuration errors:[/red]")
            for error in validation_errors:
                console.print(f"  [red]• {error}[/red]")
                logging.error(f"Config validation: {error}")
            return 1

        # Run doctor checks including guardrails - fail fast if environment is broken
        from ..infra.launcher import LaunchStatus, launch_preflight_only
        from ..execution.command_runner import LocalCommandRunner

        launch_result = launch_preflight_only(
            config=config, runner=LocalCommandRunner()
        )
        launch_status = LaunchStatus.parse(launch_result.status)
        if launch_status is LaunchStatus.DOCTOR_ERROR:
            console.print("[red]Startup checks failed:[/red]")
            for check in launch_result.doctor.checks:
                if check.status == "error":
                    console.print(f"  [red]✗ {check.name}: {check.detail}[/red]")
                    logging.error(f"Doctor check failed: {check.name}: {check.detail}")
            console.print(
                "\n[yellow]Run 'issue-orchestrator doctor' for full diagnostics[/yellow]"
            )
            return 1
        elif launch_status is LaunchStatus.DOCTOR_WARNING:
            for check in launch_result.doctor.checks:
                if check.status == "warning":
                    console.print(f"  [yellow]⚠ {check.name}: {check.detail}[/yellow]")

        logger.info(
            "Effective config: repo=%s config_path=%s filter_label=%s ui_mode=%s web_port=%s api_port=%s "
            "max_sessions=%s session_timeout=%s queue_refresh=%s "
            "gh_write_verify_timeout=%s gh_write_verify_initial_ms=%s gh_write_verify_max_ms=%s "
            "gh_write_verify_backoff=%s gh_write_verify_jitter_ms=%s "
            "gh_audit=%s gh_audit_events=%s gh_audit_file=%s",
            config.repo,
            config.config_path,
            config.filtering.label,
            config.ui_mode,
            config.web_port,
            config.control_api_port,
            config.max_concurrent_sessions,
            config.session_timeout_minutes,
            config.queue_refresh_seconds,
            config.gh_write_verify_timeout_seconds,
            config.gh_write_verify_initial_delay_ms,
            config.gh_write_verify_max_delay_ms,
            config.gh_write_verify_backoff,
            config.gh_write_verify_jitter_ms,
            config.gh_audit_enabled,
            config.gh_audit_events,
            config.gh_audit_file,
        )
        logger.debug(
            "CLI args: label=%s milestone=%s milestones=%s issue=%s no_dashboard=%s ui_mode=%s port=%s api_port=%s test_mode=%s",
            getattr(args, "label", None),
            getattr(args, "milestone", None),
            getattr(args, "milestones", None),
            getattr(args, "issue", None),
            getattr(args, "no_dashboard", None),
            getattr(args, "ui_mode", None),
            getattr(args, "port", None),
            getattr(args, "api_port", None),
            getattr(args, "test_mode", None),
        )
        override_pairs = getattr(args, "set", None) or []
        if override_pairs:
            logger.debug("CLI overrides: %s", override_pairs)
        logger.debug("Config worktree_base=%s", config.worktree_base)
        for label, agent in config.agents.items():
            logger.debug(
                "Agent config: label=%s prompt=%s model=%s timeout=%s command=%s permission_mode=%s",
                label,
                agent.prompt_path,
                agent.model,
                agent.timeout_minutes,
                agent.command,
                agent.effective_permission_mode,
            )

    except FileNotFoundError as e:
        logging.error(f"Config not found: {e}")
        console.print(f"[red]Error: {e}[/red]")
        console.print("No config found. Run 'issue-orchestrator setup' to create one.")
        return 1
    except Exception as e:
        logging.exception(f"Unexpected error loading config: {e}")
        console.print(f"[red]Unexpected error: {e}[/red]")
        return 1

    # Handle test mode
    if args.test_mode:
        if not config.repo:
            console.print("[red]Error: repo must be set in config for test mode[/red]")
            return 1
        before_test_mode = config.to_dict()
        _run_test_setup(config)
        config.filtering.label = "test-data"
        if config.to_dict() != before_test_mode:
            config.refresh_config_fingerprint()
        console.print("[cyan]Test mode: filtering.label set to 'test-data'[/cyan]")

    console.print(f"[dim]Loaded config with {len(config.agents)} agent types[/dim]")
    console.print(
        f"[dim]Max concurrent sessions: {config.max_concurrent_sessions}[/dim]"
    )

    # Handle dry-run mode
    if hasattr(args, "dry_run") and args.dry_run:
        return _run_dry_run(args, config)

    return run_locked_cli_engine(args, config, build_orchestrator)


def cmd_status(args: argparse.Namespace) -> int:
    """Show current status."""
    try:
        config = _load_config(args)

        console.print("\n[cyan]Orchestrator Status[/cyan]")
        console.print(f"\n[bold]Config:[/bold]")
        console.print(f"  Repo: {config.repo or '(auto-detect)'}")
        console.print(f"  Max sessions: {config.max_concurrent_sessions}")
        console.print(f"  Agents: {', '.join(config.agents.keys())}")
        if config.filtering.label:
            console.print(f"  Filter label: {config.filtering.label}")
        if config.filtering.milestones:
            console.print(
                f"  Filter milestones: {', '.join(config.filtering.milestones)}"
            )
        elif config.filtering.milestone:
            console.print(f"  Filter milestone: {config.filtering.milestone}")

        console.print(
            "\n[dim]Note: Use the web dashboard to view active sessions[/dim]"
        )
        return 0
    except FileNotFoundError:
        console.print("[yellow]Orchestrator not configured yet[/yellow]")
        return 0


def retired_command(name: str, guidance: str) -> int:
    """Report a retired command uniformly and fail.

    Single owner for the retired-stub behavior. These commands are kept only so
    an old script gets a pointer to the replacement instead of an argparse
    "invalid choice" error — the capability itself is gone, so they always exit
    non-zero. ``CLIStability.RETIRED`` in
    :mod:`issue_orchestrator.entrypoints.cli_parser` is the published tier, and
    ``tests/unit/test_cli.py::TestRetiredCommandStubs`` pins that tier to which
    handlers delegate here, so a tier and its behavior cannot drift apart.
    """
    console.print(f"[yellow]The '{name}' command is no longer available.[/yellow]")
    console.print(guidance)
    return 1


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach to session (retired - use the web dashboard)."""
    return retired_command(
        "attach", "Use the web dashboard to view sessions: issue-orchestrator start"
    )


def cmd_switch(args: argparse.Namespace) -> int:
    """Switch to session (retired - use the web dashboard)."""
    return retired_command(
        "switch", "Use the web dashboard to view sessions: issue-orchestrator start"
    )


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Switch to dashboard (retired - use the web dashboard)."""
    return retired_command(
        "dashboard",
        "Start the orchestrator to access the web dashboard: issue-orchestrator start",
    )


def cmd_output(args: argparse.Namespace) -> int:
    """Show recent session output (retired - read the terminal recording)."""
    return retired_command(
        "output",
        "View session recordings in .issue-orchestrator/sessions/<run>/terminal-recording.jsonl",
    )


def cmd_pause(args: argparse.Namespace) -> int:
    """Pause the orchestrator - stop launching new sessions."""
    import httpx

    port = args.port or 8080
    base_url = f"http://localhost:{port}"

    try:
        response = httpx.post(
            f"{base_url}/api/pause", timeout=5.0, headers=_control_api_headers()
        )
        if response.status_code == 200:
            console.print(
                "[yellow]Orchestrator paused - no new sessions will launch until resumed[/yellow]"
            )
            return 0
        console.print(f"[red]Failed to pause: {response.text}[/red]")
        return 1
    except httpx.ConnectError:
        console.print("[red]Could not connect to orchestrator. Is it running?[/red]")
        return 1
    except Exception as e:
        console.print(f"[red]Error pausing: {e}[/red]")
        return 1


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume the orchestrator - allow launching new sessions."""
    import httpx

    port = args.port or 8080
    base_url = f"http://localhost:{port}"

    try:
        response = httpx.post(
            f"{base_url}/api/resume", timeout=5.0, headers=_control_api_headers()
        )
        if response.status_code == 200:
            console.print(
                "[green]Orchestrator resumed - new sessions may launch[/green]"
            )
            return 0
        console.print(f"[red]Failed to resume: {response.text}[/red]")
        return 1
    except httpx.ConnectError:
        console.print("[red]Could not connect to orchestrator. Is it running?[/red]")
        return 1
    except Exception as e:
        console.print(f"[red]Error resuming: {e}[/red]")
        return 1


def cmd_refresh(args: argparse.Namespace) -> int:
    """Request immediate refresh of issues from GitHub.

    This triggers the orchestrator to fetch issues on the next loop iteration,
    bypassing the queue_refresh_seconds interval. Useful after creating new
    issues or changing labels.
    """
    import httpx

    port = args.port or 8080
    base_url = f"http://localhost:{port}"

    try:
        response = httpx.post(
            f"{base_url}/api/refresh", timeout=5.0, headers=_control_api_headers()
        )
        if response.status_code == 200:
            console.print(
                "[green]Refresh requested - issues will be fetched on next loop iteration[/green]"
            )
            return 0
        else:
            console.print(f"[red]Failed to request refresh: {response.text}[/red]")
            return 1
    except httpx.ConnectError:
        console.print("[red]Could not connect to orchestrator. Is it running?[/red]")
        return 1
    except Exception as e:
        console.print(f"[red]Error requesting refresh: {e}[/red]")
        return 1


def cmd_restart(args: argparse.Namespace) -> int:
    """Restart the orchestrator.

    This command:
    1. Sends shutdown to the running orchestrator (via API)
    2. Waits for it to exit
    3. Starts a new orchestrator
    """
    import time
    import httpx

    port = args.port or 8080
    base_url = f"http://localhost:{port}"

    # Step 1: Check if orchestrator is running
    console.print("[cyan]Checking for running orchestrator...[/cyan]")
    try:
        resp = httpx.get(
            f"{base_url}/api/status", timeout=2.0, headers=_control_api_headers()
        )
        if resp.status_code == 200:
            console.print(f"[green]Found orchestrator on port {port}[/green]")
        else:
            console.print(
                f"[yellow]Orchestrator responded with {resp.status_code}[/yellow]"
            )
    except httpx.ConnectError:
        console.print(f"[yellow]No orchestrator running on port {port}[/yellow]")
        console.print("[cyan]Starting fresh...[/cyan]")
        # Just start fresh
        return _start_fresh(args)

    # Step 2: Send shutdown request (reason is required by /api/shutdown)
    console.print("[cyan]Sending shutdown request...[/cyan]")
    try:
        resp = httpx.post(
            f"{base_url}/api/shutdown",
            timeout=5.0,
            headers=_control_api_headers(),
            json={
                "reason": "cli restart: stopping existing orchestrator before fresh start",
                "actor": "cli.restart",
            },
        )
        if resp.status_code == 200:
            console.print("[green]Shutdown request accepted[/green]")
        else:
            console.print(f"[yellow]Shutdown returned {resp.status_code}[/yellow]")
    except Exception as e:
        console.print(f"[yellow]Error sending shutdown: {e}[/yellow]")

    # Step 3: Wait for orchestrator to exit (poll the port)
    console.print("[cyan]Waiting for orchestrator to exit...[/cyan]")
    for i in range(30):  # Wait up to 30 seconds
        try:
            httpx.get(
                f"{base_url}/api/status", timeout=1.0, headers=_control_api_headers()
            )
            # Still running
            time.sleep(1)
            if i % 5 == 4:
                console.print(f"[dim]Still waiting... ({i + 1}s)[/dim]")
        except httpx.ConnectError:
            # Orchestrator has exited
            console.print("[green]Orchestrator stopped[/green]")
            break
    else:
        console.print(
            "[yellow]Orchestrator didn't stop in time, continuing anyway...[/yellow]"
        )

    # Step 4: Start new orchestrator
    console.print("[cyan]Starting new orchestrator...[/cyan]")
    return _start_fresh(args)


def _start_fresh(args: argparse.Namespace) -> int:
    """Start a fresh orchestrator instance."""
    import sys

    # Build command to run start
    cmd = [sys.executable, "-m", "issue_orchestrator.entrypoints.cli"]

    # Global flags must precede the subcommand for argparse to accept them.
    if hasattr(args, "config") and args.config:
        cmd.extend(["--config", args.config])
    if hasattr(args, "mode") and args.mode:
        cmd.extend(["--mode", args.mode])
    cmd.append("start")

    # Pass through start-command flags.
    if hasattr(args, "port") and args.port:
        cmd.extend(["--port", str(args.port)])
    if hasattr(args, "debug") and args.debug:
        cmd.append("--debug")
    if hasattr(args, "ui_mode") and args.ui_mode:
        cmd.extend(["--ui-mode", args.ui_mode])

    console.print(f"[dim]Running: {' '.join(cmd)}[/dim]")

    # Replace this process with the new orchestrator
    import os

    os.execvp(cmd[0], cmd)
    # execvp doesn't return on success
    return 1


def cmd_setup(args: argparse.Namespace) -> int:
    """Run the interactive setup wizard."""
    from pathlib import Path

    from .cli_tools.setup_wizard import run_wizard

    target_path = Path(args.path).expanduser().resolve() if args.path else None
    dry_run = getattr(args, "dry_run", False)
    run_wizard(target_path, dry_run=dry_run)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize required GitHub labels."""
    try:
        config = _load_config(args)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("No config found. Run 'issue-orchestrator setup' to create one.")
        return 1

    try:
        repo = _resolve_repo(config)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1
    if not repo:
        console.print("[red]Error: repo must be set in config[/red]")
        return 1

    console.print(f"[cyan]Initializing labels for {repo}...[/cyan]\n")
    client = _get_repository_host(config)
    if client is None:
        console.print("[red]Error: Unable to create GitHub client[/red]")
        return 1

    # Collect all labels to create (base workflow + agents + tech_lead, incl. the
    # #6779 R3 proposal gate) via the single owner so the CLI and Control
    # Center provision the same set.
    from .setup_wizard_common import required_repo_labels

    labels = required_repo_labels(config)

    created = 0
    updated = 0
    failed = 0
    existing = {
        label.get("name") for label in client.list_labels() if isinstance(label, dict)
    }

    for label in labels:
        try:
            client.create_label(label, force=True)
            if label in existing:
                console.print(f"  [yellow]↻[/yellow] {label}")
                updated += 1
            else:
                console.print(f"  [green]✓[/green] {label}")
                created += 1
        except Exception as exc:
            console.print(f"  [red]✗[/red] {label}: {exc}")
            failed += 1

    # Print summary
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Created: {created}")
    console.print(f"  Updated: {updated}")
    console.print(f"  Failed: {failed}")

    if failed > 0:
        console.print(
            "\n[yellow]Some labels failed to create. Check your GitHub token/auth.[/yellow]"
        )
        return 1

    console.print("\n[green]✓ Label initialization complete![/green]")
    return 0


def cmd_test_reset(args: argparse.Namespace) -> int:
    """Reset test environment: teardown + setup."""
    import subprocess
    import sys
    from pathlib import Path

    console.print("[bold]Test Reset: Clean slate for integration testing[/bold]\n")

    # Find the scripts directory (4 levels up from entrypoints/cli.py)
    scripts_dir = Path(__file__).parent.parent.parent.parent / "scripts"
    if not scripts_dir.exists():
        # Try installed package location
        scripts_dir = Path(__file__).parent.parent / "scripts"

    if not scripts_dir.exists():
        console.print("[red]Error: scripts directory not found[/red]")
        return 1

    # Step 1: Teardown
    console.print("[cyan]Step 1: Tearing down existing test data...[/cyan]")
    teardown_script = scripts_dir / "teardown_test_issues.py"
    if teardown_script.exists():
        result = subprocess.run([sys.executable, str(teardown_script)])
        if result.returncode != 0:
            console.print(
                "[yellow]Warning: Teardown had issues, continuing...[/yellow]"
            )
    else:
        console.print("[yellow]Teardown script not found, skipping...[/yellow]")

    console.print()

    # Step 2: Setup
    console.print("[cyan]Step 2: Creating fresh test issues...[/cyan]")
    setup_script = scripts_dir / "setup_test_issues.py"
    if setup_script.exists():
        result = subprocess.run([sys.executable, str(setup_script)])
        if result.returncode != 0:
            console.print("[red]Error: Setup failed![/red]")
            return 1
    else:
        console.print("[yellow]Setup script not found, skipping...[/yellow]")

    console.print()
    console.print("[green]✓ Test reset complete![/green]")
    console.print("\nNow run: [bold]issue-orchestrator start[/bold]")
    return 0


def cmd_e2e_reset(args: argparse.Namespace) -> int:
    """Reset E2E run history: delete all runs, test results, and artifacts."""
    from ..infra.e2e_db import E2EDB

    config = _load_config(args)
    repo_root = config.repo_root
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"

    if not db_path.exists():
        console.print("[yellow]No E2E database found — nothing to reset.[/yellow]")
        return 0

    # Load timeline store for timeline event cleanup
    timeline_store = None
    timeline_db_path = repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
    if timeline_db_path.exists():
        from ..execution.timeline_store import SqliteTimelineStore

        timeline_store = SqliteTimelineStore(db_path=timeline_db_path)

    db = E2EDB(db_path)
    counts = db.reset_all_history(timeline_store=timeline_store)

    console.print("[bold]E2E history reset complete:[/bold]")
    for table, count in counts.items():
        console.print(f"  {table}: {count} deleted")

    # Also clean up log directory
    log_dir = repo_root / ".issue-orchestrator" / "logs" / "e2e"
    if log_dir.is_dir():
        import shutil

        shutil.rmtree(log_dir, ignore_errors=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        console.print("  log files: directory cleared")

    console.print("\n[green]Done. E2E history is now empty.[/green]")
    return 0


def cmd_default(args: argparse.Namespace) -> int:  # noqa: ARG001 - args unused but required for command signature
    """Default command when no subcommand is given - open unified dashboard."""
    import webbrowser

    from ..observation.instance_detector import (
        detect_system_state,
        get_best_entry_point,
    )

    console.print("[cyan]Issue Orchestrator[/cyan]")

    # Detect current state
    state = detect_system_state()
    entry = get_best_entry_point(state)

    if entry["action"] == "open_dashboard":
        # Dashboard is already running, just open it
        console.print(f"[dim]Dashboard already running on port {entry['port']}[/dim]")
        console.print(f"[green]Opening {entry['url']}[/green]")
        webbrowser.open(entry["url"])
        return 0

    else:
        # Need to start the dashboard
        console.print("[dim]Starting dashboard...[/dim]")

        # Start the control center (dashboard) server
        import subprocess
        import sys
        import time

        port = entry["port"]
        repo_path = entry.get("repo_path")
        url = _client_dashboard_link(port, repo_path=repo_path)

        # Start control center as a subprocess
        cmd = [
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.control_center",
            "--port",
            str(port),
            "--no-browser",  # We'll open browser ourselves
        ]

        # Start in background, but capture stderr for error reporting
        import tempfile

        stderr_file = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".log")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            start_new_session=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_CC_REPO_ROOT": str(Path.cwd().resolve()),
            },
        )

        # Health check: verify server is actually responding
        import urllib.request
        import urllib.error

        health_url = f"http://localhost:{port}/control/info"
        max_attempts = 10
        for _ in range(max_attempts):
            time.sleep(0.5)

            # Check if process died
            if process.poll() is not None:
                stderr_file.close()
                with open(stderr_file.name) as f:
                    error_output = f.read()
                console.print("[red]Failed to start dashboard server[/red]")
                if "address already in use" in error_output.lower():
                    console.print(
                        f"[yellow]Port {port} is already in use. Kill the existing process:[/yellow]"
                    )
                    console.print(f"  lsof -ti :{port} | xargs kill")
                elif error_output.strip():
                    console.print(f"[dim]Error: {error_output[:500]}[/dim]")
                return 1

            # Try to reach the server
            try:
                urllib.request.urlopen(health_url, timeout=1)
                # Success!
                stderr_file.close()
                console.print(f"[green]Dashboard started on {url}[/green]")
                webbrowser.open(url)
                return 0
            except urllib.error.URLError:
                continue  # Not ready yet

        # Timed out waiting for server
        console.print("[red]Dashboard server failed to respond[/red]")
        console.print(f"[dim]Check logs or try: curl {health_url}[/dim]")
        return 1


def main() -> int:
    """Main entry point for the CLI."""
    parser = build_parser(
        CLICommandHandlers(
            start=cmd_start,
            status=cmd_status,
            attach=cmd_attach,
            switch=cmd_switch,
            dashboard=cmd_dashboard,
            output=cmd_output,
            pause=cmd_pause,
            resume=cmd_resume,
            tech_lead=cmd_tech_lead,
            health_review=cmd_health_review,
            refresh=cmd_refresh,
            restart=cmd_restart,
            setup=cmd_setup,
            init=cmd_init,
            test_reset=cmd_test_reset,
            e2e_reset=cmd_e2e_reset,
            audit=cmd_audit,
            verify=cmd_verify,
            setup_hooks=cmd_setup_hooks,
            setup_guardrails=cmd_setup_guardrails,
            auth=cmd_auth,
            keys=cmd_keys,
            doctor=cmd_doctor,
            demo=cmd_demo,
            trace=cmd_trace,
        )
    )

    args: argparse.Namespace = parser.parse_args()

    # If no command specified, run the default (open dashboard)
    if args.command is None:
        return cmd_default(args)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
