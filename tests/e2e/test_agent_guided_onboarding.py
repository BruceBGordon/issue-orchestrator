"""Opt-in live heavy E2E for agent-guided onboarding against GitHub."""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from issue_orchestrator.adapters.github import resolve_github_token
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.api_token import (
    read_existing_admin_token,
    read_existing_agent_callback_token,
)
from issue_orchestrator.testing.support.test_data import _ensure_label, close_issue
from tests.e2e.conftest import e2e_label, env_token_name, find_free_port
from tests.e2e.fixtures.orchestrator_process import OrchestratorProcess
from tests.e2e.flows import E2EFlow, start_orchestrator_runtime

logger = logging.getLogger(__name__)
LIVE_ONBOARDING_ENABLED = os.environ.get(
    "E2E_AGENT_GUIDED_ONBOARDING", ""
).strip().lower() in {"1", "true", "yes"}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.live_agent,
    pytest.mark.heavy_e2e,
    pytest.mark.requires_infra,
    pytest.mark.asyncio,
    pytest.mark.timeout(45 * 60),
    pytest.mark.xdist_group("live-agent"),
    pytest.mark.skipif(
        not LIVE_ONBOARDING_ENABLED,
        reason="Set E2E_AGENT_GUIDED_ONBOARDING=1 to run live agent-guided onboarding acceptance.",
    ),
]

WORK_AGENT_LABEL = "agent:onboarding"
_DEFAULT_PROGRESS_TIMEOUT_S = 180.0
_PROGRESS_TIMEOUT_ENV = "E2E_AGENT_GUIDED_ONBOARDING_PROGRESS_TIMEOUT_SECONDS"
_SENSITIVE_ENV_VARS = (
    "ISSUE_ORCH_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ISSUE_ORCHESTRATOR_API_TOKEN",
    "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN",
)


def _requested_providers() -> set[str]:
    raw = os.environ.get("E2E_AGENT_GUIDED_ONBOARDING_PROVIDERS", "codex")
    return {value.strip() for value in raw.split(",") if value.strip()}


def _provider_binary(provider_name: str) -> str:
    if provider_name == "codex":
        return "codex"
    if provider_name == "claude-code":
        return "claude"
    raise AssertionError(f"Unsupported provider: {provider_name}")


def _provider_available(provider_name: str) -> bool:
    return shutil.which(_provider_binary(provider_name)) is not None


def _issue_orchestrator_bin(source_root: Path) -> Path:
    preferred = source_root / ".venv" / "bin" / "issue-orchestrator"
    if preferred.exists():
        return preferred
    return Path(sys.executable).parent / "issue-orchestrator"


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )


def _sensitive_log_values() -> list[str]:
    values: list[str] = []
    for env_name in _SENSITIVE_ENV_VARS:
        value = os.environ.get(env_name)
        if value:
            values.append(value)

    admin_token = read_existing_admin_token()
    if admin_token:
        values.append(admin_token)

    agent_callback_token = read_existing_agent_callback_token()
    if agent_callback_token:
        values.append(agent_callback_token)

    try:
        github_token = resolve_github_token(configured_token=None)
    except Exception:
        github_token = None
    if github_token:
        values.append(github_token)

    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        if len(value) < 8 or value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def _scrub_sensitive_output(content: str) -> str:
    scrubbed = re.sub(
        r"(Authorization:\s*Bearer\s+)[^\s\"']+",
        r"\1[REDACTED]",
        content,
        flags=re.IGNORECASE,
    )
    scrubbed = re.sub(
        r"https://x-access-token:[^@\s]+@github\.com",
        "https://x-access-token:[REDACTED]@github.com",
        scrubbed,
    )
    for secret in _sensitive_log_values():
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    return scrubbed


def _write_log(path: Path, content: str) -> Path:
    path.write_text(_scrub_sensitive_output(content), encoding="utf-8")
    return path


def _onboarding_progress_observed(target_repo: Path) -> bool:
    if (target_repo / ".issue-orchestrator").exists():
        return True
    if (target_repo / ".prompts").exists():
        return True
    return bool(_git(target_repo, "status", "--short").stdout.strip())


def _authenticated_remote_url(repo_name: str) -> str:
    token = resolve_github_token(configured_token=None)
    return f"https://x-access-token:{token}@github.com/{repo_name}.git"


