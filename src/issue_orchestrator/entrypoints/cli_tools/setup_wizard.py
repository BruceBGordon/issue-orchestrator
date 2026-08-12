"""Interactive setup wizard for issue-orchestrator."""

import sys
from pathlib import Path
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, Optional, cast

import yaml

# Import provider registry for agent type selection
from issue_orchestrator.agent_runner import list_providers, get_provider

from ..setup_wizard_common import (
    FileCollector as FileCollector,
    PlannedWrite as _PlannedWrite,
    create_code_review_prompt as create_code_review_prompt,
    create_starter_prompt as create_starter_prompt,
    create_tech_lead_review_prompt as create_tech_lead_review_prompt,
    find_existing_config,
    find_prompt_candidates,
    get_repository_host as _get_repository_host,
    plan_setup_labels as plan_setup_labels,
    run_git,
    write_config as write_config,
)
from .setup_wizard_support import (
    ConsolePrompter,
    DetectedState,
    Prompter,
    check_prerequisites as _check_prerequisites,
    detect_repo as _detect_repo,
    fetch_github_labels as _fetch_github_labels,
    print_changes_summary as _print_changes_summary,
    prompt_worktree_setup_commands,
    scan_existing_repo as _scan_existing_repo,
    setup_ai_providers,
)
from .setup_wizard_repository_setup import (
    apply_cli_repository_setup,
    authorize_repository_setup,
    build_cli_repository_setup_request,
    setup_preview_collector,
)
from .readiness_launch import offer_readiness_assessment
from ..bootstrap_repository_setup import build_repository_setup_owner
from ...execution.providers import (
    create_repository_setup_host,
    verify_repository_setup_github_authorization,
)

# Schema metadata for defaults/labels/hints
from ...domain.worktree_paths import default_worktree_base_config
from ...infra.settings_schema import get_setup_fields
from ...ports.session_log import detect_ai_system_from_command

# Compatibility re-export for existing tests and external imports.
PlannedWrite = _PlannedWrite


def _create_cli_repository_setup_host(repo_name, authorization):
    """Use the legacy detected-auth seam or an exact explicit setup choice."""
    if authorization.kind == "detected":
        return _get_repository_host(repo_name)
    return create_repository_setup_host(repo_name, authorization)


# Default prompter for backwards compatibility
_default_prompter = ConsolePrompter()


def check_prerequisites() -> dict[str, bool]:
    """Check if required tools are installed."""
    return _check_prerequisites(run_git, list_providers, get_provider)


def detect_repo(cwd: Path | None = None) -> str | None:
    """Detect GitHub repo from the local origin remote."""
    return _detect_repo(cwd=cwd)


def fetch_github_labels(repo: str) -> list[str]:
    """Fetch all labels from GitHub repo."""
    return _fetch_github_labels(repo)


def scan_existing_repo(path: Path | None = None) -> DetectedState:
    """Scan an existing repo and detect its state."""
    return _scan_existing_repo(
        path,
        detect_repo,
        fetch_github_labels,
        find_existing_config,
        find_prompt_candidates,
    )


def _build_agent_config(
    *,
    prompt_path: str,
    model: str,
    timeout_minutes: int,
    initial_prompt: str,
    selected_provider: str | None,
    custom_command: str | None,
    permission_mode: str,
    ai_system: str | None = None,
) -> dict[str, Any]:
    """Build one agent config from the wizard answers."""
    agent_config: dict[str, Any] = {
        "prompt": prompt_path,
        "timeout_minutes": timeout_minutes,
        "initial_prompt": initial_prompt,
    }
    if model:
        agent_config["model"] = model

    if custom_command:
        agent_config["command"] = custom_command
        if ai_system:
            agent_config["ai_system"] = ai_system
        return agent_config

    if selected_provider:
        agent_config["provider"] = selected_provider
        agent_config["ai_system"] = selected_provider
        if selected_provider == "claude-code" and permission_mode != "default":
            agent_config["provider_args"] = {"permission_mode": permission_mode}

    return agent_config


def _prompt_agent_config(
    prompter: Prompter,
    *,
    agent_name: str,
    prompt_path: str,
    allow_provider_autoselect: bool = False,
) -> dict[str, Any]:
    """Prompt for one agent's runtime config."""
    timeout = _prompt_int(prompter, "Timeout in minutes", 45, min_value=1)
    agent_type = _choose_agent_type(
        prompter,
        allow_provider_autoselect=allow_provider_autoselect,
    )
    custom_command, permission_mode, selected_provider, ai_system, model = (
        _prompt_agent_runtime_details(prompter, agent_type)
    )
    initial_prompt = _default_agent_initial_prompt(
        prompter,
    )

    return _build_agent_config(
        prompt_path=prompt_path,
        model=model,
        timeout_minutes=timeout,
        initial_prompt=initial_prompt,
        selected_provider=selected_provider,
        custom_command=custom_command,
        permission_mode=permission_mode,
        ai_system=ai_system,
    )


def _choose_agent_type(
    prompter: Prompter,
    *,
    allow_provider_autoselect: bool,
) -> str:
    """Choose which provider or custom command backs an agent."""

    providers = list_providers()
    available_providers = [p for p in providers if get_provider(p).is_available()]
    if allow_provider_autoselect and len(available_providers) == 1:
        agent_type = available_providers[0]
        prompter.print(f"\n  Using available provider: {agent_type}")
        return agent_type

    prompter.print("\n  Agent provider options:")
    for provider_name in providers:
        provider_obj = get_provider(provider_name)
        available = "✓" if provider_obj.is_available() else "✗ not installed"
        prompter.print(
            f"    {provider_name}  - {provider_obj.description} ({available})"
        )
    prompter.print("    custom  - Use a custom command/script")

    provider_choices = providers + ["custom"]
    return prompter.choice("Agent provider", provider_choices)


def _prompt_agent_runtime_details(
    prompter: Prompter,
    agent_type: str,
) -> tuple[str | None, str, str | None, str | None, str]:
    """Collect provider-specific runtime details for one agent."""
    if agent_type == "custom":
        custom_command, ai_system = _prompt_custom_agent_details(prompter)
        return custom_command, "default", None, ai_system, "sonnet"

    selected_provider = agent_type
    model, permission_mode = _prompt_provider_runtime_details(
        prompter,
        selected_provider,
    )
    return None, permission_mode, selected_provider, None, model


def _prompt_custom_agent_details(prompter: Prompter) -> tuple[str, str | None]:
    """Collect custom command details and infer the ai_system when possible."""
    prompter.print("\n  Enter your custom command template. Available variables:")
    prompter.print("    {issue_number}, {issue_title}, {prompt}, {worktree}, {model}")
    custom_command = prompter.input("Custom command")
    ai_system = detect_ai_system_from_command(custom_command)
    if ai_system:
        prompter.print(f"  Detected ai_system: {ai_system}")
        return custom_command, ai_system

    prompter.print(
        "  Couldn't infer ai_system from that command. "
        "This is required for validation and diagnostics."
    )
    return custom_command, prompter.input("AI system name", "claude-code")


