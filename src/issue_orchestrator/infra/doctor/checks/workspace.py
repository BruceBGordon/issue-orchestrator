"""Workspace and agent checks for doctor."""

import shutil
from pathlib import Path

from ..types import Check
from ...config import Config
from ...provider_cli_diagnostics import provider_cli_missing_detail
from ....ports.command_runner import CommandRunner


def check_working_directory(
    runner: CommandRunner | None,
    repo_root: Path | None = None,
) -> list[Check]:
    checks: list[Check] = []
    if runner is None:
        return checks

    repo_root = repo_root or Path.cwd()
    try:
        from ....adapters.git.git_cli import GitCLI
        git = GitCLI(runner=runner)
        status_result = git.run(repo_root, ["status", "--porcelain"], timeout_s=10, check=False)
        if status_result.returncode == 0:
            has_uncommitted = bool(status_result.stdout.strip())
            if has_uncommitted:
                checks.append(Check(
                    name="Working Directory",
                    status="warning",
                    detail=(
                        "Uncommitted changes stay only in this checkout; agent worktrees "
                        "are seeded from a git ref, not your working tree"
                    ),
                ))
            else:
                checks.append(Check(
                    name="Working Directory",
                    status="ok",
                    detail="Clean",
                ))
        else:
            checks.append(Check(
                name="Working Directory",
                status="info",
                detail="Could not check git status",
            ))
    except Exception:
        checks.append(Check(
            name="Working Directory",
            status="info",
            detail="Could not check git status",
        ))

    return checks


def check_hook_dependencies(repo_root: Path) -> list[Check]:
    checks: list[Check] = []

    python3 = shutil.which("python3")
    if python3:
        checks.append(Check(
            name="Python3",
            status="ok",
            detail=python3,
        ))
    else:
        checks.append(Check(
            name="Python3",
            status="error",
            detail="python3 not found in PATH (required for hooks)",
        ))

    return checks


def _provider_script_problem(agent_name: str, provider_name: str) -> str | None:
    from issue_orchestrator.agent_runner import get_provider

    try:
        provider = get_provider(provider_name)
    except ValueError:
        return f"{agent_name}: unknown provider {provider_name}"

    if provider.is_available():
        return None

    executable = getattr(provider, "executable", provider_name)
    return f"{agent_name}: {provider_cli_missing_detail(provider_name, executable)}"


def _legacy_script_problem(agent_name: str, command: str) -> str | None:
    cmd_parts = command.split()
    script = None
    for part in cmd_parts:
        if "=" not in part or part.startswith("{"):
            script = part
            break
    if script and not shutil.which(script) and not Path(script).exists():
        return f"{agent_name}: {script}"
    return None


def _check_agent_scripts(config: Config) -> Check:
    """Check if agent scripts are available."""
    missing_scripts = []
    for name, agent_cfg in config.agents.items():
        provider_name = agent_cfg.provider
        if provider_name is None and config.default_agent:
            provider_name = config.default_agent.provider
        if provider_name:
            problem = _provider_script_problem(name, provider_name)
            if problem:
                missing_scripts.append(problem)
            continue

        problem = _legacy_script_problem(name, agent_cfg.command)
        if problem:
            missing_scripts.append(problem)

    if missing_scripts:
        return Check(
            name="Agent Scripts",
            status="error",
            detail=f"Missing: {', '.join(missing_scripts[:3])}" + ("..." if len(missing_scripts) > 3 else ""),
        )
    return Check(name="Agent Scripts", status="ok", detail="All found")


def _check_retry_templates(config: Config) -> Check | None:
    """Check if retry templates exist. Returns None if no templates configured."""
    repo_root = config.repo_root
    missing_templates = []

    if config.retry and config.retry.retry_prompt_template:
        template_path = repo_root / config.retry.retry_prompt_template
        if not template_path.exists():
            missing_templates.append(f"retry: {config.retry.retry_prompt_template}")

    for name, agent_cfg in config.agents.items():
        if agent_cfg.retry_prompt_template:
            template_path = repo_root / agent_cfg.retry_prompt_template
            if not template_path.exists():
                missing_templates.append(f"{name}: {agent_cfg.retry_prompt_template}")

    if missing_templates:
        return Check(
            name="Retry Templates",
            status="error",
            detail=f"Missing: {', '.join(missing_templates[:3])}" + ("..." if len(missing_templates) > 3 else ""),
        )
    if config.retry and config.retry.retry_prompt_template:
        return Check(name="Retry Templates", status="ok", detail="All found")
    return None


def _run_git(
    repo_root: Path,
    args: list[str],
    *,
    runner: CommandRunner | None,
):
    from ....adapters.git.git_cli import GitCLI, SubprocessCommandRunner

    try:
        git = GitCLI(runner=runner or SubprocessCommandRunner())
        return git.run(repo_root, args, timeout_s=10, check=False)
    except Exception:
        return None


