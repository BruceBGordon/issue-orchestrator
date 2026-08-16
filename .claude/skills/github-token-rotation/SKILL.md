---
name: github-token-rotation
description: Rotate or replace expiring GitHub personal access tokens used by issue-orchestrator. Use when a user receives a GitHub PAT expiration notice, asks how to update issue-orchestrator GitHub auth, needs to update a repo-scoped Keychain token, or needs to distinguish token_env, keyring_service, GitHub CLI auth, and issue-orchestrator auth store behavior.
---

# GitHub Token Rotation

Use this skill to apply the documented GitHub token rotation runbook for a
target repository.

## Workflow

1. Confirm the target repo. If you are in the `issue-orchestrator` checkout,
   switch to the repository being automated before inspecting config.
2. Read [GitHub Token Setup (Developer)](../../../docs/development/GITHUB_TOKEN_SETUP.md#rotate-an-expiring-token).
   Use [GitHub Permissions](../../../docs/user/github-permissions.md) only for
   creating the replacement token and required permissions.
3. Inspect `.issue-orchestrator/config/modes/*/*.yaml` in the target repo and identify
   `repo.github`.
4. Determine the authoritative source:
   - `repo.github.token`: config value wins; avoid committing secrets and ask
     before changing tracked config.
   - `repo.github.token_env`: update the variable in the launching process.
     This overrides Keychain while present.
   - `repo.github.keyring_service` / `keyring_username`: replace that exact OS
     keychain entry. Expand `${USER}` before using it.
   - no repo-scoped auth: follow the global fallback chain in the docs.
5. Help the user update only that source. Never print, log, or commit the token.
6. Run `issue-orchestrator --config <config-path> doctor` from the target repo
   and confirm **Token Sources** and **GitHub Auth** point at the expected repo.
7. Restart any running repository engine or Control Center-launched engine so
   it reloads the new credential.

## Guardrails

- Do not run `issue-orchestrator auth store` for repo-scoped Keychain entries;
  it writes the global fallback service (`issue-orchestrator` /
  `github-token`).
- Do not rotate a live token yourself unless the user has provided the new
  token or explicitly asks you to operate on their machine.
- Do not treat `doctor` passing `/user` auth as enough; it must confirm access
  to the configured `repo.name`.