def _prompt_provider_runtime_details(
    prompter: Prompter,
    provider_name: str,
) -> tuple[str, str]:
    """Collect runtime details for a named built-in provider."""
    if provider_name == "claude-code":
        return _prompt_claude_runtime_details(prompter)
    if provider_name == "codex":
        prompter.print(
            "\n  Leave the model blank to use the Codex CLI default for your account."
        )
        return prompter.input("Model for Codex", ""), "default"
    return prompter.input("Model name", "default"), "default"


def _prompt_claude_runtime_details(prompter: Prompter) -> tuple[str, str]:
    """Collect Claude model and permission preferences."""
    model = prompter.choice("Model", ["sonnet", "opus", "haiku"])

    prompter.print("\n  Permission mode controls how Claude handles tool permissions:")
    prompter.print("    default          - Prompt for each action (safest)")
    prompter.print("    acceptEdits      - Auto-accept file edits, prompt for others")
    prompter.print(
        "    bypassPermissions - Skip all prompts (use for trusted automation)"
    )
    permission_mode = prompter.choice(
        "Permission mode", ["default", "acceptEdits", "bypassPermissions"]
    )
    return model, _confirm_claude_permission_mode(prompter, permission_mode)


def _confirm_claude_permission_mode(
    prompter: Prompter,
    permission_mode: str,
) -> str:
    """Confirm high-risk Claude permission settings."""
    if permission_mode != "bypassPermissions":
        return permission_mode

    prompter.print("\n  ⚠️  WARNING: bypassPermissions allows the agent to:")
    prompter.print("     - Execute any shell commands without confirmation")
    prompter.print("     - Read/write any files without confirmation")
    prompter.print("     - Access network resources without confirmation")
    if prompter.yes_no(
        "Are you sure you want to bypass all permission prompts?",
        default=False,
    ):
        return permission_mode

    prompter.print("  → Using 'default' mode instead")
    return "default"


def _default_agent_initial_prompt(
    prompter: Prompter,
) -> str:
    """Build the default initial prompt for the selected agent role."""

    prompter.print("\n  Does this agent do code reviews?")
    prompter.print("    Review agents need {pr_number} in their prompt template.")
    is_review_agent = prompter.yes_no("Is this a review agent?", default=False)

    if is_review_agent:
        return (
            "Review PR #{pr_number} for issue #{issue_number}: {issue_title}. "
            "Follow the instructions in {prompt}. When done, use reviewer-done to report your verdict."
        )
    return (
        "Work on issue #{issue_number}: {issue_title}. "
        "Follow the instructions in {prompt}. When done, use coding-done to report completion."
    )


def _prompt_validation_settings(
    config: dict[str, Any],
    prompter: Prompter,
) -> None:
    """Prompt for validation settings when the config does not already define them."""
    validation = config.get("validation") or {}
    if validation.get("quick") or validation.get("publish"):
        return

    prompter.print("\n--- Validation ---")
    prompter.print("Quick validation runs on coding-done and review exchanges.")
    quick_cmd = prompter.input("Quick validation command (optional)", "make test")
    publish_cmd = prompter.input("Publish validation command (optional)", quick_cmd)
    validation_config: dict[str, Any] = {}
    if quick_cmd.strip():
        quick_timeout = _prompt_int(
            prompter,
            "Quick validation timeout (seconds)",
            300,
            min_value=1,
        )
        validation_config["quick"] = {
            "cmd": quick_cmd.strip(),
            "timeout_seconds": quick_timeout,
        }
    if publish_cmd.strip():
        publish_timeout = _prompt_int(
            prompter,
            "Publish validation timeout (seconds)",
            1800,
            min_value=1,
        )
        validation_config["publish"] = {
            "cmd": publish_cmd.strip(),
            "timeout_seconds": publish_timeout,
            "dirty_check": "tracked",
        }
    if validation_config:
        config["validation"] = validation_config


def _prompt_manual_existing_agent(
    config: dict[str, Any],
    prompter: Prompter,
    *,
    prompt_candidates: list[Path],
) -> None:
    """Force existing-project onboarding to end with at least one agent."""
    prompter.print("\n--- Agent Configuration ---")
    prompter.print(
        "No agents are configured yet. Add at least one agent so the orchestrator "
        "has agent labels to watch."
    )

    while not config.get("agents"):
        agent_name = prompter.input("Agent label (e.g., 'agent:backend')")
        if not agent_name:
            prompter.print("You need at least one agent!")
            continue
        if not agent_name.startswith("agent:"):
            if prompter.yes_no(f"Add 'agent:' prefix to make it 'agent:{agent_name}'?"):
                agent_name = f"agent:{agent_name}"

        agent_short = agent_name.split(":")[-1]
        matching_prompts = [
            candidate
            for candidate in prompt_candidates
            if agent_short.lower() in candidate.name.lower()
        ]
        if matching_prompts:
            prompter.print("  Possible prompt files:")
            for i, path in enumerate(matching_prompts[:5], 1):
                prompter.print(f"    {i}. {path.relative_to(Path.cwd())}")
            choice = prompter.input("Choose (number) or enter path", "1")
            try:
                idx = int(choice) - 1
                prompt_path = str(matching_prompts[idx].relative_to(Path.cwd()))
            except (ValueError, IndexError):
                prompt_path = choice
        else:
            prompt_path = prompter.input(
                "Prompt file path",
                f".prompts/{agent_short}.md",
            )

        config.setdefault("agents", {})
        config["agents"][agent_name] = _prompt_agent_config(
            prompter,
            agent_name=agent_name,
            prompt_path=prompt_path,
        )
        prompter.print(f"  ✓ Added {agent_name}")


def _wizard_field_default(field: dict[str, Any]) -> Any:
    """Override a few schema defaults for first-run setup UX."""
    if field.get("name") == "web_port" and field.get("default") == 0:
        return 8080
    return field["default"]


