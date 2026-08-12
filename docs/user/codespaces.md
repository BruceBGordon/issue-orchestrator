# Codespaces

This repo can run in GitHub Codespaces without changing the normal local
`main.yaml` workflow.

## Included setup

The repo now includes:

- `.devcontainer/devcontainer.json`
- `.issue-orchestrator/config/modes/default/z-codespaces.yaml`

The `z-` prefix is intentional so the Codespaces config sorts after the normal
local configs in Control Center lists.

The Codespaces config pins stable repo-engine ports:

- dashboard: `8080`
- control API: `19081`

The Control Center still runs on `19080`.

## Create the codespace

1. Open the repository on GitHub.
2. Use **Code** -> **Codespaces** -> **Create codespace on main**.
3. Wait for `postCreateCommand` to finish. It runs `make worktree-setup`
   and installs the Codex CLI.

## Set required credentials

Before starting the orchestrator, make sure the codespace has the credentials
you actually plan to use:

- `ISSUE_ORCH_GITHUB_TOKEN`

The recommended path is to add this as a GitHub Codespaces secret for the
repository or your account, so each new Codespace gets it automatically.

Provider auth depends on how you use each CLI:

- Claude Code:
  - Authenticate `claude` inside the Codespace using your CLI/subscription
    login.
  - Provider-specific API-key auth is supplied by optional external
    provider-key packages when installed.
- Codex / OpenAI:
  - The devcontainer installs `codex`.
  - Run `codex login` inside the Codespace.
  - Verify with `codex login status`.
  - `OPENAI_API_KEY` is optional if you are using ChatGPT login instead.

Your local desktop login state does not automatically carry over to the remote
Codespace. Provider CLIs need to be authenticated inside the Codespace itself.

## Authenticate Codex in the Codespace

From the integrated terminal:

```bash
codex login
codex login status
```

Expected status for a license-backed flow looks like:

```text
Logged in using ChatGPT
```

## Start the Control Center

From the integrated terminal:

```bash
source .venv/bin/activate
python -m issue_orchestrator.entrypoints.control_center --port 19080 --no-browser
```

Codespaces should auto-forward `19080` and open the Control Center URL in your
browser.

## Start the repo engine with the Codespaces config

In Control Center:

1. Select this repository.
2. Choose `z-codespaces.yaml`.
3. Click `Start engine`.

That config uses stable forwarded ports that match the devcontainer:

- Control Center: `19080`
- Repo engine dashboard: `8080`
- Repo engine control API: `19081`

## Direct start without Control Center

If you want to skip Control Center:

```bash
source .venv/bin/activate
issue-orchestrator --config .issue-orchestrator/config/modes/default/z-codespaces.yaml start --port 8080
```

## Local development

Nothing changes for local Mac development. Keep using:

- `.issue-orchestrator/config/modes/default/main.yaml`
- your existing local startup flow