def _repo_relative_path(repo_root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def _effective_worktree_seed_ref(config: Config, repo_root: Path) -> str:
    if config.worktree_seed_ref:
        return config.worktree_seed_ref

    from ....adapters.worktree._worktree import get_default_branch

    return f"origin/{get_default_branch(repo_root)}"


def _check_agent_prompts(
    config: Config,
    runner: CommandRunner | None = None,
) -> Check | None:
    """Ensure repo-local prompt files are available from the worktree seed ref."""
    repo_root = config.repo_root
    if not (repo_root / ".git").exists():
        return None

    seed_ref = _effective_worktree_seed_ref(config, repo_root)
    missing_from_head: list[str] = []
    modified_locally: list[str] = []

    for name, agent_cfg in config.agents.items():
        prompt_rel = _repo_relative_path(repo_root, agent_cfg.prompt_path)
        if prompt_rel is None:
            continue

        head_result = _run_git(
            repo_root,
            ["cat-file", "-e", f"{seed_ref}:{prompt_rel}"],
            runner=runner,
        )
        if head_result is None:
            return Check(
                name="Agent Prompts",
                status="info",
                detail="Could not verify whether prompt files are available from the worktree seed ref",
            )
        if head_result.returncode != 0:
            missing_from_head.append(f"{name}: {prompt_rel}")
            continue

        status_result = _run_git(
            repo_root,
            ["status", "--porcelain", "--", prompt_rel],
            runner=runner,
        )
        if status_result is None:
            return Check(
                name="Agent Prompts",
                status="info",
                detail="Could not verify whether prompt files have local changes",
            )
        if status_result.stdout.strip():
            modified_locally.append(f"{name}: {prompt_rel}")

    if missing_from_head:
        return Check(
            name="Agent Prompts",
            status="error",
            detail=(
                f"Not available from worktree seed ref {seed_ref}: {', '.join(missing_from_head[:3])}"
                f"{'...' if len(missing_from_head) > 3 else ''}; "
                "commit and push onboarding files to that ref, or set worktrees.seed_ref for local iteration before start"
            ),
        )

    if modified_locally:
        return Check(
            name="Agent Prompts",
            status="warning",
            detail=(
                f"Modified locally: {', '.join(modified_locally[:3])}"
                f"{'...' if len(modified_locally) > 3 else ''}; "
                f"agent worktrees use the committed seed ref version ({seed_ref})"
            ),
        )

    return Check(name="Agent Prompts", status="ok", detail=f"Available from seed ref {seed_ref}")


def check_agents(
    config: Config,
    runner: CommandRunner | None = None,
) -> list[Check]:
    checks: list[Check] = []
    agent_count = len(config.agents)

    if agent_count == 0:
        checks.append(Check(name="Agents", status="warning", detail="None configured"))
        return checks

    checks.append(Check(name="Agents", status="ok", detail=f"{agent_count} configured"))
    checks.append(_check_agent_scripts(config))

    prompt_check = _check_agent_prompts(config, runner)
    if prompt_check:
        checks.append(prompt_check)

    template_check = _check_retry_templates(config)
    if template_check:
        checks.append(template_check)

    return checks


def check_python_environment(repo_root: Path) -> Check:
    """Report whether this repo's venv resolves ``issue_orchestrator`` to itself.

    The orchestrator links the base repo's venv into every worktree it creates
    (``_link_repo_venv_into_worktree``). Anything that then runs ``uv sync`` or
    ``pip install -e .`` through that link reinstalls the *worktree's* project
    into the *shared* venv, rewriting the editable pointer. The venv keeps
    working until that worktree is deleted, at which point every import dangles
    -- including in unrelated repositories whose pre-push gate falls through to
    this interpreter.

    ``make`` refuses those mutations now (``scripts/venv_guard.sh``), but a venv
    poisoned before that guard existed, or by a tool outside make, stays broken
    until someone repoints it. Report it here so it is visible without starting
    the Control Center.
    """
    venv_python = repo_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return Check(
            name="Python environment",
            status="info",
            detail=f"No .venv in {repo_root}; using the ambient interpreter",
        )

    pointers = sorted((repo_root / ".venv").glob("lib/*/site-packages/*issue_orchestrator*.pth"))
    repair = (
        f"cd {repo_root} && uv pip install --python .venv/bin/python -e . --no-deps"
    )
    for pointer in pointers:
        try:
            target = Path(pointer.read_text().strip())
        except OSError as exc:
            return Check(
                name="Python environment",
                status="warning",
                detail=f"Could not read editable pointer {pointer.name}: {exc}",
            )
        if not target.exists():
            return Check(
                name="Python environment",
                status="error",
                detail=(
                    f"Editable install points at a MISSING path: {target}. "
                    f"That checkout was deleted while this venv still pointed at "
                    f"it, so every import fails. Repair: {repair}"
                ),
                expandable={"pointer": str(pointer), "target": str(target), "repair": repair},
            )
        if not target.is_relative_to(repo_root):
            return Check(
                name="Python environment",
                status="error",
                detail=(
                    f"Editable install points OUTSIDE this repo: {target}. "
                    f"Imports here silently resolve to another checkout's "
                    f"source. Repair: {repair}"
                ),
                expandable={"pointer": str(pointer), "target": str(target), "repair": repair},
            )
    return Check(
        name="Python environment",
        status="ok",
        detail=f"venv resolves issue_orchestrator to {repo_root}",
    )