def _prompt_int(
    prompter: Prompter,
    question: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Prompt for an integer and retry on invalid input."""
    while True:
        raw = prompter.input(question, str(default))
        try:
            value = int(raw)
        except ValueError:
            prompter.print(f"  Invalid number '{raw}'. Enter an integer.")
            continue
        if min_value is not None and value < min_value:
            prompter.print(f"  Value must be at least {min_value}.")
            continue
        if max_value is not None and value > max_value:
            prompter.print(f"  Value must be at most {max_value}.")
            continue
        return value


def _config_uses_claude_code(config: dict[str, Any]) -> bool:
    """Return whether any configured agent uses Claude Code."""
    for agent_config in config.get("agents", {}).values():
        if not isinstance(agent_config, dict):
            continue
        if agent_config.get("provider") == "claude-code":
            return True
        if agent_config.get("ai_system") == "claude-code":
            return True
    return False


def _claude_session_interactions_enabled(config: dict[str, Any]) -> bool:
    """Return whether runner-managed Claude startup interactions are enabled."""
    execution_config = config.get("execution")
    if not isinstance(execution_config, dict):
        return False
    interactions_config = execution_config.get("session_interactions")
    if not isinstance(interactions_config, dict):
        return False
    return bool(interactions_config.get("enabled"))


def _print_claude_code_worktree_note(prompter: Prompter) -> None:
    """Explain Claude Code's per-worktree trust prompt behavior."""
    prompter.print("  Claude Code note: trust is stored per worktree path.")
    prompter.print(
        "  Issue Orchestrator can auto-accept Claude's initial trust prompt "
        "when trusted session interactions are enabled."
    )
    prompter.print(
        "  A dedicated worktree directory keeps those paths predictable, but "
        "trusting the parent directory once does not automatically trust "
        "future child worktrees."
    )


def _prompt_claude_session_interactions(
    config: dict[str, Any],
    prompter: Prompter,
) -> None:
    """Offer runner-managed Claude startup prompt handling during onboarding."""
    if not _config_uses_claude_code(config):
        return

    execution_config = cast(dict[str, Any], config.setdefault("execution", {}))
    interactions_config = execution_config.get("session_interactions")
    if isinstance(interactions_config, dict) and "enabled" in interactions_config:
        return

    prompter.print("\n--- Claude Startup Prompts ---")
    prompter.print("Claude Code may pause on its first worktree trust screen.")
    prompter.print(
        "Issue Orchestrator can auto-accept this trusted startup prompt in "
        "orchestrator-created worktrees."
    )
    prompter.print("Recommended for hands-free Claude onboarding.")
    if prompter.yes_no(
        "Enable trusted session interactions for Claude startup prompts?",
        default=True,
    ):
        execution_config["session_interactions"] = {"enabled": True}


def _print_claude_code_next_steps(
    prompter: Prompter,
    config: dict[str, Any],
) -> None:
    """Call out how Claude trust prompts will behave after onboarding."""
    prompter.print("\n  Claude Code note:")
    if _claude_session_interactions_enabled(config):
        prompter.print("     Trusted session interactions are enabled.")
        prompter.print(
            "     Issue Orchestrator will auto-accept Claude's initial trust "
            "prompt in orchestrator-created worktrees."
        )
    else:
        prompter.print(
            "     The first interactive Claude session in a new worktree may pause "
            "for manual trust approval."
        )
        prompter.print(
            "     To auto-accept this trusted startup prompt later, set "
            "execution.session_interactions.enabled: true."
        )
    prompter.print(
        "     Trust is per worktree path. A dedicated worktree base keeps paths "
        "easy to find, but pre-approving the parent directory does not auto-trust "
        "child worktrees."
    )


def _collect_stage2_tech_lead(
    prompter: Prompter,
    config: MutableMapping[str, Any],
    code_reviewed_label: str,
    agent_labels: Iterable[str],
) -> None:
    """Prompt for the optional Stage 2 tech_lead batch review and write it into the
    review block (shared by both wizard flows).

    A configured tech lead agent can propose create_issue follow-ups, so it also
    REQUIRES a follow-up worker agent to route new issues to (#6779 R14) —
    collected here so the generated config passes startup validation.
    """
    prompter.print("")
    if not prompter.yes_no("Enable Stage 2: Tech Lead batch review?", default=False):
        return
    review = cast(dict[str, Any], config["review"])
    config["tech_lead"] = {
        **cast(dict[str, Any], config.get("tech_lead", {})),
        "enabled": True,
    }
    prompter.print("\n  --- Stage 2: Tech Lead Batch Review ---")
    review_agent = prompter.input("  tech_lead review agent label", "agent:tech-lead")
    reviewed_label = prompter.input(
        "  Label after tech_lead review", "tech-lead-reviewed"
    )
    threshold_raw = prompter.input("  Trigger after N code-reviewed PRs", "5")
    follow_up_default = next(
        (a for a in agent_labels if a != review_agent), review_agent
    )
    review["tech_lead_review_agent"] = review_agent
    review["tech_lead_follow_up_agent"] = prompter.input(
        "  Worker agent for tech-lead-created follow-up issues", follow_up_default
    )
    review["tech_lead_reviewed_label"] = reviewed_label
    try:
        threshold = int(threshold_raw)
    except ValueError:
        threshold = 0
    if threshold > 0:
        review["tech_lead_review_threshold"] = threshold
        prompter.print(
            f"  ✓ tech_lead review triggers after {threshold} PRs with '{code_reviewed_label}'"
        )
    prompter.print(f"  ✓ Label flow: {code_reviewed_label} → {reviewed_label}")


def _collect_internal_reviewer(
    prompter: Prompter,
    config: MutableMapping[str, Any],
) -> None:
    """Collect the optional fast reviewer loop owned by each coder turn."""
    review = cast(dict[str, Any], config.setdefault("review", {"enabled": False}))
    existing_internal = review.get("internal", {}) or {}
    if not isinstance(existing_internal, Mapping):
        raise ValueError("review.internal must be a mapping")
    existing_enabled = existing_internal.get("enabled") is True
    existing_max_rounds = existing_internal.get("max_rounds", 5)
    if not isinstance(existing_max_rounds, int):
        existing_max_rounds = 5
    existing_instructions = existing_internal.get(
        "instructions",
        ".io/internal-review.md",
    )
    if not isinstance(existing_instructions, str) or not existing_instructions.strip():
        existing_instructions = ".io/internal-review.md"

    prompter.print("")
    enabled = prompter.yes_no(
        "Enable coder-owned internal reviewer loop?",
        default=existing_enabled,
    )
    if not enabled:
        review["internal"] = {
            "enabled": False,
            "max_rounds": existing_max_rounds,
            "instructions": existing_instructions.strip(),
        }
        return
    max_rounds = _prompt_int(
        prompter,
        "  Maximum internal review rounds",
        existing_max_rounds,
        min_value=1,
        max_value=50,
    )
    instructions = prompter.input(
        "  Internal reviewer instructions file",
        existing_instructions.strip(),
    ).strip()
    review["internal"] = {
        "enabled": True,
        "max_rounds": max_rounds,
        "instructions": instructions,
    }
    prompter.print(
        "  ✓ Each coder must earn internal approval before successful completion"
    )


def wizard_new_project(prompter: Prompter) -> dict[str, Any]:  # noqa: C901, PLR0912 - interactive wizard with branches for each config option
    """Walk through new project setup."""
    config: dict[str, Any] = {"agents": {}}

    prompter.print("\n" + "=" * 50)
    prompter.print("NEW PROJECT SETUP")
    prompter.print("=" * 50)

    setup_mode = prompter.choice(
        "Setup depth",
        [
            "Quick setup (recommended)",
            "Advanced setup",
        ],
    )
    advanced = "Advanced" in setup_mode

    # Repo
    detected_repo = detect_repo()
    if detected_repo:
        repo_name = prompter.input("GitHub repo", detected_repo)
    else:
        repo_name = prompter.input("GitHub repo (owner/name)")
    config["repo"] = {"name": repo_name}

    # Agents
    prompter.print("\n--- Agent Configuration ---")
    prompter.print("Agents are identified by GitHub labels (e.g., 'agent:backend').")
    prompter.print("Each agent needs a prompt file with instructions.\n")

    while True:
        agent_name = prompter.input(
            "Agent label (e.g., 'agent:backend', or empty to finish)"
        )
        if not agent_name:
            if not config["agents"]:
                prompter.print("You need at least one agent!")
                continue
            break

        if not agent_name.startswith("agent:"):
            if prompter.yes_no(f"Add 'agent:' prefix to make it 'agent:{agent_name}'?"):
                agent_name = f"agent:{agent_name}"

        prompt_path = prompter.input(
            f"Prompt file path for {agent_name}",
            f".prompts/{agent_name.split(':')[-1]}.md",
        )
        config["agents"][agent_name] = _prompt_agent_config(
            prompter,
            agent_name=agent_name,
            prompt_path=prompt_path,
            allow_provider_autoselect=not advanced,
        )

        prompter.print(f"✓ Added {agent_name}\n")

    # Concurrency — schema-driven
    prompter.print("\n--- Concurrency Settings ---")
    concurrency_values: dict[str, Any] = {}
    for field in get_setup_fields("concurrency"):
        concurrency_values[field["name"]] = _prompt_int(
            prompter,
            field["prompt"],
            int(field["default"]),
            min_value=0,
        )
    config["execution"] = {
        "concurrency": concurrency_values,
    }

    # Issue scheduling / milestones
    if advanced:
        prompter.print("\n--- Issue Scheduling ---")
        prompter.print("Issues are scheduled using a multi-level sort:\n")
        prompter.print(
            "  1. Dependencies  - Issues with 'blocked by #N' wait for #N to close"
        )
        prompter.print("  2. Milestone     - Configurable strategy (see below)")
        prompter.print(
            "  3. Priority tier - From issue title: [P0-x] < [P1-x] < ... < [P9-x]"
        )
        prompter.print("  4. Sequence      - The number after Px: [P1-001] < [P1-002]")
        prompter.print("  5. Issue number  - Tie-breaker (lower first)\n")
        prompter.print("Milestone sort strategies:")
        prompter.print(
            "  milestone_number - Extract number from name (M1 < M2 < M10) [default]"
        )
        prompter.print("  due_date         - By milestone due date (earliest first)")
        prompter.print("  pattern          - Custom regex to extract number")
        prompter.print("  name             - Alphabetically by milestone name\n")
        valid_strategies = ("due_date", "milestone_number", "pattern", "name")
        while True:
            milestone_sort = prompter.input(
                "Milestone sort strategy", "milestone_number"
            )
            if milestone_sort in valid_strategies:
                break
            prompter.print(
                f"  Invalid strategy '{milestone_sort}'. Please choose from: {', '.join(valid_strategies)}"
            )
        milestones_config: dict[str, Any] = {"sort": milestone_sort}
        config["milestones"] = milestones_config

        if milestone_sort == "pattern":
            prompter.print(
                "\n  Enter a regex pattern with one capture group for the number."
            )
            prompter.print("  Examples:")
            prompter.print("    M(\\d+)       → matches 'M13' → 13")
            prompter.print("    Sprint (\\d+) → matches 'Sprint 5' → 5")
            pattern = prompter.input("  Pattern", r"M(\d+)")
            milestones_config["sort_config"] = {"pattern": pattern}

        order_raw = prompter.input(
            "Optional milestone order (comma-separated, blank for none)", ""
        )
        if order_raw.strip():
            milestones_config["order"] = [
                m.strip() for m in order_raw.split(",") if m.strip()
            ]

        # Foundation milestone for dependency scope
        prompter.print(
            "\n  Dependencies must be within the same milestone OR in the foundation milestone."
        )
        prompter.print(
            "  Example: Issues in M2 can depend on M2 issues or M0 (foundation) issues."
        )
        foundation_milestone = prompter.input("Foundation milestone", "M0")
        milestones_config = cast(dict[str, Any], config.setdefault("milestones", {}))
        milestones_config["foundation"] = foundation_milestone
    else:
        config["milestones"] = {"sort": "milestone_number", "foundation": "M0"}

    # Worktree location — schema-driven
    prompter.print("\n--- Worktree Location ---")
    prompter.print("Each issue gets its own git worktree for isolated work.")
    prompter.print("Examples:")
    prompter.print(
        "  '../worktrees/myrepo' → dedicated sibling collection "
        "(~/dev/worktrees/myrepo/myrepo-123)"
    )
    prompter.print("  '../'                  → direct sibling dirs (~/dev/myrepo-123)")
    if _config_uses_claude_code(config):
        _print_claude_code_worktree_note(prompter)
    _wt_fields = get_setup_fields("worktrees")
    _wt_field = _wt_fields[0] if _wt_fields else {"prompt": "Worktree Base Directory"}
    worktree_base = prompter.input(
        _wt_field["prompt"],
        default_worktree_base_config(Path.cwd().resolve()),
    )

    # Set at top-level (not per-agent)
    worktrees_config: dict[str, Any] = {"base": worktree_base}
    config["worktrees"] = worktrees_config

    # If subdirectory, offer to add to .gitignore
    if not worktree_base.startswith(".."):
        worktree_dir = worktree_base.lstrip("./")
        gitignore_path = Path(".gitignore")
        needs_gitignore = True

        if gitignore_path.exists():
            gitignore_content = gitignore_path.read_text()
            if worktree_dir in gitignore_content:
                needs_gitignore = False
                prompter.print(f"  ✓ {worktree_dir} already in .gitignore")

        if needs_gitignore:
            if prompter.yes_no(f"Add '{worktree_dir}/' to .gitignore?"):
                with open(gitignore_path, "a") as f:
                    f.write(f"\n# Issue orchestrator worktrees\n{worktree_dir}/\n")
                prompter.print(f"  ✓ Added {worktree_dir}/ to .gitignore")

    # Worktree setup commands
    setup_cmds = prompt_worktree_setup_commands(prompter)
    if setup_cmds:
        worktrees_config["setup"] = setup_cmds

    _prompt_claude_session_interactions(config, prompter)

    # UI Mode
    prompter.print("\n--- UI Mode ---")
    prompter.print("How do you want to monitor agent sessions?\n")
    prompter.print("  web    - Browser dashboard (recommended)")
    prompter.print(
        "           Best for most users. Opens in your browser or forwarded client URL."
    )
    prompter.print("  tmux   - Terminal multiplexer sessions")
    prompter.print("           For terminal power users. Requires tmux installed.\n")
    ui_mode = prompter.input("UI mode", "web")
    if ui_mode not in ("web", "tmux"):
        prompter.print(f"  Invalid mode '{ui_mode}', using 'web'")
        ui_mode = "web"
    ui_config: dict[str, Any] = {"mode": ui_mode}
    config["ui"] = ui_config
    if ui_mode == "web":
        # Schema-driven UI fields (web_port)
        for field in get_setup_fields("ui"):
            cond = field.get("condition")
            if cond and cond.get("field") == "ui_mode" and cond.get("value") != ui_mode:
                continue
            ui_config[field["name"]] = _prompt_int(
                prompter,
                field["prompt"],
                int(_wizard_field_default(field)),
                min_value=0,
                max_value=65535,
            )
        prompter.print("\n--- Terminal Backend (web mode) ---")
        prompter.print("Choose how agent sessions are executed:\n")
        prompter.print("  tmux       - Default (stable, interactive)")
        prompter.print(
            "  subprocess - No tmux dependency; records raw terminal output in "
            ".issue-orchestrator/sessions/<run>/terminal-recording.jsonl\n"
        )
        terminal_backend = prompter.input("Terminal backend", "tmux")
        if terminal_backend not in ("tmux", "subprocess"):
            prompter.print(f"  Invalid backend '{terminal_backend}', using 'tmux'")
            terminal_backend = "tmux"
        if terminal_backend == "subprocess":
            execution_config = cast(dict[str, Any], config.setdefault("execution", {}))
            execution_config["terminal_adapter"] = "subprocess"

    # Labels - use defaults, can be customized in YAML later
    # Default labels: in-progress, blocked, needs-human
    prompter.print("\n--- Label Prefix ---")
    prompter.print("Prefix for status labels to avoid conflicts with existing labels.")
    prompter.print("  Example: 'io' → 'io:in-progress', 'io:blocked', etc.\n")
    label_prefix = prompter.input("Label prefix (optional)", "")
    if label_prefix:
        config["labels"] = {"prefix": label_prefix}
        prompter.print(
            f"  ✓ Labels will be prefixed: {label_prefix}:in-progress, {label_prefix}:blocked, etc."
        )

    _prompt_validation_settings(config, prompter)

    # Filtering (optional)
    prompter.print("\n--- Filtering (Optional) ---")
    label_filter = prompter.input("Only process issues with label (optional)", "")
    if label_filter.strip():
        config["filtering"] = {"label": label_filter.strip()}

    # Review workflow
    prompter.print("\n--- Review Workflow ---")
    prompter.print("Optional automated review for PRs created by agents.\n")
    enable_review = prompter.yes_no("Enable code review?", default=False)
    if enable_review:
        prompter.print("\n  --- Stage 1: Per-PR Code Review ---")
        code_review_agent = prompter.input(
            "  Code review agent label", "agent:reviewer"
        )
        code_review_label = prompter.input(
            "  Label for PRs needing review", "needs-code-review"
        )
        code_reviewed_label = prompter.input(
            "  Label after review passes", "code-reviewed"
        )

        # Use new review structure with enabled flag
        config["review"] = {
            "enabled": True,
            "default": code_review_agent,
            "code_review_label": code_review_label,
            "code_reviewed_label": code_reviewed_label,
        }
        prompter.print(f"  ✓ PRs will be reviewed by {code_review_agent}")
        prompter.print(f"  ✓ Label flow: {code_review_label} → {code_reviewed_label}")

        # Stage 2: Tech Lead Batch Review (advanced only)
        if advanced:
            _collect_stage2_tech_lead(
                prompter, config, code_reviewed_label, config["agents"]
            )

    _collect_internal_reviewer(prompter, config)
    return config


def wizard_existing_project(  # noqa: C901, PLR0912 - interactive wizard with branches for detected state and config options
    state: DetectedState, prompter: Prompter
) -> tuple[dict[str, Any], Optional[Path]]:
    """Walk through existing project onboarding.

    Returns:
        Tuple of (config dict, existing_config_path if updating else None)
    """
    prompter.print("\n" + "=" * 50)
    prompter.print("EXISTING PROJECT ONBOARDING")
    prompter.print("=" * 50)

    # Show what we found
    prompter.print("\n--- Detected State ---")
    prompter.print(f"  Repo: {state.repo or 'Not detected'}")
    prompter.print(f"  Existing config: {state.config_path or 'None'}")
    prompter.print(f"  GitHub labels: {len(state.github_labels)} total")
    prompter.print(f"  Agent labels: {', '.join(state.agent_labels) or 'None'}")
    prompter.print(f"  Prompt candidates: {len(state.prompt_candidates)} files")

    # Start with existing config or fresh
    config: dict[str, Any]
    updating_existing_path: Optional[Path] = None
    if state.existing_config:
        prompter.print(f"\n✓ Found existing config at {state.config_path}")
        if prompter.yes_no("Update existing config?"):
            config = dict(state.existing_config)
            updating_existing_path = state.config_path
        else:
            config = {"agents": {}}
    else:
        config = {"agents": {}}

    # Ensure repo is set
    repo_value = state.repo or prompter.input("GitHub repo (owner/name)")
    if "repo" not in config or not isinstance(config.get("repo"), dict):
        config["repo"] = {"name": repo_value}
    elif "name" not in config["repo"]:
        config["repo"]["name"] = repo_value

    # Check for agent labels not in config
    configured_agents = set(config.get("agents", {}).keys())
    unconfigured_agents = [a for a in state.agent_labels if a not in configured_agents]

    if unconfigured_agents:
        prompter.print(
            f"\n--- Found {len(unconfigured_agents)} agent labels not in config ---"
        )
        for agent_label in unconfigured_agents:
            prompter.print(f"\nAgent: {agent_label}")
            if prompter.yes_no(f"Add {agent_label} to config?"):
                # Suggest prompt files
                agent_short = agent_label.split(":")[-1]
                matching_prompts = [
                    p
                    for p in state.prompt_candidates
                    if agent_short.lower() in p.name.lower()
                ]

                if matching_prompts:
                    prompter.print("  Possible prompt files:")
                    for i, p in enumerate(matching_prompts[:5], 1):
                        prompter.print(f"    {i}. {p.relative_to(Path.cwd())}")
                    choice = prompter.input("Choose (number) or enter path", "1")
                    try:
                        idx = int(choice) - 1
                        prompt_path = str(matching_prompts[idx].relative_to(Path.cwd()))
                    except (ValueError, IndexError):
                        prompt_path = choice
                else:
                    prompt_path = prompter.input(
                        "Prompt file path",
                        f".prompts/{agent_short}.md",
                    )
                config.setdefault("agents", {})
                config["agents"][agent_label] = _prompt_agent_config(
                    prompter,
                    agent_name=agent_label,
                    prompt_path=prompt_path,
                )
                prompter.print(f"  ✓ Added {agent_label}")

    if not config.get("agents"):
        _prompt_manual_existing_agent(
            config,
            prompter,
            prompt_candidates=state.prompt_candidates,
        )

    # Ensure we have concurrency settings
    if "execution" not in config or "concurrency" not in config.get("execution", {}):
        prompter.print("\n--- Concurrency Settings ---")
        max_sessions = _prompt_int(
            prompter,
            "Max concurrent sessions",
            3,
            min_value=1,
        )
        config.setdefault("execution", {})
        config["execution"]["concurrency"] = {"max_concurrent_sessions": max_sessions}

    # Milestone sorting - only ask if not already configured
    if "milestones" not in config or "sort" not in config.get("milestones", {}):
        prompter.print("\n--- Issue Scheduling ---")
        prompter.print("Issues are scheduled using a multi-level sort:\n")
        prompter.print(
            "  1. Dependencies  - Issues with 'blocked by #N' wait for #N to close"
        )
        prompter.print("  2. Milestone     - Configurable strategy (see below)")
        prompter.print(
            "  3. Priority tier - From issue title: [P0-x] < [P1-x] < ... < [P9-x]"
        )
        prompter.print("  4. Sequence      - The number after Px: [P1-001] < [P1-002]")
        prompter.print("  5. Issue number  - Tie-breaker (lower first)\n")
        prompter.print("Milestone sort strategies:")
        prompter.print(
            "  milestone_number - Extract number from name (M1 < M2 < M10) [default]"
        )
        prompter.print("  due_date         - By milestone due date (earliest first)")
        prompter.print("  pattern          - Custom regex to extract number")
        prompter.print("  name             - Alphabetically by milestone name\n")
        valid_strategies = ("due_date", "milestone_number", "pattern", "name")
        while True:
            milestone_sort = prompter.input(
                "Milestone sort strategy", "milestone_number"
            )
            if milestone_sort in valid_strategies:
                break
            prompter.print(
                f"  Invalid strategy '{milestone_sort}'. Please choose from: {', '.join(valid_strategies)}"
            )
        milestones_config = cast(dict[str, Any], config.setdefault("milestones", {}))
        milestones_config["sort"] = milestone_sort

        if milestone_sort == "pattern":
            prompter.print(
                "\n  Enter a regex pattern with one capture group for the number."
            )
            prompter.print("  Examples:")
            prompter.print("    M(\\d+)       → matches 'M13' → 13")
            prompter.print("    Sprint (\\d+) → matches 'Sprint 5' → 5")
            pattern = prompter.input("  Pattern", r"M(\d+)")
            milestones_config["sort_config"] = {"pattern": pattern}

    if "milestones" not in config or "order" not in config.get("milestones", {}):
        order_raw = prompter.input(
            "Optional milestone order (comma-separated, blank for none)", ""
        )
        if order_raw.strip():
            milestones_config = cast(
                dict[str, Any], config.setdefault("milestones", {})
            )
            milestones_config["order"] = [
                m.strip() for m in order_raw.split(",") if m.strip()
            ]

    # Foundation milestone for dependency scope - only ask if not already configured
    if "milestones" not in config or "foundation" not in config.get("milestones", {}):
        prompter.print(
            "\n  Dependencies must be within the same milestone OR in the foundation milestone."
        )
        prompter.print(
            "  Example: Issues in M2 can depend on M2 issues or M0 (foundation) issues."
        )
        foundation_milestone = prompter.input("Foundation milestone", "M0")
        milestones_config = cast(dict[str, Any], config.setdefault("milestones", {}))
        milestones_config["foundation"] = foundation_milestone

    # Check if config needs worktrees.base — schema-driven
    if "worktrees" not in config or "base" not in config.get("worktrees", {}):
        prompter.print("\n--- Worktree Location ---")
        prompter.print("Each issue gets its own git worktree for isolated work.")
        prompter.print("Examples:")
        prompter.print(
            "  '../worktrees/myrepo' → dedicated sibling collection "
            "(~/dev/worktrees/myrepo/myrepo-123)"
        )
        prompter.print(
            "  '../'                  → direct sibling dirs (~/dev/myrepo-123)"
        )
        if _config_uses_claude_code(config):
            _print_claude_code_worktree_note(prompter)
        _wt_fields = get_setup_fields("worktrees")
        _wt_field = (
            _wt_fields[0] if _wt_fields else {"prompt": "Worktree Base Directory"}
        )
        worktree_base = prompter.input(
            _wt_field["prompt"],
            default_worktree_base_config(Path.cwd().resolve()),
        )
        worktrees_config = cast(dict[str, Any], config.setdefault("worktrees", {}))
        worktrees_config["base"] = worktree_base

        # If subdirectory, offer to add to .gitignore
        if not worktree_base.startswith(".."):
            worktree_dir = worktree_base.lstrip("./")
            gitignore_path = Path(".gitignore")
            needs_gitignore = True

            if gitignore_path.exists():
                gitignore_content = gitignore_path.read_text()
                if worktree_dir in gitignore_content:
                    needs_gitignore = False
                    prompter.print(f"  ✓ {worktree_dir} already in .gitignore")

            if needs_gitignore:
                if prompter.yes_no(f"Add '{worktree_dir}/' to .gitignore?"):
                    with open(gitignore_path, "a") as f:
                        f.write(f"\n# Issue orchestrator worktrees\n{worktree_dir}/\n")
                    prompter.print(f"  ✓ Added {worktree_dir}/ to .gitignore")

    # Worktree setup commands (if not already configured)
    if "setup" not in config.get("worktrees", {}):
        setup_cmds = prompt_worktree_setup_commands(prompter)
        if setup_cmds:
            wt_config = cast(dict[str, Any], config.setdefault("worktrees", {}))
            wt_config["setup"] = setup_cmds

    _prompt_claude_session_interactions(config, prompter)

    # UI mode
    if "ui" not in config or "mode" not in config.get("ui", {}):
        prompter.print("\n--- UI Mode ---")
        prompter.print("How do you want to monitor agent sessions?\n")
        prompter.print("  web    - Browser dashboard (recommended)")
        prompter.print(
            "           Best for most users. Opens in your browser or forwarded client URL."
        )
        prompter.print("  tmux   - Terminal multiplexer sessions")
        prompter.print(
            "           For terminal power users. Requires tmux installed.\n"
        )
        ui_mode = prompter.input("UI mode", "web")
        if ui_mode not in ("web", "tmux"):
            prompter.print(f"  Invalid mode '{ui_mode}', using 'web'")
            ui_mode = "web"
        ui_config = cast(dict[str, Any], config.setdefault("ui", {}))
        ui_config["mode"] = ui_mode
        if ui_mode == "web":
            for field in get_setup_fields("ui"):
                cond = field.get("condition")
                if (
                    cond
                    and cond.get("field") == "ui_mode"
                    and cond.get("value") != ui_mode
                ):
                    continue
                ui_config[field["name"]] = _prompt_int(
                    prompter,
                    field["prompt"],
                    int(_wizard_field_default(field)),
                    min_value=0,
                    max_value=65535,
                )
            prompter.print("\n--- Terminal Backend (web mode) ---")
            prompter.print("Choose how agent sessions are executed:\n")
            prompter.print("  tmux       - Default (stable, interactive)")
            prompter.print(
                "  subprocess - No tmux dependency; records raw terminal output in "
                ".issue-orchestrator/sessions/<run>/terminal-recording.jsonl\n"
            )
            terminal_backend = prompter.input("Terminal backend", "tmux")
            if terminal_backend not in ("tmux", "subprocess"):
                prompter.print(f"  Invalid backend '{terminal_backend}', using 'tmux'")
                terminal_backend = "tmux"
            if terminal_backend == "subprocess":
                execution_config = cast(
                    dict[str, Any], config.setdefault("execution", {})
                )
                execution_config["terminal_adapter"] = "subprocess"

    # Label prefix - only ask if not already configured
    if "labels" not in config or "prefix" not in config.get("labels", {}):
        prompter.print("\n--- Label Prefix ---")
        prompter.print(
            "Prefix for status labels to avoid conflicts with existing labels."
        )
        prompter.print("  Example: 'io' → 'io:in-progress', 'io:blocked', etc.\n")
        label_prefix = prompter.input("Label prefix", "io")
        if label_prefix:
            if "labels" not in config:
                config["labels"] = {}
            config["labels"]["prefix"] = label_prefix
            prompter.print(
                f"  ✓ Labels will be prefixed: {label_prefix}:in-progress, {label_prefix}:blocked, etc."
            )

    _prompt_validation_settings(config, prompter)

    # Two-stage review workflow - ask if not already configured (default enabled)
    has_review_config = "review" in config and config["review"].get("enabled")
    if not has_review_config:
        prompter.print("\n--- Review Workflow ---")
        prompter.print("Code review is RECOMMENDED to catch issues before merging:")
        prompter.print("  Stage 1: Per-PR code review (immediate, after each PR)")
        prompter.print(
            "  Stage 2: Tech Lead batch review (when N reviewed PRs accumulate)\n"
        )

        # Stage 1: Per-PR Code Review (default enabled)
        if prompter.yes_no("Enable Stage 1: Per-PR code review?", default=True):
            prompter.print("\n  --- Stage 1: Per-PR Code Review ---")
            code_review_agent = prompter.input(
                "  Code review agent label", "agent:reviewer"
            )
            code_review_label = prompter.input(
                "  Label for PRs needing review", "needs-code-review"
            )
            code_reviewed_label = prompter.input(
                "  Label after review passes", "code-reviewed"
            )

            # Use new review structure with enabled flag
            config["review"] = {
                "enabled": True,
                "default": code_review_agent,
                "code_review_label": code_review_label,
                "code_reviewed_label": code_reviewed_label,
            }
            prompter.print(f"  ✓ PRs will be reviewed by {code_review_agent}")
            prompter.print(
                f"  ✓ Label flow: {code_review_label} → {code_reviewed_label}"
            )

            # Stage 2: Tech Lead Batch Review (only if Stage 1 enabled)
            _collect_stage2_tech_lead(
                prompter, config, code_reviewed_label, config["agents"]
            )

    _collect_internal_reviewer(prompter, config)
    return config, updating_existing_path


def default_cleanup_config(config: dict) -> dict:
    """Build safe automatic cleanup defaults for a newly generated config."""
    review_cfg = config.get("review", {})
    has_tech_lead = review_cfg.get("tech_lead_review_agent")
    has_code_review = review_cfg.get("enabled") or review_cfg.get("default")
    if has_tech_lead:
        return {
            "with_tech_lead": {
                "close_ai_session_tabs": True,
                "remove_worktrees": True,
            }
        }
    return {
        "without_tech_lead": {
            "wait_for_code_review": bool(has_code_review),
            "close_ai_session_tabs": True,
            "remove_worktrees": True,
        }
    }


def run_wizard(  # noqa: C901, PLR0912 - main wizard entry point with prerequisite checks, mode selection, and confirmation flow
    target_path: Path | None = None,
    prompter: Prompter | None = None,
    dry_run: bool = False,
) -> None:
    """Main wizard entry point.

    Args:
        target_path: Directory to set up. If None, prompts user.
        prompter: Prompter for user interaction. If None, uses ConsolePrompter.
        dry_run: If True, show what would be done without writing files.
    """
    if prompter is None:
        prompter = ConsolePrompter()

    prompter.print("\n" + "=" * 50)
    if dry_run:
        prompter.print("  issue-orchestrator Setup Wizard (DRY RUN)")
    else:
        prompter.print("  issue-orchestrator Setup Wizard")
    prompter.print("=" * 50)

    # Determine target directory
    if target_path is None:
        cwd = Path.cwd()
        prompter.print(f"\nCurrent directory: {cwd}")

        # Check if this looks like the issue-orchestrator package itself
        is_orchestrator_dir = (cwd / "src" / "issue_orchestrator").exists()
        if is_orchestrator_dir:
            prompter.print(
                "⚠ This looks like the issue-orchestrator package directory."
            )
            prompter.print("  You probably want to set up a different project.\n")
            # Don't default to this directory
            target_input = prompter.input("Project directory to set up (required)", "")
            if not target_input:
                prompter.print("Error: Please specify a project directory.")
                sys.exit(1)
        else:
            target_input = prompter.input("Project directory to set up", str(cwd))

        target_path = Path(target_input).expanduser().resolve()

        if not target_path.exists():
            prompter.print(f"Error: {target_path} does not exist")
            sys.exit(1)
        if not target_path.is_dir():
            prompter.print(f"Error: {target_path} is not a directory")
            sys.exit(1)

    # Change to target directory for the rest of the wizard
    import os

    _original_cwd = Path.cwd()
    os.chdir(target_path)
    prompter.print(f"\nSetting up: {target_path}\n")

    # Check prerequisites
    prompter.print("Checking prerequisites...")
    prereqs = check_prerequisites()

    all_ok = True
    for tool, ok in prereqs.items():
        status = "✓" if ok else "✗"
        prompter.print(f"  {status} {tool}")
        if not ok:
            all_ok = False

    if not all_ok:
        prompter.print(
            "\n⚠ Some prerequisites are missing. Install them before continuing."
        )
        if not prereqs["github_auth"]:
            prompter.print(
                "  Set a GitHub token (ISSUE_ORCH_GITHUB_TOKEN, GITHUB_TOKEN, or repo.github config)"
            )
        if not prereqs.get("any_ai_provider", False):
            prompter.print("  Install at least one AI provider CLI:")
            for name in list_providers():
                provider = get_provider(name)
                prompter.print(f"    - {name}: {provider.description}")
        if not prompter.yes_no("Continue anyway?", default=False):
            sys.exit(1)

    # Explain the wizard flow
    if dry_run:
        prompter.print("\n⚠ DRY RUN MODE - No changes will be made")
    prompter.print("\nThis wizard will:")
    prompter.print("  1. Ask questions about your project configuration")
    prompter.print("  2. Show a summary of all changes")
    prompter.print("  3. Apply changes only after your approval")

    # Optional readiness assessment before configuring (soft advisory, never blocks)
    offer_readiness_assessment(prompter, target_path, dry_run=dry_run)

    # Choose mode
    prompter.print("\n" + "-" * 50)
    mode = prompter.choice(
        "What would you like to do?",
        [
            "New project - set up from scratch",
            "Existing project - I have labels/issues already",
        ],
    )

    existing_config_path: Optional[Path] = None
    if "New" in mode:
        config = wizard_new_project(prompter)
    else:
        state = scan_existing_repo()
        config, existing_config_path = wizard_existing_project(state, prompter)

    # Add cleanup config with defaults (don't prompt - users can edit later)
    if "cleanup" not in config:
        config["cleanup"] = default_cleanup_config(config)

    # Review config
    prompter.print("\n" + "=" * 50)
    prompter.print("CONFIGURATION SUMMARY")
    prompter.print("=" * 50)
    prompter.print(yaml.dump(config, default_flow_style=False, sort_keys=False))

    # Determine config file path and collect the write
    # Use absolute paths to avoid issues with cwd
    from ...infra.config import CONFIG_DIR, DEFAULT_CONFIG_NAME

    default_config_path = f"{CONFIG_DIR}/modes/default/{DEFAULT_CONFIG_NAME}"

    if existing_config_path:
        output_path = (
            target_path / existing_config_path
            if not existing_config_path.is_absolute()
            else existing_config_path
        )
    elif dry_run:
        output_path = target_path / default_config_path
    else:
        user_path = Path(
            prompter.input(f"Config filename ({target_path}/)", default_config_path)
        )
        output_path = (
            target_path / user_path if not user_path.is_absolute() else user_path
        )

    setup_owner = build_repository_setup_owner(
        _create_cli_repository_setup_host,
        verify_repository_setup_github_authorization,
    )
    setup_request = build_cli_repository_setup_request(
        owner=setup_owner,
        config=config,
        repo_root=target_path,
        config_path=output_path,
    )
    setup_preview = setup_owner.preview(setup_request)
    file_collector = setup_preview_collector(setup_preview)
    all_prompt_paths = [
        Path(agent_config["prompt"])
        for agent_config in config.get("agents", {}).values()
        if isinstance(agent_config, dict) and agent_config.get("prompt")
    ]

    # Show summary and ask for confirmation
    _print_changes_summary(file_collector, prompter, dry_run)

    if dry_run:
        prompter.print("Run without --dry-run to apply these changes.")
        return

    if not prompter.yes_no("\nApply these changes?"):
        prompter.print("Aborted.")
        sys.exit(0)

    # Apply all changes
    prompter.print("\nApplying changes...")
    apply_cli_repository_setup(
        owner=setup_owner,
        request=authorize_repository_setup(setup_request, setup_preview),
        prompter=prompter,
    )

    install_hooks_now = False
    setup_repo_guardrails_now = False

    validation_config = config.get("validation") or {}
    publish_validation_cmd = bool((validation_config.get("publish") or {}).get("cmd"))

    if publish_validation_cmd:
        setup_repo_guardrails_now = prompter.yes_no(
            "\nInstall repo-local guardrails and AI agent hooks now? (recommended)",
            default=True,
        )
        if setup_repo_guardrails_now:
            try:
                from ...infra.config import Config
                from ...infra.repo_guardrails import setup_repo_guardrails

                temp_config = Config.load(output_path)
                result = setup_repo_guardrails(temp_config, target_root=target_path)
                prompter.print("\nRepo guardrails installed:")
                prompter.print(f"  ✓ Hooks path: {result.hooks_path_config}")
                prompter.print(f"  ✓ {result.pre_push_hook.relative_to(target_path)}")
                prompter.print(f"  ✓ {result.verify_script.relative_to(target_path)}")
                if result.agent_hook_files:
                    for agent_name, paths in sorted(result.agent_hook_files.items()):
                        for path in paths:
                            prompter.print(
                                f"  ✓ {agent_name}: {path.relative_to(target_path)}"
                            )
            except Exception as exc:
                prompter.print(f"\n⚠ Repo guardrail setup failed: {exc}")
                prompter.print(
                    "  You can retry later with: issue-orchestrator setup-guardrails"
                )
    else:
        install_hooks_now = prompter.yes_no(
            "\nInstall AI agent hooks now? (recommended)", default=True
        )
        if install_hooks_now:
            try:
                from ...infra.config import Config
                from ...infra.hooks.hooks import install_hooks_for_config

                temp_config = Config.load(output_path)
                installed = install_hooks_for_config(temp_config, target_path)
                if installed:
                    prompter.print("\nHooks installed:")
                    for agent_type, paths in installed.items():
                        for path in paths:
                            prompter.print(f"  ✓ {agent_type.value}: {path}")
                else:
                    prompter.print(
                        "\nNo hooks installed (no supported agents detected)."
                    )
            except Exception as exc:
                prompter.print(f"\n⚠ Hook installation failed: {exc}")
                prompter.print(
                    "  You can retry later with: issue-orchestrator setup-hooks"
                )
        if not publish_validation_cmd:
            prompter.print(
                "\nRepo-local pre-push guardrails skipped: configure validation.publish.cmd first, "
                "then run 'issue-orchestrator setup-guardrails'."
            )

    # Optional AI provider key setup
    if prompter.yes_no("\nSet up optional AI provider API keys now?", default=False):
        setup_ai_providers(prompter)

    # MCP review exchange probe (if configured)
    try:
        from ...infra.config import Config
        from ...infra.review_exchange_probe import probe_review_exchange
        from ...execution.command_runner import LocalCommandRunner

        exchange = (config.get("review") or {}).get("exchange", {})
        if isinstance(exchange, dict) and exchange.get("mode") in ("via-mcp", "auto"):
            prompter.print("\nRunning MCP review-exchange validation...")
            temp_config = Config.load(output_path)
            checks = probe_review_exchange(
                temp_config, LocalCommandRunner(), force=True
            )
            for check in checks:
                prompter.print(f"  - {check.name}: {check.status} ({check.detail})")
    except Exception as exc:
        prompter.print(f"\nMCP validation skipped: {exc}")

    prompter.print("\n" + "=" * 50)
    prompter.print("Setup complete! Next steps:")
    prompter.print("=" * 50)

    # List prompt files to review
    prompter.print("\n  1. Review/edit your prompt files:")
    for prompt_path in all_prompt_paths:
        prompter.print(f"     • {prompt_path}")

    step_number = 2
    has_validation_cmd = publish_validation_cmd

    if not setup_repo_guardrails_now and has_validation_cmd:
        prompter.print(
            f"\n  {step_number}. Install repo guardrails + AI hooks (recommended): issue-orchestrator setup-guardrails"
        )
        step_number += 1
    elif not install_hooks_now:
        prompter.print(
            f"\n  {step_number}. Install AI agent hooks (recommended): issue-orchestrator setup-hooks"
        )
        step_number += 1
        prompter.print(
            f"  {step_number}. Configure validation.publish.cmd, then set up repo guardrails (recommended): issue-orchestrator setup-guardrails"
        )
        step_number += 1

    prompter.print(f"\n  {step_number}. Run: issue-orchestrator doctor")
    step_number += 1
    prompter.print(f"\n  {step_number}. Run: issue-orchestrator init")
    step_number += 1
    prompter.print(
        f"\n  {step_number}. Commit the generated onboarding files before start."
    )
    prompter.print(
        "     Agent worktrees are seeded from the configured git ref (usually origin/main), "
        "so local prompt/config changes must be pushed there or you must set "
        "worktrees.seed_ref for local iteration."
    )
    step_number += 1

    # List agent labels to add to issues
    review_config = config.get("review", {}) or {}
    code_review_agent = review_config.get("default")
    tech_lead_review_agent = review_config.get("tech_lead_review_agent")
    agent_labels = list(config.get("agents", {}).keys())
    # Exclude review agents from the list (they work on PRs, not issues)
    work_agent_labels = [
        label
        for label in agent_labels
        if label != code_review_agent and label != tech_lead_review_agent
    ]
    prompter.print(f"\n  {step_number}. Add agent labels to your GitHub issues:")
    for label in work_agent_labels:
        prompter.print(f"     • {label}")
    step_number += 1

    prompter.print(f"\n  {step_number}. Run: issue-orchestrator start")

    if _config_uses_claude_code(config):
        _print_claude_code_next_steps(prompter, config)

    prompter.print(
        "\n  Re-run the readiness assessment anytime with: issue-orchestrator setup"
    )

    prompter.print("\n  Advanced features (enable in config later):")
    prompter.print(
        "     E2E Test Runner - Automatically runs your test suite when main"
    )
    prompter.print("     branch changes. Tracks flaky tests, retries failures, shows")
    prompter.print("     progress in dashboard. Enable: e2e.enabled: true")
    prompter.print("     See: docs/user/e2e.md")
    prompter.print("")


if __name__ == "__main__":
    run_wizard()