def _clone_target_repo(repo_name: str, target_repo: Path) -> None:
    auth_url = _authenticated_remote_url(repo_name)
    subprocess.run(
        ["git", "clone", "--quiet", auth_url, str(target_repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(target_repo, "remote", "set-url", "--push", "origin", auth_url)
    _git(target_repo, "checkout", "-B", "main")
    _git(target_repo, "config", "user.email", "e2e@example.com")
    _git(target_repo, "config", "user.name", "E2E Onboarding")


def _progress_timeout_seconds() -> float:
    raw_value = os.environ.get(_PROGRESS_TIMEOUT_ENV, "").strip()
    if not raw_value:
        return _DEFAULT_PROGRESS_TIMEOUT_S
    try:
        timeout = float(raw_value)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s value %r; using %.0fs default",
            _PROGRESS_TIMEOUT_ENV,
            raw_value,
            _DEFAULT_PROGRESS_TIMEOUT_S,
        )
        return _DEFAULT_PROGRESS_TIMEOUT_S
    if timeout <= 0:
        logger.warning(
            "Ignoring non-positive %s value %r; using %.0fs default",
            _PROGRESS_TIMEOUT_ENV,
            raw_value,
            _DEFAULT_PROGRESS_TIMEOUT_S,
        )
        return _DEFAULT_PROGRESS_TIMEOUT_S
    return timeout


def _prepare_pristine_onboarding_state(target_repo: Path) -> None:
    """Remove tracked or untracked onboarding artifacts so the agent starts from a clean slate."""
    candidate_paths = [
        Path("AGENTS.md"),
        Path("CLAUDE.md"),
        Path("GEMINI.md"),
        Path(".claude"),
        Path(".codex"),
        Path(".mcp.json"),
        Path(".issue-orchestrator"),
        Path(".prompts"),
        Path(".githooks"),
        Path("scripts/verify-pr.sh"),
    ]
    changed = False
    for rel_path in candidate_paths:
        abs_path = target_repo / rel_path
        if not abs_path.exists():
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(rel_path)],
            cwd=target_repo,
            capture_output=True,
            text=True,
        )
        if tracked.returncode == 0:
            rm_args = ["git", "rm", "-r", "-f", "--ignore-unmatch", str(rel_path)]
            subprocess.run(
                rm_args,
                cwd=target_repo,
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            if abs_path.is_dir():
                shutil.rmtree(abs_path)
            else:
                abs_path.unlink()
        changed = True

    if not changed:
        return

    status = _git(target_repo, "status", "--porcelain").stdout.strip()
    if status:
        _git(target_repo, "commit", "-m", "Prepare onboarding acceptance baseline")


def _build_onboarding_prompt(provider_name: str, source_root: Path) -> str:
    wizard_answers = [
        "Existing project - I have labels/issues already",
        "y",
        ".prompts/onboarding.md",
        "45",
    ]
    provider_specific = ""
    if provider_name == "codex":
        provider_specific = (
            "- For the work agent, choose provider `codex` and leave the model blank "
            "so Codex uses its current CLI default."
        )
        wizard_answers.extend(["codex", ""])
    elif provider_name == "claude-code":
        provider_specific = textwrap.dedent(
            """
            - For the work agent, choose provider `claude-code`, model `sonnet`, and permission mode `bypassPermissions`.
            - If the wizard asks for confirmation before bypassing permissions, confirm it.
            """
        ).strip()
        wizard_answers.extend(["claude-code", "sonnet", "bypassPermissions", "y"])

    wizard_answers.extend(
        [
            "n",
            "1",
            "milestone_number",
            "",
            "M0",
            "../",
            "",
            "y" if provider_name == "claude-code" else "",
            "web",
            "8080",
            "subprocess",
            "io",
            "true",
            "300",
            "n",
            "",
            "y",
            "y",
            "n",
        ]
    )
    wizard_answer_block = "\n".join(f"    {answer!r}," for answer in wizard_answers)

    return textwrap.dedent(
        f"""
        You are onboarding issue-orchestrator in the CURRENT git repository, which is the target repo.

        First read these two files from the issue-orchestrator source checkout:
        - {source_root / '.claude' / 'skills' / 'onboarding' / 'SKILL.md'}
        - {source_root / 'docs' / 'journeys' / 'agent-guided-onboarding.md'}

        Follow the agent-guided onboarding path for an EXISTING REPOSITORY.

        Requirements:
        - Use `issue-orchestrator setup`; do not hand-write the initial config instead of the wizard.
        - Drive the wizard hands-free with scripted stdin instead of leaving an interactive prompt open.
        - Use this exact pattern for the wizard step:

          ```bash
          python - <<'PY'
          import subprocess

          answers = [
{wizard_answer_block}
          ]
          wizard_input = "\\n".join(answers) + "\\n"
          subprocess.run(
              ["issue-orchestrator", "setup", "."],
              input=wizard_input,
              text=True,
              check=True,
          )
          PY
          ```

        - If the wizard does not detect an agent label automatically, add a manual work agent labeled `{WORK_AGENT_LABEL}`.
        - Use prompt file `.prompts/onboarding.md`.
        - Set timeout to `45` minutes for the work agent.
        {provider_specific}
        - This is not a review agent.
        - Set max concurrent sessions to `1`.
        - Use milestone sort `milestone_number`.
        - Leave milestone order blank.
        - Use foundation milestone `M0`.
        - Use worktree base `../`.
        - Leave worktree setup commands blank.
        - If you chose `claude-code`, enable trusted session interactions when the wizard offers them.
        - Use UI mode `web`, web port `8080`, and terminal backend `subprocess`.
        - Use label prefix `io`.
        - Set validation command to `true` with timeout `300`.
        - Disable Stage 1 review.
        - Keep the default config filename.
        - Apply the wizard changes.
        - Install repo guardrails / AI hooks when asked.
        - Do not set up provider API keys in the wizard.
        - After setup, run `issue-orchestrator doctor` and `issue-orchestrator init`.
        - Edit `.issue-orchestrator/config/modes/default/default.yaml` so `worktrees.seed_ref: HEAD`.
        - Commit all generated onboarding files locally with commit message `Add onboarding files`.
        - Do not push.
        - Stop once the onboarding files are committed and `issue-orchestrator doctor` passes.

        Reply with a short summary when finished.
        """
    ).strip()


def _run_agent_guided_setup(
    *,
    provider_name: str,
    source_root: Path,
    target_repo: Path,
    log_dir: Path,
) -> subprocess.CompletedProcess[str]:
    prompt = _build_onboarding_prompt(provider_name, source_root)
    env = dict(os.environ)
    issue_orchestrator_bin = _issue_orchestrator_bin(source_root)
    env["PATH"] = f"{issue_orchestrator_bin.parent}:{env.get('PATH', '')}"
    env["PYTHONUNBUFFERED"] = "1"

    if provider_name == "codex":
        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--color",
            "never",
            prompt,
        ]
    elif provider_name == "claude-code":
        cmd = [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            prompt,
        ]
    else:
        raise AssertionError(f"Unsupported provider: {provider_name}")

    stdout_log = log_dir / f"{provider_name}-setup.stdout.log"
    stderr_log = log_dir / f"{provider_name}-setup.stderr.log"
    progress_timeout_s = _progress_timeout_seconds()
    try:
        process = subprocess.Popen(
            cmd,
            cwd=target_repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        start = time.monotonic()
        while process.poll() is None:
            if _onboarding_progress_observed(target_repo):
                break
            if time.monotonic() - start >= progress_timeout_s:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                _write_log(stdout_log, stdout)
                _write_log(stderr_log, stderr)
                raise AssertionError(
                    f"{provider_name} onboarding agent made no onboarding progress within {int(progress_timeout_s)} seconds.\n"
                    f"Expected generated files or a dirty worktree under {target_repo}.\n"
                    f"stdout log: {stdout_log}\n"
                    f"stderr log: {stderr_log}"
                )
            time.sleep(2)
        stdout, stderr = process.communicate(timeout=20 * 60)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        _write_log(stdout_log, stdout)
        _write_log(stderr_log, stderr)
        raise AssertionError(
            f"{provider_name} onboarding agent timed out after 20 minutes.\n"
            f"stdout log: {stdout_log}\n"
            f"stderr log: {stderr_log}"
        ) from exc

    result = subprocess.CompletedProcess(
        args=cmd,
        returncode=process.returncode or 0,
        stdout=stdout,
        stderr=stderr,
    )
    _write_log(stdout_log, result.stdout)
    _write_log(stderr_log, result.stderr)
    if result.returncode != 0:
        raise AssertionError(
            f"{provider_name} onboarding agent failed with exit code {result.returncode}.\n"
            f"stdout log: {stdout_log}\n"
            f"stderr log: {stderr_log}\n"
            f"stdout (truncated): {result.stdout[-2000:]}\n"
            f"stderr (truncated): {result.stderr[-2000:]}"
        )
    return result


def _run_doctor(source_root: Path, target_repo: Path, config_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_issue_orchestrator_bin(source_root)), "--config", str(config_path), "doctor"],
        cwd=target_repo,
        capture_output=True,
        text=True,
        timeout=5 * 60,
        env=dict(os.environ),
    )


