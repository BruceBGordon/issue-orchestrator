"""Hook, verification, and repository guardrail CLI command handlers."""

import argparse
import shutil
from pathlib import Path

from rich.console import Console

from . import cli_support

console = Console()


def _print_setup_ai_gate_failure(
    *, agent_name: str, summary: str, details: str
) -> None:
    """Render a failed setup gate where no expandable diagnostic UI exists."""
    console.print(f"[red]✗[/red] {agent_name}: {summary}")
    if details:
        console.print(details, style="dim", markup=False)


def cmd_verify(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912 - multi-step verification: config, git, GitHub, tmux, agents
    """Verify the orchestrator setup works correctly."""
    console.print("[bold cyan]Orchestrator Setup Verification[/bold cyan]\n")

    errors = []
    warnings = []

    # 1. Check config file
    console.print("[bold]1. Configuration[/bold]")
    try:
        config = cli_support.load_config(args)
        console.print("  [green]✓[/green] Config file found")
        console.print(f"    Repo: {config.repo or '(auto-detect)'}")
        console.print(f"    Agents: {', '.join(config.agents.keys())}")
        console.print(f"    Repo root: {config.repo_root}")
    except FileNotFoundError as exc:
        console.print(f"  [red]✗[/red] Config not found: {exc}")
        errors.append("Config file not found - run 'issue-orchestrator setup'")
        # Can't continue without config
        console.print(
            f"\n[bold red]Verification failed: {len(errors)} error(s)[/bold red]"
        )
        return 1

    # 2. Check git repository
    console.print("\n[bold]2. Git Repository[/bold]")
    from ..execution.git_working_copy import GitWorkingCopy

    working_copy = GitWorkingCopy()
    if working_copy.is_git_repo(config.repo_root):
        console.print("  [green]✓[/green] Valid git repository")
    else:
        console.print(f"  [red]✗[/red] Not a git repository: {config.repo_root}")
        errors.append("Not a git repository")

    # 3. Check GitHub API auth
    console.print("\n[bold]3. GitHub API Auth[/bold]")
    try:
        client = cli_support.get_repository_host(config)
        if client is None:
            console.print("  [red]✗[/red] GitHub client could not be created")
            errors.append("GitHub token missing or invalid")
        else:
            snapshot = client.get_rate_limit_snapshot()
            if snapshot:
                console.print("  [green]✓[/green] GitHub token authenticated")
            else:
                console.print(
                    "  [yellow]![/yellow] GitHub token not verified (no response)"
                )
                warnings.append("GitHub token could not be verified")
    except Exception as exc:
        console.print(f"  [red]✗[/red] GitHub auth failed: {exc}")
        errors.append("GitHub token missing or invalid")

    # 4. Check hooks setup
    console.print("\n[bold]4. Git Hooks[/bold]")
    from ..execution.providers import get_hooks_dir

    bundled_hook = get_hooks_dir() / "pre-push"
    if bundled_hook.exists():
        console.print("  [green]✓[/green] Bundled pre-push hook exists")
    else:
        console.print(f"  [red]✗[/red] Bundled pre-push hook missing: {bundled_hook}")
        errors.append("Bundled pre-push hook not found")

    # Check if project uses custom hooksPath
    custom_path = working_copy.get_config_value(config.repo_root, "core.hooksPath")
    if custom_path:
        console.print(f"  [cyan]ℹ[/cyan] Project uses custom hooksPath: {custom_path}")
        project_hook = config.repo_root / custom_path / "pre-push"
        if project_hook.exists():
            console.print("  [green]✓[/green] Project pre-push hook found")
            console.print("  [cyan]ℹ[/cyan] Hooks will be chained in worktrees")
        else:
            console.print(
                f"  [yellow]![/yellow] No project pre-push hook at {project_hook}"
            )
            warnings.append("No project pre-push hook found (chaining not needed)")
    else:
        # Check standard hooks location
        main_hook = config.repo_root / ".git" / "hooks" / "pre-push"
        if main_hook.exists():
            console.print("  [green]✓[/green] Project pre-push hook found")
            console.print("  [cyan]ℹ[/cyan] Hooks will be chained in worktrees")
        else:
            console.print("  [yellow]![/yellow] No project pre-push hook")
            warnings.append(
                "No project pre-push hook (only orchestrator hook will run)"
            )

    # 5. Check agent commands
    console.print("\n[bold]5. Agent Commands[/bold]")
    for agent_name, agent_config in config.agents.items():
        cmd = agent_config.command
        if cmd:
            # Get first word of command (the executable)
            executable = cmd.split()[0]
            if shutil.which(executable):
                console.print(f"  [green]✓[/green] {agent_name}: {executable} found")
            else:
                console.print(
                    f"  [yellow]![/yellow] {agent_name}: {executable} not in PATH"
                )
                warnings.append(
                    f"Agent '{agent_name}' command '{executable}' not in PATH"
                )
        else:
            console.print(f"  [yellow]![/yellow] {agent_name}: no command configured")
            warnings.append(f"Agent '{agent_name}' has no command")

    # 6. Terminal backend
    console.print("\n[bold]6. Terminal Backend[/bold]")
    backend = config.terminal_adapter or "subprocess"
    console.print(f"  [green]✓[/green] Using terminal backend: {backend}")
    console.print(
        "  [cyan]ℹ[/cyan] Sessions run as subprocesses with raw output recorded in terminal-recording.jsonl"
    )

    # 7. Verify AI agent hooks
    console.print("\n[bold]7. AI Agent Hooks[/bold]")
    from ..infra.hooks.hooks import (
        UnsupportedAiAgentError,
        detect_agents_from_config,
        get_adapter,
    )

    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    console.print(
        f"  [cyan]ℹ[/cyan] Detected AI agents: {[t.value for t in unique_types]}"
    )

    for agent_label, agent_type in agent_types.items():
        console.print(f"    {agent_label} → {agent_type.value}")

    for agent_type in unique_types:
        try:
            adapter = get_adapter(agent_type)

            if adapter.is_installed(config.repo_root):
                console.print(f"  [green]✓[/green] {agent_type.value}: hooks installed")

                # Run thorough verification
                result = adapter.verify_hooks(config.repo_root)

                if result.success:
                    console.print(
                        f"  [green]✓[/green] {agent_type.value}: {len(result.checks_passed)} checks passed"
                    )
                    # Show some details on verbose
                    block_checks = [
                        c for c in result.checks_passed if c.startswith("blocks:")
                    ]
                    if block_checks:
                        console.print(
                            f"    [dim]Verified blocking: {len(block_checks)} patterns[/dim]"
                        )

                    # AI gate test if requested
                    if getattr(args, "test_ai_gate", False):
                        if adapter.supports_ai_gate():
                            console.print("  [cyan]🔄[/cyan] Running AI gate test...")
                            timeout = getattr(args, "ai_gate_timeout", 60)
                            ai_success, ai_msg = adapter.test_ai_gate(
                                config.repo_root, timeout=timeout
                            )
                            if ai_success:
                                console.print("  [green]✓[/green] AI gate test passed")
                                console.print(
                                    f"    [dim]{ai_msg.split(chr(10))[0]}[/dim]"
                                )
                            else:
                                console.print("  [red]✗[/red] AI gate test failed")
                                console.print(f"    {ai_msg}")
                                errors.append(
                                    f"{agent_type.value}: ai gate test failed"
                                )
                        else:
                            console.print(
                                f"  [yellow]![/yellow] {agent_type.value}: ai gate test not supported (skipping)"
                            )

                else:
                    console.print(
                        f"  [red]✗[/red] {agent_type.value}: verification failed"
                    )
                    for failure in result.checks_failed:
                        console.print(f"    [red]✗[/red] {failure}")
                        errors.append(f"{agent_type.value}: {failure}")
            else:
                console.print(
                    f"  [yellow]![/yellow] {agent_type.value}: hooks not installed"
                )
                warnings.append(
                    f"{agent_type.value} hooks not installed - run 'issue-orchestrator setup-hooks'"
                )

        except UnsupportedAiAgentError as exc:
            console.print(f"  [red]✗[/red] {agent_type.value}: {exc.reason}")
            errors.append(f"Unsupported AI agent: {exc.reason}")

    # Summary
    console.print("\n" + "=" * 50)
    if errors:
        console.print(
            f"\n[bold red]Verification FAILED: {len(errors)} error(s), {len(warnings)} warning(s)[/bold red]"
        )
        for err in errors:
            console.print(f"  [red]✗[/red] {err}")
        for warn in warnings:
            console.print(f"  [yellow]![/yellow] {warn}")
        return 1
    if warnings:
        console.print(
            f"\n[bold yellow]Verification PASSED with {len(warnings)} warning(s)[/bold yellow]"
        )
        for warn in warnings:
            console.print(f"  [yellow]![/yellow] {warn}")
        return 0
    console.print("\n[bold green]Verification PASSED - all checks OK[/bold green]")
    return 0


def cmd_setup_hooks(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912 - multi-step setup with per-agent install, verify, and AI gate tests
    """Install AI agent hooks for the target project."""
    from ..infra.ai_gate_state import load_ai_gate_state, save_ai_gate_state
    from ..infra.hooks.hooks import (
        UnsupportedAiAgentError,
        detect_agents_from_config,
        format_ai_gate_console_details,
        get_adapter,
        summarize_ai_gate_message,
    )

    console.print("[bold cyan]Installing AI Agent Hooks[/bold cyan]\n")

    # Load config
    try:
        config = cli_support.load_config(args)
    except FileNotFoundError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        console.print("No config found. Run 'issue-orchestrator setup' to create one.")
        return 1

    # Detect AI agents from config
    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    console.print("[bold]Detected AI Agents:[/bold]")
    for agent_label, agent_type in agent_types.items():
        console.print(f"  {agent_label} → {agent_type.value}")

    console.print()

    # Determine target directory
    target_root = (
        Path(args.target).resolve()
        if hasattr(args, "target") and args.target
        else config.repo_root
    )

    console.print(f"[bold]Target Project:[/bold] {target_root}\n")

    errors = []
    installed = []
    supported_adapters = []
    verification_failures = []

    for agent_type in unique_types:
        try:
            adapter = get_adapter(agent_type)

            console.print(f"[cyan]Installing hooks for {agent_type.value}...[/cyan]")
            files = adapter.install_hooks(target_root)

            for f in files:
                console.print(f"  [green]✓[/green] {f.relative_to(target_root)}")
                installed.append(f)

            # Verify installation
            result = adapter.verify_hooks(target_root)
            if result.success:
                console.print(
                    f"  [green]✓[/green] Static verification passed ({len(result.checks_passed)} checks)"
                )
                supported_adapters.append((agent_type, adapter))
            else:
                console.print("  [yellow]![/yellow] Verification had issues:")
                for failure in result.checks_failed:
                    console.print(f"    [red]✗[/red] {failure}")
                verification_failures.append(agent_type.value)

        except UnsupportedAiAgentError as exc:
            console.print(f"  [red]✗[/red] {agent_type.value}: {exc.reason}")
            errors.append(str(exc))

    console.print()

    if errors:
        console.print(
            f"[bold red]Setup completed with {len(errors)} error(s)[/bold red]"
        )
        console.print(
            "\n[yellow]Some AI agents are not supported. Consider using Claude Code.[/yellow]"
        )
        return 1

    console.print("[green]✓[/green] Files installed")
    if verification_failures:
        console.print(
            f"[yellow]![/yellow] Static verification failed for: {', '.join(verification_failures)}"
        )
    else:
        console.print("[green]✓[/green] Static verification passed")

    # Run AI gate tests (verification with state persistence)
    if supported_adapters:
        console.print("\n[cyan]Running AI gate tests...[/cyan]")
        gate_results: dict[str, tuple[bool, str]] = {}
        gate_failures = []

        for agent_type, adapter in supported_adapters:
            agent_name = agent_type.value
            try:
                if not adapter.supports_ai_gate():
                    gate_results[agent_name] = (True, "skipped (not supported)")
                    console.print(
                        f"[yellow]![/yellow] {agent_name}: ai gate test not supported (skipping)"
                    )
                    continue
                success, message = adapter.test_ai_gate(target_root)
                gate_results[agent_name] = (success, message)
                detail = summarize_ai_gate_message(message)

                if success:
                    console.print(
                        f"[green]✓[/green] {agent_name}: correctly {detail[:60]}"
                    )
                else:
                    _print_setup_ai_gate_failure(
                        agent_name=agent_name,
                        summary=detail,
                        details=format_ai_gate_console_details(message),
                    )
                    gate_failures.append(agent_name)
            except Exception as exc:
                error_msg = str(exc)
                gate_results[agent_name] = (False, error_msg)
                console.print(f"[red]✗[/red] {agent_name}: Error - {error_msg[:50]}")
                gate_failures.append(agent_name)

        # Save AI gate state
        state = load_ai_gate_state(target_root)
        state.mark_checked(gate_results)
        save_ai_gate_state(target_root, state)

        if gate_failures:
            console.print()
            if config.hooks.ai_gate.dangerous_allow_failure:
                console.print(
                    f"[bold yellow]⚠ AI gate test failed for: {', '.join(gate_failures)}[/bold yellow]"
                )
                console.print(
                    "[dim]Continuing because dangerous_allow_failure is enabled[/dim]"
                )
            else:
                console.print(
                    f"[bold red]AI gate test failed for: {', '.join(gate_failures)}[/bold red]"
                )
                console.print(
                    "\n[yellow]Hooks installed but AI gate test failed.[/yellow]"
                )
                console.print(
                    "[dim]Set hooks.ai_gate.dangerous_allow_failure: true to bypass[/dim]"
                )
                return 1

    console.print()
    if verification_failures:
        console.print(
            f"[bold yellow]Hooks installed (verification failed for {len(verification_failures)} agent(s)).[/bold yellow]"
        )
        return 1
    console.print("[bold green]Hooks installed and verified.[/bold green]")
    return 0


def cmd_setup_guardrails(args: argparse.Namespace) -> int:
    """Install repo-local guardrails and AI agent hook wiring."""
    from ..infra.repo_guardrails import RepoGuardrailsError, setup_repo_guardrails

    console.print("[bold cyan]Setting Up Repository Guardrails[/bold cyan]\n")

    try:
        config = cli_support.load_config(args)
    except FileNotFoundError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        console.print("No config found. Run 'issue-orchestrator setup' first.")
        return 1

    target_root = (
        Path(args.target).resolve()
        if getattr(args, "target", None)
        else config.repo_root
    )
    validation_cmd = getattr(args, "validation_cmd", None)
    hooks_path = getattr(args, "hooks_dir", None)

    try:
        result = setup_repo_guardrails(
            config,
            target_root=target_root,
            validation_cmd=validation_cmd,
            hooks_path=hooks_path,
        )
    except RepoGuardrailsError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1

    console.print(
        f"[green]✓[/green] Hooks path: [bold]{result.hooks_path_config}[/bold]"
    )
    console.print(
        f"[green]✓[/green] Repo pre-push: {result.pre_push_hook.relative_to(result.repo_root)}"
    )
    console.print(
        f"[green]✓[/green] PR gate: {result.verify_script.relative_to(result.repo_root)}"
    )
    console.print(
        f"[green]✓[/green] Hook helper: {result.helper_script.relative_to(result.repo_root)}"
    )

    for preserved in result.preserved_files:
        console.print(
            f"[cyan]ℹ[/cyan] Preserved existing hook: {preserved.relative_to(result.repo_root)}"
        )

    if result.agent_hook_files:
        console.print("\n[bold]AI Agent Hooks[/bold]")
        for agent_name, paths in sorted(result.agent_hook_files.items()):
            for path in paths:
                console.print(
                    f"  [green]✓[/green] {agent_name}: {path.relative_to(result.repo_root)}"
                )
    else:
        console.print(
            "\n[yellow]![/yellow] No AI agent hooks were installed (no supported agents detected)."
        )

    console.print("\n[bold green]Repository guardrails installed.[/bold green]")
    console.print(
        "[dim]Run 'issue-orchestrator doctor' to verify the guardrails end-to-end.[/dim]"
    )
    return 0
