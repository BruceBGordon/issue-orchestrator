from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.infra.config import Config


def test_codespaces_config_loads_with_stable_web_ports() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config_path = (
        repo_root
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


def test_main_config_uses_raw_validate_pr_as_publish_gate() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config_path = (
        repo_root
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
    repo_root = Path(__file__).resolve().parents[2]
    devcontainer_path = repo_root / ".devcontainer" / "devcontainer.json"

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
    repo_root = Path(__file__).resolve().parents[2]
    data = json.loads(
        (repo_root / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    )

    assert data["updateContentCommand"] == "make worktree-setup"
    assert "worktree-setup" not in data.get("postCreateCommand", "")
    # postStartCommand runs on EVERY resume, so it must stay empty.
    assert not data.get("postStartCommand")


def test_agent_onboarding_seed_runs_per_codespace_and_is_executable() -> None:
    """The seed cannot be baked: it writes per-codespace provider state.

    It also must survive a rebuild, which re-runs ``postCreateCommand`` — that
    is exactly the hook it belongs in.
    """
    repo_root = Path(__file__).resolve().parents[2]
    data = json.loads(
        (repo_root / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    )

    script = repo_root / ".devcontainer" / "seed-agent-onboarding.sh"
    assert data["postCreateCommand"] == ".devcontainer/seed-agent-onboarding.sh"
    assert script.exists()
    assert script.stat().st_mode & 0o111, "seed script must be executable"


def test_codespaces_doc_does_not_claim_openai_api_key_authenticates_codex() -> None:
    """``OPENAI_API_KEY`` does not authenticate the Codex CLI (#7100).

    The credential chain is ``CODEX_API_KEY`` (exec only) -> ephemeral store ->
    ``CODEX_ACCESS_TOKEN`` -> persisted ``auth.json``. The doc previously
    implied the env var was a usable option, which is a dead end on day one.
    """
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "docs" / "user" / "codespaces.md").read_text(encoding="utf-8")

    assert "does not authenticate the Codex CLI" in text
    assert "CODEX_API_KEY" in text
    # The supported headless Claude credentials must both be named.
    assert "ANTHROPIC_API_KEY" in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text


def test_codespaces_doc_mentions_secrets_login_and_stable_ports() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    docs_path = repo_root / "docs" / "user" / "codespaces.md"

    text = docs_path.read_text(encoding="utf-8")

    assert "Codespaces secret" in text
    assert "codex login" in text
    assert "19080" in text
    assert "19081" in text
    assert "8080" in text