def _configured_work_agent(config: Config) -> str:
    if WORK_AGENT_LABEL in config.agents:
        return WORK_AGENT_LABEL
    for agent_label in config.agents:
        if agent_label.startswith("agent:"):
            return agent_label
    raise AssertionError("Expected at least one configured work agent")


@pytest.mark.parametrize("provider_name", ["codex", "claude-code"])
@pytest.mark.gh_activity_limit(test_gh_activity_limit=450, system_gh_activity_limit=150)
async def test_live_agent_guided_existing_repo_onboarding_launches_first_issue(
    provider_name: str,
    repo_name: str,
    tmp_path: Path,
):
    """Let a real AI agent onboard a GitHub repo, then prove the first issue can launch."""
    if provider_name not in _requested_providers():
        pytest.skip(
            f"{provider_name} not requested. Set E2E_AGENT_GUIDED_ONBOARDING_PROVIDERS to include it."
        )
    if not _provider_available(provider_name):
        pytest.skip(f"{provider_name} CLI not installed")

    source_root = Path(__file__).resolve().parents[2]
    target_repo = tmp_path / f"agent-guided-{provider_name.replace('-', '_')}"
    log_dir = tmp_path / "agent-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    _clone_target_repo(repo_name, target_repo)
    _prepare_pristine_onboarding_state(target_repo)

    _ensure_label(repo_name, WORK_AGENT_LABEL)
    base_head = _git(target_repo, "rev-parse", "HEAD").stdout.strip()

    logger.info(
        "[E2E ONBOARDING] provider=%s repo=%s target_repo=%s",
        provider_name,
        repo_name,
        target_repo,
    )
    _run_agent_guided_setup(
        provider_name=provider_name,
        source_root=source_root,
        target_repo=target_repo,
        log_dir=log_dir,
    )

    current_head = _git(target_repo, "rev-parse", "HEAD").stdout.strip()
    assert current_head != base_head, "Expected onboarding agent to commit generated files"
    assert _git(target_repo, "log", "-1", "--format=%s").stdout.strip() == "Add onboarding files"

    config_path = target_repo / ".issue-orchestrator" / "config" / "default.yaml"
    assert config_path.exists(), "Expected default onboarding config to be created"
    assert (target_repo / ".prompts" / "onboarding.md").exists(), "Expected onboarding prompt file"

    doctor_result = _run_doctor(source_root, target_repo, config_path)
    assert doctor_result.returncode == 0, (
        "Expected onboarding config to pass doctor.\n"
        f"stdout:\n{doctor_result.stdout}\n"
        f"stderr:\n{doctor_result.stderr}"
    )

    onboarded_config = Config.load(config_path)
    assert onboarded_config.worktree_seed_ref == "HEAD"
    work_agent_label = _configured_work_agent(onboarded_config)
    assert onboarded_config.agents[work_agent_label].provider == provider_name
    if provider_name == "claude-code":
        assert (
            onboarded_config.agents[work_agent_label].provider_args.get("permission_mode")
            == "bypassPermissions"
        )
        assert onboarded_config.session_interactions.enabled is True

    runtime_config = copy.deepcopy(onboarded_config)
    runtime_config.repo_root = target_repo
    runtime_config.repo = repo_name
    runtime_config.worktree_base = tmp_path / f"{provider_name}-worktrees"
    runtime_config.worktree_seed_ref = "HEAD"
    runtime_config.max_concurrent_sessions = 1
    runtime_config.session_timeout_minutes = 10
    runtime_config.queue_refresh_seconds = 600
    runtime_config.control_api_port = find_free_port()
    runtime_config.web_port = find_free_port()
    runtime_config.github_token_env = env_token_name()

    run_suffix = str(int(time.time()))
    filter_label = e2e_label(f"agent-guided-onboarding-{provider_name}-{run_suffix}")
    issue_tag = e2e_label(f"agent-guided-onboarding-case-{provider_name}-{run_suffix}")
    runtime_config.filtering.label = filter_label
    runtime_config.e2e_pr_labels = [filter_label]

    runtime = None
    flow: E2EFlow | None = None
    issue_number: int | None = None

    try:
        orchestrator = OrchestratorProcess(runtime_config, target_repo)
        runtime = await start_orchestrator_runtime(
            orchestrator,
            runtime_config.control_api_port,
            max_issues=1,
        )
        flow = E2EFlow(
            repo=repo_name,
            watcher=runtime.watcher,
            filter_label=filter_label,
            fail_on_blocked_failed=True,
        )

        issue_title = (
            f"[M0-930] E2E agent-guided onboarding launch check ({provider_name})"
        )
        issue_body = textwrap.dedent(
            f"""
            Validate the first issue launch after agent-guided onboarding.

            Provider under test: {provider_name}
            Goal: prove the orchestrator can see the issue and launch a session.
            """
        ).strip()
        issue_key, issue_number = flow.create_issue(
            issue_title,
            [work_agent_label, issue_tag],
            body=issue_body,
        )

        await flow.issue_seen(issue_key, timeout_s=180)
        await flow.session_started(issue_key, timeout_s=10 * 60)
    finally:
        if flow is not None:
            flow.cleanup_created_issues()
        elif issue_number is not None:
            close_issue(repo_name, issue_number, "Live onboarding acceptance cleanup")
        if runtime is not None:
            await runtime.close()
