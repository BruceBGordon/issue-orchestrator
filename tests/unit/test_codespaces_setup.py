from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from issue_orchestrator.infra.config import Config


REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_PREPARATION = REPO_ROOT / ".devcontainer" / "prepare-image.sh"
ONBOARDING_SEED = REPO_ROOT / ".devcontainer" / "seed-agent-onboarding.sh"


def _run_onboarding_seed(home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    return subprocess.run(
        [str(ONBOARDING_SEED)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_codespaces_config_loads_with_stable_web_ports() -> None:
    config_path = (
        REPO_ROOT
        / ".issue-orchestrator"
        / "config"
        / "modes"
        / "default"
        / "z-codespaces.yaml"
    )

    config = Config.load(config_path)

    assert config.web_port == 8080
    assert config.control_api_port == 19081
    assert config.terminal_adapter == "subprocess"
    assert config.validation.quick.cmd == "make validate-quick"
    assert config.validation.quick.timeout_seconds == 600
    assert config.validation.publish.cmd == "make validate-pr-raw"
    assert config.validation.publish.timeout_seconds == 1800

    goal_pilot_agent = config.goal_pilot.agent
    assert config.goal_pilot.enabled is False
    assert goal_pilot_agent is not None
    assert config.agents[goal_pilot_agent].provider == "codex"

    enabled_providers = {
        agent.provider
        for label, agent in config.agents.items()
        if label != goal_pilot_agent
    }
    assert enabled_providers == {"claude-code"}


def test_main_config_uses_raw_validate_pr_as_publish_gate() -> None:
    config_path = (
        REPO_ROOT
        / ".issue-orchestrator"
        / "config"
        / "modes"
        / "default"
        / "main.yaml"
    )

    config = Config.load(config_path)

    assert config.validation.quick.cmd == "make validate-quick"
    assert config.validation.quick.timeout_seconds == 600
    assert config.validation.publish.cmd == "make validate-pr-raw"
    assert config.validation.publish.timeout_seconds == 1800


def test_devcontainer_forwards_codespaces_ports() -> None:
    devcontainer_path = REPO_ROOT / ".devcontainer" / "devcontainer.json"

    data = json.loads(devcontainer_path.read_text(encoding="utf-8"))

    assert data["forwardPorts"] == [19080, 19081, 8080]
    assert data["portsAttributes"]["19080"]["label"] == "Issue Orchestrator Control Center"
    assert data["portsAttributes"]["8080"]["label"] == "Issue Orchestrator Engine Dashboard"


def test_expensive_setup_is_not_in_the_hook_a_prebuild_skips() -> None:
    """Dependency installation must not sit in ``postCreateCommand`` (#7100).

    A prebuild bakes ``onCreateCommand`` and ``updateContentCommand`` but NOT
    ``postCreateCommand``. Putting ``make worktree-setup`` there is why
    enabling prebuilds appears to change nothing — the expensive work runs on
    every create regardless. This regresses silently and invisibly, because the
    only symptom is a slow codespace, so it is pinned here rather than left to
    review.
    """
    data = json.loads(
        (REPO_ROOT / ".devcontainer" / "devcontainer.json").read_text(
            encoding="utf-8"
        )
    )

    assert data["onCreateCommand"] == ".devcontainer/prepare-image.sh"
    assert data["updateContentCommand"] == "make worktree-setup"
    assert "worktree-setup" not in data.get("postCreateCommand", "")
    # postStartCommand runs on EVERY resume, so it must stay empty.
    assert not data.get("postStartCommand")


def test_image_preparation_repairs_apt_and_installs_pinned_codex() -> None:
    text = IMAGE_PREPARATION.read_text(encoding="utf-8")

    assert IMAGE_PREPARATION.stat().st_mode & 0o111, "image setup must be executable"
    assert "sudo rm -f -- /etc/apt/sources.list.d/yarn.list" in text
    assert "npm install -g @openai/codex@0.149.0" in text


def test_agent_onboarding_seed_runs_per_codespace_and_is_executable() -> None:
    """The seed cannot be baked: it writes per-codespace provider state.

    It also must survive a rebuild, which re-runs ``postCreateCommand`` — that
    is exactly the hook it belongs in.
    """
    data = json.loads(
        (REPO_ROOT / ".devcontainer" / "devcontainer.json").read_text(
            encoding="utf-8"
        )
    )

    assert data["postCreateCommand"] == ".devcontainer/seed-agent-onboarding.sh"
    assert ONBOARDING_SEED.exists()
    assert ONBOARDING_SEED.stat().st_mode & 0o111, "seed script must be executable"


def test_agent_onboarding_seed_creates_only_required_fresh_state(tmp_path: Path) -> None:
    result = _run_onboarding_seed(tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / ".claude.json").read_text()) == {
        "hasCompletedOnboarding": True
    }


def test_agent_onboarding_seed_preserves_existing_state_on_rerun(tmp_path: Path) -> None:
    state = tmp_path / ".claude.json"
    existing = {
        "hasCompletedOnboarding": False,
        "accountUuid": "existing-account",
        "projects": {"/workspaces/issue-orchestrator": {"hasTrustDialogAccepted": True}},
    }
    state.write_text(json.dumps(existing, indent=2))

    result = _run_onboarding_seed(tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text()) == existing


@pytest.mark.parametrize(
    "existing",
    ["{malformed", '["non-object"]'],
    ids=["malformed", "non-object"],
)
def test_agent_onboarding_seed_rejects_invalid_existing_state_without_overwrite(
    tmp_path: Path,
    existing: str,
) -> None:
    state = tmp_path / ".claude.json"
    state.write_text(existing)

    result = _run_onboarding_seed(tmp_path)

    assert result.returncode != 0
    assert state.read_text() == existing


def test_codespaces_doc_requires_provider_bootstrap_compatible_with_readiness() -> None:
    text = (REPO_ROOT / "docs" / "user" / "codespaces.md").read_text(
        encoding="utf-8"
    )

    assert "CLAUDE_CODE_OAUTH_TOKEN" in text
    assert "prompt-free credential\n  path for an unattended Claude Code run" in text
    assert "the following preflight is mandatory" in text
    assert "run `claude`, approve the API\n  key when prompted" in text

    assert "printenv OPENAI_API_KEY | codex login --with-api-key" in text
    assert "codex login status" in text
    assert "CODEX_API_KEY" not in text


def test_codespaces_doc_mentions_secrets_login_and_stable_ports() -> None:
    docs_path = REPO_ROOT / "docs" / "user" / "codespaces.md"

    text = docs_path.read_text(encoding="utf-8")

    assert "Codespaces secret" in text
    assert "codex login" in text
    assert "19080" in text
    assert "19081" in text
    assert "8080" in text
