"""Config checks for doctor."""

from pathlib import Path
from typing import Optional

from ..types import Check
from ...config import Config
from ...config_paths import require_engine_launch_config_path


def load_config_with_checks(
    config: Optional[Config],
    config_path: Optional[Path],
) -> tuple[Optional[Config], list[Check], bool]:
    checks: list[Check] = []

    if config is None and config_path:
        if config_path.exists():
            try:
                config = Config.load(config_path)
                require_engine_launch_config_path(config_path)
                checks.append(Check(
                    name="Config File",
                    status="ok",
                    detail=str(config_path),
                ))
            except Exception as e:
                checks.append(Check(
                    name="Config File",
                    status="error",
                    detail=f"Failed to load: {e}",
                ))
                return None, checks, True
        else:
            checks.append(Check(
                name="Config File",
                status="warning",
                detail="Not found",
            ))
            return None, checks, True

    if config is None:
        from ...config import list_configs, get_config_path

        cwd = Path.cwd()
        available = list_configs(cwd)
        if available:
            config_file = get_config_path(cwd, available[0])
            try:
                config = Config.load(config_file)
                require_engine_launch_config_path(config_file)
                checks.append(Check(
                    name="Config File",
                    status="ok",
                    detail=str(config_file.relative_to(cwd)),
                ))
            except Exception as e:
                checks.append(Check(
                    name="Config File",
                    status="error",
                    detail=f"Failed to load {config_file}: {e}",
                ))
                return None, checks, True
        else:
            checks.append(Check(
                name="Config File",
                status="warning",
                detail="Not found in current directory",
            ))
            return None, checks, True

    if config.config_path is not None:
        try:
            require_engine_launch_config_path(config.config_path)
        except ValueError as exc:
            checks.append(Check(
                name="Config File",
                status="error",
                detail=f"Failed to load: {exc}",
            ))
            return None, checks, True

    return config, checks, False


def check_config_validation(config: Config) -> list[Check]:
    checks: list[Check] = []

    validation_errors = config.validate()
    if validation_errors:
        checks.append(Check(
            name="Config Validation",
            status="error",
            detail="; ".join(validation_errors[:3]) + ("..." if len(validation_errors) > 3 else ""),
        ))
    else:
        checks.append(Check(
            name="Config Validation",
            status="ok",
            detail="All checks passed",
        ))

    return checks


def check_config_schema(config: Config) -> list[Check]:
    checks: list[Check] = []

    unknown_fields = config.validate_unknown_fields()
    if unknown_fields:
        field_names = [f[0] for f in unknown_fields]
        detail = ", ".join(field_names[:5]) + ("..." if len(field_names) > 5 else "")
        checks.append(Check(
            name="Config Schema",
            status="error",
            detail=f"Unknown fields: {detail}",
        ))
    else:
        checks.append(Check(
            name="Config Schema",
            status="ok",
            detail="No unknown fields",
        ))

    return checks


def check_template_variables(config: Config) -> list[Check]:
    checks: list[Check] = []

    invalid_templates = config.validate_template_variables()
    if invalid_templates:
        details = []
        for agent_label, field_name, bad_vars in invalid_templates[:3]:
            details.append(f"{agent_label}.{field_name}: {{{', '.join(sorted(bad_vars))}}}")
        detail = "; ".join(details) + ("..." if len(invalid_templates) > 3 else "")
        checks.append(Check(
            name="Template Variables",
            status="error",
            detail=f"Invalid: {detail}",
        ))
    else:
        checks.append(Check(
            name="Template Variables",
            status="ok",
            detail="All template variables valid",
        ))

    return checks


def check_repository_config(config: Config) -> list[Check]:
    if config.repo:
        return [Check(
            name="Repository",
            status="ok",
            detail=config.repo,
        )]

    # Try auto-detecting from git remote
    try:
        from ....adapters.github.repo import get_repo_from_git
        repo = get_repo_from_git()
        return [Check(
            name="Repository",
            status="ok",
            detail=f"Auto-detected: {repo}",
        )]
    except Exception:
        pass

    return [Check(
        name="Repository",
        status="warning",
        detail="Not configured and could not auto-detect from git remote",
    )]


def check_worktree_remediation(config: Config) -> list[Check]:
    detail = (
        f"pr_collision={config.worktree_remediation_pr_collision}, "
        f"push_rebase_retry={'on' if config.worktree_remediation_push_rebase_retry else 'off'}"
    )
    return [Check(
        name="Worktree Remediation",
        status="ok",
        detail=detail,
    )]
