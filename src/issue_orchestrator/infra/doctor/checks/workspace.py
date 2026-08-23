"""Workspace and agent checks for doctor."""

import os
import shutil
from typing import Any
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


def check_python_environment(
    repo_root: Path,
    runner: CommandRunner | None = None,
) -> Check:
    """Report whether this repo's venv resolves issue_orchestrator to itself.

    An editable install records one absolute source path, so an environment can
    only ever point at a single checkout. When it points somewhere else -- or at
    a path that no longer exists -- every import here silently resolves to
    another checkout's source, or fails with a bare ModuleNotFoundError that
    names neither the pointer nor the missing directory.

    The authoritative question is what the interpreter actually imports, not
    what a .pth file says: probing covers a missing or corrupt install that
    leaves no pointer at all, and does not fault a valid non-editable install
    merely for having none.
    """
    venv_dir = repo_root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    repair = f"cd {repo_root} && uv pip install --python .venv/bin/python -e . --no-deps"

    # A dangling .venv must not be reported as an absent one. Both fail
    # ``exists()``, but "no venv, using the ambient interpreter" is benign while
    # a dangling link is the guard's BROKEN state: the environment is unusable
    # and anything creating over the link writes into a dead path.
    if venv_dir.is_symlink() and not venv_dir.exists():
        return Check(
            name="Python environment",
            status="error",
            detail=(
                f"{venv_dir} is a dangling symlink pointing at "
                f"{os.readlink(venv_dir)}, which no longer exists. The checkout "
                f"that owned this venv was deleted. Remove the link and rebuild: "
                f"rm {venv_dir} && make venv-fast"
            ),
            expandable={"venv": str(venv_dir), "points_at": os.readlink(venv_dir)},
        )

    if not venv_dir.exists():
        return Check(
            name="Python environment",
            status="info",
            detail=f"No .venv in {repo_root}; using the ambient interpreter",
        )

    if not venv_python.exists():
        # A .venv that exists but has no interpreter is incomplete or corrupt.
        # Reporting "no .venv, using the ambient interpreter" is both factually
        # wrong and hides the broken environment.
        return Check(
            name="Python environment",
            status="error",
            detail=(
                f"{venv_dir} exists but has no interpreter at "
                f"{venv_python}; the environment is incomplete. "
                f"Rebuild it: rm -rf {venv_dir} && make venv-fast"
            ),
            expandable={"venv": str(venv_dir), "missing": str(venv_python)},
        )

    pointers: dict[str, str] = {}
    try:
        for pointer in sorted(
            venv_dir.glob("lib/*/site-packages/*issue_orchestrator*.pth")
        ):
            pointers[pointer.name] = pointer.read_text().strip()
    except OSError as exc:
        # An unreadable pointer is a diagnosis, not a crash. Raising here took
        # the whole doctor run down instead of reporting the broken install.
        return Check(
            name="Python environment",
            status="error",
            detail=(
                f"Could not read the editable pointer in {venv_dir}: {exc}. "
                f"Repair: {repair}"
            ),
            expandable={"repair": repair, "error": str(exc)},
        )

    if runner is None:
        return Check(
            name="Python environment",
            status="info",
            detail="Interpreter probe unavailable (no command runner)",
            expandable={"pointers": pointers} if pointers else None,
        )

    probe = (
        "import issue_orchestrator, pathlib, sys; "
        "sys.stdout.write(str(pathlib.Path(issue_orchestrator.__file__).resolve().parent))"
    )
    result = runner.run(
        [str(venv_python), "-c", probe], cwd=repo_root, timeout_seconds=30
    )

    details: dict[str, Any] = {"repair": repair, "interpreter": str(venv_python)}
    if pointers:
        details["pointers"] = pointers

    if result.returncode != 0:
        missing = {
            name: target
            for name, target in pointers.items()
            if not Path(target).exists()
        }
        cause = (
            f" Its editable pointer targets a MISSING path: {sorted(missing.values())}."
            if missing
            else ""
        )
        return Check(
            name="Python environment",
            status="error",
            detail=(
                f"The venv interpreter cannot import issue_orchestrator.{cause} "
                f"Repair: {repair}"
            ),
            expandable={**details, "stderr": result.stderr.strip()[:500]},
        )

    resolved = Path(result.stdout.strip())
    if not resolved.is_relative_to(repo_root):
        return Check(
            name="Python environment",
            status="error",
            detail=(
                f"The venv imports issue_orchestrator from OUTSIDE this repo: "
                f"{resolved}. Imports here silently resolve to another "
                f"checkout's source. Repair: {repair}"
            ),
            expandable={**details, "resolved": str(resolved)},
        )

    return Check(
        name="Python environment",
        status="ok",
        detail=f"venv imports issue_orchestrator from {resolved}",
    )
