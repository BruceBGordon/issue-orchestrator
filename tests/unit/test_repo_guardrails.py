"""Tests for repo-level guardrail installation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_guardrails import (
    LEGACY_MANAGED_HELPER_MARKER,
    LEGACY_MANAGED_PRE_PUSH_MARKER,
    LEGACY_MANAGED_VERIFY_MARKER,
    MANAGED_PRE_PUSH_MARKER,
    POST_VERIFY_HOOK_RELATIVE_PATH,
    RepoGuardrailsError,
    inspect_repo_guardrails,
    quarantine_managed_hook_file,
    setup_repo_guardrails,
    _render_helper_script,
    _render_repo_pre_push_hook,
    _render_verify_pr_script,
)


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_config(repo: Path) -> Config:
    config = Config(repo_root=repo)
    config.validation.publish.cmd = "make validate-pr"
    config.agents = {
        "agent:dev": AgentConfig(
            prompt_path=repo / "prompt.md",
            command="claude --print",
        )
    }
    return config


def _make_loaded_config(
    repo: Path,
    *,
    config_name: str = "main.yaml",
    mode: str | None = None,
) -> Config:
    config_dir = repo / ".issue-orchestrator" / "config"
    if mode is not None:
        config_dir = config_dir / "modes" / mode
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / config_name
    config_path.write_text(
        """
validation:
  publish:
    cmd: "make validate-pr"
""".lstrip()
    )
    config = _make_config(repo)
    config.config_path = config_path
    return config


def test_setup_repo_guardrails_installs_repo_guardrails_and_agent_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    config = _make_config(repo)
    result = setup_repo_guardrails(config)

    hooks_path = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert hooks_path == ".githooks"
    assert result.pre_push_hook == repo / ".githooks" / "pre-push"
    assert result.pre_push_hook.exists()
    assert result.verify_script.exists()
    assert result.helper_script.exists()
    assert (repo / ".claude" / "hooks" / "block-no-verify.sh").exists()
    assert (repo / ".claude" / "settings.json").exists()

    hook_result = subprocess.run(
        [str(repo / ".claude" / "hooks" / "block-no-verify.sh")],
        cwd=repo,
        input=json.dumps(
            {"tool_input": {"command": "git config --local core.hooksPath /dev/null"}}
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert hook_result.returncode == 2
    assert "BLOCKED" in hook_result.stderr

    status = inspect_repo_guardrails(repo, config=config)
    assert status.pre_push_managed
    assert status.pre_push_calls_verify
    assert status.verify_managed
    assert status.helper_executable
    assert status.helper_managed
    assert status.agent_hooks["claude-code"].installed
    assert all(
        managed.exists and managed.executable and managed.matches_template is True
        for managed in status.agent_hooks["claude-code"].managed_files
    )


def test_setup_repo_guardrails_preserves_existing_pre_push_hook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", ".githooks"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    original_hook = hooks_dir / "pre-push"
    original_hook.write_text("#!/usr/bin/env bash\necho original-hook\n")
    original_hook.chmod(0o755)

    result = setup_repo_guardrails(_make_config(repo))

    preserved = hooks_dir / "pre-push.project"
    assert preserved in result.preserved_files
    assert preserved.read_text() == "#!/usr/bin/env bash\necho original-hook\n"
    assert "scripts/verify-pr.sh" in result.pre_push_hook.read_text()


def test_inspect_repo_guardrails_recognizes_legacy_markers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", ".githooks"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    pre_push = hooks_dir / "pre-push"
    pre_push.write_text(
        f"#!/usr/bin/env bash\n# {LEGACY_MANAGED_PRE_PUSH_MARKER}\n"
        "scripts/verify-pr.sh\n"
    )
    pre_push.chmod(0o755)
    verify_script = repo / "scripts" / "verify-pr.sh"
    verify_script.parent.mkdir(parents=True)
    verify_script.write_text(f"#!/usr/bin/env bash\n# {LEGACY_MANAGED_VERIFY_MARKER}\n")
    helper_script = repo / "scripts" / "agent-hooks" / "block_no_verify.py"
    helper_script.parent.mkdir(parents=True)
    helper_script.write_text(f"#!/usr/bin/env python3\n# {LEGACY_MANAGED_HELPER_MARKER}\n")

    status = inspect_repo_guardrails(repo)

    assert status.pre_push_managed
    assert status.verify_managed
    assert status.helper_managed


def test_setup_repo_guardrails_migrates_legacy_wrapper_without_overwriting_project_hook(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", ".githooks"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    pre_push = hooks_dir / "pre-push"
    pre_push.write_text(
        f"#!/usr/bin/env bash\n# {LEGACY_MANAGED_PRE_PUSH_MARKER}\n"
        "scripts/verify-pr.sh\n"
    )
    pre_push.chmod(0o755)
    project_hook = hooks_dir / "pre-push.project"
    original_project_hook = "#!/usr/bin/env bash\necho original-project-hook\n"
    project_hook.write_text(original_project_hook)
    project_hook.chmod(0o755)

    result = setup_repo_guardrails(_make_config(repo))

    assert project_hook.read_text() == original_project_hook
    assert project_hook not in result.preserved_files
    migrated_pre_push = result.pre_push_hook.read_text()
    assert MANAGED_PRE_PUSH_MARKER in migrated_pre_push
    assert f"# {LEGACY_MANAGED_PRE_PUSH_MARKER}" not in migrated_pre_push


def test_setup_repo_guardrails_recovers_from_external_existing_hooks_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    external_hooks = tmp_path / "external-hooks"
    external_hooks.mkdir()
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", str(external_hooks)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    config = _make_config(repo)
    result = setup_repo_guardrails(config)

    hooks_path = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert hooks_path == ".githooks"
    assert result.hooks_dir == repo / ".githooks"
    assert result.pre_push_hook.exists()
    assert result.verify_script.exists()
    assert result.helper_script.exists()


def test_setup_repo_guardrails_rejects_explicit_external_hooks_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    external_hooks = tmp_path / "external-hooks"
    external_hooks.mkdir()

    with pytest.raises(
        RepoGuardrailsError,
        match="core.hooksPath must resolve inside the repository",
    ):
        setup_repo_guardrails(_make_config(repo), hooks_path=str(external_hooks))


def test_setup_repo_guardrails_rejects_non_repo_local_config_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    config = _make_config(repo)
    external_config = tmp_path / "external.yaml"
    external_config.write_text("validation:\n  publish:\n    cmd: make validate-pr\n")
    config.config_path = external_config

    with pytest.raises(
        RepoGuardrailsError,
        match="must live under",
    ):
        setup_repo_guardrails(config)


def test_checked_in_helper_matches_generated_output() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source_path = repo_root / "src" / "issue_orchestrator" / "infra" / "hooks" / "block_no_verify.py"
    helper_path = repo_root / "scripts" / "agent-hooks" / "block_no_verify.py"

    assert helper_path.read_text() == _render_helper_script(source_path)


def test_checked_in_verify_pr_matches_portable_generated_output() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    verify_path = repo_root / "scripts" / "verify-pr.sh"

    assert verify_path.read_text() == _render_verify_pr_script(
        "make validate-pr-raw",
        selected_config_name="modes/default/main.yaml",
    )


def test_render_repo_pre_push_hook_uses_repo_root_relative_path() -> None:
    repo_root = Path("/tmp/example-repo")
    verify_script = repo_root / "scripts" / "gates" / "verify-pr.sh"

    rendered = _render_repo_pre_push_hook(verify_script, repo_root)

    assert 'VERIFY_SCRIPT="$REPO_ROOT/scripts/gates/verify-pr.sh"' in rendered


def test_render_verify_pr_script_bakes_python_when_requested() -> None:
    rendered = _render_verify_pr_script(
        "make validate-pr",
        baked_python="/tmp/issue-orchestrator-python",
    )

    assert '/tmp/issue-orchestrator-python' in rendered


def test_render_verify_pr_script_exports_selected_config_name() -> None:
    rendered = _render_verify_pr_script(
        "make validate-pr",
        selected_config_name="main.yaml",
    )

    assert "export ISSUE_ORCHESTRATOR_CONFIG_NAME=main.yaml" in rendered


def test_render_verify_pr_script_exports_complete_mode_selection() -> None:
    rendered = _render_verify_pr_script(
        "make validate-pr",
        selected_config_name="modes/codex/main.yaml",
    )

    assert "export ISSUE_ORCHESTRATOR_CONFIG_NAME=main.yaml" in rendered
    assert "export ISSUE_ORCHESTRATOR_MODE=codex" in rendered
    assert "modes/codex/main.yaml" in rendered


def test_verify_script_preserves_complete_runtime_mode_selection(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    scripts = repo / "scripts"
    scripts.mkdir()
    verify_script = scripts / "verify-pr.sh"
    verify_script.write_text(
        _render_verify_pr_script(
            "make validate-pr",
            selected_config_name="modes/default/main.yaml",
        )
    )
    verify_script.chmod(0o755)
    capture = tmp_path / "selection.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s|%s|%s\\n' \"$ISSUE_ORCHESTRATOR_MODE\" "
        "\"$ISSUE_ORCHESTRATOR_CONFIG_NAME\" "
        "\"$ISSUE_ORCHESTRATOR_CONFIG_PATH\" > \"$CAPTURE_PATH\"\n"
    )
    fake_python.chmod(0o755)
    runtime_path = repo / ".issue-orchestrator/config/modes/codex/codex.yaml"
    env = {
        **os.environ,
        "CAPTURE_PATH": str(capture),
        "ISSUE_ORCHESTRATOR_PYTHON": str(fake_python),
        "ISSUE_ORCHESTRATOR_MODE": "codex",
        "ISSUE_ORCHESTRATOR_CONFIG_NAME": "codex.yaml",
        "ISSUE_ORCHESTRATOR_CONFIG_PATH": str(runtime_path),
    }

    subprocess.run([str(verify_script)], cwd=repo, env=env, check=True)

    assert capture.read_text().strip() == f"codex|codex.yaml|{runtime_path}"


def test_verify_script_supplies_baked_selection_for_human_push(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    scripts = repo / "scripts"
    scripts.mkdir()
    verify_script = scripts / "verify-pr.sh"
    verify_script.write_text(
        _render_verify_pr_script(
            "make validate-pr",
            selected_config_name="modes/default/main.yaml",
        )
    )
    verify_script.chmod(0o755)
    capture = tmp_path / "selection.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s|%s|%s\\n' \"$ISSUE_ORCHESTRATOR_MODE\" "
        "\"$ISSUE_ORCHESTRATOR_CONFIG_NAME\" "
        "\"$ISSUE_ORCHESTRATOR_CONFIG_PATH\" > \"$CAPTURE_PATH\"\n"
    )
    fake_python.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "ISSUE_ORCHESTRATOR_MODE",
            "ISSUE_ORCHESTRATOR_CONFIG_NAME",
            "ISSUE_ORCHESTRATOR_CONFIG_PATH",
        }
    }
    env.update(
        {
            "CAPTURE_PATH": str(capture),
            "ISSUE_ORCHESTRATOR_PYTHON": str(fake_python),
        }
    )

    subprocess.run([str(verify_script)], cwd=repo, env=env, check=True)

    expected_path = repo / ".issue-orchestrator/config/modes/default/main.yaml"
    assert capture.read_text().strip() == f"default|main.yaml|{expected_path}"


def test_setup_repo_guardrails_uses_portable_verify_script_for_issue_orchestrator_shape(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "src" / "issue_orchestrator" / "entrypoints").mkdir(parents=True)
    (repo / "src" / "issue_orchestrator" / "entrypoints" / "cli.py").write_text("")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "pre-push").write_text("#!/usr/bin/env bash\n")

    result = setup_repo_guardrails(_make_loaded_config(repo))

    rendered = result.verify_script.read_text()
    assert sys.executable not in rendered
    assert '.venv/bin/python' in rendered
    assert "export ISSUE_ORCHESTRATOR_CONFIG_NAME=main.yaml" in rendered


def test_setup_repo_guardrails_preserves_mode_in_portable_verify_script(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "src" / "issue_orchestrator" / "entrypoints").mkdir(parents=True)
    (repo / "src" / "issue_orchestrator" / "entrypoints" / "cli.py").write_text("")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "pre-push").write_text("#!/usr/bin/env bash\n")

    result = setup_repo_guardrails(_make_loaded_config(repo, mode="codex"))

    rendered = result.verify_script.read_text()
    assert "export ISSUE_ORCHESTRATOR_MODE=codex" in rendered
    assert "modes/codex/main.yaml" in rendered


def test_render_verify_pr_script_uses_cache_aware_prepush_check() -> None:
    rendered = _render_verify_pr_script("make validate-pr")

    assert "prepush_check -v" in rendered
    assert "verify-pr: running cache-aware pre-push validation" in rendered
    assert "validation_cmd=" not in rendered
    assert 'bash -lc "$validation_cmd"' not in rendered


def test_managed_pre_push_hook_contains_recursion_guard() -> None:
    repo_root = Path("/tmp/example-repo")
    rendered = _render_repo_pre_push_hook(
        repo_root / "scripts" / "verify-pr.sh", repo_root
    )

    assert MANAGED_PRE_PUSH_MARKER in rendered
    assert LEGACY_MANAGED_PRE_PUSH_MARKER in rendered
    assert "is_managed_wrapper" in rendered
    assert "managed-marker-detected" in rendered
    # Syntax-check the rendered script.
    result = subprocess.run(
        ["bash", "-n"], input=rendered, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_render_repo_pre_push_hook_omits_post_verify_without_hook(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    verify_script = repo_root / "scripts" / "verify-pr.sh"
    verify_script.parent.mkdir(parents=True)
    verify_script.write_text("#!/usr/bin/env bash\n")

    rendered = _render_repo_pre_push_hook(verify_script, repo_root)

    assert "run_post_verify_hook" not in rendered
    assert POST_VERIFY_HOOK_RELATIVE_PATH.as_posix() not in rendered
    assert "is_managed_wrapper" in rendered
    assert "managed-marker-detected" in rendered
    assert "verify-pr-starting" in rendered
    result = subprocess.run(
        ["bash", "-n"], input=rendered, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_render_repo_pre_push_hook_includes_post_verify_when_hook_exists(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    verify_script = repo_root / "scripts" / "verify-pr.sh"
    verify_script.parent.mkdir(parents=True)
    verify_script.write_text("#!/usr/bin/env bash\n")
    post_verify_hook = repo_root / POST_VERIFY_HOOK_RELATIVE_PATH
    post_verify_hook.parent.mkdir(parents=True)
    post_verify_hook.write_text("#!/usr/bin/env bash\n")

    rendered = _render_repo_pre_push_hook(verify_script, repo_root)

    assert "run_post_verify_hook" in rendered
    assert 'run_post_verify_hook "$@"' in rendered
    assert POST_VERIFY_HOOK_RELATIVE_PATH.as_posix() in rendered
    result = subprocess.run(
        ["bash", "-n"], input=rendered, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_checked_in_githooks_pre_push_matches_generated_wrapper() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    rendered = _render_repo_pre_push_hook(
        repo_root / "scripts" / "verify-pr.sh", repo_root
    )

    assert (repo_root / ".githooks" / "pre-push").read_text() == rendered


def test_setup_repo_guardrails_quarantines_corrupt_project_hook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", ".githooks"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    corrupt = hooks_dir / "pre-push.project"
    # A prior bug (or manual mis-copy) left the managed wrapper masquerading as
    # the project hook. If left alone, the installed wrapper would exec this
    # file and recurse forever.
    corrupt.write_text(
        f"#!/usr/bin/env bash\n# {MANAGED_PRE_PUSH_MARKER}\n"
        f'"$HOOK_DIR/pre-push.project" "$@"\n'
    )
    corrupt.chmod(0o755)

    result = setup_repo_guardrails(_make_config(repo))

    assert not corrupt.exists(), "corrupt pre-push.project must be renamed aside"
    quarantined = list(hooks_dir.glob("pre-push.project.quarantined-*"))
    assert len(quarantined) == 1
    assert quarantined[0] in result.quarantined_files
    # Fresh install still produces a managed wrapper at pre-push.
    assert MANAGED_PRE_PUSH_MARKER in result.pre_push_hook.read_text()


def test_quarantine_managed_hook_file_is_noop_when_not_managed(tmp_path: Path) -> None:
    target = tmp_path / "pre-push.project"
    target.write_text("#!/usr/bin/env bash\necho real project hook\n")
    result = quarantine_managed_hook_file(target)
    assert result is None
    assert target.exists(), "benign files must not be quarantined"


def test_quarantine_managed_hook_file_handles_legacy_marker(tmp_path: Path) -> None:
    target = tmp_path / "pre-push.project"
    target.write_text(f"# {LEGACY_MANAGED_PRE_PUSH_MARKER}\n")

    result = quarantine_managed_hook_file(target)

    assert result is not None
    assert result.exists()
    assert LEGACY_MANAGED_PRE_PUSH_MARKER in result.read_text()
    assert not target.exists()


def test_quarantine_managed_hook_file_handles_existing_collision(tmp_path: Path) -> None:
    target = tmp_path / "pre-push.project"
    target.write_text(f"# {MANAGED_PRE_PUSH_MARKER}\n")
    # Simulate a prior quarantine that collides (same timestamp, same minute).
    # quarantine helper must still succeed without overwriting the prior file.
    target_sibling = tmp_path / "pre-push.project.quarantined-20260417T054301Z"
    target_sibling.write_text("prior quarantine\n")

    first = quarantine_managed_hook_file(target)
    assert first is not None
    assert first.exists()
    assert target_sibling.read_text() == "prior quarantine\n"
    assert not target.exists()


def test_setup_repo_guardrails_refreshes_drifted_agent_hook_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    config = _make_config(repo)
    setup_repo_guardrails(config)

    hook_path = repo / ".claude" / "hooks" / "block-no-verify.sh"
    hook_path.write_text("#!/usr/bin/env bash\necho drifted\n")
    hook_path.chmod(0o755)

    status_before = inspect_repo_guardrails(repo, config=config)
    assert (
        status_before.agent_hooks["claude-code"].managed_files[0].matches_template
        is False
    )

    setup_repo_guardrails(config)

    status_after = inspect_repo_guardrails(repo, config=config)
    assert (
        status_after.agent_hooks["claude-code"].managed_files[0].matches_template
        is True
    )
